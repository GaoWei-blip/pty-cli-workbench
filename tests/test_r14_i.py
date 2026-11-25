"""第十四轮（开发线 I）单元测试：用量统计（usage.py）、模型清单、对话选项合并、Fast 注入、偏好。
不启动 claude、不碰 ~/.claude：记录文件都造在临时目录里。运行：.venv/bin/python -I tests/test_r14_i.py
"""
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usage as U  # noqa: E402
import server as S  # noqa: E402

FAIL = []
WB = '/Users/demo/claude-workbench/.venv/bin/python -I /Users/demo/claude-workbench/hook.py idle'
TEST_HOOK = '/Users/demo/cwb-dev/r14/i/.venv/bin/python -I /Users/demo/cwb-dev/r14/i/hook.py idle'


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{int(t * 1000) % 1000:03d}Z'


class W:
    """按时间顺序写一份记录文件。"""

    def __init__(self, path, sid):
        self.path, self.sid = path, sid
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def row(self, d):
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(d, ensure_ascii=False, separators=(',', ':')) + '\n')

    def prompt(self, t, text='问题'):
        self.row({'parentUuid': None, 'isSidechain': False, 'type': 'user', 'message': {'role': 'user', 'content': text},
                  'uuid': f'u{t}', 'timestamp': iso(t), 'sessionId': self.sid})

    def tool_result(self, t):
        self.row({'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}]},
                  'uuid': f'tr{t}', 'timestamp': iso(t), 'sessionId': self.sid})

    def req(self, t, rid):
        self.row({'type': 'api-request', 'sessionId': self.sid, 'id': rid, 'timestamp': iso(t)})

    def asst(self, t, mid, out, model='claude-opus-5-5', inp=10, cr=1000, cw=100, ref=None, speed='standard', fork=False, req=None):
        d = {'parentUuid': 'p', 'isSidechain': False,
             'message': {'model': model, 'id': mid, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'text', 'text': '答'}],
                         'usage': {'input_tokens': inp, 'cache_creation_input_tokens': cw, 'cache_read_input_tokens': cr,
                                   'output_tokens': out, 'speed': speed}},
             'requestId': req or ('req_' + mid), 'type': 'assistant', 'uuid': f'a{t}{mid}', 'timestamp': iso(t), 'sessionId': self.sid}
        if ref:
            d['requestRef'] = ref
        if fork:
            d['forkedFrom'] = {'sessionId': 'src'}
        self.row(d)

    def summary(self, t, cmd=WB):
        self.row({'parentUuid': 'p', 'isSidechain': False, 'type': 'system', 'subtype': 'stop_hook_summary', 'hookCount': 1,
                  'hookInfos': [{'command': cmd}], 'hookErrors': [], 'timestamp': iso(t), 'uuid': f's{t}', 'sessionId': self.sid})


def test_price():
    c = U.cost_of
    check(abs(c('claude-opus-5-5', 1_000_000, 0, 0, 0) - 4) < 1e-9 and abs(c('claude-opus-5-5', 0, 1_000_000, 0, 0) - 20) < 1e-9,
          '价目：Opus 5.5 输入 $4、输出 $20（每百万）')
    check(abs(c('claude-opus-5-5', 0, 0, 1_000_000, 0) - 0.2) < 1e-9 and abs(c('claude-opus-5-5', 0, 0, 0, 1_000_000) - 5) < 1e-9,
          '价目：Opus 5.5 缓存读 $0.20、缓存写按 1.25 倍输入价 $5')
    check(abs(c('claude-opus-5-5', 1_000_000, 1_000_000, 0, 0, True) - 48) < 1e-9, 'Fast：Opus 5.5 按 2 倍（CLI 价目表 8/40 对 4/20）')
    check(abs(c('claude-opus-4-6', 1_000_000, 0, 0, 0, True) - 30) < 1e-9, 'Fast：Opus 4.6 按 6 倍（CLI 价目表 30/150 对 5/25）')
    check(abs(c('claude-sonnet-5', 0, 0, 1_000_000, 0, True) - 0.4) < 1e-9, 'Fast：价目表里没有的按 2 倍')
    check(abs(c('claude-opus-4-5-20251101', 1_000_000, 0, 1_000_000, 0) - 5.5) < 1e-9, '价目：Opus 4.5（带日期的 ID）$5，缓存读按 0.1 倍')
    check(abs(c('claude-haiku-5-5', 100_000, 1_000_000, 0, 0) - (0.01 + 0.5)) < 1e-9, '价目：Haiku 5.5 提示 ≤10 万 token 按 $0.10/$0.50')
    check(abs(c('claude-haiku-5-5', 50_000, 1_000_000, 60_000, 0) - (0.025 + 2.5 + 0.003)) < 1e-9, '价目：Haiku 5.5 提示超过 10 万按 $0.50/$2.50，缓存读按 0.1 倍')
    check(abs(c('claude-fable-5', 0, 0, 1_000_000, 0) - 1) < 1e-9 and abs(c('claude-fable-5-1', 0, 0, 1_000_000, 0) - 0.25) < 1e-9,
          '价目：Fable 5 缓存读 $1，Fable 5.1 缓存读 $0.25')
    check(abs(c('claude-opus-9-1', 1_000_000, 0, 0, 0) - 4) < 1e-9 and abs(c('claude-fable-7', 1_000_000, 0, 0, 0) - 8) < 1e-9,
          '价目：未知版本按同系列最新价（Opus → 5.5，Fable → 5.2）')
    check(c('gpt-x', 1_000_000, 1_000_000, 0, 0) == 0, '价目：认不出系列的模型不估算')
    check(abs(c('claude-sonnet-4-6', 1_000_000, 0, 0, 1_000_000) - (3 + 3.75)) < 1e-9, '价目：Sonnet 4.6 $3，缓存写 $3.75')


def build_fixture(root, base):
    """造一组记录。base 为第一行的时间（秒）。返回各会话 ID。"""
    proj = os.path.join(root, '-tmp-proj')
    A, B, C = 'aaaaaaaa-0000-0000-0000-000000000001', 'bbbbbbbb-0000-0000-0000-000000000002', 'cccccccc-0000-0000-0000-000000000003'
    a = W(os.path.join(proj, A + '.jsonl'), A)
    # 第 1 轮：一条流式写两行的回复（去重，输出取最大），一条工具调用后的回复；工作台收尾
    a.prompt(base)
    a.req(base + 1, 'R1')
    a.asst(base + 5, 'm1', 2, ref='R1')
    a.asst(base + 6, 'm1', 100, ref='R1')
    a.tool_result(base + 7)
    a.asst(base + 9, 'm2', 50)
    a.asst(base + 9.5, 'msyn', 5, model='<synthetic>')
    a.summary(base + 10)
    # 第 2 轮被中断（没有收尾记录），第 3 轮工作台收尾：第 2 轮的回复并进第 3 轮
    a.prompt(base + 60)
    a.asst(base + 62, 'm3', 30)
    a.prompt(base + 120)
    a.asst(base + 122, 'm4', 40, speed='fast')
    a.summary(base + 125)
    # 第 4 轮还没有收尾记录：只有落在工作台运行时间段内才算
    a.prompt(base + 180)
    a.asst(base + 182, 'm5', 60)
    # 子代理：第 1 轮里启动（后台跑到这一轮结束以后）→ 整个文件算工作台；第 4 轮里启动 → 逐条按时间段判断
    s1 = W(os.path.join(proj, A, 'subagents', 'agent-x1.jsonl'), A)
    s1.row({'type': 'user', 'isSidechain': True, 'message': {'role': 'user', 'content': '子任务'}, 'timestamp': iso(base + 3), 'sessionId': A})
    s1.asst(base + 4, 'k1', 11, model='claude-haiku-5-5')
    s1.asst(base + 300, 'k2', 12, model='claude-haiku-5-5')
    s2 = W(os.path.join(proj, A, 'subagents', 'workflows', 'wf_1', 'agent-x2.jsonl'), A)
    s2.row({'type': 'user', 'isSidechain': True, 'message': {'role': 'user', 'content': '子任务'}, 'timestamp': iso(base + 181), 'sessionId': A})
    s2.asst(base + 183, 'k3', 13, model='claude-haiku-5-5')
    # B：终端里跑的（收尾记录不是工作台的钩子）；子代理也不算
    b = W(os.path.join(proj, B + '.jsonl'), B)
    b.prompt(base + 20)
    b.asst(base + 22, 'm6', 70)
    b.summary(base + 23, cmd='callback')
    sb = W(os.path.join(proj, B, 'subagents', 'agent-y.jsonl'), B)
    sb.row({'type': 'user', 'isSidechain': True, 'message': {'role': 'user', 'content': '子任务'}, 'timestamp': iso(base + 21), 'sessionId': B})
    sb.asst(base + 21.5, 'k4', 14)
    # C：测试副本的钩子不算；从 A 分叉复制来的 m1（带 forkedFrom）只算一次，归 A
    c = W(os.path.join(proj, C + '.jsonl'), C)
    c.asst(base + 6, 'm1', 100, ref='R1', fork=True)
    c.prompt(base + 200)
    c.asst(base + 202, 'm7', 80)
    c.summary(base + 203, cmd=TEST_HOOK)
    return proj, A, B, C


def outs(rows):
    return sorted(r[4] for r in rows)


def test_usage():
    root = tempfile.mkdtemp(prefix='cwb-r14i-')
    try:
        now = time.time()
        base = now - 3 * 86400  # 三天前，落在 7 天里
        proj, A, B, C = build_fixture(root, base)
        cache = os.path.join(root, 'run', 'usage-cache.json')
        runs = os.path.join(root, 'run', 'usage-runs.jsonl')
        u = U.Usage(root, cache_path=cache, runs_path=runs, titles=lambda sids: {s: '标题' + s[:4] for s in sids})
        u.start()
        for _ in range(100):
            if not u.scanning:
                break
            time.sleep(0.05)
        rows = u.build()
        o = outs(rows)
        # 计入：m1(100) m2(50) m3(30，中断轮并进下一轮) m4(40) k1(11) k2(12)；不计：m5(60，无时间段) k3(13) m6(70) k4(14) m7(80) synthetic
        check(o == [11, 12, 30, 40, 50, 100], f'归属：工作台收尾的轮次、并进下一轮的中断轮、第 1 轮启动的子代理计入，其余不计（{o}）')
        check(sum(1 for r in rows if r[4] == 100) == 1, '去重：流式写两行的 m1 只算一次，输出取最大；分叉复制的那份不重复计')
        m1 = next(r for r in rows if r[4] == 100)
        check(m1[1] == A, '去重：分叉复制的回复归原会话（没有 forkedFrom、文件更早）')
        check(m1[8] == 5000, f'耗时：requestRef 对上 api-request，末行时间减发请求时间＝{m1[8]}ms')
        m2 = next(r for r in rows if r[4] == 50)
        check(m2[8] == 2000, f'耗时：没有 api-request 时用前一行（工具结果）的时间＝{m2[8]}ms')
        check(next(r for r in rows if r[4] == 40)[7] is True, 'Fast：message.usage.speed=fast 的回复标记 fast')
        check(u.stats.get('sessions_wb') == 1 and u.stats.get('sessions_all') == 3, f"会话数：工作台 {u.stats.get('sessions_wb')} / 全部 {u.stats.get('sessions_all')}")

        q = u.query('7d', now=now)
        k = q['kpi']
        check(k['requests'] == 6 and k['output'] == 243 and k['tokens'] == k['input'] + k['output'] + k['cacheRead'] + k['cacheWrite'],
              f"7 天 KPI：请求 {k['requests']}、输出 {k['output']}、tokens＝四项之和")
        check(abs(k['cacheHit'] - k['cacheRead'] / (k['input'] + k['cacheRead'] + k['cacheWrite'])) < 1e-3 and 0 <= k['cacheHit'] <= 1,
              f"缓存命中率 {k['cacheHit']}")
        sp = {x['output']: x['speed'] for x in q['recent']}
        exp_sp = round(statistics.median([v for v in sp.values() if v is not None]), 1)
        check(sp[100] == 20.0 and sp[50] == 25.0 and k['speed'] == exp_sp, f"速度：每条＝输出 token / 耗时（m1 {sp[100]}、m2 {sp[50]} tok/s），整体取中位数 {k['speed']}")
        exp = sum(U.cost_of(r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows)
        check(abs(k['cost'] - round(exp, 4)) < 1e-6, f"费用 ${k['cost']}")
        check(len(q['buckets']) == 7 and q['unit'] == 'day' and q['buckets'][-1]['key'] == U.day_key(now, 0)
              and sum(x['requests'] for x in q['buckets']) == 6, '柱状图：7 天按天 7 个，最后一个是今天')
        check(len(q['heat']) == 371 and q['heat'][-1]['date'] == U.day_key(now, 0) and sum(x['requests'] for x in q['heat']) == 6,
              '活跃度：最近 371 天，与范围无关')
        check([m['id'] for m in q['models']] == ['claude-opus-5-5', 'claude-haiku-5-5'] and abs(sum(m['share'] for m in q['models']) - 1) < 1e-3,
              f"模型：按 tokens 降序，占比合计 1（{[(m['label'], m['requests']) for m in q['models']]}）")
        check(len(q['recent']) == 6 and q['recent'][0]['ts'] >= q['recent'][-1]['ts'] and q['recent'][0]['title'] == '标题' + A[:4]
              and set(q['recent'][0]) == {'ts', 'sid', 'title', 'model', 'label', 'fast', 'input', 'output', 'cacheRead', 'cacheWrite', 'ms', 'speed', 'cost'},
              '最近请求：新的在前，带标题和各字段')
        check(q['sessions'] == [{'sid': A, 'title': '标题' + A[:4], 'requests': 6, 'tokens': k['tokens'], 'cost': k['cost']}], '对话排行：只有工作台的会话 A')
        qt = u.query('today', now=now)
        check(len(qt['buckets']) == 24 and qt['unit'] == 'hour' and qt['kpi']['requests'] == 0 and qt['today']['requests'] == 0,
              '今天：按小时 24 个，三天前的不算今天')
        sm = u.summary(now=now)
        check(set(sm) == {'scanning', 'today'} and sm['today'] == {'requests': 0, 'tokens': 0, 'cost': 0}, f'usage-summary：{sm}')

        # 兜底时间段：第 4 轮（无收尾）落在工作台运行时间段内才计入；第 4 轮里启动的子代理逐条判断
        U.log_run(runs, 'start', 'g1', A, t=base + 170, w=base + 170)
        U.log_run(runs, 'end', 'g1', A, t=base + 190, w=base + 190)
        rows = u.build()
        check(outs(rows) == [11, 12, 13, 30, 40, 50, 60, 100], f'兜底：无收尾的 m5 与第 4 轮启动的子代理 k3 落在时间段内计入（{outs(rows)}）')
        # 有了时间段记录以后，并进下一轮的中断轮也要落在时间段内
        os.remove(runs)
        U.log_run(runs, 'start', 'g0', A, t=base + 30, w=base + 30)
        U.log_run(runs, 'end', 'g0', A, t=base + 40, w=base + 40)
        U.log_run(runs, 'start', 'g1', A, t=base + 115, w=base + 115)
        U.log_run(runs, 'end', 'g1', A, t=base + 190, w=base + 190)
        u.runs_sig = None
        rows = u.build()
        check(30 not in outs(rows) and 40 in outs(rows), f'兜底：有时间段记录后，不在工作台时间段里的中断轮（m3）不计（{outs(rows)}）')
        U.log_run(runs, 'start', 'g2', A, t=base + 55, w=base + 55)
        U.log_run(runs, 'end', 'g2', A, t=base + 65, w=base + 65)
        rows = u.build()
        check(30 in outs(rows), '兜底：中断轮落在工作台时间段里就计入')
        os.remove(runs)
        u.runs_sig = None

        # 增量：追加一轮，只解析新增的行；缓存里的结果和全量一致
        u.save_cache(force=True)
        a_path = os.path.join(proj, A + '.jsonl')
        off0 = u.files[a_path]['off']
        a = W(a_path, A)
        a.prompt(base + 400)
        a.asst(base + 402, 'm8', 77)
        with open(a_path, 'ab') as f:
            f.write(b'{"type":"assistant","message":{"model":"claude-opus-5-5","id":"m9"')  # 还没写完的半行
        rows = u.query('7d', now=now)
        e = u.files[a_path]
        check(e['off'] > off0 and e['off'] < os.path.getsize(a_path) and 77 not in outs(u.build()),
              f'增量：从偏移 {off0} 接着读到 {e["off"]}，半行不读；新的一轮还没收尾，先不计')
        with open(a_path, 'ab') as f:
            f.write(b',"type":"message","role":"assistant","content":[],"usage":{"input_tokens":1,"output_tokens":88}},"requestId":"r9","type":"assistant","timestamp":"'
                    + iso(base + 403).encode() + b'","sessionId":"' + A.encode() + b'"}\n')
        a.summary(base + 404)
        u.query('7d', now=now)
        o2 = outs(u.build())
        check(77 in o2 and 88 in o2, f'增量：半行写完、这一轮收尾后两条都计入（{o2}）')
        u.save_cache(force=True)
        u2 = U.Usage(root, cache_path=cache, runs_path=runs)
        t0 = time.time()
        u2.start()
        for _ in range(100):
            if not u2.scanning:
                break
            time.sleep(0.02)
        check(outs(u2.build()) == o2 and u2.stats.get('first_scan_s', 9) < 0.5, f'缓存：新实例读缓存后结果一致，扫描 {time.time() - t0:.3f}s')
        # 文件被改小（重写）：从头解析
        with open(a_path, 'r+b') as f:
            data = f.read()
            f.seek(0)
            f.truncate()
            f.write(data[:data.index(b'"m3"')].rsplit(b'\n', 1)[0] + b'\n')
        u2.refresh()
        o3 = outs(u2.build())
        check(o3 == [11, 12, 50, 100], f'文件变小：从头重新解析（{o3}）')
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_runs():
    d = tempfile.mkdtemp(prefix='cwb-r14i-runs-')
    try:
        p = os.path.join(d, 'r.jsonl')
        U.log_run(p, 'start', 'k0', 's0', t=500, w=1000)  # 接管升级前就在跑的 CLI：t 是它的启动时间，w 是写下的时间
        U.log_run(p, 'start', 'k1', 's1', t=1000, w=1000)
        U.log_run(p, 'sid', 'k1', 's2', t=1100, w=1100)
        U.log_run(p, 'end', 'k1', 's2', t=1200, w=1200)
        U.log_run(p, 'start', 'k2', 's3', t=2000, w=2000)
        since, win = U.load_runs(p, live_keys=['k2'], now=3000)
        check(since == 1000_000 and win['s0'][0][0] == 495_000 and win['s1'] == [(995_000, 1_205_000)] and win['s2'] == win['s1'] and win['s3'] == [(1_995_000, 3_005_000)],
              '时间段：从最早写下记录的时间起算；/clear 换的新 ID 共用同一段；还在跑的算到现在')
        since, win = U.load_runs(p, live_keys=[], now=200000)
        check(win['s3'] == [(1_995_000, (2000 + 12 * 3600) * 1000 + 5000)], '时间段：没有结束记录、CLI 也不在了，最多算 12 小时')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_server_opts():
    m = S.merge_opts({'model': 'claude-opus-5-5', 'effort': 'low', 'fast': True}, {'effort': 'medium'}, {'fast': False, 'mode': 'plan mode'})
    check(m == {'model': 'claude-opus-5-5', 'effort': 'medium', 'mode': 'plan mode'}, f'选项合并：defaults < 项目 < 显式，fast=False 能覆盖（{m}）')
    check(S.merge_opts({'mode': 'plan mode', 'model': 'x'}, {'mode': 'default', 'model': ''}) == {'model': 'x'}, '选项合并：default 清掉下层的值，空值不覆盖')
    check(S.opts_key_of({'model': 'm', 'fast': True}) != S.opts_key_of({'model': 'm'}), '预热匹配的 key 含 fast')
    m = S.merge_opts(S.merge_opts({'model': None, 'effort': '', 'mode': None, 'fast': None}), {'model': 'p', 'effort': 'high', 'mode': 'plan mode', 'fast': True},
                     {'model': None, 'effort': '', 'fast': None})
    check(m == {'model': 'p', 'effort': 'high', 'mode': 'plan mode', 'fast': True}, f'选项合并：偏好里存的 null、空串和 fast:null 都算未设置，不盖项目选项（{m}）')
    check(S.merge_opts({'fast': True}, {'fast': 'false'}) == {'fast': True} and S.merge_opts({'fast': True}, {'fast': False}) == {}, '选项合并：fast 只认布尔值')
    orig = S.user_settings
    S.user_settings = lambda: {'env': {}}
    try:
        s1 = S.build_settings({'model': 'claude-opus-5-5', 'fast': True})
        s2 = S.build_settings({'model': 'claude-sonnet-5-5', 'fast': True})
        s3 = S.build_settings({'fast': True})
        s4 = S.build_settings({'model': 'claude-opus-4-8', 'fast': False})
        # 第十四轮补（11-11）：Fast 功能去掉（公司网关返回 402），任何情况下都不注入 fastMode
        check('hooks' in s1 and not any('fastMode' in x for x in (s1, s2, s3, s4)), 'Fast 已去掉：任何模型、任何选项都不注入 fastMode')
        S.user_settings = lambda: {'env': {'ANTHROPIC_MODEL': 'claude-sonnet-5-5'}}
        check('fastMode' not in S.build_settings({'fast': True}), 'Fast 注入：默认模型是 Sonnet 时不注入')
        S.user_settings = lambda: {'env': {'ANTHROPIC_MODEL': 'opus', 'ANTHROPIC_DEFAULT_OPUS_MODEL': 'claude-opus-4-8'}}
        check('fastMode' not in S.build_settings({'fast': True}) and S.cli_default_model() == 'claude-opus-4-8', '别名 opus 按 ANTHROPIC_DEFAULT_OPUS_MODEL 解析')
    finally:
        S.user_settings = orig


def test_models_prefs():
    d = tempfile.mkdtemp(prefix='cwb-r14i-conf-')
    orig_conf, orig_us = S.CONF_PATH, S.user_settings
    S.CONF_PATH = os.path.join(d, 'config.json')
    S.user_settings = lambda: {'env': {'ANTHROPIC_DEFAULT_SONNET_MODEL': 'claude-sonnet-9-9'}}
    try:
        ml = S.model_list()
        rec = [(x['id'], x['label'], x['desc']) for x in ml['list'] if x['group'] == 'rec']
        check(rec == [('claude-opus-5-5', 'Opus 5.5', '日常主力，复杂任务首选'), ('claude-fable-5-1', 'Fable 5.1', '最强推理，适合难题，费用较高'),
                      ('claude-sonnet-5-5', 'Sonnet 5.5', '更快更省，适合大多数改动'), ('claude-haiku-5-5', 'Haiku 5.5', '最快，适合简单任务')],
              '模型清单：推荐 4 个，顺序、标签、说明按约定')
        more = [x['id'] for x in ml['list'] if x['group'] == 'more']
        check(more[:11] == ['claude-fable-5-2', 'claude-opus-5', 'claude-sonnet-5', 'claude-fable-5', 'claude-opus-4-8', 'claude-opus-4-7',
                            'claude-opus-4-6', 'claude-sonnet-4-6', 'claude-opus-4-5-20251101', 'claude-haiku-4-5-20251001', 'opusplan']
              and 'claude-sonnet-9-9' in more, f'模型清单：更多 11 个 + settings.json 里的 ANTHROPIC_DEFAULT_*_MODEL（{more[11:]}）')
        lab = {x['id']: x['label'] for x in ml['list']}
        check(lab['opusplan'] == 'Opus 规划 + Sonnet 执行' and lab['claude-opus-4-5-20251101'] == 'Opus 4.5' and lab['claude-haiku-4-5-20251001'] == 'Haiku 4.5',
              '模型标签：opusplan 单独处理，日期去掉')
        check(sorted(x['id'] for x in ml['list'] if x['fast']) == ['claude-opus-4-8', 'claude-opus-5', 'claude-opus-5-5'], '模型清单：fast 只标 opus-5 与 opus-4-8')
        check(ml['defaultId'] == 'claude-opus-5-5' and ml['default'] == 'Opus 5.5', '默认模型：没有偏好和 ANTHROPIC_MODEL 时取第一个推荐')
        S.user_settings = lambda: {'env': {'ANTHROPIC_MODEL': 'claude-custom-1'}}
        ml = S.model_list()
        check(ml['defaultId'] == 'claude-custom-1' and any(x['id'] == 'claude-custom-1' and x['group'] == 'more' for x in ml['list']),
              '默认模型：ANTHROPIC_MODEL 其次；清单外的放进“更多”')
        r = S.api_prefs({'set': {'defaults': {'model': 'claude-sonnet-5-5', 'fast': True, 'mode': 'plan mode'}, 'defaultCwd': '/tmp/x/',
                                 'extraModels': ['claude-x-1', 'claude-x-1', ' claude-x-2 ']}})['prefs']
        check(r == {'defaults': {'model': 'claude-sonnet-5-5', 'effort': None, 'mode': 'plan mode', 'fast': True}, 'defaultCwd': '/tmp/x',
                    'extraModels': ['claude-x-1', 'claude-x-2']}, f'prefs：合并保存（{r}）')
        check(S.load_conf().get('prefs', {}).get('defaults', {}).get('model') == 'claude-sonnet-5-5', 'prefs：存在 config.json 的 prefs 下')
        ml = S.model_list()
        check(ml['defaultId'] == 'claude-sonnet-5-5' and [x['id'] for x in ml['list']][-2:] == ['claude-x-1', 'claude-x-2'],
              '默认模型：prefs.defaults.model 优先；extraModels 进“更多”')
        check(S.pref_defaults() == {'model': 'claude-sonnet-5-5', 'mode': 'plan mode', 'fast': True}, 'prefs：转成对话选项')
        for bad in ({'defaults': {'effort': 'turbo'}}, {'defaults': {'mode': 'yolo'}}, {'defaultCwd': 'rel/path'}, {'extraModels': ['a b']}):
            try:
                S.api_prefs({'set': bad})
                check(False, f'prefs 校验：{bad} 应报错')
            except ValueError:
                pass
        r = S.api_prefs({'set': {'defaults': {'mode': 'default', 'fast': False}}})['prefs']
        check(r['defaults'] == {'model': 'claude-sonnet-5-5', 'effort': None, 'mode': None, 'fast': False}, 'prefs：部分字段更新，default 清空')
        r = S.api_prefs({'set': {'defaults': {'model': None, 'effort': None, 'mode': None, 'fast': None}}})['prefs']
        S.api_project_opts({'cwd': '/tmp/cwb-r14i-x', 'opts': {'effort': 'max', 'model': 'claude-opus-5'}})
        m = S.merge_opts(S.pref_defaults(), S.project_opts('/tmp/cwb-r14i-x'), {})
        check(r['defaults'] == {'model': None, 'effort': None, 'mode': None, 'fast': False} and S.pref_defaults() == {}
              and m == {'model': 'claude-opus-5', 'effort': 'max'}, f'prefs：设置里选“默认”存成 null，合并时不传给 CLI、不盖项目选项（{m}）')
    finally:
        S.CONF_PATH, S.user_settings = orig_conf, orig_us
        shutil.rmtree(d, ignore_errors=True)


def test_rule_re():
    check(bool(S.RULE_RE.match('─' * 60)) and bool(S.RULE_RE.match('─' * 60 + ' ↯ ─')) and not S.RULE_RE.match('──── 正文'),
          '输入框上沿：Fast 开着时带“ ↯ ─”也认得出')
    check(bool(S.UA_RE.match('claude-cli/2.1.321 (external, cli)')) and not S.UA_RE.match('claude-cli/2.1.321 (external, sdk-ts)'),
          '自检判定：只认 claude-cli/<版本> (external, cli)')
    check(S.mask_token('cr_7abcdefghijklmnop') == 'cr_7' + '•' * 10 and S.mask_token('') == '', '令牌打码：只露前 4 位')


if __name__ == '__main__':
    test_price()
    test_usage()
    test_runs()
    test_server_opts()
    test_models_prefs()
    test_rule_re()
    print('全部通过' if not FAIL else f'{len(FAIL)} 项失败：' + '；'.join(FAIL))
