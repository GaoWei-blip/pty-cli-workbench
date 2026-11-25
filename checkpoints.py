"""第十五轮 A1：检查点（CLI 自己拍的文件快照）推算每轮改动与差异，以及工作台自己执行的回退代码与撤销。

数据来源全部只读：
- 会话记录 jsonl 里的 file-history-snapshot（每条提问一条：这条提问发出前，所有已跟踪文件各自的备份）
  和 file-history-delta（一轮里第一次改一个没跟踪过的文件时补一条：改前的备份；backupFileName 为 null 表示改前不存在）；
- ~/.claude/file-history/<会话ID>/ 下的整文件备份。
实测（2.1.321）：键 trackingPath 是 CLI 看到的路径（可能是绝对路径，也可能相对工作目录），realParentDir 是文件所在的真实目录，
所以文件的真实路径＝realParentDir/文件名。只被 Bash 改过的文件不在快照里，恢复不了。

规则：
- 只沿当前对话链（parentUuid，压缩边界接 logicalParentUuid）取检查点，CLI 回退对话留下的废弃分支不算。
- “一轮”认真正的提问（你发的消息）；斜杠命令、系统插入的消息也会拍快照：模型在其后回复过（技能类命令、后台任务通知引起的回复）
  才单独算一轮，否则（/context、/export 这类本地命令）不算轮，期间的变化记到前一轮。
- 第 k 轮的改动＝第 k 轮开始前的状态 对比 第 k+1 轮开始前的状态（最后一轮对比磁盘现状）；
  工作台在两轮之间回退过的文件，“这一轮结束时”的状态取回退前存下的内容（否则回退会被算成上一轮的改动）。
- 回退只写快照里跟踪过的文件，路径必须落在快照记录的真实目录下；写之前把将被覆盖或删除的文件现状存到 .run/rewind/<id>/ 供撤销。
出错只影响本功能：接口抛 ValueError 给出中文说明，解析异常的行直接跳过。
"""
import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime

NS = {}  # server 的全局变量（register 时传入）
FH_ROOT = os.path.join(os.path.expanduser('~'), '.claude', 'file-history')
REWIND_DIR = ''  # register 时设为 <RUN_DIR>/rewind
PLANS_DIR = os.path.join(os.path.expanduser('~'), '.claude', 'plans')  # 修复线：计划模式写的计划文件不算改动、不回退
FORKS_FILE = ''  # 修复线：分叉记录，默认 <RUN_DIR>/forks.json
KEEP_FORKS = 500
KEEP_REWINDS = 60
DIFF_CAP = 2_000_000  # 超过这个字节数的文件不算行数
SID_RE = re.compile(r'^[0-9a-fA-F-]{36}$')
UUID_RE = re.compile(r'^[0-9a-fA-F-]{36}$')
RID_RE = re.compile(r'^\d{14}-[0-9a-f]{6}$')
BK_RE = re.compile(r'^[\w.@-]{1,120}$')
# 不在消息链上、又可能很大的记录类型：不解析
SKIP_RE = re.compile(rb'^\{"type":"(?:api-request[\w-]*|tools-used|mode|permission-mode|atis-latch|last-prompt|ai-title|custom-title|'
                     rb'queue-operation|cost-state|agent-name)"')
LOCK = threading.Lock()
HIST = {}  # 记录文件路径 → History
STATS = {}  # (前, 后) 内容标识 → (增, 删, 二进制)
CALC = threading.RLock()  # 检查点的读取、推算、回退、撤销串行执行（都很快），避免并发读写同一份解析结果


def log(*a):
    f = NS.get('log')
    if f:
        f(*a)


def ts_of(v):
    if isinstance(v, (int, float)):
        return float(v) / (1000.0 if v > 1e11 else 1.0)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return '\n'.join(b.get('text') or '' for b in c if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def classify(r):
    """user 记录是什么：'prompt' 你发的提问；'cmd' 斜杠命令；'sys' 系统插入（后台任务通知等）；None 不是提问。"""
    if r.get('isMeta') or r.get('isCompactSummary'):
        return None
    c = (r.get('message') or {}).get('content')
    if isinstance(c, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c):
        return None
    o = r.get('origin')
    kind = o.get('kind') if isinstance(o, dict) else None
    if (kind and kind != 'human') or r.get('promptSource') == 'system':
        return 'sys'
    t = text_of(c).lstrip()
    if '<command-name>' in t or t.startswith('<bash-input>'):
        return 'cmd'
    if t.startswith(('<local-command-', '[Request interrupted', '<bash-stdout>', '<bash-stderr>')):
        return None
    if not t and not (isinstance(c, list) and any(isinstance(b, dict) and b.get('type') == 'image' for b in c)):
        return None
    return 'prompt'


def label_of(r):
    t = text_of((r.get('message') or {}).get('content'))
    t = re.sub(r'<([a-z][\w-]*)>.*?</\1>', '', t, flags=re.S)
    return ' '.join(t.split())[:60]


def good_backup(b):
    if not isinstance(b, dict) or not isinstance(b.get('realParentDir'), str):
        return False
    n = b.get('backupFileName')
    return n is None or (isinstance(n, str) and BK_RE.match(n) and not n.startswith('.'))


# ---------------------------------------------------------------- 增量读取会话记录
class History:
    """一个会话记录文件里和检查点有关的部分，按字节偏移增量读；文件变短（被改写）就重读。"""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        self._reset()

    def _reset(self):
        self.off, self.buf, self.ino = 0, b'', None
        self.parent, self.last, self.users, self.asst = {}, None, {}, set()
        self.snaps, self.deltas, self.touch = {}, {}, {}
        self.cwd, self.ver = '', 0

    def update(self):
        with self.lock:
            try:
                st = os.stat(self.path)
            except OSError:
                return self
            if st.st_size < self.off or (self.ino is not None and st.st_ino != self.ino):
                self._reset()
            self.ino = st.st_ino
            if st.st_size == self.off:
                return self
            with open(self.path, 'rb') as f:
                f.seek(self.off)
                chunk = f.read(st.st_size - self.off)
            self.off += len(chunk)
            lines = (self.buf + chunk).split(b'\n')
            self.buf = lines.pop()
            for line in lines:
                if not line.strip() or SKIP_RE.match(line):
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict):
                    try:
                        self._row(r)
                    except Exception:
                        pass
            self.ver += 1
            return self

    def _row(self, r):
        t = r.get('type')
        if t == 'file-history-snapshot':
            mid, snap = r.get('messageId'), r.get('snapshot') or {}
            files = snap.get('trackedFileBackups') if isinstance(snap, dict) else None
            if mid and isinstance(files, dict):
                d = self.snaps.setdefault(mid, {})
                for k, b in files.items():
                    if isinstance(k, str) and good_backup(b):
                        d[k] = b
            return
        if t == 'file-history-delta':
            sm, k, b = r.get('snapshotMessageId'), r.get('trackingPath'), r.get('backup')
            if sm and isinstance(k, str) and good_backup(b):
                self.deltas.setdefault(sm, []).append((k, b))
            return
        u = r.get('uuid')
        if not u or r.get('isSidechain'):
            return
        self.parent[u] = r.get('parentUuid') or r.get('logicalParentUuid')
        self.last = u
        if r.get('cwd'):
            self.cwd = r['cwd']
        if t == 'assistant':
            self.asst.add(u)
        elif t == 'user':
            kind = classify(r)
            if kind:
                self.users[u] = {'kind': kind, 'label': label_of(r), 't': ts_of(r.get('timestamp'))}
            tur = r.get('toolUseResult')
            if isinstance(tur, dict) and isinstance(tur.get('filePath'), str):
                p = real_of_path(tur['filePath'], self.cwd)
                self.touch[p] = max(self.touch.get(p, 0.0), ts_of(r.get('timestamp')))

    def chain(self):
        seq, seen, u = [], set(), self.last
        while u and u not in seen:
            seen.add(u)
            seq.append(u)
            u = self.parent.get(u)
        seq.reverse()
        return seq


def real_of_path(p, cwd):
    p = p if os.path.isabs(p) else os.path.join(cwd or '/', p)
    try:
        return os.path.realpath(p)
    except Exception:
        return os.path.normpath(p)


def history(path):
    with LOCK:
        h = HIST.get(path)
        if not h:
            if len(HIST) > 24:
                HIST.pop(next(iter(HIST)))
            h = HIST[path] = History(path)
    return h.update()


# ---------------------------------------------------------------- 由记录推算检查点
def target_path(key, b):
    """快照里一项对应的真实路径；不安全（不落在记录的真实目录下）返回 None。"""
    rpd = b.get('realParentDir') or ''
    if not os.path.isabs(rpd) or os.path.normpath(rpd) != rpd.rstrip('/') or '/../' in rpd + '/':
        return None
    rpd = rpd.rstrip('/') or '/'
    k = os.path.normpath(key)
    base = os.path.basename(k)
    if not base or base in ('.', '..'):
        return None
    d = os.path.dirname(k)
    if os.path.isabs(k) or not d or rpd.endswith('/' + d):
        p = os.path.join(rpd, base)
    else:  # 旧格式：realParentDir 是工作目录、键是相对路径
        p = os.path.normpath(os.path.join(rpd, k))
    if not p.startswith(rpd.rstrip('/') + '/') or '/../' in p:
        return None
    if plan_file(p):  # 修复线：计划文件不进改动胶囊、审阅列表，也不回退
        return None
    return p


_PLANS = {}  # (计划文件夹, 目录) → 是否在计划文件夹里


def plan_file(p):
    """~/.claude/plans/ 下的文件（计划模式写的计划）。按真实路径判断：目录和计划文件夹都解析符号链接后再比。"""
    d = os.path.dirname(p)
    key = (PLANS_DIR, d)
    v = _PLANS.get(key)
    if v is None:
        roots = {PLANS_DIR.rstrip('/')}
        try:
            roots.add(os.path.realpath(PLANS_DIR).rstrip('/'))
            rd = os.path.realpath(d)
        except Exception:
            rd = d
        v = any(x == r or x.startswith(r + '/') for x in (d, rd) for r in roots if r)
        if len(_PLANS) > 4000:
            _PLANS.clear()
        _PLANS[key] = v
    return v


# ---------------------------------------------------------------- 分叉记录（修复线）
# 分叉对话里，分叉前（含分叉点那一轮）的各轮继承自原对话：不显示改动、不能回退，只对分叉之后新发生的轮计算。
# 分叉时记下时间：提问时间早于它的轮就是继承来的（“从这里分叉”和整段分叉都保留原记录的时间戳，实测 2.1.321）。
FORK_LOCK = threading.Lock()
_FORKS = {'key': None, 'd': {}}


def forks_path():
    return FORKS_FILE or os.path.join(NS.get('RUN_DIR') or '/tmp', 'forks.json')


def _forks():
    p = forks_path()
    try:
        st = os.stat(p)
    except OSError:
        return {}
    key = (p, st.st_mtime_ns, st.st_size)
    if _FORKS['key'] != key:
        try:
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            d = {}
        _FORKS['key'], _FORKS['d'] = key, d if isinstance(d, dict) else {}
    return _FORKS['d']


def note_fork(new, src, at=None):
    """记下一次分叉（在新对话写出任何记录之前调用）。出错只记日志，不影响分叉本身。"""
    try:
        with FORK_LOCK:
            d = dict(_forks())
            d[new] = {'src': src, 'at': at, 't': int(time.time() * 1000)}
            if len(d) > KEEP_FORKS:
                d = dict(sorted(d.items(), key=lambda kv: (kv[1] or {}).get('t') or 0)[-KEEP_FORKS:])
            p = forks_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, p)
    except Exception as e:
        log('记录分叉失败', repr(e))


def fork_info(sid):
    """这个对话是分叉出来的：{src, at, t}；不是或读不到返回 None。"""
    try:
        v = _forks().get(sid)
        return v if isinstance(v, dict) and isinstance(v.get('t'), (int, float)) else None
    except Exception:
        return None


class Plan:
    """当前对话链上的检查点。groups[i] = 第 i 轮（真正的提问）及其后、下一个提问之前的检查点。"""

    def __init__(self, h, sid):
        self.h, self.sid = h, sid
        self.fh = os.path.join(FH_ROOT, sid)
        cps = []  # [uuid, 类别, 其后模型回复过]
        for u in h.chain():
            if u in h.users and u in h.snaps:
                cps.append([u, h.users[u]['kind'], False])
            elif cps and u in h.asst:
                cps[-1][2] = True
        self.turns, self.groups, self.kinds, pre = [], [], [], []
        for u, kind, replied in cps:
            if kind == 'prompt' or replied:
                self.turns.append(u)
                self.kinds.append(kind)
                self.groups.append([u])
            elif self.groups:
                self.groups[-1].append(u)
            else:
                pre.append(u)
        self.tables = [self._table(g) for g in self.groups]
        self.pre = self._table(pre)
        self.idx = {u: i for i, u in enumerate(self.turns)}
        self.cwd = h.cwd
        self.base = os.path.realpath(self.cwd).rstrip('/') if self.cwd else ''
        # 修复线：分叉对话里前 first 轮继承自原对话（提问早于分叉时间），不算改动、不能回退
        self.first = 0
        fi = fork_info(sid)
        if fi:
            ft = fi['t'] / 1000.0
            while self.first < len(self.turns) and (h.users[self.turns[self.first]]['t'] or 0) < ft:
                self.first += 1

    def inherited(self, i):
        return 0 <= i < self.first

    def _table(self, group):
        m = {}
        for u in group:
            for k, b in self.h.snaps.get(u, {}).items():
                m.setdefault(k, b)
            for k, b in self.h.deltas.get(u, []):
                m.setdefault(k, b)
        return m

    def state_before(self, i):
        """第 i 轮开始前各跟踪文件应有的样子：{键: 备份}。之后才开始跟踪的文件取它最早的备份。i 等于轮数时表示“现在”（返回 None）。"""
        if i >= len(self.tables):
            return None
        out = dict(self.tables[i])
        for t in self.tables[i + 1:]:
            for k, b in t.items():
                out.setdefault(k, b)
        return out

    def earliest(self):
        out = dict(self.pre) if not self.first else {}  # 分叉对话：从分叉后第一轮开始前算起
        for t in self.tables[self.first:]:
            for k, b in t.items():
                out.setdefault(k, b)
        return out

    def rel(self, p, key=None):
        """显示用路径：在对话文件夹里的给相对路径，其余给 CLI 看到的路径（家目录缩成 ~）。"""
        base = self.base
        if base and p.startswith(base + '/'):
            return p[len(base) + 1:]
        q = key if key and os.path.isabs(key) else p
        home = os.path.expanduser('~')
        return '~' + q[len(home):] if q.startswith(home + '/') else q

    def show_path(self, key, real):
        return key if os.path.isabs(key) else real

    def turn_time(self, i):
        if 0 <= i < len(self.turns):
            return self.h.users[self.turns[i]]['t']
        return None


# ---------------------------------------------------------------- 内容与行数
def backup_path(fh, b):
    n = b.get('backupFileName')
    return os.path.join(fh, n) if n else None


class Missing(Exception):
    pass


def read_backup(fh, b):
    """备份内容：None 表示当时不存在；备份文件不在了抛 Missing。"""
    p = backup_path(fh, b)
    if p is None:
        return None
    try:
        with open(p, 'rb') as f:
            return f.read()
    except OSError:
        raise Missing(p)


def read_disk(p):
    try:
        if not os.path.isfile(p):
            return None
        with open(p, 'rb') as f:
            return f.read()
    except OSError:
        return None


def disk_id(p):
    try:
        st = os.lstat(p)
        return ('disk', p, st.st_mtime_ns, st.st_size)
    except OSError:
        return ('disk', p, None, None)


def line_stats(a, b):
    """(增, 删, 是否二进制)。a/b 为 None 表示不存在。"""
    a, b = a or b'', b or b''
    if b'\0' in a[:8000] or b'\0' in b[:8000]:
        return 0, 0, True
    if len(a) > DIFF_CAP or len(b) > DIFF_CAP:
        return None, None, False
    x = a.decode('utf-8', 'replace').splitlines()
    y = b.decode('utf-8', 'replace').splitlines()
    i = 0
    while i < len(x) and i < len(y) and x[i] == y[i]:
        i += 1
    j = 0
    while j < len(x) - i and j < len(y) - i and x[-1 - j] == y[-1 - j]:
        j += 1
    x, y = x[i:len(x) - j], y[i:len(y) - j]
    if len(x) > 6000 and len(y) > 6000:
        return len(y), len(x), False
    add = dele = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
        if tag != 'equal':
            dele += i2 - i1
            add += j2 - j1
    return add, dele, False


def compare(bid, aid, bget, aget):
    """按内容标识缓存比较结果：None 表示内容相同，否则 (状态, 增, 删, 二进制)。备份缺失抛 Missing（不缓存）。"""
    key = (bid, aid)
    if key in STATS:
        return STATS[key]
    bc, ac = bget(), aget()
    if bc == ac:
        v = None
    else:
        st = 'new' if bc is None else 'del' if ac is None else 'mod'
        v = (st,) + line_stats(bc, ac)
    if len(STATS) > 4000:
        STATS.clear()
    STATS[key] = v
    return v


# ---------------------------------------------------------------- 工作台自己的回退记录
def rewind_dir():
    return REWIND_DIR or os.path.join(NS.get('RUN_DIR') or '/tmp', 'rewind')


def load_rewinds(sid):
    out = []
    d = rewind_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if not RID_RE.match(n):
            continue
        try:
            with open(os.path.join(d, n, 'meta.json'), encoding='utf-8') as f:
                m = json.load(f)
        except Exception:
            continue
        if isinstance(m, dict) and m.get('sid') == sid:
            out.append(m)
    out.sort(key=lambda m: m.get('t') or 0)
    return out


def saved_content(m, ent):
    """回退时存下的“回退前”内容；None 表示回退前不存在。"""
    if not ent.get('existed'):
        return None
    if not re.match(r'^f\d+$', str(ent.get('saved') or '')):
        raise OSError('回退记录损坏')
    with open(os.path.join(rewind_dir(), m['id'], ent['saved']), 'rb') as f:
        return f.read()


def after_override(rws, t0, t1):
    """t0 之后、t1 之前（t1 为 None 表示不限）的第一次没撤销的回退：{真实路径: (标识, 取内容函数)}。"""
    out = {}
    for m in rws:
        t = (m.get('t') or 0) / 1000.0
        if m.get('undone') or t <= t0 or (t1 is not None and t >= t1):
            continue
        for ent in m.get('files') or []:
            p = ent.get('path')
            if p and p not in out:
                out[p] = (('saved', m['id'], ent.get('saved')), (lambda m=m, ent=ent: saved_content(m, ent)))
    return out


# ---------------------------------------------------------------- 每轮改动
def turn_files(pl, i, rws, now_only=False):
    """第 i 轮改了哪些文件：[(键, 真实路径, 状态, 增, 删, 前备份)]，以及是否有快照缺失。"""
    before = pl.tables[i]
    last = i == len(pl.turns) - 1
    after = None if last else pl.tables[i + 1]
    ov = after_override(rws, pl.turn_time(i) or 0, None if last else pl.turn_time(i + 1))
    out, expired = [], False
    for k, b in before.items():
        p = target_path(k, b)
        if not p:
            continue
        if p in ov:
            aid, aget = ov[p]
        elif after is not None:
            ab = after.get(k)
            if ab is None:
                continue
            if ab.get('backupFileName') == b.get('backupFileName'):
                continue
            aid, aget = ('bk', ab.get('backupFileName')), (lambda ab=ab: read_backup(pl.fh, ab))
        else:
            aid, aget = disk_id(p), (lambda p=p: read_disk(p))
        bid = ('bk', b.get('backupFileName'))
        if aid == bid:
            continue
        try:
            v = compare(bid, aid, lambda b=b: read_backup(pl.fh, b), aget)
        except Missing:
            expired = True
            continue
        except OSError:
            continue
        if v is None:
            continue
        st, add, dele, binary = v
        out.append({'path': pl.show_path(k, p), 'real': p, 'rel': pl.rel(p, k), 'st': st, 'add': add, 'del': dele,
                    'binary': binary})
    out.sort(key=lambda f: f['rel'])
    return out, expired


def summarize(files):
    return {'n': len(files), 'add': sum(f['add'] or 0 for f in files), 'del': sum(f['del'] or 0 for f in files)}


def all_files(pl):
    out, expired = [], False
    for k, b in pl.earliest().items():
        p = target_path(k, b)
        if not p:
            continue
        try:
            v = compare(('bk', b.get('backupFileName')), disk_id(p), lambda b=b: read_backup(pl.fh, b), lambda p=p: read_disk(p))
        except Missing:
            expired = True
            continue
        if v is None:
            continue
        st, add, dele, binary = v
        out.append({'path': pl.show_path(k, p), 'real': p, 'rel': pl.rel(p, k), 'st': st, 'add': add, 'del': dele, 'binary': binary})
    out.sort(key=lambda f: f['rel'])
    return out, expired


class NoTranscript(ValueError):
    pass


def plan_for(sid, path=None):
    if not SID_RE.match(sid or ''):
        raise ValueError('会话 ID 不对')
    path = path or (NS['find_transcript'](sid) if NS.get('find_transcript') else None)
    if not path or not os.path.isfile(path):
        raise NoTranscript('这个对话还没有记录')
    return Plan(history(path), sid)


def turn_changes(sid, path=None):
    with CALC:
        return _turn_changes(sid, path)


def _turn_changes(sid, path):
    try:
        pl = plan_for(sid, path)
    except NoTranscript:  # 新对话还没有记录：空结果
        return {'turns': [], 'all': {'n': 0, 'add': 0, 'del': 0, 'files': [], 'expired': False}, 'rewinds': [], 'bashNote': True, 'inherited': []}
    rws = load_rewinds(sid)
    turns = []
    for i, u in enumerate(pl.turns):
        if pl.inherited(i):  # 修复线：继承自原对话的轮不算改动
            continue
        files, expired = turn_files(pl, i, rws)
        if not files:
            continue
        turns.append(dict(summarize(files), uuid=u, idx=i + 1, kind=pl.kinds[i], files=files, expired=expired, why='gone' if expired else None))
    af, aexp = all_files(pl)
    # 修复线：inherited 是继承自原对话的各轮（含分叉点那一轮）的 uuid，不可回退，原因 why='fork'；expired 的轮 why='gone'（快照已清理）
    return {'turns': turns, 'all': dict(summarize(af), files=af, expired=aexp, why='gone' if aexp else None),
            'inherited': pl.turns[:pl.first],
            'rewinds': [{'id': m['id'], 'uuid': m.get('uuid'), 'after': bool(m.get('after')), 't': m.get('t'), 'label': m.get('label') or '',
                         'written': m.get('written', 0), 'removed': m.get('removed', 0), 'undone': bool(m.get('undone'))} for m in rws],
            'bashNote': True}


def unified(a, b, rel, ctx=3):
    if a is not None and b'\0' in a[:8000] or b is not None and b'\0' in b[:8000]:
        return {'diff': '', 'binary': True}
    x = (a or b'').decode('utf-8', 'replace').split('\n')
    y = (b or b'').decode('utf-8', 'replace').split('\n')
    if a is None or a == b'':
        x = []
    if b is None or b == b'':
        y = []
    if x and x[-1] == '' and len(x) > 1:
        x = x[:-1]
    if y and y[-1] == '' and len(y) > 1:
        y = y[:-1]
    lines = list(difflib.unified_diff(x, y, 'a/' + rel, 'b/' + rel, n=ctx, lineterm=''))
    return {'diff': '\n'.join(lines)[:800_000], 'binary': False}


def turn_diff(sid, uuid, path, path_tx=None, ctx=3):
    with CALC:
        return _turn_diff(sid, uuid, path, path_tx, ctx)


def _turn_diff(sid, uuid, path, path_tx, ctx):
    pl = plan_for(sid, path_tx)
    want = os.path.realpath(path) if path else ''

    def match(k, b):
        p = target_path(k, b)
        return p and (p == want or k == path or os.path.realpath(p) == want)
    if uuid == '*':
        for k, b in pl.earliest().items():
            if match(k, b):
                p = target_path(k, b)
                try:
                    bc = read_backup(pl.fh, b)
                except Missing:
                    return {'diff': '', 'binary': False, 'expired': True}
                ac = read_disk(p)
                return dict(unified(bc, ac, pl.rel(p, k), ctx), new=bc is None, deleted=ac is None)
        raise ValueError('这个文件不在本对话的改动里')
    i = pl.idx.get(uuid)
    if i is None:
        raise ValueError('找不到这一轮的快照')
    if pl.inherited(i):
        raise ValueError('这一轮在原对话里，这里看不了它的改动')
    rws = load_rewinds(sid)
    last = i == len(pl.turns) - 1
    ov = after_override(rws, pl.turn_time(i) or 0, None if last else pl.turn_time(i + 1))
    for k, b in pl.tables[i].items():
        if not match(k, b):
            continue
        p = target_path(k, b)
        try:
            bc = read_backup(pl.fh, b)
            if p in ov:
                ac = ov[p][1]()
            elif last:
                ac = read_disk(p)
            else:
                ab = pl.tables[i + 1].get(k)
                ac = read_backup(pl.fh, ab) if ab else read_disk(p)
        except Missing:
            return {'diff': '', 'binary': False, 'expired': True}
        return dict(unified(bc, ac, pl.rel(p, k), ctx), new=bc is None, deleted=ac is None)
    raise ValueError('这个文件不在这一轮的改动里')


# ---------------------------------------------------------------- 回退：预览、执行、撤销
def known_times(pl, rws):
    """CLI（或工作台的回退）最后一次知道各文件内容的时间：最新备份时间、最后一次编辑工具结果、最后一次回退写入。"""
    out = dict(pl.h.touch)
    seen = set()
    for tb in [pl.pre] + pl.tables:
        for k, b in tb.items():
            if (k, b.get('backupFileName')) in seen:
                continue
            seen.add((k, b.get('backupFileName')))
            p = target_path(k, b)
            if p:
                out[p] = max(out.get(p, 0.0), ts_of(b.get('backupTime')))
    for m in rws:
        # 第十五轮联调：撤销也是工作台自己写回的（undone 记的是撤销时间，毫秒），不算“被别的程序改过”
        u = m.get('undone')
        if u and not (isinstance(u, (int, float)) and not isinstance(u, bool) and u > 1e11):
            continue  # 旧数据只记了“已撤销”，没有时间
        t = (u if u else m.get('t') or 0) / 1000.0
        for e in m.get('files') or []:
            if e.get('path'):
                out[e['path']] = max(out.get(e['path'], 0.0), t)
    return out


def is_busy(sid):
    rs = NS.get('RUNNERS') or {}
    r = rs.get(sid)
    if r is not None and r.state.get('alive'):
        try:
            att = r.attention()
        except Exception:
            att = False
        if r.turn or r.state.get('busy') or att or r.queue or r.sending:
            return True
    return False


def compute(sid, uuid, after=False, path_tx=None):
    """回退到 uuid 这一轮开始前（after 为真：这一轮结束时）。返回 (plan, 动作列表, 缺失列表)。
    动作：{key, real, op: 'write'|'remove', bk: 备份}。"""
    if not UUID_RE.match(uuid or ''):
        raise ValueError('回退的位置不对')
    pl = plan_for(sid, path_tx)
    i = pl.idx.get(uuid)
    if i is None:
        raise ValueError('这一轮没有快照，不能回退')
    if pl.inherited(i):  # 修复线：分叉前的轮（含分叉点那一轮）在原对话里，这里不回退
        raise ValueError('这一轮在原对话里，不能在这里回退')
    tgt = pl.state_before(i + 1 if after else i)
    acts, missing = [], []
    if tgt is None:
        return pl, acts, missing
    for k, b in tgt.items():
        p = target_path(k, b)
        if not p or os.path.islink(p) or os.path.isdir(p):
            continue
        cur = read_disk(p)
        try:
            want = read_backup(pl.fh, b)
        except Missing:
            missing.append({'key': k, 'real': p})
            continue
        if want == cur:
            continue
        acts.append({'key': k, 'real': p, 'op': 'remove' if want is None else 'write', 'bk': b, 'want': want, 'cur': cur})
    acts.sort(key=lambda a: a['real'])
    return pl, acts, missing


def preview(sid, uuid, after=False, path_tx=None):
    with CALC:
        return _preview(sid, uuid, after, path_tx)


def _preview(sid, uuid, after, path_tx):
    pl, acts, missing = compute(sid, uuid, after, path_tx)
    kt = known_times(pl, load_rewinds(sid)) if acts else {}
    write, remove, conflicts = [], [], []
    for a in acts:
        p = a['real']
        ent = {'path': pl.show_path(a['key'], p), 'rel': pl.rel(p, a['key'])}
        if a['op'] == 'write':
            add, dele, _ = line_stats(a['cur'], a['want'])
            write.append(dict(ent, add=add, **{'del': dele}))
        else:
            remove.append(ent)
        if a['cur'] is not None:
            try:
                mt = os.stat(p).st_mtime
            except OSError:
                mt = 0
            if mt > kt.get(p, 0.0) + 2:
                conflicts.append(ent)
    return {'write': write, 'remove': remove, 'conflicts': conflicts,
            'missing': [{'path': pl.show_path(m['key'], m['real']), 'rel': pl.rel(m['real'], m['key'])} for m in missing],
            'busy': is_busy(sid)}


def _safe_dir(p):
    d = os.path.dirname(p)
    return os.path.realpath(d) == d


def _write_atomic(p, data, mode=None):
    d = os.path.dirname(p)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, '.%s.cwb-rewind-%s.tmp' % (os.path.basename(p)[:80], secrets.token_hex(4)))
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
        if mode is not None:
            os.chmod(tmp, mode & 0o7777)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _new_id():
    return time.strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(3)


def _prune():
    d = rewind_dir()
    try:
        ids = sorted(n for n in os.listdir(d) if RID_RE.match(n))
    except OSError:
        return
    for n in ids[:-KEEP_REWINDS]:
        x = os.path.join(d, n)
        try:
            for f in os.listdir(x):
                if re.match(r'^(f\d+|meta\.json)$', f):
                    os.remove(os.path.join(x, f))
            os.rmdir(x)
        except OSError:
            pass


def _save_meta(m):
    p = os.path.join(rewind_dir(), m['id'], 'meta.json')
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(m, f, ensure_ascii=False)
    os.replace(tmp, p)


def apply(sid, uuid, after=False, path_tx=None):
    if is_busy(sid):
        raise ValueError('对话正在回复或在等你确认，等这一轮结束后再回退')
    with CALC:
        pl, acts, missing = compute(sid, uuid, after, path_tx)
        skipped = [pl.rel(a['real'], a['key']) for a in acts if not _safe_dir(a['real'])]
        acts = [a for a in acts if _safe_dir(a['real'])]
        miss = [{'path': pl.show_path(x['key'], x['real']), 'rel': pl.rel(x['real'], x['key'])} for x in missing]
        if not acts:  # 已经是目标状态：不留回退记录
            return {'ok': True, 'id': None, 'written': 0, 'removed': 0, 'skipped': skipped, 'missing': miss}
        rid = _new_id()
        base = os.path.join(rewind_dir(), rid)
        os.makedirs(base, exist_ok=True)
        files = []
        # 先把要动的文件现状全部存下来，存不了就什么都不动
        for n, a in enumerate(acts):
            ent = {'path': a['real'], 'key': a['key'], 'rel': pl.rel(a['real'], a['key']), 'op': a['op'], 'existed': a['cur'] is not None,
                   'saved': None, 'mode': None, 'wrote': None}
            if a['cur'] is not None:
                ent['saved'] = 'f%d' % n
                with open(os.path.join(base, ent['saved']), 'wb') as f:
                    f.write(a['cur'])
                try:
                    ent['mode'] = os.stat(a['real']).st_mode & 0o7777
                except OSError:
                    pass
            if a['op'] == 'write':
                ent['wrote'] = hashlib.sha256(a['want']).hexdigest()
            files.append(ent)
        lbl = pl.h.users.get(uuid, {}).get('label') or ''
        m = {'id': rid, 'sid': sid, 'uuid': uuid, 'after': bool(after), 't': int(time.time() * 1000), 'label': lbl, 'files': files,
             'written': sum(1 for e in files if e['op'] == 'write'), 'removed': sum(1 for e in files if e['op'] == 'remove'),
             'missing': [x['real'] for x in missing], 'undone': False}
        _save_meta(m)
        done = []
        try:
            for a, ent in zip(acts, files):
                if a['op'] == 'write':
                    _write_atomic(a['real'], a['want'], ent['mode'] if ent['mode'] is not None else 0o644)
                else:
                    os.remove(a['real'])
                done.append((a, ent))
        except OSError as e:
            for a, ent in reversed(done):  # 半途失败：把已经动过的改回去
                try:
                    if ent['existed']:
                        _write_atomic(a['real'], a['cur'], ent['mode'])
                    elif os.path.exists(a['real']):
                        os.remove(a['real'])
                except OSError:
                    pass
            m['undone'], m['failed'] = int(time.time() * 1000), str(e)
            _save_meta(m)
            raise ValueError('回退没有完成，已改回原样：' + str(e))
        _prune()
        log('回退代码', sid[:8], uuid[:8], '结束时' if after else '之前', '写回', m['written'], '删除', m['removed'], '缺失', len(missing))
        return {'ok': True, 'id': rid, 'written': m['written'], 'removed': m['removed'], 'skipped': skipped, 'missing': miss}


def undo(rid):
    if not RID_RE.match(rid or ''):
        raise ValueError('找不到这次回退')
    try:
        with open(os.path.join(rewind_dir(), rid, 'meta.json'), encoding='utf-8') as f:
            m = json.load(f)
    except Exception:
        raise ValueError('找不到这次回退')
    if m.get('undone'):
        raise ValueError('这次回退已经撤销过了')
    if is_busy(m.get('sid') or ''):
        raise ValueError('对话正在回复或在等你确认，等这一轮结束后再撤销')
    restored, skipped = 0, []
    with CALC:
        for ent in m.get('files') or []:
            p = ent.get('path') or ''
            if not os.path.isabs(p) or not _safe_dir(p) or os.path.islink(p) or os.path.isdir(p):
                skipped.append(ent.get('rel') or p)
                continue
            cur = read_disk(p)
            if ent['op'] == 'write':
                same = cur is not None and hashlib.sha256(cur).hexdigest() == ent.get('wrote')
            else:
                same = cur is None
            if not same:  # 回退之后又被改过：不盖掉
                skipped.append(ent.get('rel') or p)
                continue
            try:
                if ent.get('existed'):
                    _write_atomic(p, saved_content(m, ent), ent.get('mode') if ent.get('mode') is not None else 0o644)
                elif os.path.exists(p):
                    os.remove(p)
                restored += 1
            except OSError:
                skipped.append(ent.get('rel') or p)
        m['undone'] = int(time.time() * 1000)
        m['skipped'] = skipped
        _save_meta(m)
    log('撤销回退', rid, '恢复', restored, '跳过', len(skipped))
    return {'ok': True, 'restored': restored, 'skipped': skipped}


# ---------------------------------------------------------------- 接口
def _sid(b):
    sid = b.get('sid') or ''
    f = NS.get('resolve_sid')
    return f(sid) if f else sid


def api_turn_changes(b):
    return turn_changes(_sid(b))


def api_turn_diff(b):
    ctx = b.get('ctx')
    ctx = max(0, min(int(ctx), 100000)) if isinstance(ctx, int) else 3
    if b.get('full'):  # 联调：F2 传 full=true 要全文上下文，未改动的大段由前端折叠
        ctx = 100000
    return turn_diff(_sid(b), str(b.get('uuid') or ''), str(b.get('path') or ''), ctx=ctx)


def api_rewind_preview(b):
    return preview(_sid(b), str(b.get('uuid') or ''), bool(b.get('after')))


def api_rewind_apply(b):
    sid = _sid(b)
    r = apply(sid, str(b.get('uuid') or ''), bool(b.get('after')))
    bus = NS.get('BUS')
    if bus and r.get('id'):
        bus.emit({'type': 'rewind', 'sid': sid, 'id': r['id']})
    return r


def api_rewind_undo(b):
    return undo(str(b.get('id') or ''))
