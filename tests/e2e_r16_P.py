"""第十六轮 P 端到端：Codex（真 codex app-server + 假 Responses 后端 tests/fakecodex.py，端口 18832，临时 CODEX_HOME）、
Antigravity（假 ACP agent tests/fakeacp.py）、Claude 回归（真 claude + 假网关 tests/fakegw.py，端口 18831，看请求头）。
测试服务 47951（CWB_TITLE_DELAY=99999），数据目录、项目文件夹、CODEX_HOME 都在 /tmp/cwb-p-e2e；只结束自己起的进程（按 PID 和命令行核对）。
运行：.venv/bin/python -I tests/e2e_r16_P.py [场景…]   场景：codex acp restart rwi claude ui（默认全部）
"""
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
X = '/tmp/cwb-p-e2e'
os.environ.update(CWB_E2E_PORT='47951', CWB_E2E_GW='18831', CWB_E2E_PROJ=X + '/proj', CWB_E2E_STATE=X + '/state.json',
                  CWB_E2E_GWLOG=X + '/gw/requests.jsonl')
import e2e_lib as E  # noqa: E402

ROOT, PY = E.ROOT, os.path.join(E.ROOT, '.venv/bin/python')
PROJ, DATA, CHOME, ACPST, FCLOG, SHOTS = X + '/proj', X + '/data', X + '/chome', X + '/acp', X + '/fc', X + '/shots'
FC_PORT = 18832
PIDS = {}
CODEX_ARGS = ['-c', 'model_provider="fake"', '-c', 'model_providers.fake.name="fake"',
              '-c', f'model_providers.fake.base_url="http://127.0.0.1:{FC_PORT}/v1"', '-c', 'model_providers.fake.wire_api="responses"']
SRV_ENV = dict(CWB_TITLE_DELAY='99999', CWB_DATA_DIR=DATA, CWB_CODEX_HOME=CHOME, CWB_CODEX_ARGS=json.dumps(CODEX_ARGS),
               CWB_AGY_CMD=json.dumps([PY, '-I', os.path.join(HERE, 'fakeacp.py'), ACPST]), CWB_PROV_IDLE='8', CWB_AGY_NETCHECK='off')
api, check, say, wait_until = E.api, E.check, E.say, E.wait_until


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
    for d in (X,):
        if os.path.isdir(d):
            shutil.rmtree(d)
    for d in (PROJ, PROJ + '2', DATA, CHOME, ACPST, FCLOG, SHOTS, X + '/gw'):
        os.makedirs(d, exist_ok=True)
    with open(CHOME + '/config.toml', 'w') as f:  # 临时 CODEX_HOME：只放非密钥配置（真 config 里没有要拷的项：提供方指到假后端）
        # 关掉插件同步：不然 codex 会在后台 git fetch 插件市场（连外网，进程还会留下来）
        f.write('# 第十六轮 P 测试用\nmodel = "fake-model"\n[features]\nplugins = false\nremote_plugin = false\n')
    spawn('fakecodex', [PY, '-I', os.path.join(HERE, 'fakecodex.py'), str(FC_PORT), FCLOG], {'FAKECODEX_BODIES': '1'})
    spawn('fakegw', [PY, '-I', os.path.join(HERE, 'fakegw.py'), str(E.GW), X + '/gw'])
    time.sleep(0.6)
    PIDS['server'] = E.start_server(**SRV_ENV)


def teardown():
    E.stop_server()
    pp = E.kill_ptyd(signal.SIGTERM)  # 测试用的 ptyd（47951）和它挂着的 claude 也收掉，和其他 e2e 一样；只认锁文件里的 PID＋命令行端口
    if pp:
        wait_until(lambda: not E.alive(pp), 10)
    kill('fakecodex', 'fakecodex.py')
    kill('fakegw', 'fakegw.py')
    time.sleep(0.5)
    check(not proto_left(), f'测试结束后没有残留的协议进程（{proto_left()}）')


def proto_left():
    """本测试起的协议进程：假 ACP agent（命令行带状态目录）、codex app-server（命令行带假后端地址）。"""
    return [l.strip()[:120] for l in os.popen('ps -axo pid=,command=').read().splitlines()
            if (ACPST in l and 'fakeacp.py' in l) or ('app-server' in l and f'127.0.0.1:{FC_PORT}' in l) or (CHOME in l)
            or ('/private' + CHOME in l)]


def fc_rows():
    out = []
    try:
        for l in open(FCLOG + '/requests.jsonl', encoding='utf-8'):
            out.append(json.loads(l))
    except Exception:
        pass
    return out


def conv_rows(sid):
    out = []
    try:
        for l in open(f'{DATA}/conv/{sid}.jsonl', encoding='utf-8'):
            out.append(json.loads(l))
    except Exception:
        pass
    return out


def tiny_png():
    import struct
    import zlib

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(b'\x00' + b'\xff\x00\x00' * 2 + b'\x00' + b'\x00\x00\xff' * 2)) + chunk(b'IEND', b'')


def items(sid):
    return api('open', {'sid': sid}).get('items', [])


def runner(sid):
    return api('open', {'sid': sid}).get('runner') or {}


def idle(sid, t=30):
    return wait_until(lambda: not runner(sid).get('busy'), t)


def perms(sid):
    return api('open', {'sid': sid}).get('perms', [])


def new(prov, opts=None):
    d = api('prov-new', {'prov': prov, 'cwd': PROJ, 'opts': opts or {}})
    assert d.get('sid'), d
    return d['sid']


# ---------------------------------------------------------------- Codex
def sc_codex(ev):
    say('== Codex')
    st = {p['id']: p for p in api('prov-status').get('provs', [])}
    check(st.get('codex', {}).get('installed') and st['codex'].get('ok'), f'Codex 状态：{st.get("codex")}')
    md = api('prov-models', {'prov': 'codex'})
    check(len(md.get('models', [])) > 0 and len(md.get('modes', [])) == 5, f'Codex 模型 {len(md.get("models", []))} 个、权限 {len(md.get("modes", []))} 档')
    sid = new('codex')
    # 文本流 + 思考
    t0 = time.time()
    api('send', {'sid': sid, 'text': 'THINK SLOW:2 你好'})
    live = ev.wait(lambda e: e.get('type') == 'runner' and e.get('sid') == sid and (e['state'].get('live') or '').startswith('第 '), 15, t0)
    check(bool(live), '流式正文：运行状态里有实时预览（live）')
    idle(sid)
    its = items(sid)
    ks = [i['k'] for i in its]
    check(ks[:3] == ['user', 'thought', 'text'] and '先想一想' in its[1].get('text', ''), f'条目：用户、思考摘要、正文（{ks}）')
    check(its[0].get('stat', {}).get('out') == 34, f'这一轮的统计：{its[0].get("stat")}')
    te = [r for r in conv_rows(sid) if r.get('t') == 'turn_end']
    ok = te and te[-1].get('status') == 'completed' and te[-1].get('usage', {}).get('output') == 34 and te[-1]['usage'].get('cachedInput') == 800 \
        and te[-1]['usage'].get('reasoning') == 12 and isinstance(te[-1].get('firstTokenAt'), int) and te[-1]['firstTokenAt'] >= te[-1]['startedAt'] \
        and te[-1]['endedAt'] >= te[-1]['firstTokenAt'] and te[-1].get('provider') == 'codex' and te[-1].get('usageSrc') == 'protocol'
    check(bool(ok), f'turn_end 记录：{te[-1] if te else None}')
    check('error' not in te[-1] and 'cost' not in te[-1], '拿不到的字段不写（error、cost）')
    q = json.load(open(DATA + '/quota-codex.json')) if os.path.exists(DATA + '/quota-codex.json') else {}
    check(q.get('v') == 1 and q.get('provider') == 'codex' and (q.get('unavailable') or {}).get('reason') == 'notLoggedIn',
          f'额度快照（假后端没登录 ChatGPT）：{q.get("unavailable")}')
    # 授权：每步都问 → 命令要确认
    r = api('control', {'sid': sid, 'mode': 'untrusted'})
    check(r.get('opts', {}).get('mode') == 'untrusted', '切到“每步都问”')
    api('send', {'sid': sid, 'text': 'CMD[[touch e2e_a.txt && echo hi]]END'})
    p = wait_until(lambda: perms(sid), 20)
    check(bool(p) and p[0]['tool'] == 'Bash' and 'touch e2e_a.txt' in p[0]['input']['command'] and p[0].get('prov') == 'codex', f'授权卡：{p and p[0].get("input")}')
    check(bool(p) and runner(sid).get('attention'), '等确认时左栏状态 attention')
    api('perm', {'rid': p[0]['rid'], 'decision': 'allow'})
    idle(sid)
    tool = [i for i in items(sid) if i['k'] == 'tool'][-1]
    check(tool['name'] == 'Bash' and tool.get('done') and tool.get('out') == 'hi' and os.path.exists(PROJ + '/e2e_a.txt'), f'允许后命令执行：{tool.get("out")}')
    # 拒绝（这条命令只能 cancel：这一轮随之结束）
    api('send', {'sid': sid, 'text': 'CMD[[touch e2e_b.txt]]END'})
    p = wait_until(lambda: perms(sid), 20)
    api('perm', {'rid': p[0]['rid'], 'decision': 'deny'}) if p else None
    idle(sid)
    tool = [i for i in items(sid) if i['k'] == 'tool'][-1]
    check(not os.path.exists(PROJ + '/e2e_b.txt') and tool.get('error') and tool.get('noresult') == '已拒绝', f'拒绝后命令没执行（{tool.get("noresult")}）')
    # 改文件的授权：apply_patch
    api('send', {'sid': sid, 'text': "CMD[[apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: e2e_new.txt\n+hello\n*** End Patch\nEOF]]END"})
    p = wait_until(lambda: perms(sid), 20)
    view = (p[0].get('pvView') if p else {}) or {}
    check(bool(p) and p[0]['tool'] == 'Edit' and (view.get('files') or [{}])[0].get('path', '').endswith('e2e_new.txt'), f'改文件授权卡带文件和内容：{view.get("files")}')
    api('perm', {'rid': p[0]['rid'], 'decision': 'allow'}) if p else None
    idle(sid)
    wr = [i for i in items(sid) if i['k'] == 'tool' and i['name'] == 'Write']
    check(wr and wr[-1].get('done') and os.path.exists(PROJ + '/e2e_new.txt'), '改文件条目（Write）完成，文件已建')
    # 中断
    t0 = time.time()
    api('send', {'sid': sid, 'text': 'SLOW:20 慢慢说'})
    wait_until(lambda: (runner(sid).get('live') or '').startswith('第 '), 15)
    check(api('prov-busy').get('busy') is True, 'prov-busy：回复中算忙')
    api('interrupt', {'sid': sid})
    idle(sid, 15)
    its = items(sid)
    te = [r for r in conv_rows(sid) if r.get('t') == 'turn_end'][-1]
    check(time.time() - t0 < 15 and its[-1].get('k') == 'note' and its[-1].get('text') == '已中断' and te.get('status') == 'interrupted'
          and te.get('why') == 'user', f'中断：{its[-1]}，{te.get("status")}/{te.get("why")}')
    check(any(i['k'] == 'text' and i['text'].startswith('第 0 段') for i in its), '中断前已经出来的正文写进了时间线')
    check(api('prov-busy').get('busy') is False, 'prov-busy：中断后空闲')
    # 排队 + 立即发送（turn/steer 插进当前一轮）
    api('control', {'sid': sid, 'mode': 'full'})
    api('send', {'sid': sid, 'text': 'SLOW:6 第一条'})
    wait_until(lambda: runner(sid).get('busy'), 10)
    r = api('send', {'sid': sid, 'text': '插一句'})
    check(r.get('queued') and runner(sid).get('queued') == 1, '回复中发的消息排队')
    api('send-now', {'sid': sid})
    mid = wait_until(lambda: [i for i in items(sid) if i.get('mid')], 10)
    check(bool(mid) and mid[-1]['text'] == '插一句', '立即发送：插进当前回复（mid）')
    idle(sid, 20)
    check(any('插一句' in (x.get('user') or '') for x in fc_rows()[-3:]), '插入的消息发到了后端')
    # 图片
    png = X + '/dot.png'
    with open(png, 'wb') as f:
        f.write(tiny_png())
    api('send', {'sid': sid, 'text': '看图', 'images': [png]})
    idle(sid)
    last = sorted(glob.glob(FCLOG + '/bodies/*.json'))[-1]
    check('dot.png' in open(last, encoding='utf-8').read(), '图片作为 localImage 发给了 Codex（请求里带这张图）')
    u = [i for i in items(sid) if i['k'] == 'user'][-1]
    check(u.get('imgs') == 1 and u.get('pimgs') == [png], '用户消息记下图片')
    # 续接：进程空闲回收后再发，用 thread/resume 接上（后端收到的上下文里有之前的消息）
    pid0 = (api('prov-busy').get('procs') or {}).get('codex', {}) or {}
    gone = wait_until(lambda: not ((api('prov-busy').get('procs') or {}).get('codex') or {}).get('alive'), 30)
    check(bool(gone), f'全部空闲 {SRV_ENV["CWB_PROV_IDLE"]} 秒后回收 Codex 进程（{pid0.get("pid")}）')
    check(runner(sid).get('alive') is False, '回收后对话显示未连接')
    n0 = len(fc_rows())
    api('send', {'sid': sid, 'text': '续接后的话'})
    idle(sid)
    rows = fc_rows()[n0:]
    check(rows and rows[0]['user'] == '续接后的话' and items(sid)[-1].get('text', '').startswith('收到：续接后的话'), '续接后正常回复')
    body = sorted(glob.glob(FCLOG + '/bodies/*.json'))
    txt = open(body[-1], encoding='utf-8').read() if body else ''
    check('THINK SLOW:2' in txt and '续接后的话' in txt, '续接后发给后端的上下文里有之前的消息（thread/resume 接上了原对话）')
    nat = api('open', {'sid': sid}).get('native')
    check(bool(glob.glob(f'{CHOME}/sessions/**/*{nat}*.jsonl', recursive=True)), f'Codex 原生记录在临时 CODEX_HOME 里（{nat[:8]}…）')
    return sid


# ---------------------------------------------------------------- Antigravity（假 ACP agent）
def sc_acp(ev):
    say('== Antigravity（假 agent）')
    md = api('prov-models', {'prov': 'agy'})
    check([m['id'] for m in md.get('models', [])] == ['fake-a', 'fake-b'] and [e['id'] for e in md.get('efforts', [])] == ['low', 'high']
          and [m['id'] for m in md.get('modes', [])] == ['default', 'auto'], f'模型、思考强度、模式来自 configOptions：{md.get("models")}')
    check(md['modes'][0]['label'] == '默认' and '询问' in md['modes'][0]['desc'], '模式配中文说明')
    sid = new('agy', {'model': 'fake-b'})
    api('send', {'sid': sid, 'text': 'THINK PLAN 你好'})
    idle(sid)
    its = items(sid)
    ks = [i['k'] for i in its]
    check(ks == ['user', 'thought', 'tool', 'text'] and its[2].get('name') == 'TodoWrite' and its[3]['text'] == '收到：THINK PLAN 你好', f'条目顺序：{ks}')
    calls = [json.loads(l) for l in open(ACPST + '/calls.jsonl')]
    check(any(c.get('method') == 'session/set_config_option' and c['params'].get('value') == 'fake-b' for c in calls), '选的模型用 set_config_option 交给对方')
    d = api('open', {'sid': sid})
    check(d.get('title', '').startswith('假标题') and d['runner'].get('pvctx', {}).get('used') == 1500, f'标题（session_info_update）和上下文（usage_update）：{d.get("title")}')
    check([c['name'] for c in d.get('pvCmds', [])] == ['compact'], f'对方的 / 命令：{d.get("pvCmds")}')
    te = [r for r in conv_rows(sid) if r.get('t') == 'turn_end'][-1]
    check(te.get('status') == 'completed' and te.get('model') == 'fake-b' and te.get('effort') == 'high' and 'usage' not in te and te.get('usageSrc') is None
          and te.get('ctx', {}).get('used') == 1500 and te.get('stopReason') == 'end_turn', f'turn_end：{te}')
    # 授权：允许
    api('send', {'sid': sid, 'text': 'PERM 跑'})
    p = wait_until(lambda: perms(sid), 15)
    check(bool(p) and p[0]['tool'] == 'Bash' and p[0]['input']['command'] == 'touch x' and p[0].get('pvAlways'), f'授权卡：{p and p[0]["input"]}')
    api('perm', {'rid': p[0]['rid'], 'decision': 'allow'}) if p else None
    idle(sid)
    its = items(sid)
    check(its[-2].get('name') == 'Bash' and its[-2].get('done') and its[-2].get('out') == 'ok' and its[-1].get('text') == '命令完成', '允许后工具完成')
    # 拒绝并说明：拒绝后，说明作为下一条发出
    api('send', {'sid': sid, 'text': 'PERM 再跑'})
    p = wait_until(lambda: perms(sid), 15)
    api('perm', {'rid': p[0]['rid'], 'decision': 'deny', 'allow': False, 'message': '别跑了'}) if p else None
    nxt = wait_until(lambda: [i for i in items(sid) if i['k'] == 'user' and '别跑了' in i.get('text', '')], 15)
    idle(sid)
    outs = [json.loads(l).get('perm_outcome') for l in open(ACPST + '/calls.jsonl') if 'perm_outcome' in l]
    check(outs[-1] == {'outcome': 'selected', 'optionId': 'o3'} and bool(nxt), f'拒绝选 reject_once，说明随后发出：{outs[-1]}')
    # 取消：回复中
    t0 = time.time()
    api('send', {'sid': sid, 'text': 'SLOW 一直说'})
    wait_until(lambda: (runner(sid).get('live') or '').startswith('第'), 10)
    api('interrupt', {'sid': sid})
    idle(sid, 15)
    te = [r for r in conv_rows(sid) if r.get('t') == 'turn_end'][-1]
    check(time.time() - t0 < 12 and te.get('status') == 'interrupted' and te.get('stopReason') == 'cancelled' and items(sid)[-1].get('text') == '已中断',
          f'取消：stopReason={te.get("stopReason")}')
    # 取消：等确认时（按协议，挂着的授权要回 cancelled）
    api('send', {'sid': sid, 'text': 'PERM 取消我'})
    wait_until(lambda: perms(sid), 15)
    api('interrupt', {'sid': sid})
    idle(sid, 15)
    outs = [json.loads(l).get('perm_outcome') for l in open(ACPST + '/calls.jsonl') if 'perm_outcome' in l]
    check(outs[-1] == {'outcome': 'cancelled'} and not perms(sid), f'等确认时取消，授权回 cancelled：{outs[-1]}')
    # 改文件的工具调用（diff）
    api('send', {'sid': sid, 'text': 'EDIT 改一下'})
    idle(sid)
    ed = [i for i in items(sid) if i.get('name') == 'Edit']
    check(ed and ed[-1].get('diff', {}).get('new') == 'one\n2\nthree\n' and ed[-1].get('ln') == [2, 1] and ed[-1].get('path') == '/tmp/a.txt',
          f'改文件条目带差异和增删行数：{ed and ed[-1].get("ln")}')
    # 没登录
    api('send', {'sid': sid, 'text': 'AUTH'})
    idle(sid)
    e = items(sid)[-1]
    check(e.get('k') == 'error' and '登录' in e.get('text', ''), f'没登录时提示怎么处理：{e.get("text")}')
    # 图片
    png = X + '/dot.png'
    api('send', {'sid': sid, 'text': '看图', 'images': [png]})
    idle(sid)
    check('（1 张图）' in items(sid)[-1].get('text', ''), '图片作为 image 块发给了 ACP')
    # 换模型（空闲时立即 set_config_option）
    api('control', {'sid': sid, 'model': 'fake-a', 'effort': 'low'})
    calls = [json.loads(l) for l in open(ACPST + '/calls.jsonl')]
    sets = [(c['params'].get('configId'), c['params'].get('value')) for c in calls if c.get('method') == 'session/set_config_option']
    check(('model', 'fake-a') in sets and ('thought_level', 'low') in sets, f'空闲时换模型、思考强度立即生效：{sets[-2:]}')
    return sid


# ---------------------------------------------------------------- 复核修复（11-22 晚）
def last_end(sid):
    te = [r for r in conv_rows(sid) if r.get('t') == 'turn_end']
    return te[-1] if te else {}


def sc_fixes(ev):
    say('== 复核修复')
    # 1. Codex：进程回收后新对话的第一条，在起进程、thread/start 期间点停止 → 不发出、按“已中断”结束
    wait_until(lambda: not ((api('prov-busy').get('procs') or {}).get('codex') or {}).get('alive'), 30)
    sid = new('codex')
    n0 = len(fc_rows())
    api('send', {'sid': sid, 'text': '连接时停止 STOPC'})
    api('interrupt', {'sid': sid})
    idle(sid, 20)
    time.sleep(1.5)
    te = last_end(sid)
    check(te.get('status') == 'interrupted' and te.get('why') == 'user' and items(sid)[-1].get('text') == '已中断'
          and not any('STOPC' in (x.get('user') or '') for x in fc_rows()[n0:]),
          f'Codex：正在连接时点停止，这一轮没有发给后端、记“已中断”（{te.get("status")}/{te.get("why")}）')
    api('send', {'sid': sid, 'text': '停止后再发'})
    idle(sid, 20)
    check(items(sid)[-1].get('text', '').startswith('收到：停止后再发'), 'Codex：停止过之后下一条照常回复')
    # 1. Antigravity：session/new 很慢（假 agent 等 3 秒）时点停止
    with open(ACPST + '/slow_open', 'w') as f:
        f.write('3')
    try:
        aid = new('agy')
        api('send', {'sid': aid, 'text': '连接时停止 STOPA'})
        time.sleep(0.4)
        api('interrupt', {'sid': aid})
        idle(aid, 20)
    finally:
        os.remove(ACPST + '/slow_open')
    calls = [json.loads(l) for l in open(ACPST + '/calls.jsonl')]
    te = last_end(aid)
    check(te.get('status') == 'interrupted' and te.get('why') == 'user'
          and not any(c.get('method') == 'session/prompt' and 'STOPA' in json.dumps(c, ensure_ascii=False) for c in calls),
          f'Antigravity：session/new 期间点停止，没有发 session/prompt、记“已中断”（{te.get("status")}）')
    api('send', {'sid': aid, 'text': '停止后再发'})
    idle(aid, 20)
    check(items(aid)[-1].get('text', '').startswith('收到：停止后再发'), 'Antigravity：停止过之后下一条照常回复')
    api('delete', {'sid': aid})
    # 2. 两个 Codex 对话同时跑命令：实时输出各归各的
    s1, s2 = new('codex', {'mode': 'full'}), new('codex', {'mode': 'full'})
    for s_, tag in ((s1, 'AAA'), (s2, 'BBB')):
        api('send', {'sid': s_, 'text': 'CMD[[for i in 1 2 3 4 5 6 7 8 9 10; do echo %s$i; sleep 0.4; done]]END' % tag})
    seen, bad = [], []

    def both():
        t1 = [i for i in items(s1) if i['k'] == 'tool']
        t2 = [i for i in items(s2) if i['k'] == 'tool']
        for t, mine, other in ((t1, 'AAA', 'BBB'), (t2, 'BBB', 'AAA')):
            if t and other in (t[-1].get('out') or ''):
                bad.append((mine, t[-1].get('out')))
        if t1 and t2 and t1[-1].get('out') and t2[-1].get('out') and not t1[-1].get('done') and not t2[-1].get('done'):
            seen.append((t1[-1]['id'], t2[-1]['id']))
        return len(seen) >= 3 or (t1 and t2 and t1[-1].get('done') and t2[-1].get('done'))
    wait_until(both, 30)
    idle(s1, 20)
    idle(s2, 20)
    f1 = [i for i in items(s1) if i['k'] == 'tool'][-1].get('out') or ''
    f2 = [i for i in items(s2) if i['k'] == 'tool'][-1].get('out') or ''
    check(seen and seen[0][0] == seen[0][1] and not bad, f'两个 Codex 对话同时跑命令（同号条目 {seen[:1]}），实时输出不串台（{bad[:1]}）')
    check('AAA10' in f1 and 'BBB' not in f1 and 'BBB10' in f2 and 'AAA' not in f2, '两个命令的最终输出各归各的')
    for s_ in (s1, s2, sid):
        api('delete', {'sid': s_})


# ---------------------------------------------------------------- server 重启：进行中的回复记“上次回复被中断”；续接（含 session/load）
def sc_restart(ev, csid, asid):
    say('== server 重启')
    api('send', {'sid': csid, 'text': 'SLOW:30 很长'})
    api('send', {'sid': asid, 'text': 'SLOW 很长'})
    wait_until(lambda: runner(csid).get('busy') and runner(asid).get('busy'), 15)
    time.sleep(1)
    E.stop_server()
    time.sleep(1)
    gone = wait_until(lambda: not proto_left(), 10)
    check(bool(gone), f'server 退出后协议进程跟着退出（读到 EOF）：{proto_left()}')
    PIDS['server'] = E.start_server(**dict(SRV_ENV, CWB_AGY_ARGS='["--noresume"]'))  # 这次让假 agent 不声明 resume，走 session/load
    for sid, lab in ((csid, 'Codex'), (asid, 'Antigravity')):
        d = api('open', {'sid': sid})
        te = [r for r in conv_rows(sid) if r.get('t') == 'turn_end'][-1]
        check(bool(d.get('interrupted')) and not d['runner'].get('busy') and te.get('status') == 'interrupted' and te.get('why') == 'shutdown'
              and te.get('endedAtEstimated'), f'{lab}：重启后标“上次回复被中断”（{d.get("interrupted")}）')
        s = [x for p in api('projects').get('projects', []) for x in p['sessions'] if x['sid'] == sid]
        check(s and s[0].get('resumable') and s[0].get('prov'), f'{lab}：左栏“可继续”，带提供方')
    n0 = len(items(asid))
    api('send', {'sid': asid, 'text': '继续'})
    idle(asid, 20)
    its = items(asid)
    calls = [json.loads(l) for l in open(ACPST + '/calls.jsonl')]
    check(any(c.get('method') == 'session/load' for c in calls[-20:]) and len(its) == n0 + 2 and its[-1]['text'].startswith('收到：继续'),
          f'Antigravity 用 session/load 续接，对方回放的历史没有重复写进时间线（{n0}→{len(its)}）')
    check(not api('open', {'sid': asid}).get('interrupted'), '发出新消息后中断标记清掉')
    api('send', {'sid': csid, 'text': '继续'})
    idle(csid, 20)
    check(items(csid)[-1].get('text', '').startswith('收到：继续'), 'Codex 重启后续接')


# ---------------------------------------------------------------- restart_when_idle --server-now 先等 Codex 空闲
def sc_rwi(ev, csid):
    say('== restart_when_idle --server-now')
    logp = os.path.join(ROOT, '.run', 'restart_when_idle.log')
    n0 = len(open(logp).read()) if os.path.exists(logp) else 0
    api('send', {'sid': csid, 'text': 'SLOW:8 慢'})
    wait_until(lambda: runner(csid).get('busy'), 10)
    pid = PIDS['server']
    t0 = time.time()
    subprocess.run([PY, '-I', os.path.join(ROOT, 'restart_when_idle.py'), '--server-now'], cwd=ROOT, env=dict(os.environ, CWB_PORT=str(E.PORT)))
    time.sleep(4)
    check(E.alive(pid) and runner(csid).get('busy'), '回复中：--server-now 先不重启')
    gone = wait_until(lambda: not E.alive(pid), 40)
    te = [r for r in conv_rows(csid) if r.get('t') == 'turn_end'][-1]
    check(bool(gone) and te.get('status') == 'completed', f'回复结束后才结束旧服务（{time.time() - t0:.0f} 秒），这一轮正常完成')
    tail = open(logp).read()[n0:]
    check('等 Codex / Antigravity 对话空闲' in tail and '立即结束旧服务' in tail, '日志：先等空闲，再结束旧服务')
    PIDS['server'] = E.start_server(**SRV_ENV)


# ---------------------------------------------------------------- Claude 回归：请求头
def sc_claude(ev):
    say('== Claude 回归')
    n0 = len(E.gw_rows())
    sid = E.new_session()
    api('send', {'sid': sid, 'text': '你好 PRUN'})
    wait_until(lambda: any('PRUN' in (x.get('last') or '') for x in E.gw_rows()[n0:]), 60)
    time.sleep(1)
    E.ua_check(n0)
    check(not api('open', {'sid': sid}).get('prov'), 'Claude 对话不带提供方字段')
    st = E.runner_of(sid)
    check(bool(st) and 'prov' not in st, 'Claude 对话仍由伪终端里的 claude 运行（tasks 里有它）')
    return sid


# ---------------------------------------------------------------- 界面：无头浏览器 DOM 断言 + 截图
def sc_ui(ev, csid, asid, clsid):
    say('== 界面')
    import e2e_r15_g as G
    G.SESSION = 'pv-e2e'
    ab, evj, wait = G.ab, G.ev, G.wait
    try:
        ab('set', 'viewport', '1280', '860')
        ab('open', 'http://127.0.0.1:%d/?t=%s' % (E.PORT, E.token()))
        wait('window.CWB && CWB.pv && CWB.home', 20)
        evj("if (CWB.S.cur) document.querySelector('#newHere').click(); return 1;")
        wait("document.body.classList.contains('h-home')", 10)
        r = evj("return {opts: [...document.querySelectorAll('#pvSeg button')].map(b=>b.textContent), on: (document.querySelector('#pvSeg .on')||{}).textContent, vis: getComputedStyle(document.querySelector('#pvSeg')).display};")
        check(r.get('opts') == ['Claude', 'Codex', 'Antigravity'] and r.get('on') == 'Claude' and r.get('vis') != 'none', f'首页：三个提供方，默认 Claude（{r}）')
        check(evj("return document.querySelector('#hProviderIcon').getAttribute('aria-label') === 'Claude' && !!document.querySelector('#hProviderIcon .h-provider-logo path');"), '首页默认显示 Claude 官方星芒图标')
        evj("document.querySelector('#pvSeg [data-p=codex]').click(); return 1;")
        wait("document.body.dataset.pv === 'codex' && document.querySelector('#pvHint').hidden", 10)
        check(evj("return document.querySelector('#pvHint').hidden && document.querySelector('#pvHint').textContent === '';"), 'Codex 正常可用：首页不显示登录说明和版本号')
        r = evj("return {pills: [...document.querySelectorAll('.pv-pill')].map(b=>getComputedStyle(b).display), claude: getComputedStyle(document.querySelector('#modelPill')).display, hint: document.querySelector('#pvHint').textContent};")
        check(r.get('claude') == 'none' and 'none' not in (r.get('pills') or ['none'])[:1], f'首页选 Codex：换成 Codex 的药丸（{r}）')
        check(evj("return document.querySelector('#hProviderIcon').getAttribute('aria-label') === 'Codex' && !!document.querySelector('#hProviderIcon .pv-logo-codex') && !document.querySelector('#hProviderIcon .h-provider-logo path');"), '选 Codex：大图标换成 Codex，不残留 Claude')
        ab('screenshot', SHOTS + '/1-home-codex.png')
        evj("document.querySelector('#pvSeg [data-p=agy]').click(); return 1;")
        check(wait("document.querySelector('#hProviderIcon').getAttribute('aria-label') === 'Antigravity' && (() => {const i = document.querySelector('#hProviderIcon img'); return i && i.complete && i.naturalWidth > 0;})()", 5), '选 Antigravity：大图标切换且 SVG 加载成功')
        check(evj("return document.querySelector('#pvHint').hidden && document.querySelector('#pvHint').textContent === '';"), 'Antigravity 正常可用：首页不显示登录说明和版本号')
        ab('screenshot', SHOTS + '/1-home-antigravity.png')
        evj("document.querySelector('#pvSeg [data-p=claude]').click(); return 1;")
        wait("!document.body.classList.contains('pv-on')", 5)
        check(evj("return getComputedStyle(document.querySelector('#modelPill')).display;") != 'none', '选回 Claude：Claude 的药丸回来')
        check(evj("return document.querySelector('#hProviderIcon').getAttribute('aria-label') === 'Claude' && !!document.querySelector('#hProviderIcon .h-provider-logo path') && !document.querySelector('#hProviderIcon .pv-logo');"), '选回 Claude：恢复官方星芒图标')
        # Codex 对话
        evj("CWB.openSession(%s); return 1;" % json.dumps(csid))
        wait("CWB.S.cur===%s && document.body.dataset.pv==='codex'" % json.dumps(csid), 15)
        time.sleep(1)
        r = evj("""return {badge: document.querySelector('#pvBadge').textContent, badgeVis: !document.querySelector('#pvBadge').hidden,
          ring: (()=>{const k=document.querySelector('#kRing'); return k?getComputedStyle(k).display:'none';})(), btw: (()=>{const b=document.querySelector('#rpane .rtabs [data-tab=btw]'); return b?getComputedStyle(b).display:'none';})(),
          tasks: getComputedStyle(document.querySelector('#rpane .rtabs [data-tab=tasks]')).display, rw: document.querySelectorAll('.k-uact [data-a=rw]').length,
          fk: [...document.querySelectorAll('#feed .fk, #feed .k-end')].filter(x=>x.offsetParent).length,
          tags: [...document.querySelectorAll('#projects .srow .pv-tag')].map(t=>t.textContent), mode: document.querySelector('#pvMode').textContent,
          users: document.querySelectorAll('#feed > .user').length, thumbs: document.querySelectorAll('#feed .user .thumbs img').length};""")
        check(r.get('badge') == 'Codex' and r.get('badgeVis'), f'顶栏标 Codex（{r.get("badge")}）')
        check(r.get('ring') == 'none' and r.get('btw') == 'none' and r.get('tasks') != 'none' and r.get('rw') == 0 and r.get('fk') == 0,
              f'Claude 专属（上下文环、旁问、回退、分叉）藏起来；“任务”页 11-23 起两家也显示（列子代理）：{r}')
        check('Codex' in (r.get('tags') or []) and 'Antigravity' in (r.get('tags') or []), f'左栏带提供方标识：{r.get("tags")}')
        check(r.get('thumbs', 0) >= 1, f'用户消息里的图片显示缩略图（{r.get("thumbs")}）')
        ab('screenshot', SHOTS + '/2-codex-conv.png')
        evj("document.querySelector('#pvModel').click(); return 1;")
        wait("document.querySelector('.pv-pop .h-mi')", 10)
        r = evj("return [...document.querySelectorAll('.pv-pop .h-mi .h-mn')].map(x=>x.textContent).slice(0,3);")
        check(isinstance(r, list) and r and r[0].startswith('默认'), f'模型浮层：{r}')
        ab('screenshot', SHOTS + '/3-codex-models.png')
        evj("document.querySelector('#pvModel').click(); return 1;")
        # / 补全不列 Claude 命令
        evj("const i=document.querySelector('#input'); i.focus(); i.value='/'; i.selectionStart=i.selectionEnd=1; i.dispatchEvent(new Event('input')); return 1;")
        time.sleep(0.8)
        r = evj("return {hidden: document.querySelector('#pop').hidden, n: document.querySelectorAll('#pop .pi').length};")
        check(r.get('hidden') or r.get('n') == 0, f'Codex 对话里“/”不列 Claude 命令：{r}')
        evj("const i=document.querySelector('#input'); i.value=''; i.dispatchEvent(new Event('input')); return 1;")
        # 授权卡：Codex 要确认
        api('control', {'sid': csid, 'mode': 'untrusted'})
        api('send', {'sid': csid, 'text': 'CMD[[touch ui_c.txt]]END'})
        wait("!document.querySelector('#modal').hidden && document.querySelector('#card h3 .pv-tag')", 20)
        r = evj("return {h3: document.querySelector('#card h3').textContent, btns: [...document.querySelectorAll('#card button')].map(b=>b.textContent)};")
        check(r.get('h3', '').startswith('Codex') and '拒绝并说明' in r.get('btns', []) and '允许' in r.get('btns', []), f'授权卡标出 Codex：{r}')
        ab('screenshot', SHOTS + '/4-codex-perm.png')
        evj("document.querySelector('#card [data-d=allow]').click(); return 1;")
        idle(csid)
        api('control', {'sid': csid, 'mode': 'full'})
        # Antigravity 对话
        evj("CWB.openSession(%s); return 1;" % json.dumps(asid))
        wait("CWB.S.cur===%s && document.body.dataset.pv==='agy'" % json.dumps(asid), 15)
        time.sleep(1.2)
        r = evj("return {m: document.querySelector('#pvModel').textContent, e: document.querySelector('#pvEffort').textContent, o: document.querySelector('#pvMode').textContent};")
        check(r.get('m') == 'Fake A' and '低' in r.get('e', '') and r.get('o') == '默认', f'Antigravity 药丸显示会话当前的模型、思考强度、模式：{r}')
        evj("document.querySelector('#pvMode').click(); return 1;")
        wait("document.querySelectorAll('.pv-pop .h-mi').length===2", 8)
        ab('screenshot', SHOTS + '/5-agy-modes.png')
        evj("[...document.querySelectorAll('.pv-pop .h-mi')].find(b=>b.textContent.startsWith('自动')).click(); return 1;")
        check(wait_until(lambda: api('open', {'sid': asid}).get('opts', {}).get('mode') == 'auto', 8), '界面切模式已保存')
        # 删除确认框
        r = evj("""let msg=''; const oc=window.confirm; window.confirm=m=>{msg=m; return false;};
          CWB.undoDelete([CWB.findSession(%s)]); window.confirm=oc; return msg;""" % json.dumps(asid))
        check(isinstance(r, str) and '只删除工作台里的记录' in r and 'Antigravity 自己保存的对话不会删除' in r, f'删除确认框写清楚：{r!r}')
        check(os.path.exists(f'{DATA}/conv/{asid}.jsonl'), '确认框点取消：没有删')
        # 复核修复 7：实时预览按增量推，界面接起来和后台一致
        t0 = time.time()
        api('send', {'sid': asid, 'text': 'SLOW 预览增量'})
        wait("CWB.S.runner && (CWB.S.runner.live||'').length > 40", 15)
        time.sleep(0.5)
        bl = evj("return CWB.S.runner.live || '';")
        sl = runner(asid).get('live') or ''
        adds = ev.since(t0, lambda e: e.get('type') == 'runner' and e.get('sid') == asid and 'liveAdd' in e.get('state', {}))
        check(len(adds) >= 3 and bl and sl.startswith(bl[:len(bl) - 20]), f'实时预览平时只推增量（{len(adds)} 次），界面接起来的和后台一致（{len(bl)}/{len(sl)} 字）')
        # 复核修复 6：进程退出时排队的消息放回输入框、时间线列出来
        evj("const i=document.querySelector('#input'); i.value=''; i.dispatchEvent(new Event('input')); return 1;")
        api('send', {'sid': asid, 'text': '排队的话 QQQ'})
        pi = (api('prov-busy').get('procs') or {}).get('agy') or {}
        cmd = E.cmd_of(pi.get('pid') or 0)
        if pi.get('pid') and 'fakeacp.py' in cmd and ACPST in cmd:
            os.kill(pi['pid'], signal.SIGKILL)  # 只结束本测试的假 agent（按 PID＋命令行核对）
        ok = wait("document.querySelector('#input').value.includes('QQQ')", 15)
        its = items(asid)
        check(bool(ok) and any(i.get('etype') == '没发出去' and 'QQQ' in i.get('text', '') for i in its)
              and any('进程意外退出' in i.get('text', '') for i in its), '进程退出：排队的消息放回输入框，时间线里也列出来')
        evj("const i=document.querySelector('#input'); i.value=''; i.dispatchEvent(new Event('input')); return 1;")
        # 复核修复 8：重启后台服务——Codex 在回复时确认框说明要等；空闲时照旧
        api('send', {'sid': csid, 'text': 'SLOW:15 重启时在回复'})
        wait_until(lambda: runner(csid).get('busy'), 15)
        evj("window.__cf=[]; window.__oc=window.confirm; window.confirm=m=>{window.__cf.push(m); return false;}; CWB.openSettings('about'); return 1;")
        wait("document.querySelector('.sj-btn[data-a=restart]')", 10)
        evj("document.querySelector('.sj-btn[data-a=restart]').click(); return 1;")
        wait("window.__cf.length>=1", 10)
        m1 = evj("return window.__cf[0]||'';")
        check('等 Codex 这一轮结束后重启' in m1, f'重启后台服务：Codex 在回复时，确认框写明要等（{m1[:40]!r}）')
        api('interrupt', {'sid': csid})
        idle(csid, 15)
        evj("document.querySelector('.sj-btn[data-a=restart]').click(); return 1;")
        wait("window.__cf.length>=2", 10)
        m2 = evj("const m=window.__cf[1]||''; window.confirm=window.__oc; CWB.closeSettings(); return m;")
        check(m2.startswith('重启后台服务？') and '等 ' not in m2, f'都空闲时交回设置页原来的确认框（{m2[:20]!r}）')
        # 复核修复 8：首页选了 Codex 时不预热 Claude
        evj("document.querySelector('#newHere').click(); return 1;")
        wait("document.body.classList.contains('h-home') && !CWB.S.cur", 10)
        evj("document.querySelector('#pvSeg [data-p=codex]').click(); return 1;")
        time.sleep(1.2)
        n_cl = claude_procs()
        sk0 = evj("return CWB.pv.state.pwSkip||0;")
        evj("CWB.home.pick(%s); return 1;" % json.dumps(PROJ + '2'))
        time.sleep(1.8)
        sk1 = evj("return CWB.pv.state.pwSkip||0;")
        check(sk1 == sk0 + 1 and claude_procs() == n_cl, f'首页选 Codex 后换文件夹：不预热 Claude（拦下 {sk1 - sk0} 次，claude 进程 {n_cl}→{claude_procs()}）')
        evj("document.querySelector('#pvSeg [data-p=claude]').click(); return 1;")
        evj("CWB.home.pick(%s); return 1;" % json.dumps(PROJ))
        time.sleep(1.8)
        check(evj("return CWB.pv.state.pwSkip||0;") == sk1, '选回 Claude：照常预热')
        # Claude 对话：一切照旧
        evj("CWB.openSession(%s); return 1;" % json.dumps(clsid))
        wait("CWB.S.cur===%s" % json.dumps(clsid), 15)
        time.sleep(1)
        r = evj("return {pv: document.body.classList.contains('pv-on'), mp: getComputedStyle(document.querySelector('#modelPill')).display, pvp: getComputedStyle(document.querySelector('#pvModel')).display, badge: document.querySelector('#pvBadge').hidden};")
        check(not r.get('pv') and r.get('mp') != 'none' and r.get('pvp') == 'none' and r.get('badge'), f'Claude 对话：界面照旧（{r}）')
        ab('screenshot', SHOTS + '/6-claude-conv.png')
        rc, out = ab('errors')
        bad = [l for l in out.splitlines() if l.strip() and 'No page errors' not in l and 'no errors' not in l.lower()]
        check(not bad, f'页面没有脚本错误：{bad[:3]}')
    finally:
        ab('close')


def claude_procs():
    """测试 ptyd 下面的 claude 进程数（预热会多起一个）。"""
    pp = E.ptyd_pid()
    return sum(1 for l in os.popen('ps -axo ppid=,command=').read().splitlines()
               if l.strip().split(None, 1)[0] == str(pp) and 'claude' in l) if pp else 0


def sc_delete(csid, asid):
    say('== 删除')
    nat = api('open', {'sid': csid}).get('native')
    r = api('delete', {'sid': csid})
    check(r.get('ok') and not os.path.exists(f'{DATA}/conv/{csid}.jsonl'), '删除：工作台记录移到废纸篓')
    check(bool(glob.glob(f'{CHOME}/sessions/**/*{nat}*.jsonl', recursive=True)), 'Codex 自己的记录还在')
    check(not any(x['sid'] == csid for p in api('projects').get('projects', []) for x in p['sessions']), '左栏不再有这个对话')
    api('delete', {'sid': asid})


def main():
    want = set(sys.argv[1:]) or {'codex', 'acp', 'fix', 'restart', 'rwi', 'claude', 'ui'}
    setup()
    ev = E.Events()
    try:
        csid = sc_codex(ev) if want & {'codex', 'restart', 'rwi', 'ui'} else None
        asid = sc_acp(ev) if want & {'acp', 'restart', 'ui'} else None
        if 'fix' in want:
            sc_fixes(ev)
        if 'restart' in want:
            sc_restart(ev, csid, asid)
        if 'rwi' in want:
            sc_rwi(ev, csid)
        clsid = sc_claude(ev) if want & {'claude', 'ui'} else None
        if 'ui' in want:
            sc_ui(ev, csid, asid, clsid)
        if csid and asid:
            sc_delete(csid, asid)
        if clsid:
            api('delete', {'sid': clsid})
    finally:
        ev.stop = True
        teardown()
    print('\n全部通过' if not E.FAIL else f'\n失败 {len(E.FAIL)} 项：' + '；'.join(E.FAIL))
    sys.exit(1 if E.FAIL else 0)


if __name__ == '__main__':
    main()
