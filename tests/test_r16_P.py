"""第十六轮 P 单元测试：Codex、Antigravity 对话的记录、事件映射、授权映射、左栏合并、接口接管、额度快照、重启脚本等空闲。
不启动 claude、codex、agy；数据目录、配置、未读标记都放临时目录。
运行：.venv/bin/python -I tests/test_r16_P.py
"""
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp(prefix='cwb-r16p-')
os.environ['CWB_DATA_DIR'] = os.path.join(TMP, 'data')
os.environ['CWB_PORT'] = '47959'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as S  # noqa: E402
import providers as PV  # noqa: E402
import prov_codex as PC  # noqa: E402
import prov_acp as PA  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


S.CONF_PATH = os.path.join(TMP, 'config.json')
S.MARKS_PATH = os.path.join(TMP, 'marks.json')
S.MARKS.update({'done_at': {}, 'interrupted': {}})
EVENTS = []
_emit = S.BUS.emit
S.BUS.emit = lambda ev: (EVENTS.append(ev), _emit(ev))
PROJ = os.path.join(TMP, 'proj')
os.makedirs(PROJ)


def conv(prov='codex', cwd=PROJ):
    import uuid
    c = PV.PConv(str(uuid.uuid4()), prov, cwd)
    PV.CONVS[c.sid] = c
    return c


def rows(c):
    return [json.loads(l) for l in open(c.path, encoding='utf-8')]


def t_redline():
    """红线：协议里“消耗额度重置券”的方法绝不能调用（注释里可以写明禁止，代码行里不能出现）。"""
    bad = []
    for f in ('providers.py', 'prov_codex.py', 'prov_acp.py', 'static/ux-v.js', 'tests/fakecodex.py', 'tests/e2e_r16_P.py'):
        for i, line in enumerate(open(os.path.join(ROOT, f), encoding='utf-8'), 1):
            s = line.strip()
            if s.startswith('#') or s.startswith('//') or s.startswith('*'):
                continue
            if re.search(r'rateLimitResetCredit/consume|rateLimitResetCredit\s*/\s*consume|ResetCredit.{0,3}consume', line):
                bad.append(f'{f}:{i}')
    check(not bad, f'红线：代码里没有调用 account/rateLimitResetCredit/consume（{bad}）')
    src = open(os.path.join(ROOT, 'prov_codex.py'), encoding='utf-8').read()
    calls = set(re.findall(r"request\('([^']+)'", src))
    check(calls <= {'initialize', 'thread/start', 'thread/resume', 'thread/unsubscribe', 'turn/start', 'turn/steer', 'turn/interrupt',
                    'model/list', 'config/read', 'account/read', 'account/rateLimits/read'}, f'Codex 只调用这些方法：{sorted(calls)}')


def t_record():
    c = conv()
    c.begin_turn('你好', [], 'm1', 'high')
    c.first_output()
    c.add({'k': 'thought', 'secs': 2, 'text': '想'})
    tid = c.add({'k': 'tool', 'name': 'Bash', 'in': 'ls', 'done': False, 'out': '', 'error': False})
    c.patch(tid, out='a\nb', done=True)
    c.add({'k': 'tool', 'name': 'Bash', 'in': 'sleep 9', 'done': False, 'out': '', 'error': False})
    c.turn['usage'].update(input=100, output=7, cachedInput=40)
    c.turn['ctx'] = {'used': 107, 'window': 1000}
    c.end_turn('done', stop='completed')
    r = rows(c)
    check(r[0]['t'] == 'meta' and r[0]['prov'] == 'codex' and [x['t'] for x in r].count('turn_start') == 1, '记录：第一行是对话信息，有 turn_start')
    te = r[-1]
    check(te['t'] == 'turn_end' and te['status'] == 'completed' and te['usage'] == {'input': 100, 'output': 7, 'cachedInput': 40}
          and te['usageSrc'] == 'protocol' and te['ctx'] == {'used': 107, 'size': 1000} and te['model'] == 'm1' and te['effort'] == 'high',
          f'turn_end 的用量字段：{te}')
    check(all(isinstance(te[k], int) and te[k] > 1e12 for k in ('ts', 'startedAt', 'firstTokenAt', 'endedAt')), 'turn_end 的时间是毫秒整数')
    check('error' not in te and 'cost' not in te and 'why' not in te, '拿不到的字段不写')
    c2 = PV.PConv(c.sid, '', '')
    c2.replay()
    check([i['k'] for i in c2.items] == ['user', 'thought', 'tool', 'tool'] and c2.items[2]['out'] == 'a\nb' and c2.items[3]['noresult'] == '没有返回结果'
          and c2.items[0]['stat'] == {'secs': 0, 'out': 7, 'ctx': 107}, '重放记录：条目、补丁、这一轮的统计都还原')
    check(c2.prov == 'codex' and c2.cwd == PROJ and c2.n == 4, '重放记录：提供方、文件夹、条目编号')
    check(S.MARKS['done_at'].get(c.sid), '一轮结束记未读（done_at）')
    # 出错
    c.begin_turn('再来', [])
    c.end_turn('error', error_type='rateLimitExceeded', error_msg='slow down')
    te = rows(c)[-1]
    check(te['status'] == 'failed' and te['error'] == {'kind': 'rateLimitExceeded', 'text': 'slow down'} and te['usageSrc'] is None and 'usage' not in te,
          f'出错的一轮：{te}')


def t_scan():
    c = conv('agy')
    c.begin_turn('长任务', [])
    sid = c.sid
    PV.CONVS.pop(sid)
    S.MARKS['interrupted'].pop(sid, None)
    PV.scan()
    c2 = PV.CONVS.get(sid)
    te = [r for r in rows(c2) if r['t'] == 'turn_end'][-1]
    check(c2 and not c2.busy and te['t'] == 'turn_end' and te['status'] == 'interrupted' and te['why'] == 'shutdown' and te.get('endedAtEstimated'),
          f'启动时补记：最后一轮没结束的记成中断（{te.get("why")}）')
    check(sid in S.MARKS['interrupted'], '启动时补记：“上次回复被中断”标记')
    check([i.get('text') for i in c2.items if i.get('k') == 'note'] == ['已中断'], 'server 重启导致的中断：时间线里这一轮标“已中断”（不显示成“已完成”）')


def t_projects():
    keep = dict(PV.CONVS)
    PV.CONVS.clear()  # 只看本用例建的对话
    S.save_conf({'folders': [], 'archived': [], 'pinned': [], 'names': {}})
    real = os.path.realpath(PROJ)
    link = os.path.join(TMP, 'link')
    os.symlink(PROJ, link)
    c = conv('codex', link)
    c.add({'k': 'user', 'text': '左栏'})
    other = os.path.join(TMP, 'other')
    os.makedirs(other)
    c2 = conv('agy', other)
    c2.add({'k': 'user', 'text': '另一组'})
    c3 = conv('agy', other)  # 没发过消息的空对话不进左栏
    S.save_conf({'folders': [], 'archived': [c2.sid], 'pinned': [c.sid], 'names': {c.sid: '改过的名字'}})
    res = [{'cwd': real, 'name': 'proj', 'hidden': False, 'mtime': 1, 'sessions': [{'sid': 'x' * 36, 'title': 'claude', 'mtime': 1}]}]
    PV.merge_projects(res)
    g = next(p for p in res if p['cwd'] == real)
    s = next((x for x in g['sessions'] if x['sid'] == c.sid), None)
    check(s and s['prov'] == 'codex' and s['title'] == '改过的名字' and s['pinned'] == 1 and len(g['sessions']) == 2,
          '左栏：经符号链接的同一文件夹并进 Claude 的组，带提供方、改名、置顶')
    g2 = next((p for p in res if p['cwd'] == other), None)
    check(g2 and [x['sid'] for x in g2['sessions']] == [c2.sid] and g2['sessions'][0]['archived'], '左栏：没有 Claude 对话的文件夹新建一组；空对话不列；归档标记')
    check(res[0]['mtime'] >= res[-1]['mtime'], '左栏：分组按时间重新排序')
    PV.CONVS.clear()
    PV.CONVS.update(keep)


def t_wrappers():
    c = conv()
    tc = S.API['turn-changes']({'sid': c.sid})
    check(tc.get('error') and 'turns' not in tc, 'turn-changes 对这两家对话回 error（审阅只剩“未提交”，不出改动胶囊）')
    check(S.API['btw-list']({'sid': c.sid}) == {'items': []} and S.API['agents']({'sid': c.sid}) == {'agents': [], 'shells': []}, '旁问列表、子代理列表给空结果')
    try:
        S.API['fork-at']({'sid': c.sid, 'uuid': 'x'})
        ok = False
    except ValueError as e:
        ok = 'Codex' in str(e)
    check(ok, '分叉等不支持的操作报中文错误')
    check(S.API['prewarm']({'cwd': PROJ, 'prov': 'codex'}) == {}, '首页选了 Codex 时不预热 Claude')
    d = S.API['open']({'sid': c.sid})
    check(d['prov'] == 'codex' and d['runner']['prov'] == 'codex' and d['items'] == [] and d['hidden'] == 0, 'open 接管：返回提供方和运行状态')
    r = S.API['control']({'sid': c.sid, 'model': 'gpt-x', 'mode': 'untrusted', 'effort': 'high'})
    check(r['opts'] == {'model': 'gpt-x', 'mode': 'untrusted', 'effort': 'high'} and c.opts()['mode'] == 'untrusted', 'control：存进对话选项')
    r = S.API['control']({'sid': c.sid, 'mode': 'default', 'model': ''})
    check(r['opts'] == {'effort': 'high'}, 'Codex 选“默认”＝不覆盖')
    c.busy = True
    check(S.API['prov-busy']({})['busy'] is True and S.API['prov-busy']({})['convs'][0]['sid'] == c.sid, 'prov-busy：有对话在回复就算忙')
    c.busy = False
    check(S.API['prov-busy']({})['busy'] is False, 'prov-busy：都空闲')
    r = S.API['stop']({'sid': c.sid})
    check(c.sid not in PV.CONVS, 'stop：还没发过消息的空对话直接丢掉')
    check(S.API['projects'] is not None and 'prov-new' in S.API and 'prov-status' in S.API and 'prov-models' in S.API, '新接口已登记')


class FakeRpc:
    def __init__(self):
        self.sent, self.reqs = [], []
        self.pid, self.started = 1, time.time()

    def respond(self, rid, result=None, error=None):
        self.sent.append((rid, result, error))

    def request(self, method, params, timeout=60):
        self.reqs.append((method, params))
        return {}

    def notify(self, method, params=None):
        self.reqs.append((method, params))

    def alive(self):
        return True


def t_codex_events():
    a = PC.CodexAdapter()
    c = conv()
    c.meta['native'] = 'th1'
    a.threads['th1'] = c.sid
    c.attached = True
    c.pv = {}
    c.begin_turn('做事', [], 'gpt', '')
    n = lambda m, p: a.on_notify(m, dict(p, threadId='th1', turnId='tu1'))
    n('turn/started', {'turn': {'id': 'tu1'}})
    n('item/started', {'item': {'type': 'reasoning', 'id': 'r1', 'startedAtMs': 1000}})
    n('item/reasoning/summaryTextDelta', {'itemId': 'r1', 'delta': '先看', 'summaryIndex': 0})
    n('item/completed', {'item': {'type': 'reasoning', 'id': 'r1', 'summary': ['先看看文件'], 'startedAtMs': 1000, 'completedAtMs': 4000}})
    n('item/started', {'item': {'type': 'agentMessage', 'id': 'm1', 'text': ''}})
    n('item/agentMessage/delta', {'itemId': 'm1', 'delta': '好的，'})
    check(c.live == '好的，' and c.turn['first'], '正文增量：实时预览、首字时间')
    n('item/completed', {'item': {'type': 'agentMessage', 'id': 'm1', 'text': '好的，我看看。'}})
    n('item/started', {'item': {'type': 'commandExecution', 'id': 'c1', 'command': "/bin/zsh -lc 'cat a.py'", 'cwd': PROJ, 'status': 'inProgress',
                                'commandActions': [{'type': 'read', 'command': 'cat a.py', 'name': 'a.py', 'path': PROJ + '/a.py'}]}})
    n('item/started', {'item': {'type': 'commandExecution', 'id': 'c2', 'command': "/bin/zsh -lc 'make'", 'cwd': PROJ, 'status': 'inProgress', 'commandActions': [{'type': 'unknown', 'command': 'make'}]}})
    n('item/commandExecution/outputDelta', {'itemId': 'c2', 'delta': 'building\n'})
    a.tick()
    n('item/completed', {'item': {'type': 'commandExecution', 'id': 'c2', 'command': 'make', 'status': 'failed', 'exitCode': 2, 'aggregatedOutput': 'err\n', 'commandActions': []}})
    n('item/completed', {'item': {'type': 'commandExecution', 'id': 'c1', 'command': 'cat a.py', 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'x=1\n', 'commandActions': []}})
    diff = '@@ -1,2 +1,2 @@\n a\n-b\n+B\n'
    n('item/started', {'item': {'type': 'fileChange', 'id': 'f1', 'status': 'inProgress', 'changes': [
        {'path': PROJ + '/a.py', 'kind': {'type': 'update', 'move_path': None}, 'diff': diff},
        {'path': PROJ + '/n.py', 'kind': {'type': 'add'}, 'diff': 'print(1)\n'}]}})
    n('item/completed', {'item': {'type': 'fileChange', 'id': 'f1', 'status': 'completed', 'changes': []}})
    n('turn/plan/updated', {'plan': [{'step': '读', 'status': 'completed'}, {'step': '改', 'status': 'inProgress'}]})
    n('turn/plan/updated', {'plan': [{'step': '读', 'status': 'completed'}, {'step': '改', 'status': 'completed'}]})
    for _ in range(2):
        n('thread/tokenUsage/updated', {'tokenUsage': {'last': {'inputTokens': 100, 'cachedInputTokens': 60, 'outputTokens': 10, 'reasoningOutputTokens': 3,
                                                                'cacheWriteInputTokens': 0, 'totalTokens': 110}, 'total': {}, 'modelContextWindow': 2000}})
    n('error', {'error': {'message': 'Rate limit reached', 'codexErrorInfo': 'rateLimitExceeded'}, 'willRetry': False})
    n('turn/completed', {'turn': {'id': 'tu1', 'status': 'failed', 'error': {'message': 'Rate limit reached'}}})
    ks = [(i['k'], i.get('name')) for i in c.items]
    check(ks == [('user', None), ('thought', None), ('text', None), ('tool', 'Read'), ('tool', 'Bash'), ('tool', 'Edit'), ('tool', 'Write'),
                 ('tool', 'TodoWrite'), ('error', None)], f'Codex 事件 → 时间线条目：{ks}')
    th = c.items[1]
    check(th['secs'] == 3 and th['text'] == '先看看文件', '思考摘要和用时')
    rd, mk = c.items[3], c.items[4]
    check(rd['path'] == PROJ + '/a.py' and rd['done'] and rd['out'] == 'x=1', '只读命令显示成读文件')
    check(mk['in'] == 'make' and mk['error'] and '退出码 2' in mk['out'], '命令失败：标错误、带退出码')
    ed, wr = c.items[5], c.items[6]
    check(ed['diff'] == {'old': 'a\nb', 'new': 'a\nB'} and ed['ln'] == [1, 1] and ed['done'] and wr['in'] == 'print(1)\n' and wr['ln'] == [1, 0],
          '改文件：差异（统一格式转旧新两段）、增删行数；新文件显示内容')
    check(c.items[7]['todos'] == [{'c': '读', 's': 'completed'}, {'c': '改', 's': 'completed'}], '计划更新：同一个任务清单条目原地更新')
    check(c.items[8]['etype'] == '接口限流', '错误归类成中文')
    te = rows(c)[-1]
    check(te['status'] == 'failed' and te['error']['kind'] == 'rateLimitExceeded' and te['usage'] == {'input': 200, 'cachedInput': 120, 'output': 20,
          'reasoning': 6, 'cacheWrite': 0} and te['ctx'] == {'used': 110, 'size': 2000}, f'一轮的用量＝各次 last 相加：{te.get("usage")}')
    check(not c.busy and c.turn is None, '一轮结束，空闲')
    check(PC.unwrap_cmd("/bin/zsh -lc 'echo '\\''hi'\\'''") == "echo 'hi'" and PC.unwrap_cmd('ls -la') == 'ls -la', '命令去掉登录 shell 外壳')


def wait_perm(sid, t=3):
    end = time.time() + t
    while time.time() < end:
        for rid, e in list(S.PERMS.items()):
            if e['req'].get('sid') == sid:
                return rid, e
        time.sleep(0.02)
    return None, None


def t_codex_approval():
    a = PC.CodexAdapter()
    c = conv()
    c.meta['native'] = 'th2'
    a.threads['th2'] = c.sid
    c.pv = {'turn': 'tu2'}
    c.busy = True
    r = FakeRpc()
    a.rpc = r
    cases = [
        (['accept', 'acceptForSession', 'decline', 'cancel'], 'always', None, 'acceptForSession'),
        (['accept', {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': ['touch', 'x']}}, 'cancel'], 'always', None,
         {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': ['touch', 'x']}}),
        (['accept', {'acceptWithExecpolicyAmendment': {}}, 'cancel'], 'deny', None, 'cancel'),
        (['accept', 'decline', 'cancel'], 'deny', None, 'decline'),
        (['accept', 'decline'], 'allow', None, 'accept'),
    ]
    for i, (avail, dec, msg, want) in enumerate(cases):
        a.on_request(100 + i, 'item/commandExecution/requestApproval', {'threadId': 'th2', 'turnId': 'tu2', 'itemId': 'c', 'command': "/bin/zsh -lc 'touch x'",
                                                                       'availableDecisions': avail, 'proposedExecpolicyAmendment': ['touch', 'x']})
        rid, e = wait_perm(c.sid)
        if i == 0:
            check(e and e['req']['tool'] == 'Bash' and e['req']['input']['command'] == 'touch x' and e['req']['pvAlways'] == '本对话内都允许', '命令授权卡：命令去壳，“总是允许”换成本对话内')
        S.decide(rid, {'decision': dec})
        end = time.time() + 3
        while time.time() < end and not any(x[0] == 100 + i for x in r.sent):
            time.sleep(0.02)
        got = next((x[1] for x in r.sent if x[0] == 100 + i), None)
        check(got == {'decision': want}, f'授权决定映射：{dec} + {avail} → {got}')
    # 拒绝并说明：有 decline 时说明用 turn/steer 插进这一轮
    a.on_request(200, 'item/commandExecution/requestApproval', {'threadId': 'th2', 'turnId': 'tu2', 'itemId': 'c', 'command': 'rm -rf x',
                                                               'availableDecisions': ['accept', 'decline', 'cancel']})
    rid, e = wait_perm(c.sid)
    S.decide(rid, {'decision': 'deny', 'allow': False, 'message': '别删'})
    end = time.time() + 3
    while time.time() < end and not any(m == 'turn/steer' for m, _ in r.reqs):
        time.sleep(0.02)
    st = [p for m, p in r.reqs if m == 'turn/steer']
    check(st and st[-1]['expectedTurnId'] == 'tu2' and '别删' in st[-1]['input'][0]['text'] and any(i.get('mid') and '别删' in i['text'] for i in c.items),
          '拒绝并说明：decline 后用 turn/steer 把说明插进这一轮')
    # 中断时作废：回 cancel
    a.on_request(300, 'item/fileChange/requestApproval', {'threadId': 'th2', 'turnId': 'tu2', 'itemId': 'f'})
    rid, e = wait_perm(c.sid)
    check(e and e['req']['tool'] == 'Edit' and e['req']['pvAlways'], '改文件授权卡')
    PV.drop_perms(c.sid)
    end = time.time() + 3
    while time.time() < end and not any(x[0] == 300 for x in r.sent):
        time.sleep(0.02)
    check(next((x[1] for x in r.sent if x[0] == 300), None) == {'decision': 'cancel'}, '授权作废（中断）时回 cancel')
    a.on_request(400, 'item/tool/requestUserInput', {'threadId': 'th2'})
    check(any(x[0] == 400 and x[2] for x in r.sent), '不支持的请求回错误，不卡住对方')
    a.rpc = None


def t_codex_quota():
    a = PC.CodexAdapter()
    res = {'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 37, 'windowDurationMins': 10080, 'resetsAt': 1795900000},
                          'secondary': None, 'credits': {'hasCredits': True, 'unlimited': False, 'balance': '12'}, 'planType': 'pro'},
           'rateLimitResetCredits': {'availableCount': 2}}
    doc = a.quota_doc(res)
    check(doc['v'] == 1 and doc['provider'] == 'codex' and doc['src'] == 'protocol' and doc['plan'] == 'pro' and doc['resetCredits'] == {'availableCount': 2}
          and doc['windows'] == [{'id': 'primary', 'label': '每周', 'kind': 'week', 'usedPercent': 37, 'windowMins': 10080, 'resetsAt': 1795900000000}],
          f'额度快照结构（第 7 节）：{doc["windows"]}')
    a.save_quota_doc(doc)
    p = os.path.join(PV.DATA_DIR, 'quota-codex.json')
    a.save_quota_doc({'v': 1, 'provider': 'codex', 'ts': 1, 'src': 'protocol', 'windows': [], 'unavailable': {'reason': 'probeFailed', 'text': 'x'}})
    d = json.load(open(p))
    check(d['unavailable']['reason'] == 'probeFailed' and d['lastGood']['windows'][0]['usedPercent'] == 37, '读取失败时保留上次的值（lastGood）')
    a.quota_raw = res
    a.on_notify('account/rateLimits/updated', {'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 40, 'windowDurationMins': 10080}}})
    d = json.load(open(p))
    check(d['windows'][0]['usedPercent'] == 40 and d.get('plan') == 'pro', '会话中的零散更新合进快照')


def t_acp():
    a = PA.AcpAdapter()
    v = a.tool_view({'kind': 'execute', 'title': 'Run ls', 'rawInput': {'command': ['ls', '-la']}, 'content': [{'type': 'content', 'content': {'type': 'text', 'text': 'out'}}]})
    check(v['name'] == 'Bash' and v['in'] == 'ls -la' and v['out'] == 'out' and v['desc'] == 'Run ls', 'ACP 工具：执行命令')
    v = a.tool_view({'kind': 'edit', 'title': 'Edit', 'content': [{'type': 'diff', 'path': '/a', 'oldText': 'x\ny\n', 'newText': 'x\nz\n'}]})
    check(v['name'] == 'Edit' and v['diff'] == {'old': 'x\ny\n', 'new': 'x\nz\n'} and v['ln'] == [1, 1] and v['path'] == '/a', 'ACP 工具：改文件差异')
    v = a.tool_view({'kind': 'edit', 'content': [{'type': 'diff', 'path': '/b', 'newText': 'new\n'}]})
    check(v['name'] == 'Write' and v['in'] == 'new\n', 'ACP 工具：新文件')
    v = a.tool_view({'kind': 'read', 'title': 'Read f', 'locations': [{'path': '/f'}]})
    check(v['name'] == 'Read' and v['path'] == '/f', 'ACP 工具：读文件')
    c = conv('agy')
    c.meta['native'] = 'ses1'
    a.sessions['ses1'] = c.sid
    c.pv = {}
    c.begin_turn('hi', [])
    u = lambda x: a.on_notify('session/update', {'sessionId': 'ses1', 'update': x})
    u({'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': '想一下'}})
    u({'sessionUpdate': 'plan', 'entries': [{'content': '一', 'priority': 'high', 'status': 'pending'}]})
    u({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '答'}, 'messageId': 'a'})
    u({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '案'}, 'messageId': 'a'})
    u({'sessionUpdate': 'tool_call', 'toolCallId': 't1', 'title': 'Run x', 'kind': 'execute', 'status': 'in_progress', 'rawInput': {'command': 'x'}})
    u({'sessionUpdate': 'tool_call_update', 'toolCallId': 't1', 'status': 'completed', 'content': [{'type': 'content', 'content': {'type': 'text', 'text': 'ok'}}]})
    u({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '完了'}, 'messageId': 'b'})
    u({'sessionUpdate': 'usage_update', 'used': 50, 'size': 100, 'cost': {'amount': 0.12, 'currency': 'USD'}})
    a.on_notify('session/update', {'sessionId': 'unknown', 'update': {'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'x'}]}})
    c.end_turn('done', stop='end_turn')
    ks = [(i['k'], i.get('name')) for i in c.items]
    check(ks == [('user', None), ('thought', None), ('tool', 'TodoWrite'), ('text', None), ('tool', 'Bash'), ('text', None)], f'ACP 更新 → 条目顺序：{ks}')
    check(c.items[3]['text'] == '答案' and c.items[4]['out'] == 'ok' and c.items[4]['done'] and c.items[5]['text'] == '完了', 'ACP：正文分段、工具结果')
    te = rows(c)[-1]
    check(te['cost'] == {'amount': 0.12, 'currency': 'USD', 'src': 'usage_update', 'scope': 'session'} and te['ctx'] == {'used': 50, 'size': 100}, f'ACP：会话累计费用、上下文：{te.get("cost")}')
    check('unknown' in a.early, '还没对上对话的早到通知先存着')
    u({'sessionUpdate': 'config_option_update', 'configOptions': [
        {'id': 'm', 'category': 'model', 'currentValue': 'fake-b', 'options': [{'value': 'fake-a'}, {'value': 'fake-b'}]},
        {'id': 't', 'category': 'thought_level', 'currentValue': 'high', 'options': [{'value': 'low'}, {'value': 'high'}]}]})
    u({'sessionUpdate': 'current_mode_update', 'currentModeId': 'auto'})
    pc = c.public_state()['pvcur']
    check(pc == {'model': 'fake-b', 'effort': 'high', 'mode': 'auto'}, f'ACP：会话当前的模型、思考强度、模式推给界面：{pc}')
    # 授权选项映射
    r = FakeRpc()
    a.rpc = r
    c.busy = True
    opts = [{'optionId': 'A', 'name': '允许一次', 'kind': 'allow_once'}, {'optionId': 'B', 'name': '总是', 'kind': 'allow_always'},
            {'optionId': 'C', 'name': '拒绝', 'kind': 'reject_once'}]
    for i, (dec, want) in enumerate([('allow', 'A'), ('always', 'B'), ('deny', 'C')]):
        a.on_request(10 + i, 'session/request_permission', {'sessionId': 'ses1', 'toolCall': {'toolCallId': 'tx', 'kind': 'execute', 'rawInput': {'command': 'rm a'}}, 'options': opts})
        rid, e = wait_perm(c.sid)
        if i == 0:
            check(e and e['req']['tool'] == 'Bash' and e['req']['input']['command'] == 'rm a', 'ACP 授权卡：命令')
        S.decide(rid, {'decision': dec})
        end = time.time() + 3
        while time.time() < end and not any(x[0] == 10 + i for x in r.sent):
            time.sleep(0.02)
        got = next((x[1] for x in r.sent if x[0] == 10 + i), None)
        check(got == {'outcome': {'outcome': 'selected', 'optionId': want}}, f'ACP 授权：{dec} → {got}')
    a.on_request(20, 'session/request_permission', {'sessionId': 'ses1', 'toolCall': {'toolCallId': 'ty'}, 'options': opts})
    rid, e = wait_perm(c.sid)
    S.decide(rid, {'decision': 'deny', 'allow': False, 'message': '换个办法'})
    end = time.time() + 3
    while time.time() < end and not any(x[0] == 20 for x in r.sent):
        time.sleep(0.02)
    check(c.queue and '换个办法' in c.queue[0][0], 'ACP 拒绝并说明：说明排在队列最前，这一轮结束后发出')
    a.on_request(21, 'fs/read_text_file', {'sessionId': 'ses1', 'path': '/etc/hosts'})
    check(any(x[0] == 21 and x[2] for x in r.sent), '没声明的能力（读文件）回“不支持”')
    a.rpc = None
    c.busy = False
    c.queue.clear()


def t_diff():
    old, new, ln = PV.diff_view('--- a\n+++ b\n@@ -1 +1 @@\n-a\n+b\n@@ -9 +9 @@\n x\n+y\n')
    check(old == 'a\n…\nx' and new == 'b\n…\nx\ny' and ln == [2, 1], f'统一格式 diff 转换：{old!r} {new!r} {ln}')
    check(PV.diff_view('just\ncontent\n')[2] == [2, 0], '不是 diff 时按新内容算行数')


def t_restart_when_idle():
    """重启脚本：--server-now 也要先等 Codex / Antigravity 空闲（用一个假的本机服务回答 prov-busy，前两次忙）。"""
    w = os.path.join(TMP, 'rwi')
    os.makedirs(os.path.join(w, '.run'))
    shutil.copy(os.path.join(ROOT, 'restart_when_idle.py'), w)
    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = b'ok 0'
            self.send_response(200)
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            calls.append(self.path)
            busy = self.path.endswith('prov-busy') and sum(1 for c in calls if c.endswith('prov-busy')) <= 2
            b = json.dumps({'busy': busy, 'tasks': []}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    open(os.path.join(w, '.run', f'token-{port}'), 'w').write('t' * 30)
    subprocess.run([sys.executable, '-I', os.path.join(w, 'restart_when_idle.py'), '--server-now'], env=dict(os.environ, CWB_PORT=str(port)))
    logp = os.path.join(w, '.run', 'restart_when_idle.log')
    end = time.time() + 30
    while time.time() < end and '立即结束旧服务' not in (open(logp).read() if os.path.exists(logp) else ''):
        time.sleep(0.2)
    log = open(logp).read() if os.path.exists(logp) else ''
    n = sum(1 for c in calls if c.endswith('prov-busy'))
    check('等 Codex / Antigravity 对话空闲' in log and '立即结束旧服务' in log and n >= 5, f'--server-now 先等空闲（问了 {n} 次 prov-busy）')
    check(log.index('等 Codex') < log.index('立即结束旧服务'), '日志顺序：先等，再结束旧服务')
    srv.shutdown()
    # 脚本自己转到后台，之后还会等新服务 90 秒；按命令行里的临时路径认出来结束掉，不留孤儿
    me = os.path.join(w, 'restart_when_idle.py')
    for l in os.popen('ps -axo pid=,command=').read().splitlines():
        q = l.strip().split(None, 1)
        if len(q) == 2 and me in q[1]:
            try:
                os.kill(int(q[0]), 15)
            except OSError:
                pass


# ---------------------------------------------------------------- 复核修复（11-22 晚，独立复核的结论）
def wait_for(cond, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return bool(cond())


class GateRpc(FakeRpc):
    """turn/start、session/prompt 可以挂住或在返回前插一个动作（模拟这时用户点了停止）。"""
    def __init__(self, on_req=None, block=()):
        FakeRpc.__init__(self)
        self.on_req, self.block, self.rel = on_req, set(block), threading.Event()

    def request(self, method, params, timeout=60):
        self.reqs.append((method, params))
        if self.on_req:
            self.on_req(method, params)
        if method in self.block:
            self.rel.wait(timeout if timeout < 30 else 30)
        if method == 'turn/start':
            return {'turn': {'id': 'tu9'}}
        return {}


def swap(prov, a):
    old = PV.ADAPTERS[prov]
    PV.ADAPTERS[prov] = a
    return old


def last_end(c):
    return [r for r in rows(c) if r.get('t') == 'turn_end'][-1]


def t_fix1_stop_while_connecting():
    """修复 1：“正在连接”时点停止不能丢；连接期间点“立即发送”也不能丢。"""
    for prov, mk_a, sent in (('codex', PC.CodexAdapter, 'turn/start'), ('agy', PA.AcpAdapter, 'session/prompt')):
        a, r, gate = mk_a(), GateRpc(), threading.Event()
        a.rpc = r
        a.start_proc = lambda r=r: r

        def ens(c, gate=gate, a=a):
            gate.wait(5)
            c.meta['native'] = 'n-' + c.sid[:6]
            (a.threads if prov == 'codex' else a.sessions)[c.meta['native']] = c.sid
            c.attached = True
        a.ensure_attached = ens
        old = swap(prov, a)
        try:
            c = conv(prov)
            PV.api_send({'sid': c.sid, 'text': '第一条'})
            wait_for(lambda: c.turn is not None)
            PV.api_interrupt({'sid': c.sid})
            gate.set()
            wait_for(lambda: not c.busy)
            te = last_end(c)
            check(not c.busy and not any(m == sent for m, _ in r.reqs) and te['status'] == 'interrupted' and te.get('why') == 'user'
                  and c.items[-1].get('text') == '已中断',
                  f'{PV.LABEL[prov]}：连接期间点停止，这一轮不发出、按“已中断”结束（{[m for m, _ in r.reqs]}，{te["status"]}）')
            # 下一条照常发出（停止标记在占住新一轮时重置）
            gate.set()
            PV.api_send({'sid': c.sid, 'text': '第二条'})
            wait_for(lambda: any(m == sent for m, _ in r.reqs))
            check(any(m == sent for m, _ in r.reqs), f'{PV.LABEL[prov]}：停止过之后下一条照常发出')
        finally:
            swap(prov, old)
    # Codex：turn/start 还没返回时点停止 → 拿到轮 ID 后马上 turn/interrupt（以前 send 会把标记清掉）
    a = PC.CodexAdapter()
    c = conv('codex')
    holder = {}
    r = GateRpc(on_req=lambda m, p: m == 'turn/start' and holder['a'].interrupt(holder['c']))
    holder.update(a=a, c=c)
    a.rpc, a.start_proc = r, (lambda: r)
    a.ensure_attached = lambda c: (c.meta.__setitem__('native', 'th-x'), a.threads.__setitem__('th-x', c.sid), setattr(c, 'attached', True))
    old = swap('codex', a)
    try:
        PV.api_send({'sid': c.sid, 'text': '慢慢来'})
        wait_for(lambda: any(m == 'turn/interrupt' for m, _ in r.reqs))
        ti = [p for m, p in r.reqs if m == 'turn/interrupt']
        check(ti and ti[0].get('turnId') == 'tu9', f'Codex：turn/start 返回前点的停止，拿到轮 ID 后发 turn/interrupt（{ti}）')
        a.on_notify('turn/completed', {'threadId': 'th-x', 'turn': {'id': 'tu9', 'status': 'interrupted'}})
        check(not c.busy and last_end(c).get('why') == 'user', 'Codex：随后按用户停止结束')
    finally:
        swap('codex', old)
    # Codex：连接期间点“立即发送”，拿到轮 ID 后用 turn/steer 插进去
    a, r, gate = PC.CodexAdapter(), GateRpc(), threading.Event()
    a.rpc, a.start_proc = r, (lambda: r)

    def ens2(c):
        gate.wait(5)
        c.meta['native'] = 'th-s'
        a.threads['th-s'] = c.sid
        c.attached = True
    a.ensure_attached = ens2
    old = swap('codex', a)
    try:
        c = conv('codex')
        PV.api_send({'sid': c.sid, 'text': '先做这个'})
        wait_for(lambda: c.turn is not None)
        PV.api_send({'sid': c.sid, 'text': '插一句', 'now': True})
        gate.set()
        wait_for(lambda: any(m == 'turn/steer' for m, _ in r.reqs))
        st = [p for m, p in r.reqs if m == 'turn/steer']
        check(st and st[0]['expectedTurnId'] == 'tu9' and st[0]['input'][0]['text'] == '插一句' and not c.queue,
              f'Codex：连接期间点“立即发送”，拿到轮 ID 后插进当前回复（{[m for m, _ in r.reqs]}）')
        a.on_notify('turn/completed', {'threadId': 'th-s', 'turn': {'id': 'tu9', 'status': 'completed'}})
    finally:
        swap('codex', old)


def t_fix4_stuck_busy():
    """修复 4：中断后收不到结束通知 20 秒兜底（测试里调短）；进程退出（含回收）时结束它上面进行中的轮。"""
    old_g = PV.STOP_GRACE
    PV.STOP_GRACE = 0.4
    try:
        a = PC.CodexAdapter()
        r = FakeRpc()
        a.rpc = r
        c = conv('codex')
        c.meta['native'] = 'th-w'
        a.threads['th-w'] = c.sid
        c.attached = True
        c.claim()
        c.begin_turn('长任务', [])
        c.pv['turn'] = 'tu-old'
        c.turn['native'] = 'tu-old'
        a.interrupt(c)
        ok = wait_for(lambda: not c.busy, 3)
        check(ok and last_end(c)['status'] == 'interrupted' and last_end(c).get('why') == 'user',
              'Codex：turn/interrupt 后一直没有 turn/completed，兜底时间到就在工作台这边结束')
        c.claim()
        c.begin_turn('下一条', [])
        a.on_notify('turn/completed', {'threadId': 'th-w', 'turn': {'id': 'tu-old', 'status': 'interrupted'}})
        check(c.busy and c.turn, '迟到的旧一轮结束通知不会结束下一轮')
        c.end_turn('done')
        # ACP：兜底按轮代号（以前用 gen，发下一条时 gen 变了就失效）；1.5 秒后补发一次取消
        PV.STOP_GRACE = 2.0
        b = PA.AcpAdapter()
        r2 = GateRpc(block=('session/prompt',))
        b.rpc, b.start_proc = r2, (lambda: r2)
        d = conv('agy')
        d.meta['native'] = 'as-w'
        b.sessions['as-w'] = d.sid
        d.attached = True
        d.claim()
        d.begin_turn('慢', [])
        b.send(d, '慢', [])
        wait_for(lambda: d.pv.get('prompting'))
        b.interrupt(d)
        ok = wait_for(lambda: not d.busy, 4)
        n = sum(1 for m, _ in r2.reqs if m == 'session/cancel')
        check(ok and n == 2 and last_end(d)['status'] == 'interrupted', f'Antigravity：取消后对方不结束，兜底结束这一轮（取消发了 {n} 次）')
        r2.rel.set()
    finally:
        PV.STOP_GRACE = old_g
    # 进程退出：回收（reaped）时也结束这个进程上进行中的轮；别的进程上的轮不动
    a = PC.CodexAdapter()
    r_old, r_new = FakeRpc(), FakeRpc()
    c1, c2 = conv('codex'), conv('codex')
    for c, rr in ((c1, r_old), (c2, r_new)):
        c.claim()
        c.begin_turn('做事', [])
        c.turn['rpc'] = rr
    a.rpc = r_new
    r_old.reaped = 'idle'
    a.on_proc_exit(r_old, 0)
    check(not c1.busy and last_end(c1)['status'] == 'failed' and c1.sid in S.MARKS['interrupted'] and '进程已关闭' in c1.items[-1].get('text', ''),
          '进程回收时进行中的轮也结束（以前回收的不处理，会一直“忙碌”）')
    check(c2.busy and c2.turn, '回收旧进程不影响新进程上的轮')
    a.on_proc_exit(r_new, 1)
    check(not c2.busy and '意外退出' in c2.items[-1].get('text', ''), '意外退出照旧')


def t_fix2_outbuf():
    """修复 2：两个 Codex 对话同时跑命令，实时输出不串台（条目 ID 各对话都从 p1 数起，原生条目 ID 也可能相同）。"""
    a = PC.CodexAdapter()
    cs = []
    for th in ('th-a', 'th-b'):
        c = conv('codex')
        c.meta['native'] = th
        a.threads[th] = c.sid
        c.attached = True
        c.claim()
        c.begin_turn('跑', [])
        a.on_notify('item/started', {'threadId': th, 'turnId': 't', 'item': {'type': 'commandExecution', 'id': 'call_1', 'command': 'x',
                                                                               'cwd': PROJ, 'status': 'inProgress', 'commandActions': []}})
        cs.append(c)
    tids = [[i['id'] for i in c.items if i.get('k') == 'tool'][0] for c in cs]
    a.on_notify('item/commandExecution/outputDelta', {'threadId': 'th-a', 'itemId': 'call_1', 'delta': 'AAA\n'})
    a.on_notify('item/commandExecution/outputDelta', {'threadId': 'th-b', 'itemId': 'call_1', 'delta': 'BBB\n'})
    a.tick()
    o1, o2 = cs[0].byid[tids[0]].get('out'), cs[1].byid[tids[1]].get('out')
    check(tids[0] == tids[1] and o1.strip() == 'AAA' and o2.strip() == 'BBB', f'两个对话同号条目（{tids}）的实时输出各归各的：{o1!r} {o2!r}')
    a.on_notify('item/completed', {'threadId': 'th-a', 'turnId': 't', 'item': {'type': 'commandExecution', 'id': 'call_1', 'command': 'x',
                                                                             'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'AAA\n', 'commandActions': []}})
    check(cs[0].byid[tids[0]].get('done') and not cs[1].byid[tids[1]].get('done') and ('th-b', 'call_1') not in a.nat and (cs[1].sid, 'call_1') in a.nat,
          '一个对话的命令结束不影响另一个对话同 ID 的命令')
    for c in cs:
        c.end_turn('done')


def t_fix3_acp_tools():
    """修复 3：ACP 工具调用表按 (sessionId, toolCallId) 存，两个会话用同一个 toolCallId 不串。"""
    a = PA.AcpAdapter()
    cs = []
    for sn in ('s-1', 's-2'):
        c = conv('agy')
        c.meta['native'] = sn
        a.sessions[sn] = c.sid
        c.claim()
        c.begin_turn('跑', [])
        a.on_notify('session/update', {'sessionId': sn, 'update': {'sessionUpdate': 'tool_call', 'toolCallId': 'tc1', 'title': 'Run',
                                                                   'kind': 'execute', 'status': 'in_progress', 'rawInput': {'command': 'echo ' + sn}}})
        cs.append(c)
    a.on_notify('session/update', {'sessionId': 's-2', 'update': {'sessionUpdate': 'tool_call_update', 'toolCallId': 'tc1', 'status': 'completed',
                                                                  'content': [{'type': 'content', 'content': {'type': 'text', 'text': 'two'}}]}})
    t1 = [i for i in cs[0].items if i.get('k') == 'tool'][0]
    t2 = [i for i in cs[1].items if i.get('k') == 'tool'][0]
    check(t2.get('done') and t2.get('out') == 'two' and not t1.get('done') and t1.get('in') == 'echo s-1' and ('s-1', 'tc1') in a.nat,
          f'两个会话同一个 toolCallId：结果只记到自己的工具卡（{t1.get("done")}, {t2.get("out")!r}）')
    for c in cs:
        c.end_turn('done')


def t_fix5_rwi_conservative():
    """修复 5：重启脚本查不到 Codex / Antigravity 状态（读不到令牌、接口出错）时算“不空闲”，不能直接重启。"""
    w = os.path.join(TMP, 'rwi5')
    os.makedirs(os.path.join(w, '.run'))
    shutil.copy(os.path.join(ROOT, 'restart_when_idle.py'), w)
    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = b'ok 0'
            self.send_response(200)
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            calls.append(self.path)
            n = sum(1 for c in calls if c.endswith('prov-busy'))
            code, b = (500, b'{"error": "x"}') if n <= 2 else (200, b'{"busy": false}')
            self.send_response(code)
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # 先不放令牌：读不到令牌也要算不空闲
    subprocess.run([sys.executable, '-I', os.path.join(w, 'restart_when_idle.py'), '--server-now'], env=dict(os.environ, CWB_PORT=str(port)))
    logp = os.path.join(w, '.run', 'restart_when_idle.log')
    time.sleep(3)
    early = open(logp).read() if os.path.exists(logp) else ''
    open(os.path.join(w, '.run', f'token-{port}'), 'w').write('t' * 30)
    wait_for(lambda: '立即结束旧服务' in (open(logp).read() if os.path.exists(logp) else ''), 30)
    log = open(logp).read() if os.path.exists(logp) else ''
    nf = log.count('按不空闲处理')
    check('立即结束旧服务' not in early and '按不空闲处理' in early, '读不到令牌：不算空闲，不重启')
    check(nf >= 3 and '立即结束旧服务' in log and log.rindex('按不空闲处理') < log.index('立即结束旧服务'),
          f'接口出错（500）也不算空闲；能查到并连续空闲后才重启（失败 {nf} 次）')
    srv.shutdown()
    me = os.path.join(w, 'restart_when_idle.py')
    for l in os.popen('ps -axo pid=,command=').read().splitlines():
        q = l.strip().split(None, 1)
        if len(q) == 2 and me in q[1]:
            try:
                os.kill(int(q[0]), 15)
            except OSError:
                pass


def t_fix6_unsent():
    """修复 6：一轮以出错或进程退出结束时，排队的消息不悄悄丢：时间线列出来，并通知界面放回输入框。"""
    c = conv('codex')
    c.claim()
    c.begin_turn('做事', [])
    c.queue[:] = [('我拒绝了刚才的操作，说明：换个办法', []), ('再看看 b.py', ['/tmp/x.png'])]
    n0 = len(EVENTS)
    c.end_turn('error', error_type='x', error_msg='boom')
    it = c.items[-1]
    ev = [e for e in EVENTS[n0:] if e.get('type') == 'pv-unsent']
    check(not c.queue and not c.busy and it.get('etype') == '没发出去' and '换个办法' in it['text'] and '再看看 b.py' in it['text']
          and '附 1 张图片' in it['text'], f'出错结束：排队的消息列在时间线里（{" ".join(it.get("text", "").split())[:40]}…）')
    check(ev and ev[0]['sid'] == c.sid and ev[0]['texts'] == ['我拒绝了刚才的操作，说明：换个办法', '再看看 b.py'] and ev[0]['images'] == ['/tmp/x.png'],
          '出错结束：通知界面把没发出去的消息放回输入框')
    check(any(r.get('t') == 'new' and r['it'].get('etype') == '没发出去' for r in rows(c)), '这条提示写进记录（重开还在）')
    # 中断照旧：排队的接着发
    d = conv('codex')
    d.claim()
    d.begin_turn('做事', [])
    d.queue[:] = [('接着', [])]
    called = []
    orig = PV.start_turn
    PV.start_turn = lambda cc, t, im: called.append(t)
    try:
        d.end_turn('interrupted', why='user')
        wait_for(lambda: called)
    finally:
        PV.start_turn = orig
    check(called == ['接着'] and d.busy, '中断结束：排队的消息照常发出（先占住这一轮）')
    d.busy = False


def t_fix7_live_and_lazy():
    """修复 7：实时预览只推末尾一屏、平时只推增量；启动时不把全部条目读进内存。"""
    c = conv('codex')
    c.claim()
    c.begin_turn('写长文', [])
    n0 = len(EVENTS)
    c.set_live('第一段')
    c.push_state()
    c.set_live('第一段，第二段')
    c.push_state()
    evs = [e['state'] for e in EVENTS[n0:] if e.get('type') == 'runner' and e['sid'] == c.sid]
    check(evs[-2].get('live') == '第一段' and 'live' not in evs[-1] and evs[-1].get('liveAdd') == '，第二段' and evs[-1].get('liveAt') == 3
          and evs[-1].get('liveEnd') == 7, f'正文往后长时只推新增的部分：{ {k: evs[-1].get(k) for k in ("liveAdd", "liveAt", "liveEnd")} }')
    # 模拟界面（ux-v.js liveIn）接回整段，和后台的末尾一屏一致
    base = {}

    def live_in(st):
        if 'liveAdd' not in st:
            base.update(frm=st['liveFrom'], end=st['liveEnd'], text=st['live'])
            return
        if base and base['frm'] == st['liveFrom'] and st['liveAt'] <= base['end'] <= st['liveEnd']:
            base['text'] += st['liveAdd'][base['end'] - st['liveAt']:]
            base['end'] = st['liveEnd']
    n1 = len(EVENTS)
    txt = ''
    for i in range(400):
        txt += f'第 {i} 行：' + '内容' * 10 + '\n'
        c.set_live(txt)
        c.push_state()
    sts = [e['state'] for e in EVENTS[n1:] if e.get('type') == 'runner' and e['sid'] == c.sid]
    for st in sts:
        live_in(st)
    frm, tail = c.live_view()
    full = [st for st in sts if 'live' in st]
    check(base.get('text') == tail and len(tail) <= PV.LIVE_MAX and frm > 0 and txt[frm - 1] == '\n',
          f'界面按增量接起来的就是末尾一屏（{len(tail)} 字，从第 {frm} 字的行首开始）')
    big = max(len(json.dumps(st, ensure_ascii=False)) for st in sts)
    check(big < PV.LIVE_MAX * 1.2 + 2000 and len(full) < len(sts), f'每次推送不超过一屏（最大 {big} 字节），整段只在起点挪动时推（{len(full)}/{len(sts)} 次）')
    c.lp = (c.lp[0], c.lp[1], time.time() - 5)
    n2 = len(EVENTS)
    c.set_live(txt + '尾巴')
    c.push_state()
    st = [e['state'] for e in EVENTS[n2:] if e.get('type') == 'runner' and e['sid'] == c.sid][-1]
    check('live' in st, '每 2 秒推一次整段（中途打开的窗口据此接上）')
    c.end_turn('done')
    # 按需读取
    import uuid
    sid = str(uuid.uuid4())
    d = PV.PConv(sid, 'codex', PROJ)
    d.begin_turn('启动时不要全读进来', [])
    for i in range(50):
        d.add({'k': 'text', 'text': '很长的回复 %d' % i})
    d.end_turn('done')
    PV.CONVS.pop(sid, None)
    PV.scan()
    e = PV.CONVS.get(sid)
    check(e and not e.loaded and not e.items and e.title() == '启动时不要全读进来', '启动时只读对话信息（标题取第一条消息），条目不进内存')
    p = PV.api_open({'sid': sid})
    check(e.loaded and len(e.items) == 51 and p['items'][0]['text'] == '启动时不要全读进来', f'打开时再读全部条目（{len(e.items)} 条）')


def t_fix8_search_restart():
    """修复 8：左栏内容搜索包括这两家的对话；重启后台服务要说明“等 X 这一轮结束后重启”。"""
    c = conv('agy')
    c.claim()
    c.begin_turn('帮我查查独角兽甲乙丙在哪', [])
    c.add({'k': 'text', 'text': '在 zoo.py 里'})
    c.end_turn('done')
    d = S.API['search']({'q': '独角兽甲乙'})
    hit = [x for x in d.get('results', []) if x['sid'] == c.sid]
    check(hit and hit[0].get('prov') == 'agy' and '独角兽甲乙' in hit[0]['snip'], f'内容搜索能搜到 Antigravity 对话（{hit[:1]}）')
    check(not [x for x in S.API['search']({'q': '不存在的词组九九'}).get('results', []) if x['sid'] == c.sid], '搜不到时不列')
    # 重启：换掉真正的重启（测试里绝不真的去重启），看提示
    called = []
    PV.ORIG['restart-service'], orig = (lambda b: called.append(b) or {'ok': True}), PV.ORIG.get('restart-service')
    try:
        for x in list(PV.CONVS.values()):
            x.busy = False
            x.queue = []
        e = conv('codex')
        e.claim()
        n0 = len(EVENTS)
        r = S.API['restart-service']({'pvAware': True})
        check(r.get('wait') == '等 Codex 这一轮结束后重启' and called, f'重启时这两家在回复：返回要等的说明（{r.get("wait")}）')
        check(S.API['prov-busy']({}).get('wait') == '等 Codex 这一轮结束后重启', 'prov-busy 也带这句说明（界面据此换确认框）')
        r = S.API['restart-service']({})
        time.sleep(1.5)
        toasts = [x for x in EVENTS[n0:] if x.get('type') == 'toast']
        check(len(toasts) == 1 and toasts[0]['text'] == '等 Codex 这一轮结束后重启', '不是从 ux-v.js 发的：稍后推一条提示，盖过设置页的“正在重启”')
        e.busy = False
        check(not S.API['restart-service']({}).get('wait'), '都空闲时不提示')
    finally:
        PV.ORIG['restart-service'] = orig


def main():
    for f in (t_redline, t_record, t_scan, t_projects, t_wrappers, t_codex_events, t_codex_approval, t_codex_quota, t_acp, t_diff, t_restart_when_idle,
              t_fix1_stop_while_connecting, t_fix2_outbuf, t_fix3_acp_tools, t_fix4_stuck_busy, t_fix5_rwi_conservative, t_fix6_unsent,
              t_fix7_live_and_lazy, t_fix8_search_restart):
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
