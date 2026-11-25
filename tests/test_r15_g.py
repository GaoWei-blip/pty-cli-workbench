"""第十五轮 G 单元测试：网页预览点选元素。
1. 转发的地址校验（只放行本机回环 http，拒绝工作台自身端口）；
2. 转发口行为：HTML 注入、gzip 解开再注入、非 HTML 原样、跳转改写、Host/Origin 处理、拒绝绝对地址与跨站请求、
   目标断开时返回带失败标记的页面、WebSocket 透传、只监听 127.0.0.1；
3. 选择器生成（ux-m-frame.js）：用 agent-browser 打开经转发的测试页，逐个元素核对选择器唯一且指回自身。
不启动 claude、不碰 ~/.claude。运行：.venv/bin/python -I tests/test_r15_g.py
"""
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import webpick as W  # noqa: E402

FAIL = []
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SELF_PORT = 47956


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)
    return cond


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError as e:
        return str(e) or 'ValueError'
    return ''


# ---------------------------------------------------------------- 1. 地址校验
def test_parse():
    W.setup(SELF_PORT)
    ok = {
        'http://localhost:3000/': ('localhost:3000', 3000, '/', ('127.0.0.1', '::1')),
        'http://127.0.0.1:5173/a/b?x=1#h': ('127.0.0.1:5173', 5173, '/a/b?x=1', ('127.0.0.1',)),
        'http://[::1]:8080': ('[::1]:8080', 8080, '/', ('::1',)),
        'http://0.0.0.0:4000/x': ('0.0.0.0:4000', 4000, '/x', ('127.0.0.1', '::1')),
        'HTTP://LOCALHOST:3000/Path': ('localhost:3000', 3000, '/Path', ('127.0.0.1', '::1')),
        'http://localhost/': ('localhost', 80, '/', ('127.0.0.1', '::1')),
    }
    for u, (hh, port, path, conn) in ok.items():
        try:
            t = W.parse_target(u)
            check((t.host_header, t.port, t.path, t.conn) == (hh, port, path, conn), f'放行本机地址 {u}')
        except ValueError as e:
            check(False, f'放行本机地址 {u}（报错 {e}）')
    bad = ['https://localhost:3000/', 'ftp://localhost/', 'http://example.com/', 'http://192.168.1.2:3000/', 'http://127.0.0.2:3000/',
           'http://localhost.evil.com:3000/', 'http://localhost@evil.com/', 'http://evil.com@localhost:3000/', 'http://user:pw@localhost:3000/',
           'http://2130706433:3000/', 'http://0x7f000001:3000/', 'http://localhost:99999/', 'http://localhost:0/', 'javascript:alert(1)',
           'file:///etc/passwd', '', None, 123, 'http://localhost:3000/\r\nX: y', '//localhost:3000/', 'localhost:3000',
           f'http://127.0.0.1:{SELF_PORT}/', f'http://localhost:{SELF_PORT}/static/index.html']
    for u in bad:
        check(bool(raises(W.parse_target, u)), f'拒绝非本机或不合规地址 {u!r}')
    check(raises(W.api_wp_pick, {'url': 'http://example.com/'}) == '只支持本机 http 页面', '接口对外部地址给中文原因')


# ---------------------------------------------------------------- 2. 注入与 CSP
def test_inject():
    tag = b'<script src="/__cwb_pick.js"></script>'
    h = W.inject_html(b'<!doctype html><html><head><title>x</title></head><body></body></html>')
    check(h == b'<!doctype html><html><head>' + tag + b'<title>x</title></head><body></body></html>', '插在 <head> 之后')
    h = W.inject_html(b'<!DOCTYPE html><html lang="zh"><head lang="x"><meta charset="utf-8"><title>x</title>')
    check(h.startswith(b'<!DOCTYPE html><html lang="zh"><head lang="x"><meta charset="utf-8">' + tag), 'meta charset 之后插入，保持在前 1024 字节')
    h = W.inject_html(b'<!doctype html><html><header>a</header>')
    check(h == b'<!doctype html><html>' + tag + b'<header>a</header>', '不把 <header> 当成 <head>')
    h = W.inject_html(b'<!doctype html><p>hi')
    check(h == b'<!doctype html>' + tag + b'<p>hi', '没有 head/html 时插在 doctype 之后（不触发怪异模式）')
    check(W.inject_html(b'<p>hi') == tag + b'<p>hi', '什么都没有时插在最前')
    h = W.inject_html(b'<html><head>', 'abc+/=')
    check(b'nonce="abc+/="' in h, '带上页面的 CSP nonce')
    check(W.csp_nonce([('Content-Security-Policy', "script-src 'self' 'nonce-r4nd0m'")]) == 'r4nd0m', '从 CSP 头取 nonce')
    meta = b'<head><meta http-equiv="Content-Security-Policy" content="script-src \'nonce-m3ta\'">'
    check(W.csp_nonce([], meta) == 'm3ta', '从 meta CSP 取 nonce')
    check(W.csp_nonce([('X', 'y')]) == '', '没有 CSP 时不加 nonce')


# ---------------------------------------------------------------- 3. 转发口
SEEN = []
PAGE = b'<!doctype html><html><head><meta charset="utf-8"><title>t</title></head><body><h1>hello</h1></body></html>'


class Up(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _out(self, code, body, ctype, extra=()):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        SEEN.append({'path': self.path, 'host': self.headers.get('Host'), 'origin': self.headers.get('Origin'),
                     'referer': self.headers.get('Referer'), 'ae': self.headers.get('Accept-Encoding')})
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            self.wfile.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
            self.wfile.flush()
            data = self.rfile.read(4)
            self.connection.sendall(b'echo:' + data)
            self.close_connection = True
            return
        if self.path == '/':
            return self._out(200, PAGE, 'text/html; charset=utf-8', [('ETag', '"x"')])
        if self.path == '/gz':
            return self._out(200, gzip.compress(PAGE), 'text/html', [('Content-Encoding', 'gzip')])
        if self.path == '/app.js':
            return self._out(200, b'console.log(1)', 'text/javascript')
        if self.path == '/redir':
            return self._out(302, b'', 'text/plain', [('Location', 'http://localhost:%d/next' % self.server.server_address[1])])
        if self.path == '/away':
            return self._out(302, b'', 'text/plain', [('Location', 'http://example.com/x')])
        if self.path == '/chunked':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for part in (b'<html><head>', b'<title>c</title></head>', b'<body>ok</body></html>'):
                self.wfile.write(b'%x\r\n%s\r\n' % (len(part), part))
            self.wfile.write(b'0\r\n\r\n')
            return
        if self.path == '/echo':
            return self._out(200, json.dumps(SEEN[-1]).encode(), 'application/json')
        return self._out(404, b'nf', 'text/plain')

    do_HEAD = do_GET

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n)
        self._out(200, b'got:' + body, 'text/plain')


def raw(port, data, read=65536):
    s = socket.create_connection(('127.0.0.1', port), timeout=5)
    s.sendall(data)
    out = b''
    try:
        while True:
            c = s.recv(read)
            if not c:
                break
            out += c
    except socket.timeout:
        pass
    s.close()
    return out


def get(url, headers=None, method='GET', data=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=data)
    try:
        with OP.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def test_forward():
    up = ThreadingHTTPServer(('127.0.0.1', 0), Up)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    uport = up.server_address[1]
    try:
        d = W.api_wp_pick({'url': 'http://localhost:%d/?q=1' % uport})
        fo = d['origin']
        fport = int(fo.rsplit(':', 1)[1])
        check(fo.startswith('http://127.0.0.1:') and d['src'] == fo + '/?q=1' and d['target'] == 'http://localhost:%d' % uport, '接口返回转发地址与原地址')
        fw = next(f for f in W._FW.values() if f.lport == fport)
        check(fw.srv.server_address[0] == '127.0.0.1', '转发口只监听 127.0.0.1')
        d2 = W.api_wp_pick({'url': 'http://localhost:%d/other' % uport})
        check(d2['origin'] == fo, '同一目标复用同一个转发口')
        d3 = W.api_wp_pick({'url': 'http://localhost:%d/' % uport, 'host': 'localhost'})
        check(d3['origin'] == 'http://localhost:%d' % fport, '工作台用 localhost 打开时转发地址也用 localhost（保持同站）')
        check(bool(raises(W.parse_target, 'http://127.0.0.1:%d/' % fport)), '不转发转发口自己')

        st, h, b = get(fo + '/', {'Accept': 'text/html', 'Accept-Encoding': 'gzip'})
        check(st == 200 and b'<head><meta charset="utf-8"><script src="/__cwb_pick.js"></script><title>' in b, 'HTML 注入点选脚本')
        check(int(h.get('Content-Length', -1)) == len(b), '注入后 Content-Length 正确')
        check(SEEN[-1]['host'] == 'localhost:%d' % uport, '转给目标的 Host 是原地址')
        check(SEEN[-1]['ae'] is None, '文档请求不要求压缩，便于注入')
        st, h, b = get(fo + '/gz', {'Accept': 'text/html'})
        check(st == 200 and b'/__cwb_pick.js' in b and 'Content-Encoding' not in h, 'gzip 的 HTML 解开后注入')
        st, h, b = get(fo + '/chunked', {'Accept': 'text/html'})
        check(st == 200 and b == b'<html><head><script src="/__cwb_pick.js"></script><title>c</title></head><body>ok</body></html>', '分块传输的 HTML 也能注入')
        st, h, b = get(fo + '/app.js')
        check(st == 200 and b == b'console.log(1)', '非 HTML 原样转发')
        st, h, b = get(fo + '/nope', {'Accept': 'text/html'})
        check(st == 404, '目标的 404 原样返回')
        st, h, b = get(fo + '/', method='HEAD')
        check(st == 200 and b == b'', 'HEAD 请求不带正文')
        op2 = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        try:
            op2.open(fo + '/redir', timeout=5)
            loc = ''
        except urllib.error.HTTPError as e:
            loc = e.headers.get('Location')
        check(loc == fo + '/next', '跳回本机原地址的 Location 改成转发地址')
        try:
            op2.open(urllib.request.Request('http://localhost:%d/redir' % fport), timeout=5)
            loc = ''
        except urllib.error.HTTPError as e:
            loc = e.headers.get('Location')
        except urllib.error.URLError:
            loc = 'skip'
        check(loc in ('http://localhost:%d/next' % fport, 'skip'), f'跳转保持浏览器用的主机名（{loc}）')
        try:
            op2.open(fo + '/away', timeout=5)
            loc = ''
        except urllib.error.HTTPError as e:
            loc = e.headers.get('Location')
        check(loc == 'http://example.com/x', '跳往外部的 Location 不改')
        st, h, b = get(fo + '/echo', {'Origin': fo, 'Referer': fo + '/page'})
        e = json.loads(b)
        check(e['origin'] == 'http://localhost:%d' % uport and e['referer'] == 'http://localhost:%d/page' % uport, '来自转发页自身的 Origin/Referer 改成原地址')
        st, h, b = get(fo + '/echo', {'Origin': 'http://evil.example', 'Referer': 'http://evil.example/x'})
        e = json.loads(b)
        check(e['origin'] == 'http://evil.example' and e['referer'] == 'http://evil.example/x', '其他来源的 Origin/Referer 原样转发，目标的跨域检查照常生效')
        st, h, b = get(fo + '/post', method='POST', data=b'hello=1')
        check(st == 200 and b == b'got:hello=1', 'POST 正文原样转发')
        st, h, b = get(fo + '/__cwb_pick.js')
        check(st == 200 and b'"parents": ["http://127.0.0.1:%d", "http://localhost:%d"]' % (SELF_PORT, SELF_PORT) in b and b'CWB_PICK_CFG' not in b, '提供点选脚本并写入工作台地址')
        check(b'"fail": ""' in b, '正常脚本不带失败标记')

        r = raw(fport, b'GET http://example.com/ HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n' % fport)
        check(r.startswith(b'HTTP/1.1 400'), '绝对地址请求被拒（不能当通用代理）')
        r = raw(fport, b'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n')
        check(r.startswith(b'HTTP/1.1 405'), 'CONNECT 被拒')
        r = raw(fport, b'GET / HTTP/1.1\r\nHost: evil.example:%d\r\n\r\n' % fport)
        check(r.startswith(b'HTTP/1.1 403'), 'Host 不是转发口自身时拒绝（防 DNS 重绑定）')
        st, h, b = get(fo + '/', {'Sec-Fetch-Site': 'cross-site'})
        check(st == 403, '跨站请求被拒')
        st, h, b = get(fo + '/', {'Sec-Fetch-Site': 'same-site', 'Accept': 'text/html'})
        check(st == 200, '工作台 iframe 的同站请求放行')

        ws = raw(fport, b'GET /ws HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nOrigin: %s\r\n'
                 b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\nping' % (fport, fo.encode()))
        check(ws.startswith(b'HTTP/1.1 101') and ws.endswith(b'echo:ping'), 'WebSocket 升级后双向透传')
        check(SEEN[-1]['origin'] == 'http://localhost:%d' % uport, 'WebSocket 握手的 Origin 改成原地址')
    finally:
        up.shutdown()
        up.server_close()
    time.sleep(0.2)
    st, h, b = get(fo + '/', {'Accept': 'text/html', 'Sec-Fetch-Dest': 'iframe'})
    check(st == 502 and b'/__cwb_pick.js?fail=up' in b, '目标停了：文档请求返回带失败标记的页面')
    st, h, b = get(fo + '/__cwb_pick.js?fail=up')
    check(b'"fail": "up"' in b, '失败页里的脚本带失败标记')
    st, h, b = get(fo + '/app.js')
    check(st == 502 and b'' == b, '目标停了：资源请求返回 502')
    msg = raises(W.api_wp_pick, {'url': 'http://localhost:%d/' % uport})
    check(msg.startswith('连不上'), f'目标停了：接口返回原因（{msg}）')


def test_lru():
    W.close_all()
    ups = []
    try:
        for _ in range(W.MAX_FW + 2):
            s = ThreadingHTTPServer(('127.0.0.1', 0), Up)
            s.daemon_threads = True
            threading.Thread(target=s.serve_forever, daemon=True).start()
            ups.append(s)
            W.api_wp_pick({'url': 'http://127.0.0.1:%d/' % s.server_address[1]})
            time.sleep(0.01)
        check(len(W._FW) == W.MAX_FW, f'转发口数量有上限（{len(W._FW)}）')
    finally:
        for s in ups:
            s.shutdown()
            s.server_close()
        W.close_all()


# ---------------------------------------------------------------- 4. 选择器（浏览器里跑 ux-m-frame.js）
SEL_PAGE = '''<!doctype html><html><head><meta charset="utf-8"><title>sel</title></head><body>
<header id="top"><nav><a href="/a">A</a><a href="/b">B</a></nav></header>
<main>
  <section class="pricing">
    <div class="card"><h3>基础</h3><button class="buy">买</button></div>
    <div class="card"><h3>专业</h3><button class="buy" data-testid="buy-pro">买</button></div>
    <div class="card featured"><h3>团队</h3><button class="buy btn-primary">买</button></div>
  </section>
  <ul class="list"><li>一</li><li>二</li><li class="active">三</li></ul>
  <form><input name="email" placeholder="邮箱"><input name="pwd" type="password"><select><option>甲</option></select></form>
  <div id=":r1:"><span class="css-1x2y3z">gen</span></div>
  <div id="a.b c"><p>special id</p></div>
  <div id="dup"><b>1</b></div><div id="dup"><b>2</b></div>
  <svg width="10" height="10"><g><path d="M0 0h10v10z"/></g></svg>
  <div class="bxLvGq sc-AxjAm"><em>x</em></div>
  <custom-el><i>i</i></custom-el>
  <table><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>
</main>
<footer><p>foot</p><p>foot2</p></footer>
</body></html>'''

SEL_JS = r'''(async () => {
  const core = await new Promise((res, rej) => {
    window.__cwbPickTest = c => res(c);
    const s = document.createElement('script'); s.src = '/__cwb_pick.js'; s.onerror = () => rej('load'); document.head.appendChild(s);
    setTimeout(() => rej('timeout'), 4000);
  });
  const bad = [];
  const all = Array.from(document.body.querySelectorAll('*'));
  for (const el of all) {
    const s = core.selector(el);
    let ok = false;
    try { const r = document.querySelectorAll(s); ok = r.length === 1 && r[0] === el; } catch (e) {}
    if (!ok) bad.push(el.outerHTML.slice(0, 60) + ' => ' + s);
  }
  const q = s => core.selector(document.querySelector(s));
  const d = core.describe(document.querySelector('[data-testid=buy-pro]'));
  const pw = core.describe(document.querySelector('input[name=pwd]'));
  return JSON.stringify({n: all.length, bad,
    top: q('#top'), pro: q('[data-testid=buy-pro]'), email: q('input[name=email]'), li3: q('ul.list > li:nth-child(3)'),
    gen: q('.css-1x2y3z'), sp: q('[id="a.b c"] > p'), dup2: core.selector(document.querySelectorAll('#dup')[1].firstChild),
    feat: q('.featured > button'), path: q('svg path'), td4: q('tr:nth-child(2) > td:nth-child(2)'), em: q('.bxLvGq > em'),
    lens: all.map(el => core.selector(el).length),
    d, pwText: pw.text,
    gc: [core.goodClass('card'), core.goodClass('css-1x2y3z'), core.goodClass('bxLvGq'), core.goodClass('Button_primary__3xYzA'), core.goodClass('is-open'), core.goodClass('px-4')],
    gi: [core.goodId('top'), core.goodId(':r1:'), core.goodId('a b'), core.goodId('item-123456'), core.goodId('9lives')]});
})()'''


def ab(*a, timeout=40):
    p = subprocess.run(['agent-browser', '--session', 'g'] + list(a), capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or '') + (p.stderr or '')


class Fix(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = SEL_PAGE.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def test_selector():
    if not shutil.which('agent-browser'):
        print('跳过 选择器测试：没有 agent-browser')
        return
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Fix)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = W.api_wp_pick({'url': 'http://127.0.0.1:%d/' % srv.server_address[1]})
        rc, out = ab('open', d['src'])
        check(rc == 0, '浏览器打开经转发的测试页')
        rc, out = ab('eval', SEL_JS)
        txt = out.strip()
        try:
            r = json.loads(json.loads(txt)) if txt.startswith('"') else json.loads(txt)
        except Exception:
            check(False, '选择器脚本返回结果：' + txt[:300])
            return
        check(r['n'] > 30 and not r['bad'], f"全部 {r['n']} 个元素的选择器唯一且指回自身" + ('' if not r['bad'] else '：' + '; '.join(r['bad'][:5])))
        check(r['top'] == '#top', f"稳定 id 直接用 #id（{r['top']}）")
        check(r['pro'] == 'button[data-testid="buy-pro"]', f"优先用测试属性（{r['pro']}）")
        check(r['email'] == 'input[name="email"]', f"表单项用 name（{r['email']}）")
        check('nth-of-type(3)' in r['li3'] and 'active' not in r['li3'], f"状态类名不进选择器，同类兄弟用序号（{r['li3']}）")
        check(':r1:' not in r['gen'] and 'css-1x2y3z' not in r['gen'], f"框架生成的 id 和类名不进选择器（{r['gen']}）")
        check('a.b' not in r['sp'], f"带空格的 id 不用（{r['sp']}）")
        check('#dup' not in r['dup2'], f"重复 id 不当锚点（{r['dup2']}）")
        check(r['feat'].startswith('button.btn-primary') or 'featured' in r['feat'], f"有区分度的类名优先（{r['feat']}）")
        check('path' in r['path'], f"svg 内部图形也能生成选择器（{r['path']}）")
        check('bxLvGq' not in r['em'] and 'sc-AxjAm' not in r['em'], f"随机类名不进选择器（{r['em']}）")
        check(max(r['lens']) <= 120, f"选择器长度可控（最长 {max(r['lens'])}）")
        info = r['d']
        check(info['tag'] == 'button' and info['text'] == '买' and ['data-testid', 'buy-pro'] in info['attrs'] and info['rect']['w'] > 0, '元素描述含标签、属性、文字、位置')
        check(r['pwText'] == '', '密码框不取任何文字')
        check(r['gc'] == [True, False, False, False, False, True], f"类名过滤规则（{r['gc']}）")
        check(r['gi'] == [True, False, False, False, False], f"id 过滤规则（{r['gi']}）")
    finally:
        ab('close')
        srv.shutdown()
        srv.server_close()


if __name__ == '__main__':
    test_parse()
    test_inject()
    test_forward()
    test_lru()
    W.setup(SELF_PORT)
    test_selector()
    W.close_all()
    print('\n失败 %d 项' % len(FAIL) if FAIL else '\n全部通过')
    sys.exit(1 if FAIL else 0)
