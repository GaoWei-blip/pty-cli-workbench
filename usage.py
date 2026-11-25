"""第十四轮（开发线 I）：用量统计 + 请求头自检用的本机抓包网关。

用量只统计在 Claude 工作台里运行的对话，数据只读：
- 主记录 <根>/<项目>/<sid>.jsonl，子代理记录 <根>/<项目>/<sid>/subagents/**/*.jsonl（含 workflows/<wf>/ 下的）。
- 每条回复取 assistant 行的 message.usage（input/output/cache_read/cache_creation）、message.model、message.id、requestId、timestamp。
  流式写入时同一条消息会写多行（有的版本前几行的 output_tokens 只是中间值）：按 message.id+requestId 去重，输出取最大、其余取最后一行。
  分叉、续接会把旧记录复制进新文件：全局去重，优先保留没有 forkedFrom 标记、文件创建更早的那份。跳过 <synthetic> 模型和用量全为 0 的行。
- 只算工作台：工作台的每一轮结束时（Stop 钩子），记录里有一行 system/stop_hook_summary，hookInfos 的 command 含 /claude-workbench/hook.py。
  * 主记录按文件顺序切段：两行收尾记录之间的回复归后一行收尾记录那一轮。中断、出错的轮次没有收尾记录（StopFailure 不写记录行，
    CLI 只在 Stop 钩子数大于 0 时写 stop_hook_summary），按同一会话里下一个有收尾记录的轮次归属。
  * 兜底：服务把每个对话“工作台启动 CLI 到 CLI 退出”的时间段追加到 .run/usage-runs.jsonl。最后一轮还没有收尾记录（进行中或被中断）的回复，
    落在这些时间段内才计入；有了时间段记录以后，被并进下一轮的中断轮也要落在时间段内（避免把在终端里跑、没有钩子的轮次算进来）。
  * 子代理记录按启动时间（文件第一行）落在父会话哪一轮归属；落在最后一轮之后的逐条按时间段判断。
- 耗时：assistant 行的 requestRef 对上 api-request 行的 id，用最后一行时间减去发请求的时间；没有 api-request 行时用消息第一行之前那一行的时间（近似）。
- 增量：按文件记大小、修改时间、已读偏移，只解析新增的整行；结果缓存在 .run/usage-cache.json。首次全量扫描在后台线程。
"""
import json
import os
import re
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WB_MARK = '/claude-workbench/hook.py'
CACHE_VER = 2
HEAT_DAYS = 371

# ---------------------------------------------------------------- 价目（每百万 token，美元）
# (输入, 输出, 缓存读)；缓存写按 1.25 倍输入价。Haiku 5.5 按提示 token（输入+缓存读+缓存写）是否超过 10 万分两档。
PRICES = {
    'opus-5-5': (4, 20, 0.20),
    'opus-5': (5, 25, 0.5), 'opus-4-8': (5, 25, 0.5), 'opus-4-7': (5, 25, 0.5), 'opus-4-6': (5, 25, 0.5), 'opus-4-5': (5, 25, 0.5),
    'sonnet-5-5': (2, 10, 0.10), 'sonnet-5': (2, 10, 0.2), 'sonnet-4-6': (3, 15, 0.3),
    'haiku-5-5': (0.10, 0.50, 0.01), 'haiku-4-5': (1, 5, 0.1),
    'fable-5-2': (8, 40, 0.20), 'fable-5-1': (10, 50, 0.25), 'fable-5': (10, 50, 1),
}
HAIKU55_LONG = (0.50, 2.50, 0.05)  # 提示超过 10 万 token
LONG_PROMPT = 100_000
LATEST = {'opus': 'opus-5-5', 'sonnet': 'sonnet-5-5', 'haiku': 'haiku-5-5', 'fable': 'fable-5-2'}  # 未知版本按同系列最新价
# Fast 计费倍数。来源：CLI 2.1.321 二进制里的 Fast 价目表（函数 Eln/j1t：usage.speed==="fast" 时改用这几张表）：
#   claude-opus-5-5 {8,40,缓存读0.4} 对普通 {4,20,0.2}＝2 倍；claude-opus-5 / claude-opus-4-8 {10,50,1} 对 {5,25,0.5}＝2 倍；
#   claude-fable-5-2 {16,80,0.4} 对 {8,40,0.2}＝2 倍；claude-opus-4-6 / 4-7 {30,150,3} 对 {5,25,0.5}＝6 倍。表里没有的按 2 倍。
FAST_MULT = {'opus-5-5': 2, 'opus-5': 2, 'opus-4-8': 2, 'fable-5-2': 2, 'opus-4-7': 6, 'opus-4-6': 6}
FAST_DEFAULT = 2
MODEL_RE = re.compile(r'(opus|sonnet|haiku|fable)-(\d{1,2})(?:-(\d{1,2}))?(?![\d])')


def model_key(model):
    """claude-opus-4-5-20251101 → opus-4-5；claude-opus-5-5[1m] → opus-5-5；认不出版本只返回系列，认不出系列返回 ''。"""
    m = MODEL_RE.search((model or '').lower())
    if not m:
        fam = re.search(r'(opus|sonnet|haiku|fable)', (model or '').lower())
        return fam.group(1) if fam else ''
    return '-'.join(x for x in m.groups() if x)


def price_key(model):
    k = model_key(model)
    if k in PRICES:
        return k
    fam = k.split('-')[0] if k else ''
    return LATEST.get(fam, '')


def cost_of(model, inp, out, cr, cw, fast=False):
    k = price_key(model)
    if not k:
        return 0.0
    pin, pout, pcr = PRICES[k]
    if k == 'haiku-5-5' and inp + cr + cw > LONG_PROMPT:
        pin, pout, pcr = HAIKU55_LONG
    c = (inp * pin + out * pout + cr * pcr + cw * pin * 1.25) / 1e6
    if fast:
        c *= FAST_MULT.get(k, FAST_DEFAULT)
    return c


def cache_saved(model, inp, cr, cw, fast=False):
    """缓存读比按输入价少付的钱。"""
    k = price_key(model)
    if not k:
        return 0.0
    pin, _, pcr = PRICES[k]
    if k == 'haiku-5-5' and inp + cr + cw > LONG_PROMPT:
        pin, _, pcr = HAIKU55_LONG
    return cr * (pin - pcr) / 1e6 * (FAST_MULT.get(k, FAST_DEFAULT) if fast else 1)


def default_label(i):
    s = re.sub(r'^claude-', '', i or '')
    parts = [x for x in s.replace('[1m]', '').split('-') if x and not re.fullmatch(r'\d{8}', x)]
    fam = ' '.join(x.capitalize() for x in parts if not x.isdigit())
    ver = '.'.join(x for x in parts if x.isdigit())
    return (fam + (' ' + ver if ver else '')).strip() or i


# ---------------------------------------------------------------- 记录解析
TS_RE = re.compile(rb'"timestamp":"([0-9][0-9T:.+\-Z]{9,39})"')


def ts_ms(s):
    """ISO 时间 → 毫秒；认不出返回 None。"""
    if not s:
        return None
    if isinstance(s, bytes):
        s = s.decode('ascii', 'ignore')
    try:
        return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)
    except Exception:
        return None


def hook_cmds(infos):
    for h in infos or []:
        if isinstance(h, str):
            yield h
        elif isinstance(h, dict):
            yield str(h.get('command') or '')


# 一条回复：[key, 首行时间, 末行时间, 模型, 输入, 输出, 缓存读, 缓存写, fast, 发请求时间, 段号, 轮号, 是否复制来的]
K_KEY, K_TS, K_TE, K_MODEL, K_IN, K_OUT, K_CR, K_CW, K_FAST, K_T0, K_SEG, K_TURN, K_FORK = range(13)
# 收尾记录：[时间, 是否工作台, 轮号, 这一轮提问的时间]
S_TS, S_WB, S_TURN, S_TURN_TS = range(4)


def new_entry(kind, sid, born):
    return {'kind': kind, 'sid': sid, 'born': born, 'size': 0, 'mtime': 0, 'off': 0, 'prev': None, 'reqs': {},
            'turn': 0, 'turn_ts': None, 'first': None, 'recs': [], 'sums': []}


def parse_lines(e, chunk, mark=WB_MARK):
    """解析一段完整的行（增量），就地更新文件条目 e。"""
    main = e['kind'] == 'main'
    keymap = e.get('_keys')
    if keymap is None:
        keymap = e['_keys'] = {r[K_KEY]: i for i, r in enumerate(e['recs'])}
    recs, sums, reqs = e['recs'], e['sums'], e['reqs']
    for line in chunk.split(b'\n'):
        if len(line) < 20:
            continue
        if line.startswith(b'{"type":"api-request",'):
            try:
                d = json.loads(line)
                t = ts_ms(d.get('timestamp'))
            except Exception:
                continue
            if d.get('id') and t:
                reqs[d['id']] = t
                if len(reqs) > 64:
                    for k in list(reqs)[:len(reqs) - 64]:
                        reqs.pop(k, None)
            e['prev'] = d.get('timestamp') or e['prev']
            if e['first'] is None:
                e['first'] = t
            continue
        if b'"type":"assistant"' in line:
            try:
                d = json.loads(line)
            except Exception:
                d = None
            if isinstance(d, dict) and d.get('type') == 'assistant':
                m = d.get('message') if isinstance(d.get('message'), dict) else {}
                tsr = d.get('timestamp')
                t = ts_ms(tsr)
                model = m.get('model') or ''
                u = m.get('usage') if isinstance(m.get('usage'), dict) else {}
                if e['first'] is None:
                    e['first'] = t
                vals = [int(u.get('input_tokens') or 0), int(u.get('output_tokens') or 0),
                        int(u.get('cache_read_input_tokens') or 0), int(u.get('cache_creation_input_tokens') or 0)]
                if t and model and model != '<synthetic>' and any(vals):
                    key = (m.get('id') or '') + '|' + (d.get('requestId') or '')
                    if key == '|':
                        key = 'u|' + str(d.get('uuid') or '')
                    fast = 1 if u.get('speed') == 'fast' else 0
                    i = keymap.get(key)
                    if i is None:
                        t0 = reqs.get(d.get('requestRef')) if d.get('requestRef') else None
                        if t0 is None:
                            t0 = ts_ms(e['prev'])
                        keymap[key] = len(recs)
                        recs.append([key, t, t, model] + vals + [fast, t0, len(sums), e['turn'], 1 if d.get('forkedFrom') else 0])
                    else:
                        r = recs[i]
                        r[K_TE] = max(r[K_TE], t)
                        r[K_IN], r[K_CR], r[K_CW] = vals[0], vals[2], vals[3]
                        r[K_OUT] = max(r[K_OUT], vals[1])
                        r[K_FAST] = r[K_FAST] or fast
                e['prev'] = tsr or e['prev']
                continue
        if main and b'"subtype":"stop_hook_summary"' in line:
            try:
                d = json.loads(line)
            except Exception:
                d = None
            if isinstance(d, dict) and d.get('type') == 'system' and d.get('subtype') == 'stop_hook_summary' and not d.get('isSidechain'):
                wb = 1 if any(mark in c for c in hook_cmds(d.get('hookInfos'))) else 0
                sums.append([ts_ms(d.get('timestamp')) or 0, wb, e['turn'], e['turn_ts']])
                e['prev'] = d.get('timestamp') or e['prev']
                continue
        if main and b'"type":"user"' in line and b'"type":"tool_result"' not in line:
            try:
                d = json.loads(line)
            except Exception:
                d = None
            if isinstance(d, dict) and d.get('type') == 'user':
                if not d.get('isMeta') and not d.get('isSidechain') and not d.get('isCompactSummary'):
                    e['turn'] += 1
                    e['turn_ts'] = ts_ms(d.get('timestamp'))
                e['prev'] = d.get('timestamp') or e['prev']
                if e['first'] is None:
                    e['first'] = ts_ms(d.get('timestamp'))
                continue
        mt = TS_RE.search(line)
        if mt:
            e['prev'] = mt.group(1).decode('ascii', 'ignore')
            if e['first'] is None:
                e['first'] = ts_ms(e['prev'])


# ---------------------------------------------------------------- 工作台运行时间段（.run/usage-runs.jsonl）
def log_run(path, ev, key, sid, t=None, w=None):
    """ev：start（工作台启动 CLI；接管升级前就在跑的 CLI 时 t 用它的启动时间）、sid（/clear 换了会话 ID）、end（CLI 退出）。
    w 是写下这一行的时间：从最早的 w 起才有时间段记录可依。"""
    now = w or time.time()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'t': round(t or now, 3), 'w': round(now, 3), 'ev': ev, 'key': key, 'sid': sid}) + '\n')
    except OSError:
        pass


def load_runs(path, live_keys=(), now=None):
    """→ (开始记录的时间毫秒或 None, {sid: [(开始毫秒, 结束毫秒)]})。没有 end 的：CLI 还在跑算到现在，否则最多算 12 小时。"""
    now = now or time.time()
    runs, first = {}, None
    try:
        with open(path, encoding='utf-8') as f:
            for l in f:
                try:
                    d = json.loads(l)
                    t, ev, key = float(d['t']), d['ev'], str(d['key'])
                except Exception:
                    continue
                w = float(d.get('w') or t)
                first = w if first is None else min(first, w)
                r = runs.setdefault(key, {'start': None, 'end': None, 'sids': set()})
                if ev == 'start':
                    r['start'] = t if r['start'] is None else min(r['start'], t)
                if ev in ('start', 'sid') and d.get('sid'):
                    r['sids'].add(d['sid'])
                if ev == 'end':
                    r['end'] = t
    except OSError:
        return None, {}
    live = set(live_keys or ())
    win = {}
    for key, r in runs.items():
        if r['start'] is None:
            continue
        end = r['end'] or (now if key in live else min(now, r['start'] + 12 * 3600))
        for sid in r['sids']:
            win.setdefault(sid, []).append((int(r['start'] * 1000) - 5000, int(end * 1000) + 5000))
    return (int(first * 1000) if first is not None else None), win


# ---------------------------------------------------------------- 扫描与统计
class Usage:
    def __init__(self, root, cache_path=None, runs_path=None, mark=WB_MARK, titles=None, label=None, live_keys=None, log=None):
        self.root, self.cache_path, self.runs_path, self.mark = root, cache_path, runs_path, mark
        self.titles = titles or (lambda sids: {})
        self.label = label or default_label
        self.live_keys = live_keys or (lambda: ())
        self.log = log or (lambda *a: None)
        self.files = {}
        self.lock = threading.RLock()
        self.rlock = threading.Lock()
        self.scanning, self.started = False, False
        self.ver, self.updated, self.saved_ver, self.last_save = 0, 0, 0, 0.0
        self.runs_sig, self.runs = None, (None, {})
        self.built = None  # (签名, 计入的回复列表)
        self.stats = {}

    # ---- 缓存
    def load_cache(self):
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            return
        if d.get('v') != CACHE_VER or d.get('root') != self.root or d.get('mark') != self.mark:
            return
        with self.lock:
            for p, e in (d.get('files') or {}).items():
                if isinstance(e, dict) and 'recs' in e:
                    self.files[p] = e
            self.ver += 1
            self.saved_ver = self.ver

    def save_cache(self, force=False):
        if not self.cache_path or self.ver == self.saved_ver:
            return
        if not force and time.time() - self.last_save < 60:
            return
        with self.lock:
            parts = []
            for p, e in self.files.items():
                s = e.get('_json')
                if s is None:
                    s = e['_json'] = json.dumps({k: v for k, v in e.items() if not k.startswith('_')}, ensure_ascii=False, separators=(',', ':'))
                parts.append(json.dumps(p, ensure_ascii=False) + ':' + s)
            ver = self.ver
        body = '{"v":%d,"root":%s,"mark":%s,"files":{%s}}' % (CACHE_VER, json.dumps(self.root, ensure_ascii=False),
                                                               json.dumps(self.mark), ','.join(parts))
        tmp = self.cache_path + '.tmp'
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(body)
            os.replace(tmp, self.cache_path)
            self.saved_ver, self.last_save = ver, time.time()
        except OSError as ex:
            self.log('保存用量缓存失败', repr(ex))

    # ---- 扫描
    def list_files(self):
        out = {}
        try:
            projs = list(os.scandir(self.root))
        except OSError:
            return out
        for pd in projs:
            if not pd.is_dir(follow_symlinks=False):
                continue
            try:
                ents = list(os.scandir(pd.path))
            except OSError:
                continue
            for e in ents:
                if e.name.endswith('.jsonl') and e.is_file(follow_symlinks=False):
                    out[e.path] = ('main', e.name[:-6])
                elif e.is_dir(follow_symlinks=False):
                    sub = os.path.join(e.path, 'subagents')
                    if not os.path.isdir(sub):
                        continue
                    for dp, dns, fns in os.walk(sub):
                        for fn in fns:
                            if fn.endswith('.jsonl'):
                                out[os.path.join(dp, fn)] = ('sub', e.name)
        return out

    def refresh(self):
        """按大小、修改时间增量解析有变化的文件；返回这次有没有变化。同一时间只有一个线程在刷新。"""
        with self.rlock:
            files = self.list_files()
            changed = False
            for p in [p for p in list(self.files) if p not in files]:
                with self.lock:
                    self.files.pop(p, None)
                changed = True
            for p, (kind, sid) in files.items():
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                e = self.files.get(p)
                if e and e['size'] == st.st_size and e['mtime'] == st.st_mtime_ns:
                    continue
                if self._parse(p, kind, sid, st, e):
                    changed = True
            if changed:
                with self.lock:
                    self.ver += 1
            self.updated = int(time.time() * 1000)
            return changed

    def _parse(self, p, kind, sid, st, e):
        with self.lock:
            if e is None or st.st_size < e['off']:
                e = new_entry(kind, sid, getattr(st, 'st_birthtime', st.st_ctime))
            if st.st_size > e['off']:
                try:
                    with open(p, 'rb') as f:
                        f.seek(e['off'])
                        data = f.read(st.st_size - e['off'])
                except OSError:
                    return False
                end = data.rfind(b'\n')
                if end >= 0:
                    parse_lines(e, data[:end + 1], self.mark)
                    e['off'] += end + 1
            e['size'], e['mtime'] = st.st_size, st.st_mtime_ns
            e.pop('_json', None)
            self.files[p] = e
        return True

    def start(self):
        """后台：先读缓存，再全量（增量）扫描一遍，完成后存缓存。"""
        if self.started:
            return
        self.started = self.scanning = True

        def run():
            t0 = time.time()
            try:
                self.load_cache()
                t1 = time.time()
                self.refresh()
                self.stats.update(cache_load_s=round(t1 - t0, 3), first_scan_s=round(time.time() - t1, 3))
                self.save_cache(force=True)
            except Exception as ex:
                self.log('用量扫描异常', repr(ex))
            finally:
                self.scanning = False
                self.log('用量扫描完成', len(self.files), '个文件', f'{time.time() - t0:.1f}s')
        threading.Thread(target=run, daemon=True).start()

    def ensure_fresh(self):
        if not self.started:
            self.start()
            return
        if self.scanning:
            return
        if self.refresh() and time.time() - self.last_save > 60:
            threading.Thread(target=self.save_cache, daemon=True).start()

    # ---- 归属与汇总
    def _runs(self):
        if not self.runs_path:
            return None, {}
        try:
            st = os.stat(self.runs_path)
            sig = (st.st_size, st.st_mtime_ns, int(time.time() // 30))
        except OSError:
            sig = None
        if sig != self.runs_sig:
            self.runs_sig = sig
            self.runs = load_runs(self.runs_path, self.live_keys()) if sig else (None, {})
        return self.runs

    def build(self):
        """去重、判断归属，返回计入工作台的回复（按时间排序），按版本缓存。
        每项：(时间毫秒, sid, 模型, 输入, 输出, 缓存读, 缓存写, fast, 耗时毫秒, 费用, 本地日期, 本地小时, 缓存省下的钱)"""
        since, win = self._runs()
        sig = (self.ver, self.runs_sig)
        if self.built and self.built[0] == sig:
            return self.built[1]

        def in_run(sid, t):
            return any(a <= t <= b for a, b in win.get(sid, ()))
        with self.lock:
            items = list(self.files.items())
            best = {}
            for p, e in items:
                born = e['born']
                for r in e['recs']:
                    k = r[K_KEY]
                    rank = (r[K_FORK], born, p)
                    cur = best.get(k)
                    if cur is None or rank < cur[0]:
                        best[k] = (rank, p, e, r)
            mains = {os.path.join(os.path.dirname(p), e['sid']): e for p, e in items if e['kind'] == 'main'}
            sub_cls = {}
            all_sids, wb_sids = set(), set()
            out = []
            for rank, p, e, r in best.values():
                sid = e['sid']
                all_sids.add(sid)
                t = r[K_TS]
                if e['kind'] == 'main':
                    sums = e['sums']
                    seg = r[K_SEG]
                    if seg < len(sums):
                        s = sums[seg]
                        ok = bool(s[S_WB]) and (r[K_TURN] == s[S_TURN] or since is None or t < since or in_run(sid, t))
                    else:
                        ok = in_run(sid, t)
                else:
                    c = sub_cls.get(p, 0)
                    if c == 0:
                        c = sub_cls[p] = self._sub_class(p, e, mains, since, in_run)
                    ok = c if c is not None else in_run(sid, t)
                if not ok:
                    continue
                wb_sids.add(sid)
                ms = (r[K_TE] - r[K_T0]) if r[K_T0] and r[K_TE] > r[K_T0] else None
                if ms is not None and ms > 3600_000:
                    ms = None
                lt = time.localtime(t / 1000)
                out.append((t, sid, r[K_MODEL], r[K_IN], r[K_OUT], r[K_CR], r[K_CW], bool(r[K_FAST]), ms,
                            cost_of(r[K_MODEL], r[K_IN], r[K_OUT], r[K_CR], r[K_CW], r[K_FAST]),
                            time.strftime('%Y-%m-%d', lt), lt.tm_hour, cache_saved(r[K_MODEL], r[K_IN], r[K_CR], r[K_CW], r[K_FAST])))
        out.sort(key=lambda x: x[0])
        self.stats.update(sessions_all=len(all_sids), sessions_wb=len(wb_sids), messages=len(best), counted=len(out))
        self.built = (sig, out)
        return out

    def _sub_class(self, p, e, mains, since, in_run):
        """子代理文件按启动时间落在父会话哪一轮：True/False；落在最后一轮之后（或找不到父记录）返回 None，逐条按时间段判断。"""
        launch = e.get('first') or (e['recs'][0][K_TS] if e['recs'] else None)
        d = os.path.dirname(p)
        while d and os.path.basename(d) != e['sid'] and d != os.path.dirname(d):
            d = os.path.dirname(d)
        par = mains.get(d)
        if not par or launch is None:
            return None
        for s in par['sums']:
            if s[S_TS] >= launch:
                if not s[S_WB]:
                    return False
                if since is None or launch < since or (s[S_TURN_TS] and launch >= s[S_TURN_TS]):
                    return True
                return in_run(e['sid'], launch)
        return None

    # ---- 接口
    @staticmethod
    def _sum(rows):
        return (len(rows), sum(x[3] for x in rows), sum(x[4] for x in rows), sum(x[5] for x in rows),
                sum(x[6] for x in rows), sum(x[9] for x in rows))

    @staticmethod
    def _row_speed(x):
        return x[4] / (x[8] / 1000) if x[8] and x[8] >= 200 and x[4] > 0 else None

    def _speed(self, rows):
        v = [s for s in (self._row_speed(x) for x in rows) if s is not None]
        return round(statistics.median(v), 1) if v else None

    def summary(self, now=None):
        self.ensure_fresh()
        rows = self.build()
        t0 = day_start(now, 0) * 1000
        n, i, o, cr, cw, c = self._sum([x for x in rows if x[0] >= t0])
        return {'scanning': self.scanning, 'today': {'requests': n, 'tokens': i + o + cr + cw, 'cost': round(c, 4)}}

    def query(self, rng='today', now=None):
        self.ensure_fresh()
        rows = self.build()
        rng = rng if rng in ('today', '7d', '30d') else 'today'
        now = now or time.time()
        days = {'today': 1, '7d': 7, '30d': 30}[rng]
        start = day_start(now, days - 1) * 1000
        sel = [x for x in rows if x[0] >= start]
        n, i, o, cr, cw, c = self._sum(sel)
        tok = i + o + cr + cw
        kpi = {'requests': n, 'input': i, 'output': o, 'cacheRead': cr, 'cacheWrite': cw, 'tokens': tok, 'cost': round(c, 4),
               'cacheHit': round(cr / (i + cr + cw), 4) if i + cr + cw else 0, 'speed': self._speed(sel),
               'saved': round(sum(x[12] for x in sel), 4)}
        t_today = day_start(now, 0) * 1000
        tn, ti, to, tcr, tcw, tc = self._sum([x for x in rows if x[0] >= t_today])

        def add(a, x):
            a[0] += 1
            a[1] += x[3] + x[4] + x[5] + x[6]
            a[2] += x[4]
            a[3] += x[9]

        # 柱状图：今天按小时 24 个，7 天、30 天按天；每个桶带按模型拆开的 {模型id: {tokens, cost, requests}}（按 tokens 降序）
        def bucket(key, label, a, bm):
            ms = sorted(bm.items(), key=lambda kv: -kv[1][1])
            return {'key': key, 'label': label, 'requests': a[0], 'tokens': a[1], 'output': a[2], 'cost': round(a[3], 4),
                    'models': {m: {'tokens': v[1], 'cost': round(v[3], 4), 'requests': v[0]} for m, v in ms}}
        buckets = []
        agg, aggm, day = {}, {}, day_key(now, 0)
        for x in sel:
            if rng == 'today' and x[10] != day:
                continue
            k = x[11] if rng == 'today' else x[10]
            add(agg.setdefault(k, [0, 0, 0, 0.0]), x)
            add(aggm.setdefault(k, {}).setdefault(x[2], [0, 0, 0, 0.0]), x)
        if rng == 'today':
            for h in range(24):
                buckets.append(bucket(f'{day} {h:02d}', f'{h:02d}:00', agg.get(h, [0, 0, 0, 0.0]), aggm.get(h, {})))
        else:
            for k in range(days - 1, -1, -1):
                d = day_key(now, k)
                buckets.append(bucket(d, d[5:], agg.get(d, [0, 0, 0, 0.0]), aggm.get(d, {})))
        # 模型：按 tokens 降序，全部返回
        bym = {}
        for x in sel:
            bym.setdefault(x[2], []).append(x)
        models = []
        for m, xs in bym.items():
            mn, mi, mo, mcr, mcw, mc = self._sum(xs)
            mt = mi + mo + mcr + mcw
            models.append({'id': m, 'label': self.label(m), 'requests': mn, 'tokens': mt, 'output': mo, 'cost': round(mc, 4),
                           'share': round(mt / tok, 4) if tok else 0, 'speed': self._speed(xs)})
        models.sort(key=lambda m: -m['tokens'])
        # 近一年活跃度（最近 371 天，与 range 无关）
        hs = day_start(now, HEAT_DAYS - 1) * 1000
        hagg = {}
        for x in rows:
            if x[0] >= hs:
                a = hagg.setdefault(x[10], [0, 0])
                a[0] += x[3] + x[4] + x[5] + x[6]
                a[1] += 1
        heat = []
        for k in range(HEAT_DAYS - 1, -1, -1):
            d = day_key(now, k)
            a = hagg.get(d, (0, 0))
            heat.append({'date': d, 'tokens': a[0], 'requests': a[1]})
        # 用量前 10 的对话、最近 100 条
        bys = {}
        for x in sel:
            add(bys.setdefault(x[1], [0, 0, 0, 0.0]), x)
        top = sorted(bys.items(), key=lambda kv: -kv[1][1])[:10]
        recent_rows = sel[-100:][::-1]
        try:
            titles = self.titles({s for s, _ in top} | {x[1] for x in recent_rows}) or {}
        except Exception:
            titles = {}
        sessions = [{'sid': s, 'title': titles.get(s) or '', 'requests': a[0], 'tokens': a[1], 'cost': round(a[3], 4)} for s, a in top]
        recent = []
        for x in recent_rows:
            sp = self._row_speed(x)
            recent.append({'ts': x[0], 'sid': x[1], 'title': titles.get(x[1]) or '', 'model': x[2], 'label': self.label(x[2]), 'fast': x[7],
                           'input': x[3], 'output': x[4], 'cacheRead': x[5], 'cacheWrite': x[6], 'ms': x[8],
                           'speed': round(sp, 1) if sp is not None else None, 'cost': round(x[9], 6)})
        return {'scanning': self.scanning, 'updated': self.updated, 'range': rng, 'unit': 'hour' if rng == 'today' else 'day',
                'kpi': kpi, 'today': {'requests': tn, 'tokens': ti + to + tcr + tcw, 'cost': round(tc, 4)},
                'buckets': buckets, 'models': models, 'heat': heat, 'recent': recent, 'sessions': sessions}


def day_start(now=None, back=0):
    """本机时区 back 天前的零点（秒）。"""
    d = datetime.fromtimestamp(now or time.time()).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=back)
    return d.timestamp()


def day_key(now=None, back=0):
    return (datetime.fromtimestamp(now or time.time()) - timedelta(days=back)).strftime('%Y-%m-%d')


# ---------------------------------------------------------------- 请求头自检用的本机抓包网关
class CaptureGateway:
    """只监听 127.0.0.1 的临时端口：记下第一条 POST /v1/messages 的 User-Agent 和 x-app，回一句很短的假回复，不转发任何请求。"""

    def __init__(self):
        self.first, self.paths = None, []
        self.got = threading.Event()
        gw = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                gw.paths.append('GET ' + self.path)
                self._json({'data': [], 'has_more': False})

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(n) if n else b''
                gw.paths.append('POST ' + self.path)
                path = self.path.split('?', 1)[0]
                if path == '/v1/messages/count_tokens':
                    return self._json({'input_tokens': 1})
                if path != '/v1/messages':
                    return self._json({})
                if gw.first is None:
                    gw.first = {'ua': self.headers.get('User-Agent', ''), 'xapp': self.headers.get('x-app', '')}
                    gw.got.set()
                try:
                    req = json.loads(raw or b'{}')
                except Exception:
                    req = {}
                mid = 'msg_' + uuid.uuid4().hex[:20]
                usage = {'input_tokens': 1, 'output_tokens': 1, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}
                model = req.get('model') or 'x'
                if not req.get('stream'):
                    return self._json({'id': mid, 'type': 'message', 'role': 'assistant', 'model': model,
                                       'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn', 'stop_sequence': None, 'usage': usage})
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                evs = [('message_start', {'type': 'message_start', 'message': {'id': mid, 'type': 'message', 'role': 'assistant', 'model': model,
                                                                               'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': usage}}),
                       ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
                       ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'ok'}}),
                       ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                       ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 1}}),
                       ('message_stop', {'type': 'message_stop'})]
                try:
                    for name, data in evs:
                        self.wfile.write(f'event: {name}\ndata: {json.dumps(data)}\n\n'.encode())
                    self.wfile.flush()
                except OSError:
                    pass

        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass
