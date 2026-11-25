"""11-23 单元测试：Antigravity 闲置后再用时“没登录”——等网络恢复后自己重试，不再一上来就让用户去终端运行 agy。
用真的 AcpAdapter ＋ 假 ACP agent（tests/fakeacp.py 的 auth_fail / auth_fail_prompt 开关：前 N 次回 -32000），
网络探测（prov_acp.google_reachable）在进程内换成假的。不启动真的 agy，不连 Google。
运行：.venv/bin/python -I tests/test_agy_relogin.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix='cwb-agyauth-')
ST = os.path.join(TMP, 'acp')
os.makedirs(ST)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.update(CWB_DATA_DIR=os.path.join(TMP, 'data'), CWB_PORT='47962', CWB_AGY_NET_WAIT='2',
                  CWB_AGY_CMD=json.dumps([sys.executable, '-I', os.path.join(ROOT, 'tests', 'fakeacp.py'), ST]))
sys.path.insert(0, ROOT)
import server as S  # noqa: E402
import providers as PV  # noqa: E402
import prov_acp as PA  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


S.CONF_PATH = os.path.join(TMP, 'config.json')
S.MARKS_PATH = os.path.join(TMP, 'marks.json')
S.MARKS.update({'done_at': {}, 'interrupted': {}})
A = PV.ADAPTERS['agy']
NET = {'ok': True, 'calls': 0}


def fake_net(timeout=6.0):
    NET['calls'] += 1
    if not NET['ok']:
        time.sleep(0.3)
    return NET['ok']


PA.google_reachable = fake_net


def conv():
    import uuid
    c = PV.PConv(str(uuid.uuid4()), 'agy', TMP)
    PV.CONVS[c.sid] = c
    spins = []
    orig = c.set_spin
    c.set_spin = lambda t: (spins.append(t), orig(t))
    return c, spins


def setf(name, n):
    with open(os.path.join(ST, name), 'w') as f:
        f.write(str(n))


def calls(meth):
    try:
        return sum(1 for l in open(os.path.join(ST, 'calls.jsonl')) if json.loads(l).get('method') == meth)
    except OSError:
        return 0


def run(c, text, stop_after=None):
    c.claim()
    th = threading.Thread(target=PV.start_turn, args=(c, text, []), daemon=True)
    t0 = time.time()
    th.start()
    if stop_after is not None:
        time.sleep(stop_after)
        A.interrupt(c)
    while time.time() - t0 < 30:
        if not c.busy and not th.is_alive():
            break
        time.sleep(0.05)
    return time.time() - t0


def errs(c):
    return [it for it in c.items if it.get('k') == 'error']


def last_text(c):
    t = [it for it in c.items if it.get('k') == 'text']
    return t[-1]['text'] if t else ''


def t_cases():
    # 1. 网络是通的，建会话前两次没登录、第三次好了：自己重试成功，界面只看到“正在重试”，没有报错
    NET['ok'] = True
    setf('auth_fail', 2)
    c, spins = conv()
    n0 = calls('session/new')
    run(c, '你好')
    check(not errs(c) and last_text(c).startswith('收到：你好') and c.meta.get('native'), f'建会话没登录两次后自己重试成功：{errs(c)}')
    check(calls('session/new') - n0 == 3, '一共发了 3 次 session/new（重试 2 次）')
    check(any('正在重试（第 1 次）' in s for s in spins) and any('正在重试（第 2 次）' in s for s in spins), f'转圈处写正在重试：{spins}')
    # 2. 网络一直不通：等 NET_WAIT 秒后说明是网络问题，不叫用户去终端
    NET['ok'] = False
    setf('auth_fail', 1)
    c, spins = conv()
    n0 = calls('session/new')
    dt = run(c, '你好')
    e = errs(c)
    check(len(e) == 1 and e[0]['text'] == PA.NET_HINT and '终端' not in e[0]['text'].replace('不用去终端', ''), f'网络不通时提示网络问题：{e}')
    check(calls('session/new') - n0 == 1 and 1.5 < dt < 10, f'网络不通时不重发，等了约 NET_WAIT 秒（{dt:.1f} 秒）')
    # 3. 网络通、重试两次还是没登录（真的掉登录了）：才提示去终端登录
    NET['ok'] = True
    setf('auth_fail', 5)
    c, _ = conv()
    n0 = calls('session/new')
    run(c, '你好')
    e = errs(c)
    check(len(e) == 1 and e[0]['text'] == PA.AUTH_HINT and '终端运行 agy' in e[0]['text'], f'重试后仍没登录才提示去终端：{e}')
    check(calls('session/new') - n0 == 3, '真掉登录时最多发 3 次')
    setf('auth_fail', 0)
    # 4. 发消息时没登录（会话已开、还没有任何输出）：等网络后重发这条消息
    c, spins = conv()
    run(c, '先开好')
    setf('auth_fail_prompt', 1)
    p0 = calls('session/prompt')
    run(c, '第二条')
    check(not errs(c) and last_text(c).startswith('收到：第二条'), f'发消息没登录后自己重发成功：{errs(c)}')
    check(calls('session/prompt') - p0 == 2, '这条消息发了 2 次')
    # 5. 一直回没登录的消息（e2e 的 AUTH）：重试完说明掉登录，文字里有“登录”
    run(c, 'AUTH')
    e = errs(c)
    check(e and e[-1]['text'] == PA.AUTH_HINT and e[-1].get('etype') == '鉴权失败', f'一直没登录：{e[-1:]}')
    # 6. 等网络的时候点停止：很快按“已中断”结束，不报错
    NET['ok'] = False
    os.environ['CWB_AGY_NET_WAIT'] = '20'
    PA.NET_WAIT = 20.0
    setf('auth_fail', 1)
    c, _ = conv()
    dt = run(c, '你好', stop_after=0.8)
    check(not errs(c) and not c.busy and dt < 6, f'等网络时点停止很快结束、不报错（{dt:.1f} 秒）：{errs(c)}')
    NET['ok'] = True
    setf('auth_fail', 0)


def t_probe():
    """真的探测函数（取源码单独执行，目标换成本机关着的端口）：连不上很快返回 False、不抛异常；开关能跳过。"""
    src = open(os.path.join(ROOT, 'prov_acp.py'), encoding='utf-8').read()
    ns = {}
    exec(compile(src[src.index('def google_reachable'):src.index('\n\n\n', src.index('def google_reachable'))], 'probe', 'exec'),
         {'os': os, 'socket': __import__('socket'), 'ssl': __import__('ssl'), 'GOOGLE': ('127.0.0.1', 9)}, ns)
    t0 = time.time()
    r = ns['google_reachable'](1.0)
    check(r is False and time.time() - t0 < 3, '真的探测函数：连不上时很快返回 False、不抛异常')
    os.environ['CWB_AGY_NETCHECK'] = 'off'
    check(ns['google_reachable'](1.0) is True, 'CWB_AGY_NETCHECK=off 时跳过探测')
    del os.environ['CWB_AGY_NETCHECK']


try:
    t_cases()
    t_probe()
finally:
    try:
        A.stop_proc('测试结束')
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)
print('\n全部通过' if not FAIL else f'\n{len(FAIL)} 项失败：' + '；'.join(FAIL))
sys.exit(1 if FAIL else 0)
