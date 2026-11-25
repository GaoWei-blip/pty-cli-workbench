"""第十六轮 R 端到端测试：远程访问（Tailscale）。不依赖真实 Tailscale、不连外网：
- 测试服务 47941（CWB_TITLE_DELAY=99999）+ 假网关 18821；PATH 最前面放假的 tailscale（tests/fake_tailscale.py，状态目录 FAKE_TS_DIR）；
- A 开关：读状态、开放时不覆盖用户已有的 443 规则、命令拼装、从不调用 funnel；
- B 鉴权：直接向测试服务发带 ts.net Host 和身份头的请求（就是 tailscale serve 转发过来的样子）：
  无凭据、主令牌经代理、错 Host、过期配对码、重复使用、伪造身份头、撤销后、远程不能用的接口、/hook/；
- C 撤销：远程设备的事件流连着时在电脑上撤销，连接立即断开；
- D 远程控制 Claude：用设备凭据经远程入口新建对话、发消息，假网关收到的请求头仍是 claude-cli/<版本> (external, cli)；
- E 手机页面：模拟 tailscale serve 的 HTTPS 代理（tests/fake_ts_proxy.py）+ 无头浏览器 390×844：扫码链接配对、DOM 断言、截图；
  再用桌面宽度 1440×900 打开本机页面，确认布局没被影响；
- F 空闲退出：远程开着时（CWB_IDLE_EXIT=5）超过一分钟没有窗口也不退出；关掉远程后照常退出；
- G 关闭：只撤自己那条规则。
只结束自己起的进程（按 PID 并核对命令行），测试会话记录移到废纸篓。
运行：.venv/bin/python -I tests/e2e_r16_R.py [all|nolong|cleanup]
"""
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import quote

os.environ.setdefault('CWB_E2E_PORT', '47941')
os.environ.setdefault('CWB_E2E_GW', '18821')
os.environ.setdefault('CWB_E2E_PROJ', '/tmp/cwb-r16r-proj')
os.environ.setdefault('CWB_E2E_STATE', '/tmp/cwb-r16r-exp/e2e-state.json')
os.environ.setdefault('CWB_E2E_GWLOG', '/tmp/cwb-r16r-gw/requests.jsonl')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_lib as E  # noqa: E402

PY = os.path.join(E.ROOT, '.venv/bin/python')
EXP = '/tmp/cwb-r16r-exp'
TSDIR = os.path.join(EXP, 'ts')
SHOTS = os.path.join(EXP, 'shots')
HOST = 'cwb-mac.tailfake0.ts.net'
LOGIN = 'tester@example.com'
SELF_IPS = ['100.101.102.103', 'fd7a:115c:a1e0::1']
HOME = os.path.expanduser('~')
SESS_M, SESS_D = 'r16r-m', 'r16r-d'
UA_IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
say, check = E.say, E.check


# ---------------------------------------------------------------- 准备
def fake_ts():
    os.makedirs(os.path.join(TSDIR, 'bin'), exist_ok=True)
    os.makedirs(os.path.join(TSDIR, 'state'), exist_ok=True)
    w = os.path.join(TSDIR, 'bin', 'tailscale')
    with open(w, 'w') as f:
        f.write('#!/bin/sh\nexec "%s" -I "%s" "$@"\n' % (PY, os.path.join(E.ROOT, 'tests', 'fake_tailscale.py')))
    os.chmod(w, 0o755)
    st = {'Version': '1.90.2', 'BackendState': 'Running', 'TailscaleIPs': SELF_IPS,
          'Self': {'DNSName': HOST + '.', 'UserID': 1234567, 'TailscaleIPs': SELF_IPS, 'HostName': 'cwb-mac'},
          'User': {'1234567': {'ID': 1234567, 'LoginName': LOGIN, 'DisplayName': '测试者'}},
          'CertDomains': [HOST], 'CurrentTailnet': {'Name': 'tailfake0', 'MagicDNSSuffix': 'tailfake0.ts.net', 'MagicDNSEnabled': True}}
    json.dump(st, open(os.path.join(TSDIR, 'state', 'status.json'), 'w'))
    # 用户自己已有的规则：443 上 / 指向 3000
    json.dump({'TCP': {'443': {'HTTPS': True}}, 'Web': {HOST + ':443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:3000'}}}}},
              open(os.path.join(TSDIR, 'state', 'serve.json'), 'w'))
    for n in ('calls.jsonl', 'need_https'):
        try:
            os.remove(os.path.join(TSDIR, 'state', n))
        except FileNotFoundError:
            pass


def ts_calls():
    try:
        return [json.loads(x) for x in open(os.path.join(TSDIR, 'state', 'calls.jsonl'))]
    except FileNotFoundError:
        return []


def ts_serve():
    return json.load(open(os.path.join(TSDIR, 'state', 'serve.json')))


def server_env(**kw):
    return dict(PATH=os.path.join(TSDIR, 'bin') + ':' + os.environ.get('PATH', '/usr/bin:/bin'), FAKE_TS_DIR=os.path.join(TSDIR, 'state'),
                CWB_TITLE_DELAY='99999', **kw)


def start_gw():
    os.makedirs('/tmp/cwb-r16r-gw', exist_ok=True)
    lg = open(os.path.join(EXP, 'gw.log'), 'ab')
    p = subprocess.Popen([PY, '-I', os.path.join(E.ROOT, 'tests/fakegw.py'), str(E.GW), '/tmp/cwb-r16r-gw'], cwd=E.ROOT,
                         stdin=subprocess.DEVNULL, stdout=lg, stderr=lg, start_new_session=True)
    d = E.st_load()
    d['gw'] = p.pid
    E.st_save(d)
    return p.pid


def free_port(pref):
    for p in [pref] + [0]:
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()


# ---------------------------------------------------------------- 远程请求（tailscale serve 转发过来的样子）
def rreq(method, path, body=None, cookie='', login=LOGIN, ip='100.64.0.9', host=HOST, extra=None, timeout=60):
    c = http.client.HTTPConnection('127.0.0.1', E.PORT, timeout=timeout)
    hd = {'Host': host, 'X-Forwarded-Host': host, 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': ip, 'Tailscale-User-Login': login}
    if method == 'POST':
        hd.update({'Origin': 'https://' + host, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json'})
    if cookie:
        hd['Cookie'] = cookie
    hd.update(extra or {})
    data = None
    if method == 'POST':
        data = body.encode() if isinstance(body, str) else json.dumps(body or {}).encode()
        hd['Content-Length'] = str(len(data))
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in hd.items():
        if v is not None:
            c.putheader(k, v)
    c.endheaders(data)
    r = c.getresponse()
    b = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, b


def jb(b):
    try:
        return json.loads(b or b'{}')
    except ValueError:
        return {}


def make_rapi(cookie):
    def rapi(p, body=None, timeout=60):
        st, _, b = rreq('POST', '/api/' + p, body or {}, cookie=cookie, timeout=timeout)
        d = jb(b)
        if st != 200:
            d = dict(d if isinstance(d, dict) else {}, _http=st)
        return d
    return rapi


def pair(name='测试手机', login=LOGIN):
    url = E.api('remote-pair')['url']
    code = url.split('c=', 1)[1]
    form = 'c=%s&name=%s' % (code, name)
    st, hs, b = rreq('POST', '/pair', form, login=login, extra={'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': UA_IPHONE})
    ck = hs.get('set-cookie', '')
    return st, ck, ck.split(';')[0], code


# ---------------------------------------------------------------- A 开关
def step_a():
    r = E.api('remote-status')
    ts = r.get('ts') or {}
    check(ts.get('installed') and ts.get('loggedIn') and ts.get('dns') == HOST and ts.get('login') == LOGIN and ts.get('https'),
          '读到 Tailscale：已登录、MagicDNS 名、账号、HTTPS 证书（%s）' % {k: ts.get(k) for k in ('dns', 'login', 'https')})
    check(not (r.get('remote') or {}).get('enabled'), '默认没有开放')
    r = E.api('remote-enable')
    rm = r.get('remote') or {}
    check(r.get('ok') and rm.get('enabled') and rm.get('port') == 8443 and rm.get('url') == 'https://%s:8443/' % HOST,
          '开放：443 已有用户规则，改用 8443（%s）' % rm.get('url'))
    calls = ts_calls()
    check(['serve', '--bg', '--https=8443', 'http://127.0.0.1:%d' % E.PORT] in calls, '命令：tailscale serve --bg --https=8443 http://127.0.0.1:47941')
    sc = ts_serve()
    check(sc['Web'][HOST + ':443']['Handlers']['/']['Proxy'] == 'http://127.0.0.1:3000', '用户原有的 443 规则没动')
    check(not any(c and c[0] == 'funnel' for c in calls), '没有调用过 tailscale funnel')


# ---------------------------------------------------------------- B 鉴权
def step_b():
    tok = E.token()
    st, _, b = rreq('GET', '/', extra={'Accept': 'text/html'})
    check(st == 401 and '还没有配对' in b.decode() and 'id="pc"' in b.decode(), '无凭据打开首页：401 配对说明页，可以粘贴配对链接（%s）' % st)
    st, _, b = rreq('POST', '/api/projects', {})
    check(st == 401 and jb(b).get('remote') == 'unpaired', '无凭据调接口：401')
    st, _, _ = rreq('POST', '/api/projects', {}, extra={'X-Token': tok})
    check(st == 401, 'ts.net 来的请求带主令牌：不认，401')
    st, _, _ = rreq('GET', '/events?t=' + tok)
    check(st == 401, 'ts.net 来的事件流带 ?t=主令牌：不认，401')
    # 主令牌经代理不能用：Host 写成 127.0.0.1，但带着 tailscale serve 加的头
    st, _, _ = rreq('POST', '/api/projects', {}, host='127.0.0.1:%d' % E.PORT, extra={'X-Token': tok, 'Origin': None, 'Sec-Fetch-Site': None})
    check(st == 403, '本机 Host + 转发头 + 主令牌：403（经 Tailscale 来的不能冒充本机）')
    st, _, _ = rreq('GET', '/events?t=' + tok, host='localhost:%d' % E.PORT)
    check(st == 403, 'localhost Host + 转发头 + ?t=主令牌：403')
    check(E.api('remote-me') == {'remote': False}, '本机正常请求：不受影响')
    for h in ('evil.example.com', 'other.tailfake0.ts.net', HOST + '.evil.com'):
        st, _, _ = rreq('GET', '/', host=h)
        check(st == 403, '错 Host（%s）：403' % h)
    # 配对码
    code = E.api('remote-pair')['url'].split('c=', 1)[1]
    st, _, b = rreq('GET', '/pair?c=' + code)
    check(st == 200 and '确认配对' in b.decode(), '打开配对链接：确认页（不消耗）')
    st, hs, b = rreq('POST', '/pair', 'c=' + code, extra={'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'https://evil.example.com'})
    check(st == 403, '别的网站代发配对确认：403')
    st, hs, b = rreq('POST', '/pair', 'c=' + code, login='eve@example.com', extra={'Content-Type': 'application/x-www-form-urlencoded'})
    check(st == 403, '别的 Tailscale 账号来配对：403')
    st, hs, b = rreq('POST', '/pair', 'c=' + code + '&name=甲', extra={'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': UA_IPHONE})
    ck = hs.get('set-cookie', '')
    check(st == 303 and hs.get('location') == '/' and 'HttpOnly' in ck and 'Secure' in ck and 'SameSite=Strict' in ck, '确认配对：303 + HttpOnly/Secure/SameSite=Strict 凭据')
    cookie = ck.split(';')[0]
    st, _, b = rreq('POST', '/pair', 'c=' + code, extra={'Content-Type': 'application/x-www-form-urlencoded'})
    check(st == 410 and '用过' in b.decode(), '重复使用配对码：410')
    d = E.st_load()
    d['cookie1'] = cookie
    E.st_save(d)
    st, _, b = rreq('GET', '/', cookie=cookie)
    check(st == 200 and b'<div id="app">' in b and tok.encode() not in b, '有凭据打开首页：200，页面里没有主令牌')
    # 复核修复：远程响应禁止被嵌入；/file（PDF 预览的同源 iframe）只许同源；本机响应不加
    fh = lambda hs: (hs.get('x-frame-options'), hs.get('content-security-policy'))
    check(fh(hs) == ('DENY', "frame-ancestors 'none'"), '远程首页禁止被嵌入（%s）' % (fh(hs),))
    st2, hs2, _ = rreq('GET', '/', extra={'Accept': 'text/html'})
    st3, hs3, _ = rreq('POST', '/api/projects', {}, cookie=cookie)
    check(fh(hs2) == fh(hs3) == ('DENY', "frame-ancestors 'none'"), '远程的 401 说明页、接口响应也禁止被嵌入')
    png = os.path.join(E.PROJ, 'pic.png')
    shutil.copy(os.path.join(E.ROOT, 'static', 'icon-192.png'), png)
    st4, hs4, b4 = rreq('GET', '/file?p=' + quote(png) + '&cwd=&t=', cookie=cookie)
    check(st4 == 200 and b4[:4] == b'\x89PNG' and fh(hs4) == ('SAMEORIGIN', "frame-ancestors 'self'"), '远程 /file 只许同源嵌入（%s %s）' % (st4, fh(hs4)))
    with E.OP.open('http://127.0.0.1:%d/?t=%s' % (E.PORT, tok), timeout=10) as r:
        lh = {k.lower(): v for k, v in r.getheaders()}
    check('x-frame-options' not in lh and 'content-security-policy' not in lh, '本机首页（原生 App 打开的）不加禁止嵌入的头')
    st, _, b = rreq('POST', '/api/remote-me', {}, cookie=cookie)
    check(st == 200 and jb(b).get('name') == '甲', 'remote-me 认出设备“甲”')
    st, _, b = rreq('POST', '/api/projects', {}, cookie=cookie)
    check(st == 200 and 'projects' in jb(b) and tok.encode() not in b, '有凭据调 projects：正常，响应里没有主令牌')
    st, hs, b = rreq('GET', '/static/app.js', cookie=cookie)
    check(st == 200 and len(b) > 10000, '有凭据取界面脚本')
    # 伪造身份头、没有身份头、Funnel
    st, _, b = rreq('POST', '/api/projects', {}, cookie=cookie, login='eve@example.com')
    check(st == 403, '凭据对、身份头换成别的账号：403')
    st, _, b = rreq('POST', '/api/projects', {}, cookie=cookie, login=None)
    check(st == 403, '没有身份头（带标签、共享来的设备）：403')
    st, _, b = rreq('POST', '/api/projects', {}, cookie=cookie, extra={'Tailscale-Funnel-Request': '?1'})
    check(st == 403, '公网（Funnel）来的请求：403')
    st, _, b = rreq('POST', '/api/projects', {}, cookie=cookie, extra={'Origin': 'https://evil.example.com'})
    check(st == 403, 'Origin 不对：403')
    # 过期配对码：等不了 10 分钟，用测试服务的接口作废不了“过期”，这里生成后在单元测试里覆盖；端到端只测“生成新码后旧码失效”
    old = E.api('remote-pair')['url'].split('c=', 1)[1]
    E.api('remote-pair')
    st, _, b = rreq('GET', '/pair?c=' + old)
    check(st == 410, '重新生成后，旧配对码失效：410')
    E.api('remote-pair-cancel')
    # 远程不能用的接口
    for n in ('reveal', 'open-file', 'open-url', 'pick-files', 'add-folder', 'restart-service', 'remote-enable', 'remote-status', 'remote-pair'):
        st, _, b = rreq('POST', '/api/' + n, {'path': '/tmp', 'url': 'https://example.com'}, cookie=cookie)
        check(st == 403, '远程不能用 %s：403（%s）' % (n, jb(b).get('error', '')))
    st, _, _ = rreq('POST', '/hook/busy', {}, cookie=cookie, extra={'X-Token': tok})
    check(st == 403, '远程调 /hook/：403')
    # 下载
    fp = os.path.join(E.PROJ, '说明 1.txt')
    open(fp, 'w', encoding='utf-8').write('远程下载测试')
    st, hs, b = rreq('GET', '/r/dl?p=' + quote(fp), cookie=cookie)
    check(st == 200 and b.decode() == '远程下载测试' and 'attachment' in hs.get('content-disposition', ''), '远程下载文件（%s）' % st)


# ---------------------------------------------------------------- C 撤销断开事件流
def sse_open(cookie):
    s = socket.create_connection(('127.0.0.1', E.PORT), timeout=10)
    s.sendall(('GET /events HTTP/1.1\r\nHost: %s\r\nX-Forwarded-Host: %s\r\nX-Forwarded-For: 100.64.0.9\r\nTailscale-User-Login: %s\r\n'
               'Cookie: %s\r\nAccept: text/event-stream\r\n\r\n' % (HOST, HOST, LOGIN, cookie)).encode())
    buf = b''
    end = time.time() + 5
    while b': hi' not in buf and time.time() < end:
        x = s.recv(4096)
        if not x:
            break
        buf += x
        if b'\r\n\r\n' in buf and not buf.startswith(b'HTTP/1.1 200'):
            break  # 不是 200（比如 401）：响应头到了就够了
    return s, buf


def step_c():
    st, _, cookie, _ = pair('乙')
    did = cookie.split('=', 1)[1].split('.', 1)[0]
    s, buf = sse_open(cookie)
    check(b'200 OK' in buf.split(b'\r\n')[0] and b': hi' in buf, '远程设备的事件流连上')
    devs = E.api('remote-status').get('devices') or []
    check(any(d['id'] == did and d.get('online') for d in devs), '设备列表显示“在线”')
    t0 = time.time()
    E.api('remote-revoke', {'id': did})
    s.settimeout(5)
    got = None
    try:
        while True:
            x = s.recv(4096)
            if not x:
                got = b''
                break
    except Exception as e:
        got = repr(e)
    dt = time.time() - t0
    check(got == b'' and dt < 2, '撤销后事件流立即断开（%.2f 秒）' % dt)
    s.close()
    st, _, _ = rreq('POST', '/api/remote-me', {}, cookie=cookie)
    check(st == 401, '撤销后再请求：401')
    s2, buf2 = sse_open(cookie)
    check(b'401' in buf2.split(b'\r\n')[0], '撤销后重连事件流：401')
    s2.close()


# ---------------------------------------------------------------- D 经远程入口控制 Claude
def step_d():
    cookie = E.st_load().get('cookie1')
    n0 = len(E.gw_rows())
    rapi = make_rapi(cookie)
    local = E.api
    E.api = rapi  # e2e_lib 的建对话、等就绪、查状态全部改走远程入口
    try:
        sid = E.new_session()
        r = rapi('send', {'sid': sid, 'text': '回复 ok'})
        check(not r.get('_http'), '远程发消息（%s）' % (r.get('error') or 'ok'))
        st = E.wait_until(lambda: (lambda s: s if s and not s.get('busy') and not s.get('turn') and E.transcript(sid) and 'assistant' in E.tx_text(sid) else None)(E.runner_of(sid)), 90)
        check(bool(st), '这一轮回复完成，记录里有回复')
        o = rapi('open', {'sid': sid})
        check(not o.get('_http') and isinstance(o.get('items'), list) and len(o['items']) >= 2, '远程打开对话拿到时间线（%d 条）' % len(o.get('items') or []))
    finally:
        E.api = local
    E.ua_check(n0)
    d = E.st_load()
    d['d_sid'], d['d_ok'] = sid, bool(st)
    E.st_save(d)


# ---------------------------------------------------------------- E 手机页面（无头浏览器）
LAUNCHED = set()


def ab(sess, *a, timeout=60, opts=()):
    """启动参数（opts）只在这个会话第一次启动浏览器时带上；之后再带会被忽略并打印警告。"""
    if a[:1] == ('close',):
        LAUNCHED.discard(sess)
        opts = ()
    elif sess in LAUNCHED:
        opts = ()
    else:
        LAUNCHED.add(sess)
    p = subprocess.run(['agent-browser', '--session', sess] + list(opts) + list(a), capture_output=True, text=True, timeout=timeout)
    out = '\n'.join(l for l in ((p.stdout or '') + (p.stderr or '')).splitlines() if not l.startswith('⚠'))
    return p.returncode, out.strip()


def ev(sess, js, opts=()):
    rc, out = ab(sess, 'eval', 'Promise.resolve((() => { ' + js + ' })()).then(v => JSON.stringify(v))', opts=opts)
    try:
        v = json.loads(out.splitlines()[-1] if out else 'null')
        return json.loads(v) if isinstance(v, str) else v
    except Exception:
        return {'_raw': out[:300]}


def wait(sess, js, timeout=20, opts=()):
    end = time.time() + timeout
    while time.time() < end:
        if ev(sess, 'return !!(' + js + ');', opts) is True:
            return True
        time.sleep(0.3)
    return False


def shot(sess, name, opts=()):
    os.makedirs(SHOTS, exist_ok=True)
    p = os.path.join(SHOTS, name + '.png')
    ab(sess, 'screenshot', p, opts=opts)
    return p


def start_proxy():
    cd = os.path.join(EXP, 'cert')
    os.makedirs(cd, exist_ok=True)
    crt, key = os.path.join(cd, 'cert.pem'), os.path.join(cd, 'key.pem')
    if not os.path.exists(crt):
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', crt, '-days', '3',
                        '-subj', '/CN=' + HOST, '-addext', 'subjectAltName=DNS:' + HOST], capture_output=True, check=True)
    idf = os.path.join(EXP, 'ident.json')
    json.dump({'login': LOGIN, 'name': '测试者', 'ip': '100.64.0.7'}, open(idf, 'w'))
    pp = free_port(18843)
    lg = open(os.path.join(EXP, 'proxy.log'), 'ab')
    p = subprocess.Popen([PY, '-I', os.path.join(E.ROOT, 'tests/fake_ts_proxy.py'), str(pp), str(E.PORT), crt, key, idf], cwd=E.ROOT,
                         stdin=subprocess.DEVNULL, stdout=lg, stderr=lg, start_new_session=True)
    d = E.st_load()
    d['proxy'] = p.pid
    E.st_save(d)
    for _ in range(100):
        try:
            socket.create_connection(('127.0.0.1', pp), timeout=0.3).close()
            break
        except OSError:
            time.sleep(0.05)
    return pp


def step_e():
    pp = start_proxy()
    base = 'https://%s:%d' % (HOST, pp)
    M = ('--ignore-https-errors', '--user-agent', UA_IPHONE, '--args', '--host-resolver-rules=MAP %s 127.0.0.1' % HOST)
    ab(SESS_M, 'close', opts=M)
    ab(SESS_M, 'set', 'viewport', '390', '844', opts=M)
    ab(SESS_M, 'network', 'route', '**/api/prewarm', '--body', '{"ok": false}', opts=M)  # 别在用户的真实项目里预热 CLI
    url = re.sub(r'^https://[^/]+', base, E.api('remote-pair')['url'])  # 真实环境是 :8443，这里换成模拟代理的端口
    # 先直接打开首页（没配对）：同源重试一次后显示说明页；把配对链接粘贴进去（加到主屏幕的网页 App 没有地址栏时这样配对）
    rc, out = ab(SESS_M, 'open', base + '/', opts=M)
    check(wait(SESS_M, "document.querySelector('#pc input') && document.documentElement.classList.contains('shown')", 20, M), '没配对打开首页：显示配对说明和粘贴框')
    check('_r=1' in str(ev(SESS_M, "return location.search;", M)), '说明页先用同源跳转重试过一次（SameSite=Strict 从别处点进来第一次不带 cookie）')
    shot(SESS_M, 'm-1-unpaired', M)
    ev(SESS_M, "const i = document.querySelector('#pc input'); i.value = %s; document.querySelector('#pc button').click(); return 1;" % json.dumps(url), M)
    check(wait(SESS_M, "document.querySelector('form[action=\"/pair\"] button')", 20, M), '粘贴配对链接后：出现确认页（%s）' % out[-120:])
    nm = ev(SESS_M, "return document.querySelector('input[name=name]').value;", M)
    check(nm == 'iPhone', '设备名按 UA 猜成 iPhone（%s）' % nm)
    shot(SESS_M, 'm0-pair', M)
    ab(SESS_M, 'click', 'form[action="/pair"] button', opts=M)
    ok = wait(SESS_M, "window.CWB && document.documentElement.classList.contains('cwb-remote') && document.body.classList.contains('rr-m') && CWB.S.boot", 25, M)
    check(ok, '确认后进入工作台：远程模式、手机布局')
    tok = E.token()
    info = ev(SESS_M, "return {href: location.href, cookie: document.cookie, html: document.documentElement.outerHTML.includes(%s), "
                      "sw: document.documentElement.scrollWidth, iw: innerWidth, manifest: !!document.querySelector('link[rel=manifest]'), "
                      "touch: !!document.querySelector('link[rel=apple-touch-icon]'), theme: document.querySelectorAll('meta[name=theme-color]').length, "
                      "vp: document.querySelector('meta[name=viewport]').content};" % json.dumps(tok), M)
    check(isinstance(info, dict) and 't=' not in info.get('href', 't=') and '_r=' not in info.get('href', '') and info.get('html') is False,
          '页面地址和内容里都没有主令牌（%s）' % (info or {}).get('href'))
    check(isinstance(info, dict) and 'cwb_dev' not in info.get('cookie', 'cwb_dev'), '设备凭据是 HttpOnly，脚本读不到')
    check(info.get('manifest') and info.get('touch') and info.get('theme') == 2 and 'viewport-fit=cover' in info.get('vp', ''), 'PWA：清单、主屏图标、主题色、viewport-fit=cover')
    check(info.get('sw', 999) <= 390, '没有横向滚动（scrollWidth %s）' % info.get('sw'))
    mf = ev(SESS_M, "return fetch('/manifest.webmanifest').then(r => r.json()).then(j => JSON.stringify(j)).catch(e => String(e));", M)
    check('standalone' in str(mf) or isinstance(mf, dict), 'PWA 清单能取到')
    lay = ev(SESS_M, "const r = s => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };"
                     "return {side: r('#side'), dock: r('#dock'), comp: r('#composer'), top: r('#top'), btn: r('#uxSideBtn'), fs: getComputedStyle(document.querySelector('#input')).fontSize, ih: innerHeight};", M)
    check(lay.get('side') and lay['side'][0] + lay['side'][2] <= 0, '左栏默认收在屏幕外（%s）' % lay.get('side'))
    check(lay.get('comp') and lay['comp'][2] >= 340, '首页输入框几乎占满宽度（%s）' % lay.get('comp'))
    check(lay.get('fs') == '16px', '输入框字号 16px（iOS 聚焦时不放大页面）')
    check(lay.get('btn') and lay['btn'][2] >= 36, '侧栏按钮够大（%s）' % lay.get('btn'))
    shot(SESS_M, 'm1-home', M)
    ab(SESS_M, 'click', '#uxSideBtn', opts=M)
    time.sleep(0.4)
    dr = ev(SESS_M, "const b = document.querySelector('#side').getBoundingClientRect(), s = getComputedStyle(document.querySelector('#rrScrim'));"
                    "return {open: document.body.classList.contains('rr-drawer'), sidec: document.body.classList.contains('ux-sidec'), l: Math.round(b.left), w: Math.round(b.width), h: Math.round(b.height), scrim: s.opacity, rows: document.querySelectorAll('#side .srow').length, more: (document.querySelector('#side .srow .smore') ? getComputedStyle(document.querySelector('#side .srow .smore')).display : 'none')};", M)
    check(dr.get('open') and dr.get('l') == 0 and 0 < dr.get('w', 0) <= 340 and dr.get('scrim') == '1', '点侧栏按钮：左栏作为抽屉滑出、背后有遮罩（%s）' % dr)
    check(dr.get('rows', 0) == 0 or dr.get('more') == 'block', '抽屉里对话行的“⋯”常显（手机没有悬停）')
    shot(SESS_M, 'm2-drawer', M)
    ab(SESS_M, 'eval', "document.querySelector('#rrScrim').click()", opts=M)
    time.sleep(0.3)
    check(ev(SESS_M, "return document.body.classList.contains('rr-drawer');", M) is False, '点遮罩关闭抽屉')
    # 打开远程建的对话
    sid = E.st_load().get('d_sid') if E.st_load().get('d_ok') else None
    if sid:
        ev(SESS_M, "CWB.openSession(%s); return 1;" % json.dumps(sid), M)
        check(wait(SESS_M, "CWB.S.cur === %s && document.querySelectorAll('#feed .msg, #feed > *').length > 1" % json.dumps(sid), 15, M), '打开对话')
        time.sleep(0.8)
        sw = ev(SESS_M, "return document.documentElement.scrollWidth;", M)
        check(isinstance(sw, int) and sw <= 390, '对话页没有横向滚动（%s）' % sw)
        lay = ev(SESS_M, "const r = s => { const b = document.querySelector(s).getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };"
                         "return {dock: r('#dock'), comp: r('#composer'), ih: innerHeight};", M)
        check(lay.get('dock') and lay['dock'][1] + lay['dock'][3] >= lay['ih'] - 2 and lay['comp'][2] >= 340,
              '对话页输入框贴底、几乎占满宽度（dock %s，输入框 %s）' % (lay.get('dock'), lay.get('comp')))
        pl = ev(SESS_M, "const f = document.querySelector('#feed'), p = document.createElement('div'); p.className = 'k-plan'; p.id = 'r16rPlan';"
                        "p.innerHTML = '<div class=\"k-placts\"><button class=\"btn\">按计划执行</button><button class=\"btn\">提修改意见</button></div>'; f.appendChild(p);"
                        "const h = [...p.querySelectorAll('.btn')].map(b => Math.round(b.getBoundingClientRect().height)); p.remove(); return h;", M)
        check(isinstance(pl, list) and pl and min(pl) >= 44, '计划卡按钮高度 ≥ 44（%s）' % pl)
        shot(SESS_M, 'm3-chat', M)
    else:
        say('没有 D 步建好的对话，跳过对话页检查')
    # 右栏：reveal 改成右栏预览，全屏
    ev(SESS_M, "CWB.api('reveal', {path: %s}); return 1;" % json.dumps(E.PROJ), M)
    check(wait(SESS_M, "!document.querySelector('#rpane').hidden && document.querySelector('.pv-dir')", 10, M), '远程点“在访达中显示”：改成在右栏预览文件夹')
    rp = ev(SESS_M, "const b = document.querySelector('#rpane').getBoundingClientRect(), a = document.querySelector('.racts');"
                    "return {w: Math.round(b.width), h: Math.round(b.height), l: Math.round(b.left), t: Math.round(b.top), open: a && a.querySelector('[data-a=open]').textContent,"
                    "ed: a && getComputedStyle(a.querySelector('[data-a=editor]')).display, fd: a && getComputedStyle(a.querySelector('[data-a=finder]')).display, ih: innerHeight};", M)
    check(rp.get('w') == 390 and rp.get('l') == 0 and rp.get('h') >= rp.get('ih', 844) - 2, '右栏变成全屏面板（%s）' % rp)
    check(rp.get('open') == '下载' and rp.get('ed') == 'none' and rp.get('fd') == 'none', '预览里“用默认程序打开”改成“下载”，编辑器、访达两个按钮藏起来')
    shot(SESS_M, 'm4-pane', M)
    ab(SESS_M, 'click', '#rclose', opts=M)
    # 菜单改名
    mn = ev(SESS_M, "CWB.menu(document.querySelector('#top'), [{label: '在访达中显示', fn(){}}, {label: '用编辑器打开', fn(){}}, {label: '复制路径', fn(){}}]);"
                    "return new Promise(r => setTimeout(() => r([...document.querySelectorAll('.menu .mi')].map(b => (b.hidden ? '-' : '') + b.textContent)), 100));", M)
    check(mn == ['在右栏预览', '-在右栏预览', '复制路径'], '菜单里“在访达中显示”改成“在右栏预览”，重复的藏起来（%s）' % mn)
    ab(SESS_M, 'eval', "CWB.closeMenu()", opts=M)
    # 通知改成页面内提示
    nt = ev(SESS_M, "new Notification('测试标题', {body: '需要你确认：Bash'}); const n = document.querySelector('.rr-note'); return n ? n.textContent : '';", M)
    check('测试标题' in str(nt) and '需要你确认' in str(nt), '系统通知改成页面内提示（%s）' % nt)
    shot(SESS_M, 'm5-note', M)
    ab(SESS_M, 'eval', "document.querySelectorAll('.rr-note').forEach(n => n.remove())", opts=M)
    # 授权卡按钮够大
    ev(SESS_M, "CWB.S.perms.set('r16r-fake', {rid: 'r16r-fake', sid: CWB.S.cur || '', tool: 'Bash', input: {command: 'ls -la', description: '列出文件'}}); CWB.showModal(); return 1;", M)
    pm = ev(SESS_M, "const bs = [...document.querySelectorAll('#card .acts .btn')]; const c = document.querySelector('#card').getBoundingClientRect();"
                    "return {n: bs.length, h: bs.map(b => Math.round(b.getBoundingClientRect().height)), cw: Math.round(c.width), cb: Math.round(c.bottom), ih: innerHeight};", M)
    check(pm.get('n', 0) >= 2 and min(pm.get('h') or [0]) >= 44 and pm.get('cw') == 390 and pm.get('cb') >= pm.get('ih', 844) - 2,
          '授权卡贴底、按钮高度 ≥ 44（%s）' % pm)
    shot(SESS_M, 'm6-perm', M)
    ab(SESS_M, 'eval', "document.querySelector('#card [data-d=deny]').click()", opts=M)
    # 设置：远程页面只看本机信息
    ev(SESS_M, "CWB.openSettings('remote'); return 1;", M)
    check(wait(SESS_M, "document.querySelector('.sj-body') && document.querySelector('.sj-body').textContent.includes('只能在电脑上操作')", 10, M), '远程打开设置“远程访问”：只显示这台设备，管理只能在电脑上做')
    sp = ev(SESS_M, "const p = document.querySelector('.sj-panel').getBoundingClientRect(); return {w: Math.round(p.width), sw: document.querySelector('.sj-body').scrollWidth - document.querySelector('.sj-body').clientWidth};", M)
    check(sp.get('w') == 390 and sp.get('sw', 1) <= 0, '设置面板整屏、没有横向滚动（%s）' % sp)
    shot(SESS_M, 'm7-settings', M)
    ev(SESS_M, "CWB.closeSettings(); return 1;", M)
    # 在电脑上撤销这台设备：页面立即提示
    did = [d for d in E.api('remote-status')['devices'] if d['name'] == 'iPhone']
    check(len(did) == 1 and did[0].get('online'), '电脑上能看到这台 iPhone 在线')
    if did:
        E.api('remote-revoke', {'id': did[0]['id']})
        check(wait(SESS_M, "document.querySelector('#rrLock')", 15, M), '电脑上撤销后，手机页面立即提示“授权已失效”')
        shot(SESS_M, 'm8-revoked', M)
    ab(SESS_M, 'close', opts=M)
    # 桌面：本机页面不受影响
    D = ()
    ab(SESS_D, 'close')
    ab(SESS_D, 'set', 'viewport', '1440', '900')
    ab(SESS_D, 'network', 'route', '**/api/prewarm', '--body', '{"ok": false}')
    ab(SESS_D, 'open', 'http://127.0.0.1:%d/?t=%s' % (E.PORT, tok))
    check(wait(SESS_D, "window.CWB && CWB.S.boot", 20, D), '桌面宽度打开本机页面')
    dk = ev(SESS_D, "const cs = s => getComputedStyle(document.querySelector(s)); const b = s => { const r = document.querySelector(s).getBoundingClientRect(); return [Math.round(r.left), Math.round(r.width)]; };"
                    "return {remote: document.documentElement.classList.contains('cwb-remote'), rm: document.body.classList.contains('rr-m'), scrim: !!document.querySelector('#rrScrim'),"
                    "manifest: !!document.querySelector('link[rel=manifest]'), app: cs('#app').position, side: cs('#side').position, sidebox: b('#side'), main: b('#main'),"
                    "fetchWrapped: window.fetch.toString().includes('MAP'), notif: typeof Notification === 'function' && Notification.toString().includes('note('), es: EventSource.toString().includes('RealES')};", D)
    check(dk.get('remote') is False and dk.get('rm') is False and dk.get('scrim') is False and dk.get('manifest') is False, '本机页面：没有远程模式、手机布局、PWA 清单（%s）' % dk)
    check(dk.get('app') == 'static' and dk.get('side') == 'relative' and (dk.get('sidebox') or [9])[0] == 0 and (dk.get('sidebox') or [0, 0])[1] >= 200,
          '本机页面：#app、左栏还是原来的布局（%s %s）' % (dk.get('app'), dk.get('sidebox')))
    check(dk.get('fetchWrapped') is False and dk.get('notif') is False and dk.get('es') is False, '本机页面：fetch、通知、事件流都没被接管')
    shot(SESS_D, 'd1-desktop', D)
    ev(SESS_D, "CWB.openSettings('remote'); return 1;", D)
    check(wait(SESS_D, "document.querySelector('.rr-box .sj-sw[data-k=on]')", 10, D), '桌面设置里有“远程访问”页和开关')
    ev(SESS_D, "document.querySelector('.rr-pairbox [data-a=pair]').click(); return 1;", D)
    check(wait(SESS_D, "document.querySelector('.rr-pairbox svg.rr-qr path')", 10, D), '点“生成配对二维码”：本地库画出二维码')
    qrv = ev(SESS_D, "return {link: document.querySelector('.rr-plink input').value, left: document.querySelector('.rr-left').textContent, mods: document.querySelector('svg.rr-qr').viewBox.baseVal.width};", D)
    check(str(qrv.get('link', '')).startswith('https://%s:8443/pair?c=' % HOST) and '后失效' in str(qrv.get('left')) and qrv.get('mods', 0) > 20,
          '配对链接、倒计时、二维码（%s）' % {k: str(v)[:60] for k, v in (qrv or {}).items()})
    ev(SESS_D, "document.querySelector('.rr-pairbox').scrollIntoView({block: 'center'}); return 1;", D)
    shot(SESS_D, 'd2-settings', D)
    ev(SESS_D, "document.querySelector('.rr-pairbox [data-a=cancel]').click(); return 1;", D)
    ab(SESS_D, 'close')


# ---------------------------------------------------------------- F 空闲退出
def step_f():
    E.stop_server()
    pid = E.start_server(**server_env(CWB_IDLE_EXIT='5'))
    check(E.api('remote-status')['remote']['enabled'], '重启后远程仍开着')
    say('等 65 秒：远程开着、没有窗口连接，服务应保持运行')
    time.sleep(65)
    check(E.alive(pid) and E.ping().startswith('ok'), '远程开着：超过空闲时限（5 秒）一分钟以上也不退出')
    r = E.api('remote-disable')
    check(not r['remote']['enabled'], '关闭远程')
    say('等服务按原规则空闲退出（最多 75 秒）')
    gone = E.wait_until(lambda: not E.alive(pid), 75, 1)
    check(bool(gone), '关闭远程后：没有窗口连接，服务照常空闲退出')
    try:
        os.remove(os.path.join(E.RUN, 'stopped-%d' % E.PORT))
    except OSError:
        pass
    pid = E.start_server(**server_env())
    E.api('remote-enable')


# ---------------------------------------------------------------- G 关闭
def step_g():
    port = E.api('remote-status')['remote']['port']
    n0 = len(ts_calls())
    r = E.api('remote-disable')
    calls = ts_calls()[n0:]
    check(not r['remote']['enabled'] and ['serve', '--https=%d' % port, '--set-path=/', 'off'] in calls, '关闭：只运行 serve --https=%d --set-path=/ off' % port)
    sc = ts_serve()
    check(sc['Web'][HOST + ':443']['Handlers']['/']['Proxy'] == 'http://127.0.0.1:3000' and HOST + ':%d' % port not in sc['Web'], '关闭后用户的 443 规则还在，工作台那条没了')
    cookie = E.st_load().get('cookie1')
    st, _, _ = rreq('POST', '/api/remote-me', {}, cookie=cookie)
    check(st == 403, '关闭后 ts.net 来的请求：403')
    check(not any(c and c[0] == 'funnel' for c in ts_calls()), '整个测试从没调用过 tailscale funnel')


# ---------------------------------------------------------------- 清理
def kill(pid, needle):
    if pid and E.alive(pid) and needle in E.cmd_of(pid):
        os.kill(pid, signal.SIGTERM)
        E.wait_until(lambda: not E.alive(pid), 5, 0.1)
        return True
    return False


def cleanup():
    d = E.st_load()
    sids = [s for s in d.get('sids', [])]
    if E.ping().startswith('ok'):
        for s in sids:
            try:
                E.api('stop', {'sid': s})
            except Exception:
                pass
        try:
            if E.api('remote-status').get('remote', {}).get('enabled'):
                E.api('remote-disable')
        except Exception:
            pass
        time.sleep(2)
    E.stop_server()
    pp = E.kill_ptyd(signal.SIGTERM)
    if pp:
        E.wait_until(lambda: not E.alive(pp), 10)
        say('测试 ptyd 已结束', pp)
    if kill(d.get('gw'), 'fakegw.py'):
        say('假网关已结束', d.get('gw'))
    if kill(d.get('proxy'), 'fake_ts_proxy.py'):
        say('模拟代理已结束', d.get('proxy'))
    for s in (SESS_M, SESS_D):
        ab(s, 'close')
    n = 0
    trash = os.path.join(HOME, '.Trash', 'cwb-r16r-' + time.strftime('%H%M%S'))
    for s in sids:
        for p in (os.path.join(E.PROJ_DIR, s + '.jsonl'), os.path.join(E.PROJ_DIR, s),
                  os.path.join(HOME, '.claude', 'session-env', s), os.path.join(HOME, '.claude', 'file-history', s)):
            if os.path.lexists(p):
                os.makedirs(trash, exist_ok=True)
                shutil.move(p, os.path.join(trash, os.path.basename(os.path.dirname(p)) + '__' + os.path.basename(p)))
                n += 1
    say('测试会话记录已移到废纸篓：', len(sids), '个会话，', n, '项')
    if sids:  # 服务已停：从副本的 config.json 里去掉测试会话的选项
        cp = os.path.join(E.ROOT, 'config.json')
        try:
            c = json.load(open(cp, encoding='utf-8'))
            so = c.get('session_opts') or {}
            if any(s in so for s in sids):
                for s in sids:
                    so.pop(s, None)
                tmp = cp + '.tmp'
                json.dump(c, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
                os.replace(tmp, cp)
        except Exception as e:
            say('清理 config.json 失败', repr(e))
    d['sids'] = []
    E.st_save(d)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'all'
    only = set(mode.split(',')) if re.fullmatch(r'[A-G](,[A-G])*', mode) else None  # 调试用：只跑其中几步，例如 A,B,E
    if mode == 'cleanup':
        cleanup()
        return
    os.makedirs(EXP, exist_ok=True)
    os.makedirs(E.PROJ, exist_ok=True)
    if E.ping().startswith('ok'):
        say('端口 %d 上已经有服务，先清理' % E.PORT)
        cleanup()
    d = E.st_load()
    d.pop('d_ok', None)
    E.st_save(d)
    try:
        os.remove(os.path.join(E.RUN, 'remote-%d.json' % E.PORT))  # 每次从“没开放、没有设备”开始
    except OSError:
        pass
    fake_ts()
    try:
        start_gw()
        E.start_server(**server_env())
        for name, fn in (('A 开关', step_a), ('B 鉴权', step_b), ('C 撤销', step_c), ('D 远程控制 Claude', step_d),
                         ('E 手机页面', step_e), ('F 空闲退出', step_f), ('G 关闭', step_g)):
            if (mode == 'nolong' and fn is step_f) or (only and name[0] not in only):
                continue
            say('==== ' + name)
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, '%s 异常：%r' % (name, e))
    finally:
        cleanup()
    say('截图在', SHOTS)
    print('\n' + ('全部通过' if not E.FAIL else '失败 %d 项：\n  ' % len(E.FAIL) + '\n  '.join(E.FAIL)))
    sys.exit(1 if E.FAIL else 0)


if __name__ == '__main__':
    main()
