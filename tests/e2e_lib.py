"""第十三轮（开发线 E）端到端测试的公用部分：真实 claude + 假网关（tests/fakegw.py，端口 18791）+ 测试服务（端口 47931）。
只在副本里跑，用副本自己的 .run；只结束自己记下 PID 的进程。状态（服务 PID、测试会话 ID）存在 /tmp/cwb-e-exp/e2e-state.json。
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 第十四轮 I：端口、假网关、测试文件夹可用环境变量改（默认值同第十三轮）
PORT = int(os.environ.get('CWB_E2E_PORT') or 47931)
GW = int(os.environ.get('CWB_E2E_GW') or 18791)
PROJ = os.environ.get('CWB_E2E_PROJ') or '/tmp/cwb-e-proj'
RUN = os.path.join(ROOT, '.run')
STATE = os.environ.get('CWB_E2E_STATE') or '/tmp/cwb-e-exp/e2e-state.json'
GWLOG = os.environ.get('CWB_E2E_GWLOG') or '/tmp/cwb-e-gw/requests.jsonl'
PROJ_DIR = os.path.expanduser('~/.claude/projects/' + re.sub(r'[^A-Za-z0-9]', '-', os.path.realpath(PROJ)))
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FAIL = []


def say(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def check(cond, msg):
    say(('通过 ' if cond else '失败 ') + msg)
    if not cond:
        FAIL.append(msg)
    return cond


def st_load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def st_save(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(d, open(STATE, 'w'), ensure_ascii=False, indent=1)


def token():
    return open(os.path.join(RUN, f'token-{PORT}')).read().strip()


def ping():
    try:
        with OP.open(f'http://127.0.0.1:{PORT}/ping', timeout=2) as r:
            return r.read().decode()
    except Exception:
        return ''


def api(p, body=None, timeout=30):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/{p}', data=json.dumps(body or {}).encode(),
                                 headers={'X-Token': token(), 'Content-Type': 'application/json'}, method='POST')
    try:
        with OP.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return dict(json.loads(e.read() or b'{}'), _http=e.code)
        except Exception:
            return {'_http': e.code}


def ptyd_pid():
    try:
        return int(open(os.path.join(RUN, f'ptyd-{PORT}.lock')).read().split()[0])
    except Exception:
        return 0


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    out = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith('Z')


def cmd_of(pid):
    return subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()


def kill_ptyd(sig=signal.SIGKILL):
    pid = ptyd_pid()
    if pid and alive(pid) and 'ptyd.py' in cmd_of(pid) and f'--port {PORT}' in cmd_of(pid):
        os.kill(pid, sig)
        return pid
    return 0


# ---------------------------------------------------------------- 服务
def start_server(**extra):
    # 第十四轮 I：CWB_E2E_EXTRA_ENV（JSON）里的变量一并放进 --settings 的 env（例如测试用的代理）
    senv = dict(json.loads(os.environ.get('CWB_E2E_EXTRA_ENV') or '{}'), ANTHROPIC_BASE_URL=f'http://127.0.0.1:{GW}')
    env = dict(os.environ, CWB_PORT=str(PORT), CWB_SHOW_TMP='1', **extra, CWB_EXTRA_SETTINGS=json.dumps({'env': senv}))
    os.makedirs(RUN, exist_ok=True)
    lf = open(os.path.join(RUN, 'server-test.log'), 'ab')
    p = subprocess.Popen([os.path.join(ROOT, '.venv/bin/python'), '-I', os.path.join(ROOT, 'server.py')], cwd=ROOT, env=env,
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True)
    t0 = time.time()
    for _ in range(300):
        if ping().startswith('ok'):
            break
        time.sleep(0.1)
    d = st_load()
    d['server'] = p.pid
    st_save(d)
    say('测试服务已启动', p.pid, f'{time.time() - t0:.1f}s', ping())
    return p.pid


def stop_server():
    d = st_load()
    pid = d.get('server')
    if pid and alive(pid) and 'server.py' in cmd_of(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not alive(pid):
                break
            time.sleep(0.05)
        say('测试服务已停止', pid)
    return pid


def restart_server():
    old = stop_server()
    t0 = time.time()
    new = start_server()
    return old, new, time.time() - t0


# ---------------------------------------------------------------- 事件
class Events:
    """订阅 /events，服务重启后自动重连。"""

    def __init__(self):
        self.items, self.cv, self.stop = [], threading.Condition(), False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop:
            try:
                with OP.open(f'http://127.0.0.1:{PORT}/events?t={token()}', timeout=60) as r:
                    for line in r:
                        if line.startswith(b'data: '):
                            ev = json.loads(line[6:])
                            with self.cv:
                                self.items.append((time.time(), ev))
                                self.cv.notify_all()
            except Exception:
                time.sleep(0.3)

    def wait(self, pred, timeout=30, after=0):
        end = time.time() + timeout
        with self.cv:
            while True:
                for t, ev in self.items:
                    if t >= after and pred(ev):
                        return ev
                left = end - time.time()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def since(self, t0, pred=lambda e: True):
        with self.cv:
            return [(t, e) for t, e in self.items if t >= t0 and pred(e)]


def runner_of(sid):
    for t in api('tasks').get('tasks', []):
        if t['sid'] == sid:
            return t['state']
    return None


def wait_until(fn, timeout=30, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def new_session(opts=None):
    """新建测试会话；第一次进这个文件夹会有信任弹窗，选“Yes, I trust this folder”。"""
    d = api('new', {'cwd': PROJ, 'opts': opts or {}})
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
            time.sleep(1.5)  # 第十五轮联调：弹窗刚画出来时 CLI 还不收方向键，马上选会只剩回车生效（默认项是“No, exit”，CLI 直接退出）
            api('dialog', {'sid': sid, 'option': idx})
            time.sleep(1.5)
            return None
        return st if st.get('ready') else None
    st = wait_until(ready, 40)
    say('新会话', sid, '就绪' if st else '没就绪', (st or {}).get('mode'))
    return sid


def transcript(sid):
    p = os.path.join(PROJ_DIR, f'{sid}.jsonl')
    return p if os.path.exists(p) else None


def tx_text(sid):
    p = transcript(sid)
    return open(p, encoding='utf-8').read() if p else ''


def gw_rows():
    out = []
    try:
        for l in open(GWLOG, encoding='utf-8'):
            out.append(json.loads(l))
    except Exception:
        pass
    return out


def screen(sid, redraw=False):
    return api('screen', {'sid': sid, 'redraw': redraw})


def marks():
    try:
        return json.load(open(os.path.join(RUN, f'marks-{PORT}.json')))
    except Exception:
        return {}


def ua_check(n0):
    rows = [r for r in gw_rows()[n0:] if r.get('path', '').startswith('/v1/')]
    uas = {(r['ua'], r['x_app']) for r in rows}
    ok = bool(rows) and uas <= {(f"claude-cli/{r['ua'].split('/')[1].split(' ')[0]} (external, cli)", 'cli') for r in rows}
    check(ok and all(r['ua'].startswith('claude-cli/') and r['ua'].endswith('(external, cli)') and r['x_app'] == 'cli' for r in rows),
          f'UA：本场景 {len(rows)} 个请求全部是 {sorted(uas)}')
