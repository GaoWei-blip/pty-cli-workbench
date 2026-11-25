"""第十七轮 M 端到端：多智能体协作。
真实 claude（伪终端交互式）＋ 假网关 tests/fakegw.py（18851，看请求头、按 FAKE 步骤调 mcp__cwb__delegate_task）、
真 codex app-server ＋ 假 Responses 后端 tests/fakecodex.py（18852，MCP[[工具|参数]]END 调委派工具）、假 ACP agent tests/fakeacp.py。
测试服务 47971（CWB_TITLE_DELAY=99999），数据目录、项目文件夹、CODEX_HOME 都在 /tmp/cwb-m-e2e；config.json 先备份、结束时还原。
只结束自己起的进程（按 PID 和命令行核对），不调用 stop.sh。
运行：.venv/bin/python -I tests/e2e_r17_M.py [场景…]   场景：off prewarm c2c cx2 codex cascade timeout lost busy review uacheck ui（默认全部）
"""
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
X = '/tmp/cwb-m-e2e'
os.environ.update(CWB_E2E_PORT='47971', CWB_E2E_GW='18851', CWB_E2E_PROJ=X + '/proj', CWB_E2E_STATE=X + '/state.json',
                  CWB_E2E_GWLOG=X + '/gw/requests.jsonl')
import e2e_lib as E  # noqa: E402

ROOT, PY = E.ROOT, os.path.join(E.ROOT, '.venv/bin/python')
PROJ, DATA, CHOME, ACPST, FCLOG, SHOTS = X + '/proj', X + '/data', X + '/chome', X + '/acp', X + '/fc', X + '/shots'
FC_PORT = 18852
PIDS = {}
CONF = os.path.join(ROOT, 'config.json')
CONF_BAK = X + '-config.json.bak'
CODEX_ARGS = ['-c', 'model_provider="fake"', '-c', 'model_providers.fake.name="fake"',
              '-c', f'model_providers.fake.base_url="http://127.0.0.1:{FC_PORT}/v1"', '-c', 'model_providers.fake.wire_api="responses"']
SRV_ENV = dict(CWB_TITLE_DELAY='99999', CWB_DATA_DIR=DATA, CWB_CODEX_HOME=CHOME, CWB_CODEX_ARGS=json.dumps(CODEX_ARGS),
               CWB_AGY_CMD=json.dumps([PY, '-I', os.path.join(HERE, 'fakeacp.py'), ACPST]), CWB_COLLAB_LOST='6', CWB_COLLAB_MINUTE='10')
api, check, say, wait_until = E.api, E.check, E.say, E.wait_until
TERM = ('completed', 'failed', 'cancelled', 'interrupted')


def spawn(name, argv, env=None):
    lf = open(f'{X}/{name}.log', 'ab')
    p = subprocess.Popen(argv, cwd=ROOT, env=dict(os.environ, **(env or {})), stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                         start_new_session=True)
    PIDS[name] = p.pid
    return p.pid


def kill(name, needle):
    pid = PIDS.pop(name, 0)
    if pid and E.alive(pid) and needle in E.cmd_of(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(60):
            if not E.alive(pid):
                break
            time.sleep(0.05)


def setup():
    if os.path.isdir(X):
        shutil.rmtree(X)
    for d in (PROJ, DATA, CHOME, ACPST, FCLOG, SHOTS, X + '/gw'):
        os.makedirs(d, exist_ok=True)
    shutil.copy(CONF, CONF_BAK)
    with open(CHOME + '/config.toml', 'w') as f:
        f.write('# 第十七轮 M 测试用\nmodel = "fake-model"\n[features]\nplugins = false\nremote_plugin = false\n')
    spawn('fakecodex', [PY, '-I', os.path.join(HERE, 'fakecodex.py'), str(FC_PORT), FCLOG], {'FAKECODEX_BODIES': '1'})
    spawn('fakegw', [PY, '-I', os.path.join(HERE, 'fakegw.py'), '18851', X + '/gw'], {'FAKEGW_BODIES': '1'})
    time.sleep(0.6)
    E.start_server(**SRV_ENV)


def teardown():
    for sid in E.st_load().get('sids', []):
        try:
            api('stop', {'sid': sid})
        except Exception:
            pass
    time.sleep(1)
    E.stop_server()
    E.kill_ptyd(signal.SIGTERM)
    kill('fakecodex', 'fakecodex.py')
    kill('fakegw', 'fakegw.py')
    # 结束本测试留下的孤儿协议进程（按命令行里的临时目录核对）
    out = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(' ')
        if X in cmd and ('codex' in cmd or 'fakeacp' in cmd) and pid.isdigit() and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
    if os.path.exists(CONF_BAK):
        shutil.copy(CONF_BAK, CONF)
        os.remove(CONF_BAK)
    for p in glob.glob(os.path.join(E.RUN, 'mcp-*.json')):
        os.remove(p)
    # 删掉测试会话的 Claude 记录
    for sid in E.st_load().get('sids', []) + E.st_load().get('kids', []):
        for p in glob.glob(os.path.expanduser(f'~/.claude/projects/*/{sid}.jsonl')) + glob.glob(os.path.expanduser(f'~/.claude/projects/*/{sid}')):
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)


def prefs(**kw):
    d = api('collab-prefs', {'set': kw})
    return d.get('prefs') or d


def new_parent(mode):
    sid = E.new_session({'mode': mode})
    time.sleep(1.0)  # 让 CLI 连上 cwb（本机 HTTP，通常几十毫秒）
    return sid


def remember_kid(sid):
    s = E.st_load()
    s.setdefault('kids', []).append(sid)
    E.st_save(s)


def ps_rows():
    out = subprocess.run(['ps', '-axww', '-o', 'pid=,ppid=,command='], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        f = line.split(None, 2)
        if len(f) == 3 and f[0].isdigit() and f[1].isdigit():
            rows.append((int(f[0]), int(f[1]), f[2]))
    return rows


def cli_pid(sid):
    for pid, _, cmd in ps_rows():
        exe = cmd.split(' ', 1)[0]
        if os.path.basename(exe) == 'claude' and (f'--session-id {sid}' in cmd or f'--resume {sid}' in cmd):
            return pid
    return 0


def argv_of(sid):
    """这个对话的 claude 进程的命令行（ptyd 起的交互式 CLI）。"""
    pid = cli_pid(sid)
    return next((cmd for p, _, cmd in ps_rows() if p == pid), '') if pid else ''


def wait_task(ev, pred, timeout=60, t0=0):
    e = ev.wait(lambda e: e.get('type') == 'cwb-task' and pred(e['task']), timeout, t0)
    return e and e['task']


def approve(ev, t0, tool_re=r'delegate_task', timeout=40, n=1):
    """批准 n 张授权卡（委派工具），返回第一张。"""
    done, first = set(), None
    end = time.time() + timeout
    while len(done) < n and time.time() < end:
        e = ev.wait(lambda e: e.get('type') == 'perm' and re.search(tool_re, e['req'].get('tool') or '') and e['req']['rid'] not in done,
                    max(0.1, end - time.time()), t0)
        if not e:
            break
        done.add(e['req']['rid'])
        first = first or e
        api('perm', {'rid': e['req']['rid'], 'decision': 'allow'})
    return first


TOOLSEARCH = [{'name': 'ToolSearch', 'input': {'query': 'select:mcp__cwb__delegate_task', 'max_results': 5}}]


def fake(*steps, say='parent-done'):
    """假网关按步回工具调用。cwb 的工具和 T3 一样放在 ToolSearch 后面，用之前先加一步 ToolSearch（编排说明里也这么要求）。"""
    if any(str(x.get('name', '')).startswith('mcp__cwb__') for s in steps for x in s):
        steps = (TOOLSEARCH,) + steps
    return ' '.join('FAKE[[' + json.dumps(s, ensure_ascii=False) + ']]END' for s in steps) + f' SAY[[{say}]]END'


def items(sid):
    return api('open', {'sid': sid}).get('items') or []


def x3():
    try:
        return [json.loads(l) for l in open(DATA + '/tasks.jsonl', encoding='utf-8')]
    except OSError:
        return []


def mcp_post(body, tok, host=None):
    req = urllib.request.Request(f'http://127.0.0.1:{E.PORT}/mcp', data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok, **({'Host': host} if host else {})})
    try:
        with E.OP.open(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b'null')
    except urllib.error.HTTPError as e:
        return e.code, None


# ================================================================ 场景
def sc_off(ev):
    say('== 协作关：启动参数不变、/mcp 门禁')
    prefs(on=False)
    n0 = len(E.gw_rows())
    sid = E.new_session({'mode': 'manual mode'})
    a = argv_of(sid)
    OFF_ARGV[0] = a.replace(sid, '<SID>')
    check(a and '--mcp-config' not in a and 'Claude Workbench orchestration' not in a, f'协作关：claude 启动参数里没有 --mcp-config（{a[:80]}…）')
    api('send', {'sid': sid, 'text': '回复 ok'})
    wait_until(lambda: E.runner_of(sid) and not E.runner_of(sid).get('busy') and E.tx_text(sid).count('"assistant"'), 30)
    E.ua_check(n0)
    st, _ = mcp_post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}, 'bad-token')
    check(st == 401, f'/mcp 令牌错：401（{st}）')
    st, _ = mcp_post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}, 'bad-token', host='mac.tail1.ts.net')
    check(st == 403, f'/mcp 远程 Host：403（{st}）')
    return sid


OFF_ARGV = ['']


def prewarm_cli(t0, timeout=20):
    """预热起的那个 claude（t0 之后、cwd 是测试项目、命令行带 --session-id）。"""
    def find():
        for pid, _, cmd in ps_rows():
            if os.path.basename(cmd.split(' ', 1)[0]) == 'claude' and '--session-id ' in cmd and pid not in SEEN_CLI and '127.0.0.1:18851' in cmd:
                return pid, cmd
        return None
    return wait_until(find, timeout)


SEEN_CLI = set()


def sc_prewarm(ev):
    say('== 预热：开着协作时预热，关掉后新建（复核 M-2）')
    SEEN_CLI.update(pid for pid, _, cmd in ps_rows() if os.path.basename(cmd.split(' ', 1)[0]) == 'claude')
    prefs(on=True)
    api('prewarm', {'cwd': PROJ})
    got = prewarm_cli(time.time())
    check(got and '--mcp-config' in got[1], '协作开着时预热：预热的 CLI 挂了 cwb')
    prefs(on=False)
    gone = wait_until(lambda: not E.alive(got[0]), 15) if got else False
    check(bool(gone), '关掉协作：开着时预热的 CLI 停掉了')
    sid = E.new_session({'mode': 'manual mode'})
    a = argv_of(sid)
    check(a and '--mcp-config' not in a and 'wait-task' not in a and 'orchestration' not in a and a.replace(sid, '<SID>') == OFF_ARGV[0],
          '关掉后新建：参数和一开始协作关着时新建的逐字一样（不带 cwb）')
    SEEN_CLI.update(pid for pid, _, cmd in ps_rows() if os.path.basename(cmd.split(' ', 1)[0]) == 'claude')
    api('prewarm', {'cwd': PROJ})
    got = prewarm_cli(time.time())
    check(got and '--mcp-config' not in got[1], '协作关着时预热：不挂 cwb')
    prefs(on=True)
    sid2 = new_parent('manual mode')
    a2 = argv_of(sid2)
    check('--mcp-config' in a2, '打开后新建：挂上 cwb（关着时预热的那个不再复用）')
    prefs(on=False)


def sc_c2c(ev):
    say('== Claude 父对话 → Claude 子对话（授权卡、叫醒、深度）')
    prefs(on=True)
    n0 = len(E.gw_rows())
    sid = new_parent('manual mode')
    a = argv_of(sid)
    check('--mcp-config' in a and a.count('--append-system-prompt') == 1 and ' -p ' not in a and 'Bearer' not in a,
          '协作开：父对话多了 --mcp-config，只有一个 --append-system-prompt，令牌不在命令行')
    t0 = time.time()
    api('send', {'sid': sid, 'text': fake([{'name': 'mcp__cwb__orchestrator_capabilities', 'input': {}}],
                                          [{'name': 'mcp__cwb__delegate_task', 'input': {
                                              'task': 'say hi from child', 'title': '子任务甲', 'target': {'providerInstanceId': 'claude'}}}])})
    p = approve(ev, t0)
    caps = [e for _, e in ev.since(t0, lambda e: e.get('type') == 'perm' and 'orchestrator_capabilities' in (e['req'].get('tool') or ''))]
    pv = (p or {}).get('req', {}).get('input', {}).get('_cwb_preview') or {}
    check(p and pv.get('who') == 'Claude' and pv.get('to') == 'Claude' and pv.get('mode') == '每步确认' and pv.get('task') == 'say hi from child',
          f'手动档：委派前出授权卡，写明给谁、模型、权限档、任务（{pv}）')
    t = wait_task(ev, lambda t: t['parent'] == sid and t['status'] in TERM, 60, t0)
    check(t and t['status'] == 'completed' and t['summary'].startswith('收到：say hi from child'), f'子任务完成，结果是子对话最后一条回复（{(t or {}).get("summary")}）')
    kid = t['child']
    remember_kid(kid)
    rows0 = [json.loads(l) for l in open(E.transcript(sid), encoding='utf-8')] if E.transcript(sid) else []
    capok = any('tool_result' in json.dumps(r) and 'runtimeModes' in json.dumps(r) for r in rows0 if r.get('type') == 'user')
    check(not caps and capok, f'先查能派给谁（orchestrator_capabilities）：钩子直接放行、拿到结果，不出授权卡（授权卡 {len(caps)} 张）')
    ka = argv_of(kid)
    check(ka and '--mcp-config' not in ka and 'orchestration' not in ka and '--permission-mode manual' in ka,
          '子对话：同一个交互式 claude 启动，没有 cwb（深度一层），权限和父对话一样（manual）')
    ok = wait_until(lambda: any(i.get('k') == 'sys' and i.get('text') == '子任务已完成，结果已交回' for i in items(sid)), 40)
    check(ok, '父对话被叫醒：时间线上是灰色系统行“子任务已完成，结果已交回”')
    rows = [json.loads(l) for l in open(E.transcript(sid), encoding='utf-8')] if E.transcript(sid) else []
    wake = [r for r in rows if r.get('type') == 'user' and 'PostToolUse:mcp__cwb__delegate_task' in json.dumps(r, ensure_ascii=False)]
    check(wake and wake[0].get('origin', {}).get('kind') == 'task-notification' and wake[0].get('promptSource') == 'system',
          f'记录里叫醒那一行：origin.kind={wake[0].get("origin") if wake else None}、promptSource=system（不是 human）')
    check(not any(i.get('k') == 'user' and 'Delegated task' in (i.get('text') or '') for i in items(sid)), '叫醒没有画成用户气泡')
    wait_until(lambda: E.runner_of(sid) and not E.runner_of(sid).get('busy'), 20)
    tt = api('collab-tasks', {'sid': sid})['tasks'][-1]
    check(tt['delivery'] == 'acknowledged' and tt['wake'] == 'hook', '交回方式：钩子（asyncRewake）')
    rws = [r for r in x3() if r.get('id') == t['id']]
    check([r.get('status') for r in rws if r['t'] == 'task_status'][:1] == ['queued'] and rws[0]['t'] == 'task' and rws[-1].get('delivered') is True,
          'data/tasks.jsonl：task 行、状态行，最后 delivered:true')
    ch = api('open', {'sid': kid})
    check((ch.get('cwb') or {}).get('task', {}).get('id') == t['id'] and (ch.get('cwb') or {}).get('parentTitle'), '子对话知道是谁委派的（顶部横条用）')
    E.ua_check(n0)
    return sid, kid


def sc_cx2(ev):
    say('== Claude 父对话同时派给 Codex 和 Antigravity（两个同时叫醒）')
    prefs(on=True)
    sid = new_parent('accept edits')
    t0 = time.time()
    api('send', {'sid': sid, 'text': fake([
        {'name': 'mcp__cwb__delegate_task', 'input': {'task': 'codex please say ok', 'title': '给 Codex', 'target': {'providerInstanceId': 'codex'}}},
        {'name': 'mcp__cwb__delegate_task', 'input': {'task': 'agy please say ok', 'title': '给 agy', 'target': {'providerInstanceId': 'agy'}}}])})
    approve(ev, t0, n=2)
    end = time.time() + 60
    while time.time() < end and len([t for t in api('collab-tasks', {'sid': sid})['tasks'] if t['status'] in TERM]) < 2:
        time.sleep(0.5)
    ts = api('collab-tasks', {'sid': sid})['tasks']
    check(len(ts) == 2 and all(t['status'] == 'completed' for t in ts), f'两个子任务都完成：{[(t["childProv"], t["status"]) for t in ts]}')
    by = {t['childProv']: t for t in ts}
    check(by.get('codex', {}).get('mode') == 'auto-accept-edits', 'Codex 子任务权限跟父对话（accept edits → 自动改文件）')
    ok = wait_until(lambda: sum(1 for i in items(sid) if i.get('k') == 'sys' and i.get('text') == '子任务已完成，结果已交回') >= 2, 40)
    check(ok, '两个子任务同时完成：父对话两次叫醒，两行系统行')
    kb = [json.load(open(p)) for p in sorted(glob.glob(FCLOG + '/bodies/*.json'))]
    kb = [b for b in kb if 'codex please say ok' in json.dumps(b, ensure_ascii=False)]
    check(kb and not any('mcp__cwb' in json.dumps(b.get('tools') or []) for b in kb), 'Codex 子对话没有挂 cwb（深度一层）')
    for t in ts:
        c = json.loads(open(f'{DATA}/conv/{t["child"]}.jsonl', encoding='utf-8').readline())
        check(c.get('parent') == sid and c.get('taskId') == t['id'], f'{t["childLabel"]} 子对话元数据记了 parent、taskId')
    return sid


def sc_codex(ev):
    say('== Codex 父对话 → Antigravity 子对话（MCP 授权卡、turn/start 叫醒）')
    prefs(on=True)
    d = api('prov-new', {'prov': 'codex', 'cwd': PROJ, 'opts': {'mode': 'auto'}})
    psid = d['sid']
    t0 = time.time()
    args = json.dumps({'task': 'please say ok', 'title': '交给 agy', 'target': {'providerInstanceId': 'agy'}}, ensure_ascii=False)
    api('send', {'sid': psid, 'text': 'MCP[[delegate_task|' + args + ']]END'})
    p = approve(ev, t0)
    check(p and (p['req']['input'].get('_cwb_preview') or {}).get('who') == 'Codex', 'Codex 调委派工具要授权：出授权卡（Codex 想让 Antigravity 做）')
    t = wait_task(ev, lambda t: t['parent'] == psid and t['status'] in TERM, 60, t0)
    check(t and t['status'] == 'completed' and t['childProv'] == 'agy', 'Antigravity 子任务完成')
    ok = wait_until(lambda: any(i.get('k') == 'sys' and i.get('text') == '子任务已完成，结果已交回' for i in items(psid)), 30)
    check(ok, 'Codex 父对话空闲：新起一轮交回，时间线是系统行')
    wait_until(lambda: '"trigger": "cwb_task"' in open(f'{DATA}/conv/{psid}.jsonl', encoding='utf-8').read(), 20)
    ends = [json.loads(l) for l in open(f'{DATA}/conv/{psid}.jsonl', encoding='utf-8') if '"turn_end"' in l]
    check(ends and ends[-1].get('trigger') == 'cwb_task' and not ends[0].get('trigger'), 'turn_end 写 trigger:"cwb_task"（只有叫醒那一轮）')
    bodies = [json.load(open(p)) for p in sorted(glob.glob(FCLOG + '/bodies/*.json'))]
    b0 = next((b for b in bodies if 'MCP[[delegate_task' in json.dumps(b, ensure_ascii=False)), {})
    ns = [t for t in b0.get('tools') or [] if t.get('type') == 'namespace' and t.get('name') == 'mcp__cwb']
    check(ns and {x['name'] for x in ns[0]['tools']} == {'orchestrator_capabilities', 'delegate_task', 'task_status', 'task_cancel'},
          'Codex 线程挂上了 cwb（mcp_servers.cwb，四个工具）')
    check('Claude Workbench orchestration' in json.dumps(b0, ensure_ascii=False), 'Codex：编排说明在 developerInstructions 里')
    return psid


def sc_cascade(ev):
    say('== 级联：父对话点停止，子任务一起停')
    prefs(on=True)
    sid = new_parent('manual mode')
    t0 = time.time()
    api('send', {'sid': sid, 'text': fake([{'name': 'mcp__cwb__delegate_task', 'input': {
        'task': 'SLOW:30 long job', 'title': '慢任务', 'target': {'providerInstanceId': 'codex'}}}])})
    approve(ev, t0)
    t = wait_task(ev, lambda t: t['parent'] == sid and t['status'] == 'running', 40, t0)
    check(t, '子任务在跑')
    time.sleep(1)
    api('interrupt', {'sid': sid})
    t2 = wait_task(ev, lambda x: t and x['id'] == t['id'] and x['status'] in TERM, 20, t0)
    check(t2 and t2['status'] == 'cancelled' and t2['delivery'] == 'disposed', f'父对话停止：子任务 cancelled，不再叫醒（{(t2 or {}).get("status")}）')
    ok = wait_until(lambda: not (api('open', {'sid': t['child']}).get('runner') or {}).get('busy'), 25) if t else False
    check(ok, '子对话那一轮确实停了')
    rws = [r for r in x3() if r.get('id') == (t or {}).get('id') and r['t'] == 'task_status']
    check(rws and rws[-1]['status'] == 'stopped', 'X3：stopped')
    time.sleep(3)
    check(not any(i.get('k') == 'sys' and i.get('tag') == '子任务' for i in items(sid)) and rws[-1].get('delivered') is False,
          '停掉的子任务不叫醒父对话（钩子拿到 skip）')


def sc_timeout(ev):
    say('== 超时自动停（最长时间 1 分钟，测试里 1 分钟＝10 秒）')
    prefs(on=True, maxMins=1)
    sid = new_parent('manual mode')
    t0 = time.time()
    api('send', {'sid': sid, 'text': fake([{'name': 'mcp__cwb__delegate_task', 'input': {
        'task': 'SLOW:40 forever', 'title': '会超时', 'target': {'providerInstanceId': 'codex'}}}])})
    approve(ev, t0)
    t = wait_task(ev, lambda t: t['parent'] == sid and t['status'] in TERM, 40, t0)
    check(t and t['status'] == 'interrupted' and t['reason'] == 'timeout' and t['x3'] == 'timeout' and t['state'] == '超时',
          f'到时自动停：状态“超时”，X3 记 timeout（{(t or {}).get("x3")}）')
    ok = wait_until(lambda: any(i.get('k') == 'sys' and i.get('text') == '子任务已完成，结果已交回' for i in items(sid)), 30)
    check(ok, '超时也照常交回父对话')
    prefs(maxMins=30)


def hook_procs(parent_sid):
    """父对话 CLI 起的叫醒钩子进程（hook.py wait-task；CLI 可能经 sh 起它，往上找三层）。"""
    rows = ps_rows()
    pp = {p: q for p, q, _ in rows}
    cli = cli_pid(parent_sid)
    out = []
    for p, q, cmd in rows:
        if 'hook.py wait-task' in cmd and ROOT in cmd:
            a = q
            for _ in range(3):
                if a == cli:
                    out.append(p)
                    break
                a = pp.get(a, 0)
    return out


def sc_lost(ev):
    say('== 叫醒丢失（钩子进程被结束）→ 委派卡按钮交回')
    prefs(on=True)
    sid = new_parent('manual mode')
    t0 = time.time()
    api('send', {'sid': sid, 'text': fake([{'name': 'mcp__cwb__delegate_task', 'input': {
        'task': 'SLOW:6 a while', 'title': '会丢', 'target': {'providerInstanceId': 'codex'}}}])})
    approve(ev, t0)
    t = wait_task(ev, lambda t: t['parent'] == sid and t['status'] == 'running', 30, t0)
    pids = wait_until(lambda: hook_procs(sid), 15)
    check(bool(pids), f'父对话的叫醒钩子在后台等（hook.py wait-task，PID {pids}）')
    for p in pids or []:
        if 'hook.py wait-task' in E.cmd_of(p):
            os.kill(p, signal.SIGKILL)  # 模拟父 CLI 重启杀掉钩子进程
    t2 = wait_task(ev, lambda x: t and x['id'] == t['id'] and x['delivery'] == 'pending', 40, t0)
    check(t2 and t2['status'] == 'completed', '子任务完成后没人来取：记“待交回”')
    n_users = sum(1 for i in items(sid) if i.get('k') == 'user')
    r = api('collab-deliver', {'id': t['id']})
    check(r.get('ok') and r['task']['delivery'] == 'acknowledged', '点“把结果交给 Claude”')
    ok = wait_until(lambda: any(i.get('k') == 'user' and '子任务《会丢》（Codex）的结果' in (i.get('text') or '') for i in items(sid)), 30)
    check(ok and sum(1 for i in items(sid) if i.get('k') == 'user') == n_users + 1, '按现有发消息流程发出（这时算用户发的）')
    return sid, t


def sc_busy(ev):
    say('== 子任务结束时父对话正忙（实测叫醒排在这一轮之后）')
    prefs(on=True)
    sid = new_parent('manual mode')
    t0 = time.time()
    api('send', {'sid': sid, 'text': fake([{'name': 'mcp__cwb__delegate_task', 'input': {
        'task': 'SLOW:6 busy parent', 'title': '忙时交回', 'target': {'providerInstanceId': 'codex'}}}])})
    approve(ev, t0)
    t = wait_task(ev, lambda t: t['parent'] == sid and t['status'] == 'running', 30, t0)
    wait_until(lambda: E.runner_of(sid) and not E.runner_of(sid).get('busy'), 20)
    api('send', {'sid': sid, 'text': 'SLOWTEST:15 父对话正忙'})
    wait_until(lambda: (E.runner_of(sid) or {}).get('busy'), 10)
    t2 = wait_task(ev, lambda x: t and x['id'] == t['id'] and x['status'] in TERM, 30, t0)
    busy = bool((E.runner_of(sid) or {}).get('busy'))
    check(t2 and t2['status'] == 'completed' and busy, '子任务结束时父对话正在回复')
    ok = wait_until(lambda: any(i.get('k') == 'sys' and i.get('tag') == '子任务' for i in items(sid)), 40)
    rows = [json.loads(l) for l in open(E.transcript(sid), encoding='utf-8')] if E.transcript(sid) else []
    slow_end = max((r.get('timestamp') or '' for r in rows if r.get('type') == 'assistant' and '第40段' in json.dumps(r, ensure_ascii=False)), default='')
    wake = [r.get('timestamp') or '' for r in rows if (r.get('origin') or {}).get('kind') == 'task-notification']
    check(ok and slow_end and wake and wake[0] > slow_end, f'叫醒排在正在进行的这一轮之后（这一轮完整结束 {slow_end[11:23]}，叫醒 {wake[0][11:23] if wake else "-"}），不打断')
    check((t2 or {}).get('delivery') == 'acknowledged' or wait_until(
        lambda: api('collab-tasks', {'sid': sid})['tasks'][-1]['delivery'] == 'acknowledged', 5), '照常记已送达')


def sc_review(ev):
    say('== 手动复核：让 Codex 复核这轮改动（不经过模型、不受总开关控制）')
    prefs(on=False)
    sid = E.new_session({'mode': 'accept edits'})
    f = os.path.join(PROJ, 'rev_a.py')
    api('send', {'sid': sid, 'text': fake([{'name': 'Write', 'input': {'file_path': f, 'content': 'x = 1\nprint(x)\n'}}], say='写好了')})
    ok = wait_until(lambda: os.path.exists(f) and E.runner_of(sid) and not E.runner_of(sid).get('busy'), 40)
    tc = wait_until(lambda: [t for t in api('turn-changes', {'sid': sid}).get('turns') or [] if t.get('files')], 20)
    check(ok and tc, '这一轮改了文件（turn-changes 里有）')
    uuid_ = tc[0]['uuid'] if tc else ''
    t0 = time.time()
    r = api('collab-review', {'sid': sid, 'prov': 'codex', 'uuid': uuid_})
    check(r.get('ok') and r['task']['origin'] == 'manual' and r['task']['childProv'] == 'codex', f'协作关着也能手动复核（{r.get("error")}）')
    t = wait_task(ev, lambda x: r.get('task') and x['id'] == r['task']['id'] and x['status'] in TERM, 60, t0)
    ok = t and t['status'] == 'completed' and wait_until(lambda: api('collab-tasks', {'sid': sid})['tasks'][-1]['delivery'] == 'pending', 8)
    check(ok, '复核完成：不自动交回，委派卡上等用户点')
    kid = (t or {}).get('child')
    c = json.loads(open(f'{DATA}/conv/{kid}.jsonl', encoding='utf-8').readline()) if kid else {}
    check((c.get('opts') or {}).get('mode') == 'read-only', f'手动复核用 Codex 的只读档（{c.get("opts")}）')
    first = [json.loads(l) for l in open(f'{DATA}/conv/{kid}.jsonl', encoding='utf-8') if '"new"' in l and '"user"' in l][0]['it']['text'] if kid else ''
    check('只审阅，不要修改任何文件' in first and 'rev_a.py' in first and 'print(x)' in first, '任务＝这一轮的文件清单和差异＋只审阅')
    r2 = api('collab-deliver', {'id': t['id']}) if t else {}
    check(r2.get('ok'), '“把结果交给 Claude”')
    ok = wait_until(lambda: any(i.get('k') == 'user' and 'Codex 复核了这一轮改动' in (i.get('text') or '') for i in items(sid)), 30)
    check(ok, '复核意见作为用户消息发给父对话')
    return sid, t


def sc_uacheck(ev):
    say('== 自检：“协作开”用例')
    prefs(on=True)
    r = api('ua-check', {}, timeout=200)
    c = r.get('collab') or {}
    check(r.get('ok') and c.get('ok') and re.match(r'^claude-cli/\S+ \(external, cli\)$', c.get('ua') or ''),
          f'ua-check：普通 {r.get("ua")}；协作开 {c.get("ua")} {c.get("error") or ""}')


def sc_ui(ev, parent, review):
    say('== 界面（无头浏览器 DOM 断言，截图只自查）')
    import e2e_r15_g as G
    G.SESSION = 'm-e2e'
    ab, evj, wait = G.ab, G.ev, G.wait
    try:
        ab('set', 'viewport', '1280', '880')
        ab('open', 'http://127.0.0.1:%d/?t=%s' % (E.PORT, E.token()))
        wait('window.CWB && CWB.sj && CWB.S.projects', 20)
        psid, kid = parent
        evj("CWB.openSession(%s); return 1;" % json.dumps(psid))
        wait("CWB.S.cur===%s && document.querySelector('#feed .w-card')" % json.dumps(psid), 20)
        r = evj("""const c=document.querySelector('#feed .w-card'); return {vis: !!c && c.offsetHeight > 0, who: c && c.querySelector('.w-who').textContent, st: c && c.querySelector('.w-st').textContent,
          btns: c ? [...c.querySelectorAll('button')].map(b=>b.textContent) : [], sys: [...document.querySelectorAll('#feed .sysn .syst')].map(x=>x.textContent),
          head: (document.querySelector('#feed .it.tool.w-tool[data-cwb-task^=task_] > .head')||{}).textContent, users: [...document.querySelectorAll('#feed div.user')].length};""")
        check(r.get('vis') and r.get('who', '').startswith('交给 Claude') and r.get('st') == '已完成' and '打开子智能体' in r.get('btns', []) and '查看结果' in r.get('btns', []),
              f'委派卡（过程折叠时也看得见）：交给谁、状态、打开、查看结果（{r}）')
        check('子任务已完成，结果已交回' in (r.get('sys') or []) and r.get('users') == 1 and (r.get('head') or '').startswith('已委派子任务'),
              '叫醒画成灰色系统行，没有多出用户气泡；工具行写“已委派子任务”')
        ab('screenshot', SHOTS + '/1-parent-card.png')
        r = evj("""const rows=[...document.querySelectorAll('#projects .srow')]; const k=rows.find(x=>x.dataset.sid===%s); const p=rows.find(x=>x.dataset.sid===%s);
          return {kid: !!k && k.classList.contains('w-kid') && /↳/.test(k.textContent), after: !!(k && p) && rows.indexOf(k) > rows.indexOf(p)};""" % (json.dumps(kid), json.dumps(psid)))
        check(r.get('kid') and r.get('after'), f'左栏：子对话缩进挂在父对话下（↳）（{r}）')
        rkid = (review[1] or {}).get('child') or ''
        r = evj("""const k=[...document.querySelectorAll('#projects .srow')].find(x=>x.dataset.sid===%s);
          return {tags: k ? [...k.querySelectorAll('.w-kpv, .pv-tag, .v-ptag')].map(x=>x.textContent) : null};""" % json.dumps(rkid))
        check(r.get('tags') == ['Codex'], f'左栏：Codex 子对话的提供方标识只有一个（复核 M-4）（{r}）')
        evj("document.querySelector('#feed .w-card [data-a=open]').click(); return 1;")
        wait("CWB.S.cur===%s && document.querySelector('#wBanner')" % json.dumps(kid), 15)
        r = evj("return {t: document.querySelector('#wBanner').textContent};")
        check('委派' in r.get('t', '') and '由《' in r.get('t', ''), f'子对话顶部：由《父对话》委派（{r}）')
        ab('screenshot', SHOTS + '/2-child-banner.png')
        evj("document.querySelector('#wBanner .w-bp').click(); return 1;")
        check(wait("CWB.S.cur===%s" % json.dumps(psid), 10), '点横条回父对话')
        # 手动复核：结束行按钮、卡片在结束行后面
        rsid, rt = review
        evj("CWB.openSession(%s); return 1;" % json.dumps(rsid))
        wait("CWB.S.cur===%s && document.querySelector('#feed .w-card.w-manual')" % json.dumps(rsid), 20)
        r = evj("""return {rev: [...document.querySelectorAll('#feed .w-rbtn')].map(b=>b.textContent), card: (document.querySelector('#feed .w-card.w-manual .w-who')||{}).textContent,
          done: (document.querySelector('#feed .w-card.w-manual .w-done')||{}).textContent};""")
        check(any(x == '让 Codex 复核这轮改动' for x in r.get('rev') or []), f'改过文件的那一轮结束行：让 Codex 复核这轮改动（{r.get("rev")}）')
        check((r.get('card') or '').startswith('交给 Codex') and r.get('done') == '结果已交回', f'复核卡挂在那一轮后面（{r}）')
        ab('screenshot', SHOTS + '/3-review.png')
        # 右栏“任务”的委派分组
        evj("document.querySelector('#taskBtn').click(); return 1;")
        ok = wait("document.querySelector('#rbody .w-dlg .w-ag')", 10)
        check(ok, '右栏“任务”：委派分组')
        ab('screenshot', SHOTS + '/4-tasks-pane.png')
        # 设置 →“协作”
        evj("CWB.openSettings('collab'); return 1;")
        wait("document.querySelector('.w-set .sj-sw[data-k=on]')", 10)
        r = evj("""return {nav: [...document.querySelectorAll('#sjSet .sj-nav button')].map(b=>b.textContent), on: document.querySelector('.w-set .sj-sw[data-k=on]').getAttribute('aria-checked'),
          provs: [...document.querySelectorAll('.w-set .sj-sw[data-k^=pv_]')].length, perm: [...document.querySelectorAll('.w-set .sj-seg[data-k=perm] button')].map(b=>b.textContent),
          nums: [...document.querySelectorAll('.w-set input[data-n]')].map(i=>i.value)};""")
        check('协作' in (r.get('nav') or []) and r.get('provs') == 3 and r.get('perm') == ['和父对话一样', '自动改文件', '每步确认'] and r.get('nums') == ['3', '5', '30'],
              f'设置“协作”：开关、三家、权限三档、上限 3/5、30 分钟（{r}）')
        ab('screenshot', SHOTS + '/5-settings.png')
        evj("CWB.closeSettings(); return 1;")
        # 授权卡：Claude 想让 Codex 做
        prefs(on=True)
        sid = new_parent('manual mode')
        evj("CWB.openSession(%s); return 1;" % json.dumps(sid))
        wait("CWB.S.cur===%s" % json.dumps(sid), 10)
        api('send', {'sid': sid, 'text': fake([{'name': 'mcp__cwb__delegate_task', 'input': {'task': '帮我看看 README 有没有错别字', 'title': '查错别字',
                                                                                              'target': {'providerInstanceId': 'codex'}}}])})
        ok = wait("!document.querySelector('#modal').hidden && /想让/.test(document.querySelector('#card h3').textContent)", 30)
        r = evj("return {h: document.querySelector('#card h3').textContent, m: (document.querySelector('#card .w-pcm')||{}).textContent, x: (document.querySelector('#card .w-pcx')||{}).textContent};")
        check(ok and r.get('h') == 'Claude 想让 Codex 做：' and '权限 每步确认' in (r.get('m') or '') and '错别字' in (r.get('x') or ''),
              f'授权卡：Claude 想让 Codex 做：…（模型、权限档、任务）（{r}）')
        ab('screenshot', SHOTS + '/6-perm.png')
        evj("document.querySelector('#card [data-d=deny]').click(); return 1;")
        wait_until(lambda: E.runner_of(sid) and not E.runner_of(sid).get('busy'), 20)
    finally:
        ab('close')


def main():
    want = sys.argv[1:] or ['off', 'prewarm', 'c2c', 'cx2', 'codex', 'cascade', 'timeout', 'lost', 'busy', 'review', 'uacheck', 'ui']
    setup()
    ev = E.Events()
    parent = review = None
    try:
        for name in want:
            try:
                if name == 'off':
                    sc_off(ev)
                elif name == 'prewarm':
                    sc_prewarm(ev)
                elif name == 'c2c':
                    parent = sc_c2c(ev)
                elif name == 'cx2':
                    sc_cx2(ev)
                elif name == 'codex':
                    sc_codex(ev)
                elif name == 'cascade':
                    sc_cascade(ev)
                elif name == 'timeout':
                    sc_timeout(ev)
                elif name == 'lost':
                    sc_lost(ev)
                elif name == 'busy':
                    sc_busy(ev)
                elif name == 'review':
                    review = sc_review(ev)
                elif name == 'uacheck':
                    sc_uacheck(ev)
                elif name == 'ui' and parent and review:
                    sc_ui(ev, parent, review)
            except Exception as e:
                import traceback
                traceback.print_exc()
                check(False, f'场景 {name} 异常：{e!r}')
        if len(want) > 1:
            say('== 全程请求头（父对话、子对话、叫醒的那一轮、复核、自检之外的全部 Claude 请求）')
            E.ua_check(0)
    finally:
        ev.stop = True
        teardown()
    n = sum(1 for _ in E.FAIL)
    print('\n全部通过' if not E.FAIL else f'\n失败 {n} 项：' + '；'.join(E.FAIL))
    sys.exit(1 if E.FAIL else 0)


if __name__ == '__main__':
    main()
