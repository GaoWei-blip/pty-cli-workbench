"""第十六轮 R 单元测试：远程访问（remote.py）。不启动服务、不连网络、不碰真实 Tailscale：
- PATH 里放假的 tailscale（tests/fake_tailscale.py）测命令拼装、状态解析、不覆盖已有规则、只撤自己那条、从不调用 funnel；
- 直接构造 Handler 对象跑 do_GET / do_POST，测鉴权：无凭据、过期配对码、重复使用、撤销后、错 Host、伪造身份头、主令牌经代理不能用；
- 远程开着时刷新 last_seen（不空闲退出）、防休眠开关、osa 选择框拦截、下载、手机样式只在远程页面生效。
状态文件放临时目录。运行：.venv/bin/python -I tests/test_r16_R.py
"""
import http.client
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import server as S  # noqa: E402

RM = S.RM
FAIL = []
HOST = 'cwb-mac.tailfake0.ts.net'
LOGIN = 'tester@example.com'
SELF_IPS = ['100.101.102.103', 'fd7a:115c:a1e0::1']


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)
    return cond


# ---------------------------------------------------------------- 假 tailscale
class FakeTS:
    def __init__(self):
        self.d = tempfile.mkdtemp(prefix='cwb-r16r-ts-')
        self.bin = os.path.join(self.d, 'bin')
        self.st = os.path.join(self.d, 'state')
        os.makedirs(self.bin)
        os.makedirs(self.st)
        w = os.path.join(self.bin, 'tailscale')
        with open(w, 'w') as f:
            f.write('#!/bin/sh\nexec "%s" -I "%s" "$@"\n' % (sys.executable, os.path.join(HERE, 'tests', 'fake_tailscale.py')))
        os.chmod(w, 0o755)
        self.old = (os.environ.get('PATH'), os.environ.get('FAKE_TS_DIR'))
        os.environ['PATH'] = self.bin + ':' + (self.old[0] or '/usr/bin:/bin')
        os.environ['FAKE_TS_DIR'] = self.st

    def status(self, **kw):
        d = {'Version': '1.90.2', 'BackendState': kw.get('state', 'Running'), 'TailscaleIPs': SELF_IPS,
             'Self': {'DNSName': HOST + '.', 'UserID': 1234567, 'TailscaleIPs': SELF_IPS, 'HostName': 'cwb-mac'},
             'User': {'1234567': {'ID': 1234567, 'LoginName': kw.get('login', LOGIN), 'DisplayName': '测试者'}},
             'CertDomains': [] if kw.get('nohttps') else [HOST],
             'CurrentTailnet': {'Name': 'tailfake0', 'MagicDNSSuffix': 'tailfake0.ts.net', 'MagicDNSEnabled': True}}
        self.put('status.json', d)

    def put(self, name, obj):
        with open(os.path.join(self.st, name), 'w') as f:
            json.dump(obj, f)

    def rm(self, name):
        try:
            os.remove(os.path.join(self.st, name))
        except FileNotFoundError:
            pass

    def serve(self):
        try:
            return json.load(open(os.path.join(self.st, 'serve.json')))
        except FileNotFoundError:
            return {}

    def calls(self):
        try:
            return [json.loads(x) for x in open(os.path.join(self.st, 'calls.jsonl'))]
        except FileNotFoundError:
            return []

    def close(self):
        os.environ['PATH'] = self.old[0] or ''
        if self.old[1] is None:
            os.environ.pop('FAKE_TS_DIR', None)
        else:
            os.environ['FAKE_TS_DIR'] = self.old[1]
        shutil.rmtree(self.d, ignore_errors=True)


def user_rule():
    """用户自己已有的规则：443 上 / 指向 3000，还有一个前台会话占着 8443。"""
    return {'TCP': {'443': {'HTTPS': True}}, 'Web': {HOST + ':443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:3000'}}}},
            'Foreground': {'sess1': {'TCP': {'8443': {'HTTPS': True}}, 'Web': {HOST + ':8443': {'Handlers': {'/x': {'Text': 'hi'}}}}}}}


# ---------------------------------------------------------------- 构造请求
class Sock:
    """给 Handler.connection 用的 socketpair 一端；peer 用来看是否被断开。"""

    def __init__(self):
        self.a, self.b = socket.socketpair()


def req(method, path, headers=None, body=b'', host=HOST, h=None):
    """h 不为空时复用同一个 Handler 实例（模拟同一条 keep-alive 连接上的下一个请求）。"""
    h = h or S.Handler.__new__(S.Handler)
    REQ_H[0] = h
    hd = {'Host': host}
    hd.update(headers or {})
    if body and 'Content-Length' not in hd:
        hd['Content-Length'] = str(len(body))
    raw = ''.join('%s: %s\r\n' % (k, v) for k, v in hd.items()) + '\r\n'
    h.headers = http.client.parse_headers(io.BytesIO(raw.encode('utf-8')))
    h.path, h.command, h.request_version = path, method, 'HTTP/1.1'
    h.requestline = '%s %s HTTP/1.1' % (method, path)
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    h.client_address = ('127.0.0.1', 50000)
    h.close_connection = False
    h.server = None
    sk = Sock()
    h.connection = sk.a
    (h.do_GET if method == 'GET' else h.do_POST)()
    out = h.wfile.getvalue()
    head, _, rb = out.partition(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    code = int(lines[0].split()[1]) if lines and lines[0] else 0
    hs = {}
    for l in lines[1:]:
        k, _, v = l.partition(':')
        hs.setdefault(k.strip().lower(), []).append(v.strip())
    RESP.append(rb)
    sk.a.close()
    sk.b.close()
    return code, hs, rb


RESP = []
REQ_H = [None]


def jbody(b):
    try:
        return json.loads(b or b'{}')
    except ValueError:
        return {}


def post(path, obj=None, cookie='', host=HOST, origin=None, extra=None):
    hd = {'Content-Type': 'application/json', 'Origin': origin if origin is not None else 'https://' + host,
          'Sec-Fetch-Site': 'same-origin', 'Tailscale-User-Login': LOGIN, 'X-Forwarded-For': '100.64.0.9',
          'X-Forwarded-Host': host, 'X-Forwarded-Proto': 'https'}
    if cookie:
        hd['Cookie'] = cookie
    hd.update(extra or {})
    hd = {k: v for k, v in hd.items() if v is not None}
    return req('POST', path, hd, json.dumps(obj or {}).encode(), host)


def get(path, cookie='', host=HOST, extra=None):
    hd = {'Tailscale-User-Login': LOGIN, 'X-Forwarded-For': '100.64.0.9', 'X-Forwarded-Host': host, 'X-Forwarded-Proto': 'https',
          'Accept': 'text/html'}
    if cookie:
        hd['Cookie'] = cookie
    hd.update(extra or {})
    hd = {k: v for k, v in hd.items() if v is not None}
    return req('GET', path, hd, b'', host)


def local_post(path, obj=None, extra=None):
    hd = {'Content-Type': 'application/json', 'X-Token': S.TOKEN}
    hd.update(extra or {})
    return req('POST', path, hd, json.dumps(obj or {}).encode(), host='127.0.0.1:%d' % S.PORT)


def pair_device(name='测试手机', login=LOGIN):
    code = RM.api_pair({})['url'].split('c=', 1)[1]
    c, hs, b = req('POST', '/pair', {'Host': HOST, 'Origin': 'https://' + HOST, 'Sec-Fetch-Site': 'same-origin',
                                     'Content-Type': 'application/x-www-form-urlencoded', 'Tailscale-User-Login': login,
                                     'X-Forwarded-For': '100.64.0.9', 'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)'},
                   ('c=%s&name=%s' % (code, name)).encode(), HOST)
    ck = (hs.get('set-cookie') or [''])[0]
    return c, ck, ck.split(';')[0], code


# ---------------------------------------------------------------- 用例
def test_detect(ts):
    check(RM.ts_bin() == os.path.join(ts.bin, 'tailscale'), 'PATH 里的 tailscale 优先')
    st = RM.ts_status()
    check(st['installed'] and not st['running'] and st['error'], '装了但没在运行：status 失败时报“没在运行”和原因（%s）' % st['error'][:40])
    ts.status(state='NeedsLogin')
    st = RM.ts_status()
    check(st['running'] and not st['loggedIn'] and st['state'] == 'NeedsLogin', '没登录：BackendState=NeedsLogin')
    ts.status()
    st = RM.ts_status()
    check(st['loggedIn'] and st['dns'] == HOST and st['login'] == LOGIN and st['https'] and st['magicdns'] and st['ips'] == SELF_IPS,
          'Running：MagicDNS 名去掉末尾的点、账号按 Self.UserID 查 User、CertDomains 非空算有 HTTPS（%s %s）' % (st['dns'], st['login']))
    ts.status(nohttps=True)
    check(not RM.ts_status()['https'], 'CertDomains 为空：HTTPS 证书没开')
    ts.status()
    # 没装：PATH 里没有、App 也不在
    old_path, old_app = os.environ['PATH'], RM.APP_TS
    os.environ['PATH'] = '/usr/bin:/bin'
    RM.APP_TS = '/nonexistent/Tailscale'
    le = S.LOGIN_ENV
    S.LOGIN_ENV = {'PATH': '/usr/bin:/bin'}
    try:
        st = RM.ts_status()
        check(not st['installed'] and not st['bin'], '没装：installed=False')
        fake_app = os.path.join(ts.d, 'Tailscale.app', 'Contents', 'MacOS', 'Tailscale')
        os.makedirs(os.path.dirname(fake_app))
        shutil.copy(os.path.join(ts.bin, 'tailscale'), fake_app)
        RM.APP_TS = fake_app
        check(RM.ts_bin() == fake_app and RM.ts_status()['app'], 'PATH 里没有时用 App 里带的命令行（App Store 版）')
    finally:
        os.environ['PATH'], RM.APP_TS, S.LOGIN_ENV = old_path, old_app, le
    check(RM.dec('=?utf-8?q?Ferris_B=C3=BCller?=') == 'Ferris Büller' and RM.dec('a@b.com') == 'a@b.com', '身份头的 RFC 2047 Q 编码能解开')


def test_enable_disable(ts):
    ts.put('serve.json', user_rule())
    n0 = len(ts.calls())
    r = RM.api_enable({})
    calls = ts.calls()[n0:]
    target = 'http://127.0.0.1:%d' % S.PORT
    check(r.get('ok') and r['remote']['enabled'] and RM.ST['port'] == 8444,
          '443 被用户规则占着、8443 被前台会话占着：挑 8444（%s）' % RM.ST.get('port'))
    check(['serve', '--bg', '--https=8444', target] in calls, '命令：serve --bg --https=8444 http://127.0.0.1:<端口>')
    sc = ts.serve()
    check(sc['Web'][HOST + ':443']['Handlers']['/']['Proxy'] == 'http://127.0.0.1:3000' and 'Foreground' in sc, '用户原有的 443 规则、前台会话都没动')
    check(r['remote']['url'] == 'https://%s:8444/' % HOST and r['remote']['active'] is True, '地址带端口，状态里能看到规则在')
    n1 = len(ts.calls())
    r = RM.api_enable({})
    check(r.get('ok') and not any(c[:2] == ['serve', '--bg'] for c in ts.calls()[n1:]), '已经开着再开：认出自己的规则，不重复添加')
    # 关闭：只撤自己那条
    r = RM.api_disable({})
    calls = ts.calls()[n1:]
    check(['serve', '--https=8444', '--set-path=/', 'off'] in calls and not r['remote']['enabled'], '关闭：serve --https=8444 --set-path=/ off')
    sc = ts.serve()
    check(HOST + ':8444' not in sc.get('Web', {}) and '8444' not in sc.get('TCP', {}) and sc['Web'][HOST + ':443']['Handlers']['/']['Proxy'] == 'http://127.0.0.1:3000',
          '关闭后：自己那条没了，用户的 443 还在')
    # 用户自己改掉了这条：关闭时不动
    RM.api_enable({})
    sc = ts.serve()
    sc['Web'][HOST + ':%d' % RM.ST['port']]['Handlers']['/'] = {'Proxy': 'http://127.0.0.1:5000'}
    ts.put('serve.json', sc)
    n2 = len(ts.calls())
    r = RM.api_disable({})
    check(not any(c[-1:] == ['off'] for c in ts.calls()[n2:]) and '不是工作台加的' in (r.get('note') or ''), '端口上已经是用户的规则：关闭时不撤，给出说明')
    # 没开 HTTPS：返回开启网址，不算开放
    ts.put('serve.json', {})
    ts.put('need_https', {})
    ts.status(nohttps=True)
    r = RM.api_enable({})
    check(r.get('ok') is False and r.get('need') == 'https' and r.get('url', '').startswith('https://login.tailscale.com/f/serve') and not RM.enabled(),
          '没开 HTTPS 证书：从 serve 的输出里取出开启网址（%s），不算开放' % r.get('url'))
    ts.rm('need_https')
    ts.status()
    # Funnel 设在这个端口上：拒绝
    ts.put('serve.json', {'TCP': {'443': {'HTTPS': True}}, 'Web': {HOST + ':443': {'Handlers': {'/': {'Proxy': target}}}}, 'AllowFunnel': {HOST + ':443': True}})
    try:
        RM.api_enable({})
        check(False, '端口被设成 Funnel：应该拒绝开放')
    except ValueError as e:
        check('Funnel' in str(e) and not RM.enabled(), '端口被设成 Funnel：拒绝开放（%s）' % str(e)[:30])
    ts.put('serve.json', {})
    # 关闭时 Tailscale 不能用：记下待撤，之后再撤
    RM.api_enable({})
    port = RM.ST['port']
    ts.rm('status.json')
    r = RM.api_disable({})
    check(not RM.enabled() and RM.ST.get('stale') and r.get('note'), '关闭时 Tailscale 不能用：先关掉远程、记下待撤（%s）' % (r.get('note') or '')[:30])
    ts.status()
    RM.retry_stale()
    check(not RM.ST.get('stale') and HOST + ':%d' % port not in ts.serve().get('Web', {}), 'Tailscale 能用以后补撤成功')
    # 都占满：报错不动
    full = {'TCP': {str(p): {'HTTPS': True} for p in RM.PORTS}}
    ts.put('serve.json', full)
    try:
        RM.api_enable({})
        check(False, '端口都被占：应该报错')
    except ValueError as e:
        check('没有改动' in str(e) and ts.serve() == full, '候选端口都被占：报错，不改任何规则')
    ts.put('serve.json', {})
    check(not any(c and c[0] == 'funnel' for c in ts.calls()), '整个过程从没调用过 tailscale funnel')


def test_auth(ts):
    ts.put('serve.json', {})
    RM.api_enable({})
    with RM.LOCK:
        RM.ST['devices'] = {}
    RM.save()
    RESP.clear()
    # 本机：不变
    c, _, b = local_post('/api/remote-me')
    check(c == 200 and jbody(b) == {'remote': False}, '本机请求：沿用主令牌，正常')
    c, _, _ = local_post('/api/remote-me', extra={'X-Forwarded-For': '100.64.0.9'})
    check(c == 403, '本机地址的请求带 X-Forwarded-For（经 tailscale serve 转发）：主令牌也不行，403')
    c, _, _ = local_post('/api/remote-me', extra={'Tailscale_User_Login': LOGIN})
    check(c == 403, '本机地址带下划线写法的身份头：同样 403')
    c, _, _ = req('GET', '/hook/busy', {}, b'', host='127.0.0.1:%d' % S.PORT)
    check(c == 404 or c == 403, '本机 GET /hook/ 行为不变（%s）' % c)
    # 错 Host
    c, _, _ = get('/', host='evil.example.com')
    check(c == 403, '其他 Host：403')
    c, _, _ = get('/', host='other-mac.tailfake0.ts.net')
    check(c == 403, '别的 ts.net 名：403')
    # 没凭据
    c, hs, b = get('/')
    check(c == 401 and '还没有配对' in b.decode() and '_r' in b.decode(), '没凭据打开首页：401 配对说明页（带一次同源重试）')
    c, _, b = post('/api/projects')
    check(c == 401 and jbody(b).get('remote') == 'unpaired', '没凭据调接口：401')
    c, _, b = post('/api/projects', extra={'X-Token': S.TOKEN})
    check(c == 401, 'ts.net 来的请求带主令牌也不认：401')
    c, _, b = get('/events?t=' + S.TOKEN)
    check(c == 401, 'ts.net 来的事件流带 ?t=主令牌也不认：401')
    c, _, b = get('/static/app.js')
    check(c == 401, '没凭据取界面脚本：401')
    c, hs, b = get('/manifest.webmanifest')
    check(c == 200 and jbody(b).get('display') == 'standalone' and 'manifest' in hs['content-type'][0], 'PWA 清单不用凭据（浏览器取清单默认不带 cookie）')
    c, hs, b = get('/static/icon-192.png')
    check(c == 200 and b[:4] == b'\x89PNG', '图标不用凭据')
    # 配对
    code = RM.api_pair({})['url'].split('c=', 1)[1]
    c, _, b = get('/pair?c=wrong')
    check(c == 410 and '无效' in b.decode(), '错的配对码：410')
    c, _, b = get('/pair?c=' + code)
    check(c == 200 and 'name="c"' in b.decode() and '确认配对' in b.decode() and not RM.PAIR.get('used'), '打开配对链接：显示确认页，不消耗配对码')
    c, hs, b = req('POST', '/pair', {'Host': HOST, 'Content-Type': 'application/x-www-form-urlencoded', 'Tailscale-User-Login': LOGIN,
                                     'X-Forwarded-For': '100.64.0.9'}, ('c=' + code).encode(), HOST)
    check(c == 403 and not RM.PAIR.get('used'), '确认配对不带 Origin：403，不消耗')
    c, ck, cookie, code = pair_device()
    check(c == 303 and all(x in ck for x in ('HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/', 'Max-Age=')), '确认配对：303 跳回首页，写入 HttpOnly、Secure、SameSite=Strict 的长期凭据')
    did, sec = cookie.split('=', 1)[1].split('.', 1)
    raw = open(RM.state_path()).read()
    check(sec not in raw and RM.sha(sec) in raw and oct(os.stat(RM.state_path()).st_mode & 0o777) == '0o600', '服务端只存凭据的哈希，状态文件 0600')
    dv = RM.ST['devices'][did]
    check(dv['name'] == '测试手机' and dv['login'] == LOGIN, '设备记下名称和 Tailscale 账号')
    c, _, b = req('POST', '/pair', {'Host': HOST, 'Origin': 'https://' + HOST, 'Content-Type': 'application/x-www-form-urlencoded',
                                    'Tailscale-User-Login': LOGIN, 'X-Forwarded-For': '100.64.0.9'}, ('c=' + code).encode(), HOST)
    check(c == 410 and '用过' in b.decode(), '同一个配对码再用一次：410“已经用过了”')
    c2 = RM.api_pair({})['url'].split('c=', 1)[1]
    RM.PAIR['exp'] = time.time() - 1
    c, _, b = get('/pair?c=' + c2)
    check(c == 410 and '过期' in b.decode(), '过期的配对码：410“已过期”')
    # 有凭据
    c, hs, b = get('/', cookie=cookie)
    check(c == 200 and b'<div id="app">' in b, '有凭据打开首页：200')
    c, _, b = post('/api/remote-me', cookie=cookie)
    check(c == 200 and jbody(b).get('remote') is True and jbody(b).get('id') == did, '有凭据调接口：正常，remote-me 认出这台设备')
    c, _, b = post('/api/projects', cookie=cookie)
    check(c == 200 and 'projects' in jbody(b), '有凭据调普通接口（projects）：正常')
    c, _, b = post('/api/projects', cookie=cookie, origin='https://evil.example.com')
    check(c == 403, 'Origin 不对：403')
    c, _, b = post('/api/projects', cookie=cookie, extra={'Origin': None, 'Sec-Fetch-Site': None})
    check(c == 403, '没有 Origin 也没有 Sec-Fetch-Site：403')
    c, _, b = post('/api/projects', cookie=cookie, origin='null')
    check(c == 200, 'Origin: null + Sec-Fetch-Site: same-origin（引用策略严格的页面提交表单）：通过')
    c, _, b = post('/api/projects', cookie=cookie, origin='null', extra={'Sec-Fetch-Site': None})
    check(c == 403, 'Origin: null 又没有 Sec-Fetch-Site：403')
    c, _, b = post('/api/projects', cookie=cookie, extra={'Sec-Fetch-Site': None})
    check(c == 200, '老浏览器没有 Sec-Fetch-Site、Origin 对：通过')
    c, _, b = post('/api/projects', cookie=cookie, extra={'Sec-Fetch-Site': 'cross-site'})
    check(c == 403, 'Sec-Fetch-Site=cross-site：403')
    c, _, b = post('/api/projects', cookie=cookie.replace(sec, sec[:-2] + 'xx'))
    check(c == 401, '凭据被改了一位：401')
    # 身份
    c, _, b = post('/api/projects', cookie=cookie, extra={'Tailscale-User-Login': 'eve@example.com'})
    check(c == 403 and 'eve@example.com' in jbody(b).get('error', ''), '伪造或不同的 Tailscale 账号：403')
    c, _, b = post('/api/projects', cookie=cookie, extra={'Tailscale-User-Login': None})
    check(c == 403, '没有身份头（带标签或共享来的设备）、也不是本机地址：403')
    c, _, b = post('/api/projects', cookie=cookie, extra={'Tailscale-Funnel-Request': '?1'})
    check(c == 403, '带 Tailscale-Funnel-Request（公网来的）：403')
    c, _, b = get('/', extra={'Tailscale-User-Login': None, 'X-Forwarded-For': SELF_IPS[0]})
    check(c == 401, '本机自己经 ts.net 访问（没有身份头、来源是本机 Tailscale IP）：按本机账号算，过了身份这一关（没凭据所以 401）')
    RM.api_set({'sameAccount': False})
    c, ck2, cookie2, _ = pair_device('别人的平板', login='bob@example.com')
    check(c == 303, '关掉“只允许同一个账号”后：别的账号可以配对')
    c, _, b = post('/api/projects', cookie=cookie2, extra={'Tailscale-User-Login': LOGIN})
    check(c == 403, '凭据和配对时的账号绑定：换个账号拿着它也不行')
    RM.api_set({'sameAccount': True})
    c, _, b = post('/api/projects', cookie=cookie2, extra={'Tailscale-User-Login': 'bob@example.com'})
    check(c == 403, '重新打开“只允许同一个账号”：别的账号的设备立即不能用')
    # 远程不能用的接口
    for name in ('reveal', 'open-file', 'open-url', 'pick-files', 'add-folder', 'restart-service', 'ui-shot', 'notify'):
        c, _, b = post('/api/' + name, {'path': '/tmp'}, cookie=cookie)
        check(c == 403 and jbody(b).get('error'), '远程不能用 %s：403（%s）' % (name, jbody(b).get('error')))
    for name in ('remote-status', 'remote-enable', 'remote-disable', 'remote-pair', 'remote-revoke', 'remote-set'):
        c, _, b = post('/api/' + name, cookie=cookie)
        check(c == 403, '远程不能改远程设置：%s 403' % name)
    c, _, b = post('/hook/busy', cookie=cookie, extra={'X-Token': S.TOKEN})
    check(c == 403, '远程不能调 /hook/：403')
    d = tempfile.mkdtemp(prefix='cwb-r16r-dir-')
    c, _, b = post('/api/remote-add-folder', {'path': d}, cookie=cookie)
    check(c == 200 and jbody(b).get('cwd') == d, '远程填路径添加文件夹（不弹电脑上的选择框）')
    cf = S.load_conf()
    if d in cf.get('folders', []):
        cf['folders'].remove(d)
        S.save_conf(cf)
    # 远程时 osa 选择框被拦下
    called, run0 = [], S.subprocess.run
    S.subprocess.run = lambda *a, **k: called.append(a) or run0(['true'])
    RM.CTX.dev = {'id': did}
    try:
        check(S.osa('return 1') == '' and not called, '远程请求里 osa 选择框直接返回空，不运行 osascript、不在电脑上弹窗')
    finally:
        RM.CTX.dev = None
        S.subprocess.run = run0
    check(S.osa.__qualname__ == 'setup.<locals>.osa', 'server.py 和 features.py 用的 osa 都是包过的（features 经 NS 取）')
    # 下载
    fp = os.path.join(d, '报告 1.txt')
    open(fp, 'w').write('内容abc')
    c, hs, b = get('/r/dl?p=' + RM.quote(fp), cookie=cookie)
    check(c == 200 and b.decode() == '内容abc' and "filename*=UTF-8''" in hs['content-disposition'][0], '下载文件：带中文文件名')
    c, _, _ = get('/r/dl?p=/nonexistent/x', cookie=cookie)
    check(c == 404, '下载不存在的文件：404')
    c, _, _ = get('/r/dl?p=' + RM.quote(fp))
    check(c == 401, '没凭据不能下载')
    shutil.rmtree(d, ignore_errors=True)
    # 撤销：立即生效，断开事件流
    sk = Sock()
    with RM.LOCK:
        RM.SSE.setdefault(did, set()).add(sk.a)
    devs = RM.dev_list()
    check(any(x['id'] == did and x['online'] for x in devs), '设备列表：有事件流连着时显示在线')
    local_post('/api/remote-revoke', {'id': did})
    sk.b.settimeout(2)
    try:
        got = sk.b.recv(10)
    except Exception:
        got = None
    check(got == b'', '撤销：它的事件流连接立即被断开（对方读到结束）')
    sk.a.close()
    sk.b.close()
    c, _, b = post('/api/remote-me', cookie=cookie)
    check(c == 401, '撤销后：同一个凭据 401')
    # 远程主动取消授权
    c, ck3, cookie3, _ = pair_device('第二台')
    did3 = cookie3.split('=', 1)[1].split('.', 1)[0]
    c, hs, b = post('/api/remote-forget', cookie=cookie3)
    check(c == 200 and 'Max-Age=0' in (hs.get('set-cookie') or [''])[0] and did3 not in RM.ST['devices'], '远程页面“取消授权”：删掉自己并清 cookie')
    # 改名
    c, ck4, cookie4, _ = pair_device('第三台')
    did4 = cookie4.split('=', 1)[1].split('.', 1)[0]
    r = RM.api_rename({'id': did4, 'name': '  我的\x07手机  '})
    check(any(x['id'] == did4 and x['name'] == '我的手机' for x in r['devices']), '改名：去掉控制字符和首尾空格')
    # 远程关掉后
    RM.api_disable({})
    c, _, _ = post('/api/remote-me', cookie=cookie4)
    check(c == 403, '关掉远程访问：ts.net 来的请求一律 403（设备保留，再开放时不用重配）')
    check(did4 in RM.ST['devices'], '关掉远程访问不删设备')
    # 主令牌从没出现在任何响应里
    check(not any(S.TOKEN.encode() in x for x in RESP), '所有响应里都没有主令牌（共 %d 个响应）' % len(RESP))


def test_tick_awake(ts):
    ts.put('serve.json', {})
    started, stopped = [], []

    class P:
        pid = 4242

        def poll(self):
            return None if self not in stopped else 0
    orig = (S.sched_awake_start, S.sched_awake_stop)
    S.sched_awake_start = lambda: (started.append(P()) or started[-1])
    S.sched_awake_stop = lambda p: stopped.append(p)
    try:
        RM.api_enable({})
        S.BUS.last_seen = 0
        RM.tick(time.time())
        check(time.time() - S.BUS.last_seen < 2, '远程开着：tick 刷新 BUS.last_seen，服务不因“30 分钟没有窗口连接”退出')
        check(RM.keep_alive(), 'keep_alive() 为真')
        RM.api_set({'awake': True})
        check(len(started) == 1 and RM.status_payload()['remote']['awakeOn'], '打开“防止空闲休眠”：起一个 caffeinate（复用定时任务的 sched_awake_start）')
        RM.awake_apply()
        check(len(started) == 1, '已经在防休眠：不重复起')
        RM.api_disable({})
        check(len(stopped) == 1 and stopped[0] is started[0], '关闭远程：结束自己起的那个')
        S.BUS.last_seen = 0
        RM.tick(time.time())
        check(S.BUS.last_seen == 0 and not RM.keep_alive(), '远程关着：tick 不刷新 last_seen（原来的空闲退出照常）')
        RM.api_set({'awake': False})
    finally:
        S.sched_awake_start, S.sched_awake_stop = orig
    src = open(os.path.join(HERE, 'server.py'), encoding='utf-8').read()
    check('RM.tick(now)' in src and src.count('RM.gate(self') == 2, 'server.py 挂接点：poll_loop 里 RM.tick，do_GET/do_POST 里 RM.gate')


def test_css_scope():
    css = open(os.path.join(HERE, 'static', 'ux-r.css'), encoding='utf-8').read()
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    bad = []
    for m in re.finditer(r'([^{}]+)\{[^{}]*\}', css):
        sel = m.group(1).strip()
        if sel.startswith('@') or not sel:
            continue
        sel = re.sub(r'^@media[^{]*\{', '', sel).strip()
        for s in sel.split(','):
            s = s.strip()
            if not s or re.match(r'^(from|to|\d+%)$', s):
                continue
            if not ('cwb-remote' in s or '.rr-' in s or '#rr' in s):
                bad.append(s)
    check(not bad, 'ux-r.css 的规则都限定在 html.cwb-remote 下，或者只作用于本轮新增的 rr- 元素（桌面窗口不受影响）%s' % (bad[:5] or ''))
    js = open(os.path.join(HERE, 'static', 'ux-r.js'), encoding='utf-8').read()
    check("const REMOTE = !/^(127\\.0\\.0\\.1|localhost)$/i.test(location.hostname);" in js and 'if (REMOTE) early();' in js and 'if (!REMOTE) return;' in js,
          'ux-r.js：本机打开时只加设置页，fetch、通知、事件流、手机布局只在远程页面接管')
    html = open(os.path.join(HERE, 'static', 'index.html'), encoding='utf-8').read()
    check(html.index('/static/ux-r.js') < html.index('/static/app.js') < html.index('/static/ux-j.js'), 'ux-r.js 在 app.js、ux-j.js 之前加载')


def test_review_fixes(ts):
    """复核修复：远程响应禁止嵌入（/file 只许同源嵌入）、本机响应不加；保存状态并发安全。"""
    ts.put('serve.json', {})
    RM.api_enable({})
    c, ck, cookie, _ = pair_device('复核手机')
    xfo = lambda hs: ((hs.get('x-frame-options') or [''])[0], (hs.get('content-security-policy') or [''])[0])
    deny = ('DENY', "frame-ancestors 'none'")
    cases = [('首页（有凭据）', get('/', cookie=cookie)), ('首页（没凭据，401 说明页）', get('/')), ('配对页', get('/pair?c=x')),
             ('PWA 清单', get('/manifest.webmanifest')), ('界面脚本', get('/static/ux-r.js', cookie=cookie)),
             ('接口', post('/api/remote-me', cookie=cookie)), ('被拦的接口', post('/api/reveal', cookie=cookie)),
             ('身份不对', post('/api/remote-me', cookie=cookie, extra={'Tailscale-User-Login': 'eve@example.com'})),
             ('别的 Host', get('/', host='evil.example.com')), ('下载 404', get('/r/dl?p=/nonexistent', cookie=cookie))]
    bad = [(n, r[0], xfo(r[1])) for n, r in cases if xfo(r[1]) != deny]
    check(not bad, '远程来的响应都带 X-Frame-Options: DENY 和 frame-ancestors \'none\'（%d 种）%s' % (len(cases), bad or ''))
    d = tempfile.mkdtemp(prefix='cwb-r16r-pdf-')
    pdf = os.path.join(d, 'a.pdf')
    open(pdf, 'wb').write(b'%PDF-1.4\n%%EOF\n')
    c, hs, b = get('/file?p=' + RM.quote(pdf) + '&cwd=&t=', cookie=cookie)
    check(c == 200 and b.startswith(b'%PDF') and xfo(hs) == ('SAMEORIGIN', "frame-ancestors 'self'"),
          '/file（远程页面自己的 PDF 预览 iframe）只许同源嵌入：SAMEORIGIN、frame-ancestors \'self\'（%s %s）' % (c, xfo(hs)))
    shutil.rmtree(d, ignore_errors=True)
    c, hs, b = req('GET', '/?t=' + S.TOKEN, {}, b'', host='127.0.0.1:%d' % S.PORT)
    check(c == 200 and xfo(hs) == ('', ''), '本机打开首页（原生 App 的 WKWebView）：不加任何禁止嵌入的头')
    c, hs, b = local_post('/api/remote-me')
    check(c == 200 and xfo(hs) == ('', ''), '本机调接口：不加')
    get('/', cookie=cookie)
    h = REQ_H[0]
    c, hs, b = req('GET', '/?t=' + S.TOKEN, {}, b'', host='127.0.0.1:%d' % S.PORT, h=h)
    check(c == 200 and xfo(hs) == ('', ''), '同一条连接上先远程、后本机：本机那次不带上一次的头')
    c, hs, b = req('GET', '/', {'Tailscale-User-Login': LOGIN, 'X-Forwarded-For': '100.64.0.9', 'Cookie': cookie}, b'', HOST, h=h)
    check(c == 200 and xfo(hs) == deny and list(h.__dict__).count('end_headers') == 1, '再来远程请求：照样加，而且只包一层')
    # 并发保存
    import threading
    errs = []

    def worker(k):
        try:
            for i in range(25):
                with RM.LOCK:
                    RM.ST['devices'].setdefault('t%02d' % k, {'name': 'x', 'hash': '0', 'created': 0, 'last': 0, 'login': ''})['last'] = i
                RM.save()
        except Exception as e:
            errs.append(repr(e))
    th = [threading.Thread(target=worker, args=(k,)) for k in range(16)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    data = json.load(open(RM.state_path()))
    left = [f for f in os.listdir(os.path.dirname(RM.state_path())) if f.endswith('.tmp')]
    check(not errs and all(data['devices']['t%02d' % k]['last'] == 24 for k in range(16)) and not left,
          '16 个线程同时保存 25 次：没有出错，最后的文件是最新内容，不留临时文件（%s）' % (errs[:2] or left[:2] or 'ok'))
    with RM.LOCK:
        for k in range(16):
            RM.ST['devices'].pop('t%02d' % k, None)
    RM.api_disable({})


def main():
    tmp = tempfile.mkdtemp(prefix='cwb-r16r-run-')
    old_run, old_conf = S.RUN_DIR, S.CONF_PATH
    S.RUN_DIR = tmp
    shutil.copy(S.CONF_PATH, os.path.join(tmp, 'config.json'))
    S.CONF_PATH = os.path.join(tmp, 'config.json')
    RM.load()
    ts = FakeTS()
    try:
        test_detect(ts)
        ts.status()
        test_enable_disable(ts)
        test_auth(ts)
        test_tick_awake(ts)
        test_review_fixes(ts)
        test_css_scope()
    finally:
        ts.close()
        S.RUN_DIR, S.CONF_PATH = old_run, old_conf
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n' + ('全部通过' if not FAIL else '失败 %d 项：\n  ' % len(FAIL) + '\n  '.join(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
