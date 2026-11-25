"""11-23 单元测试：Codex、Antigravity 对话的子代理接到右栏“任务”页（prov_agents.py）。
不启动 claude、codex、agy；Codex 目录、agy 数据目录、工作台数据目录都放临时目录，记录按两家真实格式造。
运行：.venv/bin/python -I tests/test_prov_agents.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix='cwb-subag-')
os.environ['CWB_DATA_DIR'] = os.path.join(TMP, 'data')
os.environ['CWB_PORT'] = '47961'
os.environ['CWB_CODEX_HOME'] = CX = os.path.join(TMP, 'codex')
os.environ['CWB_AGY_DATA_DIR'] = AG = os.path.join(TMP, 'agy')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as S  # noqa: E402
import providers as PV  # noqa: E402
import prov_agents as PA  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


S.CONF_PATH = os.path.join(TMP, 'config.json')
S.MARKS_PATH = os.path.join(TMP, 'marks.json')
NOW = time.time()


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.000Z'


def conv(prov, native, items=()):
    import uuid
    c = PV.PConv(str(uuid.uuid4()), prov, TMP)
    c.meta['native'] = native
    c.meta['created'] = NOW - 3600
    for it in items:
        c.add(dict(it), persist=False)
    PV.CONVS[c.sid] = c
    return c


def api(name, b):
    return S.API[name](b)


# ---------------------------------------------------------------- Codex 记录
def spawn(parent, path, role='', **kw):
    """真实子线程的 session_meta：source.subagent.thread_spawn 带父线程（顶层也有 parent_thread_id）。"""
    return dict({'parent_thread_id': parent, 'agent_path': path, 'agent_role': role,
                 'source': {'subagent': {'thread_spawn': {'parent_thread_id': parent, 'depth': 1, 'agent_path': path, 'agent_role': role}}}}, **kw)


def rollout(tid, meta, rows, day=None, arch=False, zst=False):
    d = os.path.join(CX, 'archived_sessions') if arch else os.path.join(CX, 'sessions', *(day or time.strftime('%Y/%m/%d', time.localtime(NOW))).split('/'))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f'rollout-{time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(NOW))}-{tid}.jsonl')
    lines = [json.dumps({'timestamp': iso(NOW - 600), 'ordinal': 0, 'type': 'session_meta', 'payload': dict(meta, id=tid, timestamp=iso(NOW - 600))})]
    lines += [json.dumps(dict({'timestamp': iso(NOW - 600 + i), 'ordinal': i}, **r)) for i, r in enumerate(rows, 1)]
    data = ('\n'.join(lines) + '\n').encode()
    if zst:  # Codex 把旧记录压缩成 .jsonl.zst
        from compression import zstd
        p += '.zst'
        data = zstd.compress(data)
    with open(p, 'wb') as f:
        f.write(data)
    return p


def ev(t, **kw):
    return {'type': 'event_msg', 'payload': dict(kw, type=t)}


def ri(t, **kw):
    return {'type': 'response_item', 'payload': dict(kw, type=t)}


PARENT = '01a00000-0000-7000-8000-000000000001'
KID1, KID2, KID3, KID4 = ('01a00000-0000-7000-8000-00000000001%d' % i for i in range(1, 5))
FORKED = [ri('message', role='user', content=[{'type': 'input_text', 'text': '父线程的历史，不该出现'}]),
          ev('turn_aborted', reason='interrupted')]
# 子代理 1：v2 分叉（前 2 行是抄来的父线程历史），代码模式 exec 包着看图和命令，最后完成
P1_ROWS = FORKED + [
    ev('task_started'),
    ri('agent_message', author='/root', recipient='/root/icon_qa',
       content=[{'type': 'input_text', 'text': 'Message Type: NEW_TASK\nTask name: /root/icon_qa\nSender: /root\nPayload:\n'},
                {'type': 'encrypted_content', 'encrypted_content': 'gAAAAABxyz'}]),
    ri('function_call', name='spawn_agent', call_id='c0', arguments=json.dumps({'task_name': 'helper', 'message': 'gAAAAAB' + 'b' * 60})),
    ri('custom_tool_call', name='exec', call_id='c1', input='text(await tools.exec_command({cmd:"ls"}))'),
    ev('item_completed', item={'type': 'ImageView', 'id': 'e1', 'path': 'file:///tmp/a%20b.png'}),
    ev('item_completed', item={'type': 'CommandExecution', 'id': 'e2', 'command': ['/bin/zsh', '-lc', 'ls -la'], 'status': 'completed',
                               'aggregated_output': 'x\n', 'exit_code': 0, 'parsed_cmd': [{'type': 'unknown', 'cmd': 'ls -la'}]}),
    ri('custom_tool_call_output', call_id='c1', output=[{'type': 'input_text', 'text': 'done'}]),
    ri('function_call_output', call_id='c0', output='spawned'),
    ri('function_call', name='send_message', call_id='c2', arguments=json.dumps({'target': '/root', 'message': 'gAAAAAB' + 'a' * 60})),
    ri('function_call_output', call_id='c2', output=''),
    ri('reasoning', summary=[{'type': 'summary_text', 'text': '想一想'}]),
    ri('message', role='assistant', content=[{'type': 'output_text', 'text': 'PASS\n全部通过'}]),
    ev('token_count', info={'total_token_usage': {'output_tokens': 321}}),
    ev('task_complete'),
]
p1 = rollout(KID1, spawn(PARENT, '/root/icon_qa', 'reviewer', agent_nickname='Archimedes', subagent_history_start_ordinal=3, forked_from_id=PARENT), P1_ROWS)
# 同一线程更早被压缩过的副本（内容不同）：有 .jsonl 时用 .jsonl
rollout(KID1, spawn(PARENT, '/root/icon_qa', 'reviewer', subagent_history_start_ordinal=3), FORKED + [ev('task_started')], arch=True, zst=True)
# 子代理 2：被打断；子代理 3：子代理 2 派的孙代理，还在跑（记录刚更新）；子代理 4：归档目录里，出错结束
# v1 子线程不抄父线程历史，记录里的 subagent_history_start_ordinal 等于总行数，不能拿来跳（真实数据如此）
p2 = rollout(KID2, spawn(PARENT, '/root/fixer', 'worker', subagent_history_start_ordinal=4, multi_agent_version='v1'),
             [ev('task_started'), ri('function_call', name='exec_command', call_id='d1', arguments=json.dumps({'cmd': 'make'})), ev('turn_aborted', reason='interrupted')])
p3 = rollout(KID3, {'source': {'subagent': {'thread_spawn': {'parent_thread_id': KID2, 'agent_path': '/root/fixer/sub', 'agent_role': 'explorer'}}}},
             [ev('task_started'), ri('message', role='user', content=[{'type': 'input_text', 'text': '去查一下日志'}])])
p4 = rollout(KID4, spawn(PARENT, '/root/broken'),
             [ev('task_started'), ev('error', message='网关 502'), ev('task_complete')], arch=True, zst=True)
# 自动审批的 guardian 线程：顶层也带 parent_thread_id，但不是 spawn_agent 派的，不列（T3 只认 thread_spawn）
rollout('01a00000-0000-7000-8000-000000000097', {'parent_thread_id': PARENT, 'source': {'subagent': {'other': 'guardian'}}},
        [ev('task_started'), ri('message', role='user', content=[{'type': 'input_text', 'text': 'The following is the Codex agent history'}])])
# 不相干的线程、别的父线程、日期早于对话前一天的目录（不该扫到）
rollout('01a00000-0000-7000-8000-000000000099', spawn('other', '/root/x'), [ev('task_started')])
rollout('01a00000-0000-7000-8000-000000000098', spawn(PARENT, '/root/old'), [ev('task_started')], day='2001/01/01')
for p in (p1, p2):
    os.utime(p, (NOW - 1000, NOW - 1000))


def t_codex():
    c = conv('codex', PARENT, [{'k': 'tool', 'name': 'Agent', 'desc': '/root/icon_qa', 'in': '', 'done': True},
                               {'k': 'tool', 'name': 'Agent', 'desc': '/root/icon_qa', 'in': '', 'done': True},
                               {'k': 'tool', 'name': 'Agent', 'desc': '/root/fixer', 'in': '', 'done': True}])
    d = api('agents', {'sid': c.sid})
    by = {a['desc']: a for a in d['agents']}
    check(set(by) == {'icon_qa', 'fixer', 'sub', 'broken'} and len(d['agents']) == 4 and d['shells'] == [],
          'Codex：列出子代理、孙代理、归档里压缩过的子代理；不列 guardian、别的线程、过早目录里的；同一线程不重复')
    check(by['icon_qa']['state'] == 'done' and by['fixer']['state'] == 'killed' and by['sub']['state'] == 'run' and by['broken']['state'] == 'err',
          'Codex：task_complete → 已完成，turn_aborted → 已停止，刚更新 → 运行中，出错后结束 → 出错')
    check(d['agents'][0]['desc'] == 'sub', '运行中的排在最前')
    a = by['icon_qa']
    check(a['type'] == 'reviewer' and a['out'] == 321 and a['last'].startswith('PASS') and a['tool_id'] == c.items[0]['id'],
          'Codex 卡片：角色、输出 token、最后一步、定位到父对话里第一条 Agent 条目')
    check(by['fixer']['tool_id'] == c.items[2]['id'] and by['broken']['tool_id'] == '', 'Codex 定位：按 agent_path 对上，对不上的留空')
    its = api('subagent', {'sid': c.sid, 'agent': a['id']})['items']
    txt = json.dumps(its, ensure_ascii=False)
    check('父线程的历史' not in txt, '抄过来的父线程历史不出现在过程里')
    check(its[0]['k'] == 'user' and '加密' in its[0]['text'] and its[0]['text'].startswith('/root 发来新任务'), '加密的任务正文说明读不到')
    names = [x.get('name') for x in its if x['k'] == 'tool']
    check(names == ['Agent', 'Read', 'Bash', '发消息'] and a['tools'] == 4,
          f'代码模式 exec 外壳隐藏，里面的看图、命令单独列出；同时在跑的 spawn_agent 不被连带藏掉；工具次数一致：{names}')
    check(a['state'] == 'done' and 'PASS' in json.dumps(its, ensure_ascii=False), '有 .jsonl 时不用更早压缩的副本')
    rd = [x for x in its if x.get('name') == 'Read'][0]
    check(rd['path'] == '/tmp/a b.png' and [x for x in its if x.get('name') == 'Bash'][0]['in'] == 'ls -la', '看图路径去掉 file:// 并解码，命令去掉 zsh -lc')
    check([x for x in its if x.get('name') == '发消息'][0]['in'].startswith('（已加密'), '加密的消息参数显示成已加密')
    check(any(x['k'] == 'thought' and x['text'] == '想一想' for x in its) and its[-1] == dict(its[-1], k='text'), '推理摘要、最终回复')
    check(all(not any(k.startswith('_') for k in x) and x.get('id') for x in its), '过程条目都有 id，不带内部字段')
    k2 = api('subagent', {'sid': c.sid, 'agent': by['fixer']['id']})['items']
    check(k2 and k2[-1].get('name') == 'Bash' and k2[-1]['in'] == 'make' and k2[-1]['done'] and k2[-1].get('noresult') == '未完成',
          '被打断时没结果的工具标成未完成；v1 子线程不按起始序号跳过内容')
    k3 = api('subagent', {'sid': c.sid, 'agent': by['sub']['id']})['items']
    check(k3 and k3[0] == dict(k3[0], k='user', text='去查一下日志'), 'v1 子代理的任务正文是第一条用户消息')
    # 增量：追加一行，过程和卡片跟着变；不在这个对话里的子代理、别家前缀都拿不到
    with open(p3, 'a') as f:
        f.write(json.dumps({'timestamp': iso(NOW), 'ordinal': 9, 'type': 'event_msg', 'payload': {'type': 'task_complete'}}) + '\n')
    d2 = {a['desc']: a for a in api('agents', {'sid': c.sid})['agents']}
    check(d2['sub']['state'] == 'done', '追加 task_complete 后变成已完成（增量读）')
    check(api('subagent', {'sid': c.sid, 'agent': 'cx-01a00000-0000-7000-8000-000000000099'})['items'] == [] and
          api('subagent', {'sid': c.sid, 'agent': 'ag-' + KID1})['items'] == [], '别的线程、别家前缀拿不到过程')


# ---------------------------------------------------------------- Antigravity 记录
AP = '527c33f6-0000-4000-8000-000000000001'
KA, KB, KC, KD, KE, KF = ('aaaaaaaa-0000-4000-8000-00000000000%d' % i for i in range(1, 7))
PROMPT_A = 'Generate icon A. ' + 'same long prefix ' * 30 + 'tail A'
PROMPT_B = 'Generate icon A. ' + 'same long prefix ' * 30 + 'tail B'


def wj(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(doc, f)


def sg(cid, *rest):
    return os.path.join(AG, 'brain', cid, '.system_generated', *rest)


def agy_kid(kid, role, prompt, state='SUBAGENT_STATE_ALIVE', steps=(), t0=None):
    wj(sg(AP, 'subagents', kid + '.json'), {'conversationId': kid, 'subagentDescriptor': {'typeName': 'image-generator', 'role': role}, 'state': state})
    wj(sg(kid, 'messages', 'm1.json'), {'id': 'm1', 'recipient': kid, 'sender': AP, 'timestamp': iso(t0 or NOW - 500), 'content': prompt[:50],
                                        'sourceMetadata': {'tool': {'conversationId': AP, 'toolCall': {'id': 'call_' + kid[-1], 'name': 'invoke_subagent',
                                                                                                        'argumentsJson': json.dumps({'Prompt': prompt, 'Role': role, 'TypeName': 'image-generator'})}}}})
    wj(sg(kid, 'messages', 'read.json'), {'m1': True})
    p = sg(kid, 'logs', 'transcript.jsonl')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w') as f:
        for s in steps:
            f.write(json.dumps(s) + '\n')
    return p


def step(i, ty, t, **kw):
    return dict({'step_index': i, 'source': 'MODEL', 'type': ty, 'status': 'DONE', 'created_at': iso(t)[:19] + 'Z'}, **kw)


T = NOW - 400
pa = agy_kid(KA, 'Designer A', PROMPT_A, steps=[
    step(0, 'SYSTEM_MESSAGE', T, source='SYSTEM', content=f'<SYSTEM_MESSAGE>\n[Message] timestamp=x sender={AP} priority=HIGH content=Generate icon A. same lo</SYSTEM_MESSAGE>'),
    step(1, 'PLANNER_RESPONSE', T + 2, thinking='先想', output_tokens=100,
         tool_calls=[{'name': 'generate_image', 'args': {'ImageName': '"icon_a"', 'Prompt': '"draw"'}},
                     {'name': 'run_command', 'args': {'CommandLine': '"ls /tmp"'}}]),
    step(2, 'GENERIC', T + 5, content='Created At: x\nCompleted At: y\nGenerated image is saved at /tmp/a.jpg'),
    step(3, 'RUN_COMMAND', T + 6, content='Created At: x\nCompleted At: y\nout-ls', error='exit 1'),
    step(4, 'PLANNER_RESPONSE', T + 8, output_tokens=20, tool_calls=[{'name': 'send_message', 'args': {'Recipient': json.dumps(AP), 'Message': '"done a"'}}]),
    step(5, 'GENERIC', T + 9, content='Message sent.'),
    step(6, 'PLANNER_RESPONSE', T + 9, content='Done A.', output_tokens=5),
])
wj(sg(AP, 'messages', 'r1.json'), {'id': 'r1', 'recipient': AP, 'sender': KA, 'timestamp': iso(T + 9), 'content': 'done a'})
wj(sg(AP, 'messages', 'read.json'), {'r1': True})
pb = agy_kid(KB, 'Designer B', PROMPT_B, steps=[step(0, 'PLANNER_RESPONSE', NOW - 5, tool_calls=[{'name': 'read_file', 'args': {'Path': '"/tmp/x"'}}])], t0=NOW - 450)
pc = agy_kid(KC, 'Designer C', 'prompt c', state='SUBAGENT_STATE_KILLED', t0=NOW - 300)
pd = agy_kid(KD, 'Designer D', 'prompt d', t0=NOW - 200)
# E：两个并行调用的第一个结果没写进记录（第 2 步缺失），后面不能错位；还有一个“在后台运行”的结果（状态停在 RUNNING）
pe = agy_kid(KE, 'Designer E', 'prompt e', t0=NOW - 700, steps=[
    step(1, 'PLANNER_RESPONSE', NOW - 700, tool_calls=[{'name': 'run_command', 'args': {'CommandLine': '"cmd-a"'}},
                                                       {'name': 'run_command', 'args': {'CommandLine': '"cmd-b"'}}]),
    step(3, 'RUN_COMMAND', NOW - 699, content='out-b'),
    step(4, 'PLANNER_RESPONSE', NOW - 698, tool_calls=[{'name': 'read_file', 'args': {'Path': '"/tmp/f"'}},
                                                       {'name': 'run_command', 'args': {'CommandLine': '"sleep 99"'}}]),
    step(5, 'GENERIC', NOW - 697, content='file-f'),
    step(6, 'RUN_COMMAND', NOW - 697, status='RUNNING', content='Created At: x\nTool is running as a background task with task id: t-6'),
])
# F：agy 进程重启前在干活、没结束，重启通知晚于它最后一步 → 已停止；D 是重启通知之后才启动的，不受影响
pf = agy_kid(KF, 'Designer F', 'prompt f', t0=NOW - 900, steps=[
    step(1, 'PLANNER_RESPONSE', NOW - 900, tool_calls=[{'name': 'run_command', 'args': {'CommandLine': '"long"'}}])])
wj(sg(AP, 'messages', 'sys1.json'), {'id': 'sys1', 'recipient': AP, 'sender': 'system', 'timestamp': iso(NOW - 250), 'hideFromUser': True,
                                     'content': '[Notice] All your subagents and background tasks have been stopped due to server restart.'})
for p in (pa, pc, pd, pe, pf):
    os.utime(p, (NOW - 1000, NOW - 1000))


def raw_in(prompt, role):
    return json.dumps({'Prompt': prompt, 'Role': role, 'TypeName': 'image-generator'}, ensure_ascii=False)[:800]


def t_agy():
    c = conv('agy', AP, [{'k': 'tool', 'name': 'Run invoke_subagent', 'desc': 'Run invoke_subagent', 'in': raw_in(PROMPT_B, 'Designer B'), 'done': True},
                         {'k': 'tool', 'name': 'Run invoke_subagent', 'desc': 'Run invoke_subagent', 'in': raw_in(PROMPT_A, 'Designer A'), 'done': True},
                         {'k': 'tool', 'name': 'Bash', 'in': 'ls', 'done': True}])
    d = api('agents', {'sid': c.sid})
    by = {a['desc']: a for a in d['agents']}
    check(set(by) == {'Designer A', 'Designer B', 'Designer C', 'Designer D', 'Designer E', 'Designer F'} and d['shells'] == [],
          'Antigravity：按 subagents/*.json 列出子代理')
    st = {k[-1]: v['state'] for k, v in by.items()}
    check(st == {'A': 'done', 'B': 'run', 'C': 'killed', 'D': 'stale', 'E': 'killed', 'F': 'killed'},
          f'这一轮结束 → 已完成，刚有动静 → 运行中，被停掉、被重启停掉 → 已停止，没结束又很久没动 → 未完成；重启通知之后才启动的不受影响：{st}')
    check(by['Designer A']['tool_id'] == c.items[1]['id'] and by['Designer B']['tool_id'] == c.items[0]['id'],
          '提示词开头 500 多字都一样时也按全文对上各自的 invoke_subagent 条目')
    a = by['Designer A']
    check(a['type'] == 'image-generator' and a['tools'] == 3 and a['out'] == 125 and a['last'] == 'Done A.', 'Antigravity 卡片：类型、工具次数、输出 token、最后一步')
    its = api('subagent', {'sid': c.sid, 'agent': a['id']})['items']
    check(its[0]['k'] == 'user' and its[0]['text'] == PROMPT_A, '第 0 步截断的提示词换成启动消息里的全文')
    tools = [x for x in its if x['k'] == 'tool']
    check([x['name'] for x in tools] == ['生成图片', 'Bash', '发消息'], '工具名对应成工作台的名字')
    check(tools[0]['out'] == 'Generated image is saved at /tmp/a.jpg' and tools[0]['desc'] == 'icon_a' and tools[1]['in'] == 'ls /tmp',
          '工具结果按顺序配对，去掉 Created At / Completed At 头，参数解了一层 JSON')
    check(tools[1]['error'] and tools[2]['desc'] == '给父代理' and its[1]['k'] == 'thought', '出错的结果、发给父代理、思考')
    ib = api('subagent', {'sid': c.sid, 'agent': by['Designer B']['id']})['items']
    check(ib[0] == dict(ib[0], k='user', text=PROMPT_B) and ib[1]['name'] == 'Read' and not ib[1]['done'], '没有第 0 步时补上提示词；还没结果的工具是运行中')
    ie = [x for x in api('subagent', {'sid': c.sid, 'agent': by['Designer E']['id']})['items'] if x['k'] == 'tool']
    check([(x['in'] or x['path'], x['out'], x.get('noresult', ''), bool(x.get('bg'))) for x in ie] ==
          [('cmd-a', '', '没有结果', False), ('cmd-b', 'out-b', '', False), ('/tmp/f', 'file-f', '', False),
           ('sleep 99', 'Tool is running as a background task with task id: t-6', '', True)] and all(x['done'] for x in ie),
          f'按 step_index 配对：缺一步不错位，没结果的标出来，后台运行的收尾：{ie}')
    # 增量：B 回报了父对话、但接着干活（最后一步还在调工具）→ 仍是运行中
    with open(pb, 'a') as f:
        f.write(json.dumps(step(1, 'GENERIC', NOW - 4, content='file body')) + '\n')
        f.write(json.dumps(step(2, 'PLANNER_RESPONSE', NOW - 3, tool_calls=[{'name': 'send_message', 'args': {'Recipient': json.dumps(AP), 'Message': '"b"'}}])) + '\n')
    wj(sg(AP, 'messages', 'r2.json'), {'id': 'r2', 'recipient': AP, 'sender': KB, 'timestamp': iso(NOW - 3), 'content': 'done b'})
    d2 = {a['desc']: a for a in api('agents', {'sid': c.sid})['agents']}
    ib2 = api('subagent', {'sid': c.sid, 'agent': d2['Designer B']['id']})['items']
    check(d2['Designer B']['state'] == 'run' and ib2[1]['done'] and ib2[1]['out'] == 'file body', '回报后接着干活仍是运行中，工具结果补上（增量读）')
    with open(pb, 'a') as f:
        f.write(json.dumps(step(3, 'GENERIC', NOW - 2, content='sent')) + '\n')
        f.write(json.dumps(step(4, 'PLANNER_RESPONSE', NOW - 2, content='Done B.')) + '\n')
    d3 = {a['desc']: a for a in api('agents', {'sid': c.sid})['agents']}
    check(d3['Designer B']['state'] == 'done' and d3['Designer B']['last'] == 'Done B.', '最后一步是不带工具调用的回复 → 已完成')
    # 空闲回收：有子代理在跑时不回收进程
    A = PV.ADAPTERS['agy']
    c.attached = True
    with open(pf, 'a') as f:
        f.write(json.dumps(step(2, 'RUN_COMMAND', NOW, content='still going')) + '\n')
    check(A.kids_running([c]), '有子代理在跑（F 重启后又动了）：空闲回收先不回收')
    bl = [x for x in PV.busy_list() if x['sid'] == c.sid]
    check(bl and bl[0]['kids'] >= 1 and not bl[0]['busy'] and PV.restart_wait_text(bl) == '等 Antigravity 的子代理跑完后重启',
          f'重启服务也等子代理跑完：{bl}')
    os.utime(pf, (NOW - 1000, NOW - 1000))
    PA.PARSERS.clear()
    check(not A.kids_running([c]) and not [x for x in PV.busy_list() if x['sid'] == c.sid], '没有子代理在跑：照常回收、照常重启')
    c.attached = False
    check(api('subagent', {'sid': c.sid, 'agent': 'ag-../../etc'})['items'] == [] and api('subagent', {'sid': c.sid, 'agent': 'ag-nope'})['items'] == [],
          '非法 ID、不是这个对话的子代理都拿不到')
    c0 = conv('agy', '')
    check(api('agents', {'sid': c0.sid}) == {'agents': [], 'shells': []}, '还没有原生会话 ID 的对话给空表')


def t_tail():
    """记录被清空重写（开头变了、长度不变小）时从头读；Codex 首次读按块读也能拼好跨块的行。"""
    p = os.path.join(TMP, 'tail.jsonl')
    rows = [step(i, 'PLANNER_RESPONSE', NOW, content='old-%d' % i) for i in range(3)]
    with open(p, 'w') as f:
        f.write(''.join(json.dumps(r) + '\n' for r in rows))
    k = PA.parser(PA.AgyKid, p)
    with k.lock:
        k.feed()
        n1 = [x['text'] for x in k.snapshot()]
    rows = [step(i, 'PLANNER_RESPONSE', NOW, content='NEW-%d' % i) for i in range(4)]
    with open(p, 'w') as f:
        f.write('{"step_index": 0, "source": "X", ' + ''.join(json.dumps(r) + '\n' for r in rows)[1:].replace('"step_index": 0, ', '', 1))
    with k.lock:
        k.feed()
        n2 = [x['text'] for x in k.snapshot()]
    check(n1 == ['old-0', 'old-1', 'old-2'] and n2 == ['NEW-0', 'NEW-1', 'NEW-2', 'NEW-3'], f'文件被重写时从头读：{n2}')
    big = os.path.join(TMP, 'big.jsonl')
    with open(big, 'w') as f:
        for i in range(30):
            f.write(json.dumps(step(i, 'PLANNER_RESPONSE', NOW, content='x' * 15000 + '-' + str(i))) + '\n')
    old = PA.Tail.CHUNK
    PA.Tail.CHUNK = 1 << 16
    try:
        k = PA.parser(PA.AgyKid, big)
        with k.lock:
            k.feed()
            got = [x['text'].rsplit('-', 1)[-1] for x in k.snapshot()]
    finally:
        PA.Tail.CHUNK = old
    check(got == [str(i) for i in range(30)], '分块读：跨块的长行拼接正确')


def t_wire():
    check(open(os.path.join(ROOT, 'static/app2.js'), encoding='utf-8').read().count('invoke_subagent/.test(x.name') == 1,
          '界面：Antigravity 的 invoke_subagent 条目也会触发刷新任务页')


try:
    t_codex()
    t_agy()
    t_tail()
    t_wire()
finally:
    shutil.rmtree(TMP, ignore_errors=True)
print('\n全部通过' if not FAIL else f'\n{len(FAIL)} 项失败：' + '；'.join(FAIL))
sys.exit(1 if FAIL else 0)
