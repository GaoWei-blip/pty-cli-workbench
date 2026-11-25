"""claude 钩子桥：把钩子输入转给工作台后端，授权请求等待界面上的决定。只用标准库。

第十三轮 E：
- 发出之前先把这次事件原子写入 .run/hooks/<代次>.json（server 重启接管时据此还原忙碌、空闲，关机后据此判断回复被中断）
- server 不在线（连接被拒、连接中途断开）时重试：授权请求重试到约 40 秒（App 每 20 秒看护一次，加上启动时间），其他事件约 5 秒
- 遇到 401/403 时重新读 .run/token-<端口>（server 重启后令牌一般沿用，以防万一）
- 授权请求等待中 server 重启了：重新提交同一个请求，新 server 会再弹出授权卡
"""
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ev = sys.argv[1] if len(sys.argv) > 1 else ''
data = sys.stdin.read()
port, token = os.environ.get('CWB_PORT'), os.environ.get('CWB_TOKEN')
if not port or not token:
    sys.exit(0)
sid, gen = os.environ.get('CWB_SID', ''), os.environ.get('CWB_GEN', '')
RUN = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.run')


def note_event():
    try:
        body = json.loads(data or '{}')
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    name = re.sub(r'[^\w.-]', '_', gen or sid)
    if not name:
        return
    rec = {'ev': ev, 't': time.time(), 'gen': gen, 'sid': sid, 'session_id': str(body.get('session_id') or ''),
           'source': str(body.get('source') or ''), 'type': str(body.get('notification_type') or ''),
           'tool': str(body.get('tool_name') or '')}
    d = os.path.join(RUN, 'hooks')
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f'.{name}.{os.getpid()}.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, name + '.json'))
    except OSError:
        pass


def read_token():
    try:
        with open(os.path.join(RUN, f'token-{port}')) as f:
            return f.read().strip()
    except OSError:
        return ''


if ev == 'wait-task':  # 第十七轮 M：协作的叫醒钩子（PostToolUse、asyncRewake）：等子任务结束，退出码 2 把结果交回；不写“最后一次事件”文件
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hook_wait
    sys.exit(hook_wait.main(data, port, token, sid, gen, RUN))

OBSERVE = ev in ('subagent', 'dir', 'compact-start', 'compact-end')  # 第十五轮 A2：只观测的事件，不改回合状态
if not OBSERVE:
    note_event()  # 只观测的事件不覆盖“最后一次事件”文件（接管时靠它还原忙碌、空闲）
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
window = 40 if ev == 'permission' else 1 if OBSERVE else 5
deadline, give_up = time.time() + window, time.time() + 3600
out = None
while time.time() < give_up:
    req = urllib.request.Request(
        f'http://127.0.0.1:{port}/hook/{ev}', data=data.encode('utf-8'), method='POST',
        headers={'X-Token': token, 'X-Sid': sid, 'X-Gen': gen, 'Content-Type': 'application/json'})
    t0 = time.time()
    try:
        with opener.open(req, timeout=3600 if ev == 'permission' else 4 if OBSERVE else 8) as r:
            out = r.read().decode('utf-8')
        break
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            nt = read_token()
            if nt and nt != token:
                token = nt
                continue
        break
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        if ev == 'permission' and time.time() - t0 > 2:
            deadline = time.time() + window  # 等待中途 server 断开（重启）：重新算重试时间，接着把请求交给新 server
        if time.time() >= deadline:
            break
        time.sleep(0.5)
if out is None:
    sys.exit(0)
if ev == 'permission':
    try:
        if json.loads(out or '{}').get('hookSpecificOutput'):
            sys.stdout.write(out)
    except Exception:
        pass
