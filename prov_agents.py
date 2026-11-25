"""11-23：Codex、Antigravity 对话的子代理，接到右栏“任务”页（和 Claude 子代理同一套卡片、“查看过程”、“在对话中定位”）。

Claude 的子代理由 server.api_agents 读 <会话>/subagents/agent-*.jsonl；这两家以前回空表（providers.CLAUDE_ONLY），
Codex 派 spawn_agent、Antigravity 派 invoke_subagent 时右栏一直是“这个对话还没有子代理或后台命令”。这里读两家自己的子代理记录：

Codex（多代理，spawn_agent）：每个子代理是一条独立线程，记录在 <CODEX_HOME>/sessions/年/月/日/rollout-*.jsonl（归档的在 archived_sessions）。
  第一行 session_meta 带 parent_thread_id、agent_path（/root/任务名）、agent_nickname、agent_role；v2 子线程先抄一份父线程历史，
  subagent_history_start_ordinal 之后才是它自己的。父线程发来的任务正文 Codex 加密保存（encrypted_content），读不到原文。
  状态口径移植自 T3 CodexAdapterV2（registerSubagentActivity：completed → 完成，interrupted → 停止）：子线程最后一次是 task_complete → 已完成，
  turn_aborted → 已停止；还在一轮里的看记录多久没更新（和 Claude 子代理同一个口径：父对话连着 15 分钟、没连着 3 分钟，超过算“未完成”）。
Antigravity（agy 1.5，invoke_subagent 启动后脱离父对话独立运行，调用本身马上就“完成”）：移植自 T3 AntigravityChildWork.ts——
  <数据目录>/brain/<父>/.system_generated/subagents/<子>.json：角色、类型、状态（SUBAGENT_STATE_KILLED＝被停掉）；
  <数据目录>/brain/<子>/.system_generated/messages/*.json：父对话发来的启动消息（sourceMetadata.tool.toolCall.argumentsJson 里的 Prompt、Role）；
  父对话收件箱里有这个子代理发来的消息＝它交回了结果（T3 readAntigravityChildrenAtWork 的判断）；
  <数据目录>/brain/<子>/.system_generated/logs/transcript.jsonl：过程（每行一步：PLANNER_RESPONSE 带思考、正文、tool_calls，随后各步依次是工具结果）。
偏离 T3（工作台已有链路）：T3 把子代理做成子线程、实时收子线程事件；工作台沿用 Claude 子代理的做法，从磁盘记录读，
服务重启、历史对话都能看。只读，不写两家的任何文件。

复核修正（同日，独立复核）：
- Codex 几天前的记录会压缩成 .jsonl.zst（archived_sessions 里全是），也读（Python 3.14 标准库 compression.zstd，压缩的不会再变，读一次）；
  同一线程既有 .jsonl 又有 .zst 时用 .jsonl。
- 只认 source.subagent.thread_spawn 派出来的线程（T3 CodexAdapterV2 同样只认 thread_spawn）；自动审批的 guardian、review、compact 等线程
  也带 parent_thread_id，但不是子代理，不列。
- 代码模式 exec 外壳只在里面真有对应操作时隐藏，不连带藏掉同时在跑的 spawn_agent、wait_agent 等。
- Antigravity 工具结果按 step_index 配对（规划步 s 的第 i 个调用 → 第 s+i 步；本机 1128 个调用 1127 个如此，缺的一步不再让后面全错位），
  新的规划步、消息来时还没结果的标“没有结果”；“在后台运行”的结果（状态停在 RUNNING，之后不会再补）标成后台运行。
- Antigravity 状态：最后一步是不带工具调用的规划步＝这一轮结束了（已完成），回报后接着干活的仍算运行中；父对话收件箱里 agy 的
  “服务重启，所有子代理已停止”通知晚于子代理最后一步、子代理又没结束的，算已停止。
- 首次读大文件按 4MB 分块读；每个记录一把锁，读一个大记录不再堵住别的请求；文件被清空重写（开头 64 字节变了）时从头读。
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import unquote

NS = {}
LOCK = threading.RLock()  # 只管缓存字典；读记录用各自的锁（Tail.lock）
MAX_KIDS = 64          # 一个对话最多列这么多子代理（T3 MAX_DETACHED_CHILDREN）
MAX_ITEMS = 400        # “查看过程”最多给这么多条（和 Claude 子代理一样取最后 400 条）
MAX_FILE = 1 << 20     # 单个小记录文件（子代理信息、收件箱消息）超过这个大小不读（T3 MAX_STEP_BYTES）
MAX_MSGS = 5000        # 收件箱超过这么多条不读（消息读过就缓存；T3 是 256，父对话收件箱会超过，放宽）
OUT_CAP, IN_CAP = 4000, 3000
NATIVE_ID = re.compile(r'[A-Za-z0-9-]{1,64}')  # 原生 ID 拼进路径前先校验（T3 NATIVE_ID），一律 fullmatch
ORD_RE = re.compile(rb'^\{"timestamp":"[^"]*","ordinal":(\d+)')
SHELL_RE = re.compile(r"^(?:/bin/|/usr/bin/)?(?:zsh|bash|sh) -l?c (['\"])(.*)\1$", re.S)
HDR_RE = re.compile(r'^(?:Created At|Completed At): [^\n]*\n?')
AGY_MSG_RE = re.compile(r'\[Message\][^\n]*?\bsender=(\S+)[^\n]*?\bcontent=(.*)', re.S)
RUNNING = ('RUNNING', 'PENDING', 'IN_PROGRESS', 'STEP_STATUS_RUNNING', 'STEP_STATUS_PENDING')


def log(*a):
    try:
        NS['log']('[子代理]', *a)
    except Exception:
        pass


def _home():
    return NS.get('HOME') or os.path.expanduser('~')


def _ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def _clean(s):
    if not isinstance(s, str):
        s = '' if s is None else json.dumps(s, ensure_ascii=False)
    f = NS.get('clean_text')
    try:
        return f(s) if f else s
    except Exception:
        return s


def _line(s, n=80):
    s = str(s or '').strip()
    return s.split('\n', 1)[0].strip()[:n]


def _short(p):
    h = _home()
    return '~' + p[len(h):] if p and h and p.startswith(h + '/') else (p or '')


def _ls(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def _read_json(p):
    try:
        if os.path.getsize(p) > MAX_FILE:
            return None
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _brief(it):
    """卡片上的“正在 / 最后：…”：工具写名字和对象，正文写第一行。"""
    if it.get('k') == 'tool':
        d = it.get('desc') or _short(it.get('path') or '') or it.get('in') or ''
        return (str(it.get('name') or '') + ' ' + _line(d)).strip()
    return _line(it.get('text'))


# ---------------------------------------------------------------- 增量读 jsonl
class Tail:
    """记住读到哪，只解析新增的整行；文件变短、换了（inode 变）或被清空重写（开头 64 字节变了）从头读。
    .zst（Codex 压缩过的记录）不会再变，整份流式解压读一次。子类实现 on_reset、row。调用 feed、snapshot、last 前先拿 self.lock。"""
    CHUNK = 4 << 20
    HEAD = 64

    def __init__(self, path):
        self.path, self.ino, self.used, self.lock = path, None, time.time(), threading.RLock()
        self.zst = path.endswith('.zst')
        self.reset()

    def reset(self):
        self.off, self.buf, self.n, self.mtime, self.head, self.zsize = 0, b'', 0, 0.0, b'', -1
        self.items, self.seq, self.tools, self.out = [], 0, 0, 0
        self.t0 = self.t_last = None
        self.on_reset()

    def on_reset(self):
        pass

    def add(self, it):
        self.seq += 1
        it['id'] = 'i%d' % self.seq
        if it.get('k') == 'tool':
            self.tools += 1
        self.items.append(it)
        if len(self.items) > MAX_ITEMS * 2:
            del self.items[:len(self.items) - MAX_ITEMS]

    def feed(self):
        self.used = time.time()
        st = os.stat(self.path)
        if self.zst:
            if st.st_ino == self.ino and st.st_size == self.zsize:
                return
            self.ino = st.st_ino
            self.reset()
            self.zsize, self.mtime = st.st_size, st.st_mtime
            from compression import zstd
            with zstd.open(self.path, 'rb') as f:
                for line in f:
                    self.one(line.rstrip(b'\n'))
            return
        with open(self.path, 'rb') as f:
            hd = f.read(self.HEAD)
            if st.st_ino != self.ino or st.st_size < self.off or (self.head and hd != self.head):
                self.ino = st.st_ino
                self.reset()
            self.mtime = st.st_mtime
            if st.st_size <= self.off:
                return
            f.seek(self.off)
            while True:
                chunk = f.read(self.CHUNK)
                if not chunk:
                    break
                self.off += len(chunk)
                rows = (self.buf + chunk).split(b'\n')
                self.buf = rows.pop()
                for line in rows:
                    self.one(line)
                if len(self.buf) > 64 << 20:  # 一行超过 64MB：不要了，免得占满内存
                    self.buf = b''
            if not self.head and len(hd) == self.HEAD:
                self.head = hd

    def one(self, line):
        idx = self.n
        self.n += 1
        if line.strip():
            try:
                self.line(idx, line)
            except Exception:
                pass

    def line(self, idx, line):
        self.row(json.loads(line))

    def stamp(self, ts):
        if ts:
            if self.t0 is None:
                self.t0 = ts
            self.t_last = ts

    def snapshot(self):
        return [{k: v for k, v in it.items() if not k.startswith('_')} for it in self.items if not it.get('_hide')][-MAX_ITEMS:]

    def last(self):
        for it in reversed(self.items):
            if not it.get('_hide') and it.get('k') in ('tool', 'text'):
                return _brief(it)
        return ''


PARSERS = {}  # 记录文件路径 -> Tail


def parser(cls, path):
    with LOCK:
        p = PARSERS.get(path)
        if not isinstance(p, cls):
            p = PARSERS[path] = cls(path)
        if len(PARSERS) > 300:
            for k, _ in sorted(PARSERS.items(), key=lambda kv: kv[1].used)[:100]:
                PARSERS.pop(k, None)
        return p


def _state(done, killed, err, mtime, alive):
    if killed:
        return 'killed'
    if done:
        return 'err' if err else 'done'
    return 'run' if time.time() - mtime < (900 if alive else 180) else 'stale'


def _match_tools(c, kids, pred):
    """“在对话中定位”：给每个子代理找父对话里启动它的那条工具条目。按子代理启动先后，每条只认一次。"""
    try:
        c.ensure_loaded()
        cands = [it for it in list(c.items) if it.get('k') == 'tool']
    except Exception:
        return {}
    used, out = set(), {}
    for key, info in kids:
        for it in cands:
            if it.get('id') not in used and pred(it, info):
                used.add(it.get('id'))
                out[key] = it.get('id')
                break
    return out


# ================================================================ Codex
def codex_home():
    return os.environ.get('CWB_CODEX_HOME') or os.environ.get('CODEX_HOME') or os.path.join(_home(), '.codex')


CX_META = {}  # rollout 路径 -> 第一行 session_meta 摘出来的信息（不是子代理的是 {}）


def cx_meta(path):
    m = CX_META.get(path)
    if m is not None:
        return m
    try:
        if path.endswith('.zst'):
            from compression import zstd
            with zstd.open(path, 'rb') as f:
                line = f.readline(8 << 20)
        else:
            with open(path, 'rb') as f:
                line = f.readline(8 << 20)
    except Exception:
        return {}
    if not line.endswith(b'\n'):
        return {}  # 第一行还没写完，下次再读，不缓存
    m = {}
    try:
        r = json.loads(line)
        p = r.get('payload') if r.get('type') == 'session_meta' else None
        src = p.get('source') if isinstance(p, dict) else None
        sub = (src.get('subagent') or src.get('subAgent')) if isinstance(src, dict) else None
        sp = sub.get('thread_spawn') if isinstance(sub, dict) else None
        if isinstance(sp, dict):  # 只认 spawn_agent 派出来的（T3 同样只认 thread_spawn）；guardian、review 等不是子代理
            m = {'id': p.get('id') or '', 'parent': sp.get('parent_thread_id') or p.get('parent_thread_id') or '',
                 'path': p.get('agent_path') or sp.get('agent_path') or '', 'nick': p.get('agent_nickname') or sp.get('agent_nickname') or '',
                 'role': p.get('agent_role') or sp.get('agent_role') or '', 'ts': _ts(p.get('timestamp'))}
    except Exception:
        m = {}
    with LOCK:
        if len(CX_META) > 20000:
            CX_META.clear()
        CX_META[path] = m
    return m


def cx_files(since):
    """父对话建立前一天起的 rollout：sessions/年/月/日 按目录名筛，archived_sessions 按文件名里的日期筛。"""
    home = codex_home()
    root, out = os.path.join(home, 'sessions'), []
    d0 = (datetime.fromtimestamp(max(0, since or 0)) - timedelta(days=1)).date()
    for y in _ls(root):
        if not (y.isdigit() and int(y) >= d0.year):
            continue
        for mo in _ls(os.path.join(root, y)):
            if not (mo.isdigit() and (int(y), int(mo)) >= (d0.year, d0.month)):
                continue
            for d in _ls(os.path.join(root, y, mo)):
                if not (d.isdigit() and (int(y), int(mo), int(d)) >= (d0.year, d0.month, d0.day)):
                    continue
                dd = os.path.join(root, y, mo, d)
                out += [os.path.join(dd, n) for n in _ls(dd) if n.startswith('rollout-') and n.endswith(('.jsonl', '.jsonl.zst'))]
    arch, day = os.path.join(home, 'archived_sessions'), d0.isoformat()
    out += [os.path.join(arch, n) for n in _ls(arch) if n.startswith('rollout-') and n.endswith(('.jsonl', '.jsonl.zst')) and n[8:18] >= day]
    return out


def cx_children(c):
    """父线程的子代理（含子代理再派的，按层往下找）：[(rollout 路径, 信息)]，按启动先后。同一线程有 .jsonl 也有 .zst 时用 .jsonl。"""
    pid = (c.meta or {}).get('native') or ''
    if not pid:
        return []
    best = {}
    for p in cx_files((c.meta or {}).get('created')):
        m = cx_meta(p)
        if m.get('parent') and m.get('id') and (m['id'] not in best or best[m['id']][0].endswith('.zst')):
            best[m['id']] = (p, m)
    by_parent = {}
    for p, m in best.values():
        by_parent.setdefault(m['parent'], []).append((p, m))
    out, q, seen = [], [pid], {pid}
    while q and len(out) < MAX_KIDS:
        x = q.pop(0)
        for p, m in sorted(by_parent.get(x, []), key=lambda t: t[1].get('ts') or 0):
            if m['id'] not in seen:
                seen.add(m['id'])
                out.append((p, m))
                q.append(m['id'])
    return out[:MAX_KIDS]


def _cx_cmd(cmd):
    if isinstance(cmd, list):
        cmd = [str(x) for x in cmd]
        if len(cmd) >= 3 and os.path.basename(cmd[0]) in ('zsh', 'bash', 'sh') and cmd[1] in ('-lc', '-c'):
            return cmd[2]
        return ' '.join(cmd)
    m = SHELL_RE.match((cmd or '').strip())
    if not m:
        return cmd or ''
    return m.group(2).replace("'\\''", "'") if m.group(1) == "'" else m.group(2)


def _plain(s):
    """Codex 多代理消息正文是加密串（gAAAA…）：显示成“已加密”。"""
    if not isinstance(s, str):
        return '' if s is None else json.dumps(s, ensure_ascii=False)[:IN_CAP]
    return '（已加密，读不到原文）' if re.match(r'^gAAAA[\w-]{40,}={0,2}$', s.strip()) else s[:IN_CAP]


def _out_text(o):
    if isinstance(o, list):
        return '\n'.join(str(x.get('text') or '') for x in o if isinstance(x, dict))
    if isinstance(o, dict):
        return str(o.get('output') or o.get('content') or json.dumps(o, ensure_ascii=False))
    return '' if o is None else str(o)


CX_CMD_CALLS = ('exec_command', 'shell', 'local_shell', 'shell_command', 'unified_exec', 'write_stdin')


def _covers(name, item):
    """外层调用 name 是不是 item_completed 里这个操作的外壳。"""
    ty = item.get('type')
    if name == 'exec':
        return True  # 代码模式：exec 里的每个操作都会单独有 item_completed
    if ty == 'CommandExecution':
        return name in CX_CMD_CALLS
    if ty == 'FileChange':
        return name == 'apply_patch'
    if ty == 'ImageView':
        return name == 'view_image'
    if ty == 'WebSearch':
        return name.startswith('web_search')
    if ty in ('McpToolCall', 'DynamicToolCall'):
        tool = str(item.get('tool') or '')
        return bool(tool) and (name == tool or name.endswith('__' + tool))
    return False


def _ctx_msg(t):
    """Codex 自动塞进历史的上下文（AGENTS.md、环境信息等），不是任务。"""
    s = t.lstrip()
    return s.startswith('<') or s.startswith('# AGENTS.md') or '<environment_context>' in s[:3000]


class CodexKid(Tail):
    """一个 Codex 子线程的 rollout → 过程条目（和工作台时间线同样的 k=user/text/thought/tool/error）。"""

    def on_reset(self):
        self.meta, self.start, self.pending = None, 0, {}
        self.life, self.err, self.prompt, self.prev = '', '', '', None

    def line(self, idx, line):
        if self.meta is None:
            r = json.loads(line)
            p = r.get('payload') if r.get('type') == 'session_meta' else None
            self.meta = p if isinstance(p, dict) else {}
            # 只有分叉出来的（v2，带 forked_from_id）才先抄一份父线程历史；v1 不抄，这个序号在 v1 记录里等于总行数，不能拿来跳
            s = self.meta.get('subagent_history_start_ordinal')
            self.start = s if isinstance(s, int) and self.meta.get('forked_from_id') else 0
            self.stamp(_ts(self.meta.get('timestamp')))
            return
        m = ORD_RE.match(line)
        if m and int(m.group(1)) < self.start:
            return  # 抄过来的父线程历史：不解析
        r = json.loads(line)
        if not m:
            o = r.get('ordinal') if isinstance(r.get('ordinal'), int) else idx
            if o < self.start:
                return
        self.row(r)

    def row(self, r):
        t, p = r.get('type'), r.get('payload')
        if not isinstance(p, dict):
            return
        ts = _ts(r.get('timestamp'))
        prev, self.prev = self.prev, ts or self.prev
        self.stamp(ts)
        pt = p.get('type')
        if t == 'response_item':
            if pt == 'message':
                parts = [x for x in p.get('content') or [] if isinstance(x, dict)]
                txt = '\n'.join(str(x.get('text') or '') for x in parts if x.get('type') in ('input_text', 'output_text', 'text')).strip()
                if p.get('role') == 'assistant' and txt:
                    self.add({'k': 'text', 'text': txt[:20000]})
                elif p.get('role') == 'user' and txt and not _ctx_msg(txt):
                    self.prompt = self.prompt or txt
                    self.add({'k': 'user', 'text': txt[:8000]})
            elif pt == 'agent_message':
                self.agent_msg(p)
            elif pt == 'reasoning':
                summ = '\n\n'.join(str(x.get('text') or '') for x in p.get('summary') or [] if isinstance(x, dict)).strip()
                if summ:
                    self.add({'k': 'thought', 'secs': max(1, round(ts - prev)) if ts and prev else 1, 'text': summ[:20000]})
            elif pt in ('function_call', 'custom_tool_call'):
                it = self.call_view(p.get('name') or '工具', p.get('arguments') if pt == 'function_call' else p.get('input'))
                it.update(k='tool', done=False, error=False, out='', _n=str(p.get('name') or ''))
                self.pending[p.get('call_id') or p.get('id') or str(self.seq)] = it
                self.add(it)
            elif pt in ('function_call_output', 'custom_tool_call_output'):
                it = self.pending.pop(p.get('call_id') or '', None)
                if it:
                    it.update(done=True, out=_clean(_out_text(p.get('output')))[:OUT_CAP])
        elif t == 'event_msg':
            if pt == 'item_completed':
                item = p.get('item') or {}
                vs = self.item_view(item)
                if vs:
                    # 外层调用和里面实际的操作是同一件事时，只列里面的：代码模式的 exec（里面跑命令、看图等）、exec_command 对命令、
                    # apply_patch 对改文件……同时在跑的 spawn_agent、wait_agent 等不受影响
                    for w in self.pending.values():
                        if not w.get('_hide') and _covers(w.get('_n') or '', item):
                            w['_hide'] = True
                            self.tools -= 1
                    for v in vs:
                        self.add(v)
            elif pt in ('task_started', 'task_complete', 'turn_aborted'):
                self.life = pt
                if pt == 'task_started':
                    self.err = ''
                else:
                    for w in self.pending.values():
                        w.update(done=True, noresult='未完成' if pt == 'turn_aborted' else '')
                    self.pending = {}
            elif pt == 'error':
                self.err = str(p.get('message') or '出错了')
                self.add({'k': 'error', 'text': self.err[:2000]})
            elif pt == 'token_count':
                tot = (p.get('info') or {}).get('total_token_usage') or {}
                if isinstance(tot.get('output_tokens'), int):
                    self.out = tot['output_tokens']

    def agent_msg(self, p):
        """多代理 v2 的代理间消息：别人发来的显示成一条“任务 / 消息”，正文加密的说明读不到。"""
        author, own = p.get('author') or '', (self.meta or {}).get('agent_path') or ''
        if author and author == own:
            return
        parts = [x for x in p.get('content') or [] if isinstance(x, dict)]
        plain = '\n'.join(str(x.get('text') or '') for x in parts if x.get('type') in ('input_text', 'output_text', 'text'))
        enc = any(x.get('type') == 'encrypted_content' for x in parts)
        kind = re.search(r'Message Type:\s*(\w+)', plain)
        body = plain.split('Payload:', 1)[1] if 'Payload:' in plain else ('' if kind else plain)
        body = body.strip() or ('（Codex 加密保存了正文，读不到原文）' if enc else '')
        label = '新任务' if kind and kind.group(1) == 'NEW_TASK' else '消息'
        text = f'{author or "父代理"} 发来{label}' + ('：\n' + body if body else '')
        if label == '新任务' and not self.prompt and not enc:
            self.prompt = body
        self.add({'k': 'user', 'text': text[:8000]})

    def call_view(self, name, args):
        a = args
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except Exception:
                pass
        d = a if isinstance(a, dict) else {}
        if name in ('exec_command', 'shell', 'local_shell', 'shell_command') and d:
            return {'name': 'Bash', 'desc': '', 'path': None, 'in': _cx_cmd(d.get('cmd') or d.get('command') or '')[:IN_CAP]}
        if name == 'spawn_agent':
            return {'name': 'Agent', 'desc': d.get('task_name') or d.get('agent_type') or '', 'path': None, 'in': _plain(d.get('message'))}
        if name == 'send_message':
            return {'name': '发消息', 'desc': ('给 ' + str(d.get('target'))) if d.get('target') else '', 'path': None, 'in': _plain(d.get('message'))}
        if name == 'wait_agent':
            return {'name': '等待子代理', 'desc': '', 'path': None, 'in': ''}
        if name == 'close_agent':
            return {'name': '结束子代理', 'desc': str(d.get('target') or ''), 'path': None, 'in': ''}
        txt = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
        return {'name': name, 'desc': '', 'path': None, 'in': (txt or '')[:IN_CAP]}

    def item_view(self, it):
        """rollout 里 item_completed 事件的条目（类型首字母大写、字段下划线）→ 工具条目，口径和 prov_codex 的实时映射一致。"""
        ty = it.get('type')
        if ty == 'CommandExecution':
            cmd = _cx_cmd(it.get('command'))
            acts = [x for x in it.get('parsed_cmd') or [] if isinstance(x, dict)]
            v = {'name': 'Bash', 'path': None, 'desc': '', 'in': cmd[:IN_CAP]}
            if len(acts) == 1 and acts[0].get('type') == 'read':
                v.update(name='Read', path=acts[0].get('path'))
            elif len(acts) == 1 and acts[0].get('type') == 'search':
                v.update(name='Grep', path=acts[0].get('path'), desc=acts[0].get('query') or cmd)
            elif len(acts) == 1 and acts[0].get('type') in ('list_files', 'listFiles'):
                v.update(name='Glob', path=acts[0].get('path'), desc=cmd)
            code, st = it.get('exit_code'), it.get('status')
            out = _clean(it.get('aggregated_output') or '')[:OUT_CAP]
            if code not in (None, 0):
                out = (out + ('\n' if out else '') + f'退出码 {code}').strip()
            v.update(k='tool', done=True, out=out, error=st in ('failed', 'declined') or code not in (None, 0))
            return [v]
        if ty == 'ImageView':
            p = str(it.get('path') or '')
            if p.startswith('file://'):
                p = unquote(p[7:])
            return [{'k': 'tool', 'name': 'Read', 'path': p or None, 'desc': '' if p else '查看图片', 'in': '', 'done': True, 'out': '', 'error': False}]
        if ty == 'FileChange':
            ch = it.get('changes') or []
            if isinstance(ch, dict):
                ch = [dict(v, path=k) if isinstance(v, dict) else {'path': k} for k, v in ch.items()]
            out = []
            for x in ch:
                if not isinstance(x, dict):
                    continue
                kind = x.get('kind') if isinstance(x.get('kind'), str) else (x.get('kind') or {}).get('type') or x.get('type') or ''
                diff = x.get('diff') or x.get('unified_diff') or x.get('content') or ''
                v = {'k': 'tool', 'name': 'Write' if kind == 'add' else '删除文件' if kind == 'delete' else 'Edit', 'path': x.get('path') or '',
                     'desc': '', 'in': '', 'done': True, 'out': '', 'error': it.get('status') in ('failed', 'declined')}
                try:
                    from providers import diff_view as dv
                except Exception:
                    dv = None
                old, new, _ = dv(diff) if dv and kind not in ('add', 'delete') else (None, None, None)
                if old is not None:
                    v['diff'] = {'old': old, 'new': new}
                elif kind != 'delete':
                    v['in'] = str(diff)[:IN_CAP]
                out.append(v)
            return out
        if ty == 'McpToolCall':
            res, err = it.get('result') or {}, it.get('error') or None
            parts = [x.get('text') or '' for x in (res.get('content') or []) if isinstance(x, dict)] if isinstance(res, dict) else []
            return [{'k': 'tool', 'name': it.get('tool') or 'MCP', 'desc': it.get('server') or '', 'path': None,
                     'in': json.dumps(it.get('arguments'), ensure_ascii=False)[:800], 'done': True, 'error': bool(err),
                     'out': _clean((err or {}).get('message') if isinstance(err, dict) else '\n'.join(parts))[:OUT_CAP]}]
        if ty == 'WebSearch':
            return [{'k': 'tool', 'name': 'WebSearch', 'desc': it.get('query') or '', 'path': None, 'in': '', 'done': True, 'out': '', 'error': False}]
        if ty == 'DynamicToolCall':
            return [{'k': 'tool', 'name': it.get('tool') or '工具', 'desc': it.get('namespace') or '', 'path': None,
                     'in': json.dumps(it.get('arguments'), ensure_ascii=False)[:800], 'done': True, 'out': '', 'error': it.get('success') is False}]
        return None


def codex_agents(c, alive):
    kids = cx_children(c)
    out, info = [], []
    for path, m in kids:
        k = parser(CodexKid, path)
        try:
            with k.lock:
                k.feed()
                st = _state(k.life == 'task_complete', k.life == 'turn_aborted', k.err, k.mtime, alive)
                a = {'id': 'cx-' + m['id'], 'desc': (m['path'].rsplit('/', 1)[-1] if m['path'] else '') or m['nick'] or '子代理',
                     'type': m['role'] or '', 'bg': False, 'tool_id': '', 'state': st, 'start': k.t0 or m.get('ts'),
                     'end': None if st == 'run' else (k.t_last or k.mtime), 'tools': k.tools, 'out': k.out, 'last': k.last()}
                prompt = k.prompt
        except Exception as e:  # 记录被删、压缩包坏了等：这个子代理先不列
            log('读 Codex 子代理记录出错', os.path.basename(path), repr(e))
            continue
        out.append(a)
        info.append((a['id'], {'path': m['path'], 'prompt': prompt}))

    def pred(it, x):  # 父对话里 Codex 子代理的条目：name=Agent，desc 是 agentPath（v2）或任务正文前 200 字（v1）
        if it.get('name') != 'Agent':
            return False
        if x['path'] and it.get('desc') == x['path']:
            return True
        head = (it.get('in') or it.get('desc') or '')[:200]
        return bool(x['prompt'] and len(head) >= 16 and x['prompt'].startswith(head))
    ids = _match_tools(c, info, pred)
    for a in out:
        a['tool_id'] = ids.get(a['id'], '')
    return out


def codex_items(c, tid):
    for path, m in cx_children(c):
        if m['id'] == tid:
            k = parser(CodexKid, path)
            with k.lock:
                k.feed()
                return k.snapshot()
    return []


# ================================================================ Antigravity
def agy_roots():
    env = os.environ.get('CWB_AGY_DATA_DIR')
    if env:
        return [env]
    g = os.path.join(_home(), '.gemini')
    return [os.path.join(g, n) for n in ('antigravity-cwb', 'antigravity-cli', 'antigravity')]


def agy_brain(native):
    for r in agy_roots():
        if os.path.isdir(os.path.join(r, 'brain', native)):
            return os.path.join(r, 'brain')
    return None


INBOX = {}   # 收件箱消息文件 -> 摘要（消息写好就不再变）
LAUNCH = {}  # 子代理 ID -> 启动信息（找到过就不再变）


def _inbox(brain, cid):
    """一个对话的收件箱：[{sender, ts, content, call}]。read.json 是已读回执，不是消息（T3 readInbox）。"""
    d = os.path.join(brain, cid, '.system_generated', 'messages')
    names = [n for n in _ls(d) if n.endswith('.json') and n != 'read.json']
    if len(names) > MAX_MSGS:
        return []
    out = []
    for n in names:
        p = os.path.join(d, n)
        m = INBOX.get(p)
        if m is None:
            r = _read_json(p)
            if not isinstance(r, dict):
                continue
            tool = ((r.get('sourceMetadata') or {}).get('tool') or {}) if isinstance(r.get('sourceMetadata'), dict) else {}
            m = {'sender': r.get('sender') or '', 'ts': _ts(r.get('timestamp')), 'content': r.get('content') or '',
                 'call': tool.get('toolCall') if isinstance(tool.get('toolCall'), dict) else None, 'conv': tool.get('conversationId')}
            with LOCK:
                if len(INBOX) > 20000:
                    INBOX.clear()
                INBOX[p] = m
        out.append(m)
    return out


def agy_launch(brain, kid, parent):
    """子代理收件箱里父对话用 invoke_subagent 发来的那条：提示词、角色、类型（T3 findDetachedInvokeRecord）。"""
    if kid in LAUNCH:
        return LAUNCH[kid]
    for m in _inbox(brain, kid):
        call = m.get('call') or {}
        if m['sender'] != parent or call.get('name') != 'invoke_subagent' or (m.get('conv') not in (None, parent)):
            continue
        args = {}
        try:
            args = json.loads(call.get('argumentsJson') or '{}')
        except Exception:
            pass
        if isinstance(args.get('Subagents'), list) and args['Subagents']:
            args = args['Subagents'][0] if isinstance(args['Subagents'][0], dict) else {}
        r = {'prompt': str(args.get('Prompt') or m.get('content') or ''), 'role': str(args.get('Role') or ''),
             'type': str(args.get('TypeName') or ''), 'ts': m.get('ts'), 'call': call.get('id') or ''}
        LAUNCH[kid] = r
        return r
    return {'prompt': '', 'role': '', 'type': '', 'ts': None, 'call': ''}


def _args(a):
    """agy 记录里每个参数值都是 JSON 文本（"\\"1:1\\""），先解一层（T3 toolArguments）。"""
    if not isinstance(a, dict):
        return {}
    out = {}
    for k, v in a.items():
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                pass
        out[k] = v
    return out


def agy_tool(name, a, parent):
    g = lambda *ks: next((a[k] for k in ks if a.get(k) not in (None, '')), '')  # noqa: E731
    if name == 'run_command':
        return {'name': 'Bash', 'desc': '', 'path': None, 'in': str(g('CommandLine', 'Command'))[:IN_CAP]}
    if name in ('read_file', 'view_file', 'view_file_outline'):
        return {'name': 'Read', 'desc': '', 'path': str(g('Path', 'AbsolutePath', 'File')) or None, 'in': ''}
    if name in ('edit_file', 'replace_file_content', 'multi_replace_file_content'):
        v = {'name': 'Edit', 'desc': '', 'path': str(g('Path', 'TargetFile', 'AbsolutePath')) or None, 'in': ''}
        if isinstance(a.get('Target'), str) or isinstance(a.get('Replacement'), str):
            v['diff'] = {'old': str(a.get('Target') or '')[:IN_CAP], 'new': str(a.get('Replacement') or '')[:IN_CAP]}
        return v
    if name in ('write_file', 'write_to_file'):
        return {'name': 'Write', 'desc': '', 'path': str(g('Path', 'TargetFile', 'AbsolutePath')) or None, 'in': str(g('Content', 'CodeContent'))[:IN_CAP]}
    if name in ('grep_search', 'find_by_name'):
        return {'name': 'Grep', 'desc': str(g('Query', 'Pattern')), 'path': str(g('SearchPath', 'SearchDirectory')) or None, 'in': ''}
    if name == 'list_dir':
        return {'name': 'Glob', 'desc': '', 'path': str(g('DirectoryPath', 'Path')) or None, 'in': ''}
    if name == 'search_web':
        return {'name': 'WebSearch', 'desc': str(g('query', 'Query')), 'path': None, 'in': ''}
    if name == 'read_url_content':
        return {'name': 'WebFetch', 'desc': str(g('Url', 'url')), 'path': None, 'in': ''}
    if name == 'generate_image':
        return {'name': '生成图片', 'desc': str(g('ImageName')), 'path': None, 'in': str(g('Prompt'))[:IN_CAP]}
    if name == 'send_message':
        to = str(g('Recipient'))
        return {'name': '发消息', 'desc': '给父代理' if to == parent else ('给 ' + to if to else ''), 'path': None, 'in': str(g('Message'))[:IN_CAP]}
    if name == 'invoke_subagent':
        return {'name': 'Agent', 'desc': str(g('Role')), 'path': None, 'in': str(g('Prompt'))[:IN_CAP]}
    return {'name': name or '工具', 'desc': '', 'path': None, 'in': json.dumps(a, ensure_ascii=False)[:800] if a else ''}


class AgyKid(Tail):
    """一个 Antigravity 子代理的 transcript.jsonl → 过程条目。
    工具结果按 step_index 配对：规划步 s 的第 i 个调用 → 第 s+i 步（本机 1128 个调用 1127 个如此；缺了一步也不会让后面错位）。
    ended：最后一步是不带工具调用、已完成的规划步＝这一轮结束了。"""

    def on_reset(self):
        self.pending, self.prev, self.ended = {}, None, False
        self.parent = getattr(self, 'parent', '')  # 父对话 ID 由 agy_feed 设，从头重读时保留

    def close_left(self):
        """新的规划步、新消息来了，前面还没结果的调用不会再有结果了。"""
        for v in self.pending.values():
            v.update(done=True, noresult='没有结果')
        self.pending = {}

    def row(self, r):
        ty, ts, idx = r.get('type'), _ts(r.get('created_at')), r.get('step_index')
        prev, self.prev = self.prev, ts or self.prev
        self.stamp(ts)
        content = r.get('content') if isinstance(r.get('content'), str) else ''
        status = str(r.get('status') or '')
        if ty == 'PLANNER_RESPONSE':
            self.close_left()
            th = (r.get('thinking') or '').strip() if isinstance(r.get('thinking'), str) else ''
            if th:
                self.add({'k': 'thought', 'secs': max(1, round(ts - prev)) if ts and prev else 1, 'text': th[:20000]})
            if content.strip():
                self.add({'k': 'text', 'text': content.strip()[:20000]})
            calls = [tc for tc in r.get('tool_calls') or [] if isinstance(tc, dict)]
            for i, tc in enumerate(calls, 1):
                v = agy_tool(tc.get('name') or '', _args(tc.get('args')), self.parent)
                v.update(k='tool', done=False, error=False, out='')
                self.pending[idx + i if isinstance(idx, int) else ('q', self.seq)] = v
                self.add(v)
            if isinstance(r.get('output_tokens'), int):
                self.out += r['output_tokens']
            self.ended = not calls and status not in RUNNING
            return
        self.ended = False
        if ty == 'SYSTEM_MESSAGE':
            self.close_left()
            m = AGY_MSG_RE.search(content)
            if m:
                body = re.sub(r'\s*</SYSTEM_MESSAGE>[\s\S]*$', '', m.group(2)).strip()
                who = '父代理' if m.group(1) == self.parent else m.group(1)
                self.add({'k': 'user', 'text': body[:8000] if idx == 0 else f'{who} 发来消息：\n{body}'[:8000], '_launch': idx == 0})
        elif ty == 'USER_INPUT':
            self.close_left()
            m = re.search(r'<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>', content)
            t = (m.group(1) if m else content).strip()
            if t:
                self.add({'k': 'user', 'text': t[:8000]})
        elif ty == 'ERROR_MESSAGE':
            self.add({'k': 'error', 'text': str(r.get('error') or content or '出错了')[:2000]})
        elif ty == 'CHECKPOINT':
            self.add({'k': 'note', 'text': '已压缩上下文'})
        else:
            v = self.pending.pop(idx, None) if isinstance(idx, int) else None
            if v is None and not isinstance(idx, int) and self.pending:  # 没有 step_index 的旧格式：按先后
                v = self.pending.pop(next(iter(self.pending)))
            if v is None:
                return
            out = content
            while HDR_RE.match(out):
                out = HDR_RE.sub('', out, 1)
            err = r.get('error')
            # “Tool is running as a background task”：状态停在 RUNNING，之后不会再补一行，按后台运行收尾
            v.update(done=True, bg=status in RUNNING, error=bool(err), out=_clean(str(err) if err and not out.strip() else out.strip())[:OUT_CAP])


def agy_kids(c):
    native = (c.meta or {}).get('native') or ''
    if not NATIVE_ID.fullmatch(native):
        return None, native, []
    brain = agy_brain(native)
    if not brain:
        return None, native, []
    d = os.path.join(brain, native, '.system_generated', 'subagents')
    ids = [n[:-5] for n in _ls(d) if n.endswith('.json') and NATIVE_ID.fullmatch(n[:-5])]
    return brain, native, ids[:MAX_KIDS]


def agy_feed(brain, native, kid):
    """调用方持有返回对象的 lock。"""
    p = os.path.join(brain, kid, '.system_generated', 'logs', 'transcript.jsonl')
    k = parser(AgyKid, p)
    with k.lock:
        k.parent = native
        try:
            k.feed()
        except OSError:
            k.mtime = 0.0
            try:
                k.mtime = os.path.getmtime(os.path.join(brain, kid))
            except OSError:
                pass
    return k


def agy_state(k, rec, rep, stopped, alive, launched=None):
    """一个 Antigravity 子代理的状态。rep：它最后一次回报父对话的时间；stopped：agy 通知“服务重启，子代理都停了”的时间；launched：启动时间。"""
    if rec.get('state') == 'SUBAGENT_STATE_KILLED':  # manage_subagents 停掉的（T3：只标记，不会回报）
        return 'killed'
    if k.ended:
        return 'done'  # 这一轮结束了（最后一步是不带工具调用的规划步）
    if stopped and stopped > max(k.t_last or 0, launched or 0):
        return 'killed'  # agy 进程重启（含工作台空闲回收）把它停了
    if time.time() - k.mtime < (900 if alive else 180):
        return 'run'  # 还没结束、记录刚更新：回报过也算还在干活
    return 'done' if rep else 'stale'


def agy_agents(c, alive):
    brain, native, ids = agy_kids(c)
    if not brain:
        return []
    reports, stopped = {}, 0
    for m in _inbox(brain, native):
        if m['sender'] == 'system' and 'stopped due to server restart' in str(m.get('content') or ''):
            stopped = max(stopped, m.get('ts') or 0)
        elif m['sender'] and m['sender'] != native:
            reports[m['sender']] = max(reports.get(m['sender']) or 0, m.get('ts') or 0)
    out, info = [], []
    for kid in ids:
        rec = _read_json(os.path.join(brain, native, '.system_generated', 'subagents', kid + '.json')) or {}
        if rec.get('conversationId') not in (None, kid):
            continue
        desc = rec.get('subagentDescriptor') if isinstance(rec.get('subagentDescriptor'), dict) else {}
        ln = agy_launch(brain, kid, native)
        k = agy_feed(brain, native, kid)
        with k.lock:
            st = agy_state(k, rec, reports.get(kid), stopped, alive, ln.get('ts'))
            a = {'id': 'ag-' + kid, 'desc': ln['role'] or desc.get('role') or '子代理', 'type': ln['type'] or desc.get('typeName') or '',
                 'bg': False, 'tool_id': '', 'state': st, 'start': k.t0 or ln.get('ts'), 'end': None if st == 'run' else (k.t_last or k.mtime or None),
                 'tools': k.tools, 'out': k.out, 'last': k.last()}
        out.append(a)
        info.append((a['id'], {'prompt': ln['prompt'], 'ts': a['start'] or 0}))
    info.sort(key=lambda x: x[1]['ts'])

    def pred(it, x):  # 父对话里的 invoke_subagent 条目：in 是 rawInput 的 JSON（前 800 字），比对 Prompt 开头
        if 'invoke_subagent' not in str(it.get('name') or '') or not x['prompt']:
            return False
        s = str(it.get('in') or '')
        i = s.find('"Prompt": "')
        if i < 0:
            return False
        body, esc = s[i + 11:], json.dumps(x['prompt'], ensure_ascii=False)[1:-1]
        n = min(len(body), len(esc), 700)
        return n >= 16 and body[:n] == esc[:n]
    ids_map = _match_tools(c, info, pred)
    for a in out:
        a['tool_id'] = ids_map.get(a['id'], '')
    return out


def agy_items(c, kid):
    brain, native, ids = agy_kids(c)
    if not brain or kid not in ids:
        return []
    ln = agy_launch(brain, kid, native)
    k = agy_feed(brain, native, kid)
    with k.lock:
        items = k.snapshot()
        first = dict(k.items[0]) if k.items else None
        whole = len(items) == k.seq  # 没有因为太长截掉开头
    if ln['prompt']:  # 第 0 步的系统消息里只有截断过的提示词，换成启动消息里的全文；还没有过程记录时先显示提示词
        if first and first.get('_launch') and items and items[0].get('id') == first.get('id'):
            items[0] = dict(items[0], text=ln['prompt'][:8000])
        elif whole and not (first and first.get('_launch')):
            items.insert(0, {'k': 'user', 'text': ln['prompt'][:8000], 'id': 'i0'})
    return items


# ================================================================ 接口
def _alive(c):
    return bool(getattr(c, 'attached', False) or getattr(c, 'busy', False))


def agents(c):
    """右栏“任务”页：这个对话的子代理卡片（字段和 server.api_agents 一样）。两家都没有“后台命令”，shells 给空。"""
    if not c:
        return {'agents': [], 'shells': []}
    try:
        lst = codex_agents(c, _alive(c)) if c.prov == 'codex' else agy_agents(c, _alive(c)) if c.prov == 'agy' else []
    except Exception as e:
        log('读子代理出错', getattr(c, 'prov', ''), repr(e))
        lst = []
    lst.sort(key=lambda a: (a['state'] != 'run', -(a['start'] or 0)))
    return {'agents': lst[:60], 'shells': []}


def items(c, agent):
    """“查看过程”：一个子代理的过程条目。agent 是卡片的 id（cx-<线程ID> / ag-<对话ID>）。"""
    agent = str(agent or '')
    kind, _, nid = agent.partition('-')
    if not c or not NATIVE_ID.fullmatch(nid or ''):
        return {'items': []}
    try:
        if kind == 'cx' and c.prov == 'codex':
            return {'items': codex_items(c, nid)}
        if kind == 'ag' and c.prov == 'agy':
            return {'items': agy_items(c, nid)}
    except Exception as e:
        log('读子代理过程出错', agent, repr(e))
    return {'items': []}
