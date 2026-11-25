"""第十五轮 G：网页预览里点选元素用的本机转发。

为什么要转发：网页预览的 iframe 是 http://localhost:<端口>，和工作台（127.0.0.1:<工作台端口>）不同源，
浏览器不让工作台往里注入脚本。这里按需为某个本机端口起一个只监听 127.0.0.1 的转发口，把页面原样转过去，
只在 HTML 文档的 <head> 开头插一行 <script src="/__cwb_pick.js">，点选脚本与工作台之间用 postMessage 通信。
转发口和工作台不同源（端口不同），被预览页面的脚本碰不到工作台的令牌和接口。

安全边界：
- 只转发本机回环地址：http 协议，主机只认 localhost / 127.0.0.1 / ::1 / 0.0.0.0（后者按本机连），不转发工作台自己的端口和转发口。
- 每个转发口的目标在创建时固定，请求只能是以 / 开头的路径；不接受绝对地址或 CONNECT，不能当通用代理用。
- 只接受 Host 是 127.0.0.1:<转发口> 或 localhost:<转发口> 的请求（防 DNS 重绑定）；Sec-Fetch-Site 为 cross-site 的请求一律拒绝。
- Origin / Referer 只在等于转发口自身地址时改写成目标地址；其他来源原样转给目标，目标自己的跨域检查照常生效。
- 任何异常只影响这次请求；转发口最多同时开 MAX_FW 个，超出关掉最久没用的。
"""
import http.client
import json
import os
import re
import select
import socket
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

SCRIPT_PATH = '/__cwb_pick.js'
CFG_MARK = '/*CWB_PICK_CFG*/{}'
MAX_FW = 8
MAX_HTML = 8 * 1024 * 1024
MAX_BODY = 64 * 1024 * 1024
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 600
LOOP_HOSTS = {'localhost': ('127.0.0.1', '::1'), '0.0.0.0': ('127.0.0.1', '::1'), '127.0.0.1': ('127.0.0.1',), '::1': ('::1',)}
HOP = {'connection', 'keep-alive', 'proxy-connection', 'proxy-authorization', 'proxy-authenticate', 'te', 'trailer',
       'transfer-encoding', 'upgrade'}

_LOCK = threading.Lock()
_FW = {}
CONF = {'port': 0, 'static': os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'), 'log': None}


def setup(port, static_dir=None, log=None):
    CONF['port'] = int(port or 0)
    if static_dir:
        CONF['static'] = static_dir
    CONF['log'] = log


def _log(*a):
    f = CONF.get('log')
    if f:
        try:
            f('点选转发', *a)
        except Exception:
            pass


def own_ports():
    with _LOCK:
        return {fw.lport for fw in _FW.values()}


class Target:
    __slots__ = ('host', 'port', 'conn', 'host_header', 'path', 'origin', 'key')

    def __init__(self, host, port, conn, host_header, path, origin):
        self.host, self.port, self.conn, self.host_header, self.path, self.origin = host, port, conn, host_header, path, origin
        self.key = (conn, port, host_header)


def parse_target(url):
    """校验预览地址，只放行本机回环 http 地址。不合格抛 ValueError（中文原因，直接显示在按钮提示里）。"""
    if not isinstance(url, str) or not url.strip() or len(url) > 4096:
        raise ValueError('地址无效')
    url = url.strip()
    if any(c in url for c in '\r\n\t\0'):
        raise ValueError('地址无效')
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise ValueError('地址无效')
    if u.scheme.lower() != 'http':
        raise ValueError('只支持本机 http 页面')
    if u.username is not None or u.password is not None or '@' in u.netloc:
        raise ValueError('只支持本机 http 页面')
    host = (u.hostname or '').lower()
    if host not in LOOP_HOSTS:
        raise ValueError('只支持本机 http 页面')
    port = 80 if port is None else port
    if not 0 < port < 65536:
        raise ValueError('地址无效')
    if port == CONF['port'] or port in own_ports():
        raise ValueError('这个地址不支持点选')
    hh = ('[::1]' if host == '::1' else host) + ('' if u.port is None else ':%d' % port)
    path = (u.path or '/') + ('?' + u.query if u.query else '')
    if not path.startswith('/'):
        path = '/' + path
    origin = 'http://' + hh
    return Target(host, port, LOOP_HOSTS[host], hh, path, origin)


def probe(t):
    """依次试连目标（localhost 先 IPv4 再 IPv6），返回连得上的那个地址。"""
    for h in t.conn:
        try:
            s = socket.create_connection((h, t.port), timeout=1.5)
            s.close()
            return h
        except OSError:
            continue
    raise ValueError('连不上 %s' % t.host_header)


def get_forwarder(t):
    up = probe(t)
    with _LOCK:
        fw = _FW.get(t.key)
        if fw and fw.alive:
            fw.up = up
            fw.touch()
            return fw
        while len(_FW) >= MAX_FW:
            k, old = min(_FW.items(), key=lambda kv: kv[1].used)
            _FW.pop(k, None)
            old.close()
        fw = Forwarder(t, up)
        _FW[t.key] = fw
        return fw


def api_wp_pick(b):
    b = b or {}
    t = parse_target(b.get('url'))
    try:
        fw = get_forwarder(t)
    except ValueError:
        raise  # 连不上等：原因直接给按钮提示
    except Exception as e:
        _log('起转发失败', repr(e))
        raise ValueError('暂时无法点选')
    # 转发地址的主机名跟工作台页面一致（127.0.0.1 或 localhost），iframe 与工作台同站，Sec-Fetch-Site 不会是 cross-site
    origin = fw.selves[1] if b.get('host') == 'localhost' else fw.selves[0]
    return {'src': origin + t.path, 'origin': origin, 'target': t.origin}


def close_all():
    with _LOCK:
        items = list(_FW.values())
        _FW.clear()
    for fw in items:
        fw.close()


# ---------------------------------------------------------------- HTML 注入
_HEAD = re.compile(rb'<head(?:\s[^>]*)?>', re.I)
_HTML = re.compile(rb'<html(?:\s[^>]*)?>', re.I)
_DOCTYPE = re.compile(rb'<!doctype[^>]*>', re.I)
_CHARSET = re.compile(rb'<meta[^>]+charset[^>]*>', re.I)
_NONCE = re.compile(r"'nonce-([A-Za-z0-9+/=_-]{1,200})'")
_META_CSP = re.compile(rb'<meta[^>]+http-equiv\s*=\s*["\']?content-security-policy[^>]*>', re.I)


def csp_nonce(headers, html=b''):
    """页面 CSP 带 nonce 时把同一个 nonce 给注入的脚本（同源脚本 'self' 本来就放行）。"""
    for k, v in headers:
        if k.lower() == 'content-security-policy':
            m = _NONCE.search(v)
            if m:
                return m.group(1)
    m = _META_CSP.search(html[:65536])
    if m:
        n = _NONCE.search(m.group(0).decode('latin-1'))
        if n:
            return n.group(1)
    return ''


def inject_html(raw, nonce=''):
    tag = b'<script src="' + SCRIPT_PATH.encode() + b'"' + ((b' nonce="' + nonce.encode() + b'"') if nonce else b'') + b'></script>'
    head = raw[:262144]
    m = _HEAD.search(head)
    if m:
        c = _CHARSET.search(head, m.end(), m.end() + 1024)
        at = c.end() if c else m.end()  # meta charset 要留在前 1024 字节里
        return raw[:at] + tag + raw[at:]
    for rx in (_HTML, _DOCTYPE):
        m = rx.search(head)
        if m:
            return raw[:m.end()] + tag + raw[m.end():]
    return tag + raw


def fail_page(reason):
    return ('<!doctype html><meta charset="utf-8"><script src="%s?fail=%s"></script>' % (SCRIPT_PATH, reason)).encode()


def read_chunked(f, cap):
    out = bytearray()
    while True:
        line = f.readline(1024)
        if not line:
            raise ValueError('请求体不完整')
        n = int(line.split(b';', 1)[0].strip() or b'0', 16)
        if n == 0:
            while True:
                t = f.readline(8192)
                if not t or t in (b'\r\n', b'\n'):
                    break
            return bytes(out)
        if len(out) + n > cap:
            raise OverflowError
        out += f.read(n)
        f.readline(8)


def decode_body(data, enc):
    if enc in ('', 'identity'):
        return data
    if enc in ('gzip', 'x-gzip'):
        return zlib.decompress(data, 16 + zlib.MAX_WBITS)
    if enc == 'deflate':
        try:
            return zlib.decompress(data)
        except zlib.error:
            return zlib.decompress(data, -zlib.MAX_WBITS)
    raise ValueError(enc)


# ---------------------------------------------------------------- 转发口
class Forwarder:
    def __init__(self, t, up):
        self.t, self.up = t, up
        self.used = time.time()
        self.alive = True
        srv = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        srv.daemon_threads = True
        srv.fw = self
        self.srv = srv
        self.lport = srv.server_address[1]
        self.origin = 'http://127.0.0.1:%d' % self.lport
        self.selves = ('http://127.0.0.1:%d' % self.lport, 'http://localhost:%d' % self.lport)
        self.hosts = ('127.0.0.1:%d' % self.lport, 'localhost:%d' % self.lport)
        self.targets = tuple('http://%s%s' % (h, '' if t.port == 80 else ':%d' % t.port)
                             for h in ('localhost', '127.0.0.1', '[::1]', '0.0.0.0'))
        threading.Thread(target=self._serve, daemon=True, name='cwb-pick-%d' % self.lport).start()

    def _serve(self):
        try:
            self.srv.serve_forever(poll_interval=0.5)
        except Exception as e:
            _log('转发口退出', repr(e))
        finally:
            self.alive = False

    def touch(self):
        self.used = time.time()

    def close(self):
        self.alive = False

        def stop():
            try:
                self.srv.shutdown()
                self.srv.server_close()
            except Exception:
                pass
        threading.Thread(target=stop, daemon=True).start()

    def to_target(self, v):
        for s in self.selves:
            if v == s or v.startswith(s + '/'):
                return self.t.origin + v[len(s):]
        return v

    def to_self(self, v, own=None):
        for s in self.targets:
            if v == s or v.startswith(s + '/') or v.startswith(s + '?'):
                return (own or self.origin) + v[len(s):]
        return v

    def script(self, fail=''):
        with open(os.path.join(CONF['static'], 'ux-m-frame.js'), encoding='utf-8') as f:
            src = f.read()
        p = CONF['port']
        cfg = {'parents': ['http://127.0.0.1:%d' % p, 'http://localhost:%d' % p], 'fail': fail}
        return src.replace(CFG_MARK, json.dumps(cfg)).encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'cwb-pick'
    timeout = 120

    def log_message(self, *a):
        pass

    def _plain(self, code, body=b'', ctype='text/plain; charset=utf-8'):
        self.close_connection = True
        self.send_response_only(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD' and body:
            self.wfile.write(body)

    def _handle(self):
        fw = self.server.fw
        self.close_connection = True
        try:
            if (self.headers.get('Host') or '').lower() not in fw.hosts:
                return self._plain(403)
            if not self.path.startswith('/'):
                return self._plain(400)
            if (self.headers.get('Sec-Fetch-Site') or '').lower() == 'cross-site':
                return self._plain(403)
            fw.touch()
            if self.path.split('?', 1)[0] == SCRIPT_PATH:
                q = self.path.split('?', 1)[1] if '?' in self.path else ''
                fail = 'up' if 'fail=up' in q else ''
                return self._plain(200, fw.script(fail), 'text/javascript; charset=utf-8')
            if 'websocket' in (self.headers.get('Upgrade') or '').lower():
                return self._tunnel(fw)
            return self._forward(fw)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError):
            pass
        except Exception as e:
            _log('转发异常', self.command, self.path[:120], repr(e))
            try:
                self._plain(502)
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

    def do_CONNECT(self):
        self._plain(405)

    do_TRACE = do_CONNECT

    def _is_doc(self):
        dest = (self.headers.get('Sec-Fetch-Dest') or '').lower()
        if dest:
            return dest in ('document', 'iframe', 'frame')
        return self.command == 'GET' and 'text/html' in (self.headers.get('Accept') or '')

    def _req_headers(self, fw, upgrade=False, doc=False):
        drop = {x.strip().lower() for x in (self.headers.get('Connection') or '').split(',') if x.strip()}
        out = []
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP or lk == 'host' or lk == 'content-length' or (lk in drop and not (upgrade and lk == 'upgrade')):
                continue
            if lk in ('origin', 'referer'):
                v = fw.to_target(v)
            elif lk == 'accept-encoding' and doc:
                continue  # 文档要插脚本：请目标返回不压缩的 HTML
            out.append((k, v))
        out.append(('Host', fw.t.host_header))
        if upgrade:
            out += [('Connection', 'Upgrade'), ('Upgrade', self.headers.get('Upgrade'))]
        else:
            out.append(('Connection', 'close'))
        return out

    def _read_body(self):
        if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
            return read_chunked(self.rfile, MAX_BODY)
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
            raise OverflowError
        if n < 0:
            raise ValueError('bad length')
        return self.rfile.read(n) if n else (b'' if self.headers.get('Content-Length') is not None else None)

    def _forward(self, fw):
        doc = self._is_doc()
        try:
            body = self._read_body()
        except OverflowError:
            return self._plain(413)
        conn = http.client.HTTPConnection(fw.up, fw.t.port, timeout=CONNECT_TIMEOUT)
        try:
            try:
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for k, v in self._req_headers(fw, doc=doc):
                    conn.putheader(k, v)
                if body is not None:
                    conn.putheader('Content-Length', str(len(body)))
                conn.endheaders(body if body else None)
                if conn.sock:
                    conn.sock.settimeout(READ_TIMEOUT)
                resp = conn.getresponse()
            except (ConnectionError, socket.timeout, TimeoutError, OSError, http.client.HTTPException) as e:
                _log('目标无响应', fw.t.host_header, repr(e))
                if doc and self.command == 'GET':
                    return self._plain(502, fail_page('up'), 'text/html; charset=utf-8')
                return self._plain(502)
            self._relay(fw, resp)
        finally:
            conn.close()

    def _resp_headers(self, fw, resp, skip=()):
        own = 'http://' + (self.headers.get('Host') or '').lower()  # 已校验过是转发口自身；跳转保持浏览器用的那个主机名
        out = []
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk in HOP or lk in skip:
                continue
            if lk in ('location', 'content-location'):
                v = fw.to_self(v, own)
            out.append((k, v))
        return out

    def _relay(self, fw, resp):
        st = resp.status
        no_body = self.command == 'HEAD' or st in (204, 304) or 100 <= st < 200
        ctype = (resp.getheader('Content-Type') or '').lower()
        enc = (resp.getheader('Content-Encoding') or '').strip().lower()
        try:
            cl = int(resp.getheader('Content-Length')) if resp.getheader('Content-Length') else None
        except ValueError:
            cl = None
        if (not no_body and self.command == 'GET' and 'text/html' in ctype and enc in ('', 'identity', 'gzip', 'x-gzip', 'deflate')
                and (cl is None or cl <= MAX_HTML)):
            data = resp.read(MAX_HTML + 1)
            if len(data) <= MAX_HTML:
                try:
                    html = decode_body(data, enc)
                    out = inject_html(html, csp_nonce(resp.getheaders(), html))
                except Exception:
                    out = None
                if out is not None:
                    self.send_response_only(st, resp.reason)
                    for k, v in self._resp_headers(fw, resp, ('content-length', 'content-encoding', 'content-md5', 'digest')):
                        self.send_header(k, v)
                    self.send_header('Content-Length', str(len(out)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(out)
                    return
            return self._stream(fw, resp, data, keep_len=cl is not None)
        return self._stream(fw, resp, b'', keep_len=cl is not None, no_body=no_body)

    def _stream(self, fw, resp, first, keep_len, no_body=False):
        self.send_response_only(resp.status, resp.reason)
        for k, v in self._resp_headers(fw, resp, () if keep_len else ('content-length',)):
            self.send_header(k, v)
        self.send_header('Connection', 'close')
        self.end_headers()
        if no_body:
            return
        if first:
            self.wfile.write(first)
        rd = getattr(resp, 'read1', None) or resp.read
        while True:
            chunk = rd(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

    def _buffered(self):
        try:
            self.connection.settimeout(0)
            data = self.rfile.peek(65536) or b''
            return self.rfile.read(len(data)) if data else b''
        except Exception:
            return b''
        finally:
            try:
                self.connection.settimeout(None)
            except OSError:
                pass

    def _tunnel(self, fw):
        try:
            up = socket.create_connection((fw.up, fw.t.port), timeout=CONNECT_TIMEOUT)
        except OSError:
            return self._plain(502)
        try:
            lines = ['%s %s HTTP/1.1' % (self.command, self.path)] + ['%s: %s' % kv for kv in self._req_headers(fw, upgrade=True)]
            up.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))
            up.settimeout(None)
            cli = self.connection
            early = self._buffered()  # 握手头之后已经读进缓冲区的数据，先交给目标
            if early:
                up.sendall(early)
            cli.settimeout(None)
            socks = [cli, up]
            while True:
                r, _, _ = select.select(socks, [], [], 3600)
                if not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (up if s is cli else cli).sendall(data)
        except OSError:
            pass
        finally:
            try:
                up.close()
            except OSError:
                pass
