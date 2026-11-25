"""第十六轮 R 测试用：模拟 tailscale serve 的 HTTPS 反向代理，只监听 127.0.0.1，给无头浏览器测手机页面用。

用法：.venv/bin/python -I tests/fake_ts_proxy.py <监听端口> <后端端口> <证书> <私钥> <身份文件>
做法照官方源码 ipn/ipnlocal/serve.go（reverseProxy.ServeHTTP 的 Rewrite）：
- Host 原样转给后端（r.Out.Host = r.In.Host）；
- 设 X-Forwarded-Host（=Host）、X-Forwarded-Proto: https、X-Forwarded-For（=来源地址，这里取身份文件里的 ip）；
- 先删掉客户端自带的 Tailscale-User-Login/Name/Profile-Pic、Tailscale-Funnel-Request、Tailscale-Headers-Info（含下划线写法），
  再按身份文件加 Tailscale-User-Login、Tailscale-User-Name（login 为空时模拟带标签的设备，不加）。
身份文件是 JSON：{"login": "...", "name": "...", "ip": "100.x.y.z"}，每个请求现读，测试可以随时改。
响应边收边转（事件流要实时），每个响应后关连接。
"""
import http.client
import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, BACK, CERT, KEY, IDENT = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
STRIP = {'tailscale-user-login', 'tailscale-user-name', 'tailscale-user-profile-pic', 'tailscale-funnel-request', 'tailscale-headers-info',
         'tailscale-app-capabilities', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto'}
HOP = {'connection', 'keep-alive', 'proxy-connection', 'transfer-encoding', 'te', 'trailer', 'upgrade'}


def qenc(v):
    """和官方 encTailscaleHeaderValue（mime.QEncoding.Encode）一样：纯 ASCII 原样，含非 ASCII 的用 RFC 2047 Q 编码。"""
    v = str(v or '')
    if v.isascii():
        return v
    out = ''.join(chr(b) if (48 <= b <= 57 or 65 <= b <= 90 or 97 <= b <= 122) else '_' if b == 32 else '=%02X' % b for b in v.encode('utf-8'))
    return '=?utf-8?q?' + out + '?='


def ident():
    try:
        with open(IDENT, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def fwd(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else None
        hd = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in HOP or kl.replace('_', '-') in STRIP:
                continue
            hd[k] = v
        idt = ident()
        hd['X-Forwarded-Host'] = self.headers.get('Host') or ''
        hd['X-Forwarded-Proto'] = 'https'
        hd['X-Forwarded-For'] = idt.get('ip') or '100.64.0.2'
        if idt.get('login'):
            hd['Tailscale-User-Login'] = qenc(idt['login'])
            hd['Tailscale-User-Name'] = qenc(idt.get('name') or '')
            hd['Tailscale-Headers-Info'] = 'https://tailscale.com/s/serve-headers'
        c = http.client.HTTPConnection('127.0.0.1', BACK, timeout=600)
        try:
            c.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for k, v in hd.items():
                c.putheader(k, v)
            c.endheaders(body)
            r = c.getresponse()
        except Exception as e:
            msg = ('bad gateway: %r' % e).encode()
            self.send_response(502)
            self.send_header('Content-Length', str(len(msg)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(msg)
            self.close_connection = True
            return
        self.send_response(r.status, r.reason)
        for k, v in r.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            while True:
                b = r.read1(65536)
                if not b:
                    break
                self.wfile.write(b)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            c.close()
            self.close_connection = True

    do_GET = do_POST = do_HEAD = fwd


srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
srv.daemon_threads = True
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print('fake tailscale serve on https://127.0.0.1:%d -> %d' % (PORT, BACK), flush=True)
srv.serve_forever()
