"""第十六轮 P：Codex、Antigravity（agy）对话。

和 Claude 完全分开：Claude 仍是伪终端里的交互式 claude（server.py、ptyd.py 一字未改）；这里的两家走各自的协议进程，
由 server 直接拉起和持有（不进 ptyd）：
- Codex：`codex app-server`（stdio JSON-RPC，见 prov_codex.py）；
- Antigravity：`agy acp`（stdio Agent Client Protocol，见 prov_acp.py）。
一个提供方一个进程，承载多个对话；全部空闲 10 分钟后回收，下次发消息用原生 ID 续接（Codex 的 threadId、ACP 的 sessionId）。

时间线：工作台自己存一份规范化记录，每个对话一个 jsonl（`data/conv/<sid>.jsonl`），条目格式和 Claude 时间线一样
（k=user/text/thought/tool/error/note），界面原样渲染。记录的行：
  {"t":"meta", ...}           对话信息（第一行完整，后面的是增量：原生 ID、标题、选项）
  {"t":"new","it":{...}}      新条目
  {"t":"patch","p":{...}}     改条目（工具结果等）
  {"t":"turn_start", ...}     一轮开始（server 重启后据此认出“上次回复被中断”）
  {"t":"turn_end", ...}       一轮结束：用量和时间，按调研文档（r16/research/多智能体与用量-调研与设计.md 第 7 节）的约定：
      ts、turnId、provider、model、effort、startedAt、firstTokenAt、endedAt（毫秒）、status（completed / interrupted / failed）、
      error{kind,text}、usage{input,cachedInput,cacheWrite,output,reasoning}、usageSrc、ctx{used,size}、cost{amount,currency,src}；
      拿不到的字段不写（不写 0）。另加 uid（这一轮的用户消息条目）、why（中断原因 user / shutdown / other）、stopReason（原生结束原因）

server.py 只加了一行挂接（import 后 setup(globals())）。这里通过替换 API 表里的处理函数接管这两家对话的接口，
其余对话原样交给原来的函数；另外包了 load_marks（启动时补中断标记）。
"""
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid

NS = {}
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('CWB_DATA_DIR') or os.path.join(ROOT, 'data')
CONV_DIR = os.path.join(DATA_DIR, 'conv')
IDLE_REAP = float(os.environ.get('CWB_PROV_IDLE') or 600)  # 提供方进程全部空闲这么久就回收（测试可调短）
STOP_GRACE = float(os.environ.get('CWB_PROV_STOP_GRACE') or 20)  # 复核修复 4：点停止后对方这么久还没结束这一轮，工作台这边结束（测试可调短）
LIVE_MAX, LIVE_STEP, LIVE_FULL = 6000, 2000, 2.0  # 复核修复 7：实时预览只推末尾一屏（和 Claude 一样最多 6000 字），平时只推增量，每 2 秒一次整段
LABEL = {'codex': 'Codex', 'agy': 'Antigravity'}
CONVS = {}            # sid -> PConv
ADAPTERS = {}         # 'codex' / 'agy' -> 适配器
ORIG = {}             # 被包了一层的原接口（测试里可以换掉，免得真的去重启）
LOCK = threading.RLock()
SID_RE = re.compile(r'^[0-9a-fA-F-]{36}$')
TURN_END_KEYS = ('ts', 'turnId', 'provider', 'model', 'effort', 'startedAt', 'firstTokenAt', 'endedAt', 'status', 'error', 'usage',
                 'usageSrc', 'ctx', 'cost', 'uid', 'why', 'stopReason')
USAGE_FIELDS = ('input', 'cachedInput', 'cacheWrite', 'output', 'reasoning')
STATUS = {'done': 'completed', 'interrupted': 'interrupted', 'error': 'failed'}
MAX_ITEMS_OPEN = 600
HOOKS = {}            # 第十七轮 M：协作（collab.py）挂在这里：codex_thread（建、续接线程时加参数）、codex_elicitation（MCP 工具授权）


def log(*a):
    try:
        NS['log']('[提供方]', *a)
    except Exception:
        pass


def emit(ev):
    try:
        NS['BUS'].emit(ev)
    except Exception:
        pass


def now():
    return time.time()


def ms(t):
    return int(round(t * 1000))


# ---------------------------------------------------------------- 一个对话
class PConv:
    """一个 Codex / Antigravity 对话：规范化记录 + 运行状态。条目由适配器通过 add / patch 写入。"""

    def __init__(self, sid, prov, cwd, created=None):
        self.sid, self.prov, self.cwd = sid, prov, cwd
        self.meta = {'v': 1, 'prov': prov, 'cwd': cwd, 'created': created or now(), 'native': '', 'title': '', 'opts': {}}
        self.items, self.byid, self.n = [], {}, 0
        self.lock = threading.RLock()
        self.path = os.path.join(CONV_DIR, sid + '.jsonl')
        self.persisted = os.path.exists(self.path)
        self.mtime = self.meta['created']
        # 运行状态（不落盘）
        self.attached = False      # 已在提供方进程里打开（thread/start、resume；session/new、load）
        self.busy = False
        self.since = 0.0
        self.live = ''             # 正在流式输出的正文（界面按“实时预览”显示）
        self.spin = ''
        self.queue = []            # 回复中发的消息：[(text, images)]
        self.turn = None           # 本轮：{t0, first, uid, model, effort, usage{}, native, err}
        self.dirty = False         # 运行状态有变化，后台循环按节奏推给界面
        self.cmds = []             # 提供方自己的 / 命令（ACP available_commands_update）
        self.ctx = None            # {used, window}
        self.last_usage = None
        self.err = ''              # 最近一次打开失败的原因
        self.dropped = False
        self.pv = {}               # 适配器自己的运行状态
        self.stop_req = False      # 复核修复 1：这一轮点过停止（还在连接时也算），占住新一轮时（claim）才重置
        self.tseq = 0              # 第几轮（claim 时加一）：兜底计时按它判断是不是还是同一轮
        self.loaded = True         # 复核修复 7：启动时只读对话信息，条目等打开时再读（ensure_loaded）
        self.first_user = ''       # 没读条目时用来当标题
        self.lp = ('', 0, 0.0)     # 上次推给界面的实时预览：(全文, 起点, 上次整段推送的时间)

    # ---- 记录
    def write(self, row):
        with self.lock:
            if self.dropped:
                return
            os.makedirs(CONV_DIR, exist_ok=True)
            if not self.persisted:
                with open(self.path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(dict(self.meta, t='meta'), ensure_ascii=False) + '\n')
                self.persisted = True
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
            self.mtime = now()

    def set_meta(self, **kw):
        ch = {k: v for k, v in kw.items() if self.meta.get(k) != v}
        if not ch:
            return
        self.meta.update(ch)
        if self.persisted:
            self.write(dict(ch, t='meta'))

    def replay(self):
        """读记录文件，还原条目和对话信息；返回最后一轮是否没结束（server 中途重启）。"""
        open_turn = None
        try:
            f = open(self.path, encoding='utf-8')
        except OSError:
            return None
        with f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                t = r.get('t')
                if t == 'meta':
                    m = {k: v for k, v in r.items() if k != 't'}
                    if 'opts' in m and isinstance(m['opts'], dict):
                        m['opts'] = dict(m['opts'])
                    self.meta.update(m)
                elif t == 'new' and isinstance(r.get('it'), dict):
                    it = r['it']
                    if it.get('id') in self.byid:
                        self.byid[it['id']].update(it)
                        continue
                    self.items.append(it)
                    self.byid[it.get('id')] = it
                    m = re.match(r'^p(\d+)', str(it.get('id') or ''))
                    if m:
                        self.n = max(self.n, int(m.group(1)))
                elif t == 'patch' and isinstance(r.get('p'), dict):
                    it = self.byid.get(r['p'].get('id'))
                    if it:
                        it.update({k: v for k, v in r['p'].items() if k != 'id'})
                elif t == 'turn_start':
                    open_turn = r
                elif t == 'turn_end':
                    open_turn = None
                    self.last_usage = r
        self.cwd = self.meta.get('cwd') or self.cwd
        self.prov = self.meta.get('prov') or self.prov
        try:
            self.mtime = os.path.getmtime(self.path)
        except OSError:
            pass
        self.persisted = True
        return open_turn

    def peek(self):
        """复核修复 7：启动时只读对话信息（meta、第一条用户消息、最后一轮有没有结束），不把条目读进内存。"""
        open_turn = None
        try:
            f = open(self.path, 'rb')
        except OSError:
            return None
        with f:
            for line in f:
                try:
                    if line.rstrip().endswith(b'"t": "meta"}'):
                        r = json.loads(line)
                        if r.get('t') == 'meta':
                            m = {k: v for k, v in r.items() if k != 't'}
                            if 'opts' in m and isinstance(m['opts'], dict):
                                m['opts'] = dict(m['opts'])
                            self.meta.update(m)
                    elif not self.first_user and line.startswith(b'{"t": "new", "it": {"k": "user"'):
                        self.first_user = ' '.join(str(json.loads(line)['it'].get('text') or '').split())[:60]
                    elif line.startswith(b'{"t": "turn_start"'):
                        open_turn = json.loads(line)
                    elif line.startswith(b'{"t": "turn_end"'):
                        open_turn = None
                except Exception:
                    continue
        self.cwd = self.meta.get('cwd') or self.cwd
        self.prov = self.meta.get('prov') or self.prov
        try:
            self.mtime = os.path.getmtime(self.path)
        except OSError:
            pass
        self.persisted = True
        self.loaded = False
        return open_turn

    def ensure_loaded(self):
        if self.loaded:
            return
        with self.lock:
            if self.loaded:
                return
            self.items, self.byid, self.n = [], {}, 0
            self.replay()
            self.loaded = True

    # ---- 条目
    def add(self, it, persist=True):
        self.ensure_loaded()
        with self.lock:
            self.n += 1
            it['id'] = 'p%d' % self.n
            self.items.append(it)
            self.byid[it['id']] = it
            if persist:
                self.write({'t': 'new', 'it': it})
        emit({'type': 'items', 'sid': self.sid, 'new': [it], 'patch': [], 'open': self.busy})
        return it['id']

    def patch(self, iid, persist=True, **kw):
        self.ensure_loaded()
        with self.lock:
            it = self.byid.get(iid)
            if not it:
                return
            it.update(kw)
            p = dict(kw, id=iid)
            if persist:
                self.write({'t': 'patch', 'p': p})
        emit({'type': 'items', 'sid': self.sid, 'new': [], 'patch': [p], 'open': self.busy})

    def close_tools(self, why):
        """这一轮结束（或被中断）：还没拿到结果的工具不再算“运行中”。"""
        self.ensure_loaded()
        for it in list(self.items[-200:]):
            if it.get('k') == 'tool' and not it.get('done'):
                self.patch(it['id'], done=True, noresult=why)

    # ---- 状态
    def title(self):
        names = NS['load_conf']().get('names', {})
        if names.get(self.sid):
            return names[self.sid]
        if self.meta.get('title'):
            return self.meta['title']
        if not self.loaded:
            return self.first_user or '新对话'
        for it in self.items:
            if it.get('k') == 'user' and it.get('text'):
                return ' '.join(it['text'].split())[:60]
        return '新对话'

    def opts(self):
        return dict(self.meta.get('opts') or {})

    def attention(self):
        return any(e['req'].get('sid') == self.sid for e in list(NS['PERMS'].values()))

    def live_view(self):
        """复核修复 7：实时预览只给末尾一屏。起点按 2000 字一档往后挪，再对齐到下一个换行，
        这样起点不会每次都变（界面按开头认是不是同一段），增量也能接上。"""
        t = self.live if self.busy else ''
        if len(t) <= LIVE_MAX:
            return 0, t
        frm = ((len(t) - LIVE_MAX) // LIVE_STEP + 1) * LIVE_STEP
        nl = t.find('\n', frm, frm + 400)
        if nl >= 0:
            frm = nl + 1
        return frm, t[frm:]

    def public_state(self):
        a = ADAPTERS.get(self.prov)
        o = self.opts()
        frm, lv = self.live_view()
        alive = bool(self.attached or self.busy or self.queue)
        return {'alive': alive, 'ready': True, 'busy': bool(self.busy), 'spinner': self.spin if self.busy else '', 'status': '',
                'mode': o.get('mode') or 'default', 'effort': o.get('effort') or '', 'dialog': None,
                'attention': self.attention(), 'sid': self.sid, 'turn': bool(self.busy), 'notice': '', 'since': self.since,
                'dirs': [], 'done_at': NS['MARKS']['done_at'].get(self.sid, 0), 'ctx': None, 'pending': None,
                'live': lv, 'liveFrom': frm, 'liveEnd': len(self.live) if lv else 0, 'bg': 0, 'exit': None, 'prov': self.prov,
                'provLabel': LABEL.get(self.prov, self.prov), 'queued': len(self.queue),
                'native': self.meta.get('native') or '', 'pvctx': self.ctx, 'model': (a.cur_model(self) if a else '') or '',
                'pvcur': a.live_opts(self) if a and hasattr(a, 'live_opts') else {}}  # 会话当前的模型、思考强度、模式（药丸显示）

    def push_state(self, sessions=False):
        st = self.public_state()
        full, frm0, t_full = self.lp
        cur = self.live if self.busy else ''
        if cur and full and st['liveFrom'] == frm0 and cur.startswith(full) and time.time() - t_full < LIVE_FULL:
            # 复核修复 7：正文只是往后长了，只推新增的部分（界面 ux-v.js 接到上次的后面）
            ev = dict(st, liveAdd=cur[len(full):], liveAt=len(full))
            ev.pop('live', None)
            self.lp = (cur, frm0, t_full)
        else:
            ev = st
            self.lp = (cur, st['liveFrom'], time.time())
        emit({'type': 'runner', 'sid': self.sid, 'state': ev})
        if sessions:
            emit({'type': 'sessions'})
        self.dirty = False

    # ---- 一轮
    def claim(self):
        """复核修复 1：占住新的一轮（在发消息的线程开始之前同步调用）。停止、立即发送的标记在这里重置，
        之后连接（thread/start、session/new）期间点的停止不会再被冲掉。"""
        with self.lock:
            self.busy = True
            self.tseq += 1
            self.stop_req = False
            self.pv.update(user_int=False, int_req=False, steer_req=False)

    def begin_turn(self, text, images, model='', effort='', wake=None):
        NS['clear_interrupted'](self.sid)
        if wake:  # 第十七轮 M：子任务结果交回父对话（叫醒）：时间线上画成灰色系统行，不是用户气泡；发给对方的正文照常
            it = {'k': 'sys', 'tag': wake.get('tag') or '子任务', 'text': wake.get('note') or '子任务已完成，结果已交回',
                  'body': text[:20000], 'turn': True, 'stat': None}
        else:
            it = {'k': 'user', 'text': text, 'stat': None}
        if images:
            it['imgs'] = len(images)
            it['pimgs'] = list(images)[:6]
        uid = self.add(it)
        t0 = now()
        self.turn = {'t0': t0, 'first': None, 'uid': uid, 'model': model, 'effort': effort, 'native': '',
                     'usage': {}, 'err': None, 'ctx': None, 'trigger': (wake or {}).get('trigger')}
        self.write({'t': 'turn_start', 'ts': ms(t0), 'uid': uid, 'provider': self.prov})
        self.busy, self.since, self.live, self.spin = True, t0, '', '思考中…'
        self.push_state(sessions=True)
        return uid

    def first_output(self):
        if self.turn and not self.turn['first']:
            self.turn['first'] = now()

    def end_turn(self, reason, error_type=None, error_msg=None, stop=None, why='', ended=None):
        """reason：done 完成 / interrupted 中断 / error 出错。写一行 turn_end（用量和时间），更新界面。"""
        tr = self.turn
        if not tr:
            self.busy = False
            self.push_state(sessions=True)
            return
        a = ADAPTERS.get(self.prov)
        if a:
            try:
                a.flush(self)  # 还没写进时间线的正文、思考先落地
            except Exception as e:
                log('结束一轮时整理正文出错', self.sid[:8], repr(e))
        self.close_tools('已中断' if reason == 'interrupted' else '出错，未完成' if reason == 'error' else '没有返回结果')
        t1 = ended or now()
        u = {k: v for k, v in (tr.get('usage') or {}).items() if k in USAGE_FIELDS and isinstance(v, int)}
        ctx = tr.get('ctx') or self.ctx or {}
        st = {'secs': max(0, round(t1 - tr['t0'])), 'out': u.get('output') or 0, 'ctx': ctx.get('used') or 0}
        self.patch(tr['uid'], stat=st)
        row = {'t': 'turn_end', 'ts': ms(now()), 'turnId': tr.get('native') or tr['uid'], 'provider': self.prov,
               'model': tr.get('model'), 'effort': tr.get('effort'), 'startedAt': ms(tr['t0']),
               'firstTokenAt': ms(tr['first']) if tr.get('first') else None, 'endedAt': ms(t1), 'status': STATUS.get(reason, reason),
               'error': {'kind': error_type or 'unknown', 'text': (error_msg or '')[:500]} if reason == 'error' else None,
               'usage': u or None, 'usageSrc': 'protocol' if u else None,
               'ctx': {'used': ctx['used'], 'size': ctx.get('window')} if isinstance(ctx.get('used'), int) and ctx.get('used') else None,
               'cost': (tr.get('usage') or {}).get('cost'), 'uid': tr['uid'], 'why': (why or 'other') if reason == 'interrupted' else None,
               'stopReason': stop}
        row = {k: v for k, v in row.items() if v is not None or k == 'usageSrc'}  # 拿不到的不写（不写 0）；usageSrc 按约定可以是 null
        if isinstance(row.get('ctx'), dict):
            row['ctx'] = {k: v for k, v in row['ctx'].items() if v is not None}
        if ended:
            row['endedAtEstimated'] = True  # server 重启后补记的：结束时间取记录文件最后修改的时间
        if tr.get('trigger'):
            row['trigger'] = tr['trigger']  # 第十七轮 M：这一轮是工作台交回子任务结果时起的（cwb_task）
        self.write(row)
        self.last_usage = row
        if reason == 'interrupted':
            self.add({'k': 'note', 'text': '已中断'})  # 和 Claude 一样：结束行显示“已中断”（server 重启导致的也是，免得显示成“已完成”）
        self.turn = None
        self.busy, self.since, self.live, self.spin = False, 0.0, '', ''
        if reason != 'interrupted' or why != 'shutdown':
            NS['mark_done'](self.sid)  # 沿用未读：一轮结束的时间
        self.push_state(sessions=True)
        if self.queue and reason != 'error':
            text, images = self.queue.pop(0)
            self.claim()  # 先占住，免得这时用户发的消息另开一轮
            threading.Thread(target=start_turn, args=(self, text, images), daemon=True).start()
        elif self.queue:
            self.unsent()

    def unsent(self):
        """复核修复 6：这一轮出错或进程退出结束时，排队的消息（含“拒绝并说明”的说明）不自动发，也不悄悄丢：
        时间线里列出来，正开着这个对话的窗口把它们放回输入框（输入框是空的时候）。"""
        q, self.queue = list(self.queue), []
        lines = []
        for i, (text, images) in enumerate(q, 1):
            extra = f'（附 {len(images)} 张图片）' if images else ''
            lines.append(f'{i}. {text}{extra}' if len(q) > 1 else f'{text}{extra}')
        self.add({'k': 'error', 'etype': '没发出去', 'text': '这一轮没有完成，下面这几条排队的消息没发出去，可以重新发送：\n\n' + '\n\n'.join(lines)})
        emit({'type': 'pv-unsent', 'sid': self.sid, 'texts': [t for t, _ in q if t], 'images': [p for _, ims in q for p in ims]})
        self.push_state()

    def set_live(self, text):
        self.live = text
        self.dirty = True

    def set_spin(self, text):
        if self.spin != text:
            self.spin = text
            self.dirty = True


# ---------------------------------------------------------------- 对话表
def owns(sid):
    return bool(sid) and sid in CONVS


def get(sid):
    c = CONVS.get(sid)
    if not c:
        raise ValueError('找不到这个对话')
    return c


def scan():
    """启动时读 data/conv 下的全部记录的对话信息（条目打开时再读）。最后一轮没结束的（server 重启或崩溃时正在回复）补“上次回复被中断”。"""
    try:
        names = [n for n in os.listdir(CONV_DIR) if n.endswith('.jsonl')]
    except OSError:
        names = []
    for n in names:
        sid = n[:-6]
        if not SID_RE.match(sid) or sid in CONVS:
            continue
        c = PConv(sid, '', '')
        try:
            open_turn = c.peek()  # 复核修复 7：只读对话信息，条目打开时再读
        except Exception as e:
            log('读取记录失败', n, repr(e))
            continue
        if c.prov not in LABEL:
            continue
        CONVS[sid] = c
        if open_turn:
            c.ensure_loaded()
            t0 = (open_turn.get('ts') / 1000) if isinstance(open_turn.get('ts'), (int, float)) else c.mtime
            c.turn = {'t0': float(t0), 'first': None, 'uid': open_turn.get('uid') or '', 'model': '',
                      'effort': '', 'native': '', 'usage': {}, 'err': None, 'ctx': None}
            c.busy = True
            c.end_turn('interrupted', stop=None, why='shutdown', ended=c.mtime)
            NS['mark_interrupted'](sid, 'shutdown')
            log('上次回复被中断', LABEL[c.prov], sid[:8])


# ---------------------------------------------------------------- 发消息、开一轮
def start_turn(c, text, images, wake=None):
    """开一轮：先把用户消息写进时间线（界面立刻看到），再确保对话已在提供方进程里打开，最后发出。
    第十七轮 M：wake 不为空时是交回子任务结果，时间线上画成系统行（见 PConv.begin_turn）。"""
    a = ADAPTERS[c.prov]
    c.ensure_loaded()
    c.begin_turn(text, images, wake=wake)
    tr = c.turn
    if not c.attached:
        c.set_spin(f'正在连接 {LABEL.get(c.prov, c.prov)}…')
        c.push_state()
    try:
        a.ensure_attached(c)
    except Exception as e:
        c.err = str(e)
        log('打开对话失败', c.sid[:8], repr(e))
        if c.turn is tr:
            c.add({'k': 'error', 'text': str(e), 'etype': '启动失败'})
            c.end_turn('error', error_type='start_failed', error_msg=str(e))
        return
    if c.turn is not tr:
        return  # 连接期间这一轮已经结束了（进程退出等）
    if c.stop_req:  # 复核修复 1：连接期间点了停止：不发出，按“已中断”结束
        c.end_turn('interrupted', why='user')
        return
    tr['rpc'] = a.rpc  # 复核修复 4：记下这一轮在哪个进程上，进程退出时据此结束
    tr['model'], tr['effort'] = a.turn_model(c), a.turn_effort(c)
    c.set_spin('思考中…')
    try:
        a.send(c, text, images)
    except Exception as e:
        log('发消息失败', c.sid[:8], repr(e))
        if c.turn is tr:
            c.add({'k': 'error', 'text': '发送失败：' + str(e), 'etype': '接口出错'})
            c.end_turn('error', error_type='send_failed', error_msg=str(e))


# ---------------------------------------------------------------- 接口
def payload(c, b=None):
    b = b or {}
    items = list(c.items)
    hid = max(0, len(items) - NS['window_lim'](items, b))
    toc, chg = NS['hidden_index'](items[:hid])
    a = ADAPTERS.get(c.prov)
    return {'sid': c.sid, 'cwd': c.cwd, 'title': c.title(), 'items': items[hid:], 'hidden': hid, 'toc': toc, 'chg': chg,
            'runner': c.public_state(), 'model': (a.cur_model(c) if a else '') or None, 'recent': False,
            'opts': c.opts(), 'perms': [e['req'] for e in list(NS['PERMS'].values()) if e['req'].get('sid') == c.sid],
            'open': False, 'mtime': c.mtime, 'exit': None, 'interrupted': NS['MARKS']['interrupted'].get(c.sid),
            'compacts': [], 'prov': c.prov, 'provLabel': LABEL.get(c.prov, c.prov), 'native': c.meta.get('native') or '',
            'pvCmds': c.cmds, 'pvCaps': a.caps(c) if a else {}}


def api_prov_new(b):
    prov = str(b.get('prov') or '')
    if prov not in ADAPTERS:
        raise ValueError('没有这个提供方')
    cwd = (b.get('cwd') or '').rstrip('/') or '/'
    if not os.path.isdir(cwd):
        raise ValueError('文件夹不存在')
    cwd = os.path.realpath(cwd)  # 和 Claude 的记录一致（CLI 记的是真实路径，/tmp → /private/tmp），左栏才能归到同一组
    a = ADAPTERS[prov]
    st = a.status()
    if not st.get('installed'):
        raise ValueError(st.get('hint') or (LABEL[prov] + ' 没有安装'))
    sid = str(uuid.uuid4())
    c = PConv(sid, prov, cwd)
    opts = {k: v for k, v in (b.get('opts') or {}).items() if k in ('model', 'effort', 'mode') and isinstance(v, str) and v}
    c.meta['opts'] = opts
    CONVS[sid] = c
    log('新建对话', LABEL[prov], sid[:8], cwd)
    emit({'type': 'sessions'})
    return payload(c)


def api_open(b):
    c = get(b.get('sid') or '')
    c.ensure_loaded()
    return payload(c, b)


def api_page(b):
    c = get(b.get('sid') or '')
    before = b.get('before') or ''
    n = max(1, min(int(b.get('n') or 300), 5000))
    c.ensure_loaded()
    items = list(c.items)
    idx = next((i for i in range(len(items) - 1, -1, -1) if items[i].get('id') == before), None)
    if idx is None:
        raise ValueError('这段记录已经变了，请重新打开这个对话')
    lo = max(0, idx - n)
    return {'sid': c.sid, 'items': items[lo:idx], 'hidden': lo}


def api_send(b):
    c = get(b.get('sid') or '')
    text = (b.get('text') or '').strip()
    images = [p for p in (b.get('images') or []) if isinstance(p, str) and os.path.isfile(p)]
    if not text and not images:
        raise ValueError('消息为空')
    a = ADAPTERS[c.prov]
    if images and not a.caps(c).get('image', False):
        raise ValueError(LABEL[c.prov] + ' 这边不支持发送图片')
    NS['clear_interrupted'](c.sid)
    if c.busy or c.queue:
        c.queue.append((text, images))
        c.push_state()
        if b.get('now'):
            a.send_now(c)
        return {'ok': True, 'queued': True}
    c.claim()  # 先占住，免得连点两次开出两轮（begin_turn 里正式开始）；停止标记在这里重置
    threading.Thread(target=start_turn, args=(c, text, images), daemon=True).start()
    return {'ok': True}


def api_interrupt(b):
    c = get(b.get('sid') or '')
    drop_perms(c.sid, cancel=True)
    if c.busy:
        ADAPTERS[c.prov].interrupt(c)
    return {}


def api_send_now(b):
    c = get(b.get('sid') or '')
    if c.queue and c.busy:
        ADAPTERS[c.prov].send_now(c)
    return {}


def api_unqueue(b):
    c = get(b.get('sid') or '')
    n = len(c.queue)
    c.queue.clear()
    c.push_state()
    return {'ok': True, 'n': n}


def api_stop(b):
    """结束这个对话的运行（不删记录）：回复中先中断；还没发过消息的空对话直接丢掉。"""
    c = get(b.get('sid') or '')
    if not c.persisted and not c.items and not c.busy:
        CONVS.pop(c.sid, None)
        c.dropped = True
        emit({'type': 'sessions'})
        return {}
    if c.busy:
        ADAPTERS[c.prov].interrupt(c)
    return {}


def api_control(b):
    """模型、思考强度、权限模式：存进记录，下一轮起生效（Codex 按轮传参数；ACP 发 set_config_option），不用重启。"""
    c = get(b.get('sid') or '')
    o = c.opts()
    for k in ('model', 'effort', 'mode'):
        if k in b:
            v = b.get(k) or ''
            if v and v != 'default':
                o[k] = str(v)[:120]
            else:
                o.pop(k, None)
    c.set_meta(opts=o)
    a = ADAPTERS[c.prov]
    try:
        a.apply_opts(c)
    except Exception as e:
        log('切换选项失败', c.sid[:8], repr(e))
        raise ValueError('切换没有成功：' + str(e))
    c.push_state()
    return {'opts': o, 'pending': bool(c.busy)}


def api_delete(b):
    """只删工作台这边的记录（移到废纸篓）；各家自己保存的对话（Codex 的 thread、Antigravity 的会话）不动。"""
    c = get(b.get('sid') or '')
    if c.busy:
        try:
            ADAPTERS[c.prov].interrupt(c)
        except Exception:
            pass
    drop_perms(c.sid, cancel=True)
    try:
        ADAPTERS[c.prov].detach(c)
    except Exception:
        pass
    if c.persisted and os.path.exists(c.path):
        if not NS['trash']([c.path]):
            raise ValueError('移到废纸篓失败')
    c.dropped = True
    CONVS.pop(c.sid, None)
    G = NS['G']
    with G:
        conf = NS['load_conf']()
        conf['archived'] = [x for x in conf.get('archived', []) if x != c.sid]
        conf.get('names', {}).pop(c.sid, None)
        conf['pinned'] = [x for x in conf.get('pinned', []) if x != c.sid]
        NS['save_conf'](conf)
    emit({'type': 'sessions'})
    return {'ok': True}


def api_session_path(b):
    c = get(b.get('sid') or '')
    if not c.persisted:
        raise ValueError('这个对话还没有记录文件')
    return {'path': c.path}


def api_prov_status(b):
    out = []
    for k, a in ADAPTERS.items():
        try:
            st = a.status(force=bool(b.get('refresh')))
        except Exception as e:
            st = {'installed': False, 'ok': False, 'hint': str(e)}
        out.append(dict(st, id=k, label=LABEL[k]))
    return {'provs': out}


def api_prov_models(b):
    prov = str(b.get('prov') or '')
    a = ADAPTERS.get(prov)
    if not a:
        raise ValueError('没有这个提供方')
    c = CONVS.get(b.get('sid') or '')
    return a.models(c, refresh=bool(b.get('refresh')))


def busy_list():
    out = []
    for c in list(CONVS.values()):
        att = c.attention()
        kids = 0
        if not (c.busy or att or c.queue) and c.attached:  # 11-23：这一轮结束了、子代理还在进程里跑，重启也会把它们停掉
            try:
                kids = sum(1 for a in prov_agents().agents(c).get('agents') or [] if a.get('state') == 'run')
            except Exception as e:
                log('查子代理出错', c.sid[:8], repr(e))
        if c.busy or att or c.queue or kids:
            out.append({'sid': c.sid, 'prov': c.prov, 'busy': c.busy, 'attention': att, 'queued': len(c.queue), 'kids': kids})
    return out


def api_prov_busy(b):
    """restart_when_idle.py 用：Codex / Antigravity 对话有没有在回复、等确认、排队。"""
    lst = busy_list()
    return {'busy': bool(lst), 'convs': lst, 'procs': {k: a.proc_info() for k, a in ADAPTERS.items()}, 'wait': restart_wait_text(lst)}


def restart_wait_text(lst=None):
    """复核修复 8：“重启后台服务”要等 Codex / Antigravity 这一轮结束（它们的回复在 server 里，重启会断开）。"""
    lst = busy_list() if lst is None else lst
    labs = [LABEL[p] for p in LABEL if any(x['prov'] == p for x in lst)]
    if labs and all(x.get('kids') and not (x['busy'] or x['attention'] or x['queued']) for x in lst):
        return f'等 {" / ".join(labs)} 的子代理跑完后重启'
    return f'等 {" / ".join(labs)} 这一轮结束后重启' if labs else ''


# ---------------------------------------------------------------- 复核修复 8：左栏内容搜索也搜这两家的对话
SEARCH_CACHE = {}   # sid -> (mtime, 文本, 小写)


def search_text(path):
    """和 Claude 的索引一样：只收用户消息、回复正文、工具说明和路径（不收工具输出、思考）。"""
    parts, total = [], 0
    try:
        f = open(path, encoding='utf-8')
    except OSError:
        return ''
    with f:
        for line in f:
            if total > 3_000_000:
                break
            if not line.startswith('{"t": "new"'):
                continue
            try:
                it = json.loads(line).get('it') or {}
            except Exception:
                continue
            k = it.get('k')
            if k in ('user', 'text'):
                t = str(it.get('text') or '')
            elif k == 'tool':
                pth = it.get('path') if isinstance(it.get('path'), str) else ''
                t = ' '.join(filter(None, [str(it.get('desc') or ''), pth, str(it.get('in') or '')[:300]]))
            else:
                continue
            if t:
                parts.append(t)
                total += len(t)
    return '\n'.join(parts)


def search_convs(q, limit=40):
    ql = (q or '').lower().strip()
    if len(ql) < 2:
        return []
    names = NS['load_conf']().get('names', {})
    res = []
    for c in list(CONVS.values()):
        if c.dropped or not c.persisted:
            continue
        try:
            mt = os.path.getmtime(c.path)
        except OSError:
            continue
        d = SEARCH_CACHE.get(c.sid)
        if not d or d[0] != mt:
            txt = search_text(c.path)
            d = SEARCH_CACHE[c.sid] = (mt, txt, txt.lower())
        i = d[2].find(ql)
        if i < 0:
            continue
        a = max(0, i - 24)
        snip = ' '.join(d[1][a:i + len(ql) + 56].split())
        res.append({'sid': c.sid, 'cwd': c.cwd, 'title': names.get(c.sid) or c.title(), 'mtime': c.mtime,
                    'snip': ('…' if a else '') + snip, 'hits': d[2].count(ql), 'prov': c.prov})
    res.sort(key=lambda x: -x['mtime'])
    return res[:limit]


def api_prov_quota(b):
    out = {}
    for k in ADAPTERS:
        p = os.path.join(DATA_DIR, f'quota-{k}.json')
        try:
            out[k] = json.load(open(p, encoding='utf-8'))
        except Exception:
            out[k] = None
    return {'quota': out}


def write_json(p, doc):
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        log('写文件失败', p, repr(e))


# ---------------------------------------------------------------- 授权：复用 server 的 PERMS 和授权卡
def ask_perm(c, tool, inp, view, suggestions, on_done, extra=None):
    """在界面上弹授权卡（和 Claude 的同一套：perm / perm-done 事件，api_perm → decide）。
    决定由 server 的 decide() 写成钩子格式，等到后交给 on_done(decision, message)：
      decision：allow / always / deny；message：拒绝时的说明（“拒绝并说明”），没有为空；None 表示作废（中断、进程退出）。"""
    rid = uuid.uuid4().hex[:12]
    req = {'rid': rid, 'sid': c.sid, 'tool': tool, 'input': inp, 'suggestions': suggestions or [], 'title': c.title(),
           'cwd': c.cwd, 'prov': c.prov, 'provLabel': LABEL.get(c.prov, c.prov), 'pvView': view}
    if extra:
        req.update(extra)
    ent = {'req': req, 'ev': threading.Event(), 'res': None, 'keep': None, 'pv': True}
    NS['PERMS'][rid] = ent
    emit({'type': 'perm', 'req': req})
    c.set_spin('等你确认…')
    c.push_state(sessions=True)

    def wait():
        ent['ev'].wait(3500)
        NS['PERMS'].pop(rid, None)
        emit({'type': 'perm-done', 'rid': rid})
        res = ent['res']
        dec = None
        msg = ''
        if res:
            d = (res.get('hookSpecificOutput') or {}).get('decision') or {}
            if d.get('behavior') == 'allow':
                dec = 'always' if d.get('updatedPermissions') else 'allow'
            else:
                dec = 'deny'
                m = str(d.get('message') or '')
                if m.startswith('用户拒绝了这次操作，说明：'):
                    msg = m[len('用户拒绝了这次操作，说明：'):]
        try:
            on_done(dec, msg)
        except Exception as e:
            log('交回授权决定出错', c.sid[:8], repr(e))
        if c.busy:
            c.set_spin('处理中…')
        c.push_state(sessions=True)
    threading.Thread(target=wait, daemon=True).start()
    return rid


def drop_perms(sid, cancel=False):
    for rid, ent in list(NS['PERMS'].items()):
        if ent.get('pv') and ent['req'].get('sid') == sid:
            ent['res'] = None
            ent['ev'].set()


# ---------------------------------------------------------------- 左栏、接口接管
def merge_projects(res):
    """把 Codex / Antigravity 对话并进左栏的项目分组（同一文件夹下和 Claude 对话混排），带上提供方和运行状态。"""
    conf = NS['load_conf']()
    archived, pinned, names = set(conf.get('archived', [])), conf.get('pinned', []), conf.get('names', {})
    hidden = set(conf.get('hidden_projects', []))
    groups = {os.path.realpath(p['cwd']): p for p in res}
    touched = set()
    for c in list(CONVS.values()):
        if c.dropped or (not c.persisted and not c.items and not c.busy):
            continue  # 还没发过消息的空对话不进左栏（和 Claude 的空对话一样，只在首页里）
        cwd = NS['remap_cwd'](c.cwd) if 'remap_cwd' in NS else c.cwd
        key = os.path.realpath(cwd)
        g = groups.get(key)
        if not g:
            g = {'cwd': cwd, 'name': os.path.basename(cwd.rstrip('/')) or cwd, 'hidden': cwd in hidden, 'mtime': 0, 'sessions': []}
            groups[key] = g
            res.append(g)
        att = c.attention()
        alive = bool(c.attached or c.busy or c.queue)
        g['sessions'].append({'sid': c.sid, 'title': names.get(c.sid) or c.title(), 'mtime': c.mtime, 'archived': c.sid in archived,
                              'renamed': c.sid in names, 'pinned': pinned.index(c.sid) + 1 if c.sid in pinned else 0, 'wt': None,
                              'done_at': NS['MARKS']['done_at'].get(c.sid, 0), 'resumable': c.sid in NS['MARKS']['interrupted'],
                              'running': alive, 'busy': bool(c.busy), 'attention': att, 'prov': c.prov,
                              'provLabel': LABEL.get(c.prov, c.prov)})
        touched.add(key)
    for key in touched:
        g = groups[key]
        g['sessions'].sort(key=lambda x: -x['mtime'])
        live = [x for x in g['sessions'] if not x.get('archived')]
        g['mtime'] = (live or g['sessions'])[0]['mtime'] if g['sessions'] else 0
    res.sort(key=lambda p: -p['mtime'])
    return res


CLAUDE_ONLY = {
    # 按轮的改动来自 Claude 的 file-history：这两家没有。回 error，界面的审阅就只剩“未提交（git）”，结束行不出改动胶囊
    'turn-changes': lambda b: {'error': '这个对话没有按轮的改动记录'},
    'btw-list': lambda b: {'items': []},
    # 11-23：右栏“任务”页的子代理改读两家自己的子代理记录（prov_agents.py）；没有“后台命令”，shells 仍给空
    'agents': lambda b: prov_agents().agents(CONVS.get((b or {}).get('sid') or '')),
    'subagent': lambda b: prov_agents().items(CONVS.get((b or {}).get('sid') or ''), (b or {}).get('agent')),
    'apply-opts': lambda b: {'ok': True},
    'watch': lambda b: {},
    'screen': lambda b: {'lines': [], 'live_hist': []},
    'press': lambda b: {},
    'dialog': lambda b: {},
}
NOT_HERE = ('btw', 'add-dir', 'task-stop', 'fork', 'fork-at', 'rewind-preview', 'rewind-apply', 'turn-diff', 'context-detail', 'prewarm')


def prov_agents():
    import prov_agents as m
    if not m.NS:
        m.NS = NS
    return m


def setup(ns):
    """server.py import 之后调用一次：接管两家对话的接口，补登记新接口，起后台循环。"""
    global NS
    NS = ns  # 直接用 server 的全局字典（LOGIN_ENV、TOKEN 等在 main() 里才赋值，要现取）
    API = ns['API']
    import prov_codex
    import prov_acp
    prov_codex.NS = prov_acp.NS = ns
    ADAPTERS['codex'] = prov_codex.CodexAdapter()
    ADAPTERS['agy'] = prov_acp.AcpAdapter()

    mine = {'open': api_open, 'page': api_page, 'send': api_send, 'interrupt': api_interrupt, 'send-now': api_send_now,
            'unqueue': api_unqueue, 'stop': api_stop, 'control': api_control, 'delete': api_delete,
            'session-path': api_session_path}
    mine.update(CLAUDE_ONLY)
    for name, fn in mine.items():
        _wrap(API, name, fn)
    for name in NOT_HERE:
        _wrap(API, name, _not_here)
    orig_projects = API['projects']

    def projects(b):
        d = orig_projects(b)
        try:
            merge_projects(d.get('projects') or [])
        except Exception as e:
            log('合并左栏出错', repr(e))
        return d
    API['projects'] = projects
    orig_search = API.get('search')

    def search(b):  # 复核修复 8：内容搜索把这两家的对话也算上，和 Claude 的结果按时间混排
        d = orig_search(b) if orig_search else {'results': [], 'ready': True}
        try:
            more = search_convs((b or {}).get('q') or '')
            if more:
                d = dict(d, results=sorted((d.get('results') or []) + more, key=lambda x: -(x.get('mtime') or 0))[:40])
        except Exception as e:
            log('搜索 Codex / Antigravity 对话出错', repr(e))
        return d
    API['search'] = search
    ORIG['restart-service'] = API.get('restart-service')

    def restart_service(b):  # 复核修复 8：这两家正在回复时，重启要等这一轮结束，界面上要说清楚
        lst = busy_list()
        o = ORIG.get('restart-service')
        d = o(b) if o else {'ok': True}
        w = restart_wait_text(lst)
        if w:
            d = dict(d, wait=w)
            if not (b or {}).get('pvAware'):  # 不是 ux-v.js 发的（设置页自己会弹“正在重启”）：稍后再推一条提示盖过去
                threading.Timer(1.2, lambda: emit({'type': 'toast', 'text': w})).start()
        return d
    API['restart-service'] = restart_service
    API.update({'prov-new': api_prov_new, 'prov-status': api_prov_status, 'prov-models': api_prov_models,
                'prov-busy': api_prov_busy, 'prov-quota': api_prov_quota})
    # 启动顺序：main() 先 load_marks() 再 adopt_all()。中断标记要在读出 MARKS 之后补，所以包在 load_marks 后面
    orig_lm = ns['load_marks']

    def load_marks():
        orig_lm()
        try:
            scan()
        except Exception as e:
            log('读取 Codex / Antigravity 记录失败', repr(e))
        for a in ADAPTERS.values():
            try:
                a.reap_orphans()
            except Exception as e:
                log('清理上次留下的进程失败', repr(e))
    ns['load_marks'] = load_marks
    threading.Thread(target=loop, daemon=True).start()


def _not_here(b):
    c = CONVS.get(b.get('sid') or '')
    raise ValueError(f'{LABEL.get(c.prov, "这个") if c else "这个"} 对话不支持这个功能')


def _wrap(API, name, fn):
    orig = API.get(name)

    def w(b):
        sid = (b or {}).get('sid') or ''
        if isinstance(sid, str) and sid in CONVS:
            return fn(b)
        if name == 'prewarm':  # 首页选了 Codex / Antigravity 时界面会带 prov，不预热 Claude
            if (b or {}).get('prov') in LABEL:
                return {}
        return orig(b) if orig else {}
    API[name] = w


def loop():
    """后台：每 0.15 秒把有变化的运行状态推给界面（流式正文、转圈文字）；每 5 秒检查空闲回收；
    有对话在忙时刷新 BUS.last_seen，免得 server 按“30 分钟没有窗口连接”退出时把它们断掉。"""
    k = 0
    while True:
        time.sleep(0.15)
        k += 1
        for c in list(CONVS.values()):
            if c.dirty:
                try:
                    c.push_state()
                except Exception:
                    pass
        for a in ADAPTERS.values():
            try:
                a.tick()
            except Exception as e:
                log('后台循环出错', repr(e))
        if k % 33 == 0:
            if busy_list():
                try:
                    NS['BUS'].last_seen = time.time()
                except Exception:
                    pass
            for a in ADAPTERS.values():
                try:
                    a.maybe_reap(IDLE_REAP)
                except Exception as e:
                    log('回收进程出错', repr(e))


# ---------------------------------------------------------------- 协议进程（两家共用）：一行一个 JSON-RPC 消息
class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Rpc:
    """stdio JSON-RPC 2.0 客户端：一行一条消息。通知在读线程里按顺序处理（处理函数不能阻塞）；
    对方发来的请求交给 on_request（另起线程），用 respond() 回复。"""

    def __init__(self, name, argv, env, cwd, on_notify, on_request, on_exit, logpath):
        self.name, self.on_notify, self.on_request, self.on_exit = name, on_notify, on_request, on_exit
        self.wlock, self.plock = threading.Lock(), threading.Lock()
        self.pending, self.seq, self.closed = {}, 0, False
        os.makedirs(os.path.dirname(logpath), exist_ok=True)
        try:
            if os.path.getsize(logpath) > 5 << 20:
                os.replace(logpath, logpath + '.1')
        except OSError:
            pass
        self.errf = open(logpath, 'ab')
        self.errf.write(('\n==== %s 启动 %s\n' % (time.strftime('%m-%d %H:%M:%S'), ' '.join(argv))).encode())
        self.errf.flush()
        # 单独一个进程组（组号＝它的 PID）：它退出后用 SIGHUP 收掉组里剩下的子进程（MCP 服务、后台 git fetch 等），
        # 和关掉终端时 Claude 那边的清理规则一样；nohup、另起会话的常驻进程不受影响
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errf, cwd=cwd, env=env,
                                  start_new_session=True)
        self.pid = self.p.pid
        self.started = time.time()
        threading.Thread(target=self._read, daemon=True).start()

    def alive(self):
        return not self.closed and self.p.poll() is None

    def _read(self):
        try:
            for line in self.p.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if not isinstance(m, dict):
                    continue
                if 'method' in m and 'id' in m:
                    threading.Thread(target=self._req, args=(m,), daemon=True).start()
                elif 'method' in m:
                    try:
                        self.on_notify(m['method'], m.get('params') or {})
                    except Exception as e:
                        log(self.name, '处理通知出错', m.get('method'), repr(e))
                elif 'id' in m:
                    with self.plock:
                        w = self.pending.pop(m['id'], None)
                    if w:
                        w['msg'] = m
                        w['ev'].set()
        except Exception as e:
            log(self.name, '读取进程输出出错', repr(e))
        self.closed = True
        try:
            code = self.p.wait(timeout=10)
        except Exception:
            code = None
        self.hup_group()
        with self.plock:
            ws, self.pending = list(self.pending.values()), {}
        for w in ws:
            w['msg'] = {'error': {'code': -32099, 'message': '进程已退出'}}
            w['ev'].set()
        try:
            self.on_exit(code)
        except Exception as e:
            log(self.name, '处理进程退出出错', repr(e))

    def _req(self, m):
        try:
            self.on_request(m['id'], m['method'], m.get('params') or {})
        except Exception as e:
            log(self.name, '处理请求出错', m.get('method'), repr(e))
            self.respond(m['id'], error={'code': -32603, 'message': str(e)[:200]})

    def _write(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
        with self.wlock:
            if self.closed:
                raise RpcError(-32099, '进程已退出')
            try:
                self.p.stdin.write(data)
                self.p.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.closed = True
                raise RpcError(-32099, '进程已退出')

    def request(self, method, params, timeout=60):
        with self.plock:
            self.seq += 1
            rid = self.seq
            w = {'ev': threading.Event(), 'msg': None}
            self.pending[rid] = w
        try:
            self._write({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
        except RpcError:
            with self.plock:
                self.pending.pop(rid, None)
            raise
        if not w['ev'].wait(timeout):
            with self.plock:
                self.pending.pop(rid, None)
            raise RpcError(-32098, f'{method} 超时')
        m = w['msg'] or {}
        if 'error' in m:
            e = m['error'] or {}
            raise RpcError(e.get('code'), str(e.get('message') or '出错'), e.get('data'))
        return m.get('result')

    def notify(self, method, params=None):
        msg = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            msg['params'] = params
        self._write(msg)

    def respond(self, rid, result=None, error=None):
        msg = {'jsonrpc': '2.0', 'id': rid}
        if error is not None:
            msg['error'] = error
        else:
            msg['result'] = result
        try:
            self._write(msg)
        except RpcError:
            pass

    def hup_group(self):
        try:
            os.killpg(self.pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def close(self, grace=5.0):
        """关 stdin（两家都会在读到 EOF 后退出，实测），等一会儿还在就 SIGTERM，再不行 SIGKILL。"""
        try:
            with self.wlock:
                self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=grace)
        except Exception:
            try:
                self.p.terminate()
                self.p.wait(timeout=3)
            except Exception:
                try:
                    self.p.kill()
                except Exception:
                    pass
        self.hup_group()


# ---------------------------------------------------------------- 适配器公用
class Adapter:
    """两家适配器的公用部分：进程（一家一个）、PID 记录、空闲回收、环境变量。"""
    id = ''
    label = ''

    def __init__(self, core):
        self.core = core
        self.rpc = None
        self.lock = threading.RLock()
        self.last_active = time.time()
        self.st_cache = None

    # 子类实现：argv() start_proc() ensure_attached(c) send(c, text, images) interrupt(c) send_now(c) apply_opts(c)
    #           detach(c) models(c, refresh) status(force) caps(c) cur_model(c) turn_model(c) turn_effort(c) flush(c) tick()

    def env(self):
        env = dict(NS.get('LOGIN_ENV') or os.environ)
        if not env.get('PATH'):
            env['PATH'] = os.environ.get('PATH', '/usr/bin:/bin')
        return env

    def pidfile(self):
        return os.path.join(NS['RUN_DIR'], f'prov-{self.id}-{NS["PORT"]}.json')

    def note_pid(self, rpc, argv):
        try:
            with open(self.pidfile(), 'w') as f:
                json.dump({'pid': rpc.pid, 'argv0': argv[0], 'started': rpc.started}, f)
        except OSError:
            pass

    def reap_orphans(self):
        """上一个 server 留下的协议进程组（正常情况下它读到 EOF 就退出了，但它起的后台子进程可能还在）：
        按记录的组号找，只收已经成了孤儿（父进程是 1）的成员；组长还在时要求命令行对得上；记录超过 3 天不动。不按名字杀。"""
        try:
            d = json.load(open(self.pidfile()))
        except Exception:
            return
        pid = int(d.get('pid') or 0)
        if not pid or (self.rpc and self.rpc.pid == pid) or time.time() - float(d.get('started') or 0) > 3 * 86400:
            return
        try:
            out = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid=,command='], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return
        base = os.path.basename(str(d.get('argv0') or '#'))
        for line in out.splitlines():
            f = line.split(None, 3)
            if len(f) < 4 or int(f[2]) != pid or f[1] != '1':
                continue
            if int(f[0]) == pid and base not in f[3]:
                return  # 组号被别的程序重用了
            try:
                os.kill(int(f[0]), signal.SIGHUP)
                log('结束上次留下的进程', self.label, f[0], f[3][:80])
            except OSError:
                pass

    def proc_info(self):
        r = self.rpc
        return {'pid': r.pid, 'alive': r.alive(), 'started': r.started} if r else None

    def convs(self):
        return [c for c in list(CONVS.values()) if c.prov == self.id]

    def touch(self):
        self.last_active = time.time()

    def maybe_reap(self, idle):
        r = self.rpc
        if not r or not r.alive():
            return
        cs = self.convs()
        if any(c.busy or c.queue or c.attention() for c in cs):
            self.touch()
            return
        if time.time() - self.last_active < idle:
            return
        if self.kids_running(cs):  # 11-23：父对话空闲了，子代理还在这个进程里跑（agy 子代理、Codex 子线程），回收会把它们停掉
            self.touch()
            return
        log('空闲回收', self.label, '进程', r.pid)
        self.stop_proc('idle')

    def kids_running(self, cs):
        try:
            m = prov_agents()
            return any(a.get('state') == 'run' for c in cs if c.attached for a in m.agents(c).get('agents') or [])
        except Exception as e:
            log('查子代理出错', self.label, repr(e))
            return False

    def stop_proc(self, why):
        with self.lock:
            r, self.rpc = self.rpc, None
        if not r:
            return
        r.reaped = why
        for c in self.convs():
            c.attached = False
            c.dirty = True
        threading.Thread(target=r.close, daemon=True).start()

    def on_proc_exit(self, rpc, code):
        """协议进程退出：意外退出时提示；回收或主动关掉的不提示。
        复核修复 4：不管哪种退出，在这个进程上进行中的轮一律结束（记“上次回复被中断”），不会一直卡在“忙碌”。
        只看这一轮记下的进程（start_turn 里的 tr['rpc']）：回收后新起的进程上的轮不受影响。"""
        why = getattr(rpc, 'reaped', None)
        cur = False
        with self.lock:
            if self.rpc is rpc:
                self.rpc, cur = None, True
        log(self.label, f'进程已结束（{why}）' if why else '进程意外退出', rpc.pid, '退出码', code)
        for c in self.convs():
            tr = c.turn
            mine = bool(c.busy and tr and tr.get('rpc') is rpc)
            if cur:
                c.attached = False
            if mine or (cur and not why):
                drop_perms(c.sid, cancel=True)
            if mine:
                txt = (f'{self.label} 进程意外退出（退出码 {code}），这一轮没有完成。可以发“继续”接着做。' if not why
                       else f'{self.label} 进程已关闭，这一轮没有完成。可以发“继续”接着做。')
                c.add({'k': 'error', 'text': txt, 'etype': '进程退出'})
                c.end_turn('error', error_type='process_exit', error_msg=f'exit {code}' + (f' ({why})' if why else ''))
                NS['mark_interrupted'](c.sid, 'exit')
            if mine or cur:
                c.push_state(sessions=True)

    def tick(self):
        pass


def b64file(p):
    with open(p, 'rb') as f:
        return base64.b64encode(f.read()).decode()


def mime_of(p):
    ext = os.path.splitext(p)[1].lower()
    return {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
            '.bmp': 'image/bmp'}.get(ext, 'image/png')


def which(name):
    env = NS.get('LOGIN_ENV') or {}
    return shutil.which(name, path=env.get('PATH')) or shutil.which(name) or ''


def diff_view(diff):
    """统一格式的 diff（或新文件全文）→ 界面差异框要的 old / new 两段文字，和增删行数。"""
    old, new, add, rm = [], [], 0, 0
    lines = (diff or '').splitlines()
    if not any(l.startswith(('@@', '--- ', '+++ ')) for l in lines[:6]):
        return None, None, [len(lines), 0]
    for l in lines:
        if l.startswith(('--- ', '+++ ', 'diff ', 'index ')):
            continue
        if l.startswith('@@'):
            if old or new:
                old.append('…')
                new.append('…')
            continue
        if l.startswith('+'):
            new.append(l[1:])
            add += 1
        elif l.startswith('-'):
            old.append(l[1:])
            rm += 1
        else:
            s = l[1:] if l.startswith(' ') else l
            old.append(s)
            new.append(s)
    return '\n'.join(old)[:3000], '\n'.join(new)[:3000], [add, rm]


def text_lines(old, new):
    """ACP 的 diff（整段旧文、新文）算增删行数：首尾相同的行去掉后按行数估。"""
    a, b = (old or '').splitlines(), (new or '').splitlines()
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    j = 0
    while j < len(a) - i and j < len(b) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    return [len(b) - i - j, len(a) - i - j]
