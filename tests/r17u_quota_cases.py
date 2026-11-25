"""第十七轮 U：额度（quota_hub.py）的单元测试用例，由 tests/test_r17_U.py 调用 run(check)。
不启动 claude；Codex 用假 app-server（tests/fakecodex.py app-server），agy 用假命令（tests/fakeagy，放进 PATH）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, '.venv', 'bin', 'python')


def E(k, v):
    """测试替身的开关：子进程的环境取自 server 的 LOGIN_ENV，所以两边都写。"""
    import server as S
    for env in (os.environ, S.LOGIN_ENV):
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v


def run(check):
    import server as S
    import providers as PV
    import prov_codex as PC
    import quota_hub as QH
    tmp = tempfile.mkdtemp(prefix='cwb-r17u-q-')
    saved = {k: os.environ.get(k) for k in ('CWB_CODEX_CMD', 'CWB_CODEX_HOME', 'CWB_CODEX_ARGS', 'FAKECX_RL', 'FAKECX_LOG', 'FAKECX_PROVIDER',
                                            'FAKEAGY_MODE', 'FAKEAGY_LOG', 'CWB_AGY_BIN')}
    old_home, old_env, old_run = S.HOME, dict(S.LOGIN_ENV), S.RUN_DIR
    events = []
    old_emit = S.BUS.emit
    S.BUS.emit = lambda ev: events.append(ev)
    old_ad = dict(PV.ADAPTERS)
    try:
        S.HOME = os.path.join(tmp, 'home')
        S.RUN_DIR = os.path.join(tmp, 'run')
        os.makedirs(os.path.join(S.HOME, '.claude'))
        bindir = os.path.join(tmp, 'bin')
        os.makedirs(bindir)
        os.symlink(os.path.join(ROOT, 'tests', 'fakeagy'), os.path.join(bindir, 'agy'))
        S.LOGIN_ENV.clear()
        S.LOGIN_ENV.update({'PATH': bindir + ':/usr/bin:/bin', 'HOME': S.HOME})  # 先清空，下面 E() 再往里加替身开关
        cxhome = os.path.join(tmp, 'codexhome')
        os.makedirs(cxhome)
        with open(os.path.join(cxhome, 'auth.json'), 'w') as f:
            f.write('{}')
        os.environ['CWB_CODEX_HOME'] = cxhome
        os.environ['CWB_CODEX_CMD'] = json.dumps([PY, '-I', os.path.join(ROOT, 'tests', 'fakecodex.py'), 'app-server'])
        os.environ.pop('CWB_CODEX_ARGS', None)
        cxlog = os.path.join(tmp, 'fakecx.jsonl')
        E('FAKECX_LOG', cxlog)
        agylog = os.path.join(tmp, 'fakeagy.jsonl')
        E('FAKEAGY_LOG', agylog)
        os.environ.pop('CWB_AGY_BIN', None)
        PV.ADAPTERS['codex'] = PC.CodexAdapter()
        for f in (QH.codex_file(), QH.agy_file()):
            if os.path.exists(f):
                os.remove(f)
        QH._state.clear(); QH._last_try.clear(); QH._codex_raw.clear(); QH._sig.clear()

        t_pace(check, QH)
        t_claude(check, S, QH)
        t_codex(check, S, PV, QH, cxlog)
        t_agy(check, S, QH, agylog)
        t_api(check, S, QH, events)
        t_alerts(check, S, QH, events)
        t_redline(check)
    finally:
        S.BUS.emit = old_emit
        S.HOME, S.RUN_DIR = old_home, old_run
        S.LOGIN_ENV.clear(); S.LOGIN_ENV.update(old_env)
        PV.ADAPTERS.clear(); PV.ADAPTERS.update(old_ad)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def t_pace(check, QH):
    now = 1_800_000_000_000
    w = {'usedPercent': 60, 'windowMins': 300, 'resetsAt': now + 150 * 60000}   # 过去一半时间
    check(abs(QH.elapsed_share(w, now) - 0.5) < 1e-9, 'T3 elapsedShare：窗口过去一半＝0.5')
    check(QH.pace_of(w, now) == 'fast', '节奏：已用 60% 比时间进度 50% 高 10 个点 → 偏快')
    check(QH.pace_of(dict(w, usedPercent=54), now) == 'ok' and QH.pace_of(dict(w, usedPercent=44), now) == 'slow',
          '节奏：差 4 个点适中、低 6 个点偏慢（T3 ±5）')
    check(QH.pace_of({'usedPercent': 10, 'windowMins': 300}, now) is None and QH.elapsed_share({'usedPercent': 1, 'resetsAt': now}, now) is None,
          '没有重置时间或窗口长度：不算匀速线和节奏')
    check(QH.elapsed_share({'windowMins': 300, 'resetsAt': now - 1000}, now) == 1.0, '重置时间已过：已过去比例夹到 1')
    check(QH.clamp_percent(130) == 100 and QH.clamp_percent(-3) == 0 and QH.clamp_percent('x') == 0, 'clampPercent')


def t_claude(check, S, QH):
    f = QH.claude_file()
    p = QH.get('claude')
    check(not p['ok'] and p['unavailable']['reason'] == 'none' and '状态栏脚本' in p['unavailable']['text'], f'Claude：没有额度文件 → {p.get("unavailable")}')
    now = time.time()
    with open(f, 'w') as fh:
        json.dump({'ts': int(now), 'w': [{'lbl': '5h', 'u': 30, 'r': int(now + 3600), 't': 18000},
                                         {'lbl': '7d', 'u': 72, 'r': int(now + 86400), 't': 604800},
                                         {'lbl': 'Fable', 'u': 91, 'r': int(now + 86400), 't': 604800}]}, fh)
    p = QH.get('claude')
    ids = [w['id'] for w in p['windows']]
    check(p['ok'] and ids == ['five_hour', 'seven_day', 'seven_day_fable'], f'Claude：crs 文件转成 T3 的窗口 id、按种类排序（{ids}）')
    w5 = p['windows'][0]
    check(w5['kind'] == '5h' and w5['label'] == '5 小时' and w5['windowMins'] == 300 and w5['resetsAt'] == int(now + 3600) * 1000
          and 'elapsed' in w5 and w5['pace'] in ('fast', 'ok', 'slow'), f'Claude：5 小时窗口带重置时间（毫秒）、匀速线、节奏 {w5}')
    wf = p['windows'][2]
    check(wf['kind'] == 'model' and wf['label'] == '每周 · Fable' and wf['group'] == 'Fable', f'Claude：按模型的每周额度 {wf["label"]}')
    check(QH.tightest('claude') == {'usedPercent': 91.0, 'label': '每周 · Fable', 'resetsAt': int(now + 86400) * 1000}, f'X1 tightest：{QH.tightest("claude")}')
    with open(f, 'w') as fh:
        json.dump({'ts': int(now), 'w': [], 'err': 'key_not_found'}, fh)
    p = QH.get('claude')
    check(not p['ok'] and p['unavailable']['text'] == '网关查询失败' and len((p.get('lastGood') or {}).get('windows') or []) == 3,
          'Claude：状态栏脚本报错 → 卡片写“网关查询失败”，保留上次的三条窗口')
    check(QH.tightest('claude') is None, 'X1 tightest：读不到时返回 None（M 线照常委派、写“额度未知”）')
    with open(f, 'w') as fh:
        json.dump({'ts': int(now) - 1200, 'w': [{'lbl': '5h', 'u': 10, 'r': int(now - 60), 't': 18000}]}, fh)
    p = QH.get('claude')
    check(p['ok'] and p['stale'], 'Claude：文件 20 分钟没更新 → 可能已过时')
    check(QH.tightest('claude') is None, 'tightest：已过重置时间的窗口不算')


def t_codex(check, S, PV, QH, cxlog):
    a = PV.ADAPTERS['codex']
    p = QH.get('codex')
    check(not p['ok'] and p['unavailable']['reason'] == 'none', f'Codex：还没读过 → {p["unavailable"]}')
    E('FAKECX_RL', 'ok')
    t0 = time.time()
    p = QH.get('codex', max_age=0)
    starts = [json.loads(l) for l in open(cxlog)] if os.path.exists(cxlog) else []
    meths = [x.get('method') for x in starts if x.get('method')]
    check(p['ok'] and [w['id'] for w in p['windows']] == ['primary', 'secondary'], f'Codex：进程不在 → 临时进程读到两条窗口 {[w.get("label") for w in p["windows"]]}')
    check(meths == ['initialize', 'config/read', 'account/rateLimits/read'], f'临时进程只做 initialize、config/read、account/rateLimits/read（{meths}）')
    st = [x for x in starts if 'start' in x]
    check(st and st[-1]['start'][-4:] == ['-c', 'mcp_servers={}', '-c', 'features.plugins=false'], '临时进程不带 MCP 服务和插件')
    pid = st[-1]['pid'] if st else 0
    time.sleep(0.3)
    alive = subprocess.run(['ps', '-p', str(pid)], capture_output=True).returncode == 0 if pid else True
    check(not alive and a.rpc is None, '临时进程读完就结束，不登记到适配器（不算进忙闲判断）')
    w = p['windows'][0]
    check(w['kind'] == '5h' and w['label'] == '5 小时' and w['usedPercent'] == 42 and p['windows'][1]['kind'] == 'week',
          'T3 codexRateLimitsToWindows：300 分钟 → 5 小时，10080 → 每周')
    check(all(x.get('kind') != 'model' for x in p['windows']), '按模型的额度（limitId 不是 codex）不进主窗口（T3）')
    check(p.get('plan') == 'pro' and p.get('resetCredits', {}).get('availableCount') == 1 and p['resetCredits'].get('nextExpiresAt'),
          'Codex：套餐、重置券张数和最近过期时间（只显示）')
    doc = json.load(open(QH.codex_file()))
    check(doc.get('windows') and doc.get('lastGood'), 'Codex：结果写进 data/quota-codex.json（P 线同一个文件，带 lastGood）')
    check(not p.get('viaProxy'), '没走代理时不标注')
    # -32600：这种登录方式没有额度，保留上次的值
    E('FAKECX_RL', 'unsupported')
    p = QH.get('codex', max_age=0)
    check(not p['ok'] and p['unavailable'] == {'reason': 'unsupported', 'text': '这种登录方式没有额度'} and len(p['lastGood']['windows']) == 2,
          'Codex：-32600 → “这种登录方式没有额度”，保留上次的窗口')
    E('FAKECX_RL', 'ok')
    E('FAKECX_PROVIDER', 'custom')
    p = QH.get('codex', max_age=0)
    check(p['ok'] and p.get('viaProxy'), 'Codex：config 里换了 model_provider → 标“账号额度，和实际用量可能不符”')
    E('FAKECX_PROVIDER', None)
    # 超时：最多等 20 秒（测试把单个请求超时调成 1 秒）
    E('FAKECX_RL', 'slow')
    old_to = QH.CODEX_TIMEOUT
    QH.CODEX_TIMEOUT = 1
    t0 = time.time()
    p = QH.get('codex', max_age=0)
    check(time.time() - t0 < 6 and not p['ok'] and p['unavailable']['reason'] == 'probeFailed' and p.get('lastGood'),
          f'Codex：不回额度 → 超时后记“这次没读到”，保留上次的值（{time.time() - t0:.1f}s）')
    QH.CODEX_TIMEOUT = old_to
    E('FAKECX_RL', 'ok')
    # 没登录：不为查额度起进程
    n0 = sum(1 for l in open(cxlog) if '"start"' in l)
    os.remove(os.path.join(os.environ['CWB_CODEX_HOME'], 'auth.json'))
    a.st_cache = None
    p = QH.get('codex', max_age=0)
    n1 = sum(1 for l in open(cxlog) if '"start"' in l)
    check(n1 == n0 and p['unavailable']['text'] == 'Codex 还没登录', 'Codex：没登录时不起进程，写“Codex 还没登录”')
    with open(os.path.join(os.environ['CWB_CODEX_HOME'], 'auth.json'), 'w') as f:
        f.write('{}')
    a.st_cache = None
    # 进程在：用它读（read_quota），不另起临时进程
    class FakeRpc:
        pid = 1
        started = time.time()

        def alive(self):
            return True

        def request(self, method, params, timeout=60):
            assert method == 'account/rateLimits/read', method
            return {'rateLimits': {'limitId': 'codex', 'planType': 'plus', 'primary': {'usedPercent': 7, 'windowDurationMins': 10080,
                                                                                         'resetsAt': int(time.time()) + 3600}}}
    a.rpc = FakeRpc()
    QH._codex_raw.clear()
    p = QH.get('codex', max_age=0)
    n2 = sum(1 for l in open(cxlog) if '"start"' in l)
    check(n2 == n1 and p['ok'] and p.get('plan') == 'plus' and p['windows'][0]['kind'] == 'week' and p['windows'][0]['usedPercent'] == 7,
          'Codex：提供方进程在就用它读 account/rateLimits/read（不起临时进程）')
    a.rpc = None


def t_agy(check, S, QH, agylog):
    E('FAKEAGY_MODE', 'ok')
    p = QH.get('agy', max_age=0)
    labels = [w['label'] for w in p['windows']]
    check(p['ok'] and labels == ['Gemini · 5 小时', 'Gemini · 每周', 'Claude 与 GPT（共享）· 5 小时', 'Claude 与 GPT（共享）· 每周'],
          f'agy：/quota 两组×两窗口，Gemini 在前（{labels}）')
    gw = next(w for w in p['windows'] if w['id'] == 'gemini-weekly')
    check(abs(gw['usedPercent'] - 85) < 1e-6 and gw['kind'] == 'week' and gw['windowMins'] == 10080 and gw.get('resetsAt'),
          '已用％＝(1−remaining_fraction)×100')
    tw = next(w for w in p['windows'] if w['id'] == '3p-5h')
    check('resetsAt' not in tw and 'pace' not in tw, 'T3 summaryResetTime：没动过的桶不给重置时间（不画匀速线）')
    calls = [json.loads(l) for l in open(agylog)]
    check(calls[-1]['argv'] == ['-p', '/quota', '--output-format', 'json'] and calls[-1]['cwd'].endswith('quota-agy-cwd'),
          'agy：命令行默认数据目录（不带 --app-data-dir），在空目录里运行')
    d = json.load(open(QH.agy_file()))
    check(d.get('id') == 'agy' and d.get('windows') and d.get('lastGood'), 'agy：结果写 data/quota-agy.json（X1 的 P 结构）')
    E('FAKEAGY_MODE', 'apikey')
    p = QH.get('agy', max_age=0)
    check(not p['ok'] and p['unavailable'] == {'reason': 'unsupported', 'text': '这种登录方式没有额度'} and len(p['lastGood']['windows']) == 4,
          'agy：退出码 2（API key 登录）→ “这种登录方式没有额度”，保留上次的值')
    E('FAKEAGY_MODE', 'bad')
    p = QH.get('agy', max_age=0)
    check(not p['ok'] and p['unavailable']['reason'] == 'probeFailed', 'agy：输出读不懂 → 这次没读到')
    E('FAKEAGY_MODE', 'timeout')
    old = QH.AGY_TIMEOUT
    QH.AGY_TIMEOUT = 1.5
    t0 = time.time()
    p = QH.get('agy', max_age=0)
    time.sleep(0.3)
    left = [json.loads(l)['pid'] for l in open(agylog)][-1]
    alive = subprocess.run(['ps', '-p', str(left)], capture_output=True).returncode == 0
    check(time.time() - t0 < 5 and p['unavailable']['text'] == 'Antigravity 没有及时返回额度' and not alive,
          f'agy：超时 → 结束整个进程组、记“没有及时返回”（{time.time() - t0:.1f}s，进程还在={alive}）')
    QH.AGY_TIMEOUT = old
    E('FAKEAGY_MODE', 'ok')
    QH.get('agy', max_age=0)


def t_api(check, S, QH, events):
    d = S.API['quota']({})
    check({'ok', 'err', 'ts', 'age', 'stale', 'windows'} <= set(d) and isinstance(d['windows'], list), '原有字段不变（第十四轮界面还能用）')
    check([p['id'] for p in d['providers']] == ['claude', 'codex', 'agy'], 'quota：providers 三家，顺序 Claude、Codex、Antigravity')
    d2 = S.API['quota']({'provider': 'agy'})
    check([p['id'] for p in d2['providers']] == ['agy'] and d2['tightest'] == d['tightest'], 'quota：provider 过滤只影响 providers，tightest 总看三家')
    t = d['tightest']
    check(t and t['provider'] == 'agy' and abs(t['usedPercent'] - 85) < 1e-6 and t['label'] == 'Gemini · 每周',
          f'tightest：三家里最紧的窗口带是哪家（{t}）')
    events.clear()
    QH._last_try.clear()
    S.API['quota']({'provider': 'agy', 'refresh': True})
    for _ in range(50):
        if any(e.get('type') == 'quota' for e in events):
            break
        time.sleep(0.1)
    ev = [e for e in events if e.get('type') == 'quota']
    check(ev and len(ev[-1]['providers']) == 3 and ev[-1].get('tightest'), '打开额度页（refresh）→ 后台读完推 quota 事件')
    n = len(open(os.environ['FAKEAGY_LOG']).readlines())
    S.API['quota']({'provider': 'agy', 'refresh': True})
    time.sleep(0.5)
    check(len(open(os.environ['FAKEAGY_LOG']).readlines()) == n, '60 秒内再打开不重复读')
    # 定时读取只在有窗口连接时
    S.BUS.clients = []
    check(not QH.active(), '没有窗口连接、协作没开 → 不定时读')
    QH.KEEP_ACTIVE.append(lambda: True)
    check(QH.active(), '协作线登记的开关打开 → 定时读')
    QH.KEEP_ACTIVE.clear()


def t_alerts(check, S, QH, events):
    now = QH.now_ms()
    ps = [{'id': 'codex', 'label': 'Codex', 'ok': True, 'windows': [
        {'id': 'secondary', 'label': '每周', 'kind': 'week', 'usedPercent': 85, 'windowMins': 10080, 'resetsAt': now + 3 * 3600000},
        {'id': 'primary', 'label': '5 小时', 'kind': '5h', 'usedPercent': 75, 'windowMins': 300, 'resetsAt': now + 4 * 3600000},   # 才过 1/5 就用了 75%
        {'id': 'x', 'label': '每月', 'kind': 'month', 'usedPercent': 50, 'windowMins': 43200, 'resetsAt': now + 20 * 86400000}]}]
    if os.path.exists(QH.alerts_path()):
        os.remove(QH.alerts_path())
    out = QH.check_alerts(ps, now)
    texts = [e['text'] for e in out]
    check(len(out) == 2 and texts[0].startswith('Codex · 每周：剩余 15%') and '后重置' in texts[0] and '偏快' in texts[1],
          f'预警：≥80% 一条、偏快且剩余不到 30% 一条、其余不发（{texts}）')
    check(QH.check_alerts(ps, now + 60000) == [], '同一个重置周期只发一次（记在 .run/quota-alerts.json）')
    ps[0]['windows'][0]['resetsAt'] += 7 * 86400000
    check(len(QH.check_alerts(ps, now + 120000)) == 1, '重置周期变了再发一次')


def t_redline(check):
    bad = []
    for f in ('quota_hub.py', 'tests/fakecodex.py', 'tests/r17u_quota_cases.py'):
        for i, line in enumerate(open(os.path.join(ROOT, f), encoding='utf-8'), 1):
            code = line.split('#', 1)[0]
            if ('rateLimitResetCredit' + '/') in code and ('con' + 'sume') in code:
                bad.append(f'{f}:{i}')
    check(not bad, f'红线：代码里不调用兑换重置券的方法（{bad}）')
