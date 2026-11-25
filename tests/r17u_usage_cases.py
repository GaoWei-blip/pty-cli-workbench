"""第十七轮 U：用量与速度（usage_p.py 三家汇总；Claude 用第十四轮 usage.py 原样，只算工作台）的单元测试用例。
由 tests/test_r17_U.py 调用 run(check)；也可以单独跑：.venv/bin/python -I tests/r17u_usage_cases.py
只用临时目录造的小型记录：不读真实 ~/.claude/projects、~/.codex、~/.gemini，不启动任何 CLI。

build_fixtures(home, data_dir, now=None) 也给端到端测试用：在 home 下按真实布局写三家的记录，在 data_dir 下写工作台对话和委派，
返回关键数字（各家今天的请求数、token、对话 ID 等）。
"""
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

WB_HOOK = '/Users/demo/claude-workbench/.venv/bin/python -I /Users/demo/claude-workbench/hook.py idle'

# 对话 ID
CA, CB, CC, CD = ('aaaaaaaa-0000-4000-8000-00000000000a', 'bbbbbbbb-0000-4000-8000-00000000000b',
                  'cccccccc-0000-4000-8000-00000000000c', 'dddddddd-0000-4000-8000-00000000000d')
XA, XW, XF, XY = ('019a0000-0000-7000-8000-0000000000a1', '019a0000-0000-7000-8000-0000000000b2',
                  '019a0000-0000-7000-8000-0000000000c3', '019a0000-0000-7000-8000-0000000000d4')
GT, GG, GH = ('77777777-0000-4000-8000-000000000001', '88888888-0000-4000-8000-000000000002', '99999999-0000-4000-8000-000000000003')
S1, S2, S3 = ('5e5e0001-0000-4000-8000-000000000001', '5e5e0002-0000-4000-8000-000000000002', '5e5e0003-0000-4000-8000-000000000003')
R1 = 1795600000  # Codex 周窗口的重置时间（秒）
RANGES_D = {'today': 1, '7d': 7, '30d': 30}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{int(round(t * 1000)) % 1000:03d}Z'


def _w(path, rows, mode='w'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding='utf-8') as f:
        for d in rows:
            f.write(json.dumps(d, ensure_ascii=False, separators=(',', ':')) + '\n')


def day_start(now, back=0):
    return (datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=back)).timestamp()


# ---------------------------------------------------------------- Claude
class CW:
    def __init__(self, path, sid):
        self.path, self.sid, self.rows = path, sid, []

    def prompt(self, t, fork=False):
        d = {'parentUuid': None, 'isSidechain': False, 'type': 'user', 'message': {'role': 'user', 'content': '问题'},
             'uuid': f'u{t}', 'timestamp': iso(t), 'sessionId': self.sid}
        if fork:
            d['forkedFrom'] = {'sessionId': CB}
        self.rows.append(d)

    def req(self, t, rid):
        self.rows.append({'type': 'api-request', 'sessionId': self.sid, 'id': rid, 'timestamp': iso(t)})

    def asst(self, t, mid, out, model='claude-opus-5-5', inp=10, cr=1000, cw=100, ref=None, h1=0, fork=False, side=False):
        u = {'input_tokens': inp, 'cache_creation_input_tokens': cw, 'cache_read_input_tokens': cr, 'output_tokens': out, 'speed': 'standard'}
        if h1:
            u['cache_creation'] = {'ephemeral_5m_input_tokens': cw - h1, 'ephemeral_1h_input_tokens': h1}
        d = {'parentUuid': 'p', 'isSidechain': side, 'message': {'model': model, 'id': mid, 'type': 'message', 'role': 'assistant',
                                                                  'content': [{'type': 'text', 'text': '答'}], 'usage': u},
             'requestId': 'req_' + mid, 'type': 'assistant', 'uuid': f'a{t}{mid}', 'timestamp': iso(t), 'sessionId': self.sid}
        if ref:
            d['requestRef'] = ref
        if fork:
            d['forkedFrom'] = {'sessionId': CB}
        self.rows.append(d)

    def tool_result(self, t):
        self.rows.append({'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}]},
                          'uuid': f'tr{t}', 'timestamp': iso(t), 'sessionId': self.sid})

    def summary(self, t, cmd=WB_HOOK):
        self.rows.append({'parentUuid': 'p', 'isSidechain': False, 'type': 'system', 'subtype': 'stop_hook_summary', 'hookCount': 1,
                          'hookInfos': [{'command': cmd}], 'hookErrors': [], 'timestamp': iso(t), 'uuid': f's{t}', 'sessionId': self.sid})

    def dur(self, t, ms):
        self.rows.append({'parentUuid': 'p', 'isSidechain': False, 'type': 'system', 'subtype': 'turn_duration', 'durationMs': ms,
                          'messageCount': 3, 'timestamp': iso(t), 'uuid': f'd{t}', 'isMeta': False, 'sessionId': self.sid})

    def save(self):
        _w(self.path, self.rows)


# ---------------------------------------------------------------- Codex
def cx(t, typ, payload):
    return {'timestamp': iso(t), 'type': typ, 'payload': payload}


def cx_usage(i, c, o, r=0, w=0):
    return {'input_tokens': i, 'cached_input_tokens': c, 'cache_write_input_tokens': w, 'output_tokens': o,
            'reasoning_output_tokens': r, 'total_tokens': i + o}


def cx_add(a, b):
    return {k: a[k] + b[k] for k in a}


# ---------------------------------------------------------------- Antigravity（照 T3 antigravityUsageReader.test.ts 的造法）
def pnum(field, value):
    def varint(n):
        out = []
        while True:
            b = n % 128
            n //= 128
            out.append(b + (128 if n > 0 else 0))
            if n == 0:
                return out
    return varint(field * 8) + varint(value)


def pbytes(field, bs):
    enc = pnum(field, len(bs))
    enc[0] += 2
    return enc + list(bs)


def ptext(field, s):
    return pbytes(field, s.encode())


def pts(field, t):
    return pbytes(field, pnum(1, int(t)) + pnum(2, int(round((t - int(t)) * 1e9))))


def agy_usage(mid, inp, out, cr=0, reasoning=0, key=''):
    b = pnum(1, mid) + pnum(2, inp) + pnum(3, out)
    if cr:
        b += pnum(5, cr)
    if reasoning:
        b += pnum(9, reasoning)
    if key:
        b += ptext(11, key) + ptext(12, key)
    return b


def agy_db(path, steps, gens, traj_t):
    """steps: [(idx, step_type, 时间, usage 字节或 None)]；gens: [(idx, 模型 ID, 名字, 显示名, usage 字节)]。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, metadata BLOB);'
                      'CREATE TABLE gen_metadata (idx INTEGER, data BLOB);'
                      'CREATE TABLE trajectory_metadata_blob (data BLOB);')
    for idx, typ, t, usage in steps:
        meta = pts(8, t)
        if usage:
            meta += pbytes(9, usage)
        con.execute('INSERT INTO steps VALUES (?, ?, 3, ?)', (idx, typ, bytes(meta)))
    for idx, mid, name, disp, usage in gens:
        data = pnum(3, mid) + ptext(19, name) + ptext(21, disp) + pbytes(4, usage)
        con.execute('INSERT INTO gen_metadata VALUES (?, ?)', (idx, bytes(pbytes(1, data))))
    con.execute('INSERT INTO trajectory_metadata_blob VALUES (?)', (bytes(pbytes(2, pnum(1, int(traj_t)))),))
    con.commit()
    con.close()


def build_fixtures(home, data_dir, now=None):
    """造三家的小型记录。时间：今天（now 前两小时内，跨不过零点）和 3 天前各一些。返回关键数字。"""
    now = now or time.time()
    base = max(day_start(now) + 60, now - 7200)
    old = now - 3 * 86400
    proj = os.path.join(home, '.claude', 'projects')
    # ---- Claude：A 工作台（两轮，有收尾记录、turn_duration、子代理、1 小时缓存写），B 终端，C 是 B 的分叉，D 是 3 天前的终端对话
    a0 = base + 10
    a = CW(os.path.join(proj, '-tmp-proj', CA + '.jsonl'), CA)
    a.prompt(a0)
    a.req(a0 + 1, 'R1')
    a.asst(a0 + 5, 'm1', 2, ref='R1', h1=40)
    a.asst(a0 + 6, 'm1', 100, ref='R1', h1=40)
    a.tool_result(a0 + 7)
    a.asst(a0 + 9, 'm2', 50)
    a.summary(a0 + 10)
    a.dur(a0 + 10.1, 10000)
    a.prompt(a0 + 60)
    a.asst(a0 + 62, 'm3', 30)
    a.summary(a0 + 65)
    a.dur(a0 + 65.1, 5000)
    a.save()
    sub = CW(os.path.join(proj, '-tmp-proj', CA, 'subagents', 'agent-x1.jsonl'), CA)
    sub.rows.append({'type': 'user', 'isSidechain': True, 'message': {'role': 'user', 'content': '子任务'}, 'timestamp': iso(a0 + 3), 'sessionId': CA})
    sub.asst(a0 + 4, 'k1', 11, model='claude-haiku-5-5', side=True)
    sub.save()
    b0 = base + 200
    b = CW(os.path.join(proj, '-tmp-other', CB + '.jsonl'), CB)
    b.prompt(b0)
    b.asst(b0 + 3, 'n1', 40)
    b.dur(b0 + 4, 4000)
    b.save()
    c0 = base + 300
    c = CW(os.path.join(proj, '-tmp-other', CC + '.jsonl'), CC)
    c.prompt(b0, fork=True)
    c.asst(b0 + 3, 'n1', 40, fork=True)
    c.prompt(c0)
    c.asst(c0 + 2, 'n2', 7)
    c.save()
    d = CW(os.path.join(proj, '-tmp-other', CD + '.jsonl'), CD)
    d.prompt(old)
    d.asst(old + 2, 'o1', 25)
    d.save()
    # ---- Codex：X 终端（逐次请求账两次、周额度样本），W 工作台对话的原生线程，F 是 X 的分叉，Y 是 3 天前的旧格式（.jsonl.zst 和 .jsonl 两份）
    sess = os.path.join(home, '.codex', 'sessions')
    dd = datetime.fromtimestamp(base).strftime('%Y/%m/%d')
    x0 = base + 400
    u1, u2 = cx_usage(10000, 8000, 100, r=20), cx_usage(12000, 10000, 200)
    xrows = [cx(x0, 'session_meta', {'id': XA, 'model_provider': 'openai', 'cwd': '/tmp/x', 'source': 'cli'}),
             cx(x0 + 1, 'event_msg', {'type': 'task_started', 'turn_id': 'tX1', 'started_at': int(x0 + 1)}),
             cx(x0 + 1, 'response_item', {'type': 'message', 'id': 'msg1', 'role': 'user', 'content': []}),
             cx(x0 + 1.01, 'turn_context', {'turn_id': 'tX1', 'model': 'gpt-6-astra'}),
             cx(x0 + 5, 'token_usage_record', {'thread_id': XA, 'turn_id': 'tX1', 'response_id': 'resp_x1', 'usage': u1}),
             cx(x0 + 8, 'response_item', {'type': 'function_call_output', 'id': 'fco1', 'output': 'ok'}),
             cx(x0 + 12, 'token_usage_record', {'thread_id': XA, 'turn_id': 'tX1', 'response_id': 'resp_x2', 'usage': u2}),
             cx(x0 + 12.1, 'event_msg', {'type': 'token_count', 'info': {'total_token_usage': cx_add(u1, u2), 'last_token_usage': u2},
                                         'rate_limits': {'limit_id': 'codex', 'primary': {'used_percent': 20.0, 'window_minutes': 10080, 'resets_at': R1}}}),
             cx(x0 + 12.5, 'event_msg', {'type': 'task_complete', 'turn_id': 'tX1', 'duration_ms': 11500, 'time_to_first_token_ms': 3000}),
             cx(x0 + 13, 'event_msg', {'type': 'token_count', 'info': None,
                                       'rate_limits': {'limit_id': 'codex', 'primary': {'used_percent': 23.0, 'window_minutes': 10080, 'resets_at': R1}}})]
    xpath = os.path.join(sess, dd, f'rollout-x-{XA}.jsonl')
    _w(xpath, xrows)
    w0 = base + 500
    uw = cx_usage(5000, 0, 50)
    _w(os.path.join(sess, dd, f'rollout-w-{XW}.jsonl'), [
        cx(w0, 'session_meta', {'id': XW, 'model_provider': 'openai', 'originator': 'claude_workbench'}),
        cx(w0, 'event_msg', {'type': 'task_started', 'turn_id': 'tW1', 'started_at': int(w0)}),
        cx(w0, 'response_item', {'type': 'message', 'id': 'mw', 'role': 'user', 'content': []}),
        cx(w0 + 0.01, 'turn_context', {'turn_id': 'tW1', 'model': 'gpt-6-astra'}),
        cx(w0 + 6, 'token_usage_record', {'thread_id': XW, 'response_id': 'resp_w1', 'usage': uw}),
        cx(w0 + 6.01, 'event_msg', {'type': 'token_count', 'info': {'total_token_usage': uw, 'last_token_usage': uw}}),
        cx(w0 + 6.2, 'event_msg', {'type': 'task_complete', 'turn_id': 'tW1', 'duration_ms': 6200, 'time_to_first_token_ms': 5900})])
    f0 = base + 600
    uf = cx_usage(3000, 1000, 30)
    frows = [cx(f0, 'session_meta', {'id': XF, 'forked_from_id': XA, 'model_provider': 'openai'})]
    for k, r in enumerate(xrows[1:]):  # 分叉：父会话的历史成批复制、重新打时间戳
        frows.append(dict(r, timestamp=iso(f0 + 0.005 * (k + 1))))
    frows += [cx(f0 + 10, 'event_msg', {'type': 'task_started', 'turn_id': 'tF1', 'started_at': int(f0 + 10)}),
              cx(f0 + 10, 'turn_context', {'turn_id': 'tF1', 'model': 'gpt-6-astra'}),
              cx(f0 + 10, 'response_item', {'type': 'message', 'id': 'mf', 'role': 'user', 'content': []}),
              cx(f0 + 15, 'token_usage_record', {'thread_id': XF, 'response_id': 'resp_f1', 'usage': uf}),
              cx(f0 + 15.5, 'event_msg', {'type': 'task_complete', 'turn_id': 'tF1', 'duration_ms': 5500, 'time_to_first_token_ms': 4500})]
    _w(os.path.join(sess, dd, f'rollout-f-{XF}.jsonl'), frows)
    y1, y2 = cx_usage(2000, 500, 40), cx_usage(1000, 0, 10)
    yrows = [cx(old, 'session_meta', {'id': XY, 'model_provider': 'openai'}),
             cx(old + 1, 'turn_context', {'turn_id': 'tY1', 'model': 'gpt-5-codex'}),
             cx(old + 5, 'event_msg', {'type': 'token_count', 'info': {'total_token_usage': y1, 'last_token_usage': y1},
                                       'rate_limits': {'limit_id': 'codex', 'primary': {'used_percent': 10.0, 'window_minutes': 10080, 'resets_at': R1}}}),
             cx(old + 9, 'event_msg', {'type': 'token_count', 'info': {'total_token_usage': cx_add(y1, y2), 'last_token_usage': y2}})]
    od = datetime.fromtimestamp(old).strftime('%Y/%m/%d')
    ypath = os.path.join(sess, od, f'rollout-y-{XY}.jsonl')
    _w(ypath, yrows)
    from compression import zstd
    with open(ypath, 'rb') as f:
        raw = f.read()
    with open(ypath + '.zst', 'wb') as f:
        f.write(zstd.compress(raw))
    _w(os.path.join(home, '.codex', 'session_index.jsonl'), [{'id': XA, 'thread_name': 'X 的终端对话', 'updated_at': iso(x0)},
                                                              {'id': XY, 'thread_name': '旧对话', 'updated_at': iso(old)}])
    # ---- Antigravity：T 终端（antigravity-cli，另有一份在 antigravity-backup，去重），G 工作台（antigravity-cwb），H 工作台但没有库
    g = os.path.join(home, '.gemini')
    t0 = base + 700
    kt1 = agy_usage(1320, 1000, 50, cr=3000, reasoning=10, key='kT1')
    kt2 = agy_usage(1320, 500, 20, cr=4000, key='kT2')
    tdb = os.path.join(g, 'antigravity-cli', 'conversations', GT + '.db')
    agy_db(tdb, [(0, 14, t0, None), (1, 15, t0 + 4, kt1), (2, 14, t0 + 60, None), (3, 15, t0 + 66, kt2)],
           [(1, 1320, 'gemini-3.8-flash', 'Gemini 3.8 Flash (Low)', kt1)], t0)
    os.makedirs(os.path.join(g, 'antigravity-backup', 'conversations'), exist_ok=True)
    shutil.copy(tdb, os.path.join(g, 'antigravity-backup', 'conversations', GT + '.db'))
    con = sqlite3.connect(os.path.join(g, 'antigravity-cli', 'conversation_summaries.db'))
    con.execute('CREATE TABLE conversation_summaries (conversation_id text, title text NOT NULL DEFAULT "", preview text NOT NULL DEFAULT "")')
    con.execute('INSERT INTO conversation_summaries VALUES (?, ?, ?)', (GT, 'T 的终端对话', '预览'))
    con.commit()
    con.close()
    g0 = base + 800
    kg1, kg2 = agy_usage(1320, 200, 5, cr=800, key='kG1'), agy_usage(1320, 100, 7, cr=900, key='kG2')
    agy_db(os.path.join(g, 'antigravity-cwb', 'conversations', GG + '.db'),
           [(0, 14, g0, None), (1, 15, g0 + 4, kg1), (2, 14, g0 + 60, None), (3, 15, g0 + 66, kg2)],
           [(1, 1320, 'gemini-3.8-flash', 'Gemini 3.8 Flash (Low)', kg1), (3, 1320, 'gemini-3.8-flash', 'Gemini 3.8 Flash (Low)', kg2)], g0)
    h0 = base + 900
    # ---- 工作台对话记录 data/conv（P 线格式）
    conv = os.path.join(data_dir, 'conv')
    ms = lambda t: int(round(t * 1000))  # noqa: E731
    _w(os.path.join(conv, S1 + '.jsonl'), [
        {'v': 1, 'prov': 'codex', 'cwd': '/tmp/p', 'created': w0, 'native': '', 'title': '', 'opts': {}, 't': 'meta'},
        {'t': 'new', 'it': {'k': 'user', 'text': '工作台 Codex 提问', 'id': 'p1'}},
        {'t': 'turn_start', 'ts': ms(w0), 'uid': 'p1', 'provider': 'codex'},
        {'native': XW, 't': 'meta'},
        {'t': 'turn_end', 'ts': ms(w0 + 6.2), 'turnId': 'tW1', 'provider': 'codex', 'model': 'gpt-6-astra', 'startedAt': ms(w0),
         'firstTokenAt': ms(w0) + 4000, 'endedAt': ms(w0 + 6.2), 'status': 'completed',
         'usage': {'input': 5000, 'output': 50, 'cachedInput': 0, 'cacheWrite': 0, 'reasoning': 0}, 'usageSrc': 'protocol'}])
    _w(os.path.join(conv, S2 + '.jsonl'), [
        {'v': 1, 'prov': 'agy', 'cwd': '/tmp/p', 'created': g0, 'native': '', 'title': '', 'opts': {}, 't': 'meta'},
        {'t': 'new', 'it': {'k': 'user', 'text': '工作台 agy 提问', 'id': 'p1'}},
        {'native': GG, 't': 'meta'}, {'title': 'G 的标题', 't': 'meta'},
        {'t': 'turn_end', 'ts': ms(g0 + 4.1), 'turnId': 'p1', 'provider': 'agy', 'model': 'gemini-3.8-flash', 'startedAt': ms(g0),
         'firstTokenAt': ms(g0) + 3000, 'endedAt': ms(g0 + 4.1), 'status': 'completed',
         'usage': {'input': 200, 'output': 5, 'cachedInput': 800}, 'cost': {'amount': 0.01, 'currency': 'USD', 'src': 'usage_update', 'scope': 'session'}},
        {'t': 'turn_end', 'ts': ms(g0 + 66.1), 'turnId': 'p3', 'provider': 'agy', 'model': 'gemini-3.8-flash', 'startedAt': ms(g0 + 59),
         'firstTokenAt': ms(g0 + 59) + 5000, 'endedAt': ms(g0 + 66.1), 'status': 'completed',
         'usage': {'input': 100, 'output': 7, 'cachedInput': 900}, 'cost': {'amount': 0.03, 'currency': 'USD', 'src': 'usage_update', 'scope': 'session'}}])
    _w(os.path.join(conv, S3 + '.jsonl'), [
        {'v': 1, 'prov': 'agy', 'cwd': '/tmp/p', 'created': h0, 'native': '', 'title': '', 'opts': {}, 't': 'meta'},
        {'t': 'new', 'it': {'k': 'user', 'text': '没有库的 agy 对话', 'id': 'p1'}},
        {'native': GH, 't': 'meta'},
        {'t': 'turn_end', 'ts': ms(h0 + 3), 'turnId': 'p1', 'provider': 'agy', 'model': 'gemini-3.8-flash', 'startedAt': ms(h0),
         'firstTokenAt': ms(h0) + 1500, 'endedAt': ms(h0 + 3), 'status': 'completed', 'usage': {'input': 300, 'output': 9, 'cachedInput': 0}}])
    _w(os.path.join(data_dir, 'tasks.jsonl'), [
        {'t': 'task', 'id': 'task1', 'parent': {'conv': CA, 'provider': 'claude'}, 'child': {'conv': S1, 'provider': 'codex', 'model': 'gpt-6-astra'},
         'title': '复核', 'mode': 'review', 'origin': 'manual', 'createdAt': ms(w0) - 1000},
        {'t': 'task_status', 'id': 'task1', 'status': 'done', 'ts': ms(w0 + 7)}])
    return {
        'now': now, 'base': base,
        # Claude 只算工作台（第十四轮口径）：会话 A 的 m1、m2、m3 和第 1 轮里启动的子代理 k1；终端 B、分叉 C、3 天前的 D 都不算
        'claude': {'today_requests': 4, 'today_output': 100 + 50 + 30 + 11, 'week_requests': 4, 'sessions': 1,
                   'sid_wb': CA, 'sid_terminal': CB, 'sid_fork': CC, 'old_sid': CD},
        'codex': {'today_requests': 4, 'week_requests': 6, 'today_uncached': 11000, 'today_cached': 19000, 'today_output': 380,
                  'today_tokens': 11000 + 19000 + 380, 'week_quota_today': 13.0, 'turns_today': 3,
                  'native_wb': XW, 'native_terminal': XA, 'wb_sid': S1, 'xpath': xpath, 'ypath': ypath},
        'agy': {'today_requests': 5, 'today_uncached': 2100, 'today_cached': 8700, 'today_output': 91, 'today_reasoning': 10,
                'cost': 0.03, 'turns_today': 5, 'native_wb': GG, 'native_terminal': GT, 'native_nodb': GH, 'wb_sid': S2, 'nodb_sid': S3},
        'tasks': {'parent': CA, 'child': S1},
    }


# ================================================================ 用例
def _close(a, b, eps=1e-6):
    return a is not None and b is not None and abs(a - b) < eps


def run(check):
    import usage as US
    import usage_p as UP
    tmp = tempfile.mkdtemp(prefix='cwb-r17u-u-')
    saved_zmode, saved_read = UP.ZSTD_MODE, UP.read_codex_rollout
    try:
        home, data = os.path.join(tmp, 'home'), os.path.join(tmp, 'data')
        now = day_start(time.time()) + 15 * 3600  # 固定在今天下午三点，记录都在它前两小时内
        fx = build_fixtures(home, data, now)
        proj = os.path.join(home, '.claude', 'projects')

        def mk_claude(cache=None):
            return US.Usage(proj, cache_path=cache, titles=lambda sids: {s: '标题' + s[:4] for s in sids})

        def mk_hub(claude, cache=None, conv=True, env=None):
            return UP.Hub(home=home, env=env if env is not None else {}, conv_dir=os.path.join(data, 'conv') if conv else os.path.join(tmp, 'none'),
                          data_dir=data, cache_path=cache, claude=claude, titles=lambda sids: {s: '标题' + s[:4] for s in sids})

        def wait_scan(h, cl=None, limit=20):
            t0 = time.time()
            while time.time() - t0 < limit and (any(h.scanning.values()) or (cl is not None and cl.scanning)):
                time.sleep(0.05)

        # ---- 1. Claude 用 usage.py 原样（第十四轮：只算工作台）
        u = mk_claude()
        u.refresh()
        rows = [r for r in u.build() if r[0] >= day_start(now) * 1000]
        check(len(rows) == fx['claude']['today_requests'] and {r[1] for r in rows} == {CA} and sum(r[4] for r in rows) == fx['claude']['today_output'],
              f"Claude 只算工作台：今天 {len(rows)} 条、输出 {sum(r[4] for r in rows)}（终端会话 B、分叉 C 不算）")
        q_old = u.query('today', now=now)
        check(q_old['kpi']['requests'] == 4 and len(q_old['buckets']) == 24 and
              set(q_old) == {'scanning', 'updated', 'range', 'unit', 'kpi', 'today', 'buckets', 'models', 'heat', 'recent', 'sessions'},
              'Claude query() 原有字段一个不少、不多')
        check(not hasattr(US.Usage, 'turn_rows') and US.CACHE_VER == 2, 'usage.py 是第十四轮原样（没有 turn_rows，CACHE_VER=2）')

        # ---- 2. 三家汇总
        hub = mk_hub(u)
        hub.start()
        wait_scan(hub)
        q = hub.query('today', now=now)
        P = {p['id']: p for p in q['providers']}
        check([p['id'] for p in q['providers']] == ['claude', 'codex', 'agy'] and [p['label'] for p in q['providers']] == ['Claude', 'Codex', 'Antigravity'],
              '三家按 claude、codex、agy 排')
        check([p['scope'] for p in q['providers']] == ['workbench', 'all', 'all'], 'scope：Claude 只算工作台，Codex、agy 全部')
        for rg in ('today', '7d', '30d'):
            qo = u.query(rg, now=now)
            ko = qo['kpi']
            k = next(p for p in hub.query(rg, now=now)['providers'] if p['id'] == 'claude')['kpi']
            check(k == {'requests': ko['requests'], 'turns': None, 'sessions': len({r[1] for r in u.build() if r[0] >= day_start(now, RANGES_D[rg] - 1) * 1000}),
                        'uncachedInput': ko['input'], 'cachedInput': ko['cacheRead'], 'cacheWrite': ko['cacheWrite'], 'output': ko['output'],
                        'reasoning': 0, 'tokens': ko['tokens'], 'cost': ko['cost'], 'costSource': 'modelPriced', 'cacheHit': ko['cacheHit'],
                        'saved': ko['saved'], 'weekQuota': None},
                  f"{rg}：Claude 一栏的 KPI 和 query() 的 kpi 一一对应（请求 {k['requests']}、token {k['tokens']}、费用 {k['cost']}、缓存命中 {k['cacheHit']}）")
        k = P['claude']['kpi']
        check(k['requests'] == fx['claude']['today_requests'] and k['sessions'] == fx['claude']['sessions'] and k['turns'] is None,
              f"Claude 今天：{k['requests']} 条、{k['sessions']} 个会话（只算工作台），轮数为空")
        k = P['codex']['kpi']
        check(k['requests'] == fx['codex']['today_requests'] and k['uncachedInput'] == fx['codex']['today_uncached'] and
              k['cachedInput'] == fx['codex']['today_cached'] and k['output'] == fx['codex']['today_output'] and k['reasoning'] == 20,
              f"Codex KPI：请求 {k['requests']}、未缓存 {k['uncachedInput']}、缓存 {k['cachedInput']}、输出 {k['output']}（分叉复制的不计，工作台的 token 不从 data/conv 重复取）")
        check(k['tokens'] == fx['codex']['today_tokens'] and _close(k['cacheHit'], 19000 / 30000, 1e-4) and k['cost'] is None and
              k['costSource'] == 'unpriced' and k['saved'] is None, f"Codex 缓存命中＝cachedInput÷input（{k['cacheHit']}），不折算钱")
        check(_close(k['weekQuota'], fx['codex']['week_quota_today'], 1e-6), f"Codex 占周额度约 {k['weekQuota']}%（23 − 时段前的 10）")
        check(k['turns'] == fx['codex']['turns_today'] and k['sessions'] == 3, f"Codex 轮数 {k['turns']}、会话 {k['sessions']}")
        k = P['agy']['kpi']
        check(k['requests'] == fx['agy']['today_requests'] and k['uncachedInput'] == 2100 and k['cachedInput'] == 8700 and
              k['output'] == 91 and k['reasoning'] == 10, f"agy KPI：请求 {k['requests']}（备份目录里的同一对话去重；没有库的工作台对话用 turn_end 补）")
        check(_close(k['cacheHit'], 8700 / (2100 + 8700), 1e-4) and _close(k['cost'], 0.03) and k['costSource'] == 'mixed' and k['saved'] is None,
              f"agy 缓存命中＝cache_read÷(input+cache_read)={k['cacheHit']}，费用＝会话累计按轮差 {k['cost']}（{k['costSource']}）")
        check(k['turns'] == fx['agy']['turns_today'], f"agy 轮数 {k['turns']}（库里的用户输入步＋没有库的工作台轮）")
        # usageBuckets
        ub = q['usageBuckets']
        check(all('hourStart' in b and b['hourStart'].startswith(b['day'] + 'T') and b['hourStart'].endswith(':00') for b in ub) and
              {b['provider'] for b in ub} == {'claude', 'codex', 'agy'},
              f'今天的桶按小时（本机时区 hourStart），{len(ub)} 个')
        for p in ('claude', 'codex', 'agy'):
            n = sum(b['records'] for b in ub if b['provider'] == p)
            check(n == P[p]['kpi']['requests'], f'{p} 桶的记录数之和＝KPI 请求数（{n}）')
        for rg in ('today', '7d'):
            qq, qo = hub.query(rg, now=now), u.query(rg, now=now)
            cb = [b for b in qq['usageBuckets'] if b['provider'] == 'claude']
            tot = sum(b['totals']['uncachedInputTokens'] + b['totals']['cachedInputTokens'] + b['totals']['cacheCreationTokens'] + b['totals']['outputTokens'] for b in cb)
            check(sum(b['records'] for b in cb) == qo['kpi']['requests'] and tot == qo['kpi']['tokens'] and
                  _close(round(sum(b['costUsd'] for b in cb), 4), qo['kpi']['cost'], 1e-4) and
                  _close(round(sum(b['cacheSavingsUsd'] for b in cb), 4), qo['kpi']['saved'], 1e-4),
                  f'{rg}：Claude 的分桶（build() 的行）合计和 query() 的 kpi 对得上（{sum(b["records"] for b in cb)} 条）')
        srcs = {(b['provider'], b['model']): b['costSource'] for b in ub}
        check(srcs.get(('codex', 'gpt-6-astra')) == 'unpriced' and srcs.get(('claude', 'claude-opus-5-5')) == 'modelPriced',
              f'桶的 costSource：Codex unpriced、Claude modelPriced')
        bk = next(b for b in ub if b['provider'] == 'codex')
        check(set(bk) == {'day', 'hourStart', 'provider', 'model', 'totals', 'costUsd', 'cacheSavingsUsd', 'costSource', 'records', 'unpricedRecords', 'sessions'}
              and set(bk['totals']) == {'uncachedInputTokens', 'cachedInputTokens', 'cacheCreationTokens', 'outputTokens', 'reasoningTokens'},
              'UsageBucket 字段照 T3 契约')
        q7 = hub.query('7d', now=now)
        check(all('hourStart' not in b for b in q7['usageBuckets']) and
              next(p for p in q7['providers'] if p['id'] == 'codex')['kpi']['requests'] == fx['codex']['week_requests'],
              '7 天按天分桶；Codex 3 天前的旧格式记录（.jsonl 和 .jsonl.zst 两份）只算一次')
        k7 = next(p for p in q7['providers'] if p['id'] == 'codex')['kpi']
        check(_close(k7['weekQuota'], 13.0), f"7 天占周额度：时段内 10→23，约 {k7['weekQuota']}%")
        # provider 过滤
        qc = hub.query('today', provider='codex', now=now)
        check([p['id'] for p in qc['providers']] == ['codex'] and {b['provider'] for b in qc['usageBuckets']} == {'codex'} and
              [s['id'] for s in qc['speed']['providers']] == ['codex'] and all(x['provider'] == 'codex' for x in qc['ranking'] + qc['speed']['recent']),
              'provider=codex 只给 Codex')

        # ---- 2b. 11-23：Codex、agy 的老样式页（legacy）：字段照 usage.py query()，数和新页面那一栏对得上
        old_keys = {'scanning', 'updated', 'range', 'unit', 'kpi', 'today', 'buckets', 'models', 'heat', 'recent', 'sessions'}
        for p in ('codex', 'agy'):
            for rg in ('today', '7d', '30d'):
                lg = hub.legacy(rg, p, now=now)
                kp = next(x for x in hub.query(rg, provider=p, now=now)['providers'] if x['id'] == p)['kpi']
                k = lg['kpi']
                same_cost = (k['cost'] is None and kp['cost'] is None) or (k['cost'] is not None and kp['cost'] is not None and _close(k['cost'], kp['cost'], 1e-4))
                check(set(lg) == old_keys | {'provider', 'heatSince'} and lg['provider'] == p and lg['range'] == rg and
                      k['requests'] == kp['requests'] and k['tokens'] == kp['tokens'] and k['input'] == kp['uncachedInput'] and
                      k['cacheRead'] == kp['cachedInput'] and k['cacheWrite'] == kp['cacheWrite'] and k['output'] == kp['output'] and
                      same_cost and k['cacheHit'] == kp['cacheHit'] and k['saved'] is None,
                      f'{p} {rg}：老样式的 kpi 和新页面这一栏一致（请求 {k["requests"]}、token {k["tokens"]}、费用 {k["cost"]}、缓存命中 {k["cacheHit"]}）')
                bs = lg['buckets']
                check(len(bs) == (24 if rg == 'today' else RANGES_D[rg]) and sum(b['requests'] for b in bs) == k['requests'] and
                      sum(b['tokens'] for b in bs) == k['tokens'] and sum(m['requests'] for m in lg['models']) == k['requests'] and
                      all(sum(v['requests'] for v in b['models'].values()) == b['requests'] for b in bs) and
                      len(lg['recent']) == min(100, k['requests']) and [x['ts'] for x in lg['recent']] == sorted((x['ts'] for x in lg['recent']), reverse=True),
                      f'{p} {rg}：柱子、模型、最近请求的条数合计对得上（{len(bs)} 根柱子）')
            l30 = hub.legacy('30d', p, now=now)
            hs = [h for h in l30['heat'] if h['date'] >= l30['heatSince']]
            check(len(l30['heat']) == 371 and len(hs) == 30 and sum(h['requests'] for h in l30['heat']) == l30['kpi']['requests'],
                  f'{p}：活跃度 371 天，只有 {l30["heatSince"]} 起的 30 天有统计，合计＝30 天请求数')
        lc, la = hub.legacy('today', 'codex', now=now), hub.legacy('today', 'agy', now=now)
        check(lc['kpi']['cost'] is None and lc['today']['cost'] is None and _close(lc['kpi']['weekQuota'], fx['codex']['week_quota_today'], 1e-6) and
              lc['kpi'].get('approxSpeed') is True and all(b['cost'] is None for b in lc['buckets']) and all(s['cost'] is None for s in lc['sessions'])
              and lc['kpi']['pricedRequests'] == 0,
              f"Codex 老样式：不算钱（cost 为 None），占周额度 {lc['kpi']['weekQuota']}% 和新页面同一算法")
        check(_close(la['kpi']['cost'], 0.03) and 'weekQuota' not in la['kpi'] and la['kpi']['requests'] == fx['agy']['today_requests'],
              f"agy 老样式：费用只算提供方报的（{la['kpi']['cost']}）")
        # 复核 11-23：只有部分请求报了费用时，“每次”按报了费用的次数算；没有报费用的桶 cost 为 None（不写 $0.00）
        rawa = hub.query('30d', 'agy', now=now, raw=True)['kept']['agy']
        want_pr = sum(1 for r, s in rawa if s != 'unpriced' and r[0] >= day_start(now) * 1000)
        ab = [b for b in la['buckets'] if b['requests']]
        check(la['kpi']['pricedRequests'] == want_pr and 0 < want_pr < la['kpi']['requests'] and
              all(b['cost'] is None for b in la['buckets'] if not b['requests']) and
              _close(sum(b['cost'] for b in ab if b['cost'] is not None), la['kpi']['cost'], 1e-4) and
              all((v['cost'] is None) or v['cost'] >= 0 for b in ab for v in b['models'].values()),
              f"agy 只有部分请求报了费用：pricedRequests {la['kpi']['pricedRequests']}／{la['kpi']['requests']}，没费用的桶不写 0")
        check(all(s['title'] for s in lc['sessions'] + la['sessions']) and any(s['sid'] for s in lc['sessions']) and any(not s['sid'] for s in lc['sessions']),
              '用量最多的对话：都有标题；工作台里的带工作台 sid（能点开），终端的不带')
        check(all(isinstance(m['label'], str) and m['label'] for m in lc['models'] + la['models']), '模型都有显示名')

        # ---- 3. 速度
        S = {s['id']: s for s in q['speed']['providers']}
        sc = S['claude']
        check(sc['tps'] == q_old['kpi']['speed'] and sc['tps'] is not None and sc['ttft'] is None and sc['turn'] is None and sc['turnP90'] is None and
              sc['approx'] == {'tps': True} and sc['why'].get('ttft') == '只算工作台的 Claude 统计没有记首字时间' and
              sc['why'].get('turn') == '只算工作台的 Claude 统计没有记每轮用时' and 'tps' not in sc['why'],
              f"Claude 速度：输出速度取 query() 的 kpi.speed（{sc['tps']}，约），首字、每轮用时为空并给原因")
        cm = {m['model']: m for m in sc['models']}
        check({m['id']: m['speed'] for m in q_old['models']} == {m: v['tps'] for m, v in cm.items()} and
              all(v['turns'] is None and v['ttft'] is None and v['approx'] == {'tps': True} for v in cm.values()),
              f"Claude 按模型的输出速度取 query() 的 models[].speed（{[(m, v['tps']) for m, v in cm.items()]}）")
        exp_pts = {}
        for r in rows:
            sp = US.Usage._row_speed(r)
            if sp is not None:
                exp_pts.setdefault(datetime.fromtimestamp(r[0] / 1000).strftime('%Y-%m-%d %H'), []).append(sp)
        got_pts = {p['key']: p['claude']['tps'] for p in q['speed']['series']['points'] if 'claude' in p}
        check(got_pts == {k2: round(statistics.median(v), 1) for k2, v in exp_pts.items()} and
              all(p['claude']['ttft'] is None and p['claude']['turn'] is None for p in q['speed']['series']['points'] if 'claude' in p),
              f"Claude 折线：build() 的行按小时用 _row_speed 取中位数（{got_pts}）")
        sx = S['codex']
        check(sx['ttft'] == 4000 and sx['turn'] == 6200 and sx['turnP90'] == 11500 and sx['approx'] == {'tps': True},
              f"Codex 速度：首字中位数 {sx['ttft']}（工作台那轮用 turn_end 的 4000，不用 rollout 的 5900）、每轮 {sx['turn']}、P90 {sx['turnP90']}")
        check(sx['tps'] == 16.7, f"Codex 输出速度：逐次请求 输出÷(本条−请求开始)，中位数 {sx['tps']}（25、50、8.3、6 → 16.7）")
        sa = S['agy']
        check(sa['ttft'] == 3000 and sa['turn'] == 4100 and sa['approx'] == {'tps': True} and sa['tps'] is not None,
              f"agy 速度：工作台的轮用 turn_end（首字 {sa['ttft']}、每轮 {sa['turn']}），输出速度约 {sa['tps']}")
        mx = {m['model']: m for m in sx['models']}
        check('gpt-6-astra' in mx and mx['gpt-6-astra']['label'] == 'GPT-6 Astra' and mx['gpt-6-astra']['turns'] == 3,
              f"按模型：{[(m['model'], m['label'], m['requests'], m['turns']) for m in sx['models']]}")
        ser = q['speed']['series']
        check(ser['unit'] == 'hour' and len(ser['points']) == 24 and ser['points'][0]['label'] == '00:00' and
              any('codex' in p for p in ser['points']) and not any('codex' in p for p in ser['points'][:1] if p['key'] < datetime.fromtimestamp(fx['base']).strftime('%Y-%m-%d %H')),
              '速度折线：今天 24 个点，没数据的点不给那家的键')
        check(len(hub.query('7d', now=now)['speed']['series']['points']) == 168 and
              (lambda s: s['unit'] == 'day' and len(s['points']) == 30)(hub.query('30d', now=now)['speed']['series']),
              '速度折线：7 天 168 个点（按小时），30 天 30 个点（按天）')
        rc = q['speed']['recent']
        check(0 < len(rc) <= 20 and all(rc[i]['ts'] >= rc[i + 1]['ts'] for i in range(len(rc) - 1)) and
              {x['where'] for x in rc} == {'workbench', 'other'} and not any(x['provider'] == 'claude' for x in rc) and
              set(rc[0]) >= {'ts', 'provider', 'model', 'label', 'title', 'ttft', 'turn', 'output', 'tps', 'approx', 'where'},
              f'最近几轮：{len(rc)} 轮，新的在前，分工作台和其他，不列 Claude')
        wbx = next(x for x in rc if x['provider'] == 'codex' and x['where'] == 'workbench')
        check(wbx.get('sid') == S1 and wbx['ttft'] == 4000 and wbx['output'] == 50, f"Codex 工作台的轮：sid={wbx.get('sid')}、首字 {wbx['ttft']}")
        tx = next(x for x in rc if x['provider'] == 'codex' and x['title'] == 'X 的终端对话')
        check('sid' not in tx and tx['ttft'] == 3000 and tx['turn'] == 11500 and tx['output'] == 300 and tx['tps'] == 37.5,
              f"Codex 终端的轮：标题取 session_index，首字取 rollout {tx['ttft']}，输出 {tx['output']}，约 {tx['tps']} token/秒")
        # agy 只有终端：首字、每轮用时取不到，给原因
        hub2 = mk_hub(u, conv=False)
        hub2.start()
        wait_scan(hub2)
        s2 = next(s for s in hub2.query('today', provider='agy', now=now)['speed']['providers'])
        check(s2['ttft'] is None and s2['turn'] is None and '终端' in s2['why'].get('ttft', '') and '终端' in s2['why'].get('turn', ''),
              f"agy 只有终端记录：首字、每轮用时为空，原因“{s2['why'].get('ttft')}”")
        s3 = hub2.query('today', provider='codex', now=day_start(now) / 1 + 86400 * 10)
        check(s3['speed']['providers'][0]['why'].get('tps') == '这段时间没有用过', '没用过的时段：原因是“这段时间没有用过”')

        # ---- 4. 排行和子任务
        rk = q['ranking']
        check(len(rk) <= 10 and all(rk[i]['tokens'] >= rk[i + 1]['tokens'] for i in range(len(rk) - 1)) and
              {x['provider'] for x in rk} == {'claude', 'codex', 'agy'}, f'排行三家合并按 token 降序（{len(rk)} 行）')
        rx = {x['key']: x for x in rk}
        check(rx.get('codex:' + XW, {}).get('sid') == S1 and rx['codex:' + XW]['title'] == '工作台 Codex 提问' and
              'sid' not in rx.get('codex:' + XA, {'sid': 1}) and rx['codex:' + XA]['title'] == 'X 的终端对话' and
              rx.get('claude:' + CA, {}).get('sid') == CA and rx['claude:' + CA]['title'] == '标题' + CA[:4] and
              rx.get('agy:' + GG, {}).get('title') == 'G 的标题' and rx.get('agy:' + GT, {}).get('title') == 'T 的终端对话' and
              rx['codex:' + XA]['cost'] is None and rx['agy:' + GG]['cost'] == 0.03,
              '排行标题：工作台对话用工作台标题并给 sid；Codex 终端用 session_index；agy 用摘要库；Codex 费用为空')
        crk = [{'sid': x['sid'], 'title': x['title'], 'requests': x['requests'], 'tokens': x['tokens'], 'cost': x['cost']} for x in rk if x['provider'] == 'claude']
        check(crk == q_old['sessions'] and len(crk) == 1,
              f'排行里的 Claude 对话就是 query() 的 sessions（工作台对话，{len(crk)} 个）')
        check(q['hasTasks'] is True and not any('children' in x for x in rk), 'tasks.jsonl 有内容 → hasTasks；不勾选时不汇总')
        rr = {x['key']: x for x in hub.query('today', rollup=True, now=now)['ranking']}
        check('codex:' + XW not in rr and rr['claude:' + CA]['children'] == 1 and
              rr['claude:' + CA]['tokens'] == rx['claude:' + CA]['tokens'] + rx['codex:' + XW]['tokens'],
              '子任务算到父对话头上：Codex 子对话的用量加到 Claude 父对话，子对话那行去掉，children=1')
        os.rename(os.path.join(data, 'tasks.jsonl'), os.path.join(data, 'tasks.bak'))
        check(hub.query('today', now=now)['hasTasks'] is False, 'tasks.jsonl 不存在 → hasTasks:false')
        open(os.path.join(data, 'tasks.jsonl'), 'w').close()
        check(hub.query('today', now=now)['hasTasks'] is False, 'tasks.jsonl 为空 → hasTasks:false')
        os.replace(os.path.join(data, 'tasks.bak'), os.path.join(data, 'tasks.jsonl'))

        # ---- 5. 增量续读、尾行、改写
        xpath = fx['codex']['xpath']
        ent = hub.codex.files[xpath]
        off0 = ent['pos']['o']
        x9 = fx['base'] + 420
        un = cx_usage(4000, 3000, 70)
        with open(xpath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cx(x9, 'event_msg', {'type': 'task_started', 'turn_id': 'tX2', 'started_at': int(x9)}), separators=(',', ':')) + '\n')
            f.write(json.dumps(cx(x9, 'turn_context', {'turn_id': 'tX2', 'model': 'gpt-6-astra'}), separators=(',', ':')) + '\n')
            f.write(json.dumps(cx(x9 + 4, 'token_usage_record', {'response_id': 'resp_x3', 'usage': un}), separators=(',', ':'))[:40])  # 半行
        calls = []

        def spy(path, resume=None, side_init=None, tick=None):
            res = saved_read(path, resume, side_init, tick)
            calls.append((os.path.basename(path), bool(resume), isinstance(res, dict) and res['resumed']))
            return res
        UP.read_codex_rollout = spy
        hub.last_ref = 0
        hub.refresh_all()
        ent = hub.codex.files[xpath]
        check(calls == [(os.path.basename(xpath), True, True)] and ent['pos']['o'] > off0 and len(ent['r']) == 2,
              f'增量：只续读变大的文件（{calls}），偏移 {off0}→{ent["pos"]["o"]}，半行不算')
        with open(xpath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cx(x9 + 4, 'token_usage_record', {'response_id': 'resp_x3', 'usage': un}), separators=(',', ':'))[40:] + '\n')
        calls.clear()
        hub.last_ref = 0
        hub.refresh_all()
        k2 = next(p for p in hub.query('today', now=now)['providers'] if p['id'] == 'codex')['kpi']
        check(calls and calls[0][2] and k2['requests'] == 5 and k2['output'] == 380 + 70, f'半行写完后续读计入：请求 {k2["requests"]}、输出 {k2["output"]}')
        # 尾行（没有换行的完整一行）当场算，补上换行后不重复
        with open(xpath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cx(x9 + 8, 'token_usage_record', {'response_id': 'resp_x4', 'usage': cx_usage(100, 0, 5)}), separators=(',', ':')))
        hub.last_ref = 0
        hub.refresh_all()
        k3 = next(p for p in hub.query('today', now=now)['providers'] if p['id'] == 'codex')['kpi']
        with open(xpath, 'a', encoding='utf-8') as f:
            f.write('\n')
        hub.last_ref = 0
        hub.refresh_all()
        k4 = next(p for p in hub.query('today', now=now)['providers'] if p['id'] == 'codex')['kpi']
        check(k3['requests'] == 6 and k4['requests'] == 6 and len(hub.codex.files[xpath]['t']) == 0,
              f'没写完换行的尾行当场算（{k3["requests"]}），补上换行后不重复（{k4["requests"]}）')
        # 改写（续读偏移前 64 字节里的内容变了，T3 只核对这一段）：整文件重读
        o_now = hub.codex.files[xpath]['pos']['o']
        with open(xpath, 'r+b') as f:
            f.seek(o_now - 20)
            ch = f.read(1)
            f.seek(o_now - 20)
            f.write(b'9' if ch != b'9' else b'8')
        with open(xpath, 'a', encoding='utf-8') as f:
            f.write('\n')
        calls.clear()
        hub.last_ref = 0
        hub.refresh_all()
        check(calls and calls[0][1] and not calls[0][2], f'偏移前 64 字节变了：不续读，整文件重读（{calls}）')
        UP.read_codex_rollout = saved_read

        # ---- 6. 缓存：新实例读缓存不重读
        cache = os.path.join(tmp, 'run', 'usage-p-cache.json')
        hub.cache_path = lambda: cache
        hub.save_cache(force=True)
        hub3 = mk_hub(u, cache=cache)
        calls.clear()
        UP.read_codex_rollout = spy
        hub3.start()
        wait_scan(hub3)
        UP.read_codex_rollout = saved_read
        a, b = hub.query('today', now=now), hub3.query('today', now=now)
        check(not calls and a['providers'] == b['providers'] and a['usageBuckets'] == b['usageBuckets'],
              f'缓存：新实例读缓存后不重读 rollout（读了 {len(calls)} 个），结果一致')

        # ---- 7. .zst：没有标准库时用 zstd 命令行；都没有就跳过并计入 skipped
        for mode in ('cli', 'none'):
            if mode == 'cli' and not shutil.which('zstd'):
                continue
            UP.ZSTD_MODE = mode
            h = mk_hub(u)
            h.start()
            wait_scan(h)
            kz = next(p for p in h.query('7d', now=now)['providers'] if p['id'] == 'codex')['kpi']
            if mode == 'cli':
                check(kz['requests'] == fx['codex']['week_requests'] + 2 and h.codex.skipped == 0 and fx['codex']['ypath'] + '.zst' in h.codex.files,
                      f'.zst 用 zstd -dc 解压（请求 {kz["requests"]}）')
            else:
                check(h.codex.skipped == 1 and fx['codex']['ypath'] + '.zst' not in h.codex.files and kz['requests'] == fx['codex']['week_requests'] + 2,
                      f'.zst 没法解压：跳过计入 skipped={h.codex.skipped}，同一会话的 .jsonl 照算（请求 {kz["requests"]}）')
        UP.ZSTD_MODE = saved_zmode

        # ---- 8. 首扫期间接口不阻塞（Claude、Codex、agy 都在扫）
        def slow_read(path, resume=None, side_init=None, tick=None):
            time.sleep(0.6)
            return saved_read(path, resume, side_init, tick)
        UP.read_codex_rollout = slow_read
        orig_read_db = UP.agy_read_db
        UP.agy_read_db = lambda *a: (time.sleep(0.6), orig_read_db(*a))[1]
        ucl = mk_claude()
        orig_parse = ucl._parse
        ucl._parse = lambda *a: (time.sleep(0.4), orig_parse(*a))[1]
        h = mk_hub(ucl)
        ucl.start()
        h.start()
        time.sleep(0.1)
        t0 = time.time()
        qs = h.query('today', now=now)
        dt = time.time() - t0
        t1 = time.time()
        q0 = ucl.query('today', now=now)
        dt0 = time.time() - t1
        ps = {p['id']: p for p in qs['providers']}
        check(dt < 1.0 and dt0 < 1.0 and ps['codex']['scanning'] and ps['agy']['scanning'] and ps['claude']['scanning'] and q0['scanning'],
              f'首扫期间接口不阻塞：三家汇总 {dt * 1000:.0f}ms、Claude 原接口 {dt0 * 1000:.0f}ms，三家都标“正在扫描”')
        check(all(set(ps[p].get('progress') or {}) == {'files', 'totalFiles', 'bytes', 'totalBytes'} for p in ('codex', 'agy')) and
              ps['codex']['progress']['totalFiles'] >= 4 and 'progress' not in ps['claude'],
              f"扫描中带进度（Codex、agy；Claude 的第十四轮扫描没有进度）：{ps['codex'].get('progress')}")
        wait_scan(h, ucl, 30)
        UP.read_codex_rollout = saved_read
        UP.agy_read_db = orig_read_db
        qd = h.query('today', now=now)
        check(not any(p['scanning'] for p in qd['providers']) and all('progress' not in p for p in qd['providers']) and
              next(p for p in qd['providers'] if p['id'] == 'claude')['kpi']['requests'] == fx['claude']['today_requests'],
              '扫完后不再标扫描中、不带进度，数据完整')

        # ---- 9. 根目录：CODEX_HOME、ANTIGRAVITY_DATA_DIR
        alt = os.path.join(tmp, 'altcodex')
        shutil.copytree(os.path.join(home, '.codex'), alt)
        h = UP.Hub(home=os.path.join(tmp, 'nohome'), env={'CODEX_HOME': alt, 'ANTIGRAVITY_DATA_DIR': os.path.join(home, '.gemini', 'antigravity-cli')})
        check(h.codex_root() == os.path.join(alt, 'sessions') and h.agy_dirs()[0] == os.path.realpath(os.path.join(home, '.gemini', 'antigravity-cli', 'conversations'))
              and h.agy_dirs()[-1].endswith(os.path.join('antigravity-cwb')), f'根目录：CODEX_HOME、ANTIGRAVITY_DATA_DIR 生效，另加 antigravity-cwb（{h.agy_dirs()}）')
        h = UP.Hub(home=home, env={})
        check(h.codex_root() == os.path.join(home, '.codex', 'sessions') and
              any(d.endswith(os.path.join('antigravity-cwb', 'conversations')) for d in h.agy_dirs()), '默认根目录在 HOME 下')
        # 只读：agy 库旁边没留下 -wal / -shm
        left = [n for n in os.listdir(os.path.join(home, '.gemini', 'antigravity-cli', 'conversations')) if n.endswith(('-wal', '-shm'))]
        check(not left, f'只读打开 agy 的库，没留下 -wal/-shm（{left}）')

        # ---- 10. 移植正确性的几个点（T3 测试里的情形）
        st = UP.initial_codex_state()
        recs = []
        for line in [cx(1, 'session_meta', {'id': 's'}), cx(2, 'turn_context', {'turn_id': 't', 'model': 'm'}),
                     cx(3, 'event_msg', {'type': 'token_count', 'info': {'total_token_usage': cx_usage(10, 0, 1), 'last_token_usage': cx_usage(10, 0, 1)}}),
                     cx(3, 'event_msg', {'type': 'token_count', 'info': {'total_token_usage': cx_usage(10, 0, 1), 'last_token_usage': cx_usage(10, 0, 1)}}),
                     cx(4, 'event_msg', {'type': 'token_count', 'info': {'total_token_usage': cx_usage(30, 0, 3), 'last_token_usage': cx_usage(20, 0, 2)}}),
                     cx(5, 'event_msg', {'type': 'token_count', 'info': {'total_token_usage': cx_usage(5, 0, 1), 'last_token_usage': cx_usage(5, 0, 1)}})]:
            recs += UP.parse_codex_record(json.loads(json.dumps(line)), st)
        check([r[UP.R_U] for r in recs] == [10, 20, 5], f'Codex 状态机：重复快照不重计、续接后累计重新开始（{[r[UP.R_U] for r in recs]}）')
        check(UP.read_codex_token_usage({'input_tokens': 5, 'output_tokens': 1, 'cached_input_tokens': 9}) is None and
              UP.read_codex_token_usage({'input_tokens': 5, 'output_tokens': 1, 'cached_input_tokens': None}) is None and
              UP.read_codex_token_usage({'input_tokens': 5.0, 'output_tokens': 1})['total_tokens'] == 6, 'readCodexTokenUsage 的校验')
        check(UP.agy_model_name('Claude 4.5 Sonnet (Thinking)') == 'claude-sonnet-4-5-thinking' and
              UP.agy_model_name('Gemini 3.8 Flash (Low)') == 'gemini-3.8-flash-low' and UP.agy_is_concrete('gemini-3.8-flash') and
              not UP.agy_is_concrete('gemini-3-flash-agent'), 'agy 模型名换算（T3 modelName / isConcreteModel）')
        check(UP.fnv1a(b'hello') == 0x4f9f2cab, 'fnv1a 和 T3 一致')
        check(UP.week_quota([[10, 'codex', 5.0, 1], [20, 'codex', 9.0, 1], [30, 'codex', 1.0, 2], [40, 'codex', 4.0, 2], [35, 'other', 50.0, 3]], 15, 50) == 7.0,
              '占周额度：按重置时间分组，每组 最大 − 时段前最后一个（没有用最小），相加')
        check(UP.week_quota([], 0, 10) is None, '占周额度：没有样本为 None')

        # ---- 11. 挂到 server 上：USAGE 不动；usage、usage-summary 原样只加新字段；第一次调用时才懒启动 Codex、agy 的扫描
        import server as S
        base_keys = {'scanning', 'updated', 'range', 'unit', 'kpi', 'today', 'buckets', 'models', 'heat', 'recent', 'sessions'}
        check(type(S.USAGE) is US.Usage and 'start' not in vars(S.USAGE) and S.USAGE.cache_path == os.path.join(S.RUN_DIR, 'usage-cache.json'),
              'server 的 USAGE 没被替换、没被包（还是 server 自己建的那个对象）')
        old_ns, old_hub = UP.NS, UP.HUB
        try:
            u5 = mk_claude()
            api = {'usage': lambda b: u5.query(str(b.get('range') or 'today')), 'usage-summary': lambda b: u5.summary()}
            ns = {'API': api, 'USAGE': u5, 'HOME': home, 'RUN_DIR': os.path.join(tmp, 'run2'), 'log': lambda *a: None,
                  'usage_titles': lambda sids: {x: '标题' + x[:4] for x in sids}, 'load_conf': lambda: {}, 'model_label': S.model_label}
            before = dict(vars(u5))
            UP.setup(ns)
            check(ns['USAGE'] is u5 and vars(u5) == before and UP.HUB.started is False,
                  'setup：不换、不改 USAGE；导入时不扫任何目录（Codex、agy 还没开始）')
            sm = api['usage-summary']({})
            check(sm == u5.summary() and set(sm) == {'scanning', 'today'} and UP.HUB.started is True,
                  f'usage-summary 返回和原来一样（{sm}），第一次调用时后台启动扫描')
            wait_scan(UP.HUB, None, 20)
            d = api['usage']({'range': 'today', 'provider': 'all'})
            d0 = u5.query('today')
            check(set(d) == base_keys | {'providers', 'usageBuckets', 'speed', 'ranking', 'hasTasks'} and
                  all(d[k2] == d0[k2] for k2 in base_keys - {'updated'}),
                  'usage 接口：原有字段和直接调 query() 一模一样，只多出新字段')
            d2 = api['usage']({'range': '7d', 'provider': 'agy', 'rollup': True})
            check([p['id'] for p in d2['providers']] == ['agy'] and d2['range'] == '7d' and len(d2['buckets']) == 7 and d2['kpi'] == u5.query('7d')['kpi'],
                  'usage 接口：provider 只过滤新字段，原字段仍是 Claude')
            check(next(p for p in d['providers'] if p['id'] == 'claude')['kpi']['requests'] == d['kpi']['requests'],
                  '接口里 Claude 一栏直接用原函数已算好的 query() 结果')
            d3 = api['usage']({'range': 'today', 'legacy': 'codex'})
            kq = next(p for p in UP.HUB.query('today', provider='codex')['providers'] if p['id'] == 'codex')['kpi']
            check(set(d3) == base_keys | {'provider', 'heatSince'} and d3['provider'] == 'codex' and d3['kpi']['requests'] == kq['requests'] and
                  d3['kpi']['tokens'] == kq['tokens'], f'usage 接口带 legacy=codex：只出 Codex 的老样式字段（请求 {d3["kpi"]["requests"]}），不带 Claude 的')
            d4 = api['usage']({'range': '7d', 'legacy': 'nope'})
            check(set(d4) == base_keys | {'providers', 'usageBuckets', 'speed', 'ranking', 'hasTasks'}, 'legacy 不是 codex、agy 时照原样返回')
        finally:
            UP.NS, UP.HUB = old_ns, old_hub

        # ---- 12. 换了 HOME、共用同一个 .run 缓存：别的根目录留下的条目不能算进来（走接口的路径，用真实当前时间，不传 now）
        import providers as PV
        old_ns, old_hub, old_conv, old_data = UP.NS, UP.HUB, PV.CONV_DIR, PV.DATA_DIR
        try:
            run_dir = os.path.join(tmp, 'run-shared')

            def via_api(h, d):
                PV.CONV_DIR, PV.DATA_DIR = os.path.join(d, 'conv'), d
                uu = US.Usage(os.path.join(h, '.claude', 'projects'), cache_path=os.path.join(run_dir, 'usage-cache.json'))
                api = {'usage': lambda b: uu.query(str(b.get('range') or 'today')), 'usage-summary': lambda b: uu.summary()}
                UP.setup({'API': api, 'USAGE': uu, 'HOME': h, 'RUN_DIR': run_dir, 'log': lambda *a: None, 'usage_titles': lambda sids: {},
                          'load_conf': lambda: {}, 'model_label': S.model_label})
                api['usage-summary']({})
                wait_scan(UP.HUB, None, 20)
                return {rg: {p['id']: p['kpi']['requests'] for p in api['usage']({'range': rg})['providers']} for rg in ('today', '7d')}
            h2, d2 = os.path.join(tmp, 'home-a'), os.path.join(tmp, 'data-a')
            h3, d3 = os.path.join(tmp, 'home-b'), os.path.join(tmp, 'data-b')
            fa = build_fixtures(h2, d2)
            ra = via_api(h2, d2)
            with open(os.path.join(run_dir, 'usage-p-cache.json'), encoding='utf-8') as f:
                cached = json.load(f)
            fb = build_fixtures(h3, d3)
            rb = via_api(h3, d3)
            want_t = {'claude': fb['claude']['today_requests'], 'codex': fb['codex']['today_requests'], 'agy': fb['agy']['today_requests']}
            want_w = {'claude': fb['claude']['week_requests'], 'codex': fb['codex']['week_requests'], 'agy': fb['agy']['today_requests']}
            check(any(k.startswith(h2) for k in cached['codex']) and ra['today'] == {'claude': fa['claude']['today_requests'],
                  'codex': fa['codex']['today_requests'], 'agy': fa['agy']['today_requests']},
                  f'第一个 HOME：今天 {ra["today"]}，缓存里记下了它的 rollout')
            check(rb['today'] == want_t and rb['7d'] == want_w,
                  f'换 HOME、共用缓存后：今天 {rb["today"]}（应为 {want_t}）、7 天 {rb["7d"]}（应为 {want_w}），上一个 HOME 的缓存条目不算')
        finally:
            UP.NS, UP.HUB, PV.CONV_DIR, PV.DATA_DIR = old_ns, old_hub, old_conv, old_data
    finally:
        UP.ZSTD_MODE, UP.read_codex_rollout = saved_zmode, saved_read
        shutil.rmtree(tmp, ignore_errors=True)


def measure_real(cache_dir):
    """真实目录只读性能测量（不在 run() 里跑）：缓存写到 cache_dir。返回 Codex、agy 首扫、二次增量的耗时和峰值内存（Claude 用第十四轮原样，不测）。"""
    import resource
    import usage_p as UP
    out = {}
    home = os.path.expanduser('~')
    rss = lambda: resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1048576  # macOS：字节 → MB  # noqa: E731
    h = UP.Hub(home=home, cache_path=os.path.join(cache_dir, 'usage-p-cache.json'), claude=None)
    for name, fn in (('codex', h._refresh_codex), ('agy', h.agy.refresh)):
        m0, t0 = rss(), time.time()
        fn()
        t1 = time.time()
        fn()
        out[name] = {'first_s': round(t1 - t0, 2), 'second_s': round(time.time() - t1, 3), 'rss_mb_after': round(rss(), 1)}
    out['codex']['files'] = len(h.codex.files)
    out['codex']['skipped'] = h.codex.skipped
    out['agy']['dbs'] = len(h.agy.dbs)
    t0 = time.time()
    h.save_cache(force=True)
    out['p_save_s'] = round(time.time() - t0, 2)
    try:
        out['p_cache_mb'] = round(os.path.getsize(os.path.join(cache_dir, 'usage-p-cache.json')) / 1048576, 1)
    except OSError:
        pass
    t0 = time.time()
    h2 = UP.Hub(home=home, cache_path=os.path.join(cache_dir, 'usage-p-cache.json'), claude=None)
    h2.load_cache()
    t1 = time.time()
    h2._refresh_codex()
    h2.agy.refresh()
    out['p_restart'] = {'load_s': round(t1 - t0, 2), 'refresh_s': round(time.time() - t1, 3)}
    return out


if __name__ == '__main__':
    import atexit
    if 'CWB_DATA_DIR' not in os.environ:
        _dd = tempfile.mkdtemp(prefix='cwb-r17u-data-')
        os.environ['CWB_DATA_DIR'] = _dd
        atexit.register(shutil.rmtree, _dd, True)
    os.environ.setdefault('CWB_PORT', '47969')
    os.environ.setdefault('CWB_QUOTA_NO_LOOP', '1')
    FAIL = []

    def check(cond, msg):
        print(('通过 ' if cond else '失败 ') + msg, flush=True)
        if not cond:
            FAIL.append(msg)
    if len(sys.argv) > 2 and sys.argv[1] == 'measure':
        print(json.dumps(measure_real(sys.argv[2]), ensure_ascii=False, indent=1))
        sys.exit(0)
    import server  # noqa: F401  顺带执行 usage_p.setup，确认挂得上
    run(check)
    print(f'\n共 {len(FAIL)} 项失败' if FAIL else '\n全部通过')
    sys.exit(1 if FAIL else 0)
