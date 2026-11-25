"""第十四轮（开发线 I）端到端测试：真实 claude + 假网关（tests/fakegw.py）+ 测试服务，全部只在本机回环地址。
用法（在副本根目录）：
  CWB_E2E_PORT=47942 CWB_E2E_GW=18792 CWB_E2E_PROJ=/tmp/cwb-r14-i-proj CWB_E2E_STATE=/tmp/cwb-r14-i-exp/e2e-state.json \\
  CWB_E2E_GWLOG=/tmp/cwb-r14-i-gw/requests.jsonl .venv/bin/python -I tests/e2e_r14_i.py [all|cleanup]
覆盖：Fast 已去掉（11-11，fast_ok 恒为 False）后 prefs / 新对话 / 预热 / control 带 fast 都不注入 fastMode、不重启、请求与 CLI 状态不出现 Fast，请求头不变、
prefs 默认值 < 项目选项 < 显式选项（含预热）、Fast 前后 ~/.claude/settings.json 与 ~/.claude.json 的 Fast 相关键不变、
本机代理记录 CLI 发往网关以外的请求、ua-check / about / usage / restart-service。
只结束自己启动的进程（按 PID），测完按会话 ID 删掉自己建的测试会话记录。
"""
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_lib as L  # noqa: E402
from e2e_lib import api, check, say, st_load, st_save, wait_until, runner_of, screen, tx_text, gw_rows, alive, cmd_of  # noqa: E402

HOME = os.path.expanduser('~')
SETTINGS = os.path.join(HOME, '.claude', 'settings.json')
CLAUDE_JSON = os.path.join(HOME, '.claude.json')
FAST_BETA = 'fast-mode-2026-02-01'


# ---------------------------------------------------------------- 本机代理：记下 CLI 发往网关以外的请求（一律拒绝，不出网）
class Proxy:
    def __init__(self, allow_port):
        self.allow, self.log, self.lk = allow_port, [], threading.Lock()
        self.s = socket.socket()
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.bind(('127.0.0.1', 0))
        self.s.listen(64)
        self.port = self.s.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        try:
            buf = b''
            while b'\r\n\r\n' not in buf and len(buf) < 65536:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
            head, rest = buf.split(b'\r\n\r\n', 1)
            line = head.split(b'\r\n', 1)[0].decode('latin-1')
            meth, target = line.split(' ')[:2]
            with self.lk:
                self.log.append((meth, target))
            if meth == 'CONNECT':
                host, port = target.rsplit(':', 1)
                if host in ('127.0.0.1', 'localhost') and int(port) == self.allow:
                    u = socket.create_connection(('127.0.0.1', self.allow))
                    c.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                    if rest:
                        u.sendall(rest)
                    self._pipe(c, u)
                else:
                    c.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                return
            # 绝对地址形式的普通 http 请求：只转给本机假网关
            if target.startswith(f'http://127.0.0.1:{self.allow}/') or target.startswith(f'http://localhost:{self.allow}/'):
                path = '/' + target.split('/', 3)[3]
                lines = head.split(b'\r\n')
                lines[0] = f'{meth} {path} HTTP/1.1'.encode()
                lines = [x for x in lines if not x.lower().startswith((b'connection:', b'proxy-connection:', b'keep-alive:'))] + [b'Connection: close']
                u = socket.create_connection(('127.0.0.1', self.allow))
                u.sendall(b'\r\n'.join(lines) + b'\r\n\r\n' + rest)
                self._pipe(c, u)
            else:
                c.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        except Exception:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    @staticmethod
    def _pipe(a, b):
        socks = [a, b]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 60)
                if not r:
                    return
                for s in r:
                    d = s.recv(65536)
                    if not d:
                        return
                    (b if s is a else a).sendall(d)
        finally:
            b.close()

    def targets(self):
        with self.lk:
            return list(self.log)


# ---------------------------------------------------------------- 辅助
def sha(p):
    try:
        return hashlib.sha256(open(p, 'rb').read()).hexdigest()
    except OSError:
        return ''


def flat(d, pre=''):
    """键路径 → 值的哈希（只比较哪些键变了，不显示值）。projects 下只展开到项目一级。"""
    out = {}
    if isinstance(d, dict) and (pre.count('.') < 2):
        for k, v in d.items():
            out.update(flat(v, f'{pre}.{k}' if pre else k))
    else:
        out[pre] = hashlib.sha1(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
    return out


def conf_snap():
    st = json.load(open(SETTINGS))
    cj = json.load(open(CLAUDE_JSON))
    fk = {k: st.get(k) for k in st if 'fast' in k.lower() or 'penguin' in k.lower()}
    ck = {k: cj.get(k) for k in cj if any(w in k.lower() for w in ('fast', 'penguin', 'speed'))}
    return {'sha_settings': sha(SETTINGS), 'sha_claude': sha(CLAUDE_JSON), 'settings_fast': fk, 'claude_fast': ck, 'flat': flat(cj)}


def ensure_gw():
    try:
        socket.create_connection(('127.0.0.1', L.GW), timeout=1).close()
        return
    except OSError:
        pass
    logdir = os.path.dirname(L.GWLOG)
    os.makedirs(logdir, exist_ok=True)
    p = subprocess.Popen([os.path.join(L.ROOT, '.venv/bin/python'), '-I', os.path.join(L.ROOT, 'tests/fakegw.py'), str(L.GW), logdir],
                         stdin=subprocess.DEVNULL, stdout=open(os.path.join(logdir, 'gw.out'), 'ab'), stderr=subprocess.STDOUT, start_new_session=True)
    d = st_load()
    d['gw'] = p.pid
    st_save(d)
    for _ in range(50):
        try:
            socket.create_connection(('127.0.0.1', L.GW), timeout=1).close()
            break
        except OSError:
            time.sleep(0.1)
    say('假网关已启动', p.pid)


def cli_pid(sid):
    return (screen(sid).get('ptyd') or {}).get('pid')


def argv_of(sid):
    p = cli_pid(sid)
    return cmd_of(p) if p else ''


def new(opts=None):
    d = api('new', {'cwd': L.PROJ, 'opts': opts or {}})
    sid = d['sid']
    s = st_load()
    s.setdefault('sids', []).append(sid)
    st_save(s)

    def ready():
        st = runner_of(sid)
        if not st:
            return None
        dl = st.get('dialog')
        if dl and dl.get('kind') == 'trust':
            idx = next((i for i, o in enumerate(dl['options']) if 'yes' in o['label'].lower()), 1)
            api('dialog', {'sid': sid, 'option': idx})
            time.sleep(1.5)
            return None
        return st if st.get('ready') else None
    st = wait_until(ready, 40)
    say('新会话', sid[:8], '就绪' if st else '没就绪', json.dumps(d.get('opts'), ensure_ascii=False))
    return sid, d


def ask(sid, tag, wait=60):
    """发一句话，等这一轮结束，返回这一轮主对话请求（带 tools）的网关记录。"""
    n0 = len(gw_rows())
    api('send', {'sid': sid, 'text': f'{tag} 你好'})
    ok = wait_until(lambda: f'收到：{tag}' in tx_text(sid) and not (runner_of(sid) or {}).get('busy'), wait)
    time.sleep(0.5)
    rows = [r for r in gw_rows()[n0:] if r.get('kind') == 'messages']
    main = [r for r in rows if r.get('tools')]
    return bool(ok), (main[-1] if main else {}), gw_rows()[n0:]


def cli_fast_mode(sid):
    """CLI 交给状态行的 JSON 里的 fast_mode（工作台注入的状态行落盘在 .run/ctx/<代次>.json）；拿不到返回 None。"""
    gen = ((screen(sid).get('ptyd') or {}).get('meta') or {}).get('gen')
    p = os.path.join(L.RUN, 'ctx', f'{gen}.json') if gen else ''
    wait_until(lambda: os.path.exists(p), 10)
    try:
        return json.load(open(p)).get('fast_mode')
    except Exception:
        return None


def usage_speed(sid):
    vals = []
    p = L.transcript(sid)
    for l in open(p, encoding='utf-8') if p else []:
        if '"type":"assistant"' in l:
            try:
                d = json.loads(l)
            except Exception:
                continue
            vals.append(((d.get('message') or {}).get('usage') or {}).get('speed'))
    return vals


def official_pid():
    out = subprocess.run(['lsof', '-nP', '-iTCP:47821', '-sTCP:LISTEN', '-t'], capture_output=True, text=True).stdout.split()
    return out[0] if out else ''


# ---------------------------------------------------------------- 场景
def run_all():
    ensure_gw()
    proxy = Proxy(L.GW)
    os.environ['CWB_E2E_EXTRA_ENV'] = json.dumps({'HTTPS_PROXY': f'http://127.0.0.1:{proxy.port}', 'HTTP_PROXY': f'http://127.0.0.1:{proxy.port}',
                                                  'https_proxy': f'http://127.0.0.1:{proxy.port}', 'http_proxy': f'http://127.0.0.1:{proxy.port}'})
    L.stop_server()
    L.start_server()
    off0 = official_pid()
    c0 = conf_snap()
    g0 = len(gw_rows())
    os.makedirs(L.PROJ, exist_ok=True)

    # 1) 模型清单
    m = api('projects').get('models') or {}
    rec = [x['id'] for x in m.get('list', []) if x.get('group') == 'rec']
    more = [x['id'] for x in m.get('list', []) if x.get('group') == 'more']
    fastids = sorted(x['id'] for x in m.get('list', []) if x.get('fast'))
    check(rec == ['claude-opus-5-5', 'claude-fable-5-1', 'claude-sonnet-5-5', 'claude-haiku-5-5'] and 'opusplan' in more and m.get('defaultId'),
          f"模型清单：推荐 {rec}，更多 {len(more)} 个，默认 {m.get('defaultId')}（{m.get('default')}）")
    check(fastids == ['claude-opus-4-8', 'claude-opus-5', 'claude-opus-5-5'] and all(x.get('desc') is not None for x in m['list']),
          f'模型清单：支持 Fast 的 {fastids}')
    plan = next((x for x in m['list'] if x['id'] == 'opusplan'), {})
    check(plan.get('label') == 'Opus 规划 + Sonnet 执行', f"opusplan 标签：{plan.get('label')}")

    # 2) prefs 默认值
    p0 = api('prefs').get('prefs') or {}
    check(set(p0) >= {'defaults', 'defaultCwd', 'extraModels'} and set(p0['defaults']) >= {'model', 'effort', 'mode', 'fast'}, f'prefs 读取：{p0}')
    p1 = api('prefs', {'set': {'defaults': {'model': 'claude-opus-5-5', 'effort': 'low', 'fast': True}, 'extraModels': ['claude-test-x-1']}}).get('prefs')
    check(p1['defaults'] == {'model': 'claude-opus-5-5', 'effort': 'low', 'mode': None, 'fast': True} and p1['extraModels'] == ['claude-test-x-1'],
          f'prefs 保存：{p1}')
    p2 = api('prefs', {'set': {'defaults': {'mode': 'default'}}}).get('prefs')
    check(p2['defaults']['model'] == 'claude-opus-5-5' and p2['defaults']['fast'] is True, 'prefs 部分字段合并：只改 mode 不影响其他默认值')
    bad = api('prefs', {'set': {'defaults': {'effort': 'turbo'}}})
    check(bad.get('_http') == 400, f"prefs 校验：非法思考强度返回 400（{bad.get('error')}）")
    m2 = api('projects').get('models') or {}
    check(m2.get('defaultId') == 'claude-opus-5-5' and any(x['id'] == 'claude-test-x-1' and x['group'] == 'more' for x in m2['list']),
          '模型清单：默认取 prefs.defaults.model，extraModels 进“更多”')

    # 3) 新对话：不带选项 → 用 prefs 默认值（Opus 5.5 + low + Fast）
    x0 = len(proxy.targets())
    sa, da = new()
    av = argv_of(sa)
    check(da['opts'].get('fast') is True and da['opts'].get('model') == 'claude-opus-5-5' and da['opts'].get('effort') == 'low',
          f"prefs 默认值生效：新对话 opts={da['opts']}")
    # 11-11 Fast 功能去掉（fast_ok 恒为 False）：prefs 里留着 fast=true 也不注入 fastMode，请求、记录、CLI 状态都不出现 Fast
    check('--model claude-opus-5-5' in av and '--effort low' in av and 'fastMode' not in av, 'Fast 已去掉：CLI 启动参数有 --model、--effort，不注入 fastMode')
    ok, r, rows_a = ask(sa, 'R14IA')
    check(ok and r.get('speed') is None and FAST_BETA not in (r.get('beta') or '') and r.get('effort') == 'low',
          f"Fast 已去掉：prefs 开着 fast 也不发 speed（{r.get('speed')}），beta 不含 {FAST_BETA}，effort={r.get('effort')}")
    keys_fast = r.get('keys') or []
    check(usage_speed(sa) and 'fast' not in usage_speed(sa), f'Fast 已去掉：记录里 assistant 的 message.usage.speed 没有 fast：{usage_speed(sa)}')
    check(cli_fast_mode(sa) is False, f'Fast 已去掉：CLI 自己报的 fast_mode（状态行数据）为 {cli_fast_mode(sa)!r}，终端界面不出现 Fast')

    x1 = len(proxy.targets())
    # 4) 显式选项覆盖默认值：fast=False、effort=high
    sb, db = new({'fast': False, 'effort': 'high'})
    avb = argv_of(sb)
    ok, rb, rows_b = ask(sb, 'R14IB')
    x2 = len(proxy.targets())
    check(not db['opts'].get('fast') and db['opts'].get('effort') == 'high' and 'fastMode' not in avb and '--effort high' in avb,
          f"显式选项覆盖默认值：opts={db['opts']}，启动参数无 fastMode")
    check(ok and rb.get('speed') is None and FAST_BETA not in (rb.get('beta') or '') and rb.get('effort') == 'high',
          f"不开 Fast：请求体没有 speed（{rb.get('speed')}），beta 不含 {FAST_BETA}")
    extra = sorted(set(keys_fast) - set(rb.get('keys') or []))
    check(extra == [], f'(a) Fast 已去掉：prefs 开 fast 与不开相比请求体顶层没有多出的键：{extra}；service_tier={r.get("service_tier")}')
    pa = sorted({x.get('method', '') + ' ' + x['path'].split('?')[0] for x in rows_a})
    pb = sorted({x.get('method', '') + ' ' + x['path'].split('?')[0] for x in rows_b})
    check(pa == pb, f'(c) 一轮里经网关的请求路径：开 Fast {pa}，不开 {pb}')
    tg = proxy.targets()
    qa = sorted({f'{m_} {t}' for m_, t in tg[x0:x1] if f':{L.GW}' not in t})
    qb = sorted({f'{m_} {t}' for m_, t in tg[x1:x2] if f':{L.GW}' not in t})
    check(qa == qb, f'(c) 启动加一轮里网关以外的请求（经本机代理）：开 Fast {qa or "无"}，不开 {qb or "无"}')

    # 5) 项目选项介于默认值和显式选项之间
    api('project-opts', {'cwd': L.PROJ, 'opts': {'effort': 'medium'}})
    sc_, dc = new()
    check(dc['opts'].get('effort') == 'medium' and dc['opts'].get('fast') is True and dc['opts'].get('model') == 'claude-opus-5-5',
          f"合并顺序 defaults < 项目选项：opts={dc['opts']}")
    sd, dd = new({'effort': 'max'})
    check(dd['opts'].get('effort') == 'max', f"合并顺序 项目选项 < 显式选项：opts={dd['opts']}")
    api('project-opts', {'cwd': L.PROJ, 'opts': {}})
    for s in (sb, sc_, sd):
        api('stop', {'sid': s})
    wait_until(lambda: not any(runner_of(s) for s in (sb, sc_, sd)), 20)

    # 6) 预热也用合并后的选项
    api('prewarm', {'cwd': L.PROJ})
    time.sleep(4)
    t_call = time.time()
    se, de = new()
    meta = (screen(se).get('ptyd') or {}).get('meta') or {}
    check(float(meta.get('started') or t_call) < t_call - 1 and 'fastMode' not in argv_of(se) and de['opts'].get('fast') is True,
          f"预热：新对话直接用了预热的 CLI（提前 {t_call - float(meta.get('started') or t_call):.1f}s 启动），不注入 fastMode")
    api('stop', {'sid': se})

    # 7) control 切换 fast：Fast 已去掉，注入不变，不重启
    pid1 = cli_pid(sa)
    d = api('control', {'sid': sa, 'fast': False})
    check(d.get('opts', {}).get('fast') is False, f"control 关 fast：返回 opts={d.get('opts')}")
    time.sleep(3)
    pid2 = cli_pid(sa)
    check(bool(pid2) and pid2 == pid1 and alive(pid1) and 'fastMode' not in argv_of(sa),
          f'Fast 已去掉：control 切换 fast 不重启 CLI（{pid1} → {pid2}），启动参数没有 fastMode')
    wait_until(lambda: (runner_of(sa) or {}).get('ready'), 30)
    ok, r2, _ = ask(sa, 'R14IC')
    check(ok and r2.get('speed') is None, f"关 Fast 后：请求体 speed={r2.get('speed')}")
    # 回复中打开：等这一轮结束再重启
    api('send', {'sid': sa, 'text': 'SLOWTEST:6 慢一点'})
    wait_until(lambda: (runner_of(sa) or {}).get('busy'), 20)
    d = api('control', {'sid': sa, 'fast': True})
    time.sleep(1.5)
    check(cli_pid(sa) == pid2 and (runner_of(sa) or {}).get('busy'), '回复中切换 fast：不打断，这一轮继续')
    done = wait_until(lambda: '第40段' in tx_text(sa) and not (runner_of(sa) or {}).get('busy'), 40)
    time.sleep(3)
    pid3 = cli_pid(sa)
    check(bool(done) and pid3 == pid2 and alive(pid2) and 'fastMode' not in argv_of(sa),
          f'Fast 已去掉：这一轮结束后也不重启（{pid2} → {pid3}），不注入 fastMode')
    wait_until(lambda: (runner_of(sa) or {}).get('ready'), 30)
    ok, r3, _ = ask(sa, 'R14ID')
    check(ok and r3.get('speed') is None and cli_fast_mode(sa) is False, f"Fast 已去掉：再打开 fast 后请求体仍没有 speed（{r3.get('speed')}），CLI fast_mode={cli_fast_mode(sa)!r}")

    # 8) 模型不支持 Fast：不注入；切换 fast 不重启
    sf, df = new({'model': 'claude-sonnet-5-5', 'fast': True})
    avf = argv_of(sf)
    ok, rf, _ = ask(sf, 'R14IF')
    check('fastMode' not in avf and ok and rf.get('speed') is None, f"Sonnet 5.5 + fast：不注入 fastMode，请求体 speed={rf.get('speed')}")
    pf = cli_pid(sf)
    api('control', {'sid': sf, 'fast': False})
    time.sleep(3)
    check(cli_pid(sf) == pf and alive(pf), 'Sonnet 5.5 上切换 fast：注入不变，不重启')
    api('stop', {'sid': sf})

    # 9) 请求头不变；网关以外的请求
    L.ua_check(g0)
    tg = proxy.targets()
    local = ('127.0.0.1', 'localhost', 'http://127.0.0.1', 'http://localhost')
    outside = sorted({f'{m_} {t}' for m_, t in tg if not t.startswith(local)})
    other = sorted({f'{m_} {t}' for m_, t in tg if t.startswith(local) and f':{L.GW}' not in t})
    via = len([1 for m_, t in tg if f':{L.GW}' in t])
    say(f'(c) 本机代理：经代理到假网关的请求 {via} 个；本机其他目标 {other or "无"}；本机以外 {outside or "无"}（代理一律拒绝，不出网）')
    # 本机以外的请求来自用户自己的状态栏脚本等（不开 Fast 时同样有）：要求 Fast 没有带来新的目标
    check(via > 0, f'(c) 代理确实被 CLI 使用（{via} 个经代理到假网关）；本机以外的目标一律被代理拒绝（Fast 已去掉，不再比较开不开 Fast）')

    # 10) 配置文件：Fast 相关键前后不变
    c1 = conf_snap()
    changed = sorted(k for k in set(c0['flat']) | set(c1['flat']) if c0['flat'].get(k) != c1['flat'].get(k))
    check(c0['sha_settings'] == c1['sha_settings'] and c0['settings_fast'] == c1['settings_fast'] == {},
          f"(d) ~/.claude/settings.json 前后 sha256 相同（{c1['sha_settings'][:12]}），没有 Fast 相关键")
    check(c0['claude_fast'] == c1['claude_fast'], f"(d) ~/.claude.json 的 Fast 相关键前后一致：{c1['claude_fast']}；sha256 "
          f"{'不变' if c0['sha_claude'] == c1['sha_claude'] else '变了'}，变化的键：{changed[:12]}")

    # 11) about、ua-check
    ab = api('about')
    gw = ab.get('gateway') or {}
    check(ab.get('app', {}).get('round') == 15 and ab.get('cli', {}).get('version') and ab.get('port') == L.PORT and gw.get('host') == f'127.0.0.1:{L.GW}'
          and set(ab.get('logs') or {}) == {'server', 'ptyd', 'restart'} and isinstance(ab.get('backups'), list),
          f"about：版本 {ab.get('app')}，CLI {ab.get('cli')}，网关 {gw.get('host')}，令牌 {gw.get('tokenMasked')}")
    tm = gw.get('tokenMasked') or ''
    check(not tm or (tm.endswith('•' * 10) and len(tm.rstrip('•')) <= 4), '令牌打码：只露前 4 位')
    ud = os.path.join(HOME, '.claude', 'projects', '-private-tmp-cwb-uacheck')

    def sess_files():  # 会话记录：<sid>.jsonl 和 <sid>/ 目录（memory 等 CLI 自己的目录不算）
        return {x for x in (os.listdir(ud) if os.path.isdir(ud) else []) if re.fullmatch(r'[0-9a-f-]{36}(\.jsonl)?', x)}
    before = sess_files()
    t0 = time.time()
    u = api('ua-check', timeout=90)
    after = sess_files()
    check(u.get('ok') and u.get('xapp') == 'cli' and u.get('ms', 99999) < 60000 and time.time() - t0 < 60,
          f"ua-check：ok={u.get('ok')} ua={u.get('ua')} x-app={u.get('xapp')} 用时 {u.get('ms')}ms {u.get('error') or ''}")
    check(after <= before, f'ua-check：这次的会话记录已删除（目录里新增 {sorted(after - before)}）')
    check((api('about').get('uaCheck') or {}).get('at') == u.get('at'), 'about 里带上次自检结果')

    # 12) 用量接口
    wait_until(lambda: not api('usage-summary').get('scanning'), 60, 0.5)
    sm = api('usage-summary')
    check(set(sm) == {'scanning', 'today'} and set(sm['today']) == {'requests', 'tokens', 'cost'}, f'usage-summary：{sm}')
    mine = set(st_load().get('sids', []))
    for rg in ('today', '7d', '30d'):
        t0 = time.time()
        us = api('usage', {'range': rg})
        dt = time.time() - t0
        nb = {'today': 24, '7d': 7, '30d': 30}[rg]
        check(len(us.get('buckets', [])) == nb and len(us.get('heat', [])) == 371 and us.get('unit') == ('hour' if rg == 'today' else 'day')
              and set(us.get('kpi', {})) >= {'requests', 'input', 'output', 'cacheRead', 'cacheWrite', 'tokens', 'cost', 'cacheHit', 'speed'}
              and len(us.get('recent', [])) <= 100 and len(us.get('sessions', [])) <= 10,
              f"usage {rg}：{us['kpi']['requests']} 次请求、{us['kpi']['tokens']} token、${us['kpi']['cost']:.2f}，接口 {dt * 1000:.0f}ms")
        check(not any(x['sid'] in mine for x in us.get('recent', [])) and not any(x['sid'] in mine for x in us.get('sessions', [])),
              f'usage {rg}：测试副本的会话（钩子路径不是 /claude-workbench/hook.py）不计入')

    # 13) restart-service：只重启本测试服务，正式服务不动
    srv = st_load().get('server')
    rs = api('restart-service')
    gone = wait_until(lambda: not alive(srv), 15, 0.1)
    check(rs.get('ok') and bool(gone), f'restart-service：测试服务 {srv} 已结束')
    L.start_server()  # 代替 App 的看护拉起
    lg = open(os.path.join(L.RUN, 'restart_when_idle.log'), encoding='utf-8').read().splitlines()[-3:]
    check(any('立即结束旧服务' in x and str(srv) in x for x in lg), '部署脚本日志：' + ' | '.join(lg))
    check(official_pid() == off0, f'正式服务（47821）进程没变：{off0 or "未运行"}')
    st = wait_until(lambda: runner_of(sa), 15)
    check(bool(st) and cli_pid(sa) == pid3, f'新服务接管了还在跑的 CLI（{pid3}）')

    # 收尾：prefs 清空
    api('prefs', {'set': {'defaults': {'model': None, 'effort': None, 'mode': None, 'fast': False}, 'extraModels': []}})
    proxy.s.close()


def cleanup():
    """结束自己建的 CLI、测试服务、ptyd、假网关（都按 PID），按会话 ID 删掉测试会话记录。"""
    d = st_load()
    sids = d.get('sids', [])
    if L.ping().startswith('ok'):
        for s in sids:
            try:
                api('stop', {'sid': s})
            except Exception:
                pass
        time.sleep(3)
    L.stop_server()
    pp = L.kill_ptyd(signal.SIGTERM)
    if pp:
        wait_until(lambda: not alive(pp), 10)
        say('测试 ptyd 已结束', pp)
    gw = d.get('gw')
    if gw and alive(gw) and 'fakegw.py' in cmd_of(gw):
        os.kill(gw, signal.SIGTERM)
        say('假网关已结束', gw)
    n = 0
    trash = os.path.join(HOME, '.Trash', 'cwb-r14-i-' + time.strftime('%H%M%S'))
    for s in sids:  # 修复线：测试会话记录移到废纸篓，不直接删
        for p in (os.path.join(L.PROJ_DIR, s + '.jsonl'), os.path.join(L.PROJ_DIR, s),
                  os.path.join(HOME, '.claude', 'session-env', s), os.path.join(HOME, '.claude', 'file-history', s)):
            if os.path.lexists(p):
                os.makedirs(trash, exist_ok=True)
                shutil.move(p, os.path.join(trash, os.path.basename(os.path.dirname(p)) + '__' + os.path.basename(p)))
                n += 1
    say('测试会话记录已移到废纸篓', len(sids), '个会话，', n, '项')
    d['sids'] = []
    st_save(d)


if __name__ == '__main__':
    sc = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if sc == 'cleanup':
        cleanup()
    else:
        run_all()
        say('全部通过' if not L.FAIL else f'{len(L.FAIL)} 项失败：' + '；'.join(L.FAIL))
