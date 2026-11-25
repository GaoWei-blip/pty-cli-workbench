"""第十七轮 M 单元测试：多智能体协作（collab.py、hook_wait.py 及各挂接点）。
不启动 claude、codex、agy；数据目录、配置、令牌文件都放临时目录；额度模块用替身（X2）。
运行：.venv/bin/python -I tests/test_r17_M.py
"""
import http.client
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid

TMP = tempfile.mkdtemp(prefix='cwb-r17m-')
os.environ['CWB_DATA_DIR'] = os.path.join(TMP, 'data')
os.environ['CWB_PORT'] = '47979'
os.environ['CWB_COLLAB_RECONCILE'] = '0.01'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(os.path.dirname(ROOT), 'base')
sys.path.insert(0, ROOT)
import server as S  # noqa: E402
import providers as PV  # noqa: E402
import collab as CB  # noqa: E402
import hook_wait as HW  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


S.CONF_PATH = os.path.join(TMP, 'config.json')
S.MARKS_PATH = os.path.join(TMP, 'marks.json')
S.RUN_DIR = os.path.join(TMP, 'run')
os.makedirs(S.RUN_DIR)
S.MARKS.update({'done_at': {}, 'interrupted': {}})
EVENTS = []
_emit = S.BUS.emit
S.BUS.emit = lambda ev: (EVENTS.append(ev), _emit(ev))
PROJ = os.path.join(TMP, 'proj')
os.makedirs(PROJ)
json.dump({'folders': []}, open(S.CONF_PATH, 'w'))


def set_prefs(**kw):
    c = S.load_conf()
    c['collab'] = dict(CB.DEFAULTS, **kw)
    S.save_conf(c)


def reset():
    CB.TASKS.clear()
    CB.CHILD.clear()
    CB.REQKEY.clear()
    CB.RATE.clear()
    CB.RESV.clear()
    S.PERMS.clear()


class FakeRunner:
    def __init__(self, sid, mode='', cwd=PROJ):
        self.sid, self.cwd, self.state, self.turn, self.opts = sid, cwd, {'alive': True, 'busy': False, 'mode': mode}, False, {}
        self.base_mode, self.prewarm, self.unattended, self.title, self.started = None, False, False, '', time.time()
        S.RUNNERS[sid] = self

    gen = None

    def attention(self):
        return False

    def stop(self):
        self.state['alive'] = False


SPAWNED = []


def fake_spawn(t):
    """替身：不起真的 CLI，只登记子对话。"""
    sid = str(uuid.uuid4())
    with CB.LOCK:
        CB.CHILD[sid] = t['id']
        t['child'] = sid
        CB.TASKS[t['id']] = t
        if t.get('clientRequestId'):
            CB.REQKEY[(t['parent'], t['clientRequestId'])] = t['id']
    CB.x3_task(t)
    CB.set_status(t, 'starting')
    SPAWNED.append(t)


def x3_rows():
    try:
        return [json.loads(l) for l in open(CB.x3_path(), encoding='utf-8')]
    except OSError:
        return []


class FakeQH:
    def __init__(self, pct=None, ok=True, windows=True):
        self.pct, self.ok, self.windows = pct, ok, windows

    def get(self, pid, max_age=None):
        return {'id': pid, 'ok': self.ok, 'windows': [{'id': 'w', 'usedPercent': self.pct}] if self.windows else []}

    def tightest(self, pid):
        if self.pct is None:
            return None
        return {'usedPercent': self.pct, 'label': '每周', 'resetsAt': int(time.time() * 1000) + 5 * 3600 * 1000 - 60000}


# ================================================================ 启动参数
ARGV_SNIPPET = r'''
import json, os, sys, tempfile
sys.path.insert(0, os.getcwd())
import server as S
tmp = sys.argv[1]
S.CONF_PATH = os.path.join(tmp, 'config.json'); S.RUN_DIR = os.path.join(tmp, 'run'); S.RUNS_PATH = os.path.join(tmp, 'runs.jsonl')
os.makedirs(S.RUN_DIR, exist_ok=True)
got = {}
class P:
    upgrading = False
    def request(self, h, *a, **k):
        if h.get('op') == 'open': got['argv'] = h['argv']
        return {'pid': 4242}
    def subscribe(self, r): pass
    def send(self, *a, **k): return True
S.PTYD = P()
child = sys.argv[3] == 'child'
if child and hasattr(S, 'CB'):
    S.CB.CHILD['11111111-2222-3333-4444-555555555555'] = 'task_x'
opts = json.loads(sys.argv[2])
r = S.Runner('11111111-2222-3333-4444-555555555555', tmp, True, opts)
print(json.dumps(got['argv'], ensure_ascii=False))
'''


def argv_of(root, conf, opts, child=False):
    d = tempfile.mkdtemp(prefix='cwb-argv-', dir=TMP)
    json.dump(conf, open(os.path.join(d, 'config.json'), 'w'), ensure_ascii=False)
    p = subprocess.run([os.path.join(root, '.venv/bin/python'), '-I', '-c', ARGV_SNIPPET, d, json.dumps(opts), 'child' if child else 'parent'],
                       cwd=root, capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, CWB_PORT='47978', CWB_DATA_DIR=os.path.join(d, 'data')))
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-800:])
    argv = json.loads(p.stdout.strip().splitlines()[-1])
    return [a.replace(root, '<ROOT>').replace(d, '<TMP>') for a in argv]


def t_args_identical():
    """协作关着时，Claude 启动参数和 base（第十六轮上线版）逐字一致；子对话（即使协作开着）也一致；开着时只多三样。"""
    if not os.path.isdir(BASE):
        check(False, f'找不到基准目录 {BASE}')
        return
    persona = {'tone': 'pro', 'extra': '回答用中文'}
    for opts in ({}, {'model': 'claude-opus-5-5', 'effort': 'high', 'mode': 'manual mode'}):
        b = argv_of(BASE, {'folders': [], 'persona': persona}, opts)
        m_off = argv_of(ROOT, {'folders': [], 'persona': persona}, opts)
        m_off2 = argv_of(ROOT, {'folders': [], 'persona': persona, 'collab': dict(CB.DEFAULTS, on=False)}, opts)
        check(b == m_off, f'协作关（没设置过）：参数和 base 逐字一致（{len(b)} 个，选项 {opts or "默认"}）')
        check(b == m_off2, '协作关（设置里关着）：参数和 base 逐字一致')
        m_child = argv_of(ROOT, {'folders': [], 'persona': persona, 'collab': dict(CB.DEFAULTS, on=True)}, opts, child=True)
        check(b == m_child, '协作开但这是子对话：参数和 base 逐字一致（子对话不挂 cwb，深度固定一层）')
        m_on = argv_of(ROOT, {'folders': [], 'persona': persona, 'collab': dict(CB.DEFAULTS, on=True)}, opts)
        extra = [a for a in m_on if a not in b]
        check(m_on.count('--append-system-prompt') == 1 and m_on.count('--settings') == 1 and m_on.count('--mcp-config') == 1,
              '协作开：--append-system-prompt、--settings、--mcp-config 各一个')
        check(not any(a in ('-p', '--print', '--output-format', '--input-format') for a in m_on), '协作开：没有 -p、--output-format 等非交互参数')
        i = m_on.index('--settings')
        s_on, s_b = json.loads(m_on[i + 1]), json.loads(b[b.index('--settings') + 1])
        pt = s_on['hooks'].pop('PostToolUse', None)
        check(s_on == s_b, '协作开：--settings 里除了 PostToolUse 叫醒钩子，其余和 base 一样')
        h = (pt or [{}])[0]
        check(h.get('matcher') == 'mcp__cwb__delegate_task' and h['hooks'][0].get('asyncRewake') is True and
              h['hooks'][0]['command'].endswith('hook.py wait-task') and h['hooks'][0]['timeout'] == 30 * 60 + 600,
              '叫醒钩子：PostToolUse、matcher mcp__cwb__delegate_task、asyncRewake、hook.py wait-task、超时＝最长时间＋10 分钟')
        j = m_on.index('--append-system-prompt')
        bj = b.index('--append-system-prompt')
        check(m_on[j + 1].startswith(b[bj + 1]) and 'delegate_task' in m_on[j + 1] and 'ToolSearch' in m_on[j + 1],
              '说明并进个性化那一个 --append-system-prompt（原文在前，协作说明在后，含 ToolSearch 提示）')
        k = m_on.index('--mcp-config')
        check(m_on[k + 1].startswith('<TMP>/run/mcp-') and m_on[:k] + m_on[k + 2:] != b and 'Bearer' not in ' '.join(m_on),
              '--mcp-config 是 .run 里的文件路径，令牌不在命令行参数里')
        check(m_on[0] == b[0] == 'claude' and m_on[1:3] == b[1:3], '协作开：仍是同一个交互式 claude 启动（--session-id 新建）')


def t_claude_args_file():
    reset()
    set_prefs(on=True)
    sid = str(uuid.uuid4())
    args = ['claude', '--session-id', sid, '--settings', json.dumps({'hooks': {}}), '--append-system-prompt', 'X']
    out = CB.claude_args(sid, args)
    p = CB.mcp_path(sid)
    st = os.stat(p)
    d = json.load(open(p))
    srv = d['mcpServers']['cwb']
    check(oct(st.st_mode & 0o777) == '0o600', f'.run/mcp-<对话>.json 权限 0600（{oct(st.st_mode & 0o777)}）')
    check(srv['type'] == 'http' and srv['url'] == 'http://127.0.0.1:47979/mcp' and srv['headers']['Authorization'].startswith('Bearer cwb_'),
          'MCP 配置：http＋Bearer 令牌，指向本机 /mcp')
    tok = srv['headers']['Authorization'][7:]
    check(CB.caller_of(tok) == sid and tok not in ' '.join(out), '令牌只在内存和 0600 文件里，能认出父对话')
    out2 = CB.claude_args(sid, args)
    check(json.load(open(p)) == d, '同一个对话重启沿用同一个令牌')
    CB.TOK.clear()
    CB.SID_TOK.clear()
    CB.load_tokens()
    check(CB.caller_of(tok) == sid, '服务重启后从 0600 文件找回令牌（ptyd 里的 CLI 还在用它）')
    set_prefs(on=False)
    check(CB.claude_args(sid, args) is args, '协作关：原样返回同一个列表')
    check(out2.count('--append-system-prompt') == 1, '只有一个 --append-system-prompt')
    CB.drop_token(sid)
    check(not os.path.exists(p) and CB.caller_of(tok) is None, '删除对话：令牌和 0600 文件一起清掉')


# ================================================================ /mcp
def serve():
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), S.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def post(port, path, body, headers):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
    c.request('POST', path, body=json.dumps(body).encode(), headers=dict({'Content-Type': 'application/json'}, **headers))
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, (json.loads(data) if data else None)


def t_mcp_http():
    reset()
    set_prefs(on=True)
    sid = str(uuid.uuid4())
    FakeRunner(sid, mode='manual mode')
    tok = CB.token_for(sid)
    srv = serve()
    port = srv.server_address[1]
    try:
        st, _ = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                     {'Host': f'127.0.0.1:{port}', 'Authorization': 'Bearer nope'})
        check(st == 401, f'令牌错：401（{st}）')
        st, _ = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}, {'Host': f'127.0.0.1:{port}'})
        check(st == 401, '没带令牌：401')
        st, _ = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                     {'Host': f'127.0.0.1:{port}', 'Authorization': 'Bearer ' + S.TOKEN, 'X-Token': S.TOKEN})
        check(st == 401, '主令牌不能当对话令牌用：401')
        st, _ = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                     {'Host': 'evil.example.ts.net', 'Authorization': 'Bearer ' + tok})
        check(st == 403, f'远程 Host（远程没开）：403（{st}）')
        orig_en, orig_host = S.RM.enabled, S.RM.ST.get('host')
        S.RM.enabled = lambda: True
        S.RM.ST['host'] = 'mac.tail1234.ts.net'
        try:
            st, _ = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                         {'Host': 'mac.tail1234.ts.net', 'Authorization': 'Bearer ' + tok, 'Origin': 'https://mac.tail1234.ts.net',
                          'Sec-Fetch-Site': 'same-origin'})
            check(st == 403, f'远程 Host（远程开着、是本机开放的 ts.net 名）：/mcp 照样 403（{st}）')
        finally:
            S.RM.enabled = orig_en
            if orig_host is None:
                S.RM.ST.pop('host', None)
            else:
                S.RM.ST['host'] = orig_host
        st, _ = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                     {'Host': f'127.0.0.1:{port}', 'Authorization': 'Bearer ' + tok, 'X-Forwarded-For': '100.1.2.3'})
        check(st == 403, '本机 Host 但带 tailscale 转发头：403')
        H = {'Host': f'127.0.0.1:{port}', 'Authorization': 'Bearer ' + tok}
        st, r = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}, H)
        check(st == 200 and r['result']['protocolVersion'] == '2025-06-18' and 'tools' in r['result']['capabilities'],
              'initialize：回协议版本和 tools 能力')
        st, r = post(port, '/mcp', {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, H)
        check(st == 202 and r is None, '通知：202 没有正文')
        st, r = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, H)
        names = [t['name'] for t in r['result']['tools']]
        check(names == ['orchestrator_capabilities', 'delegate_task', 'task_status', 'task_cancel'], f'四个工具（T3 的名字）：{names}')
        dt = r['result']['tools'][1]
        check(dt['description'].startswith('Start one child agent with only `task`. No parent history.') and
              dt['inputSchema']['required'] == ['task'] and 'clientRequestId' in dt['inputSchema']['properties'],
              'delegate_task 的说明和参数按 T3 tools.ts / contracts')
        check(r['result']['tools'][0]['annotations']['readOnlyHint'] is True and '_meta' not in dt,
              '只读标注；不标 alwaysLoad（和 T3 一样放在 ToolSearch 后面）')
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request('GET', '/mcp', headers={'Host': f'127.0.0.1:{port}', 'Authorization': 'Bearer ' + tok})
        rr = c.getresponse()
        rr.read()
        check(rr.status == 405, 'GET /mcp：405（不开 SSE）')
        orig_models = CB.provider_models
        CB.provider_models = lambda p: ([{'id': p + '-m1', 'label': p + ' 一号', 'efforts': ['low', 'high'], 'isDefault': True}], '')
        orig_st = CB.prov_status
        CB.prov_status = lambda p, force=False: {'installed': True, 'ok': p != 'agy', 'hint': '还没登录 Antigravity'}
        try:
            st, r = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'orchestrator_capabilities', 'arguments': {}}}, H)
            sc = r['result']['structuredContent']
            byp = {p['providerInstanceId']: p for p in sc['providers']}
            check(not r['result']['isError'] and sc['parentThreadId'] == sid and sc['runtimeMode'] == 'approval-required',
                  'orchestrator_capabilities：父对话、运行档（manual → approval-required）')
            check(byp['codex']['canRunChildTask'] and byp['codex']['models'][0]['options'][0]['id'] == 'effort' and byp['codex']['quota'] == 'unknown',
                  '提供方：模型、effort 选项、额度未知写 unknown')
            check(not byp['agy']['canRunChildTask'] and '还没登录' in byp['agy']['constraints'][0], '没登录的一家：canRunChildTask=false 并写原因')
            check(byp['codex']['runtimeModes'] == ['approval-required', 'auto-accept-edits', 'full-access'], 'Codex 的运行档（没有 T3 的 auto）')
        finally:
            CB.provider_models, CB.prov_status = orig_models, orig_st
        set_prefs(on=False)
        st, r = post(port, '/mcp', {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                                    'params': {'name': 'delegate_task', 'arguments': {'task': 'x'}}}, H)
        check(r['result']['isError'] and r['result']['structuredContent']['code'] == 'capability_denied' and '设置' in
              r['result']['structuredContent']['message'], '协作关着：delegate_task 返回 capability_denied 和白话原因')
    finally:
        srv.shutdown()
        set_prefs(on=False)


# ================================================================ 委派规则
def with_target(models=None, status=None):
    om, osx = CB.provider_models, CB.prov_status
    CB.provider_models = lambda p: (models or [{'id': p + '-m1', 'label': p, 'efforts': ['low', 'high'], 'isDefault': True},
                                               {'id': p + '-m2', 'label': p, 'efforts': [], 'isDefault': False}], '')
    CB.prov_status = status or (lambda p, force=False: {'installed': True, 'ok': True, 'hint': ''})
    return lambda: (setattr(CB, 'provider_models', om), setattr(CB, 'prov_status', osx))


def expect_fail(fn, code, msg):
    try:
        fn()
    except CB.Fail as e:
        check(e.code == code, f'{msg}（{e.code}：{e.message[:60]}）')
        return e
    check(False, msg + '（没有报错）')


def t_delegate_rules():
    reset()
    restore = with_target()
    osp, oqh = CB.spawn, CB.QH
    CB.spawn = fake_spawn
    CB.QH = None
    try:
        set_prefs(on=False)
        sid = str(uuid.uuid4())
        FakeRunner(sid, mode='manual mode')
        expect_fail(lambda: CB.delegate(sid, {'task': 'a'}), 'capability_denied', '协作关：拒绝')
        set_prefs(on=True)
        r = CB.delegate(sid, {'task': 'say ok', 'target': {'providerInstanceId': 'codex'}, 'title': '甲'})
        t = CB.TASKS[r['taskId']]
        check(r['status'] == 'starting' and r['workState'] == 'working' and r['next'] == CB.ORCHESTRATOR_MCP_RUNNING_TASK_NEXT,
              '委派成功：starting、working、带 next（T3 原文）')
        check(t['model'] == 'codex-m1' and t['runtimeMode'] == 'approval-required' and r['quota'] == '额度未知' and t['quotaUnknown'],
              '别家默认用它的第一个（推荐）模型；运行档跟父对话；额度模块不在：照派并写“额度未知”')
        expect_fail(lambda: CB.delegate(t['child'], {'task': 'x'}), 'capability_denied', '子对话不能再委派（深度一层）')
        expect_fail(lambda: CB.delegate(sid, {'task': 'x', 'runtimeMode': 'full-access'}), 'runtime_mode_escalation_denied',
                    '要求比父对话宽的运行档：拒绝')
        expect_fail(lambda: CB.delegate(sid, {'task': 'x', 'target': {'providerInstanceId': 'codex', 'model': 'nope'}}), 'model_unavailable',
                    '模型不在列表里：model_unavailable')
        expect_fail(lambda: CB.delegate(sid, {'task': 'x', 'target': {'providerInstanceId': 'codex', 'options': {'effort': 'max'}}}),
                    'invalid_request', 'effort 不在这个模型的档位里：invalid_request')
        expect_fail(lambda: CB.delegate(sid, {'task': 'x', 'isolation': 'worktree'}), 'invalid_request', "isolation 只支持 shared")
        r1 = CB.delegate(sid, {'task': 'dup', 'clientRequestId': 'k1'})
        n = len(SPAWNED)
        r2 = CB.delegate(sid, {'task': 'dup', 'clientRequestId': 'k1'})
        check(r1['taskId'] == r2['taskId'] and len(SPAWNED) == n, 'clientRequestId 重试：返回同一个任务，不重复建')
        # 并发：每个父对话 3 个
        CB.delegate(sid, {'task': 'c3'})
        e = expect_fail(lambda: CB.delegate(sid, {'task': 'c4'}), 'limit_exceeded', '同一父对话第 4 个：超出并发上限')
        check('3 个' in e.message, '并发超限的白话说明')
        # 全局 5 个
        sid2 = str(uuid.uuid4())
        FakeRunner(sid2, mode='bypass permissions')
        CB.delegate(sid2, {'task': 'g1'})
        CB.delegate(sid2, {'task': 'g2'})
        expect_fail(lambda: CB.delegate(sid2, {'task': 'g3'}), 'limit_exceeded', '全局第 6 个：超出全局上限')
        # 频率：每小时 10 次
        for x in list(CB.TASKS.values()):
            x['status'] = 'completed'
        sid3 = str(uuid.uuid4())
        FakeRunner(sid3, mode='auto mode')
        CB.RATE[sid3] = [time.time()] * 10
        e = expect_fail(lambda: CB.delegate(sid3, {'task': 'r11'}), 'limit_exceeded', '一小时第 11 次：超出频率上限')
        check('一小时' in e.message, '频率超限的白话说明')
        CB.RATE[sid3] = []
        # 权限：父对话完全放开 → agy 子任务也是 full-access（11-25 偏离 T3：会话用 agy 的 auto 档，其余确认工作台自动允许）
        r = CB.delegate(sid2, {'task': 'agy', 'target': {'providerInstanceId': 'agy'}})
        check(CB.TASKS[r['taskId']]['runtimeMode'] == 'full-access' and CB.child_mode(CB.TASKS[r['taskId']]) == 'auto',
              f'父对话完全放开，agy 子任务也是 full-access，会话用 agy 的 auto 档（{CB.TASKS[r["taskId"]]["runtimeMode"]}）')
        r = CB.delegate(sid2, {'task': 'cc', 'target': {'providerInstanceId': 'claude'}})
        check(CB.TASKS[r['taskId']]['runtimeMode'] == 'full-access' and CB.child_mode(CB.TASKS[r['taskId']]) == 'bypass permissions',
              '§9 和父对话一样：父对话跳过确认 → Claude 子任务也是 bypass permissions')
        for x in list(CB.TASKS.values()):
            x['status'] = 'completed'
        set_prefs(on=True, perm='auto-accept-edits')
        r = CB.delegate(sid2, {'task': 'cap', 'target': {'providerInstanceId': 'codex'}})
        check(CB.TASKS[r['taskId']]['runtimeMode'] == 'auto-accept-edits' and CB.child_mode(CB.TASKS[r['taskId']]) == 'auto',
              '设置调低到“自动改文件”：Codex 子任务用 auto 档')
        expect_fail(lambda: CB.delegate(sid2, {'task': 'x', 'runtimeMode': 'full-access'}), 'runtime_mode_escalation_denied',
                    '设置调低后要求更宽：拒绝')
        set_prefs(on=True)
        # 额度：≥90% 拒绝；ok:false、没窗口按未知照派
        for x in list(CB.TASKS.values()):
            x['status'] = 'completed'
        CB.QH = FakeQH(93)
        e = expect_fail(lambda: CB.delegate(sid3, {'task': 'q', 'target': {'providerInstanceId': 'codex'}}), 'quota_exhausted', '目标额度 93%：拒绝')
        check('Codex 每周额度已用 93%' in e.message and '5 小时后重置' in e.message, f'拒绝说明：{e.message[:40]}')
        CB.QH = FakeQH(40)
        r = CB.delegate(sid3, {'task': 'q2', 'target': {'providerInstanceId': 'codex'}})
        check('quota' not in r and not CB.TASKS[r['taskId']]['quotaUnknown'], '额度 40%：照派，不标未知')
        CB.QH = FakeQH(95, ok=False)
        r = CB.delegate(sid3, {'task': 'q3', 'target': {'providerInstanceId': 'codex'}})
        check(r.get('quota') == '额度未知', '额度快照 ok:false：按未知照派')
        CB.QH = FakeQH(None)
        r = CB.delegate(sid3, {'task': 'q4', 'target': {'providerInstanceId': 'agy'}})
        check(r.get('quota') == '额度未知', '拿不到最紧的窗口：按未知照派')
        set_prefs(on=True, provs={'claude': True, 'codex': False, 'agy': True})
        expect_fail(lambda: CB.delegate(sid3, {'task': 'x', 'target': {'providerInstanceId': 'codex'}}), 'provider_unavailable',
                    '设置里没勾的一家：provider_unavailable')
        set_prefs(on=True)
        for x in list(CB.TASKS.values()):
            x['status'] = 'completed'
        r = CB.delegate(sid3, {'task': 'role', 'role': 'review'})
        check(CB.TASKS[r['taskId']]['task'] == 'Act as the review sub-agent for this task.\n\nrole', 'role：按 T3 taskPrompt 加一句')
    finally:
        CB.spawn, CB.QH = osp, oqh
        restore()
        set_prefs(on=False)


def t_status_x3():
    reset()
    restore = with_target()
    osp = CB.spawn
    CB.spawn = fake_spawn
    try:
        set_prefs(on=True)
        open(CB.x3_path(), 'w').close()
        sid = str(uuid.uuid4())
        FakeRunner(sid, mode='accept edits')
        r = CB.delegate(sid, {'task': 'x3', 'target': {'providerInstanceId': 'codex'}, 'title': '标题'})
        t = CB.TASKS[r['taskId']]
        CB.set_status(t, 'running')
        CB.set_status(t, 'waiting')
        t['summary'] = '结果正文'
        CB.set_status(t, 'completed')
        rows = x3_rows()
        check(rows[0] == {'t': 'task', 'id': t['id'], 'parent': {'conv': sid, 'provider': 'claude'},
                          'child': {'conv': t['child'], 'provider': 'codex', 'model': 'codex-m1'}, 'title': '标题',
                          'mode': 'auto-accept-edits', 'origin': 'model', 'createdAt': t['createdAt']}, 'X3 task 行：字段齐全、只有约定的键')
        sts = [x['status'] for x in rows[1:]]
        check(sts == ['queued', 'running', 'waiting', 'done'], f'X3 task_status：{sts}')
        last = rows[-1]
        check(set(last) == {'t', 'id', 'status', 'ts', 'resultBytes', 'delivered'} and last['resultBytes'] == len('结果正文'.encode()) and
              last['delivered'] is False and isinstance(last['ts'], int) and last['ts'] > 10 ** 12, '结束行带 resultBytes、delivered，时间是毫秒')
        check(not CB.set_status(t, 'running'), '结束后不再回到运行中')
        rd = CB.read_task(sid, t['id'], ack=True)
        check(rd['summary'] == '结果正文' and rd['status'] == 'completed' and rd['workState'] == 'result_available' and 'next' not in rd,
              'task_status：已完成、带结果、没有 next')
        check(t['delivery'] == 'acknowledged' and x3_rows()[-1]['delivered'] is True, 'T3：读到结果就记为已送达（X3 再写一行 delivered:true）')
        expect_fail(lambda: CB.read_task(str(uuid.uuid4()), t['id']), 'task_not_found', '别的对话查不到这个任务')
        # 取消：T3 cancel 丢掉交回；超时记 timeout
        r = CB.delegate(sid, {'task': 'c', 'target': {'providerInstanceId': 'codex'}})
        t2 = CB.TASKS[r['taskId']]
        stopped = []
        osc = CB.stop_child
        CB.stop_child = lambda t: stopped.append(t['id'])
        try:
            res = CB.cancel_tool(sid, {'taskId': t2['id']})
            check(res == {'taskId': t2['id'], 'status': 'cancel_requested'} and t2['status'] == 'cancelled' and t2['delivery'] == 'disposed'
                  and stopped == [t2['id']], 'task_cancel：停子对话、cancelled、不再叫醒（disposed）')
            check(x3_rows()[-1]['status'] == 'stopped', 'X3：cancelled 记 stopped')
            r = CB.delegate(sid, {'task': 'to', 'target': {'providerInstanceId': 'codex'}})
            t3 = CB.TASKS[r['taskId']]
            t3['createdAt'] -= 31 * 60 * 1000
            CB.track(t3, CB.prefs(), time.time())
            check(t3['status'] == 'interrupted' and t3['reason'] == 'timeout' and x3_rows()[-1]['status'] == 'timeout' and
                  t3['delivery'] == 'none' and '自动停止' in t3['summary'], '超过 30 分钟：自动停，X3 记 timeout，照常交回')
        finally:
            CB.stop_child = osc
    finally:
        CB.spawn = osp
        restore()
        set_prefs(on=False)


def t_wake():
    reset()
    tick = CB.tick
    CB.tick = lambda: None  # 后台循环每秒跑一次 tick，会把测试里没有真子对话的任务记成“没了”；这里手动调
    big = 'x' * 17000
    ts = [{'id': 'task_a', 'title': 'A', 'child': 'c1', 'status': 'completed', 'summary': '短结果'},
          {'id': 'task_b', 'title': 'B', 'child': 'c2', 'status': 'failed', 'summary': big}]
    d = CB.wake_detail(ts)
    check(d.startswith('Delegated task task_a (A) in child thread c1 [completed]:\n短结果'), '叫醒正文：T3 delegatedCompletionWakeDetail 的格式')
    check('Delegated task task_b (B) in child thread c2 [failed]. Use task_status with taskId task_b to read the result.' in d and big not in d,
          '合计超过 16000 字节：只给标签，让智能体自己用 task_status 读')
    check(CB.hook_task_id({'tool_response': [{'type': 'text', 'text': '{"taskId": "task_123abc", "status": "running"}'}]}) == 'task_123abc',
          '钩子输入的 tool_response（内容块数组，实测格式）里取出 taskId')
    # 钩子长轮询：结束时交回；已交回的再来就 skip；关了自动交回就 skip 并等按钮
    set_prefs(on=True)
    sid = str(uuid.uuid4())
    FakeRunner(sid)
    t = {'id': 'task_w1', 'parent': sid, 'parentProv': 'claude', 'child': 'k1', 'childProv': 'codex', 'model': 'm', 'title': 'W',
         'task': 'x', 'runtimeMode': 'approval-required', 'origin': 'model', 'createdAt': CB.now_ms(), 'status': 'running', 'x3': 'running',
         'delivery': 'none'}
    CB.TASKS[t['id']] = t
    CB.CHILD['k1'] = t['id']
    CB.WAKE_POLL = 5
    body = {'tool_response': [{'type': 'text', 'text': json.dumps({'taskId': 'task_w1'})}]}
    out = {}
    th = threading.Thread(target=lambda: out.update(r=CB.task_wait(sid, body, '')))
    th.start()
    time.sleep(0.4)
    t['summary'] = '完成了'
    CB.set_status(t, 'completed')
    th.join(5)
    check(out.get('r', {}).get('deliver') and '[completed]:\n完成了' in out['r']['text'] and t['delivery'] == 'none',
          '钩子在等：子任务一结束马上交回（deliver），钩子回报之前还不算送达')
    check(CB.task_ack(str(uuid.uuid4()), body, '') == {'ok': False} and t['delivery'] == 'none', '别的对话回报不算')
    check(CB.task_ack(sid, body, '') == {'ok': True} and t['delivery'] == 'acknowledged' and t['wake'] == 'hook' and
          x3_rows()[-1].get('delivered') is True, '钩子回报（task-ack）后记已送达，X3 写 delivered:true')
    check(CB.task_wait(sid, body, '') == {'skip': True}, '已交回过：skip')
    check(CB.task_wait(sid, {'tool_response': [{'type': 'text', 'text': '{"code": "limit_exceeded"}'}]}, '') == {'skip': True},
          '委派失败（没有 taskId）：skip')
    check(CB.task_wait(str(uuid.uuid4()), body, '') == {'skip': True}, '别的对话的钩子拿不到这个任务')
    t2 = dict(t, id='task_w2', delivery='none', status='completed', summary='r2')
    CB.TASKS['task_w2'] = t2
    set_prefs(on=True, autoReturn=False)
    check(CB.task_wait(sid, {'tool_response': [{'type': 'text', 'text': '{"taskId": "task_w2"}'}]}, '') == {'skip': True} and
          t2['delivery'] == 'pending', '关了自动交回：钩子 skip，卡片等用户点')
    set_prefs(on=True)
    # 叫醒丢失：结束后没人来取 → pending → 按钮发出（按现有发消息流程）
    CB.LOST_AFTER = 0.2
    t3 = dict(t, id='task_w3', delivery='none', status='completed', summary='r3', title='丢了')
    t3.pop('_waiter', None)
    CB.TASKS['task_w3'] = t3
    tick()
    time.sleep(0.3)
    tick()
    check(t3['delivery'] == 'pending', '叫醒丢失（钩子没来取）：记“待交回”')
    # 钩子进程在长轮询中被结束：服务端照样把内容“交”给断开的连接，但没有回报 → 过一会儿也记“待交回”
    t4 = dict(t, id='task_w4', delivery='none', status='running', summary='', title='断了')
    t4.pop('_waiter', None)
    CB.TASKS['task_w4'] = t4
    CB.WAKE_POLL = 5
    b4 = {'tool_response': [{'type': 'text', 'text': '{"taskId": "task_w4"}'}]}
    th = threading.Thread(target=lambda: out.update(r4=CB.task_wait(sid, b4, '')))
    th.start()
    time.sleep(0.3)
    t4['summary'] = 'r4'
    CB.set_status(t4, 'completed')
    th.join(5)
    tick()
    check(out.get('r4', {}).get('deliver') and t4['delivery'] == 'none', '交给了已经断开的钩子：还不算送达')
    time.sleep(0.3)
    tick()
    check(t4['delivery'] == 'pending', '一直没有回报：记“待交回”，卡片上出按钮')
    sent = []
    osend = S.API['send']
    S.API['send'] = lambda b: sent.append(b) or {'ok': True}
    try:
        r = CB.api_deliver({'id': 'task_w3'})
        check(r['ok'] and sent and sent[0]['sid'] == sid and '子任务《丢了》（Codex）的结果：' in sent[0]['text'] and 'r3' in sent[0]['text'] and
              t3['delivery'] == 'acknowledged' and t3['wake'] == 'button', '“把结果交给 Claude”：用户点了才按现有流程发出')
        try:
            CB.api_deliver({'id': 'task_w3'})
            check(False, '交回过的不能再交')
        except ValueError:
            check(True, '交回过的不能再交')
    finally:
        S.API['send'] = osend
        set_prefs(on=False)
        CB.LOST_AFTER = 40
        CB.tick = tick


def t_hook_wait():
    """hook_wait.py：服务不在时重试等新服务；deliver → 退出码 2 写 stderr；skip → 0。"""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    seen, acks = [], []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            self.rfile.read(n)
            if self.path == '/hook/task-ack':
                acks.append(len(seen))
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')
                return
            seen.append(self.headers.get('X-Token'))
            if self.headers.get('X-Token') != 'good':
                self.send_response(401)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            body = json.dumps({'wait': True} if len(seen) < 3 else {'deliver': True, 'text': 'Delegated task t1 [completed]:\nOK'}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    run = os.path.join(TMP, 'hwrun')
    os.makedirs(run, exist_ok=True)
    open(os.path.join(run, f'token-{port}'), 'w').write('good')

    def start_later():
        time.sleep(1.5)  # 先让钩子连不上（服务在重启）
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=start_later, daemon=True).start()
    buf = io.StringIO()
    t0 = time.time()
    code = HW.main('{"tool_response": []}', str(port), 'stale', 'sid', 'gen', run, out=buf)
    check(code == 2 and buf.getvalue() == 'Delegated task t1 [completed]:\nOK' and time.time() - t0 >= 1.4,
          '服务重启期间连不上：重试等新服务，拿到结果以退出码 2 交回（stderr 是交回内容）')
    check(seen[0] == 'stale' and 'good' in seen, '401 时重读令牌文件')
    check(acks == [len(seen)], '拿到交回内容先回报 /hook/task-ack，再写 stderr')
    p = subprocess.run([os.path.join(ROOT, '.venv/bin/python'), '-I', os.path.join(ROOT, 'hook.py'), 'wait-task'], input='{}',
                       capture_output=True, text=True, env={'PATH': os.environ['PATH']}, timeout=20)
    check(p.returncode == 0 and not p.stderr, '没有 CWB_PORT（不是工作台启动的 CLI）：hook.py wait-task 直接退出 0')


def t_codex_hooks():
    reset()
    c = PV.PConv(str(uuid.uuid4()), 'codex', PROJ)
    PV.CONVS[c.sid] = c
    params = {'cwd': PROJ}
    set_prefs(on=False)
    check(CB.codex_thread(c, params) is params, 'Codex：协作关，thread/start 参数原样')
    set_prefs(on=True)
    p2 = CB.codex_thread(c, params)
    m = p2['config']['mcp_servers.cwb']
    check(list(p2['config']) == ['mcp_servers.cwb'] and m['url'] == 'http://127.0.0.1:47979/mcp' and m['tool_timeout_sec'] == 3900 and
          m['http_headers']['Authorization'].startswith('Bearer cwb_'), 'Codex：config 用点号键 mcp_servers.cwb（T3 写法，不整表传）')
    check('delegate_task' in p2['developerInstructions'] and 'ToolSearch' not in p2['developerInstructions'], 'Codex：说明放 developerInstructions')
    CB.CHILD[c.sid] = 'task_x'
    check(CB.codex_thread(c, params) is params, 'Codex 子对话：不挂 cwb')
    CB.CHILD.pop(c.sid)
    # MCP 工具授权（mcpServer/elicitation/request）→ 授权卡 → accept / decline
    resp = []
    rpc = types.SimpleNamespace(respond=lambda rid, res=None, error=None: resp.append((rid, res)))
    ad = types.SimpleNamespace(core=PV)
    asked = []
    oask = PV.ask_perm
    PV.ask_perm = lambda c, tool, inp, view, sug, done, extra=None: asked.append((tool, inp, done)) or 'rid1'
    try:
        p = {'threadId': 'th', 'serverName': 'cwb', 'mode': 'form', 'message': 'Allow the cwb MCP server to run tool "delegate_task"?',
             'requestedSchema': {}, '_meta': {'codex_approval_kind': 'mcp_tool_call', 'tool_name': 'delegate_task',
                                              'tool_params': {'task': '查一下', 'target': {'providerInstanceId': 'agy'}}}}
        ok = CB.codex_elicitation(ad, rpc, 7, p, c)
        check(ok and asked and asked[0][0] == 'mcp__cwb__delegate_task' and asked[0][1]['_cwb_preview']['to'] == 'Antigravity' and
              asked[0][1]['_cwb_preview']['who'] == 'Codex', 'Codex 调委派工具要授权：出授权卡，写“Codex 想让 Antigravity 做”')
        asked[0][2]('allow', '')
        asked[0][2]('deny', '')
        check(resp == [(7, {'action': 'accept'}), (7, {'action': 'decline'})], '允许 → accept，拒绝 → decline')
        c.meta['opts'] = {'mode': 'full'}
        resp.clear()
        CB.codex_elicitation(ad, rpc, 8, p, c)
        check(resp == [(8, {'action': 'accept'})], 'T3：完全放开档直接接受')
    finally:
        PV.ask_perm = oask
        set_prefs(on=False)


def t_agy_full_access():
    """11-25：父对话跳过确认 → agy 子对话记 autoAllow，所有 session/request_permission 由工作台直接允许、不出授权卡。"""
    import prov_acp as PA
    reset()
    restore = with_target()
    oqh, ocwd, opv, oapi = CB.QH, CB.parent_cwd, CB.NS.get('PV'), CB.NS['API']
    CB.QH = None
    CB.parent_cwd = lambda sid: PROJ
    made = []

    def prov_new(b):
        c = PV.PConv(str(uuid.uuid4()), b['prov'], b['cwd'])
        c.meta['opts'] = dict(b.get('opts') or {})
        fake.CONVS[c.sid] = c
        made.append(c)
        return {'sid': c.sid}
    fake = types.SimpleNamespace(CONVS={}, api_prov_new=prov_new, ADAPTERS={}, DATA_DIR=getattr(opv, 'DATA_DIR', PV.DATA_DIR))
    CB.NS['PV'] = fake
    CB.NS['API'] = dict(oapi, send=lambda b: {})
    try:
        set_prefs(on=True)
        cases = [('bypass permissions', None, True, '父对话跳过确认'), ('auto mode', None, False, '父对话自动档'),
                 ('bypass permissions', 'auto-accept-edits', False, '父对话跳过确认但点名要窄档'),
                 ('bypass permissions', 'plan', False, '父对话跳过确认但子任务用规划档')]
        for pmode, req, want, label in cases:
            for x in list(CB.TASKS.values()):
                x['status'] = 'completed'
            sid = str(uuid.uuid4())
            FakeRunner(sid, mode=pmode)
            inp = {'task': 't', 'target': {'providerInstanceId': 'agy'}}
            if req == 'plan':
                inp['interactionMode'] = 'plan'
            elif req:
                inp['runtimeMode'] = req
            CB.delegate(sid, inp)
            c = made[-1]
            check(bool(c.meta.get('autoAllow')) == want, f'{label}：agy 子对话 autoAllow={want}（{c.meta.get("opts")}）')
        # 手动复核（只读档）不放开
        t = {'childProv': 'agy', 'runtimeMode': 'full-access', 'origin': 'manual', 'interactionMode': 'default'}
        check(CB.child_mode(t) == 'default', '手动复核：agy 仍是每步确认档')
        # on_request：autoAllow 的对话直接回“允许这一次”，不调 ask_perm；没有 autoAllow 照旧出卡
        resp, asked = [], []
        rpc = types.SimpleNamespace(respond=lambda rid, res=None, error=None: resp.append((rid, res)))
        c1 = PV.PConv(str(uuid.uuid4()), 'agy', PROJ)
        c1.meta['autoAllow'] = True
        c2 = PV.PConv(str(uuid.uuid4()), 'agy', PROJ)
        PV.CONVS[c1.sid], PV.CONVS[c2.sid] = c1, c2
        ad = PA.AcpAdapter.__new__(PA.AcpAdapter)
        ad.rpc, ad.sessions, ad.nat = rpc, {'n1': c1.sid, 'n2': c2.sid}, {}
        oask = PV.ask_perm
        PV.ask_perm = lambda c, tool, inp, view, sug, done, extra=None: asked.append(c.sid) or 'rid'
        opts = [{'kind': 'allow_always', 'optionId': 'aa', 'name': '总是允许'}, {'kind': 'allow_once', 'optionId': 'ao', 'name': '允许'},
                {'kind': 'reject_once', 'optionId': 'ro', 'name': '拒绝'}]
        try:
            ad.on_request(5, 'session/request_permission',
                          {'sessionId': 'n1', 'options': opts, 'toolCall': {'toolCallId': 'x', 'kind': 'delete', 'title': 'rm -rf build'}})
            check(resp == [(5, {'outcome': {'outcome': 'selected', 'optionId': 'ao'}})] and not asked,
                  'autoAllow：直接选“允许这一次”（不选“总是允许”，免得写进 agy 设置），不出授权卡')
            resp.clear()
            ad.on_request(6, 'session/request_permission',
                          {'sessionId': 'n1', 'options': opts[:1], 'toolCall': {'toolCallId': 'y', 'kind': 'execute', 'title': 'x'}})
            check(resp == [(6, {'outcome': {'outcome': 'selected', 'optionId': 'aa'}})], 'autoAllow：只有“总是允许”时选它')
            resp.clear()
            ad.on_request(7, 'session/request_permission',
                          {'sessionId': 'n2', 'options': opts, 'toolCall': {'toolCallId': 'z', 'kind': 'execute', 'title': 'ls'}})
            check(not resp and asked == [c2.sid], '没有 autoAllow 的 agy 对话：照旧出授权卡')
        finally:
            PV.ask_perm = oask
            PV.CONVS.pop(c1.sid, None)
            PV.CONVS.pop(c2.sid, None)
    finally:
        restore()
        CB.QH, CB.parent_cwd, CB.NS['API'] = oqh, ocwd, oapi
        if opv is None:
            CB.NS.pop('PV', None)
        else:
            CB.NS['PV'] = opv
        set_prefs(on=False)


def t_sys_note_and_wrappers():
    raw = ('<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n<system-reminder>\nStop hook blocking error from command '
           '"PostToolUse:mcp__cwb__delegate_task": Delegated task task_0123456789abcdef (X) in child thread c [completed]:\nok\n</system-reminder>')
    it = S.sys_note({'kind': 'task-notification', 'producer': 'session-task'}, raw)
    check(it['k'] == 'sys' and it['tag'] == '子任务' and it['text'] == '子任务已完成，结果已交回' and it['cwbTask'] == 'task_0123456789abcdef',
          '叫醒那一行（origin.kind=task-notification）画成灰色系统行“子任务已完成，结果已交回”')
    it2 = S.sys_note({'kind': 'task-notification'}, '<task-notification><summary>Background command done</summary><status>completed</status></task-notification>')
    check(it2['tag'] == '后台任务通知', '别的后台任务通知不受影响')
    check(S.system_origin({'type': 'user', 'origin': {'kind': 'task-notification', 'producer': 'session-task'}, 'promptSource': 'system'}) is not None,
          '现有过滤把它当系统消息（不是用户气泡）')
    # 级联：用户停父对话（interrupt），连带停掉进行中的子任务，不再叫醒
    reset()
    psid = str(uuid.uuid4())
    for i in range(2):
        t = {'id': f'task_c{i}', 'parent': psid, 'parentProv': 'claude', 'child': f'cc{i}', 'childProv': 'codex', 'title': 'c', 'task': 'x',
             'runtimeMode': 'approval-required', 'origin': 'model', 'createdAt': CB.now_ms(), 'status': 'running', 'x3': 'running', 'delivery': 'none'}
        CB.TASKS[t['id']] = t
    done = dict(CB.TASKS['task_c0'], id='task_c9', status='completed')
    CB.TASKS['task_c9'] = done
    stopped = []
    osc = CB.stop_child
    CB.stop_child = lambda t: stopped.append(t['id'])
    try:
        S.API['interrupt']({'sid': psid})
        check(sorted(stopped) == ['task_c0', 'task_c1'] and all(CB.TASKS[f'task_c{i}']['status'] == 'cancelled' and
                                                                 CB.TASKS[f'task_c{i}']['delivery'] == 'disposed' for i in range(2)) and done['status'] == 'completed',
              '父对话点停止：进行中的子任务一起停（cancelled，不再叫醒），已完成的不动')
        t = dict(CB.TASKS['task_c0'], id='task_c5', status='running', delivery='none')
        CB.TASKS['task_c5'] = t
        stopped.clear()
        oarch = CB.NS['API']
        S.API['collab-cascade']({'sid': psid})
        check(stopped == ['task_c5'], '删除、归档前确认后：collab-cascade 停掉子任务')
    finally:
        CB.stop_child = osc
    # 左栏：子对话带 cwbParent，父对话带在跑的子任务个数
    reset()
    psid = str(uuid.uuid4())
    c = PV.PConv(str(uuid.uuid4()), 'codex', PROJ)
    c.persisted = True
    c.items = [{'k': 'user', 'text': 'x', 'id': 'p1'}]
    PV.CONVS[c.sid] = c
    t = {'id': 'task_s1', 'parent': psid, 'parentProv': 'claude', 'child': c.sid, 'childProv': 'codex', 'title': 's', 'task': 'x',
         'runtimeMode': 'approval-required', 'origin': 'model', 'createdAt': CB.now_ms(), 'status': 'running', 'x3': 'running', 'delivery': 'none'}
    CB.TASKS['task_s1'] = t
    CB.CHILD[c.sid] = 'task_s1'
    orig = S.API['projects']
    d = orig({})
    ss = [s for p in d['projects'] for s in p['sessions'] if s['sid'] == c.sid]
    check(ss and ss[0].get('cwbParent') == psid and ss[0].get('cwbTask') == 'task_s1', '左栏：子对话带 cwbParent、cwbTask')
    PV.CONVS.pop(c.sid, None)


def t_auto_allow():
    """T3 预先放行自己的 MCP 工具；工作台在已有的 PermissionRequest 钩子里放行查看、查状态、取消，委派照样出授权卡。"""
    reset()
    set_prefs(on=True)
    sid = str(uuid.uuid4())
    FakeRunner(sid, 'manual mode')
    CB.claude_args(sid, ['claude', '--settings', '{}'])  # 发令牌（协作开着的父对话）
    ns = CB.NS
    allow = {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}
    check(all(S.handle_hook('permission', sid, {'tool_name': n, 'tool_input': {}}, '') == allow for n in CB.AUTO_ALLOW),
          '父对话调 orchestrator_capabilities、task_status、task_cancel：钩子直接放行，不出授权卡')
    check(CB.auto_allow(ns, sid, {'tool_name': 'mcp__cwb__delegate_task'}, '') is None, '委派（delegate_task）照样出授权卡（PLAN M8）')
    other = str(uuid.uuid4())
    FakeRunner(other, 'manual mode')
    check(CB.auto_allow(ns, other, {'tool_name': 'mcp__cwb__task_status'}, '') is None, '没挂 cwb 的对话（没有令牌）不放行')
    kid = str(uuid.uuid4())
    FakeRunner(kid, 'manual mode')
    CB.SID_TOK[kid] = 'cwb_fake'
    CB.CHILD[kid] = 'task_x'
    check(CB.auto_allow(ns, kid, {'tool_name': 'mcp__cwb__task_status'}, '') is None, '子对话不放行')
    CB.drop_token(sid)
    set_prefs(on=False)


# ================================================================ 复核修复（REVIEW-M.md 第二节）
def t_review_fixes():
    otick = CB.tick
    CB.tick = lambda: None  # 后台循环每秒跑 tick，会把没有真子对话的任务记成“没了”；整段测试不让它动
    try:
        _review_fixes()
    finally:
        CB.tick = otick


def _review_fixes():
    # M-1：上限每对话 2、全局 3，同时发 6 个（建对话故意放慢，拉大窗口）：最多 2／3 个
    reset()
    restore = with_target()
    osp, oqh = CB.spawn, CB.QH
    CB.QH = None

    def slow_spawn(t):
        time.sleep(0.3)
        fake_spawn(t)
    CB.spawn = slow_spawn
    try:
        set_prefs(on=True, maxPer=2, maxAll=3)
        a = str(uuid.uuid4())
        FakeRunner(a, mode='manual mode')
        res = []

        def go(sid, i):
            try:
                CB.delegate(sid, {'task': f'SLOW:20 #{i}', 'target': {'providerInstanceId': 'codex'}})
                res.append(('ok', sid))
            except CB.Fail as e:
                res.append((e.code, sid))
        ths = [threading.Thread(target=go, args=(a, i)) for i in range(6)]
        [t.start() for t in ths]
        [t.join(10) for t in ths]
        live = [t for t in CB.TASKS.values() if CB.live(t)]
        check(sum(1 for r in res if r[0] == 'ok') == 2 and len(live) == 2 and sum(1 for r in res if r[0] == 'limit_exceeded') == 4,
              f'同一父对话同时发 6 个（上限 2）：只接受 2 个，同时在跑 2 个（{res}）')
        b, c = str(uuid.uuid4()), str(uuid.uuid4())
        FakeRunner(b, mode='manual mode')
        FakeRunner(c, mode='manual mode')
        res.clear()
        ths = [threading.Thread(target=go, args=(x, i)) for i, x in enumerate([b, b, b, c, c, c])]
        [t.start() for t in ths]
        [t.join(10) for t in ths]
        live = [t for t in CB.TASKS.values() if CB.live(t)]
        check(len(live) == 3 and sum(1 for r in res if r[0] == 'ok') == 1, f'两个父对话再同时各发 3 个（全局上限 3）：全局只到 3 个（在跑 {len(live)}）')
        check(not CB.RESV, '占位都已释放')
        # 建对话失败：占位和频率记录退回
        n0 = len(CB.RATE.get(a, []))
        for x in list(CB.TASKS.values()):
            x['status'] = 'completed'

        def bad_spawn(t):
            raise RuntimeError('起不来')
        CB.spawn = bad_spawn
        expect_fail(lambda: CB.delegate(a, {'task': 'x', 'target': {'providerInstanceId': 'codex'}}), 'orchestration_error', '建对话失败：orchestration_error')
        check(not CB.RESV and len(CB.RATE.get(a, [])) == n0, '建对话失败：占位释放、频率记录退回')
    finally:
        CB.spawn, CB.QH = osp, oqh
        restore()
        set_prefs(on=False)
    # M-2：开关变化时停掉预热的 CLI；新建对话前停掉和开关对不上的
    reset()

    class PreRunner(FakeRunner):
        stopped = 0

        def stop(self):
            self.stopped += 1
            self.state['alive'] = False
    set_prefs(on=True)
    pw = str(uuid.uuid4())
    pr = PreRunner(pw)
    CB.claude_args(pw, ['claude', '--settings', '{}'])
    S.PREWARM[PROJ] = pr
    check(CB.ARGS_ON.get(pw) is True, '记下这个 CLI 启动时挂了 cwb')
    CB.api_prefs({'set': {'on': False}})
    check(pr.stopped == 1 and PROJ not in S.PREWARM, '关掉协作：开着时预热的 CLI 停掉（复核 M-2）')
    pw2 = str(uuid.uuid4())
    pr2 = PreRunner(pw2)
    CB.ARGS_ON[pw2] = True  # 模拟：开着时预热、开关改动没经过设置页（比如直接改了 config.json）
    S.PREWARM[PROJ] = pr2
    seen = []
    o_new = CB.NS['API']['new']
    try:
        CB.NS['API']['new']  # 包装后的 new
        orig = o_new.__closure__  # 只为确认是包装函数
        check(orig is not None, 'api new 已包装')
        n = CB.drop_prewarm()
        check(n == 1 and pr2.stopped == 1 and PROJ not in S.PREWARM, '新建对话前：挂没挂 cwb 和现在的开关对不上的预热 CLI 停掉，不复用')
        pw3 = str(uuid.uuid4())
        pr3 = PreRunner(pw3)
        CB.claude_args(pw3, ['claude', '--settings', '{}'])
        S.PREWARM[PROJ] = pr3
        check(CB.ARGS_ON.get(pw3) is False and CB.drop_prewarm() == 0 and S.PREWARM.get(PROJ) is pr3, '开关对得上：照常复用')
    finally:
        S.PREWARM.pop(PROJ, None)
        S.RUNNERS.pop(pw, None), S.RUNNERS.pop(pw2, None)
    # M-5：中段抛异常，原样返回同一个列表
    set_prefs(on=True)
    owh = CB.wake_hook

    def boom():
        raise RuntimeError('中段出错')
    CB.wake_hook = boom
    try:
        args = ['claude', '--settings', '{"env": {}}', '--append-system-prompt', 'X']
        sid = str(uuid.uuid4())
        check(CB.claude_args(sid, args) is args and args == ['claude', '--settings', '{"env": {}}', '--append-system-prompt', 'X'] and
              CB.ARGS_ON[sid] is False, 'claude_args 中段抛异常：原样返回同一个列表，不改参数（复核 M-5）')
    finally:
        CB.wake_hook = owh
        CB.drop_token(sid)
        set_prefs(on=False)
    # M-3：用户 config.toml 的 developer_instructions 留着，我们的接在后面
    home = os.path.join(TMP, 'chome-m3')
    os.makedirs(home, exist_ok=True)
    open(os.path.join(home, 'config.toml'), 'w').write('developer_instructions = "USER-DEV-MARK-123"\nmodel = "x"\n[features]\nplugins = false\n')
    old_home = os.environ.get('CWB_CODEX_HOME')
    os.environ['CWB_CODEX_HOME'] = home
    set_prefs(on=True)
    c = PV.PConv(str(uuid.uuid4()), 'codex', PROJ)
    PV.CONVS[c.sid] = c
    try:
        p = CB.codex_thread(c, {'threadId': 'x'})
        di = p.get('developerInstructions') or ''
        check(di.startswith('USER-DEV-MARK-123') and 'Claude Workbench orchestration' in di, 'Codex：用户的 developer_instructions 在前，编排说明接在后面（复核 M-3）')
        p2 = CB.codex_thread(c, {'developerInstructions': 'P-LINE'})
        check(p2['developerInstructions'].startswith('P-LINE\n\n'), '参数里已有 developerInstructions：同样接在后面')
    finally:
        PV.CONVS.pop(c.sid, None)
        CB.drop_token(c.sid)
        set_prefs(on=False)
        if old_home is None:
            os.environ.pop('CWB_CODEX_HOME', None)
        else:
            os.environ['CWB_CODEX_HOME'] = old_home
    # M-6：Content-Length 非法回 400
    srv = serve()
    try:
        port = srv.server_address[1]
        for bad in ('abc', '-5'):
            sk = socket.create_connection(('127.0.0.1', port), timeout=5)
            sk.sendall(('POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer x\r\nContent-Length: %s\r\n\r\n' % (port, bad)).encode())
            head = sk.recv(200).decode(errors='replace')
            sk.close()
            check(head.startswith('HTTP/1.0 400') or head.startswith('HTTP/1.1 400'), f'/mcp 的 Content-Length 是 {bad}：回 400（复核 M-6）')
    finally:
        srv.shutdown()
    # M-7：状态没变不重写任务文件
    reset()
    t = {'id': 'task_m7', 'parent': 'p', 'parentProv': 'claude', 'child': 'k', 'childProv': 'codex', 'title': 't', 'task': 'x',
         'runtimeMode': 'approval-required', 'origin': 'model', 'createdAt': CB.now_ms(), 'status': 'running', 'x3': 'running', 'delivery': 'none'}
    CB.TASKS['task_m7'] = t
    saves = []
    osave = CB.save
    CB.save = lambda x: saves.append(x['id'])
    try:
        CB.set_status(t, 'running')
        CB.set_status(t, 'running')
        n_same = len(saves)
        CB.set_status(t, 'waiting')
        check(n_same == 0 and len(saves) == 1, '状态没变不重写任务文件，变了才写（复核 M-7）')
    finally:
        CB.save = osave
    # M-8：旧代次的钩子：task-wait 回 skip、task-ack 不算送达
    reset()
    psid = str(uuid.uuid4())
    pr = FakeRunner(psid, 'manual mode')
    pr.gen = 'gen-now'
    t = {'id': 'task_m8', 'parent': psid, 'parentProv': 'claude', 'child': 'k8', 'childProv': 'codex', 'title': 't', 'task': 'x',
         'runtimeMode': 'approval-required', 'origin': 'model', 'createdAt': CB.now_ms(), 'status': 'completed', 'x3': 'done',
         'delivery': 'none', 'summary': 'r'}
    CB.TASKS['task_m8'] = t
    body = {'tool_response': [{'type': 'text', 'text': '{"taskId": "task_m8"}'}]}
    check(CB.task_wait(psid, body, 'gen-old') == {'skip': True}, '旧代次的钩子 task-wait：skip（复核 M-8）')
    check(CB.task_ack(psid, body, 'gen-old') == {'ok': False} and t['delivery'] == 'none', '旧代次的钩子 task-ack：不算送达')
    r = CB.task_wait(psid, body, 'gen-now')
    check(r.get('deliver') and CB.task_ack(psid, body, 'gen-now') == {'ok': True} and t['delivery'] == 'acknowledged', '当前代次照常交回、记已送达')
    S.RUNNERS.pop(psid, None)
    # M-9：原接口失败时不停子任务、不清令牌；成功后才停、才清
    reset()
    set_prefs(on=True)
    psid = str(uuid.uuid4())
    FakeRunner(psid, 'manual mode')
    CB.claude_args(psid, ['claude', '--settings', '{}'])
    tok = CB.SID_TOK[psid]
    t = {'id': 'task_m9', 'parent': psid, 'parentProv': 'claude', 'child': 'k9', 'childProv': 'codex', 'title': 't', 'task': 'x',
         'runtimeMode': 'approval-required', 'origin': 'model', 'createdAt': CB.now_ms(), 'status': 'running', 'x3': 'running', 'delivery': 'none'}
    CB.TASKS['task_m9'] = t
    osc = CB.stop_child
    CB.stop_child = lambda x: None
    try:
        try:
            S.API['delete']({'sid': psid})  # 还没有记录：原接口报错
            check(False, '删除没有记录的对话应当报错')
        except ValueError:
            pass
        check(t['status'] == 'running' and CB.caller_of(tok) == psid, '删除失败：子任务不停、令牌照旧（复核 M-9）')
        tr = os.path.join(TMP, 'm9.jsonl')
        open(tr, 'w').write('')
        ofind, otrash = S.find_transcript, S.trash
        S.find_transcript = lambda sid: tr if sid == psid else ofind(sid)
        S.trash = lambda paths: True
        try:
            S.API['delete']({'sid': psid})
        finally:
            S.find_transcript, S.trash = ofind, otrash
        check(t['status'] == 'cancelled' and CB.caller_of(tok) is None, '删除成功后：子任务一起停、令牌清掉')
    finally:
        CB.stop_child = osc
        S.RUNNERS.pop(psid, None)
        set_prefs(on=False)
    # M-10：无人值守的定时任务：只读的 cwb 工具也不例外
    reset()
    set_prefs(on=True)
    sid = str(uuid.uuid4())
    ur = FakeRunner(sid, 'manual mode')
    ur.unattended = True
    CB.claude_args(sid, ['claude', '--settings', '{}'])
    res = S.handle_hook('permission', sid, {'tool_name': 'mcp__cwb__orchestrator_capabilities', 'tool_input': {}}, '')
    check(res['hookSpecificOutput']['decision']['behavior'] == 'deny', '无人值守的定时任务：只读的 cwb 工具也照现有规则拒绝（复核 M-10）')
    CB.drop_token(sid)
    S.RUNNERS.pop(sid, None)
    set_prefs(on=False)


def t_review_text():
    reset()
    c = PV.PConv(str(uuid.uuid4()), 'codex', PROJ)
    c.items = [{'k': 'user', 'text': '改一下', 'id': 'p1'},
               {'k': 'tool', 'name': 'Edit', 'path': PROJ + '/a.py', 'ln': [2, 1], 'diff': {'old': 'a = 1', 'new': 'a = 2\nb = 3'}, 'id': 'p2'},
               {'k': 'tool', 'name': 'Bash', 'in': 'ls', 'id': 'p3'},
               {'k': 'user', 'text': '下一轮', 'id': 'p4'},
               {'k': 'tool', 'name': 'Write', 'path': PROJ + '/b.py', 'in': 'zzz', 'id': 'p5'}]
    PV.CONVS[c.sid] = c
    txt = CB.review_text(c.sid, {'uid': 'p1'})
    check('只审阅，不要修改任何文件' in txt and PROJ + '/a.py' in txt and 'b = 3' in txt and 'b.py' not in txt,
          '手动复核：任务＝这一轮改动的文件清单和差异＋只审阅不改文件')
    try:
        CB.review_text(c.sid, {'uid': 'nope'})
        check(False, '找不到这一轮要报错')
    except ValueError:
        check(True, '找不到这一轮要报错')
    check(CB.REVIEW_MODE == {'claude': 'manual mode', 'codex': 'read-only', 'agy': 'default'}, '手动复核用只读或每步确认档')
    PV.CONVS.pop(c.sid, None)


def t_reconcile():
    reset()
    d = os.path.join(CB.data_dir(), 'tasks')
    os.makedirs(d, exist_ok=True)
    t = {'id': 'task_rc', 'parent': 'p', 'parentProv': 'claude', 'child': 'cx', 'childProv': 'codex', 'title': 'r', 'task': 'x',
         'runtimeMode': 'approval-required', 'origin': 'model', 'createdAt': CB.now_ms(), 'status': 'running', 'x3': 'running', 'delivery': 'none'}
    json.dump(t, open(os.path.join(d, 'task_rc.json'), 'w'))
    CB.load_tasks()
    S.MARKS['interrupted']['cx'] = {'why': 'shutdown'}
    os.environ['CWB_COLLAB_RECONCILE'] = '0.6'
    th = threading.Thread(target=CB.reconcile)
    th.start()
    time.sleep(0.2)
    fresh = dict(t, id='task_new', child='cy', createdAt=CB.now_ms())  # 服务刚起、核对还在等时新建的任务
    CB.TASKS['task_new'] = fresh
    th.join(5)
    os.environ.pop('CWB_COLLAB_RECONCILE', None)
    tt = CB.TASKS['task_rc']
    check(tt['status'] == 'interrupted' and tt['x3'] == 'stopped', '服务重启：进行中的子任务（子对话被中断）记 stopped')
    check(fresh.get('reason') != 'restart', '核对只管启动时读到的任务，服务起来后新建的不动')


def t_redline_and_hooks():
    bad = []
    for f in ('collab.py', 'hook_wait.py', 'hook.py', 'static/ux-w.js', 'tests/e2e_r17_M.py', 'tests/fakecodex.py'):
        p = os.path.join(ROOT, f)
        if not os.path.exists(p):
            continue
        for i, line in enumerate(open(p, encoding='utf-8'), 1):
            if 'rateLimitResetCredit' in line and 'consume' in line and not line.lstrip().startswith(('#', '//', '"""', '-', '*')):
                bad.append(f'{f}:{i}')
    check(not bad, f'红线：不调用 account/rateLimitResetCredit/consume（{bad}）')
    src = open(os.path.join(ROOT, 'collab.py'), encoding='utf-8').read()
    check("'-p'" not in src and 'stream-json' not in src and '--output-format' not in src and 'claude_agent_sdk' not in src,
          'collab.py 没有 -p、stream-json、--output-format、SDK')
    rm = open(os.path.join(ROOT, 'remote.py'), encoding='utf-8').read()
    check("path == '/mcp' or path.startswith('/mcp/')" in rm, 'remote.py：/mcp 按 /hook/ 同样规则拒绝远程')
    for f in ('server.py', 'providers.py', 'prov_codex.py', 'hook.py', 'remote.py', 'static/index.html'):
        n = open(os.path.join(ROOT, f), encoding='utf-8').read().count('第十七轮 M')
        check(n >= 1, f'{f} 的挂接点都注释“第十七轮 M”（{n} 处）')
    srv = open(os.path.join(ROOT, 'server.py'), encoding='utf-8').read()
    check(srv.index('import providers as PV; PV.setup(globals())') < srv.index('import collab as CB; CB.setup(globals())'),
          'server.py：协作挂接行在第十六轮 P 那行之后（X5）')
    pt = open(os.path.join(ROOT, 'ptyd.py'), encoding='utf-8').read() == open(os.path.join(BASE, 'ptyd.py'), encoding='utf-8').read()
    check(pt, 'ptyd.py 一个字没改')
    us = open(os.path.join(ROOT, 'usage.py'), encoding='utf-8').read() == open(os.path.join(BASE, 'usage.py'), encoding='utf-8').read()
    check(us, 'usage.py 没改（X5）')


def main():
    for f in (t_args_identical, t_claude_args_file, t_mcp_http, t_delegate_rules, t_status_x3, t_wake, t_hook_wait, t_codex_hooks, t_auto_allow, t_agy_full_access, t_review_fixes,
              t_sys_note_and_wrappers, t_review_text, t_reconcile, t_redline_and_hooks):
        try:
            f()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(False, f'{f.__name__} 异常：{e!r}')
    shutil.rmtree(TMP, ignore_errors=True)
    print('\n全部通过' if not FAIL else f'\n失败 {len(FAIL)} 项：' + '；'.join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
