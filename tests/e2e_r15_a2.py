"""第十五轮（开发线 A2）端到端测试：真实 claude + 假网关（tests/fakegw.py）+ 测试服务，全部只在本机回环地址。
用法（在副本根目录）：.venv/bin/python -I tests/e2e_r15_a2.py [all|cleanup]
默认端口 47952、假网关 18802、测试文件夹 /tmp/cwb-r15-a2-proj（可用 CWB_E2E_* 环境变量改）。
覆盖：计划三种决定（沿用 / 自动处理修改 / 每处修改问我）和修改意见、授权卡拒绝并说明、停止后台任务（一个 / 多个 / 回复进行中）、
旁问（空闲 / 回复进行中 / 经 send 的 /btw）、添加文件夹（含空格和中文的路径、重复、已在工作范围、经 send 的 /add-dir）、
压缩事件与钩子成功提示行过滤、被拦截命令与命令清单、用户条目 uuid、请求头不变。
只结束自己启动的进程（按 PID 并核对命令行），测完按会话 ID 把测试会话记录、CLI 写的计划文件移到废纸篓。
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time

os.environ.setdefault('CWB_E2E_PORT', '47952')
os.environ.setdefault('CWB_E2E_GW', '18802')
os.environ.setdefault('CWB_E2E_PROJ', '/tmp/cwb-r15-a2-proj')
os.environ.setdefault('CWB_E2E_STATE', '/tmp/cwb-r15-a2-exp/e2e-state.json')
os.environ.setdefault('CWB_E2E_GWLOG', '/tmp/cwb-r15-a2-gw/requests.jsonl')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_lib as L  # noqa: E402
from e2e_lib import api, check, say, st_load, st_save, wait_until, runner_of, screen, alive, cmd_of  # noqa: E402

HOME = os.path.expanduser('~')
EXTRA = '/tmp/cwb-r15-a2-extra'
TRASH = os.path.join(HOME, '.Trash')


def ensure_gw():
    try:
        socket.create_connection(('127.0.0.1', L.GW), timeout=1).close()
        return
    except OSError:
        pass
    logdir = os.path.dirname(L.GWLOG)
    os.makedirs(logdir, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != 'FAKEGW_BODIES'}
    p = subprocess.Popen([os.path.join(L.ROOT, '.venv/bin/python'), '-I', os.path.join(L.ROOT, 'tests/fakegw.py'), str(L.GW), logdir],
                         stdin=subprocess.DEVNULL, stdout=open(os.path.join(logdir, 'gw.out'), 'ab'), stderr=subprocess.STDOUT,
                         start_new_session=True, env=env)
    d = st_load()
    d['gw'] = p.pid
    st_save(d)
    wait_until(lambda: _port_open(L.GW), 5, 0.1)
    say('假网关已启动', p.pid)


def _port_open(port):
    try:
        socket.create_connection(('127.0.0.1', port), timeout=1).close()
        return True
    except OSError:
        return False


def fake(*steps, say_=None):
    """FAKE 步骤：每步一个工具调用列表（[(名字, 输入), …]）。"""
    out = ['FAKE[[' + json.dumps([{'name': n, 'input': i} for n, i in st], ensure_ascii=False) + ']]END' for st in steps]
    return ' '.join(out) + (f' SAY[[{say_}]]END' if say_ else '')


def rows(sid):
    out = []
    for l in L.tx_text(sid).splitlines():
        try:
            out.append(json.loads(l))
        except Exception:
            pass
    return out


def tool_results(sid):
    res = []
    for r in rows(sid):
        c = (r.get('message') or {}).get('content') if r.get('type') == 'user' else None
        for b in c if isinstance(c, list) else []:
            if isinstance(b, dict) and b.get('type') == 'tool_result':
                x = b.get('content')
                res.append(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False))
    return res


def st(sid):
    return runner_of(sid) or {}


def idle(sid):
    s = st(sid)
    return s if s.get('ready') and not s.get('busy') and not s.get('turn') and not s.get('attention') else None


def wait_text(sid, text, t=60):
    return wait_until(lambda: text in L.tx_text(sid) and idle(sid), t)


def remember(key, val):
    d = st_load()
    d.setdefault(key, [])
    if val not in d[key]:
        d[key].append(val)
    st_save(d)


def shells(sid):
    return api('agents', {'sid': sid}).get('shells') or []


def shell_id(sid, cmd):
    return next((x['id'] for x in shells(sid) if x['cmd'] == cmd), None)


def shell_state(sid, tid):
    return next((x['state'] for x in shells(sid) if x['id'] == tid), None)


def no_panel(sid):
    lines = screen(sid).get('lines') or []
    j = '\n'.join(lines)
    return not any(w in j for w in ('Esc to close', 'Shell details', 'Add directory to workspace'))


# ---------------------------------------------------------------- 场景
def plan_round(ev, sid, tag, decision, extra=None, bash=True):
    """在计划模式下走一遍：写计划文件 → ExitPlanMode（授权卡）→ 按 decision 决定。返回授权请求。"""
    api('control', {'sid': sid, 'mode': 'plan mode'})
    check(bool(wait_until(lambda: st(sid).get('mode') == 'plan mode' and idle(sid), 20)), f'计划[{tag}]：切到规划模式')
    steps = [[('Write', {'file_path': '@PLANFILE@', 'content': f'# {tag} 计划\n\n1. 改 a.txt\n'})], [('ExitPlanMode', {})]]
    if bash:
        steps.append([('Bash', {'command': f'touch {tag}.txt', 'description': '建文件'})])
    t0 = time.time()
    api('send', {'sid': sid, 'text': f'{tag} ' + fake(*steps, say_=f'{tag} 执行完了')})
    e = ev.wait(lambda e: e.get('type') == 'perm' and e['req']['sid'] == sid and e['req']['tool'] == 'ExitPlanMode', 40, t0)
    req = (e or {}).get('req') or {}
    if req.get('planPath'):
        remember('plans', req['planPath'])
    check(f'# {tag} 计划' in (req.get('plan') or '') and (req.get('planPath') or '').startswith(os.path.join(HOME, '.claude', 'plans'))
          and req['planPath'].endswith('.md'), f'计划[{tag}]：授权请求带计划全文和计划文件路径')
    d = api('perm', dict({'rid': req.get('rid'), 'plan': decision}, **(extra or {})))
    check(d.get('ok') is True, f'计划[{tag}]：perm plan={decision} 已交给 CLI')
    return req, t0


def scenario_plan(ev):
    sid = L.new_session({'mode': 'bypass permissions'})
    time.sleep(2)  # 让“原模式”稳定下来
    # 1) 沿用跳过确认
    req, _ = plan_round(ev, sid, 'pkeep', 'keep')
    plan_path = req.get('planPath') or ''
    check([o['key'] for o in req.get('options') or []] == ['keep', 'accept', 'ask'] and '沿用跳过确认' in req['options'][0]['label'],
          f'计划：原模式是跳过确认时选项为 沿用 / 自动处理修改 / 每处修改问我：{[o["label"] for o in req.get("options") or []]}')
    check(bool(wait_text(sid, 'pkeep 执行完了')) and st(sid).get('mode') == 'bypass permissions'
          and os.path.exists(os.path.join(L.PROJ, 'pkeep.txt')), f'计划 keep：执行完、回到跳过确认（{st(sid).get("mode")}）、命令没再问就执行了')
    # 2) 自动处理修改
    plan_round(ev, sid, 'paccept', 'accept')
    ok = wait_until(lambda: ('paccept 执行完了' in L.tx_text(sid) and idle(sid)) or _approve_any(sid), 60)
    if ok == 'approved':
        ok = wait_text(sid, 'paccept 执行完了')
    check(bool(ok) and st(sid).get('mode') == 'accept edits', f'计划 accept：执行完，模式为自动接受编辑（{st(sid).get("mode")}）')
    # 3) 修改意见：拒绝并把意见交给模型，留在规划模式
    msg = '第一步之前先备份 a.txt'
    plan_round(ev, sid, 'pfeed', 'feedback', {'message': msg}, bash=False)
    ok = wait_text(sid, 'pfeed 执行完了')
    check(bool(ok) and any(r == '用户没有批准这个计划，修改意见：' + msg for r in tool_results(sid)) and st(sid).get('mode') == 'plan mode',
          f'计划 feedback：模型收到意见（tool_result），仍在规划模式（{st(sid).get("mode")}）')
    # 4) 每处修改问我：之后的命令弹授权
    _, t0 = plan_round(ev, sid, 'pask', 'ask')
    e = ev.wait(lambda e: e.get('type') == 'perm' and e['req']['sid'] == sid and e['req']['tool'] == 'Bash', 30, t0)
    check(bool(e) and 'pask.txt' in json.dumps(e['req'].get('input'), ensure_ascii=False), '计划 ask：之后的命令要授权')
    if e:
        api('perm', {'rid': e['req']['rid'], 'decision': 'allow'})
    check(bool(wait_text(sid, 'pask 执行完了')) and st(sid).get('mode') == 'manual mode', f'计划 ask：执行完，模式为手动确认（{st(sid).get("mode")}）')
    plan_not_changes(sid, plan_path)
    return sid


def plan_not_changes(sid, plan_path):
    """修复线：计划模式写的计划文件（~/.claude/plans/）不进改动胶囊、审阅列表，也不回退。"""
    real = os.path.realpath(os.path.join(HOME, '.claude', 'plans')) + '/'
    tc = api('turn-changes', {'sid': sid})
    paths = [f.get('real') or f['path'] for t in tc.get('turns') or [] for f in t['files']] + [f.get('real') or f['path'] for f in (tc.get('all') or {}).get('files') or []]
    check('turns' in tc and not any(os.path.realpath(p).startswith(real) for p in paths), f'修复线：turn-changes 不含计划文件（{len(paths)} 项）')
    r = api('turn-diff', {'sid': sid, 'uuid': '*', 'path': plan_path})
    check(bool(plan_path) and r.get('_http') == 400, f'修复线：turn-diff 不给计划文件（{r.get("error")}）')
    u = next((x.get('uuid') for x in rows(sid) if x.get('type') == 'user' and isinstance((x.get('message') or {}).get('content'), str)
              and x['message']['content'].startswith('pkeep ')), None)
    pv = api('rewind-preview', {'sid': sid, 'uuid': u or ''})
    fs = [f.get('path') or '' for f in (pv.get('write') or []) + (pv.get('remove') or [])]
    check(bool(u) and 'write' in pv and not any(os.path.realpath(os.path.expanduser(p)).startswith(real) for p in fs),
          f'修复线：回退预览不含计划文件（{fs}）')


def _approve_any(sid):
    """计划之后 CLI 若对命令再要授权就批准（自动接受编辑模式下 Bash 是否要问取决于 CLI），返回 'approved'。"""
    for p in api('open', {'sid': sid}).get('perms') or []:
        if p['tool'] == 'Bash':
            api('perm', {'rid': p['rid'], 'decision': 'allow'})
            return 'approved'
    return None


def scenario_deny(ev, sid):
    t0 = time.time()
    api('send', {'sid': sid, 'text': 'deny1 ' + fake([('Bash', {'command': 'rm -f keep-me.txt', 'description': '删除文件'})], say_='好的不删了')})
    e = ev.wait(lambda e: e.get('type') == 'perm' and e['req']['sid'] == sid and e['req']['tool'] == 'Bash', 30, t0)
    d = api('perm', {'rid': (e or {}).get('req', {}).get('rid'), 'allow': False, 'message': '这个文件还要用，别删'})
    ok = wait_text(sid, '好的不删了')
    check(d.get('ok') and bool(ok) and '用户拒绝了这次操作，说明：这个文件还要用，别删' in tool_results(sid),
          '拒绝并说明：说明作为工具结果交给模型，模型接着回复')


def scenario_tasks(ev):
    sid = L.new_session({'mode': 'bypass permissions'})
    one = 'sleep 300; echo a2-one'
    api('send', {'sid': sid, 'text': 'bg1 ' + fake([('Bash', {'command': one, 'run_in_background': True, 'description': '后台一'})], say_='一已启动')})
    wait_text(sid, '一已启动')
    tid = wait_until(lambda: shell_id(sid, one), 10)
    t0 = time.time()
    d = api('task-stop', {'sid': sid, 'id': tid})
    check(d.get('ok') and d.get('confirmed') and shell_state(sid, tid) == 'killed' and no_panel(sid) and idle(sid),
          f'停止后台任务（只有一个，详情页）：{d}，{time.time() - t0:.1f}s，面板已关')
    check(api('task-stop', {'sid': sid, 'id': tid}).get('already') is True, '已经停掉的再停：返回 already')
    two, three = 'sleep 301; echo a2-two', 'sleep 302; echo a2-three'
    api('send', {'sid': sid, 'text': 'bg2 ' + fake([('Bash', {'command': two, 'run_in_background': True, 'description': '后台二'}),
                                                     ('Bash', {'command': three, 'run_in_background': True, 'description': '后台三'})],
                                                    say_='二三已启动')})
    wait_text(sid, '二三已启动')
    t2, t3 = wait_until(lambda: shell_id(sid, two), 10), wait_until(lambda: shell_id(sid, three), 10)
    d = api('task-stop', {'sid': sid, 'id': t2})
    check(d.get('ok') and shell_state(sid, t2) == 'killed' and shell_state(sid, t3) == 'run' and no_panel(sid),
          f'停止后台任务（两个，列表页移动光标）：只停了选中的那条：{d}')
    d = api('task-stop', {'sid': sid, 'id': t3})
    check(d.get('ok') and shell_state(sid, t3) == 'killed' and no_panel(sid), '剩下一个时再停（详情页）')
    four = 'sleep 303; echo a2-four'
    api('send', {'sid': sid, 'text': 'bg4 ' + fake([('Bash', {'command': four, 'run_in_background': True, 'description': '后台四'})], say_='四已启动')})
    wait_text(sid, '四已启动')
    t4 = wait_until(lambda: shell_id(sid, four), 10)
    api('send', {'sid': sid, 'text': 'SLOWTEST:12 慢慢回复一'})
    wait_until(lambda: st(sid).get('busy'), 10)
    d = api('task-stop', {'sid': sid, 'id': t4})
    busy = st(sid).get('busy')
    check(d.get('ok') and shell_state(sid, t4) == 'killed' and busy, f'回复进行中停止后台任务：{d}，回复仍在进行')
    check(bool(wait_until(lambda: '第40段' in L.tx_text(sid) and idle(sid), 40)), '回复没被打断，照常答完')
    d = api('task-stop', {'sid': sid, 'id': 'no-such'})
    check(d.get('_http') == 400 and d.get('error'), f'不存在的任务：返回错误（{d.get("error")}）')
    return sid


def scenario_btw(ev, sid):
    t0 = time.time()
    n0 = len(L.tx_text(sid))
    d = api('btw', {'sid': sid, 'q': '旁问一'})
    e = ev.wait(lambda e: e.get('type') == 'btw' and e.get('id') == d.get('id'), 40, t0)
    check(bool(e) and e.get('sid') == sid and e.get('q') == '旁问一' and e.get('a') == '收到：旁问一（共 3 字）' and isinstance(e.get('t'), int),
          f'旁问（空闲）：推 btw 事件带答案（{time.time() - t0:.1f}s）：{e}')
    check(bool(wait_until(lambda: idle(sid) and no_panel(sid), 5)), '旁问（空闲）：面板已关、输入框回来')
    # 回复进行中
    api('send', {'sid': sid, 'text': 'SLOWTEST:12 慢慢回复二'})
    wait_until(lambda: st(sid).get('busy') and '第3段' in '\n'.join(screen(sid).get('lines') or []), 15)
    t1 = time.time()
    d2 = api('btw', {'sid': sid, 'q': '回复中的旁问二'})
    e = ev.wait(lambda e: e.get('type') == 'btw' and e.get('id') == d2.get('id'), 40, t1)
    check(bool(e) and e.get('a') == '收到：回复中的旁问二（共 7 字）' and st(sid).get('busy'),
          f'旁问（回复进行中）：拿到答案，回复仍在进行（{time.time() - t1:.1f}s）')
    check(bool(wait_until(lambda: '第40段' in L.tx_text(sid) and idle(sid), 40)) and no_panel(sid), '旁问（回复进行中）：主回复照常答完，面板已关')
    # 经 send 发 /btw
    t2 = time.time()
    d3 = api('send', {'sid': sid, 'text': '/btw 旁问三'})
    e = ev.wait(lambda e: e.get('type') == 'btw' and e.get('id') == d3.get('id'), 40, t2)
    check(d3.get('btw') and bool(e) and e.get('a') == '收到：旁问三（共 3 字）', 'send 里的 /btw 走旁问')
    items = api('btw-list', {'sid': sid}).get('items') or []
    check([x['q'] for x in items][-3:] == ['旁问一', '回复中的旁问二', '旁问三'] and all(x.get('a') for x in items[-3:]),
          'btw-list：三条记录已落盘')
    tx = L.tx_text(sid)
    check('旁问一' not in tx[n0:] and '旁问三' not in tx, '旁问不进会话记录')
    check(api('send', {'sid': sid, 'text': '/btw'}).get('_http') == 400, '/btw 不带问题：报错，不发给 CLI')


def settings_has(path):
    hits = []
    for p in (os.path.join(HOME, '.claude', 'settings.json'), os.path.join(HOME, '.claude', 'settings.local.json'),
              os.path.join(L.PROJ, '.claude', 'settings.json'), os.path.join(L.PROJ, '.claude', 'settings.local.json')):
        try:
            with open(p, encoding='utf-8') as f:
                if path in f.read():
                    hits.append(p)
        except OSError:
            pass
    return hits


def scenario_add_dir(ev, sid):
    d1 = os.path.join(EXTRA, '子 文件夹')
    d2 = os.path.join(EXTRA, '第二个')
    os.makedirs(d1, exist_ok=True)
    os.makedirs(d2, exist_ok=True)
    t0 = time.time()
    d = api('add-dir', {'sid': sid, 'path': d1})
    check(d.get('ok') and d.get('dir') == d1 and d1 in (st(sid).get('dirs') or []) and no_panel(sid) and idle(sid),
          f'添加文件夹（路径含空格和中文）：{d}，{time.time() - t0:.1f}s，运行状态 dirs={st(sid).get("dirs")}')
    tx = wait_until(lambda: (lambda t: t if 'as a working directory for this session' in t else None)(L.tx_text(sid)), 5)
    check(bool(tx) and not settings_has(d1), f'选的是“仅本次会话”：没有写进任何设置文件（{settings_has(d1)}）')
    check(api('add-dir', {'sid': sid, 'path': d1}) == {'ok': True, 'dir': d1}, '重复添加：直接返回成功')
    os.makedirs(os.path.join(L.PROJ, 'inner'), exist_ok=True)
    d = api('add-dir', {'sid': sid, 'path': os.path.join(L.PROJ, 'inner')})
    check(d.get('ok') and d.get('already') and len(st(sid).get('dirs') or []) == 1 and no_panel(sid), f'已在工作范围里的文件夹：{d}')
    d = api('add-dir', {'sid': sid, 'path': '/tmp/cwb-r15-a2-no-such-dir'})
    check(d.get('_http') == 400 and '不存在' in (d.get('error') or ''), '不存在的文件夹：报错')
    d = api('send', {'sid': sid, 'text': '/add-dir ' + d2})
    check(d.get('ok') and d.get('addDir') and d2 in (st(sid).get('dirs') or []) and not settings_has(d2), 'send 里的 /add-dir 走添加文件夹')


def scenario_compact(ev, sid):
    t0 = time.time()
    api('send', {'sid': sid, 'text': '/compact 侧重保留文件改动'})
    s = ev.wait(lambda e: e.get('type') == 'compact' and e.get('sid') == sid and e.get('phase') == 'start', 30, t0)
    e = ev.wait(lambda e: e.get('type') == 'compact' and e.get('sid') == sid and e.get('phase') == 'end', 60, t0)
    check(bool(s) and s.get('trigger') == 'manual' and s.get('instructions') == '侧重保留文件改动', f'压缩开始事件：{s}')
    check(bool(e) and '假网关给的压缩摘要' in (e.get('summary') or ''), f'压缩结束事件带摘要（{time.time() - t0:.1f}s）')
    wait_until(lambda: idle(sid), 20)
    time.sleep(1)
    items = api('open', {'sid': sid}).get('items') or []
    j = json.dumps(items, ensure_ascii=False)
    check('completed successfully' not in j and 'hook.py' not in j and any(i.get('k') == 'note' and 'Compacted' in i.get('text', '') for i in items),
          '时间线里有压缩完成的提示，没有钩子执行成功的提示行')
    check('completed successfully' in L.tx_text(sid), '（对照）会话记录里确实有钩子成功提示行')
    d = api('open', {'sid': sid})
    cs = d.get('compacts') or []
    note = next((i for i in reversed(d.get('items') or []) if i.get('k') == 'note' and 'Compacted' in i.get('text', '')), {})
    check(bool(cs) and '假网关给的压缩摘要' in cs[-1].get('summary', '') and bool(note.get('at')) and abs(note['at'] - cs[-1]['t']) < 120000,
          f'修复线：open 带落盘的压缩摘要，“Compacted”那一行带完成时间、两者对得上（相差 {abs((note.get("at") or 0) - (cs[-1]["t"] if cs else 0))}ms）')


def scenario_commands(sid):
    before = '\n'.join(screen(sid).get('lines') or [])
    d = api('send', {'sid': sid, 'text': '/Model claude-opus-5-5'})
    time.sleep(1)
    after = '\n'.join(screen(sid).get('lines') or [])
    check(d.get('blocked') is True and d.get('hint') and d.get('cmd') == '/model' and '/Model' not in after,
          f'被拦截的命令不发给 CLI：{d.get("error")}（{d.get("hint")}）')
    check(before.count('/model') == after.count('/model'), '屏幕上没有出现 /model')
    cs = api('commands', {'cwd': L.PROJ}).get('commands') or []
    b = {c['name']: c for c in cs if c.get('src') == '内置'}
    check({'compact', 'btw', 'plan', 'context', 'export', 'recap', 'init', 'code-review', 'security-review', 'simplify'} <= set(b)
          and all(c.get('group') in ('chat', 'review') for c in b.values()), 'commands：内置命令扩充并分组')
    bl = api('projects').get('blockedCmds') or []
    check(any(x['cmd'] == '/model' for x in bl) and any(x['cmd'] == '/fork' for x in bl), f'projects：blockedCmds {len(bl)} 条')


def scenario_uuid(sid):
    items = api('open', {'sid': sid, 'all': True}).get('items') or []
    users = [i for i in items if i.get('k') == 'user' and not i.get('mid')]
    recs = {r.get('uuid'): r for r in rows(sid) if r.get('type') == 'user'}
    ok = bool(users) and all(i.get('uuid') in recs for i in users)
    cmds = [i for i in items if i.get('k') == 'cmd']
    check(ok, f'每轮开头的用户条目带 uuid，且都对得上会话记录（{len(users)} 条）')
    check(bool(cmds) and all(i.get('uuid') in recs and '<command-name>' in json.dumps(recs[i['uuid']].get('message'), ensure_ascii=False)
                             for i in cmds), f'命令条目带那条命令记录的 uuid（{len(cmds)} 条）')


def run_all():
    ensure_gw()
    L.stop_server()
    L.start_server()
    os.makedirs(L.PROJ, exist_ok=True)
    ev = L.Events()
    time.sleep(0.5)
    g0 = len(L.gw_rows())
    sp = scenario_plan(ev)
    scenario_deny(ev, sp)
    sb = scenario_tasks(ev)
    scenario_btw(ev, sb)
    scenario_add_dir(ev, sb)
    scenario_compact(ev, sb)
    scenario_commands(sb)
    scenario_uuid(sb)
    L.ua_check(g0)
    ev.stop = True


def to_trash(p, tag):
    if not os.path.lexists(p):
        return 0
    dst = os.path.join(TRASH, f'cwb-r15-a2-{tag}-{int(time.time() * 1000)}-{os.path.basename(p)}')
    return 1 if subprocess.run(['mv', p, dst]).returncode == 0 else 0


def cleanup():
    """结束自己建的 CLI、测试服务、ptyd、假网关（都按 PID 并核对命令行），把测试会话记录、计划文件、测试文件夹移到废纸篓。"""
    d = st_load()
    sids = d.get('sids', [])
    if L.ping().startswith('ok'):
        for s in sids:
            try:
                api('stop', {'sid': s})
            except Exception:
                pass
        time.sleep(3)
    L.stop_server()
    pp = L.kill_ptyd(signal.SIGTERM)
    if pp:
        wait_until(lambda: not alive(pp), 10)
        say('测试 ptyd 已结束', pp)
    gw = d.get('gw')
    if gw and alive(gw) and 'fakegw.py' in cmd_of(gw) and str(L.GW) in cmd_of(gw):
        os.kill(gw, signal.SIGTERM)
        say('假网关已结束', gw)
    n = 0
    for s in sids:
        for p in (os.path.join(L.PROJ_DIR, s + '.jsonl'), os.path.join(L.PROJ_DIR, s),
                  os.path.join(HOME, '.claude', 'session-env', s), os.path.join(HOME, '.claude', 'file-history', s),
                  os.path.join(L.RUN, 'btw', s + '.json')):
            n += to_trash(p, 'sess')
    for p in d.get('plans', []):
        if os.path.dirname(p) == os.path.join(HOME, '.claude', 'plans'):
            n += to_trash(p, 'plan')
    n += to_trash(EXTRA, 'dir')
    if os.path.isdir(L.PROJ_DIR) and not [x for x in os.listdir(L.PROJ_DIR) if x.endswith('.jsonl')]:
        n += to_trash(L.PROJ_DIR, 'proj')  # 测试文件夹对应的 CLI 项目目录（只剩 CLI 自己的 memory 等）
    say('已移到废纸篓：', len(sids), '个会话，共', n, '项')
    d['sids'], d['plans'] = [], []
    st_save(d)


if __name__ == '__main__':
    sc = sys.argv[1] if len(sys.argv) > 1 else 'all'  # all：跑完自动清理；run：跑完保留现场（之后再 cleanup）
    if sc == 'cleanup':
        cleanup()
    else:
        try:
            run_all()
        finally:
            if sc == 'all':
                cleanup()
        say('全部通过' if not L.FAIL else f'{len(L.FAIL)} 项失败：' + '；'.join(L.FAIL))
        sys.exit(1 if L.FAIL else 0)
