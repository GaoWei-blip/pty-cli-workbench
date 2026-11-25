# 等所有对话空闲后结束当前后台服务（不留停止标记），由 Claude 工作台 App 的看护在 20 秒内重新拉起。
# 这样新服务归属于 App（不会被系统算在 Cursor 等其他程序名下）。
# 用法：python3 -I restart_when_idle.py [--server-now] [--relaunch-app]
#   --server-now    第十三轮 E：不等空闲，立即重启后台服务。CLI 在终端守护进程 ptyd 里，回复照常进行，新服务起来后接管。
#                   只适用于 ptyd.py 没改的部署；改了 ptyd.py 用默认的等空闲模式（新服务发现 ptyd 代码不一致，空闲时自动更换）。
#   --relaunch-app  服务重启后再正常退出并重开 App（换了原生 App 的程序文件时用）
# 空闲的判定：每个对话都不在回复中、不等确认、没有后台任务、钩子回合已结束、记录文件 30 秒没写、屏幕上没有转圈状态行，连续 5 次。
# 第十六轮 P：Codex / Antigravity 对话由 server 直接持有（不在 ptyd 里），重启 server 会断开它们的回复，
#   所以两种模式（含 --server-now）都要先等它们空闲（接口 prov-busy：没有在回复、等确认、排队的），连续 3 次才重启。
import os, sys, time, json, signal, glob, re, urllib.request

if os.fork(): sys.exit(0)
os.setsid()
if os.fork(): sys.exit(0)

W = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('CWB_PORT', '47821'))
LOG = open(os.path.join(W, '.run', 'restart_when_idle.log'), 'a', buffering=1)
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SPIN = re.compile(r'^[✻✳✶✽✢·*✺✹✸✷]\s{1,2}\S.*…')


def log(*a):
    print(time.strftime('%m-%d %H:%M:%S'), *a, file=LOG)


def tok():
    return open(os.path.join(W, '.run', f'token-{PORT}')).read().strip()


def api(p, body=None):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/{p}', data=json.dumps(body or {}).encode(),
                                 headers={'X-Token': tok(), 'Content-Type': 'application/json'}, method='POST')
    with op.open(req, timeout=10) as r:
        return json.loads(r.read())


def ping():
    try:
        with op.open(f'http://127.0.0.1:{PORT}/ping', timeout=2) as r:
            return r.read().decode()
    except Exception:
        return ''


def server_pid():
    me = os.path.join(W, 'server.py')
    for l in os.popen('ps -axo pid=,command=').read().splitlines():
        p = l.strip().split(None, 1)
        if len(p) == 2 and me in p[1] and 'restart_when_idle' not in p[1]:
            return int(p[0])
    return None


def prov_busy():
    """第十六轮 P：Codex / Antigravity 对话有没有在忙。
    复核修复 5：和 Claude 那条路径一样保守——超时、连不上、读不到令牌、返回出错都算“不空闲”；
    只有服务明确回 404（旧版服务没有这个接口，也就没有这两家的对话）才算空闲。"""
    try:
        return bool(api('prov-busy').get('busy'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        log('查询 Codex / Antigravity 状态失败，按不空闲处理', e)
        return True
    except Exception as e:
        log('查询 Codex / Antigravity 状态失败，按不空闲处理', e)
        return True


def wait_prov(limit=7200, need=3):
    """第十六轮 P：等 Codex / Antigravity 对话连续 need 次空闲（每 2 秒看一次），最多 limit 秒。"""
    n, t = 0, time.time()
    while time.time() - t < limit:
        if prov_busy():
            if n or not getattr(wait_prov, 'said', False):
                log('等 Codex / Antigravity 对话空闲')
                wait_prov.said = True
            n = 0
        else:
            n += 1
            if n >= need:
                return True
        time.sleep(2)
    return False


def transcript(sid):
    fs = glob.glob(os.path.expanduser(f'~/.claude/projects/*/{sid}.jsonl'))
    return fs[0] if fs else None


def quiet(sid):
    p = transcript(sid)
    return not p or time.time() - os.path.getmtime(p) > 30


def spinning(sid):
    try:
        ls = api('screen', {'sid': sid}).get('lines', [])
    except Exception:
        return True
    return any(SPIN.match(l) for l in ls[-45:])


ok, t0, ts = 0, time.time(), []
NOW = '--server-now' in sys.argv
if NOW:
    log('立即重启后台服务（--server-now）')
while not NOW and time.time() - t0 < 7200:
    try:
        ts = api('tasks').get('tasks', [])
        idle = all(not x['state'].get('busy') and not x['state'].get('attention') and not x['state'].get('bg')
                   and not x['state'].get('turn') and quiet(x['sid']) and not spinning(x['sid']) for x in ts) and not prov_busy()  # 第十六轮 P
    except Exception as e:
        log('查询失败', e)
        idle = False
    ok = ok + 1 if idle else 0
    if ok >= 5:
        break
    time.sleep(3)
else:
    if not NOW:
        log('两小时内没有空闲，放弃')
        sys.exit(1)

if not wait_prov():  # 第十六轮 P：两种模式都先等 Codex / Antigravity 对话空闲
    log('两小时内 Codex / Antigravity 对话没有空闲，放弃')
    sys.exit(1)
old = server_pid()
log('立即结束旧服务' if NOW else '全部空闲，结束旧服务', old)
# 记下这些对话记录此刻的修改时间：新服务据此知道是工作台自己写的，打开时不再提示“在别的窗口里更新过”
try:
    op_ = os.path.join(W, '.run', 'own-mtimes.json')
    own = json.load(open(op_)) if os.path.exists(op_) else {}
    for x in ts:
        p = transcript(x['sid'])
        if p:
            own[x['sid']] = os.path.getmtime(p)
    json.dump(own, open(op_ + '.tmp', 'w'))
    os.replace(op_ + '.tmp', op_)
except Exception as e:
    log('记录修改时间失败', e)
if old:
    os.kill(old, signal.SIGTERM)
for _ in range(90):
    time.sleep(1)
    if ping().startswith('ok'):
        break
new, chain, p = server_pid(), [], server_pid()
for _ in range(8):
    if not p or p <= 1:
        break
    line = os.popen(f'ps -o ppid=,comm= -p {p}').read().strip()
    if not line:
        break
    pp, comm = line.split(None, 1)
    chain.append(comm.split('/')[-1])
    p = int(pp)
log('新服务', new, '连接', ping(), '来源', ' <- '.join(chain))
if '--relaunch-app' in sys.argv and ping().startswith('ok'):
    os.system("osascript -e 'tell application id \"local.claude-workbench\" to quit' >/dev/null 2>&1")
    for _ in range(20):
        time.sleep(0.5)
        if not os.popen("pgrep -f 'Claude 工作台.app/Contents/MacOS/'").read().strip():
            break
    os.system('open -b local.claude-workbench')
    log('已重开 App')
