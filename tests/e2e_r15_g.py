"""第十五轮 G 端到端测试：网页预览“点选元素”。
测试服务 47956 + 假网关 18806（不建对话、不发请求）+ 测试项目文件夹里的本机静态页面服务；浏览器 agent-browser --session g。
只结束自己启动的进程（按 PID 并核对命令行）。运行：.venv/bin/python -I tests/e2e_r15_g.py
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

os.environ.setdefault('CWB_E2E_PORT', '47956')
os.environ.setdefault('CWB_E2E_GW', '18806')
os.environ.setdefault('CWB_E2E_PROJ', '/tmp/cwb-r15-g-proj')
os.environ.setdefault('CWB_E2E_STATE', '/tmp/cwb-r15-g-exp/e2e-state.json')
os.environ.setdefault('CWB_E2E_GWLOG', '/tmp/cwb-r15-g-gw/requests.jsonl')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_lib as E  # noqa: E402

PY = os.path.join(E.ROOT, '.venv/bin/python')
PROJ = E.PROJ
SESSION = 'g'

INDEX = '''<!doctype html><html><head><meta charset="utf-8"><title>G 测试页</title>
<style>body{margin:0;font:14px -apple-system,sans-serif}nav{position:absolute;left:40px;top:20px}
#buy{position:absolute;left:40px;top:120px;width:200px;height:60px;margin:0}
ul{position:absolute;left:40px;top:220px;margin:0;padding:0;list-style:none}</style>
</head><body>
<nav><a id="to2" href="/page2.html">第二页</a></nav>
<button id="buy" class="cta primary" type="button" onclick="window.__clicks++">立即购买</button>
<ul><li class="it">一</li><li class="it">二</li><li class="it">三</li></ul>
<script>window.__clicks = 0;</script>
</body></html>'''
PAGE2 = '''<!doctype html><html><head><meta charset="utf-8"><title>第二页</title></head><body style="margin:0">
<section class="hero"><h1 class="title" style="position:absolute;left:30px;top:30px;width:300px;height:40px;margin:0;font-size:20px">第二页标题 ```代码```</h1></section>
</body></html>'''


def say(*a):
    E.say(*a)


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def ab(*a, timeout=40):
    p = subprocess.run(['agent-browser', '--session', SESSION] + list(a), capture_output=True, text=True, timeout=timeout)
    return p.returncode, ((p.stdout or '') + (p.stderr or '')).strip()


def ev(js):
    rc, out = ab('eval', 'JSON.stringify((() => { ' + js + ' })())')
    try:
        v = json.loads(out)
        return json.loads(v) if isinstance(v, str) else v
    except Exception:
        return {'_raw': out[:300]}


def wait(js, timeout=15, step=0.25):
    end = time.time() + timeout
    v = None
    while time.time() < end:
        v = ev('return !!(' + js + ');')
        if v is True:
            return True
        time.sleep(step)
    return False


HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cdp_frame_eval.mjs')
CDP = {'url': ''}


def frame_ev(js):
    """agent-browser 的 eval 进不了跨源 iframe：用 CDP 在网页预览的 iframe 里执行（tests/cdp_frame_eval.mjs）。"""
    src = ev("return document.querySelector('.wp-frame').getAttribute('src');")
    m = re.match(r'^https?://[^/]+', src or '')
    if not m:
        return {'_err': src}
    if not CDP['url']:
        CDP['url'] = ab('get', 'cdp-url')[1].splitlines()[-1].strip()
    p = subprocess.run(['node', HELPER, CDP['url'], m.group(0) + '/', '(() => { ' + js + ' })()'], capture_output=True, text=True, timeout=20)
    try:
        return json.loads((p.stdout or '').strip().splitlines()[-1])
    except Exception:
        return {'_err': (p.stdout + p.stderr)[:300]}


def kill_pid(pid, needle):
    if pid and E.alive(pid) and needle in E.cmd_of(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(60):
            if not E.alive(pid):
                break
            time.sleep(0.05)
        return True
    return False


def start_static(port):
    lf = open('/tmp/cwb-r15-g-exp/static.log', 'ab')
    p = subprocess.Popen([PY, '-I', '-m', 'http.server', str(port), '--bind', '127.0.0.1'], cwd=PROJ,
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True)
    for _ in range(100):
        try:
            socket.create_connection(('127.0.0.1', port), timeout=0.3).close()
            break
        except OSError:
            time.sleep(0.05)
    return p.pid


BTN = "document.querySelector('.wp-bar .wpk-btn')"
FR = "document.querySelector('.wp-frame')"


def main():
    os.makedirs(PROJ, exist_ok=True)
    os.makedirs('/tmp/cwb-r15-g-exp', exist_ok=True)
    for name, txt in (('index.html', INDEX), ('page2.html', PAGE2), ('data.json', '{"a": 1}')):
        with open(os.path.join(PROJ, name), 'w', encoding='utf-8') as f:
            f.write(txt)
    pids = {}
    try:
        lg = open('/tmp/cwb-r15-g-exp/gw.log', 'ab')
        pids['gw'] = subprocess.Popen([PY, '-I', os.path.join(E.ROOT, 'tests/fakegw.py'), str(E.GW), '/tmp/cwb-r15-g-gw'], cwd=E.ROOT,
                                      stdin=subprocess.DEVNULL, stdout=lg, stderr=lg, start_new_session=True).pid
        pids['server'] = E.start_server()
        sp = free_port()
        pids['static'] = start_static(sp)
        say('本机页面服务', pids['static'], sp)
        url = 'http://localhost:%d/' % sp

        # ---- 接口：地址校验 ----
        r = E.api('wp-pick', {'url': 'http://example.com/'})
        E.check(r.get('_http') == 400 and r.get('error') == '只支持本机 http 页面', f'接口拒绝外部地址（{r}）')
        r = E.api('wp-pick', {'url': 'http://127.0.0.1:%d/' % E.PORT})
        E.check(r.get('_http') == 400, '接口拒绝工作台自身端口')

        ab('set', 'viewport', '1440', '900')
        rc, out = ab('open', 'http://127.0.0.1:%d/?t=%s' % (E.PORT, E.token()))
        E.check(rc == 0 and wait('window.CWB && CWB.openWeb && CWB.wpPick', 20), '打开测试服务页面')
        ev("const i = document.querySelector('#input'); i.value = '已有内容'; i.dispatchEvent(new Event('input', {bubbles: true})); return 1;")
        ev("CWB.openWeb(%s); return 1;" % json.dumps(url))
        E.check(wait(f"{FR} && {BTN}"), '网页预览出现“点选元素”按钮')
        st = ev(f"const b = {BTN}, f = {FR}; return {{src: f.getAttribute('src'), dis: b.getAttribute('aria-disabled'), title: b.title, "
                f"after: b.previousElementSibling && b.previousElementSibling.id, txt: b.textContent, sw: document.querySelector('#rbody').scrollWidth - document.querySelector('#rbody').clientWidth, "
                f"rows: (t => Math.max(...t) - Math.min(...t) < 12 ? 1 : 2)(Array.from(b.parentElement.children).map(c => c.getBoundingClientRect().top))}};")
        E.check(st.get('src') == url, f"未点选前预览仍直连原地址（{st.get('src')}）")
        E.check(st.get('dis') == 'false' and st.get('title') == '点选元素' and st.get('after') == 'wpUrl' and st.get('txt') == '点选元素', f'按钮在地址框后、可用（{st}）')
        E.check(st.get('sw', 1) <= 0, '右栏没有横向滚动')
        E.check(st.get('rows') == 1, f"默认右栏宽度下地址栏一行放得下（{st.get('rows')} 行）")

        # ---- 进入点选、悬停高亮、点击引用 ----
        ab('click', '.wpk-btn')
        E.check(wait(f"{BTN}.classList.contains('on') && {FR}.getAttribute('src').startsWith('http://127.0.0.1:') && {FR}.classList.contains('wpk-picking')", 15),
                '点按钮进入点选（经转发打开、按钮高亮、预览框描边）')
        rect = ev(f"const r = {FR}.getBoundingClientRect(); return {{x: r.left, y: r.top}};")
        x, y = int(rect['x'] + 1 + 140), int(rect['y'] + 1 + 150)
        ab('mouse', 'move', str(x - 5), str(y - 5))
        ab('mouse', 'move', str(x), str(y))
        time.sleep(0.4)
        hl = frame_ev("const h = document.querySelector('cwb-pick-layer'); if (!h || !h.shadowRoot) return {none: 1};"
                      "const [box, lab] = h.shadowRoot.children; const r = box.getBoundingClientRect();"
                      "return {disp: box.style.display, x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height), label: lab.textContent, lshow: lab.style.display, cur: document.documentElement.style.cursor};")
        E.check(hl.get('disp') == 'block' and (hl.get('x'), hl.get('y'), hl.get('w'), hl.get('h')) == (40, 120, 200, 60), f'悬停时高亮框贴合元素（{hl}）')
        E.check(hl.get('lshow') == 'block' and hl.get('label') == 'button#buy200 × 60', f"旁边显示标签名和尺寸（{hl.get('label')}）")
        E.check(hl.get('cur') == 'crosshair', '点选时鼠标为十字')
        ab('mouse', 'down')
        ab('mouse', 'up')
        E.check(wait(f"!{BTN}.classList.contains('on') && document.querySelector('#input').value.includes('网页预览中选中的元素')", 10), '点击后退出点选、输入框出现引用')
        val = ev("return document.querySelector('#input').value;")
        expect = ('已有内容\n\n网页预览中选中的元素：\n```text\n页面：%s\n选择器：#buy\n元素：<button id="buy" class="cta primary" type="button">\n'
                  '文字：立即购买\n位置：x 40，y 120（相对页面左上角），宽 200，高 60；视口 ' % url)
        E.check(isinstance(val, str) and val.startswith(expect) and val.endswith('\n```\n'), '引用内容正确且追加在已有内容后面：\n' + str(val))
        clicks = frame_ev("return {c: window.__clicks, layer: !!document.querySelector('cwb-pick-layer'), cur: document.documentElement.style.cursor};")
        E.check(clicks.get('c') == 0, f'点选的点击没有触发页面自己的点击处理（{clicks}）')
        time.sleep(0.4)
        clicks = frame_ev("return {layer: !!document.querySelector('cwb-pick-layer'), cur: document.documentElement.style.cursor};")
        E.check(not clicks.get('layer') and clicks.get('cur') == '', '退出后页面里不留高亮层和光标样式')
        E.check(ev("return document.activeElement && document.activeElement.id;") == 'input', '引用后焦点回到输入框')
        sent = ev("return CWB.S && CWB.S.cur || '';")
        E.check(not sent, '没有自动发送（未建对话）')

        # ---- Esc 退出（工作台里按 / 页面里按） ----
        ab('click', '.wpk-btn')
        E.check(wait(f"{BTN}.classList.contains('on')", 5), '再次点按钮：已经转发过，直接进入点选')
        ab('press', 'Escape')
        E.check(wait(f"!{BTN}.classList.contains('on') && !{FR}.classList.contains('wpk-picking')", 5), '工作台里按 Esc 退出点选')
        time.sleep(0.3)
        E.check(frame_ev("return !document.querySelector('cwb-pick-layer');") is True, 'Esc 后页面里的高亮层移除')
        ab('click', '.wpk-btn')
        wait(f"{BTN}.classList.contains('on')", 5)
        frame_ev("window.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); return 1;")
        E.check(wait(f"!{BTN}.classList.contains('on')", 5), '页面里按 Esc 也退出点选')

        # ---- 经转发跳到第二页，地址换回原地址，代码块围栏加长 ----
        frame_ev("location.href = '/page2.html'; return 1;")
        time.sleep(1.0)
        ab('click', '.wpk-btn')
        E.check(wait(f"{BTN}.classList.contains('on')", 8), '页面内跳转后还能点选')
        ab('mouse', 'move', str(int(rect['x'] + 1 + 60)), str(int(rect['y'] + 1 + 45)))
        ab('mouse', 'move', str(int(rect['x'] + 1 + 70)), str(int(rect['y'] + 1 + 50)))
        time.sleep(0.3)
        ab('mouse', 'down')
        ab('mouse', 'up')
        wait("document.querySelector('#input').value.includes('page2.html')", 8)
        val = ev("return document.querySelector('#input').value;")
        E.check(isinstance(val, str) and ('页面：%spage2.html' % url) in val and '选择器：h1.title' in val, '第二页的引用用原地址和短选择器')
        E.check(isinstance(val, str) and '\n````text\n' in val and val.endswith('\n````\n'), '文字里有 ``` 时围栏自动加长')

        # ---- 非本机地址：按钮不可用 ----
        ev("CWB.openWeb('http://example.invalid/'); return 1;")
        E.check(wait(f"{FR} && {FR}.getAttribute('src') === 'http://example.invalid/' && {BTN} && {BTN}.getAttribute('aria-disabled') === 'true'", 8), '非本机地址时按钮不可用')
        E.check(ev(f"return {BTN}.title;") == '只支持本机 http 页面', '按钮提示原因')
        ab('click', '.wpk-btn')
        time.sleep(0.4)
        E.check(ev(f"return {FR}.getAttribute('src');") == 'http://example.invalid/', '点不可用的按钮不改预览')

        # ---- 页面不是 HTML（没有注入）：换回原地址，按钮不可用 ----
        jurl = url + 'data.json'
        ev("CWB.openWeb(%s); return 1;" % json.dumps(jurl))
        wait(f"{FR} && {FR}.getAttribute('src') === {json.dumps(jurl)} && {BTN}.getAttribute('aria-disabled') === 'false'", 8)
        ab('click', '.wpk-btn')
        E.check(wait(f"{BTN}.getAttribute('aria-disabled') === 'true' && {FR}.getAttribute('src') === {json.dumps(jurl)}", 10),
                '不能注入的页面：换回原地址照常预览，按钮不可用')
        E.check(ev(f"return {BTN}.title;") == '这个页面不支持点选', '提示“这个页面不支持点选”')

        # ---- 转发中目标停了：失败页通知后换回原地址 ----
        ev("CWB.openWeb(%s); return 1;" % json.dumps(url))
        wait(f"{FR}.getAttribute('src') === {json.dumps(url)} && {BTN}.getAttribute('aria-disabled') === 'false'", 8)
        ab('click', '.wpk-btn')
        wait(f"{BTN}.classList.contains('on')", 8)
        ab('press', 'Escape')
        kill_pid(pids.pop('static'), 'http.server')
        frame_ev("location.reload(); return 1;")
        E.check(wait(f"{BTN}.getAttribute('aria-disabled') === 'true' && {FR}.getAttribute('src') === {json.dumps(url)}", 10),
                '转发时目标停了：换回原地址，按钮不可用')
        E.check(ev(f"return {BTN}.title;") == '连不上这个地址', '提示“连不上这个地址”')
        time.sleep(2.2)
        E.check(ev(f"return {BTN}.title;") == '连不上这个地址' and ev("return document.querySelectorAll('#toast:not([hidden]), .toast').length;") is not None,
                '换回原地址后不再重复报错')

        # ---- 目标没在监听：接口报错，预览不动 ----
        ab('click', '[data-a=reload]')
        wait(f"{FR} && {BTN} && {BTN}.getAttribute('aria-disabled') === 'false'", 8)
        ab('click', '.wpk-btn')
        E.check(wait(f"{BTN}.getAttribute('aria-disabled') === 'true'", 8), '目标没在监听：按钮不可用')
        E.check(ev(f"return {FR}.getAttribute('src');") == url and ev(f"return {BTN}.title;") == '连不上 localhost:%d' % sp, '预览保持原地址，提示连不上')

        # ---- 浅色 / 深色：点选中按钮用强调色 ----
        pids['static'] = start_static(sp)
        for mode in ('light', 'dark'):
            ab('set', 'media', mode)
            ab('click', '[data-a=reload]')
            wait(f"{BTN} && {BTN}.getAttribute('aria-disabled') === 'false'", 8)
            ab('click', '.wpk-btn')
            wait(f"{BTN}.classList.contains('on')", 8)
            c = ev(f"const t = document.createElement('i'); t.style.color = 'var(--accent)'; document.body.appendChild(t); const a = getComputedStyle(t).color; t.remove();"
                   f"return {{btn: getComputedStyle({BTN}).color, accent: a}};")
            E.check(bool(c.get('btn')) and c.get('btn') == c.get('accent'), f'{mode} 模式下点选中按钮为强调色（{c}）')
            ab('press', 'Escape')
    finally:
        ab('close')
        kill_pid(pids.get('static'), 'http.server')
        E.stop_server()
        pid = E.ptyd_pid()
        if pid and E.alive(pid) and 'ptyd.py' in E.cmd_of(pid) and f'--port {E.PORT}' in E.cmd_of(pid):
            os.kill(pid, signal.SIGTERM)
        kill_pid(pids.get('gw'), 'fakegw.py')
    print('\n失败 %d 项：%s' % (len(E.FAIL), E.FAIL) if E.FAIL else '\n全部通过')
    return 1 if E.FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
