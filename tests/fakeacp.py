"""第十六轮 P：测试用假 ACP agent（stdio，Agent Client Protocol v1 的最小实现），用来测工作台的 ACP 客户端。

用法：python3 -I tests/fakeacp.py <状态目录> [--noresume]
- 会话存在 <状态目录>/sessions.json（进程重启后 session/load 能回放历史、session/resume 能接上）
- 每条收到的消息记一行到 <状态目录>/calls.jsonl
- --noresume（或环境变量 FAKEACP_NORESUME=1）：不声明 sessionCapabilities.resume（工作台改走 session/load）
- <状态目录>/slow_open 文件存在时，session/new、load、resume 先等文件里写的秒数再回（复核修复 1：测“正在连接”时点停止）
- session/prompt 按提示里的关键字：
    PERM     先发 tool_call（execute），再 session/request_permission（allow_once / allow_always / reject_once），
             允许就把工具标完成并回“命令完成”，拒绝回“好的，不做了”，cancelled 就结束这一轮（stopReason=cancelled）
    SLOW     每 0.2 秒发一段正文，直到收到 session/cancel（stopReason=cancelled），最多 30 秒
    THINK    先发 agent_thought_chunk 再回复
    EDIT     发一个 kind=edit、带 diff 的工具调用（完成）
    PLAN     发 plan
    FAIL     回 JSON-RPC 错误
    AUTH     回 -32000（没登录）
    其他     回“收到：<前 20 字>”，并发 usage_update；第一轮发 session_info_update 标题
"""
import json
import os
import sys
import threading
import time
import uuid

STATE = sys.argv[1] if len(sys.argv) > 1 else '/tmp/cwb-p-acp'
os.makedirs(STATE, exist_ok=True)
SPATH = os.path.join(STATE, 'sessions.json')
CALLS = os.path.join(STATE, 'calls.jsonl')
WL = threading.Lock()
PEND = {}
CANCEL = {}
SEQ = [1000]
CONFIG = lambda cur: [
    {'id': 'model', 'name': 'Model', 'category': 'model', 'type': 'select', 'currentValue': cur.get('model', 'fake-a'),
     'options': [{'value': 'fake-a', 'name': 'Fake A'}, {'value': 'fake-b', 'name': 'Fake B'}]},
    {'id': 'thought_level', 'name': 'Thinking Level', 'category': 'thought_level', 'type': 'select', 'currentValue': cur.get('thought_level', 'high'),
     'options': [{'value': 'low', 'name': 'Low'}, {'value': 'high', 'name': 'High'}]},
    {'id': 'mode', 'name': 'Mode', 'category': 'mode', 'type': 'select', 'currentValue': cur.get('mode', 'default'),
     'options': [{'value': 'default', 'name': 'Default', 'description': 'Ask before edits'}, {'value': 'auto', 'name': 'Auto', 'description': 'Approve safe actions'}]},
]
MODES = lambda cur: {'currentModeId': cur.get('mode', 'default'), 'availableModes': [
    {'id': 'default', 'name': 'Default', 'description': 'Ask before edits'}, {'id': 'auto', 'name': 'Auto', 'description': 'Approve safe actions'}]}


def load():
    try:
        return json.load(open(SPATH))
    except Exception:
        return {}


SESS = load()


def save():
    with open(SPATH + '.tmp', 'w') as f:
        json.dump(SESS, f, ensure_ascii=False)
    os.replace(SPATH + '.tmp', SPATH)


def send(o):
    with WL:
        sys.stdout.write(json.dumps(o, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def note(o):
    with WL:
        with open(CALLS, 'a') as f:
            f.write(json.dumps(dict(o, _t=time.time()), ensure_ascii=False) + '\n')


def upd(sid, u, keep=True):
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': u}})
    if keep and u.get('sessionUpdate') in ('agent_message_chunk', 'user_message_chunk'):
        SESS[sid].setdefault('hist', []).append(u)


def request(method, params):
    SEQ[0] += 1
    rid = SEQ[0]
    ev = threading.Event()
    PEND[rid] = {'ev': ev, 'msg': None}
    send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
    ev.wait(600)
    return (PEND.pop(rid, {}).get('msg') or {}).get('result')


def say(sid, text):
    for i in range(0, len(text), 6):
        upd(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text[i:i + 6]}, 'messageId': 'm1'})
        time.sleep(0.02)


def prompt(rid, p):
    sid = p.get('sessionId')
    if sid not in SESS:
        return send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32002, 'message': 'session not found'}})
    text = ' '.join(b.get('text', '') for b in p.get('prompt') or [] if b.get('type') == 'text')
    imgs = sum(1 for b in p.get('prompt') or [] if b.get('type') == 'image')
    SESS[sid].setdefault('hist', []).append({'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': text}})
    CANCEL[sid] = False
    first = SESS[sid].get('turns', 0) == 0
    SESS[sid]['turns'] = SESS[sid].get('turns', 0) + 1
    time.sleep(0.2)
    stop = 'end_turn'
    if 'AUTH' in text or auth_fail('auth_fail_prompt'):
        return send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32000, 'message': 'Authentication required'}})
    if 'FAIL' in text:
        return send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32603, 'message': 'fake internal error'}})
    if 'THINK' in text:
        for piece in ['先想', '一想。']:
            upd(sid, {'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': piece}})
            time.sleep(0.05)
    if 'PLAN' in text:
        upd(sid, {'sessionUpdate': 'plan', 'entries': [{'content': '第一步', 'priority': 'high', 'status': 'completed'},
                                                        {'content': '第二步', 'priority': 'medium', 'status': 'in_progress'}]})
    if 'EDIT' in text:
        tc = 'tc_' + uuid.uuid4().hex[:6]
        upd(sid, {'sessionUpdate': 'tool_call', 'toolCallId': tc, 'title': 'Edit a.txt', 'kind': 'edit', 'status': 'in_progress',
                  'locations': [{'path': '/tmp/a.txt'}]})
        upd(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': tc, 'status': 'completed',
                  'content': [{'type': 'diff', 'path': '/tmp/a.txt', 'oldText': 'one\ntwo\n', 'newText': 'one\n2\nthree\n'}]})
    if 'PERM' in text:
        tc = 'tc_' + uuid.uuid4().hex[:6]
        upd(sid, {'sessionUpdate': 'tool_call', 'toolCallId': tc, 'title': 'Run touch x', 'kind': 'execute', 'status': 'pending',
                  'rawInput': {'command': 'touch x'}})
        res = request('session/request_permission', {'sessionId': sid, 'toolCall': {'toolCallId': tc, 'title': 'Run touch x', 'kind': 'execute'},
                                                     'options': [{'optionId': 'o1', 'name': 'Allow once', 'kind': 'allow_once'},
                                                                 {'optionId': 'o2', 'name': 'Always allow', 'kind': 'allow_always'},
                                                                 {'optionId': 'o3', 'name': 'Reject', 'kind': 'reject_once'}]}) or {}
        oc = res.get('outcome') or {}
        note({'perm_outcome': oc})
        if oc.get('outcome') == 'cancelled' or CANCEL.get(sid):
            upd(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': tc, 'status': 'failed'})
            save()
            return send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'cancelled'}})
        if oc.get('optionId') in ('o1', 'o2'):
            upd(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': tc, 'status': 'completed',
                      'content': [{'type': 'content', 'content': {'type': 'text', 'text': 'ok\n'}}]})
            say(sid, '命令完成')
        else:
            upd(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': tc, 'status': 'failed'})
            say(sid, '好的，不做了')
    elif 'SLOW' in text:
        t0 = time.time()
        i = 0
        while time.time() - t0 < 30:
            if CANCEL.get(sid):
                stop = 'cancelled'
                break
            upd(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '第%d段。' % i}, 'messageId': 'm1'})
            i += 1
            time.sleep(0.2)
    elif 'EDIT' not in text:
        say(sid, '收到：' + text[:20] + (f'（{imgs} 张图）' if imgs else ''))
    upd(sid, {'sessionUpdate': 'usage_update', 'used': 1500 * SESS[sid]['turns'], 'size': 1048576}, keep=False)
    if first:
        upd(sid, {'sessionUpdate': 'session_info_update', 'title': '假标题 ' + text[:8]}, keep=False)
    save()
    send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': stop}})


def slow_open():
    try:
        time.sleep(float(open(os.path.join(STATE, 'slow_open')).read().strip() or 0))
    except Exception:
        pass


def auth_fail(name):
    """11-23：<状态目录>/<name> 文件里写 N：接下来 N 次回 -32000（没登录），每次减一。
    auth_fail 管 session/new、load、resume；auth_fail_prompt 管 session/prompt。测工作台等网络后自己重试。"""
    p = os.path.join(STATE, name)
    try:
        n = int(open(p).read().strip() or 0)
    except Exception:
        return False
    if n <= 0:
        return False
    with open(p, 'w') as f:
        f.write(str(n - 1))
    return True


def handle(m):
    note(m)
    if 'id' in m and 'method' not in m:
        w = PEND.get(m['id'])
        if w:
            w['msg'] = m
            w['ev'].set()
        return
    meth, p, rid = m.get('method'), m.get('params') or {}, m.get('id')
    if meth == 'initialize':
        sc = {'load': {}, 'close': {}}
        if os.environ.get('FAKEACP_NORESUME') != '1' and '--noresume' not in sys.argv[2:]:
            sc['resume'] = {}
        return send({'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': 1, 'agentCapabilities': {
            'loadSession': True, 'promptCapabilities': {'image': True, 'embeddedContext': False}, 'sessionCapabilities': sc},
            'agentInfo': {'name': 'fake-acp', 'version': '0.1'}, 'authMethods': []}})
    if meth in ('session/new', 'session/load', 'session/resume') and auth_fail('auth_fail'):
        return send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32000, 'message': 'Authentication required'}})
    if meth == 'session/new':
        slow_open()
        sid = 'fs-' + uuid.uuid4().hex[:10]
        SESS[sid] = {'cwd': p.get('cwd'), 'cur': {}, 'hist': []}
        save()
        send({'jsonrpc': '2.0', 'id': rid, 'result': {'sessionId': sid, 'modes': MODES({}), 'configOptions': CONFIG({})}})
        upd(sid, {'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'compact', 'description': 'Compact now'}]}, keep=False)
        return
    if meth in ('session/load', 'session/resume'):
        slow_open()
        sid = p.get('sessionId')
        if sid not in SESS:
            return send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32002, 'message': 'session not found'}})
        if meth == 'session/load':
            for u in SESS[sid].get('hist', []):
                upd(sid, u, keep=False)
        cur = SESS[sid].get('cur', {})
        return send({'jsonrpc': '2.0', 'id': rid, 'result': {'modes': MODES(cur), 'configOptions': CONFIG(cur)}})
    if meth == 'session/set_config_option':
        sid = p.get('sessionId')
        SESS[sid].setdefault('cur', {})[p.get('configId')] = p.get('value')
        save()
        return send({'jsonrpc': '2.0', 'id': rid, 'result': {'configOptions': CONFIG(SESS[sid]['cur'])}})
    if meth == 'session/set_mode':
        sid = p.get('sessionId')
        SESS[sid].setdefault('cur', {})['mode'] = p.get('modeId')
        save()
        return send({'jsonrpc': '2.0', 'id': rid, 'result': {}})
    if meth == 'session/close':
        return send({'jsonrpc': '2.0', 'id': rid, 'result': {}})
    if meth == 'session/prompt':
        threading.Thread(target=prompt, args=(rid, p), daemon=True).start()
        return
    if meth == 'session/cancel':
        CANCEL[p.get('sessionId')] = True
        return
    if rid is not None:
        send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'method not found: ' + str(meth)}})


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        m = json.loads(line)
    except Exception:
        continue
    handle(m)
