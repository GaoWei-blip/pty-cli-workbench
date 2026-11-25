"""第十六轮 P：Codex 适配器（`codex app-server`，stdio JSON-RPC）。

协议以 `codex app-server generate-json-schema` 导出的 schema 为准（本机 codex-cli 0.171.0），用到的：
- 请求：initialize（+ initialized 通知）、thread/start、thread/resume、thread/unsubscribe、turn/start、turn/steer、
  turn/interrupt、model/list、config/read、account/read、account/rateLimits/read
- 通知：turn/started、turn/completed、item/started、item/completed、item/agentMessage/delta、item/reasoning/summaryTextDelta、
  item/reasoning/summaryPartAdded、item/commandExecution/outputDelta、turn/plan/updated、thread/tokenUsage/updated、error、
  thread/name/updated、account/rateLimits/updated、serverRequest/resolved
- 对方发来的请求：item/commandExecution/requestApproval、item/fileChange/requestApproval（其余回“不支持”）
用户自己的登录和配置原样使用（读 ~/.codex/config.toml 和 auth.json，工作台不改它们；本机是 ChatGPT 登录直连，没有走 cc-switch）；
只有测试时用环境变量 CWB_CODEX_HOME / CWB_CODEX_ARGS / CWB_CODEX_CMD 换成临时目录和假后端。

【红线】额度只读：只用 account/rateLimits/read 和 account/rateLimits/updated 通知。
协议里那个“消耗重置券”的方法（account/rateLimitResetCredit 下的 consume）一调就会花掉用户一张额度重置券，
任何代码、测试、冒烟都绝对不许调用（tests/test_r16_P.py 有一条断言检查这几个文件里没有这个调用）。
"""
import json
import os
import re
import subprocess
import threading
import time

import providers as core

NS = {}

# 权限模式：Codex 自己的档位（approvalPolicy × sandbox），配一句白话。default＝不传，沿用 config.toml
MODES = [
    ('default', '按 Codex 设置', '沿用 ~/.codex/config.toml 里的权限设置'),
    ('read-only', '只读', '能看文件；改文件、运行会改东西的命令前先问你'),
    ('auto', '自动', '在这个文件夹里改文件、运行命令不问；要联网或改外面的文件时再问'),
    ('untrusted', '每步都问', '只有确定安全的只读命令直接运行，其他都先问你'),
    ('full', '完全放开', '所有操作都不问，也不限制访问范围，风险最高'),
]
POLICY = {
    'read-only': ('on-request', 'read-only', {'type': 'readOnly'}),
    'auto': ('on-request', 'workspace-write', {'type': 'workspaceWrite'}),
    'untrusted': ('untrusted', 'workspace-write', {'type': 'workspaceWrite'}),
    'full': ('never', 'danger-full-access', {'type': 'dangerFullAccess'}),
}
EFF_ZH = {'none': '不思考', 'minimal': '最少', 'low': '低', 'medium': '中', 'high': '高', 'xhigh': '超高', 'max': '最高', 'ultra': '极高'}
ERR_ZH = {'rateLimitExceeded': '接口限流', 'usageLimitExceeded': '额度不足', 'sessionBudgetExceeded': '额度不足',
          'unauthorized': '鉴权失败', 'serverOverloaded': '网关错误', 'internalServerError': '网关错误',
          'modelNotFound': '请求被拒绝', 'badRequest': '请求被拒绝', 'contextWindowExceeded': '上下文超长',
          'sandboxError': '沙箱出错', 'tooManyDenials': '拒绝次数过多'}
SHELL_RE = re.compile(r"^(?:/bin/|/usr/bin/)?(?:zsh|bash|sh) -l?c (['\"])(.*)\1$", re.S)


def unwrap_cmd(cmd):
    """/bin/zsh -lc 'touch a' → touch a（Codex 把命令包在登录 shell 里执行）。"""
    m = SHELL_RE.match((cmd or '').strip())
    if not m:
        return cmd or ''
    s = m.group(2)
    return s.replace("'\\''", "'") if m.group(1) == "'" else s


def err_kind(info):
    """codexErrorInfo → (原生代码, 中文类型)。"""
    if isinstance(info, str):
        return info, ERR_ZH.get(info, '接口出错')
    if isinstance(info, dict) and info:
        k = next(iter(info))
        code = (info.get(k) or {}).get('httpStatusCode') if isinstance(info.get(k), dict) else None
        lbl = NS['err_label']('', str(code or '')) if code else '连接出错'
        return k, lbl
    return 'other', '接口出错'


class CodexAdapter(core.Adapter):
    id, label = 'codex', 'Codex'

    def __init__(self, core_mod=None):
        core.Adapter.__init__(self, core)
        self.threads = {}       # threadId -> sid
        self.nat = {}           # (对话 ID, 原生条目 ID) -> (sid, [时间线条目 ID], 附加信息)；各对话的条目 ID 可能重复，要带上对话
        self.outbuf = {}        # (对话 ID, 时间线条目 ID) -> [sid, 输出, 有变化]；条目 ID 各对话都从 p1 数起，单用会串台
        self.models_cache = None
        self.cfg_model = ''
        self.cfg_provider = ''
        self.quota_raw = None
        self.req_map = {}       # 对方请求 ID -> 授权卡 rid
        self.ver = None
        self.quota = None
        self.quota_err = ''
        self.cfg_read = False

    # ---------------------------------------------------------------- 进程
    def argv(self):
        if os.environ.get('CWB_CODEX_CMD'):
            cmd = json.loads(os.environ['CWB_CODEX_CMD'])
        else:
            cmd = [self.core.which('codex') or 'codex', 'app-server']
        return cmd + json.loads(os.environ.get('CWB_CODEX_ARGS') or '[]')

    def env(self):
        e = core.Adapter.env(self)
        if os.environ.get('CWB_CODEX_HOME'):
            e['CODEX_HOME'] = os.environ['CWB_CODEX_HOME']
        return e

    def start_proc(self):
        with self.lock:
            if self.rpc and self.rpc.alive():
                return self.rpc
            argv = self.argv()
            holder = {}
            try:
                rpc = self.core.Rpc('codex', argv, self.env(), NS['HOME'], self.on_notify, self.on_request,
                                    lambda code: self.on_proc_exit(holder['r'], code),
                                    os.path.join(NS['RUN_DIR'], f'prov-codex-{NS["PORT"]}.log'))
            except OSError as e:
                raise ValueError('启动 Codex 失败：' + str(e))
            holder['r'] = rpc
            try:
                rpc.request('initialize', {'clientInfo': {'name': 'claude_workbench', 'title': 'Claude 工作台', 'version': '1.0'}}, 30)
                rpc.notify('initialized')
            except Exception as e:
                rpc.reaped = 'init-failed'
                rpc.close(1)
                raise ValueError('Codex 启动失败：' + str(e))
            self.rpc = rpc
            self.threads = {}
            self.note_pid(rpc, argv)
            self.touch()
            self.core.log('Codex 进程已启动', rpc.pid)
            threading.Thread(target=self.after_start, daemon=True).start()
            return rpc

    def after_start(self):
        """进程起来后：读一次配置（默认模型、提供方），再读一次额度。"""
        r = self.rpc
        if not r:
            return
        try:
            cfg = (r.request('config/read', {}, 15) or {}).get('config') or {}
            self.cfg_model = str(cfg.get('model') or '')
            self.cfg_provider = str(cfg.get('model_provider') or '')
        except Exception:
            pass
        self.read_quota()

    def ensure_attached(self, c):
        rpc = self.start_proc()
        tid = c.meta.get('native') or ''
        if c.attached and tid and self.threads.get(tid) == c.sid:
            return
        o = c.opts()
        params = {'cwd': c.cwd}
        pol = POLICY.get(o.get('mode') or '')
        if pol:
            params['approvalPolicy'], params['sandbox'] = pol[0], pol[1]
        if o.get('model'):
            params['model'] = o['model']
        hk = self.core.HOOKS.get('codex_thread')  # 第十七轮 M：协作开着时挂 cwb（config 的 mcp_servers.cwb）和编排说明（developerInstructions）
        if hk:
            params = hk(c, params) or params
        try:
            if tid:
                res = rpc.request('thread/resume', dict(params, threadId=tid, excludeTurns=True), 90)
            else:
                res = rpc.request('thread/start', params, 90)
        except self.core.RpcError as e:
            if tid:
                raise ValueError(f'Codex 续接不上这个对话（{e.message}）。原来的记录可能已被 Codex 删除，可以新开一个对话。')
            raise ValueError('Codex 新建对话失败：' + e.message)
        th = (res or {}).get('thread') or {}
        tid = th.get('id') or tid
        if not tid:
            raise ValueError('Codex 没有返回对话 ID')
        c.set_meta(native=tid)
        self.threads[tid] = c.sid
        c.attached = True
        c.pv = getattr(c, 'pv', {})
        c.pv.update(model=res.get('model') or '', effort=res.get('reasoningEffort') or '', cfgcwd=res.get('cwd') or '')
        if th.get('name') and not c.meta.get('title'):
            c.set_meta(title=th['name'])
        self.touch()

    def detach(self, c):
        tid = c.meta.get('native') or ''
        r = self.rpc
        if r and r.alive() and tid and self.threads.get(tid) == c.sid:
            try:
                r.request('thread/unsubscribe', {'threadId': tid}, 10)
            except Exception:
                pass
        self.threads.pop(tid, None)
        c.attached = False

    # ---------------------------------------------------------------- 一轮
    def send(self, c, text, images):
        rpc = self.start_proc()
        inp = [{'type': 'text', 'text': text}] if text else []
        inp += [{'type': 'localImage', 'path': p} for p in images]
        params = {'threadId': c.meta['native'], 'input': inp}
        o = c.opts()
        if o.get('model'):
            params['model'] = o['model']
        if o.get('effort'):
            params['effort'] = o['effort']
        pol = POLICY.get(o.get('mode') or '')
        if pol:
            params['approvalPolicy'], params['sandboxPolicy'] = pol[0], pol[2]
        c.pv = getattr(c, 'pv', {})
        # 复核修复 1：停止、立即发送的标记（user_int、int_req、steer_req）在占住这一轮时（PConv.claim）就重置了，
        # 这里不能再清，否则“正在连接”时点的停止会被冲掉
        c.pv.update(msg=None, reason=None, plan_item=None, turn='')
        if c.stop_req:  # 连接期间已经点了停止：不发出，这一轮按“已中断”结束
            c.end_turn('interrupted', why='user')
            return
        res = rpc.request('turn/start', params, 60)
        tu = (res or {}).get('turn') or {}
        if c.turn:
            c.turn['native'] = tu.get('id') or ''
        c.pv['turn'] = tu.get('id') or c.pv.get('turn') or ''
        if c.pv.get('int_req') and c.pv['turn']:
            c.pv['int_req'] = False
            self.interrupt(c)
        elif c.pv.get('steer_req') and c.pv['turn']:
            self.send_now(c)
        self.touch()

    def interrupt(self, c):
        c.pv = getattr(c, 'pv', {})
        c.pv['user_int'] = True
        c.stop_req = True  # 复核修复 1：还在连接（thread/start、resume）时，start_turn / send 看到它就不发出
        tid, turn = c.meta.get('native') or '', c.pv.get('turn') or ''
        if not turn:
            c.pv['int_req'] = True  # turn/start 还没返回：拿到轮 ID 后再中断
            c.set_spin('正在停止…')
            return
        r = self.rpc
        if not r or not r.alive():
            c.end_turn('interrupted', why='user')
            return
        c.set_spin('正在停止…')
        seq = c.tseq
        try:
            r.request('turn/interrupt', {'threadId': tid, 'turnId': turn}, 15)
        except Exception as e:
            self.core.log('Codex 中断失败', c.sid[:8], repr(e))

        def watchdog():  # 复核修复 4：20 秒内没收到 turn/completed，就在工作台这边结束，免得一直卡在“忙碌”
            time.sleep(self.core.STOP_GRACE)
            if c.busy and c.tseq == seq:
                self.core.log('Codex 中断后没有结束这一轮，工作台这边结束', c.sid[:8])
                c.pv.setdefault('dead', set()).add(turn)  # 之后迟到的 turn/completed 不再理会
                c.end_turn('interrupted', why='user', stop='timeout')
        threading.Thread(target=watchdog, daemon=True).start()

    def send_now(self, c):
        """排队的消息立即发送：用 turn/steer 插进当前这一轮（界面上标“已插入当前回复”）。"""
        c.pv = getattr(c, 'pv', {})
        r, turn = self.rpc, c.pv.get('turn')
        if not c.busy:
            return
        if not turn or not (r and r.alive()):
            c.pv['steer_req'] = True  # 还在连接或 turn/start 还没返回：拿到轮 ID 后再插（复核修复 1：标记不再被 send 清掉）
            return
        c.pv['steer_req'] = False
        while c.queue:
            text, images = c.queue.pop(0)
            inp = [{'type': 'text', 'text': text}] if text else []
            inp += [{'type': 'localImage', 'path': p} for p in images]
            try:
                r.request('turn/steer', {'threadId': c.meta['native'], 'expectedTurnId': turn, 'input': inp}, 20)
            except Exception as e:
                c.queue.insert(0, (text, images))
                self.core.log('插入当前回复失败，留在队列里', c.sid[:8], repr(e))
                break
            it = {'k': 'user', 'text': text, 'stat': None, 'mid': True}
            if images:
                it['imgs'], it['pimgs'] = len(images), list(images)[:6]
            c.add(it)
        c.push_state()

    def steer_text(self, c, text):
        c.queue.insert(0, (text, []))
        self.send_now(c)

    def apply_opts(self, c):
        """模型、思考强度、权限模式都按轮传给 turn/start，下一轮起生效，这里不用做什么。"""
        return

    def flush(self, c):
        pv = getattr(c, 'pv', {}) or {}
        m = pv.get('msg')
        if m and m.get('text', '').strip():
            c.add({'k': 'text', 'text': m['text']})
        pv['msg'] = None
        rs = pv.get('reason')
        if rs and rs.get('text', '').strip():
            c.add({'k': 'thought', 'secs': max(1, round(time.time() - rs['t0'])), 'text': rs['text'][:20000]})
        pv['reason'] = None
        for k, v in list(self.outbuf.items()):
            if v[0] == c.sid:
                self.outbuf.pop(k, None)

    # ---------------------------------------------------------------- 选项与状态
    def cur_model(self, c):
        o = c.opts() if c else {}
        return o.get('model') or ((getattr(c, 'pv', {}) or {}).get('model') if c else '') or self.cfg_model or ''

    def turn_model(self, c):
        return self.cur_model(c)

    def turn_effort(self, c):
        o = c.opts()
        return o.get('effort') or (getattr(c, 'pv', {}) or {}).get('effort') or ''

    def live_opts(self, c):
        """正在用的模型（没选时按 config.toml），界面“默认”项后面显示它。"""
        pv = getattr(c, 'pv', {}) or {}
        return {'model': pv.get('model') or self.cfg_model or ''}

    def caps(self, c):
        return {'image': True, 'steer': True, 'denyMsg': True}

    def models(self, c=None, refresh=False):
        out = {'models': [], 'modes': [{'id': k, 'label': l, 'desc': d} for k, l, d in MODES], 'defaultMode': 'default',
               'effortNames': EFF_ZH, 'defaultLabel': '', 'error': ''}
        mc = self.models_cache
        if refresh or not mc or time.time() - mc[0] > 600:
            try:
                rpc = self.start_proc()
                data, cur = [], None
                for _ in range(5):
                    res = rpc.request('model/list', dict({'cursor': cur} if cur else {}), 30) or {}
                    data += res.get('data') or []
                    cur = res.get('nextCursor')
                    if not cur:
                        break
                try:
                    cfg = (rpc.request('config/read', {}, 15) or {}).get('config') or {}
                    self.cfg_model = str(cfg.get('model') or '')
                    self.cfg_provider = str(cfg.get('model_provider') or '')
                    out['cfgEffort'] = str(cfg.get('model_reasoning_effort') or '')
                except Exception:
                    pass
                lst = []
                for m in data:
                    if m.get('hidden'):
                        continue
                    lst.append({'id': m.get('model') or m.get('id'), 'label': m.get('displayName') or m.get('model') or m.get('id'),
                                'desc': m.get('description') or '', 'isDefault': bool(m.get('isDefault')),
                                'efforts': [e.get('reasoningEffort') for e in (m.get('supportedReasoningEfforts') or []) if e.get('reasoningEffort')],
                                'defaultEffort': m.get('defaultReasoningEffort') or '', 'image': 'image' in (m.get('inputModalities') or ['text', 'image'])})
                mc = self.models_cache = (time.time(), lst, out.get('cfgEffort', ''))
            except Exception as e:
                out['error'] = 'Codex 的模型列表读不出来：' + str(e)
                mc = mc or (0, [], '')
        out['models'] = mc[1]
        out['cfgEffort'] = mc[2]
        out['defaultLabel'] = self.cfg_model or next((m['label'] for m in mc[1] if m.get('isDefault')), '')
        out['cfgModel'] = self.cfg_model
        return out

    def version(self):
        if self.ver is None:
            try:
                p = subprocess.run(self.argv()[:1] + ['--version'], capture_output=True, text=True, timeout=15, env=self.env())
                self.ver = (p.stdout or '').strip().split()[-1] if p.returncode == 0 and p.stdout.strip() else ''
            except Exception:
                self.ver = ''
        return self.ver

    def status(self, force=False):
        sc = self.st_cache
        if sc and not force and time.time() - sc[0] < 60:
            return sc[1]
        exe = self.argv()[0]
        if not (os.path.isabs(exe) and os.path.exists(exe)) and not self.core.which(exe):
            st = {'installed': False, 'ok': False, 'hint': '没有找到 codex 命令。先在终端安装（npm i -g @openai/codex），再运行一次 codex 登录。'}
        else:
            st = {'installed': True, 'ok': True, 'version': self.version(), 'hint': ''}
            r = self.rpc
            if r and r.alive():  # 进程已经在跑：问它（account/read）
                try:
                    acc = r.request('account/read', {}, 20) or {}
                    st['login'] = (acc.get('account') or {}).get('type') or ('不需要' if not acc.get('requiresOpenaiAuth') else '')
                    if acc.get('requiresOpenaiAuth') and not acc.get('account'):
                        st.update(ok=False, hint='Codex 还没登录：在终端运行 codex，按提示登录后再回来。')
                except Exception as e:
                    st.update(ok=False, hint='Codex 现在用不了：' + str(e))
            elif not self.logged_in_files():
                # 没在跑时不为了查状态去起进程（它会连带起 config 里的 MCP 服务）：看登录文件、API key、自定义提供方
                st.update(ok=False, hint='Codex 还没登录：在终端运行 codex，按提示登录后再回来。')
        self.st_cache = (time.time(), st)
        return st

    def logged_in_files(self):
        home = self.env().get('CODEX_HOME') or os.path.join(NS['HOME'], '.codex')
        if os.path.exists(os.path.join(home, 'auth.json')) or self.env().get('OPENAI_API_KEY'):
            return True
        args = ' '.join(self.argv())
        if 'model_provider=' in args:
            return True
        try:
            return bool(re.search(r'(?m)^\s*model_provider\s*=', open(os.path.join(home, 'config.toml'), encoding='utf-8').read()))
        except OSError:
            return False

    def read_quota(self):
        """额度快照：account/rateLimits/read（只读；需要 ChatGPT 登录，API key 或没登录时回 -32600）。
        写 data/quota-codex.json，结构按调研文档第 7 节：{v, provider, ts, src, plan, windows[], credits, resetCredits, unavailable, lastGood}。"""
        r = self.rpc
        if not r:
            return
        try:
            res = r.request('account/rateLimits/read', {}, 20) or {}
        except Exception as e:
            msg = getattr(e, 'message', str(e))
            self.quota_err = msg
            reason = 'notLoggedIn' if getattr(e, 'code', None) == -32600 or 'authentication' in msg.lower() else 'probeFailed'
            self.save_quota_doc({'v': 1, 'provider': 'codex', 'ts': self.core.ms(time.time()), 'src': 'protocol', 'windows': [],
                                 'unavailable': {'reason': reason, 'text': msg[:300]}})
            self.core.log('Codex 额度读不出来', msg)
            return
        self.quota_raw = res
        self.quota_err = ''
        self.save_quota_doc(self.quota_doc(res))

    def quota_doc(self, res):
        rl = res.get('rateLimits') or {}
        windows = []

        def add(snap, group=None):
            for key in ('primary', 'secondary'):
                w = (snap or {}).get(key)
                if not isinstance(w, dict) or w.get('usedPercent') is None:
                    continue
                mins = w.get('windowDurationMins')
                kind = '5h' if mins == 300 else 'week' if mins == 10080 else 'month' if mins and 40000 <= mins <= 45000 else 'other'
                win = {'id': (group + ':' if group else '') + key, 'label': {'5h': '5 小时', 'week': '每周', 'month': '每月'}.get(kind) or
                       (f'{mins} 分钟' if mins else key), 'kind': 'model' if group else kind, 'usedPercent': w.get('usedPercent')}
                if group:
                    win['group'] = group
                if mins:
                    win['windowMins'] = mins
                if w.get('resetsAt'):
                    win['resetsAt'] = int(w['resetsAt']) * 1000  # 协议给的是秒，这里统一成毫秒
                windows.append(win)
        add(rl)
        main_id = rl.get('limitId') or 'codex'
        for lid, snap in (res.get('rateLimitsByLimitId') or {}).items():
            if lid != main_id and isinstance(snap, dict):
                add(snap, snap.get('limitName') or lid)
        doc = {'v': 1, 'provider': 'codex', 'ts': self.core.ms(time.time()), 'src': 'protocol', 'windows': windows}
        if rl.get('planType'):
            doc['plan'] = rl['planType']
        if isinstance(rl.get('credits'), dict):
            doc['credits'] = rl['credits']
        if isinstance(res.get('rateLimitResetCredits'), dict):
            doc['resetCredits'] = res['rateLimitResetCredits']  # 只记张数，绝不兑换
        if self.cfg_provider and self.cfg_provider != 'openai':
            doc['viaProxy'] = True  # 请求转给了别的提供方：这是 ChatGPT 账号的额度，和实际用量可能不符
        return doc

    def save_quota_doc(self, doc):
        """保留最近一次拿到窗口数据的那份（lastGood），读取失败时界面还能显示上次的值。"""
        p = os.path.join(self.core.DATA_DIR, 'quota-codex.json')
        try:
            old = json.load(open(p, encoding='utf-8'))
        except Exception:
            old = {}
        if doc.get('windows'):
            doc['lastGood'] = {'ts': doc['ts'], 'windows': doc['windows']}
        elif old.get('lastGood') or old.get('windows'):
            doc['lastGood'] = old.get('lastGood') or {'ts': old.get('ts'), 'windows': old.get('windows')}
        self.quota = doc
        self.core.write_json(p, doc)

    # ---------------------------------------------------------------- 通知
    def conv_of(self, p):
        sid = self.threads.get(p.get('threadId') or '')
        return self.core.CONVS.get(sid) if sid else None

    def on_notify(self, method, p):
        self.touch()
        if method == 'account/rateLimits/updated':
            # 零散更新：合进最近一次 account/rateLimits/read 的结果再写快照（按协议说明，值为空的项不覆盖）
            rl = p.get('rateLimits') or {}
            raw = dict(getattr(self, 'quota_raw', None) or {})
            if not raw and not any(rl.get(k) for k in ('primary', 'secondary', 'credits')):
                return  # 没登录 ChatGPT 时也会推一条全空的，不写
            lid = rl.get('limitId')
            base = raw.get('rateLimits') or {}
            if not lid or lid == (base.get('limitId') or lid):
                raw['rateLimits'] = dict(base, **{k: v for k, v in rl.items() if v is not None})
            else:
                bl = dict(raw.get('rateLimitsByLimitId') or {})
                bl[lid] = dict(bl.get(lid) or {}, **{k: v for k, v in rl.items() if v is not None})
                raw['rateLimitsByLimitId'] = bl
            self.quota_raw = raw
            self.save_quota_doc(self.quota_doc(raw))
            return
        c = self.conv_of(p)
        if not c:
            return
        pv = c.pv = getattr(c, 'pv', {})
        if method == 'turn/started':
            tu = p.get('turn') or {}
            pv['turn'] = tu.get('id') or pv.get('turn')
            if c.turn and not c.turn.get('native'):
                c.turn['native'] = pv['turn']
            if pv.get('int_req') and pv['turn']:  # 复核修复 1：turn/start 返回前点的停止，拿到轮 ID 马上中断
                pv['int_req'] = False
                threading.Thread(target=self.interrupt, args=(c,), daemon=True).start()
            elif pv.get('steer_req') and pv['turn']:
                threading.Thread(target=self.send_now, args=(c,), daemon=True).start()
        elif method == 'item/started':
            self.item_started(c, p.get('item') or {})
        elif method == 'item/completed':
            self.item_completed(c, p.get('item') or {})
        elif method == 'item/agentMessage/delta':
            m = pv.get('msg')
            if not m or m.get('id') != p.get('itemId'):
                self.finish_text(c)  # 上一段没收到 item/completed（比如被插入的消息打断）：已经出来的先落地
                m = pv['msg'] = {'id': p.get('itemId'), 'text': ''}
            m['text'] += p.get('delta') or ''
            c.first_output()
            c.set_live(m['text'])
            c.set_spin('回复中…')
        elif method in ('item/reasoning/summaryTextDelta', 'item/reasoning/textDelta'):
            rs = pv.get('reason')
            if not rs or rs.get('id') != p.get('itemId'):
                rs = pv['reason'] = {'id': p.get('itemId'), 'text': '', 't0': time.time()}
            if method == 'item/reasoning/summaryTextDelta':
                rs['text'] += p.get('delta') or ''
            c.first_output()
            c.set_spin('思考中…')
        elif method == 'item/reasoning/summaryPartAdded':
            rs = pv.get('reason')
            if rs and rs.get('text'):
                rs['text'] += '\n\n'
        elif method == 'item/commandExecution/outputDelta':
            ent = self.nat.get((c.sid, p.get('itemId') or ''))
            if ent and ent[1]:
                b = self.outbuf.setdefault((c.sid, ent[1][0]), [c.sid, '', False])
                b[1] = (b[1] + (p.get('delta') or ''))[-8000:]
                b[2] = True
        elif method == 'turn/plan/updated':
            todos = [{'c': s.get('step') or '', 's': {'inProgress': 'in_progress'}.get(s.get('status'), s.get('status') or 'pending')}
                     for s in p.get('plan') or []]
            if pv.get('plan_item') and pv['plan_item'] in c.byid:
                c.patch(pv['plan_item'], todos=todos)
            else:
                pv['plan_item'] = c.add({'k': 'tool', 'name': 'TodoWrite', 'todos': todos, 'done': True, 'out': '', 'error': False,
                                         'desc': p.get('explanation') or '', 'in': '', 'path': None})
        elif method == 'thread/tokenUsage/updated':
            tu = p.get('tokenUsage') or {}
            last = tu.get('last') or {}
            if c.turn and (not c.turn.get('native') or p.get('turnId') == c.turn.get('native')):
                u = c.turn['usage']
                # last 是这一次模型请求的用量（实测一轮里调两次模型，total 翻倍、last 不变），一轮的用量＝本轮各次 last 相加
                for src, dst in (('inputTokens', 'input'), ('outputTokens', 'output'), ('cachedInputTokens', 'cachedInput'),
                                 ('cacheWriteInputTokens', 'cacheWrite'), ('reasoningOutputTokens', 'reasoning')):
                    if isinstance(last.get(src), int):
                        u[dst] = (u.get(dst) or 0) + last[src]
                c.turn['ctx'] = {'used': last.get('totalTokens') or 0, 'window': tu.get('modelContextWindow')}
            c.ctx = {'used': last.get('totalTokens') or 0, 'window': tu.get('modelContextWindow')}
        elif method == 'error':
            e = p.get('error') or {}
            if p.get('willRetry'):
                c.set_spin('出错了，正在重试…')
                return
            code, lbl = err_kind(e.get('codexErrorInfo'))
            if c.turn and not c.turn.get('err'):
                c.turn['err'] = (code, e.get('message') or '')
                c.add({'k': 'error', 'text': (e.get('message') or '出错了') + (('\n' + e['additionalDetails']) if e.get('additionalDetails') else ''),
                       'etype': lbl})
        elif method == 'turn/completed':
            tu = p.get('turn') or {}
            if not c.turn or (tu.get('id') and tu['id'] in pv.get('dead', ())):
                return  # 复核修复 4：这一轮已经由工作台结束（中断后超时），迟到的结束通知不能结束下一轮
            if c.turn.get('native') and tu.get('id') and tu['id'] != c.turn['native']:
                return
            st = tu.get('status')
            pv['turn'] = ''
            if st == 'completed':
                c.end_turn('done', stop='completed')
            elif st == 'interrupted':
                c.end_turn('interrupted', stop='interrupted', why='user' if pv.get('user_int') else 'other')
            else:
                e = tu.get('error') or {}
                code, lbl = err_kind(e.get('codexErrorInfo'))
                if c.turn and not c.turn.get('err'):
                    c.add({'k': 'error', 'text': e.get('message') or 'Codex 这一轮没有完成', 'etype': lbl})
                    c.turn['err'] = (code, e.get('message') or '')
                err = (c.turn or {}).get('err') or (code, e.get('message') or '')
                c.end_turn('error', error_type=err[0], error_msg=err[1], stop=st)
        elif method == 'thread/name/updated':
            if p.get('threadName'):
                c.set_meta(title=p['threadName'])
                self.core.emit({'type': 'sessions'})
        elif method == 'serverRequest/resolved':
            rid = self.req_map.pop(p.get('requestId'), None)
            if rid and rid in NS['PERMS']:
                ent = NS['PERMS'][rid]
                ent['res'] = None
                ent['ev'].set()

    def item_started(self, c, it):
        t, iid = it.get('type'), it.get('id') or ''
        pv = c.pv
        if t in ('userMessage', 'hookPrompt'):
            return
        c.first_output()
        if t == 'agentMessage':
            self.finish_text(c)
            pv['msg'] = {'id': iid, 'text': it.get('text') or ''}
            c.set_spin('回复中…')
            return
        if t == 'reasoning':
            pv['reason'] = {'id': iid, 'text': '', 't0': (it.get('startedAtMs') or time.time() * 1000) / 1000}
            c.set_spin('思考中…')
            return
        self.finish_text(c)
        if t == 'commandExecution':
            view = self.cmd_view(it)
            tid = c.add(dict(view, k='tool', done=False, out='', error=False))
            self.nat[(c.sid, iid)] = (c.sid, [tid], {'cmd': view})
            c.set_spin('正在运行命令…')
        elif t == 'fileChange':
            ids = []
            for ch in it.get('changes') or []:
                ids.append(c.add(dict(self.change_view(ch), k='tool', done=False, out='', error=False)))
            self.nat[(c.sid, iid)] = (c.sid, ids, {'changes': it.get('changes') or []})
            c.set_spin('正在修改文件…')
        elif t == 'mcpToolCall':
            tid = c.add({'k': 'tool', 'name': it.get('tool') or 'MCP', 'desc': it.get('server') or '', 'path': None,
                         'in': json.dumps(it.get('arguments'), ensure_ascii=False)[:800], 'done': False, 'out': '', 'error': False})
            self.nat[(c.sid, iid)] = (c.sid, [tid], {})
            c.set_spin('正在调用工具…')
        elif t == 'dynamicToolCall':
            tid = c.add({'k': 'tool', 'name': it.get('tool') or '工具', 'desc': it.get('namespace') or '', 'path': None,
                         'in': json.dumps(it.get('arguments'), ensure_ascii=False)[:800], 'done': False, 'out': '', 'error': False})
            self.nat[(c.sid, iid)] = (c.sid, [tid], {})
        elif t == 'webSearch':
            tid = c.add({'k': 'tool', 'name': 'WebSearch', 'desc': it.get('query') or '', 'path': None, 'in': '', 'done': False,
                         'out': '', 'error': False})
            self.nat[(c.sid, iid)] = (c.sid, [tid], {})
            c.set_spin('正在搜索网页…')
        elif t == 'imageView':
            tid = c.add({'k': 'tool', 'name': 'Read', 'desc': '', 'path': it.get('path'), 'in': '', 'done': False, 'out': '', 'error': False})
            self.nat[(c.sid, iid)] = (c.sid, [tid], {})
        elif t in ('collabAgentToolCall', 'subAgentActivity'):
            tid = c.add({'k': 'tool', 'name': 'Agent', 'desc': (it.get('prompt') or it.get('agentPath') or '')[:200], 'path': None,
                         'in': (it.get('prompt') or '')[:2000], 'done': False, 'out': '', 'error': False})
            self.nat[(c.sid, iid)] = (c.sid, [tid], {})
        elif t == 'contextCompaction':
            c.set_spin('正在压缩上下文…')

    def item_completed(self, c, it):
        t, iid = it.get('type'), it.get('id') or ''
        pv = c.pv
        if t in ('userMessage', 'hookPrompt'):
            return
        if t == 'agentMessage':
            m = pv.get('msg')
            text = it.get('text') or (m or {}).get('text') or ''
            pv['msg'] = None
            if text.strip():
                c.add({'k': 'text', 'text': text})
            c.set_live('')
            return
        if t == 'reasoning':
            rs = pv.get('reason') or {}
            text = '\n\n'.join(x for x in (it.get('summary') or []) if x) or rs.get('text') or ''
            pv['reason'] = None
            t0 = (it.get('startedAtMs') or 0) / 1000 or rs.get('t0') or time.time()
            t1 = (it.get('completedAtMs') or 0) / 1000 or time.time()
            secs = max(0, round(t1 - t0))
            if text.strip() or secs >= 1:
                c.add({'k': 'thought', 'secs': max(1, secs), 'text': text[:20000]})
            return
        if t == 'plan':
            if (it.get('text') or '').strip():
                c.add({'k': 'text', 'text': '**计划**\n\n' + it['text']})
            return
        if t == 'contextCompaction':
            c.add({'k': 'note', 'text': '已压缩上下文'})
            return
        if t in ('enteredReviewMode', 'exitedReviewMode'):
            c.add({'k': 'note', 'text': ('开始审查：' if t == 'enteredReviewMode' else '审查结束：') + (it.get('review') or '')[:200]})
            return
        ent = self.nat.pop((c.sid, iid), None)
        if not ent:
            return
        ids = ent[1]
        for i in ids:
            self.outbuf.pop((c.sid, i), None)
        st = it.get('status')
        if t == 'commandExecution':
            code = it.get('exitCode')
            out = NS['clean_text'](it.get('aggregatedOutput') or '')[:8000]
            err = st in ('failed', 'declined') or (code not in (None, 0))
            kw = {'out': out, 'error': err, 'done': True}
            if st == 'declined':
                kw['noresult'] = '已拒绝'
            elif code not in (None, 0):
                kw['out'] = (out + ('\n' if out else '') + f'退出码 {code}').strip()
            for i in ids:
                c.patch(i, **kw)
        elif t == 'fileChange':
            for i in ids:
                c.patch(i, done=True, error=st in ('failed', 'declined'), **({'noresult': '已拒绝'} if st == 'declined' else {}))
        elif t == 'mcpToolCall':
            res = it.get('result') or {}
            parts = [x.get('text') or '' for x in (res.get('content') or []) if isinstance(x, dict) and x.get('type') == 'text']
            err = it.get('error') or None
            for i in ids:
                c.patch(i, done=True, error=bool(err) or st == 'failed', out=((err or {}).get('message') or '\n'.join(parts))[:8000])
        elif t == 'dynamicToolCall':
            parts = [x.get('text') or '' for x in (it.get('contentItems') or []) if isinstance(x, dict)]
            for i in ids:
                c.patch(i, done=True, error=it.get('success') is False, out='\n'.join(parts)[:8000])
        else:
            for i in ids:
                c.patch(i, done=True, error=st == 'failed')
        if c.busy:
            c.set_spin('思考中…')

    def finish_text(self, c):
        """工具开始前，前面那段正文要先落地（正常情况下 item/completed 已经写了）。"""
        m = c.pv.get('msg')
        if m and m.get('text', '').strip():
            c.add({'k': 'text', 'text': m['text']})
            c.pv['msg'] = None
            c.set_live('')

    def cmd_view(self, it):
        cmd = unwrap_cmd(it.get('command') or '')
        acts = it.get('commandActions') or []
        if acts and all(a.get('type') == 'read' for a in acts) and len(acts) == 1:
            return {'name': 'Read', 'path': acts[0].get('path'), 'desc': '', 'in': cmd}
        if acts and all(a.get('type') == 'search' for a in acts) and len(acts) == 1:
            return {'name': 'Grep', 'path': acts[0].get('path'), 'desc': acts[0].get('query') or cmd, 'in': cmd}
        if acts and all(a.get('type') == 'listFiles' for a in acts) and len(acts) == 1:
            return {'name': 'Glob', 'path': acts[0].get('path'), 'desc': cmd, 'in': cmd}
        return {'name': 'Bash', 'path': None, 'desc': '', 'in': NS['CTRL_RE'].sub('', cmd)}

    def change_view(self, ch):
        kind = (ch.get('kind') or {}).get('type')
        path, diff = ch.get('path') or '', ch.get('diff') or ''
        if kind == 'add':
            return {'name': 'Write', 'path': path, 'desc': '', 'in': diff[:3000], 'ln': [len(diff.splitlines()), 0]}
        if kind == 'delete':
            return {'name': '删除文件', 'path': path, 'desc': '', 'in': '', 'ln': [0, len(diff.splitlines())]}
        old, new, ln = self.core.diff_view(diff)
        v = {'name': 'Edit', 'path': path, 'desc': ('移到 ' + ch['kind']['move_path']) if (ch.get('kind') or {}).get('move_path') else '',
             'in': '', 'ln': ln}
        if old is not None:
            v['diff'] = {'old': old, 'new': new}
        else:
            v['in'] = diff[:3000]
        return v

    # ---------------------------------------------------------------- 授权
    def on_request(self, rid, method, p):
        r = self.rpc
        c = self.conv_of(p)
        if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval') and c and r:
            return self.approval(r, rid, method, p, c)
        hk = self.core.HOOKS.get('codex_elicitation')  # 第十七轮 M：MCP 工具调用要授权（cwb 的委派工具等）时出授权卡
        if method == 'mcpServer/elicitation/request' and c and r and hk and hk(self, r, rid, p, c):
            return
        if method in ('execCommandApproval', 'applyPatchApproval'):
            # 旧版（v1）接口，v2 客户端一般收不到；收到就按“拒绝”回，免得卡住
            return r.respond(rid, {'decision': 'denied'})
        r.respond(rid, error={'code': -32601, 'message': f'工作台暂不支持 {method}'})

    def approval(self, r, rpc_id, method, p, c):
        avail = p.get('availableDecisions')
        has = lambda d: avail is None or any((x == d) or (isinstance(x, dict) and d in x) for x in avail)
        if method.startswith('item/commandExecution'):
            cmd = unwrap_cmd(p.get('command') or '')
            tool, inp = 'Bash', {'command': cmd, 'description': p.get('reason') or ''}
            view = {'title': '运行命令', 'cmd': cmd, 'cwd': p.get('cwd') or '', 'reason': p.get('reason') or ''}
            always = 'acceptForSession' if has('acceptForSession') else ('amend' if has('acceptWithExecpolicyAmendment') and p.get('proposedExecpolicyAmendment') else '')
            alabel = '本对话内都允许' if always == 'acceptForSession' else ('以后类似命令都允许' if always == 'amend' else '')
        else:
            ent = self.nat.get((c.sid if c else '', p.get('itemId') or '')) or (None, [], {})
            changes = (ent[2] or {}).get('changes') or []
            files = []
            for ch in changes:
                v = self.change_view(ch)
                files.append({'path': v.get('path'), 'kind': v['name'], 'old': (v.get('diff') or {}).get('old'), 'new': (v.get('diff') or {}).get('new'),
                              'text': v.get('in') or ''})
            f0 = files[0] if files else {'path': '', 'old': '', 'new': ''}
            tool = 'Edit'
            inp = {'file_path': f0.get('path') or '', 'old_string': f0.get('old') or '', 'new_string': f0.get('new') or f0.get('text') or ''}
            view = {'title': '修改文件' if len(files) <= 1 else f'修改 {len(files)} 个文件', 'files': files, 'reason': p.get('reason') or '',
                    'grantRoot': p.get('grantRoot') or ''}
            always = 'acceptForSession' if has('acceptForSession') else ''
            alabel = '本对话内这些文件都允许' if always else ''
        deny = 'decline' if has('decline') else 'cancel'

        def done(dec, msg):
            if dec == 'allow':
                d = 'accept'
            elif dec == 'always' and always == 'acceptForSession':
                d = 'acceptForSession'
            elif dec == 'always' and always == 'amend':
                d = {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': p.get('proposedExecpolicyAmendment')}}
            elif dec == 'always':
                d = 'accept'
            elif dec == 'deny':
                d = deny
            else:
                d = 'cancel'  # 作废（中断、进程退出）：拒绝并结束这一轮
            r.respond(rpc_id, {'decision': d})
            if dec == 'deny' and msg:
                if d == 'decline':  # 这一轮还在继续：说明插进当前这一轮
                    self.steer_text(c, '我拒绝了刚才的操作，说明：' + msg)
                else:  # 只能 cancel（这一轮随之结束）：说明排在最前面，结束后作为下一条发出
                    c.queue.insert(0, ('我拒绝了刚才的操作，说明：' + msg, []))
                    c.push_state()
        prid = self.core.ask_perm(c, tool, inp, view, [{'type': 'pv', 'label': alabel}] if always else [], done,
                                  {'pvAlways': alabel, 'pvDenyMsg': deny == 'decline'})
        self.req_map[rpc_id] = prid

    # ---------------------------------------------------------------- 后台
    def tick(self):
        for key, b in list(self.outbuf.items()):
            if not b[2]:
                continue
            b[2] = False
            tid = key[1]
            c = self.core.CONVS.get(b[0])
            if c and tid in c.byid and not c.byid[tid].get('done'):
                c.patch(tid, persist=False, out=NS['clean_text'](b[1])[-8000:])
