"""第十六轮 R：通过 Tailscale 远程控制工作台（详见 README“第十六轮 R：远程控制”）。

- server.py 仍只监听 127.0.0.1。对外只靠 `tailscale serve --bg --https=<端口> http://127.0.0.1:<端口>`（只在 tailnet 内可达的 HTTPS）。
  工作台从不调用 `tailscale funnel`。开启前先读 `tailscale serve status --json`，只挑没被任何规则用的 HTTPS 端口，
  不覆盖用户已有的规则；关闭时用 `--set-path=/ off` 只撤自己加的那一条。
- 鉴权入口 gate()：Handler.do_GET / do_POST 的第一行调用。
  · Host 是 127.0.0.1/localhost：原样交回 server.py，沿用主令牌；但带着 tailscale serve 才会加的转发头、身份头的一律 403，
    所以主令牌即使泄露也不能经 Tailscale 用。
  · Host 是已开放的 ts.net 名：只认设备凭据（cookie，服务端只存 sha256）。通过后在进程内把这次请求改写成本机请求
    （Host 换成 127.0.0.1、补上 X-Token 或 ?t=），交回 server.py 原来的处理；主令牌只在进程内用，不写进任何响应。
  · 其他 Host：403（和原来一样）。
- 配对：一次性配对码 10 分钟有效、只能用一次，只在内存里存哈希。手机打开链接后确认，写入长期设备凭据
  （HttpOnly、Secure、SameSite=Strict）。已授权设备可改名、撤销；撤销立即生效，并断开它的事件流。
- 身份：tailscale serve 转发时带 Tailscale-User-Login（官方先删掉客户端自带的同名头，带标签的设备、Funnel 不带）。
  默认只允许和本机同一个 Tailscale 账号的设备；本机自己经 ts.net 访问时没有身份头，按来源地址是本机的 Tailscale IP 认作本机账号。
"""
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from email.header import decode_header, make_header
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse

NS = {}                      # server.py 的 globals()
CTX = threading.local()      # 当前请求来自哪台远程设备（CTX.dev），本机请求为 None
LOCK = threading.RLock()     # 状态、配对码、事件流登记
OP = threading.Lock()        # 开、关同一时间只做一个
ST = {}                      # 持久状态：.run/remote-<端口>.json（0600）
PAIR = {}                    # 当前配对码：hash、exp、used（只在内存，重启即失效）
SSE = {}                     # 设备 ID → 正在推事件的连接
TS = {'at': 0, 'busy': False, 'st': None}
AW = {'p': None}             # 防休眠的 caffeinate
TK = {'flush': 0, 'aw': 0, 'stale': 0}

PAIR_TTL = 600
COOKIE = 'cwb_dev'
COOKIE_AGE = 400 * 86400     # 浏览器允许的最长有效期
APP_TS = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'
PORTS = (443, 8443, 8444, 9443, 10443)
LOCAL = ('127.0.0.1', 'localhost')
ADMIN_DNS = 'https://login.tailscale.com/admin/dns'
DL_MAX = 512 * 1024 * 1024
# tailscale serve 转发时加的头（X-Forwarded-*：addProxyForwardedHeaders；Tailscale-*：addTailscaleIdentityHeaders 等）
FWD = ('x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto', 'forwarded', 'tailscale-user-login', 'tailscale-user-name',
       'tailscale-user-profile-pic', 'tailscale-headers-info', 'tailscale-funnel-request', 'tailscale-app-capabilities')
# 远程页面不能用的接口：会在电脑上打开程序、弹出选择框、重启服务，或者只给本机 CLI（cwb.py）、原生 App 用
BLOCK = {
    'reveal': '远程页面不能在电脑上打开访达', 'open-file': '远程页面不能在电脑上打开文件', 'open-editor': '远程页面不能在电脑上打开编辑器',
    'open-with': '远程页面不能在电脑上打开应用', 'open-url': '远程页面不能在电脑上打开网页', 'pick-files': '远程页面不能弹出电脑上的选择框',
    'add-folder': '远程页面不能弹出电脑上的选择框', 'skill-pick': '远程页面不能弹出电脑上的选择框', 'notify': '远程页面不能发电脑上的通知',
    'restart-service': '远程页面不能重启后台服务（电脑上没开 App 时，重启后没人把它拉起来）',
    'ui-open': '只给本机用', 'ui-web': '只给本机用', 'ui-shot': '只给本机用', 'ui-shot-ack': '只给本机用', 'ui-shot-done': '只给本机用',
}
REMOTE_OK = {'remote-me', 'remote-add-folder'}  # remote-* 里远程页面能用的（remote-forget 在 gate 里直接处理）
PUBLIC = {'/manifest.webmanifest', '/static/icon.svg', '/static/icon-192.png', '/static/icon-512.png', '/static/apple-touch-icon.png'}
MANIFEST = {
    'name': 'Claude 工作台', 'short_name': '工作台', 'lang': 'zh-CN', 'start_url': '/', 'scope': '/', 'display': 'standalone',
    'background_color': '#1a1a19', 'theme_color': '#1a1a19',
    'icons': [{'src': '/static/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
              {'src': '/static/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
              {'src': '/static/icon.svg', 'sizes': 'any', 'type': 'image/svg+xml'}],
}
DEF = {'enabled': False, 'host': '', 'port': 0, 'target': '', 'same_account': True, 'awake': False, 'since': 0,
       'self_login': '', 'self_ips': [], 'stale': None, 'devices': {}}


def log(*a):
    (NS.get('log') or print)(*a)


def sha(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def short(s, n=200):
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    return s[:n]


# ---------------------------------------------------------------- 状态文件
def state_path():
    return os.path.join(NS['RUN_DIR'], 'remote-%d.json' % NS['PORT'])


def load():
    d = {}
    try:
        with open(state_path(), encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        pass
    with LOCK:
        ST.clear()
        ST.update(json.loads(json.dumps(DEF)))
        if isinstance(d, dict):
            ST.update({k: v for k, v in d.items() if k in DEF})
        if not isinstance(ST.get('devices'), dict):
            ST['devices'] = {}


def save():
    """复核修复：整个保存过程持锁（取快照、写临时文件、替换都在锁里，新的不会被旧的覆盖），临时文件名带进程、线程和随机后缀。"""
    with LOCK:
        data = json.dumps(ST, ensure_ascii=False, indent=1)
        TK['dirty'] = False
        p = state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = '%s.%d-%d-%s.tmp' % (p, os.getpid(), threading.get_ident(), secrets.token_hex(3))
        try:
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as f:
                f.write(data)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def enabled():
    return bool(ST.get('enabled') and ST.get('host') and ST.get('port'))


def base_url():
    port = int(ST.get('port') or 443)
    return 'https://' + ST.get('host', '') + ('' if port == 443 else ':%d' % port)


# ---------------------------------------------------------------- tailscale 命令行
def ts_bin():
    """先找 PATH 里的 tailscale（测试用 PATH 注入假的；独立版装了命令行集成在 /usr/local/bin），再找 App Store 版 App 里带的那个。"""
    cands = []
    for path in (os.environ.get('PATH'), (NS.get('LOGIN_ENV') or {}).get('PATH')):
        if path:
            p = shutil.which('tailscale', path=path)
            if p:
                cands.append(p)
    cands += ['/usr/local/bin/tailscale', '/opt/homebrew/bin/tailscale', APP_TS]
    for p in cands:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ''


def _txt(x):
    return x.decode('utf-8', 'replace') if isinstance(x, bytes) else (x or '')


def ts_run(args, timeout=10):
    """返回 (退出码, 标准输出, 标准错误)；没装返回 None。TAILSCALE_BE_CLI=1：App 里那个可执行文件按命令行模式运行，不打开界面。"""
    b = ts_bin()
    if not b:
        return None
    env = dict(os.environ, TAILSCALE_BE_CLI='1')
    try:
        p = subprocess.run([b] + list(args), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        return p.returncode, p.stdout or '', p.stderr or ''
    except subprocess.TimeoutExpired as e:
        return -9, _txt(e.stdout), _txt(e.stderr)
    except OSError as e:
        return -1, '', str(e)


def ts_status():
    """读 `tailscale status --json`（ipnstate.Status）：BackendState、Self.DNSName（末尾带点）、Self.UserID → User[ID].LoginName、
    Self.TailscaleIPs、CertDomains（非空才有 HTTPS 证书）、CurrentTailnet.MagicDNSEnabled。"""
    b = ts_bin()
    out = {'installed': bool(b), 'bin': b, 'app': os.path.isfile(APP_TS), 'running': False, 'state': '', 'loggedIn': False,
           'dns': '', 'login': '', 'name': '', 'ips': [], 'https': False, 'magicdns': False, 'tailnet': '', 'version': '',
           'authUrl': '', 'error': ''}
    if not b:
        return out
    r = ts_run(['status', '--json'], 10)
    rc, so, se = r
    d = None
    try:
        d = json.loads(so) if so.strip() else None
    except ValueError:
        d = None
    if not isinstance(d, dict):
        out['error'] = short(se or so) or ('读取状态失败（%s）' % rc)
        return out
    me = d.get('Self') or {}
    up = (d.get('User') or {}).get(str(me.get('UserID'))) or {}
    tn = d.get('CurrentTailnet') or {}
    caps = list(me.get('Capabilities') or []) + list((me.get('CapMap') or {}).keys())
    st = d.get('BackendState') or ''
    out.update(running=st not in ('', 'NoState', 'Stopped'), state=st, loggedIn=st == 'Running',
               dns=(me.get('DNSName') or '').rstrip('.').lower(), login=up.get('LoginName') or '', name=up.get('DisplayName') or '',
               ips=[str(x) for x in (me.get('TailscaleIPs') or d.get('TailscaleIPs') or [])],
               https=bool(d.get('CertDomains')) or 'https' in caps, magicdns=bool(tn.get('MagicDNSEnabled')),
               tailnet=tn.get('Name') or '', version=d.get('Version') or '', authUrl=d.get('AuthURL') or '')
    if out['loggedIn'] and out['login']:
        with LOCK:
            if ST.get('self_login') != out['login'] or ST.get('self_ips') != out['ips']:
                ST['self_login'], ST['self_ips'] = out['login'], out['ips']
                TK['dirty'] = True
    TS.update(at=time.time(), st=out)
    return out


def serve_conf():
    """`tailscale serve status --json` 原样输出 ipn.ServeConfig（只含后台配置；Foreground 是前台会话的）。"""
    r = ts_run(['serve', 'status', '--json'], 10)
    if r is None:
        raise ValueError('这台电脑上没有找到 Tailscale')
    rc, so, se = r
    if rc != 0:
        raise ValueError('读取 Tailscale 的 serve 配置失败：' + (short(se or so) or str(rc)))
    so = so.strip()
    try:
        d = json.loads(so) if so else {}
    except ValueError:
        raise ValueError('Tailscale 的 serve 配置看不懂：' + short(so, 120))
    return d if isinstance(d, dict) else {}


def _confs(sc):
    yield sc
    for f in (sc.get('Foreground') or {}).values():
        if isinstance(f, dict):
            yield f


def port_used(sc, port):
    """这个端口有没有被任何规则用（TCP 监听、Web 处理、Funnel 设置；后台和前台会话都算）。"""
    for c in _confs(sc):
        if str(port) in (c.get('TCP') or {}):
            return True
        for hp in list((c.get('Web') or {}).keys()) + list((c.get('AllowFunnel') or {}).keys()):
            if str(hp).rsplit(':', 1)[-1] == str(port):
                return True
    return False


def _norm(u):
    return str(u or '').strip().rstrip('/').lower()


def ours(sc, host, port, target):
    hd = ((((sc.get('Web') or {}).get('%s:%d' % (host, port)) or {}).get('Handlers') or {}).get('/')) or {}
    return bool(target) and _norm(hd.get('Proxy')) == _norm(target)


def find_ours(sc, host, target):
    for hp in (sc.get('Web') or {}):
        h, _, p = str(hp).rpartition(':')
        if h == host and p.isdigit() and ours(sc, host, int(p), target):
            return int(p)
    return 0


def funnel_on(sc, host, port):
    return any(bool((c.get('AllowFunnel') or {}).get('%s:%d' % (host, port))) for c in _confs(sc))


def serve_off(host, port, target):
    """只撤工作台加的那一条：端口上 / 这个挂载点确实还指向本服务时，才运行 serve --https=N --set-path=/ off。"""
    sc = serve_conf()
    if not ours(sc, host, port, target):
        return 'gone'
    rc, so, se = ts_run(['serve', '--https=%d' % port, '--set-path=/', 'off'], 15)
    if ours(serve_conf(), host, port, target):
        raise ValueError('撤销 tailscale serve 规则没有成功：' + (short(se or so) or str(rc)))
    return 'off'


# ---------------------------------------------------------------- 请求鉴权
def hdr(h, name):
    """按名字取请求头；下划线和连字符算同一个（和 tailscale 的 delHeaderVariants 一致）。"""
    for k, v in h.items():
        if k.lower().replace('_', '-') == name:
            return v
    return None


def proxied(h):
    keys = {k.lower().replace('_', '-') for k in h.keys()}
    return any(k in keys for k in FWD)


def dec(v):
    """身份头里非 ASCII 的值是 RFC 2047 Q 编码（=?utf-8?q?...?=）。"""
    v = (v or '').strip()
    if '=?' not in v:
        return v
    try:
        return str(make_header(decode_header(v))).strip()
    except Exception:
        return v


def host_of(raw):
    raw = (raw or '').strip().lower()
    if ':' in raw and not raw.startswith('['):
        raw = raw.rsplit(':', 1)[0]
    return raw.rstrip('.')


def eff_login(login, ip):
    """这次请求算哪个 Tailscale 账号：有身份头用身份头；没有、但来源是本机自己的 Tailscale IP，算本机账号。"""
    if login:
        return login
    if ip and ip in (ST.get('self_ips') or []):
        return ST.get('self_login') or ''
    return ''


def ident_ok(login, ip):
    if not ST.get('same_account', True):
        return True, ''
    me = (ST.get('self_login') or '').lower()
    who = eff_login(login, ip)
    if not me:
        return False, '还没读到这台电脑登录的 Tailscale 账号，请在电脑上打开“设置 → 远程访问”看一下'
    if not who:
        return False, '认不出这台设备的 Tailscale 账号（带标签的设备、别人共享过来的设备都认不出）。只允许和电脑同一个 Tailscale 账号的设备'
    if who.lower() != me:
        return False, '这台设备登录的 Tailscale 账号（%s）和电脑上的不一样。只允许同一个账号的设备' % who
    return True, ''


def origin_ok(h, raw):
    """POST 只接受本页面自己发出的。有 Sec-Fetch-Site（浏览器自己加、网页改不了）就必须是 same-origin；
    Origin 有具体值时必须是 https://<这个 Host>（引用策略严格时表单提交的 Origin 是 null，这时以 Sec-Fetch-Site 为准）；
    两个都没有就拒绝（浏览器的 POST 至少带其中一个）。"""
    sfs = (h.headers.get('Sec-Fetch-Site') or '').strip().lower()
    o = (h.headers.get('Origin') or '').strip().lower()

    def n(x):
        return re.sub(r':443$', '', x)
    if o and o != 'null' and n(o) != n('https://' + raw.strip().lower()):
        return False
    if sfs:
        return sfs == 'same-origin'
    return bool(o) and o != 'null'


def device_of(h):
    c = h.headers.get('Cookie') or ''
    for part in c.split(';'):
        k, _, v = part.strip().partition('=')
        if k == COOKIE and '.' in v:
            did, _, sec = v.strip().strip('"').partition('.')
            with LOCK:
                d = (ST.get('devices') or {}).get(did)
                if d and sec and hmac.compare_digest(sha(sec), str(d.get('hash') or '')):
                    return dict(d, id=did)
    return None


def set_hdr(h, k, v):
    try:
        del h.headers[k]
    except KeyError:
        pass
    h.headers[k] = v


def send(h, code, body, ctype, extra=None):
    if isinstance(body, str):
        body = body.encode('utf-8')
    h.send_response(code)
    h.send_header('Content-Type', ctype)
    h.send_header('Content-Length', str(len(body)))
    h.send_header('Cache-Control', 'no-store')
    h.send_header('X-Content-Type-Options', 'nosniff')
    h.send_header('Referrer-Policy', 'same-origin')  # 不能用 no-referrer：那样表单提交的 Origin 会变成 null
    for k, v in (extra or []):
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)
    return True


def jsend(h, code, obj, extra=None):
    return send(h, code, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8', extra)


def wants_html(h, method, path):
    return method == 'GET' and (path == '/' or 'text/html' in (h.headers.get('Accept') or ''))


def deny(h, method, path, code, msg, title='不能访问'):
    h.close_connection = True  # 没读请求体就回绝的，别让这条连接接着用
    if wants_html(h, method, path):
        return send(h, code, page(title, '<p>%s</p>' % html.escape(msg or '这个地址不能访问')), 'text/html; charset=utf-8')
    return jsend(h, code, {'error': msg} if msg else {})


# 复核修复：远程（ts.net）来的响应一律禁止被别的页面嵌入（同一 tailnet 的其他站点、开了 Funnel 的站点可能算同站，
# 带着 Strict cookie 嵌进 iframe 有点击劫持可能）。只有 /file 例外：远程页面自己的 PDF 预览是同源 iframe，改为只许同源嵌入。
NOFRAME = (('X-Frame-Options', 'DENY'), ('Content-Security-Policy', "frame-ancestors 'none'"))
SELFFRAME = (('X-Frame-Options', 'SAMEORIGIN'), ('Content-Security-Policy', "frame-ancestors 'self'"))


def frame_guard(h, hdrs):
    """给这次请求的所有响应（含 server.py 原来的 _send、_sse、/img、send_error）在 end_headers 前补上头：用实例属性盖住类方法。"""
    end = type(h).end_headers

    def end_headers():
        for k, v in hdrs:
            h.send_header(k, v)
        end(h)
    h.end_headers = end_headers


def gate(h, method):
    """返回 True：已经回应（拒绝或远程专用的处理）；False：交给 server.py 原来的处理（本机请求，或已鉴权并改写过的远程请求）。"""
    CTX.dev = None
    h.__dict__.pop('end_headers', None)  # 同一条连接上的上一个请求如果是远程的，先去掉它加的那层；本机请求的响应不加任何头
    raw = h.headers.get('Host') or ''
    if raw.rsplit(':', 1)[0] in LOCAL:
        if proxied(h.headers):
            log('第十六轮 R：拒绝带转发头的本机地址请求（经 tailscale serve 来的不能用主令牌）', method, h.path[:60])
            h.close_connection = True
            h._json(403, {})
            return True
        return False
    try:
        return remote(h, method, raw)
    except Exception as e:
        log('远程请求处理异常', method, h.path[:80], repr(e))
        try:
            h.close_connection = True
            return jsend(h, 500, {'error': '远程访问出错：' + str(e)[:120]})
        except Exception:
            return True


def remote(h, method, raw):
    u = urlparse(h.path)
    path = u.path
    frame_guard(h, SELFFRAME if method == 'GET' and path == '/file' else NOFRAME)
    host = host_of(raw)
    if not enabled() or host != ST.get('host'):
        h.close_connection = True
        h._json(403, {})  # 和原来一样：不是本机、也不是已开放的 ts.net 名
        return True
    if hdr(h.headers, 'tailscale-funnel-request'):
        return deny(h, method, path, 403, '工作台不接受公网（Funnel）来的请求')
    if path.startswith('/hook/'):
        return deny(h, method, path, 403, '')
    if path == '/mcp' or path.startswith('/mcp/'):  # 第十七轮 M：协作服务只给本机的 CLI 用，远程一律拒绝（和 /hook/ 同一规则）
        return deny(h, method, path, 403, '')
    xfh = hdr(h.headers, 'x-forwarded-host')
    if xfh and xfh.strip().lower() != raw.strip().lower():
        return deny(h, method, path, 403, '')
    if method == 'GET' and path in PUBLIC:
        return public(h, path)
    if method == 'POST' and not origin_ok(h, raw):
        return deny(h, method, path, 403, '请求来源不对')
    login = dec(hdr(h.headers, 'tailscale-user-login'))
    ip = (hdr(h.headers, 'x-forwarded-for') or '').split(',')[0].strip()
    ok, why = ident_ok(login, ip)
    if not ok:
        return deny(h, method, path, 403, why)
    if path == '/pair':
        return pair_http(h, method, u, eff_login(login, ip), ip)
    dev = device_of(h)
    if not dev:
        return unpaired(h, method, path)
    who = eff_login(login, ip)
    if dev.get('login') and who.lower() != str(dev['login']).lower():
        return deny(h, method, path, 403, '这台设备配对时用的 Tailscale 账号和现在的不一样，请在电脑上重新配对')
    CTX.dev = dev
    touch(dev['id'], ip)
    if method == 'GET' and path == '/r/dl':
        return download(h, u)
    if method == 'POST' and path == '/api/remote-forget':
        revoke(dev['id'], '这台设备自己取消了授权')
        h.close_connection = True
        return jsend(h, 200, {'ok': True}, [('Set-Cookie', '%s=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict' % COOKIE)])
    if method == 'POST' and path.startswith('/api/'):
        name = path[len('/api/'):]
        if name in BLOCK:
            return deny(h, method, path, 403, BLOCK[name])
        if name.startswith('remote-') and name not in REMOTE_OK:
            return deny(h, method, path, 403, '远程访问的设置只能在电脑上改')
    # 改写成本机请求，交给 server.py 原来的处理（主令牌只在进程内用）
    set_hdr(h, 'Host', '127.0.0.1:%d' % NS['PORT'])
    if method == 'POST':
        set_hdr(h, 'X-Token', NS['TOKEN'])
    elif path in ('/events', '/img', '/file'):
        q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != 't'] + [('t', NS['TOKEN'])]
        h.path = path + '?' + urlencode(q)
        if path == '/events':
            with LOCK:
                socks = SSE.setdefault(dev['id'], set())
                socks.difference_update([s for s in socks if s.fileno() == -1])  # 顺手清掉已经关掉的连接
                socks.add(h.connection)
    return False


def touch(did, ip):
    with LOCK:
        d = (ST.get('devices') or {}).get(did)
        if d:
            d['last'] = time.time()
            if ip:
                d['ip'] = ip
            TK['dirty'] = True


def public(h, path):
    if path == '/manifest.webmanifest':
        return send(h, 200, json.dumps(MANIFEST, ensure_ascii=False), 'application/manifest+json; charset=utf-8')
    p = os.path.join(NS['STATIC_DIR'], path[len('/static/'):])
    if not os.path.isfile(p):
        return jsend(h, 404, {})
    with open(p, 'rb') as f:
        return send(h, 200, f.read(), 'image/svg+xml' if p.endswith('.svg') else 'image/png')


def unpaired(h, method, path):
    h.close_connection = method == 'POST'
    if wants_html(h, method, path):
        # SameSite=Strict：从别的 App、别的网站点进来的第一次请求不带 cookie。先用同源的跳转再试一次（_r=1），还没有才显示说明。
        retry = ('<script>(()=>{const u=new URL(location.href);if(!u.searchParams.has("_r")){u.searchParams.set("_r","1");'
                 'location.replace(u.href);}else document.documentElement.classList.add("shown")})()</script>')
        # 加到主屏幕的网页 App 可能和浏览器不共用 cookie、又没有地址栏：可以把配对链接粘贴到这里
        paste = ('<script>document.addEventListener("submit",e=>{if(e.target.id!=="pc")return;e.preventDefault();'
                 'const v=e.target.elements.c.value.trim(),m=/[?&]c=([^&#\\s]+)/.exec(v),c=m?m[1]:v;'
                 'if(c)location.href="/pair?c="+encodeURIComponent(decodeURIComponent(c));});</script>')
        body = page('这台设备还没有配对',
                    '<p>要用这台设备控制电脑上的 Claude 工作台，先配对一次：</p><ol><li>在电脑上打开 Claude 工作台，进入“设置 → 远程访问”。</li>'
                    '<li>点“配对新设备”，用这台设备扫二维码，或者打开配对链接。</li></ol>'
                    '<form id="pc"><label>已经有配对链接？粘贴到这里<input name="c" autocomplete="off" spellcheck="false" placeholder="https://…/pair?c=…"></label>'
                    '<button type="submit">打开配对</button></form><p class="dim">如果刚被撤销了授权，也需要重新配对。</p>',
                    retry + paste, hide=True)
        return send(h, 401, body, 'text/html; charset=utf-8')
    return jsend(h, 401, {'error': '这台设备还没有配对，或者授权已被撤销', 'remote': 'unpaired'})


# ---------------------------------------------------------------- 配对
def pair_check(code):
    if not code or not PAIR.get('hash'):
        return '配对链接无效。请在电脑上重新生成'
    if not hmac.compare_digest(sha(code), PAIR['hash']):
        return '配对链接无效或已经换过了。请在电脑上重新生成'
    if PAIR.get('used'):
        return '这个配对链接已经用过了，只能用一次。请在电脑上重新生成'
    if time.time() > PAIR.get('exp', 0):
        return '配对链接已过期（10 分钟内有效）。请在电脑上重新生成'
    return ''


def guess(ua):
    ua = ua or ''
    for k, v in (('iPhone', 'iPhone'), ('iPad', 'iPad')):
        if k in ua:
            return v
    if 'Android' in ua:
        return 'Android 手机' if 'Mobile' in ua else 'Android 平板'
    for k, v in (('Macintosh', 'Mac'), ('Windows', 'Windows 电脑'), ('Linux', 'Linux 电脑')):
        if k in ua:
            return v
    return '新设备'


def clean_name(s):
    s = re.sub(r'[\x00-\x1f\x7f]', '', str(s or '')).strip()
    return s[:40]


def pair_http(h, method, u, login, ip):
    ua = h.headers.get('User-Agent') or ''
    if method == 'GET':
        code = (parse_qs(u.query).get('c') or [''])[0]
        with LOCK:
            err = pair_check(code)
        if err:
            return send(h, 410, page('配对链接不能用', '<p>%s</p>' % html.escape(err)), 'text/html; charset=utf-8')
        body = page('把这台设备连到 Claude 工作台',
                    '<p>配对后，这台设备可以查看和操作电脑上的所有对话。只在你自己的设备上确认。</p>'
                    '<form method="post" action="/pair"><input type="hidden" name="c" value="%s">'
                    '<label>设备名称<input name="name" maxlength="40" value="%s" autocomplete="off"></label>'
                    '<button type="submit">确认配对</button></form><p class="dim">链接 10 分钟内有效，只能用一次。%s</p>'
                    % (html.escape(code), html.escape(guess(ua)), ('Tailscale 账号：' + html.escape(login)) if login else ''))
        return send(h, 200, body, 'text/html; charset=utf-8')
    if method != 'POST':
        return deny(h, method, '/pair', 405, '')
    n = int(h.headers.get('Content-Length') or 0)
    if n < 0 or n > 8192:
        return deny(h, method, '/pair', 413, '请求太大')
    raw = h.rfile.read(n).decode('utf-8', 'replace') if n else ''
    if 'json' in (h.headers.get('Content-Type') or ''):
        try:
            f = json.loads(raw or '{}')
        except ValueError:
            f = {}
    else:
        f = {k: v for k, v in parse_qsl(raw, keep_blank_values=True)}
    code, name = str(f.get('c') or ''), clean_name(f.get('name')) or guess(ua)
    with LOCK:
        err = pair_check(code)
        if not err:
            PAIR['used'] = True
            did, sec = secrets.token_hex(6), secrets.token_urlsafe(32)
            ST['devices'][did] = {'name': name, 'hash': sha(sec), 'created': time.time(), 'last': time.time(),
                                  'login': login or '', 'ip': ip, 'ua': ua[:200]}
    if err:
        h.close_connection = True
        return send(h, 410, page('配对没有成功', '<p>%s</p>' % html.escape(err)), 'text/html; charset=utf-8')
    save()
    log('第十六轮 R：新设备已配对', did, name, login or '（无账号头）', ip)
    NS['BUS'].emit({'type': 'remote', 'what': 'paired', 'id': did, 'name': name})
    cookie = '%s=%s.%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Strict' % (COOKIE, did, sec, COOKIE_AGE)
    h.send_response(303)
    h.send_header('Location', '/')
    h.send_header('Set-Cookie', cookie)
    h.send_header('Content-Length', '0')
    h.send_header('Cache-Control', 'no-store')
    h.end_headers()
    return True


def revoke(did, why=''):
    with LOCK:
        d = ST['devices'].pop(did, None)
    if d is None:
        return False
    save()
    drop_sse(did)
    log('第十六轮 R：已撤销设备', did, d.get('name'), why)
    NS['BUS'].emit({'type': 'remote', 'what': 'revoked', 'id': did})
    return True


def drop_sse(did=None):
    """断开设备的事件流：shutdown 让对方立即收到断开；server 那边的推送线程在下一次写入时退出。"""
    with LOCK:
        keys = list(SSE) if did is None else [did]
        socks = [s for k in keys for s in SSE.pop(k, set())]
    n = 0
    for s in socks:
        try:
            if s.fileno() != -1:
                s.shutdown(socket.SHUT_RDWR)
                n += 1
        except OSError:
            pass
    return n


def download(h, u):
    q = parse_qs(u.query)
    p = NS['resolve_path']((q.get('p') or [''])[0], (q.get('cwd') or [''])[0])
    if not p or not os.path.isfile(p):
        return jsend(h, 404, {'error': '找不到这个文件'})
    size = os.path.getsize(p)
    if size > DL_MAX:
        return jsend(h, 413, {'error': '文件超过 512MB，不能下载'})
    name = os.path.basename(p)
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', '_', name) or 'file'
    h.send_response(200)
    h.send_header('Content-Type', mimetypes.guess_type(name)[0] or 'application/octet-stream')
    h.send_header('Content-Length', str(size))
    h.send_header('Content-Disposition', "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, quote(name)))
    h.send_header('Cache-Control', 'no-store')
    h.send_header('X-Content-Type-Options', 'nosniff')
    h.end_headers()
    with open(p, 'rb') as f:
        while True:
            b = f.read(256 * 1024)
            if not b:
                break
            h.wfile.write(b)
    return True


PAGE_CSS = (':root{color-scheme:light dark;--bg:#fff;--fg:#1f1f1e;--dim:#77756f;--line:#e3e1dc;--acc:#2f6bd8}'
            '@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--fg:#ececea;--dim:#9a9893;--line:#34332f;--acc:#7aa5f8}}'
            'html.hide body{visibility:hidden}html.hide.shown body{visibility:visible}'
            'body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}'
            'main{max-width:440px;margin:0 auto;padding:calc(40px + env(safe-area-inset-top)) 20px 40px}'
            'h1{font-size:21px;line-height:1.4;margin:0 0 12px}p,li{color:var(--fg)}.dim{color:var(--dim);font-size:14px}'
            'ol{padding-left:22px}label{display:block;margin:18px 0 6px;color:var(--dim);font-size:14px}'
            'form input{display:block;box-sizing:border-box;width:100%;margin-top:6px;font:inherit;padding:11px 12px;border-radius:10px;'
            'border:1px solid var(--line);background:transparent;color:var(--fg)}'
            'button{display:block;width:100%;margin:18px 0 8px;min-height:48px;border:0;border-radius:12px;background:var(--acc);'
            'color:#fff;font:600 17px/1 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}')


def page(title, inner, script='', hide=False):
    return ('<!doctype html><html lang="zh-CN"%s><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<title>%s · Claude 工作台</title><style>%s</style>%s</head><body><main><h1>%s</h1>%s</main></body></html>'
            % (' class="hide"' if hide else '', html.escape(title), PAGE_CSS, script, html.escape(title), inner))


# ---------------------------------------------------------------- 接口（除 remote-me、remote-add-folder 外只给本机用）
def dev_list():
    with LOCK:
        ds = [dict(id=k, name=v.get('name') or '', created=v.get('created') or 0, last=v.get('last') or 0, login=v.get('login') or '')
              for k, v in (ST.get('devices') or {}).items()]
    ds.sort(key=lambda d: -d['created'])
    with LOCK:
        for d in ds:
            d['online'] = any(s.fileno() != -1 for s in SSE.get(d['id'], ()))
    return ds


def status_payload(ts=None):
    ts = ts if ts is not None else ts_status()
    rm = {'enabled': enabled(), 'url': base_url() + '/' if enabled() else '', 'host': ST.get('host') or '', 'port': ST.get('port') or 0,
          'sameAccount': bool(ST.get('same_account', True)), 'awake': bool(ST.get('awake')),
          'awakeOn': bool(AW['p'] is not None and AW['p'].poll() is None), 'since': ST.get('since') or 0,
          'selfLogin': ST.get('self_login') or '', 'active': None, 'funnel': False, 'stale': bool(ST.get('stale'))}
    if enabled() and ts.get('running'):
        try:
            sc = serve_conf()
            rm['active'] = ours(sc, ST['host'], int(ST['port']), ST.get('target'))
            rm['funnel'] = funnel_on(sc, ST['host'], int(ST['port']))
        except ValueError as e:
            rm['error'] = str(e)
    pair = None
    with LOCK:
        if PAIR.get('hash') and not PAIR.get('used') and time.time() < PAIR.get('exp', 0):
            pair = {'exp': PAIR['exp']}
    return {'ts': ts, 'remote': rm, 'devices': dev_list(), 'pair': pair}


def api_status(b):
    retry_stale()
    return status_payload()


def api_enable(b):
    if not OP.acquire(timeout=1):
        raise ValueError('正在处理上一次操作，请稍等')
    try:
        ts = ts_status()
        if not ts['installed']:
            raise ValueError('这台电脑还没有安装 Tailscale')
        if not ts['running']:
            raise ValueError('Tailscale 没有在运行，请先打开 Tailscale' + ('：' + ts['error'] if ts['error'] else ''))
        if not ts['loggedIn']:
            raise ValueError('还没有登录 Tailscale，请先在 Tailscale 里登录')
        if not ts['dns']:
            raise ValueError('没有读到这台电脑的 Tailscale 名称')
        host, target = ts['dns'], 'http://127.0.0.1:%d' % NS['PORT']
        sc = serve_conf()
        port = find_ours(sc, host, target)
        if not port:
            port = next((p for p in PORTS if not port_used(sc, p)), 0)
            if not port:
                raise ValueError('%s 这几个端口都已经有你自己的 tailscale serve 规则，工作台没有改动它们' % '、'.join(map(str, PORTS)))
            log('第十六轮 R：开放远程访问', 'tailscale serve --bg --https=%d %s' % (port, target))
            rc, so, se = ts_run(['serve', '--bg', '--https=%d' % port, target], 20)
            sc = serve_conf()
            if not ours(sc, host, port, target):
                m = re.search(r'https://login\.tailscale\.com/\S+', (so or '') + '\n' + (se or ''))
                if m or not ts['https']:
                    return dict(status_payload(ts), ok=False, need='https', url=m.group(0) if m else ADMIN_DNS)
                raise ValueError('开放没有成功：' + (short(se or so) or str(rc)))
        if funnel_on(sc, host, port):
            raise ValueError('端口 %d 被设成了公网访问（Funnel）。工作台只在 tailnet 内开放，请先在终端运行 tailscale funnel --https=%d off' % (port, port))
        with LOCK:
            ST.update(enabled=True, host=host, port=port, target=target, since=time.time(), self_login=ts['login'], self_ips=ts['ips'])
        save()
        awake_apply()
        return dict(status_payload(ts), ok=True)
    finally:
        OP.release()


def api_disable(b):
    if not OP.acquire(timeout=1):
        raise ValueError('正在处理上一次操作，请稍等')
    try:
        host, port, target = ST.get('host'), int(ST.get('port') or 0), ST.get('target')
        note, stale = '', None
        if host and port and target:
            try:
                if serve_off(host, port, target) == 'gone':
                    note = '端口 %d 上已经不是工作台加的规则，没有改动' % port
            except ValueError as e:
                stale = {'host': host, 'port': port, 'target': target}
                note = str(e) + '。等 Tailscale 能用时会再撤一次；在这之前远程请求一律拒绝'
        with LOCK:
            ST.update(enabled=False, stale=stale)
            PAIR.clear()
        save()
        drop_sse()
        awake_apply()
        log('第十六轮 R：已关闭远程访问', note)
        return dict(status_payload(), ok=True, note=note)
    finally:
        OP.release()


def retry_stale():
    """关闭时没撤掉的规则（当时 Tailscale 不能用），之后再撤一次；还开着远程就不动。"""
    s = ST.get('stale')
    if not s or enabled() or not OP.acquire(blocking=False):
        return
    try:
        serve_off(s['host'], int(s['port']), s['target'])
        with LOCK:
            ST['stale'] = None
        save()
    except Exception:
        pass
    finally:
        OP.release()


def api_set(b):
    with LOCK:
        if 'sameAccount' in b:
            ST['same_account'] = bool(b['sameAccount'])
        if 'awake' in b:
            ST['awake'] = bool(b['awake'])
    save()
    awake_apply()
    return status_payload(TS['st'] or ts_status())


def api_pair(b):
    if not enabled():
        raise ValueError('先打开“通过 Tailscale 开放”')
    code = secrets.token_urlsafe(24)
    with LOCK:
        PAIR.clear()
        PAIR.update(hash=sha(code), exp=time.time() + PAIR_TTL, used=False)
    return {'url': base_url() + '/pair?c=' + code, 'exp': PAIR['exp'], 'ttl': PAIR_TTL}


def api_pair_cancel(b):
    with LOCK:
        PAIR.clear()
    return {'ok': True}


def api_rename(b):
    did, name = str(b.get('id') or ''), clean_name(b.get('name'))
    if not name:
        raise ValueError('名称不能为空')
    with LOCK:
        d = ST['devices'].get(did)
        if not d:
            raise ValueError('找不到这台设备')
        d['name'] = name
    save()
    return {'devices': dev_list()}


def api_revoke(b):
    if not revoke(str(b.get('id') or ''), '在电脑上撤销'):
        raise ValueError('找不到这台设备')
    return {'devices': dev_list()}


def api_me(b):
    d = getattr(CTX, 'dev', None)
    if not d:
        return {'remote': False}
    return {'remote': True, 'id': d['id'], 'name': d.get('name') or '', 'created': d.get('created') or 0, 'login': d.get('login') or ''}


def api_add_folder(b):
    """远程页面没有电脑上的选择框：直接填文件夹路径（本机也能用）。"""
    p = os.path.abspath(os.path.expanduser(str(b.get('path') or '').strip())).rstrip('/') or '/'
    if not str(b.get('path') or '').strip() or not os.path.isdir(p):
        raise ValueError('文件夹不存在：' + str(b.get('path') or ''))
    c = NS['load_conf']()
    if p not in c.setdefault('folders', []):
        c['folders'].append(p)
        NS['save_conf'](c)
    return {'cwd': p}


# ---------------------------------------------------------------- 定时：远程开着时不空闲退出、防休眠、写回最近访问
def keep_alive():
    return enabled()


def awake_apply():
    want = enabled() and bool(ST.get('awake'))
    p = AW['p']
    alive = p is not None and p.poll() is None
    if want and not alive:
        AW['p'] = NS['sched_awake_start']()  # 复用定时任务的做法：caffeinate -i -s -w <服务 PID>
        if AW['p']:
            log('第十六轮 R：远程开放期间防止电脑空闲休眠', AW['p'].pid)
    elif not want and p is not None:
        NS['sched_awake_stop'](p)
        AW['p'] = None


def tick(now):
    """poll_loop 每 0.4 秒调一次。远程开着时刷新 BUS.last_seen：服务不会因为“30 分钟没有窗口连接”而退出。"""
    if enabled():
        NS['BUS'].last_seen = now
    if TK.get('dirty') and now - TK['flush'] > 60:
        TK['flush'] = now
        try:
            save()
        except OSError:
            pass
    if now - TK['aw'] > 5:
        TK['aw'] = now
        awake_apply()
    if enabled() and now - TS['at'] > 600 and not TS['busy']:
        TS['busy'] = True
        TS['at'] = now

        def run():
            try:
                ts_status()
            except Exception:
                pass
            finally:
                TS['busy'] = False
        threading.Thread(target=run, daemon=True).start()
    if ST.get('stale') and now - TK['stale'] > 300:
        TK['stale'] = now
        threading.Thread(target=retry_stale, daemon=True).start()


def setup(ns):
    global NS
    NS = ns
    load()
    real_osa = ns['osa']

    def osa(script):
        if getattr(CTX, 'dev', None):  # 远程请求：不在电脑屏幕上弹选择框（add-dir 等不带路径时）
            log('第十六轮 R：远程请求不弹电脑上的选择框')
            return ''
        return real_osa(script)
    ns['osa'] = osa
    ns['API'].update({'remote-status': api_status, 'remote-enable': api_enable, 'remote-disable': api_disable, 'remote-set': api_set,
                      'remote-pair': api_pair, 'remote-pair-cancel': api_pair_cancel, 'remote-rename': api_rename,
                      'remote-revoke': api_revoke, 'remote-me': api_me, 'remote-add-folder': api_add_folder})
