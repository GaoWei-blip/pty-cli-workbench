"""第十六轮 P：Antigravity 适配器（`agy acp`，stdio Agent Client Protocol v1）。

协议以 agentclientprotocol.com 的 v1 schema 为准（schema/v1/schema.json、meta.json），用到的：
- 请求：initialize、session/new、session/resume（对方声明 sessionCapabilities.resume 时）或 session/load（loadSession）、
  session/prompt、session/set_config_option（模型、思考强度、模式）、session/set_mode（没有配置项时）、session/close
- 通知：session/cancel（发）、session/update（收：agent_message_chunk、agent_thought_chunk、tool_call、tool_call_update、plan、
  usage_update、current_mode_update、config_option_update、session_info_update、available_commands_update、notice）
- 对方发来的请求：session/request_permission（其余如 fs/*、terminal/* 没有声明能力，回“不支持”）
数据目录：agy acp 默认和用户自己用的 agy 命令行共用 ~/.gemini/antigravity-cli。实测共用时（见 NOTES-P）工作台开的对话会混进
命令行的历史，而且 ACP 进程在跑时命令行偶尔报“没登录”，所以用独立的 `--app-data-dir antigravity-cwb`；登录信息在 ~/.gemini 下，照样共用。
测试时用 CWB_AGY_CMD（JSON 数组）换成假 agent。
"""
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time

import providers as core

NS = {}
APP_DATA = 'antigravity-cwb'
MODE_ZH = {'default': ('默认', '改文件、运行命令前按你的权限设置询问'),
           'accept-edits': ('自动接受编辑', '改这个文件夹里的文件不问，其他操作照常确认'),
           'auto': ('自动', '改文件和安全的命令直接做，不可撤回的操作前再问'),
           'plan': ('规划', '先想清楚、出方案再动手')}
EFF_ZH = {'low': '低', 'medium': '中', 'high': '高', 'xhigh': '超高', 'max': '最高', 'minimal': '最少'}
KIND_NAME = {'execute': 'Bash', 'read': 'Read', 'edit': 'Edit', 'delete': '删除文件', 'move': '移动文件', 'search': 'Grep',
             'fetch': 'WebFetch', 'think': '思考', 'switch_mode': '切换模式'}
# 11-23：登录没成功时自己重试。实测（acp-server-20261123_134639 日志）：闲置后 agy 进程被回收，再用时新进程启动要用钥匙串里的令牌
# 去 oauth2.googleapis.com 刷新，网络刚恢复（休眠唤醒、代理重连）时 TLS 握手超时，agy 静默登录失败，之后的会话请求都回
# auth_required（-32000）；但每次 session/new、resume、load、prompt 它都会重新读钥匙串、自己刷新令牌，网络通了再请求一次就好。
# 所以：先等到能连上 Google，再重发同一个请求，最多重试 AUTH_RETRIES 次；网络一直不通、或通了还失败（真的掉登录了）才提示。
# 不调 ACP authenticate：agy 的 oauth-personal 会走它自己的浏览器登录（T3 AntigravityDriver 的注释），网络抖一下就弹浏览器不合适。
GOOGLE = ('oauth2.googleapis.com', 443)
AUTH_RETRIES = 2
NET_WAIT = float(os.environ.get('CWB_AGY_NET_WAIT') or 45)   # 最多等这么久网络恢复（测试可调短）
NET_HINT = '连不上 Google（oauth2.googleapis.com），Antigravity 没法刷新登录。等网络或代理恢复后再发一次就行，不用去终端。'
AUTH_HINT = '登录 Antigravity 没成功（已自动重试，网络是通的，多半是登录过期了）：在终端运行 agy，按提示用 Google 登录后再回来。'


def google_reachable(timeout=6.0):
    """能不能连上 Google 的登录服务：只做一次 TLS 握手（不发数据、不校验证书，只看通不通）。
    用 TUN 代理时 TCP 连接总是本机秒通，要握手成功才说明真的连出去了。测试设 CWB_AGY_NETCHECK=off 跳过。"""
    if os.environ.get('CWB_AGY_NETCHECK') == 'off':
        return True
    try:
        with socket.create_connection(GOOGLE, timeout=timeout) as s:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with ctx.wrap_socket(s, server_hostname=GOOGLE[0]):
                return True
    except Exception:
        return False


def cfg_opts(opts):
    """SessionConfigSelectOptions：平铺的 [{value,name}] 或分组的 [{group,name,options:[…]}]，摊平。"""
    out = []
    for o in opts or []:
        if isinstance(o, dict) and isinstance(o.get('options'), list):
            out += [x for x in o['options'] if isinstance(x, dict)]
        elif isinstance(o, dict):
            out.append(o)
    return out


def find_cfg(config, cat):
    for o in config or []:
        if isinstance(o, dict) and (o.get('category') == cat or o.get('id') == cat):
            return o
    return None


def block_text(b):
    if not isinstance(b, dict):
        return ''
    if b.get('type') == 'text':
        return b.get('text') or ''
    if b.get('type') == 'resource_link':
        return b.get('uri') or b.get('name') or ''
    if b.get('type') == 'resource':
        r = b.get('resource') or {}
        return r.get('text') or r.get('uri') or ''
    return ''


class AcpAdapter(core.Adapter):
    id, label = 'agy', 'Antigravity'

    def __init__(self):
        core.Adapter.__init__(self, core)
        self.sessions = {}     # sessionId -> sid
        self.loading = set()   # 正在 session/load 的会话：对方回放的历史不再写进时间线（工作台有自己的记录）
        self.nat = {}          # (sessionId, toolCallId) -> (sid, 时间线条目 ID, 原始字段)；toolCallId 只在一个会话里唯一（复核修复 3）
        self.early = {}        # session/new 返回前后就到的通知（还不知道属于哪个对话）：会话 ID -> [通知]，对上后补处理
        self.init = {}
        self.ver = None
        self.cache_path = os.path.join(core.DATA_DIR, 'prov-cache-agy.json')
        self.cache = self.load_cache()

    def load_cache(self):
        try:
            return json.load(open(self.cache_path, encoding='utf-8'))
        except Exception:
            return {}

    def save_cache(self):
        try:
            os.makedirs(core.DATA_DIR, exist_ok=True)
            tmp = self.cache_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except Exception:
            pass

    # ---------------------------------------------------------------- 进程
    def argv(self):
        if os.environ.get('CWB_AGY_CMD'):
            cmd = json.loads(os.environ['CWB_AGY_CMD'])
        else:
            cmd = [core.which('agy') or 'agy', 'acp', '--app-data-dir', APP_DATA]
        return cmd + json.loads(os.environ.get('CWB_AGY_ARGS') or '[]')

    def start_proc(self):
        with self.lock:
            if self.rpc and self.rpc.alive():
                return self.rpc
            argv = self.argv()
            holder = {}
            try:
                rpc = core.Rpc('agy', argv, self.env(), NS['HOME'], self.on_notify, self.on_request,
                               lambda code: self.on_proc_exit(holder['r'], code),
                               os.path.join(NS['RUN_DIR'], f'prov-agy-{NS["PORT"]}.log'))
            except OSError as e:
                raise ValueError('启动 Antigravity 失败：' + str(e))
            holder['r'] = rpc
            try:
                res = rpc.request('initialize', {'protocolVersion': 1,
                                                 'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False},
                                                 'clientInfo': {'name': 'claude-workbench', 'title': 'Claude 工作台', 'version': '1.0'}}, 40) or {}
            except Exception as e:
                rpc.reaped = 'init-failed'
                rpc.close(1)
                raise ValueError('Antigravity 启动失败：' + str(e))
            self.init = res
            pc = (res.get('agentCapabilities') or {}).get('promptCapabilities') or {}
            self.cache['image'] = bool(pc.get('image'))
            self.save_cache()
            self.rpc = rpc
            self.sessions = {}
            self.note_pid(rpc, argv)
            self.touch()
            core.log('Antigravity 进程已启动', rpc.pid, (res.get('agentInfo') or {}).get('version'))
            return rpc

    def session_caps(self):
        ac = self.init.get('agentCapabilities') or {}
        return ac.get('sessionCapabilities') or ac.get('session_capabilities') or {}

    def auth_hint(self, e):
        if getattr(e, 'code', None) == -32000 or 'auth' in str(getattr(e, 'message', e)).lower() or '登录' in str(e):
            return '还没登录 Antigravity：在终端运行 agy，按提示用 Google 登录后再回来。'
        return ''

    def ensure_attached(self, c):
        rpc = self.start_proc()
        sid_n = c.meta.get('native') or ''
        if c.attached and sid_n and self.sessions.get(sid_n) == c.sid:
            return
        c.pv = getattr(c, 'pv', {})
        base = {'cwd': c.cwd, 'mcpServers': []}
        tries = 0
        while True:
            sid_n = c.meta.get('native') or ''
            try:
                res = self.open_session(rpc, c, sid_n, base)
                break
            except core.RpcError as e:
                self.sessions.pop(sid_n, None)
                hint = self.auth_hint(e)
                if hint and tries < AUTH_RETRIES and not c.stop_req:  # 11-23：登录没成功，等网络恢复后自己重试
                    tries += 1
                    ok = self.wait_login(c, tries, e)
                    if c.stop_req:
                        return  # 等的时候点了停止：start_turn 按“已中断”结束这一轮
                    if not ok:
                        raise ValueError(NET_HINT)
                    rpc = self.start_proc()  # 等的期间进程被回收或退出了就重新拉起
                    continue
                if hint:
                    raise ValueError(AUTH_HINT if tries else hint)
                if c.meta.get('native'):
                    raise ValueError(f'Antigravity 续接不上这个对话（{e.message}），可以新开一个对话。')
                raise ValueError('Antigravity 新建对话失败：' + e.message)
        self.take_state(c, res)
        c.attached = True
        self.touch()
        try:
            self.apply_opts(c)
        except Exception as e:
            core.log('Antigravity 设置选项失败', c.sid[:8], repr(e))

    def open_session(self, rpc, c, sid_n, base):
        """有原生 ID 就续接（resume，不支持时 load），没有就新建；返回会话信息。"""
        if sid_n:
            sc = self.session_caps()
            self.sessions[sid_n] = c.sid
            if 'resume' in sc and sc.get('resume') is not None:
                return rpc.request('session/resume', dict(base, sessionId=sid_n), 90) or {}
            if (self.init.get('agentCapabilities') or {}).get('loadSession'):
                self.loading.add(sid_n)
                try:
                    return rpc.request('session/load', dict(base, sessionId=sid_n), 120) or {}
                finally:
                    self.loading.discard(sid_n)
            raise ValueError('Antigravity 不支持续接对话')
        res = rpc.request('session/new', base, 90) or {}
        sid_n = res.get('sessionId') or ''
        if not sid_n:
            raise ValueError('Antigravity 没有返回会话 ID')
        c.set_meta(native=sid_n)
        self.sessions[sid_n] = c.sid
        for p in self.early.pop(sid_n, []):
            self.on_notify('session/update', p)
        return res

    def wait_login(self, c, tries, e):
        """11-23：登录没成功。转圈处写“正在重试”，等到能连上 Google（最多 NET_WAIT 秒）。
        返回 True＝网络通了可以重发；False＝一直连不上，或者等的时候点了停止（调用方看 c.stop_req 区分）。"""
        core.log('Antigravity 登录没成功，等网络恢复后重试', c.sid[:8], tries, getattr(e, 'message', e))
        c.set_spin(f'登录 Antigravity 没成功，正在重试（第 {tries} 次）…')
        c.push_state()
        end = time.time() + NET_WAIT
        while not c.stop_req:
            if google_reachable(min(6.0, max(1.0, end - time.time()))):
                return True
            nxt = min(end, time.time() + 3.0)  # 每 3 秒探测一次，期间每 0.2 秒看一眼有没有点停止
            if time.time() >= end:
                return False
            while time.time() < nxt and not c.stop_req:
                time.sleep(0.2)
        return False

    def take_state(self, c, res):
        pv = c.pv
        if isinstance(res.get('configOptions'), list):
            pv['config'] = res['configOptions']
            self.cache['config'] = res['configOptions']
            self.save_cache()
        if isinstance(res.get('modes'), dict):
            pv['modes'] = res['modes']
            pv['mode'] = res['modes'].get('currentModeId') or pv.get('mode')
            self.cache['modes'] = res['modes']
            self.save_cache()

    def detach(self, c):
        sid_n = c.meta.get('native') or ''
        r = self.rpc
        if r and r.alive() and sid_n and self.sessions.get(sid_n) == c.sid and 'close' in self.session_caps():
            try:
                r.request('session/close', {'sessionId': sid_n}, 10)
            except Exception:
                pass
        self.sessions.pop(sid_n, None)
        c.attached = False

    # ---------------------------------------------------------------- 选项
    def cur_cfg(self, c, cat):
        o = find_cfg((getattr(c, 'pv', {}) or {}).get('config') if c else None, cat)
        return (o or {}).get('currentValue') or ''

    def apply_opts(self, c):
        """把对话选的模型、思考强度、模式交给对方；回复进行中先存着，发下一条前再设。"""
        r = self.rpc
        if not (c.attached and r and r.alive()) or (getattr(c, 'pv', {}) or {}).get('prompting'):
            return  # session/prompt 进行中不改，发下一条前再设
        sid_n = c.meta.get('native')
        pv = c.pv
        o = c.opts()
        for key, cat in (('model', 'model'), ('effort', 'thought_level'), ('mode', 'mode')):
            want = o.get(key)
            if not want:
                continue
            opt = find_cfg(pv.get('config'), cat)
            if opt and opt.get('currentValue') != want and any(x.get('value') == want for x in cfg_opts(opt.get('options'))):
                res = r.request('session/set_config_option', {'sessionId': sid_n, 'configId': opt['id'], 'value': want}, 30) or {}
                if isinstance(res.get('configOptions'), list):
                    pv['config'] = res['configOptions']
                if cat == 'mode':
                    pv['mode'] = want
            elif not opt and cat == 'mode' and pv.get('mode') != want:
                r.request('session/set_mode', {'sessionId': sid_n, 'modeId': want}, 30)
                pv['mode'] = want

    def cur_model(self, c):
        if not c:
            return ''
        return c.opts().get('model') or self.cur_cfg(c, 'model') or ''

    def turn_model(self, c):
        return self.cur_model(c)

    def turn_effort(self, c):
        return c.opts().get('effort') or self.cur_cfg(c, 'thought_level') or ''

    def live_opts(self, c):
        """会话自己当前的值（不看工作台选的），界面药丸在没选时显示它。"""
        pv = getattr(c, 'pv', {}) or {}
        return {'model': self.cur_cfg(c, 'model'), 'effort': self.cur_cfg(c, 'thought_level'),
                'mode': self.cur_cfg(c, 'mode') or pv.get('mode') or ''}

    def caps(self, c):
        return {'image': bool(self.cache.get('image', True)), 'steer': False, 'denyMsg': True}

    def models(self, c=None, refresh=False):
        config = (getattr(c, 'pv', {}) or {}).get('config') if c else None
        modes = (getattr(c, 'pv', {}) or {}).get('modes') if c else None
        config = config or self.cache.get('config')
        modes = modes or self.cache.get('modes')
        err = ''
        if (refresh or not config) and self.status().get('installed'):
            try:
                config, modes = self.probe_options()
            except Exception as e:
                err = 'Antigravity 的模型列表读不出来：' + (self.auth_hint(e) or str(e))
        mo, eo = find_cfg(config, 'model'), find_cfg(config, 'thought_level')
        md = find_cfg(config, 'mode')
        out = {'models': [{'id': x.get('value'), 'label': x.get('name') or x.get('value'), 'desc': x.get('description') or ''}
                          for x in cfg_opts((mo or {}).get('options'))],
               'efforts': [{'id': x.get('value'), 'label': EFF_ZH.get(x.get('value'), x.get('name') or x.get('value'))}
                           for x in cfg_opts((eo or {}).get('options'))],
               'defaultModel': (mo or {}).get('currentValue') or '', 'defaultEffort': (eo or {}).get('currentValue') or '',
               'error': err}
        if md:
            ml = [{'id': x.get('value'), 'name': x.get('name'), 'description': x.get('description')} for x in cfg_opts(md.get('options'))]
            cur_mode = md.get('currentValue') or 'default'
        else:
            ml = (modes or {}).get('availableModes') or []
            cur_mode = (modes or {}).get('currentModeId') or 'default'
        out['modes'] = [{'id': m.get('id'), 'label': MODE_ZH.get(m.get('id'), (m.get('name'),))[0] or m.get('id'),
                         'desc': MODE_ZH[m['id']][1] if m.get('id') in MODE_ZH else (m.get('description') or '')} for m in ml]
        out['defaultMode'] = cur_mode
        out['defaultLabel'] = next((m['label'] for m in out['models'] if m['id'] == out['defaultModel']), '')
        out['effortNames'] = EFF_ZH
        return out

    def probe_options(self):
        """首页还没开过 Antigravity 对话时，模型列表要从 session/new 的结果里拿：在临时文件夹开一个空会话（不发消息）读出来就关掉。"""
        rpc = self.start_proc()
        d = os.path.join(tempfile.gettempdir(), 'cwb-agy-probe')
        os.makedirs(d, exist_ok=True)
        res = rpc.request('session/new', {'cwd': os.path.realpath(d), 'mcpServers': []}, 90) or {}
        if res.get('sessionId') and 'close' in self.session_caps():
            try:
                rpc.request('session/close', {'sessionId': res['sessionId']}, 10)
            except Exception:
                pass
        if isinstance(res.get('configOptions'), list):
            self.cache['config'] = res['configOptions']
        if isinstance(res.get('modes'), dict):
            self.cache['modes'] = res['modes']
        self.save_cache()
        return self.cache.get('config'), self.cache.get('modes')

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
        if not (os.path.isabs(exe) and os.path.exists(exe)) and not core.which(exe):
            st = {'installed': False, 'ok': False, 'hint': '没有找到 agy 命令。先安装 Antigravity 命令行，再在终端运行一次 agy 用 Google 登录。'}
        elif os.environ.get('CWB_AGY_CMD'):
            st = {'installed': True, 'ok': True, 'version': 'test', 'hint': ''}
        else:
            gem = os.path.join(NS['HOME'], '.gemini')
            logged = os.path.exists(os.path.join(gem, 'oauth_creds.json')) or bool((NS.get('LOGIN_ENV') or {}).get('GEMINI_API_KEY'))
            st = {'installed': True, 'ok': logged, 'version': self.version(),
                  'hint': '' if logged else '还没登录 Antigravity：在终端运行 agy，按提示用 Google 登录后再回来。'}
        self.st_cache = (time.time(), st)
        return st

    # ---------------------------------------------------------------- 一轮
    def send(self, c, text, images):
        rpc = self.start_proc()
        self.apply_opts(c)
        prompt = [{'type': 'text', 'text': text}] if text else []
        for p in images:
            try:
                prompt.append({'type': 'image', 'data': core.b64file(p), 'mimeType': core.mime_of(p)})
            except OSError:
                pass
        pv = c.pv
        # 复核修复 1：user_int（停止）在占住这一轮时（PConv.claim）重置，这里不能再清，否则连接期间点的停止会被冲掉
        pv.update(msg=None, thought=None, plan_item=None, gen=pv.get('gen', 0) + 1)
        gen = pv['gen']
        sid_n = c.meta['native']
        if c.stop_req:  # 连接期间已经点了停止：不发出，这一轮按“已中断”结束
            c.end_turn('interrupted', why='user')
            return

        def run():
            tries = 0
            while True:
                with c.lock:  # 和 interrupt 对齐：要么这里先看到停止不发，要么 interrupt 看到 prompting 去发 session/cancel
                    if c.stop_req:
                        stopped = True
                    else:
                        stopped = False
                        pv['prompting'] = True
                if stopped:
                    c.end_turn('interrupted', why='user')
                    return
                try:
                    res = rpc.request('session/prompt', {'sessionId': sid_n, 'prompt': prompt}, 6 * 3600) or {}
                    break
                except core.RpcError as e:
                    if pv.get('gen') == gen:
                        pv['prompting'] = False  # 已被工作台结束的旧一轮迟到返回时，不动下一轮的状态
                    if pv.get('gen') != gen or not c.busy:
                        return
                    if pv.get('user_int') and e.code in (-32800, -32099):
                        c.end_turn('interrupted', why='user', stop='cancelled')
                        return
                    hint, etype = self.auth_hint(e), '鉴权失败'
                    # 11-23：登录没成功、这一轮还没有任何输出（对方还没开始处理这条消息）：等网络恢复后重发
                    if hint and tries < AUTH_RETRIES and c.turn and not c.turn.get('first') and not c.stop_req:
                        tries += 1
                        ok = self.wait_login(c, tries, e)
                        if pv.get('gen') != gen or not c.busy:
                            return
                        if c.stop_req:
                            c.end_turn('interrupted', why='user')
                            return
                        if ok:
                            c.set_spin('思考中…')
                            continue
                        hint, etype = NET_HINT, '连接出错'
                    elif hint and tries:
                        hint = AUTH_HINT
                    c.add({'k': 'error', 'text': hint or ('Antigravity 出错：' + e.message), 'etype': etype if hint else '接口出错'})
                    c.end_turn('error', error_type=str(e.code), error_msg=e.message)
                    return
            if pv.get('gen') == gen:
                pv['prompting'] = False
            if pv.get('gen') != gen or not c.busy:
                return
            sr = res.get('stopReason') or ''
            u = res.get('usage') if isinstance(res.get('usage'), dict) else None
            if u and c.turn:  # 新版协议的 PromptResponse 可能带 usage（实测 agy 1.5.14 不带）
                for src, dst in (('inputTokens', 'input'), ('outputTokens', 'output'), ('cachedReadTokens', 'cachedInput'),
                                 ('cachedWriteTokens', 'cacheWrite'), ('thoughtTokens', 'reasoning')):
                    if isinstance(u.get(src), int):
                        c.turn['usage'][dst] = u[src]
            if sr == 'cancelled':
                c.end_turn('interrupted', why='user' if pv.get('user_int') else 'other', stop=sr)
            elif sr == 'refusal':
                c.add({'k': 'error', 'text': '模型拒绝回答这个请求', 'etype': '请求被拒绝'})
                c.end_turn('error', error_type='refusal', stop=sr)
            else:
                c.end_turn('done', stop=sr)
        threading.Thread(target=run, daemon=True).start()
        self.touch()

    def interrupt(self, c):
        pv = c.pv = getattr(c, 'pv', {})
        with c.lock:
            pv['user_int'] = True
            c.stop_req = True  # 复核修复 1：还在连接（session/new、resume、load）时，start_turn / send 看到它就不发出
            prompting = bool(pv.get('prompting'))
        c.set_spin('正在停止…')
        core.drop_perms(c.sid, cancel=True)  # 按协议，取消时还挂着的授权请求要回 cancelled
        if not prompting:
            return  # session/prompt 还没发出：start_turn / send 会按“已中断”结束这一轮
        r = self.rpc
        seq = c.tseq

        def cancel():
            if r and r.alive() and c.meta.get('native'):
                try:
                    r.notify('session/cancel', {'sessionId': c.meta['native']})
                except Exception:
                    pass
        cancel()

        def watchdog():
            # 复核修复 1/4：按这一轮的代号（tseq）判断，不再用 gen（发下一条时 gen 会变，兜底就失效了）。
            # session/prompt 刚发出时对方可能还没开始这一轮，1.5 秒后再发一次取消；20 秒内还没结束就在工作台这边结束
            time.sleep(1.5)
            if c.busy and c.tseq == seq:
                cancel()
            time.sleep(max(0.0, core.STOP_GRACE - 1.5))
            if c.busy and c.tseq == seq:
                core.log('Antigravity 取消后没有结束这一轮，工作台这边结束', c.sid[:8])
                pv['gen'] = pv.get('gen', 0) + 1  # 之后对方迟到的返回不再结束下一轮
                pv['prompting'] = False
                c.end_turn('interrupted', why='user', stop='cancelled')
        threading.Thread(target=watchdog, daemon=True).start()

    def send_now(self, c):
        """ACP 不能往进行中的一轮插消息：立即发送＝停掉这一轮，停好后自动发排队的消息。"""
        if c.busy and c.queue:
            self.interrupt(c)

    def flush(self, c):
        pv = getattr(c, 'pv', {}) or {}
        th = pv.get('thought')
        if th and th.get('text', '').strip():
            c.add({'k': 'thought', 'secs': max(1, round(time.time() - th['t0'])), 'text': th['text'][:20000]})
        pv['thought'] = None
        m = pv.get('msg')
        if m and m.get('text', '').strip():
            c.add({'k': 'text', 'text': m['text']})
        pv['msg'] = None
        c.live = ''

    def flush_thought(self, c):
        th = c.pv.get('thought')
        if th and th.get('text', '').strip():
            c.add({'k': 'thought', 'secs': max(1, round(time.time() - th['t0'])), 'text': th['text'][:20000]})
        c.pv['thought'] = None

    def flush_msg(self, c):
        m = c.pv.get('msg')
        if m and m.get('text', '').strip():
            c.add({'k': 'text', 'text': m['text']})
        c.pv['msg'] = None
        c.set_live('')

    # ---------------------------------------------------------------- 通知
    def on_notify(self, method, p):
        self.touch()
        if method != 'session/update':
            return
        sid_n = p.get('sessionId') or ''
        if sid_n in self.loading:
            return
        c = core.CONVS.get(self.sessions.get(sid_n) or '')
        if not c:
            if sid_n and sid_n not in self.sessions:
                q = self.early.setdefault(sid_n, [])
                if len(q) < 50:
                    q.append(p)
                while len(self.early) > 20:
                    self.early.pop(next(iter(self.early)))
            return
        pv = c.pv = getattr(c, 'pv', {})
        u = p.get('update') or {}
        t = u.get('sessionUpdate')
        if t == 'agent_message_chunk':
            if not c.busy:
                return
            txt = block_text(u.get('content'))
            if not txt:
                return
            self.flush_thought(c)
            m = pv.get('msg')
            mid = u.get('messageId')
            if m and mid and m.get('mid') and m['mid'] != mid:
                self.flush_msg(c)
                m = None
            if not m:
                m = pv['msg'] = {'mid': mid, 'text': ''}
            m['text'] += txt
            c.first_output()
            c.set_live(m['text'])
            c.set_spin('回复中…')
        elif t == 'agent_thought_chunk':
            if not c.busy:
                return
            self.flush_msg(c)
            th = pv.get('thought')
            if not th:
                th = pv['thought'] = {'text': '', 't0': time.time()}
            th['text'] += block_text(u.get('content'))
            c.first_output()
            c.set_spin('思考中…')
        elif t == 'tool_call':
            if not c.busy:
                return
            self.flush_thought(c)
            self.flush_msg(c)
            c.first_output()
            tcid = u.get('toolCallId') or ''
            v = self.tool_view(u)
            st = u.get('status') or 'pending'
            done = st in ('completed', 'failed')
            iid = c.add(dict(v, k='tool', done=done, error=st == 'failed'))
            self.nat[(sid_n, tcid)] = (c.sid, iid, dict(u))
            c.set_spin('正在运行命令…' if u.get('kind') == 'execute' else '正在使用工具…')
        elif t == 'tool_call_update':
            tcid = u.get('toolCallId') or ''
            ent = self.nat.get((sid_n, tcid))
            if not ent or ent[0] != c.sid:
                return
            raw = ent[2]
            for k, v in u.items():
                if v is not None and k != 'sessionUpdate':
                    raw[k] = v
            v = self.tool_view(raw)
            st = raw.get('status') or 'pending'
            done = st in ('completed', 'failed')
            kw = dict(v, done=done, error=st == 'failed')
            c.patch(ent[1], persist=done, **kw)
            if done:
                self.nat.pop((sid_n, tcid), None)
                if c.busy:
                    c.set_spin('思考中…')
        elif t == 'plan':
            self.flush_thought(c)
            self.flush_msg(c)
            todos = [{'c': e.get('content') or '', 's': e.get('status') or 'pending'} for e in u.get('entries') or []]
            if pv.get('plan_item') and pv['plan_item'] in c.byid:
                c.patch(pv['plan_item'], todos=todos)
            elif c.busy:
                pv['plan_item'] = c.add({'k': 'tool', 'name': 'TodoWrite', 'todos': todos, 'done': True, 'out': '', 'error': False,
                                         'desc': '', 'in': '', 'path': None})
        elif t == 'usage_update':
            c.ctx = {'used': u.get('used'), 'window': u.get('size')}
            if c.turn:
                c.turn['ctx'] = dict(c.ctx)
                cost = u.get('cost')
                if isinstance(cost, dict) and cost.get('amount') is not None:
                    # 规范：cost 是会话累计费用，新值覆盖旧值，不能累加
                    c.turn['usage']['cost'] = {'amount': cost.get('amount'), 'currency': cost.get('currency'), 'src': 'usage_update', 'scope': 'session'}
        elif t == 'current_mode_update':
            pv['mode'] = u.get('currentModeId') or pv.get('mode')
            c.dirty = True
        elif t == 'config_option_update':
            if isinstance(u.get('configOptions'), list):
                pv['config'] = u['configOptions']
                c.dirty = True
        elif t == 'session_info_update':
            if u.get('title'):
                c.set_meta(title=str(u['title'])[:80])
                core.emit({'type': 'sessions'})
        elif t == 'available_commands_update':
            c.cmds = [{'name': x.get('name'), 'desc': x.get('description') or ''} for x in u.get('availableCommands') or [] if x.get('name')]
        elif t == 'notice':
            self.flush_thought(c)
            if u.get('title'):
                c.add({'k': 'note', 'text': (u.get('title') or '') + (('：' + u['description']) if u.get('description') else '')})

    def tool_view(self, tc):
        kind = tc.get('kind') or 'other'
        title = tc.get('title') or ''
        raw = tc.get('rawInput') if isinstance(tc.get('rawInput'), dict) else {}
        locs = [x for x in tc.get('locations') or [] if isinstance(x, dict)]
        path = (locs[0].get('path') if locs else None) or raw.get('path') or raw.get('file_path') or raw.get('filePath') or None
        out, diff = [], None
        for ct in tc.get('content') or []:
            if not isinstance(ct, dict):
                continue
            if ct.get('type') == 'content':
                out.append(block_text(ct.get('content')))
            elif ct.get('type') == 'diff':
                diff = ct
            elif ct.get('type') == 'terminal':
                out.append('（在终端里运行）')
        if not out and tc.get('rawOutput') is not None:
            ro = tc['rawOutput']
            out.append(ro if isinstance(ro, str) else json.dumps(ro, ensure_ascii=False)[:4000])
        v = {'name': KIND_NAME.get(kind, title[:40] or '工具'), 'desc': '', 'path': path, 'in': '', 'out': NS['clean_text']('\n'.join(x for x in out if x))[:8000]}
        if kind == 'execute':
            cmd = raw.get('command') or raw.get('cmd') or ''
            if isinstance(cmd, list):
                cmd = ' '.join(str(x) for x in cmd)
            v['in'] = NS['CTRL_RE'].sub('', str(cmd or title))
            v['desc'] = title if cmd and title and title != cmd else ''
            v['path'] = None
        elif diff:
            v['path'] = diff.get('path') or path
            if diff.get('oldText') is None:
                v['name'], v['in'] = 'Write', (diff.get('newText') or '')[:3000]
                v['ln'] = [len((diff.get('newText') or '').splitlines()), 0]
            else:
                v['name'] = 'Edit'
                v['diff'] = {'old': (diff.get('oldText') or '')[:3000], 'new': (diff.get('newText') or '')[:3000]}
                v['ln'] = core.text_lines(diff.get('oldText'), diff.get('newText'))
        elif kind in ('read', 'edit', 'delete', 'move'):
            if not path:
                v['desc'] = title
        elif kind == 'fetch':
            v['desc'] = raw.get('url') or title
        else:
            v['desc'] = title
            if raw and kind not in ('think',):
                v['in'] = json.dumps(raw, ensure_ascii=False)[:800]
        return v

    # ---------------------------------------------------------------- 授权
    def on_request(self, rid, method, p):
        r = self.rpc
        if method != 'session/request_permission' or not r:
            if r:
                r.respond(rid, error={'code': -32601, 'message': f'工作台不支持 {method}'})
            return
        c = core.CONVS.get(self.sessions.get(p.get('sessionId') or '') or '')
        if not c:
            r.respond(rid, {'outcome': {'outcome': 'cancelled'}})
            return
        if c.meta.get('autoAllow'):
            # 11-25：父对话“跳过确认”派来的子任务（委派运行档 full-access）。工作台替用户点“允许”，不弹卡。
            # 先选“允许这一次”：选“总是允许”会把规则写进 antigravity-cwb 的设置，连带影响用户自己开的 agy 对话。
            po = [o for o in p.get('options') or [] if isinstance(o, dict)]
            oid = next((o.get('optionId') for k in ('allow_once', 'allow_always') for o in po if o.get('kind') == k), None)
            if oid:
                r.respond(rid, {'outcome': {'outcome': 'selected', 'optionId': oid}})
                core.log('Antigravity 自动允许', c.sid[:8], ((p.get('toolCall') or {}).get('title') or '')[:80])
                return
        tc = dict((self.nat.get((p.get('sessionId') or '', (p.get('toolCall') or {}).get('toolCallId') or '')) or (None, None, {}))[2] or {})
        tc.update({k: v for k, v in (p.get('toolCall') or {}).items() if v is not None})
        v = self.tool_view(tc)
        opts = [o for o in p.get('options') or [] if isinstance(o, dict)]
        by = {}
        for o in opts:
            by.setdefault(o.get('kind'), o.get('optionId'))
        if v['name'] == 'Bash':
            tool, inp = 'Bash', {'command': v.get('in') or '', 'description': v.get('desc') or ''}
            view = {'title': '运行命令', 'cmd': v.get('in') or '', 'reason': ''}
        elif v.get('diff') or v['name'] == 'Write':
            tool = 'Edit'
            inp = {'file_path': v.get('path') or '', 'old_string': (v.get('diff') or {}).get('old') or '',
                   'new_string': (v.get('diff') or {}).get('new') or v.get('in') or ''}
            view = {'title': '修改文件', 'files': [{'path': v.get('path'), 'kind': v['name'], 'old': (v.get('diff') or {}).get('old'),
                                                 'new': (v.get('diff') or {}).get('new'), 'text': v.get('in') or ''}]}
        else:
            tool = tc.get('title') or v['name']
            inp = tc.get('rawInput') if isinstance(tc.get('rawInput'), dict) else {'说明': tc.get('title') or ''}
            view = {'title': tc.get('title') or '使用工具', 'text': json.dumps(inp, ensure_ascii=False, indent=1)[:3000]}
        always = by.get('allow_always')
        alabel = next((o.get('name') for o in opts if o.get('kind') == 'allow_always'), '') if always else ''

        def done(dec, msg):
            oid = None
            if dec == 'allow':
                oid = by.get('allow_once') or by.get('allow_always')
            elif dec == 'always':
                oid = by.get('allow_always') or by.get('allow_once')
            elif dec == 'deny':
                oid = by.get('reject_once') or by.get('reject_always')
            if oid:
                r.respond(rid, {'outcome': {'outcome': 'selected', 'optionId': oid}})
            else:
                r.respond(rid, {'outcome': {'outcome': 'cancelled'}})
            if dec == 'deny' and msg:
                # ACP 不能往进行中的一轮插消息：说明排在最前面，这一轮结束后作为下一条发出
                c.queue.insert(0, ('我拒绝了刚才的操作，说明：' + msg, []))
                c.push_state()
        core.ask_perm(c, tool, inp, view, [{'type': 'pv', 'label': alabel}] if always else [], done,
                      {'pvAlways': ('总是允许' if alabel else ''), 'pvDenyMsg': True, 'pvOptions': [{'kind': o.get('kind'), 'name': o.get('name')} for o in opts]})
        if c.busy:
            c.set_spin('等你确认…')
