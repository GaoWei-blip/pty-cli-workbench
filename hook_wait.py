"""第十七轮 M：协作的叫醒钩子（hook.py wait-task 调用这里）。

挂法（collab.py 的 claude_args）：父对话是 Claude、协作开着时，--settings 里多一个 PostToolUse 钩子，
matcher 是 mcp__cwb__delegate_task，asyncRewake: true。CLI 调完委派工具就在后台起这个进程，这一轮照常结束；
本进程长轮询工作台的 /hook/task-wait，子任务结束时拿到交回内容，写到标准错误、以退出码 2 结束，
CLI 就把它当成系统通知（<task-notification>，记录里 origin.kind=task-notification）交给模型，模型接着干。
不往伪终端里打字，请求头不变（实测见 NOTES-M）。

- 服务重启、连不上：每秒重试，等新服务起来接着等（不直接退出）；401/403 重读令牌文件。
- 拿到交回内容先回报一次 /hook/task-ack（服务端这时才记已送达），再写标准错误、退出码 2。
- 服务说不用交回（委派失败、结果已经由 task_status 读过、设置里关了自动交回、任务不属于这个对话）：退出码 0，什么都不交。
- 最长等 6 小时；CLI 那边的钩子超时（单个子任务最长时间＋10 分钟）先到的话由 CLI 结束本进程。
只用标准库。
"""
import http.client
import json
import os
import time
import urllib.error
import urllib.request

MAX_WAIT = 6 * 3600


def read_token(run, port):
    try:
        with open(os.path.join(run, f'token-{port}')) as f:
            return f.read().strip()
    except OSError:
        return ''


def ack(opener, port, token, sid, gen, body):
    req = urllib.request.Request(f'http://127.0.0.1:{port}/hook/task-ack', data=body, method='POST',
                                 headers={'X-Token': token, 'X-Sid': sid, 'X-Gen': gen, 'Content-Type': 'application/json'})
    try:
        opener.open(req, timeout=10).read()
    except Exception:
        pass  # 回报失败照样交回：宁可卡片上多一个按钮，也不丢结果


def main(data, port, token, sid, gen, run, out=None):
    import sys
    out = out or sys.stderr
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.time() + MAX_WAIT
    body = (data or '{}').encode('utf-8')
    while time.time() < end:
        req = urllib.request.Request(f'http://127.0.0.1:{port}/hook/task-wait', data=body, method='POST',
                                     headers={'X-Token': token, 'X-Sid': sid, 'X-Gen': gen, 'Content-Type': 'application/json'})
        try:
            with opener.open(req, timeout=90) as r:
                j = json.loads(r.read().decode('utf-8') or '{}')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                nt = read_token(run, port)
                if nt and nt != token:
                    token = nt
                    continue
                time.sleep(2)
                continue
            if e.code == 404:
                return 0  # 旧版服务没有这个接口
            time.sleep(2)
            continue
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            time.sleep(1)  # 服务在重启：等新服务
            continue
        if j.get('deliver') and j.get('text'):
            ack(opener, port, token, sid, gen, body)  # 先回报“拿到了”，服务端才记已送达（本进程被结束就不算，卡片上出“交回”按钮）
            out.write(j['text'])
            out.flush()
            return 2
        if j.get('skip'):
            return 0
    return 0
