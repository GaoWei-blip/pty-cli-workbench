"""第十五轮 E 单元测试：定时任务（下次运行时间、补跑判断、历史记录、旧数据兼容、保持唤醒）；设置文件里的钩子与权限规则解析和打码；
插件命令行拼装（空跑开关，不执行任何改动）；插件列表只读读取（真实 claude plugin list --json，不发模型请求）。
不启动对话、不碰 ~/.claude 的写入：配置、历史、设置文件都造在临时目录里。运行：.venv/bin/python -I tests/test_r15_e.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S  # noqa: E402
import features as F  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def dt(s):
    return datetime.strptime(s, '%Y-%m-%d %H:%M')


def fmt(x):
    return x.strftime('%m-%d %H:%M') if x else None


class Tmp:
    """把配置文件、历史文件指到临时目录，测完还原。"""

    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix='cwb-r15e-')
        self.orig = (S.CONF_PATH, S.SCHED_HIST, S.start_scheduled, S.is_trusted)
        S.CONF_PATH = os.path.join(self.d, 'config.json')
        S.SCHED_HIST = os.path.join(self.d, 'sched-history.json')
        S.SCHED_ACTIVE.clear()
        return self

    def __exit__(self, *a):
        S.CONF_PATH, S.SCHED_HIST, S.start_scheduled, S.is_trusted = self.orig
        S.SCHED_ACTIVE.clear()
        shutil.rmtree(self.d, ignore_errors=True)


# ---------------------------------------------------------------- 定时任务：时刻计算
def test_slots():
    now = dt('2026-11-13 10:00')  # 周五
    cases = [
        ({'time': '09:00', 'days': 'daily'}, '11-13 09:00', '11-14 09:00', '旧数据 days=daily：上一次今天 9 点，下一次明天 9 点'),
        ({'time': '09:00', 'days': 'weekdays'}, '11-13 09:00', '11-16 09:00', '旧数据 days=weekdays：周五之后的下一次是周一'),
        ({'freq': 'daily', 'time': '11:30'}, '11-12 11:30', '11-13 11:30', '每天 11:30：还没到点，上一次是昨天'),
        ({'freq': 'weekly', 'weekdays': [0, 2], 'time': '09:00'}, '11-11 09:00', '11-16 09:00', '每周一、三：上一次周三，下一次周一'),
        ({'freq': 'weekly', 'weekdays': [4], 'time': '10:00'}, '11-13 10:00', '11-20 10:00', '每周五 10:00：正好到点算上一次'),
        ({'freq': 'hourly', 'minute': 30}, '11-13 09:30', '11-13 10:30', '每小时 :30'),
        ({'freq': 'hourly', 'minute': 0}, '11-13 10:00', '11-13 11:00', '每小时 :00 正好到点'),
        ({'freq': 'interval', 'every': 5, 'time': '09:00', 'anchor': '2026-11-12'}, '11-13 10:00', '11-13 15:00', '每 5 小时从昨天 9 点起：9、14、19、0、5、10'),
        ({'freq': 'interval', 'every': 1, 'time': '09:15', 'anchor': '2026-11-13'}, '11-13 09:15', '11-13 10:15', '每 1 小时'),
        ({'freq': 'interval', 'every': 4, 'time': '20:00', 'anchor': '2026-11-13'}, None, '11-13 20:00', '自定义间隔还没到第一次：只有下一次'),
        ({'freq': 'weekly', 'weekdays': [], 'time': '09:00'}, None, None, '每周但没选星期几：没有时刻'),
        ({'freq': 'daily', 'time': '25:00'}, None, None, '时间不合法：没有时刻'),
    ]
    for t, p, n, msg in cases:
        a, b = S.sched_slots(t, now)
        check((fmt(a), fmt(b)) == (p, n), f'{msg}（得到 {fmt(a)} / {fmt(b)}）')
    check(S.sched_freq({'days': 'weekdays'}) == 'weekdays' and S.sched_freq({}) == 'daily' and S.sched_freq({'freq': 'x', 'days': 'daily'}) == 'daily',
          '没有 freq 的旧数据按 days 判断频率')


def test_due():
    now = dt('2026-11-13 12:00')
    ts = now.timestamp()
    base = {'id': 'a', 'freq': 'daily', 'time': '09:00', 'since': ts - 30 * 86400}
    d = S.sched_due(dict(base), now)
    check(d and d[1] == 'catchup' and fmt(datetime.fromtimestamp(d[0])) == '11-13 09:00', '9 点没跑、12 点检查：补跑今天 9 点这一次')
    d = S.sched_due(dict(base, catchup=False), now)
    check(d and d[1] == 'missed', '关掉补跑：记为已错过，不运行')
    d = S.sched_due(dict(base, last_slot=dt('2026-11-13 09:00').timestamp()), now)
    check(d is None, '这个时刻已经处理过（last_slot）就不再触发')
    d = S.sched_due(dict(base, last_run='2026-11-13 09:00'), now)
    check(d is None, '旧数据只有 last_run（到分钟）：同一分钟的时刻算处理过')
    d = S.sched_due(dict(base, last_run='2026-11-13 08:50'), now)
    check(d and d[1] == 'catchup', '早于时刻的手动运行不挡住这一次')
    d = S.sched_due(dict(base, since=dt('2026-11-13 10:00').timestamp()), now)
    check(d is None, '新建或改过排程之后的时刻才算：建在 10 点，不补跑 9 点')
    d = S.sched_due(dict(base), dt('2026-11-13 09:00'))
    check(d and d[1] == 'auto', '正好到点：按时运行')
    d = S.sched_due(dict(base), dt('2026-11-13 09:50'))
    check(d and d[1] == 'auto', '每天的任务到点后 60 分钟内仍算按时（和旧版一致）')
    d = S.sched_due(dict(base, catchup=False), dt('2026-11-13 10:01'))
    check(d and d[1] == 'missed', '每天的任务超过 60 分钟、补跑关着：错过')
    h = {'id': 'h', 'freq': 'hourly', 'minute': 0, 'since': ts - 86400}
    d = S.sched_due(dict(h), dt('2026-11-13 12:20'))
    check(d and d[1] == 'auto', '每小时的任务到点后 30 分钟内算按时')
    d = S.sched_due(dict(h), dt('2026-11-13 12:40'))
    check(d and d[1] == 'catchup' and fmt(datetime.fromtimestamp(d[0])) == '11-13 12:00', '每小时的任务过了 30 分钟：补跑，且只补最近的一次')
    w = {'id': 'w', 'freq': 'weekly', 'weekdays': [0], 'time': '09:00', 'since': ts - 30 * 86400}
    d = S.sched_due(dict(w), dt('2026-11-22 08:00'))  # 周日：上周一 11-16 错过了
    check(d and d[1] == 'catchup' and fmt(datetime.fromtimestamp(d[0])) == '11-16 09:00', '每周一的任务：周日检查时补跑 6 天前那一次（7 天内）')
    # 旧数据升级：没有 since 的按一小时前起算
    old = {'id': 'o', 'time': '09:00', 'days': 'daily', 'last_run': '2026-11-12 09:00'}
    t1 = dict(old)
    S.sched_fill(t1, dt('2026-11-13 09:30').timestamp())
    d = S.sched_due(t1, dt('2026-11-13 09:30'))
    check(d and d[1] == 'auto', '旧任务升级时正在 9 点后 60 分钟内：照常运行（旧版行为）')
    t2 = dict(old)
    S.sched_fill(t2, dt('2026-11-13 15:00').timestamp())
    check(S.sched_due(t2, dt('2026-11-13 15:00')) is None, '旧任务升级时已过了窗口：不翻出更早的错过去补跑')
    check(S.sched_fill(t2, ts) is False, 'since 已有就不再改')


def test_clean():
    now = time.time()
    try:
        S.sched_clean({'cwd': '/tmp', 'prompt': 'x', 'freq': 'weekly', 'time': '09:00', 'weekdays': []}, None)
        check(False, '每周不选星期几应报错')
    except ValueError as e:
        check('星期' in str(e), '每周不选星期几：' + str(e))
    for ev in (0, 169, 'abc', True):
        try:
            S.sched_clean({'cwd': '/tmp', 'prompt': 'x', 'freq': 'interval', 'time': '09:00', 'every': ev}, None)
            check(False, f'间隔 {ev!r} 应报错')
        except ValueError as e:
            check('1 到 168' in str(e), f'间隔 {ev!r}：{e}')
    try:
        S.sched_clean({'cwd': '/tmp', 'prompt': ' ', 'time': '09:00'}, None)
        check(False, '空提示词应报错')
    except ValueError as e:
        check('提示词' in str(e), '空提示词：' + str(e))
    t = S.sched_clean({'cwd': '/tmp', 'prompt': 'x', 'freq': 'hourly', 'minute': 15, 'time': '', 'since': 1, 'last_slot': 99, 'last_run': 'x',
                       'anchor': '2000-01-01', 'next_at': 5, 'runs': [1], 'running': True}, None)
    check(t['freq'] == 'hourly' and t['minute'] == 15 and t['time'] == '09:00' and t['days'] == 'daily', '每小时：记分钟，time 留一个合法值给旧版')
    check(t['since'] >= now and 'last_slot' not in t and 'last_run' not in t and 'anchor' not in t, '后台字段（since、last_slot、last_run、anchor）前端改不了')
    check(not ({'next_at', 'runs', 'running'} & set(t)), '列表里的计算字段不会存进配置')
    check(t['catchup'] is True and t['awake'] is False and t['enabled'] is True and t['fast'] is False, '补跑默认开、保持唤醒默认关')
    w = S.sched_clean({'cwd': '/tmp', 'prompt': 'x', 'freq': 'weekly', 'time': '08:00', 'weekdays': [4, 0, 0, 9, True]}, None)
    check(w['weekdays'] == [0, 4], '星期去重、排序、去掉不合法的值')
    old = dict(w, since=123.0, last_slot=456.0, last_run='2026-11-01 08:00', id='k1')
    r = S.sched_clean(dict(old, name='改名', since=999, catchup=False), old)
    check(r['since'] == 123.0 and r['last_slot'] == 456.0 and r['last_run'] == '2026-11-01 08:00' and r['catchup'] is False and r['id'] == 'k1',
          '只改名称、补跑开关：起算时间和已处理时刻不变')
    r = S.sched_clean(dict(old, time='08:30'), old)
    check(r['since'] > 1000, '改了时间：从现在起算（之前的时刻不算错过）')
    r = S.sched_clean(dict(old, enabled=True), dict(old, enabled=False))
    check(r['since'] > 1000, '从暂停恢复：从现在起算，暂停期间的不补')
    iv = S.sched_clean({'cwd': '/tmp', 'prompt': 'x', 'freq': 'interval', 'every': '6', 'time': '07:00'}, None)
    check(iv['every'] == 6 and iv['anchor'] == datetime.now().strftime('%Y-%m-%d'), '自定义间隔：记下起算日期')
    legacy = {'id': 'L', 'name': 'n', 'cwd': '/tmp', 'prompt': 'p', 'time': '09:00', 'days': 'weekdays', 'enabled': True, 'last_run': '2026-11-12 09:00', 'since': 5.0}
    r = S.sched_clean(dict(legacy, enabled=False), legacy)
    check(r['freq'] == 'weekdays' and r['days'] == 'weekdays' and r['since'] == 5.0 and r['last_run'] == '2026-11-12 09:00',
          '旧前端只发旧字段（开关）：频率按 days 补上，其他不变')


# ---------------------------------------------------------------- 定时任务：历史、到点触发、补跑、跳过
def test_history_and_due():
    with Tmp() as tmp:
        for i in range(25):
            S.sched_hist_add('t1', {'id': 'r%d' % i, 't': i, 'result': 'done'})
        h = S.sched_hist_load()['t1']
        check(len(h) == 20 and h[0]['id'] == 'r24' and h[-1]['id'] == 'r5', '每个任务只留最近 20 次，新的在前')
        S.sched_hist_set('t1', 'r24', result='error', note='x')
        check(S.sched_hist_load()['t1'][0]['result'] == 'error', '按运行 id 更新结果')

        calls = []
        S.start_scheduled = lambda t, kind='auto', slot=None, rid=None: calls.append((t['id'], kind, slot, rid))
        now = datetime.now()
        ts = now.timestamp()
        hm = lambda x: x.strftime('%H:%M')
        past3 = datetime.fromtimestamp(ts - 3 * 3600)
        conf = {'schedules': [
            {'id': 'due', 'cwd': '/tmp', 'prompt': 'p', 'freq': 'hourly', 'minute': now.minute, 'since': ts - 86400},  # 正好到点
            {'id': 'catch', 'cwd': '/tmp', 'prompt': 'p', 'freq': 'interval', 'every': 168, 'time': hm(past3),
             'anchor': past3.strftime('%Y-%m-%d'), 'since': ts - 86400},  # 3 小时前该跑、没跑
            {'id': 'miss', 'cwd': '/tmp', 'prompt': 'p', 'freq': 'interval', 'every': 168, 'time': hm(past3),
             'anchor': past3.strftime('%Y-%m-%d'), 'since': ts - 86400, 'catchup': False},
            {'id': 'busy', 'cwd': '/tmp', 'prompt': 'p', 'freq': 'hourly', 'minute': now.minute, 'since': ts - 86400},
            {'id': 'off', 'cwd': '/tmp', 'prompt': 'p', 'freq': 'hourly', 'minute': now.minute, 'since': ts - 86400, 'enabled': False},
            {'id': 'legacy', 'cwd': '/tmp', 'prompt': 'p', 'time': hm(now), 'days': 'daily', 'last_run': '2020-01-01 00:00'},
        ]}
        S.save_conf(conf)
        S.SCHED_ACTIVE['busy'] = {'rid': 'x', 'sid': '', 'caf': 0}
        S.run_due()
        time.sleep(0.3)
        kinds = {c[0]: c[1] for c in calls}
        check(kinds.get('due') == 'auto', '到点的任务按时启动')
        check(kinds.get('catch') == 'catchup', '错过的任务补跑（kind=catchup）')
        check('miss' not in kinds and 'busy' not in kinds and 'off' not in kinds, '关了补跑的、上一次还在运行的、暂停的都不启动')
        check(kinds.get('legacy') == 'auto', '旧格式任务（time + days + last_run）到点照常启动')
        h = S.sched_hist_load()
        check((h.get('miss') or [{}])[0].get('result') == 'missed', '关了补跑：历史里记“已错过”')
        check((h.get('busy') or [{}])[0].get('result') == 'busy', '上一次还在运行：历史里记“已跳过”')
        c = S.load_conf()
        tm = {t['id']: t for t in c['schedules']}
        check(all(tm[k].get('last_slot') for k in ('due', 'catch', 'miss', 'busy', 'legacy')) and not tm['off'].get('last_slot'),
              '处理过的时刻都记进 last_slot')
        check(isinstance(tm['legacy'].get('since'), float) and tm['legacy']['last_run'][:10] == now.strftime('%Y-%m-%d'),
              '旧任务补上 since，last_run 照常更新')
        check(all(k in S.SCHED_ACTIVE for k in ('due', 'catch', 'legacy')), '启动前先占位，防止同一任务重复启动')
        n = len(calls)
        S.run_due()
        time.sleep(0.2)
        check(len(calls) == n, '同一个时刻不会再次触发')

        # 立即运行：正在运行时拒绝；不存在时报错
        try:
            S.api_schedules({'action': 'run', 'id': 'due'})
            check(False, '正在运行的任务立即运行应报错')
        except ValueError as e:
            check('正在运行' in str(e), '正在运行的任务不能再立即运行：' + str(e))
        try:
            S.api_schedules({'action': 'run', 'id': 'nope'})
            check(False, '不存在的任务应报错')
        except ValueError as e:
            check('找不到' in str(e), '不存在的任务：' + str(e))
        S.SCHED_ACTIVE.clear()
        S.api_schedules({'action': 'run', 'id': 'off'})
        time.sleep(0.2)
        check(calls[-1][0] == 'off' and calls[-1][1] == 'manual', '暂停的任务也能手动立即运行（kind=manual）')

        # 列表：下次运行、历史、运行中
        S.SCHED_ACTIVE.clear()
        S.SCHED_ACTIVE['catch'] = {'rid': 'z', 'sid': '', 'caf': 0}
        lst = {t['id']: t for t in S.api_schedules({'action': 'list'})['schedules']}
        check(lst['due']['next_at'] and lst['due']['next_at'] > ts and lst['off']['next_at'] is None, '列表带下次运行时间；暂停的没有')
        check(lst['catch']['running'] is True and lst['due']['running'] is False, '列表标出正在运行的任务')
        check(lst['miss']['runs'][0]['result'] == 'missed' and lst['legacy']['freq'] == 'daily', '列表带历史；旧任务给出频率')
        # 保存保持顺序；删除连带删历史
        S.api_schedules({'action': 'save', 'task': dict(lst['catch'], name='改名')})
        ids = [t['id'] for t in S.load_conf()['schedules']]
        check(ids == ['due', 'catch', 'miss', 'busy', 'off', 'legacy'], '编辑、开关不改变任务顺序')
        S.api_schedules({'action': 'delete', 'id': 'miss'})
        check('miss' not in S.sched_hist_load() and 'miss' not in [t['id'] for t in S.load_conf()['schedules']], '删除任务连带删掉它的历史')
        r = S.api_schedules({'action': 'history', 'id': 'busy'})
        check(r['runs'] and r['runs'][0]['result'] == 'busy', 'history 接口返回单个任务的历史')


def test_start_paths():
    with Tmp() as tmp:
        S.is_trusted = lambda c: False
        S.SCHED_ACTIVE['u'] = {'rid': 'r1', 'sid': '', 'caf': 0}
        S.start_scheduled({'id': 'u', 'cwd': tmp.d, 'prompt': 'p'}, 'auto', 1.0, 'r1')
        h = S.sched_hist_load().get('u') or [{}]
        check(h[0].get('result') == 'untrusted' and 'u' not in S.SCHED_ACTIVE, '项目没信任：记“未执行”，释放占位')
        S.SCHED_ACTIVE['m'] = {'rid': 'r2', 'sid': '', 'caf': 0}
        S.start_scheduled({'id': 'm', 'cwd': os.path.join(tmp.d, 'gone'), 'prompt': 'p'}, 'manual', None, 'r2')
        h = S.sched_hist_load().get('m') or [{}]
        check(h[0].get('result') == 'failed' and '不在' in h[0].get('note', '') and 'm' not in S.SCHED_ACTIVE, '项目文件夹不在了：记“启动失败”')


class FakeRunner:
    def __init__(self):
        self.turn, self.since, self.state = False, 0.0, {'alive': True}


def ps_cmd(pid):
    return subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()


def test_watch_awake():
    with Tmp():
        sid, tid, rid = 'sid-r15e-test', 'aw', 'rid1'
        fr = FakeRunner()
        S.RUNNERS[sid] = fr
        orig_err = S.sched_turn_error
        S.sched_turn_error = lambda s: False
        try:
            S.sched_hist_add(tid, {'id': rid, 't': time.time(), 'kind': 'auto', 'result': 'running', 'sid': sid})
            S.SCHED_ACTIVE[tid] = {'rid': rid, 'sid': sid, 'caf': 0}
            t0 = time.time()
            th = threading.Thread(target=S.sched_watch, args=(tid, rid, sid, t0, True), daemon=True)
            th.start()
            time.sleep(1.2)
            fr.turn, fr.since = True, time.time()
            caf = (S.SCHED_ACTIVE.get(tid) or {}).get('caf')
            cmd = ps_cmd(caf) if caf else ''
            check(bool(caf) and 'caffeinate' in cmd and f'-w {os.getpid()}' in cmd, f'保持唤醒：运行期间有自己起的 caffeinate（{cmd}）')
            time.sleep(1.2)
            check(th.is_alive(), '一轮没结束时一直跟踪')
            fr.turn = False
            S.MARKS['done_at'][sid] = time.time()
            th.join(10)
            check(not th.is_alive(), '一轮结束（done_at）后跟踪结束')
            time.sleep(0.2)
            check(not ps_cmd(caf), '运行结束后 caffeinate 已结束')
            e = S.sched_hist_load()[tid][0]
            check(e['result'] == 'done' and e.get('end') and tid not in S.SCHED_ACTIVE, '历史记成“完成”，释放占位')
            # 出错的一轮
            S.sched_turn_error = lambda s: True
            rid2 = 'rid2'
            S.sched_hist_add(tid, {'id': rid2, 't': time.time(), 'kind': 'manual', 'result': 'running', 'sid': sid})
            S.SCHED_ACTIVE[tid] = {'rid': rid2, 'sid': sid, 'caf': 0}
            t1 = time.time()
            S.MARKS['done_at'][sid] = t1 + 0.5
            S.sched_watch(tid, rid2, sid, t1, False)
            e = S.sched_hist_load()[tid][0]
            check(e['result'] == 'error' and e['note'] == '接口出错', '这一轮接口出错：历史记成“出错”')
        finally:
            S.sched_turn_error = orig_err
            S.RUNNERS.pop(sid, None)
            S.MARKS['done_at'].pop(sid, None)


def test_resume():
    with Tmp():
        S.save_conf({'schedules': [{'id': 'q', 'cwd': '/tmp', 'prompt': 'p', 'time': '09:00'}]})
        S.sched_hist_add('q', {'id': 'old', 't': time.time() - 100, 'result': 'running', 'sid': 'not-running-sid'})
        S.sched_resume()
        e = S.sched_hist_load()['q'][0]
        check(e['result'] == 'unknown' and 'q' not in S.SCHED_ACTIVE, '重启后对话已不在：记“结果未知”')


# ---------------------------------------------------------------- 钩子与权限规则
TOK = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6'
SK = 'sk-ant-api03-' + 'Zz9Yy8Xx7Ww6Vv5Uu4'


def test_rules():
    d = tempfile.mkdtemp(prefix='cwb-r15e-rules-')
    orig = F.NS
    try:
        home = os.path.join(d, 'home')
        proj = os.path.join(d, 'proj')
        os.makedirs(os.path.join(home, '.claude'))
        os.makedirs(os.path.join(proj, '.claude'))
        F.NS = dict(orig, HOME=home)
        json.dump({'hooks': {
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': f'GITHUB_TOKEN={TOK} node ~/h.js --api-key {SK}', 'timeout': 30}]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': 'say done', 'async': True}, {'type': 'http', 'url': 'https://x.test/hook?token=abc123', 'headers': {'Authorization': 'Bearer zzz'}}]}],
            'Bad': 'x'},
            'permissions': {'allow': ['Bash(npm run test:*)', 'Read'], 'deny': [f'Bash(curl -H "Authorization: Bearer {TOK}")'], 'ask': 'notalist'}},
            open(os.path.join(home, '.claude', 'settings.json'), 'w'))
        json.dump({'permissions': {'ask': ['Bash(git push:*)']}, 'disableAllHooks': True,
                   'hooks': {'SessionStart': [{'matcher': 'startup', 'hooks': [{'type': 'command', 'command': 'node', 'args': ['a b', 'c']}]}]}},
                  open(os.path.join(proj, '.claude', 'settings.json'), 'w'))
        open(os.path.join(proj, '.claude', 'settings.local.json'), 'w').write('{ not json')
        r = F.api_settings_rules({'cwd': proj})
        blob = json.dumps(r, ensure_ascii=False)
        check(TOK not in blob and SK not in blob and 'abc123' not in blob and 'zzz' not in blob, '结果里没有任何完整令牌')
        fs = {f['key']: f for f in r['files']}
        check(set(fs) == {'user', 'project', 'local'} and fs['local']['error'] and not fs['user']['error'] and fs['project']['disableAllHooks'],
              f"三份文件都列出；本机设置格式错有提示（{fs['local']['error']}）；项目设置停用了全部钩子")
        hk = r['hooks']
        check(len(hk) == 4 and [h['event'] for h in hk] == ['PreToolUse', 'Stop', 'Stop', 'SessionStart'], '钩子按文件、事件列出，不合法的分组跳过')
        h0 = hk[0]
        check(h0['matcher'] == 'Bash' and h0['src'] == 'user' and h0['timeout'] == 30 and h0['path'].endswith('/home/.claude/settings.json'),
              '钩子带匹配、来源、超时、来源文件路径')
        check('GITHUB_TOKEN=••••' in h0['text'] and '--api-key ••••' in h0['text'] and 'node ~/h.js' in h0['text'], '命令里的令牌打码，其余照常显示：' + h0['text'])
        check(hk[1]['async'] is True and hk[2]['type'] == 'http' and 'token=••••' in hk[2]['text'] and 'Authorization: ••••' in hk[2]['text'],
              'http 钩子：网址参数和请求头打码：' + hk[2]['text'])
        check(hk[3]['text'] == "node 'a b' c" and hk[3]['src'] == 'project', '带 args 的命令按参数拼出')
        pm = r['perms']
        check([(p['kind'], p['src']) for p in pm] == [('allow', 'user'), ('allow', 'user'), ('deny', 'user'), ('ask', 'project')],
              '权限规则：允许 / 询问 / 拒绝 + 来源；不是列表的忽略')
        check('Bearer ••••' in pm[2]['rule'], '权限规则里的令牌也打码：' + pm[2]['rule'])
        r2 = F.api_settings_rules({'cwd': ''})
        check([f['key'] for f in r2['files']] == ['user'], '没打开项目：只读用户级')
        r3 = F.api_settings_rules({'cwd': home})
        check([f['key'] for f in r3['files']] == ['user', 'local'], '项目就是主目录时，同一个文件不重复列出')
    finally:
        F.NS = orig
        shutil.rmtree(d, ignore_errors=True)


def test_mask():
    cases = [
        ('curl -H "Authorization: Bearer abcDEF1234567890xyz" https://x.com', 'abcDEF1234567890xyz'),
        ('export OPENAI_API_KEY="sk-proj-1234567890abcdef"; run', '1234567890abcdef'),
        ('git clone https://user:s3cretPass@github.com/a/b.git', 's3cretPass'),
        ('x --token=QWERTY123456', 'QWERTY123456'),
        ('password: hunter22', 'hunter22'),
        ('run eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.SflKxwRJSMeKKF2QT4fwpMe', 'SflKxwRJSMeKKF2QT4fwpMe'),
        ('node x.js 9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddcc', '9f8e7d6c5b4a39281706f5e4d3c2b1a0ffeeddcc'),
        ('AWS AKIAIOSFODNN7EXAMPLE', 'IOSFODNN7EXAMPLE'),
    ]
    for s, secret in cases:
        m = F.mask_secrets(s)
        check(secret not in m and '••••' in m, f'打码：{m}')
    keep = ['python3 ~/.claude/hooks/token_counter.py', 'echo $MY_TOKEN', 'X_SECRET=$SECRET_FROM_ENV ./run.sh', 'Bash(npm run test:*)',
            '/Users/demo/cwb-dev/r15/e/.venv/bin/python -I /Users/demo/cwb-dev/r15/e/hook.py idle', 'WebFetch(domain:github.com)']
    for s in keep:
        check(F.mask_secrets(s) == s, f'不该打码的原样保留：{s}')


# ---------------------------------------------------------------- 插件
DETAILS = """claude-md-management 1.0.0
  Description: Tools to maintain and improve CLAUDE.md files.
  Source: claude-md-management@inline

Component inventory
  Skills (2)  claude-md-improver, revise-claude-md
  Agents (0)
  Hooks (1)
  MCP servers (3)
  LSP servers (0)

Projected token cost
  Always-on:   ~1.2k tok   added to every session
"""


def test_plugin_parse():
    d = F.pl_parse_details(DETAILS)
    check(d['counts'] == {'skills': 2, 'agents': 0, 'hooks': 1, 'mcp': 3, 'lsp': 0} and d['names']['skills'] == ['claude-md-improver', 'revise-claude-md']
          and d['tokens'] == 1200 and d['desc'].startswith('Tools'), 'details 文本：组件数量、名字、常驻 token、说明')
    ls = F.pl_parse_list(json.dumps([{'id': 'a@m', 'version': '1.0', 'scope': 'user', 'enabled': False, 'installPath': '/x'},
                                     {'id': 'b@inline', 'scope': 'session', 'enabled': True}, 'junk', {'nope': 1}]))
    check([(p['id'], p['name'], p['market'], p['enabled'], p['readonly']) for p in ls] == [('a@m', 'a', 'm', False, False), ('b@inline', 'b', 'inline', True, True)],
          'list --json：拆出名称和市场；随会话加载的（scope=session）只读')
    ls2 = F.pl_parse_list(json.dumps({'installed': [{'id': 'c@m'}], 'available': []}))
    check(ls2[0]['id'] == 'c@m', 'list --json --available 的对象格式也能读')
    try:
        F.pl_parse_list('{"x": 1}')
        check(False, '格式变了应报错')
    except ValueError:
        check(True, '输出格式变了：报错（界面显示“暂时拿不到”）')
    check(F.pl_json_line('Installing…\n{"ok":true,"id":"a@m"}\n') == {'ok': True, 'id': 'a@m'} and F.pl_json_line('nothing') is None, '改动类命令：取最后一行 JSON')
    check(F.pl_err(None, '', 'Error: getaddrinfo ENOTFOUND github.com') == '网络连接失败，检查网络或代理后再试', '联网失败给中文提示')
    check(F.pl_err({'error': {'message': 'Plugin "x" not found'}}, '', '') == 'Plugin "x" not found', '其他错误原样带出')
    tmp = tempfile.mkdtemp(prefix='cwb-r15e-pl-')
    try:
        os.makedirs(os.path.join(tmp, 'commands', 'sub'))
        open(os.path.join(tmp, 'commands', 'revise-claude-md.md'), 'w').close()
        open(os.path.join(tmp, 'commands', 'sub', 'other.md'), 'w').close()
        check(F.pl_command_names(tmp) == {'revise-claude-md', 'other'}, '命令文件按文件名认出')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_plugin_dryrun():
    F.PLUGIN_DRYRUN = True
    proj = tempfile.mkdtemp(prefix='cwb-r15e-proj-')
    try:
        cases = [
            (F.api_plugin_install, {'id': 'frontend-design@claude-plugins-official'}, ['claude', 'plugin', 'install', 'frontend-design@claude-plugins-official', '--json', '-s', 'user']),
            (F.api_plugin_install, {'id': 'x@m', 'scope': 'project', 'cwd': proj}, ['claude', 'plugin', 'install', 'x@m', '--json', '-s', 'project']),
            (F.api_plugin_install, {'id': 'x@m', 'accept': 'a' * 64}, ['claude', 'plugin', 'install', 'x@m', '--json', '-s', 'user', '--accept-command', 'a' * 64]),
            (F.api_plugin_enable, {'id': 'x@m', 'scope': 'user', 'on': True}, ['claude', 'plugin', 'enable', 'x@m', '--json', '-s', 'user']),
            (F.api_plugin_enable, {'id': 'x@m', 'scope': 'local', 'on': False, 'cwd': proj}, ['claude', 'plugin', 'disable', 'x@m', '--json', '-s', 'local']),
            (F.api_plugin_enable, {'id': 'x', 'on': False}, ['claude', 'plugin', 'disable', 'x', '--json']),
            (F.api_plugin_uninstall, {'id': 'x@m', 'scope': 'user'}, ['claude', 'plugin', 'uninstall', 'x@m', '--json', '-s', 'user']),
            (F.api_plugin_market_add, {'source': 'anthropics/claude-plugins-official'}, ['claude', 'plugin', 'marketplace', 'add', 'anthropics/claude-plugins-official', '--json']),
        ]
        for fn, b, want in cases:
            r = fn(dict(b, confirmed=True))
            check(r.get('dry') and r['cmd'] == want, '空跑拼出：' + ' '.join(r.get('cmd') or []))
        r = F.api_plugin_install({'id': 'x@m', 'scope': 'project', 'cwd': proj, 'confirmed': True})
        check(r['cwd'] == proj, '项目范围在项目文件夹里执行')
        r = F.api_plugin_install({'id': 'x@m', 'confirmed': True})
        check(r['cwd'] == F.home(), '用户范围在主目录执行')
        bad = [
            (F.api_plugin_install, {'id': 'x@m'}, '确认'),
            (F.api_plugin_install, {'id': 'x@m', 'confirmed': 'yes'}, '确认'),
            (F.api_plugin_install, {'id': '--help', 'confirmed': True}, '名称'),
            (F.api_plugin_install, {'id': 'a b', 'confirmed': True}, '名称'),
            (F.api_plugin_install, {'id': 'x@m', 'scope': 'global', 'confirmed': True}, '范围'),
            (F.api_plugin_install, {'id': 'x@m', 'scope': 'project', 'cwd': '/nonexistent-r15e', 'confirmed': True}, '项目'),
            (F.api_plugin_install, {'id': 'x@m', 'accept': 'abc', 'confirmed': True}, '确认码'),
            (F.api_plugin_enable, {'id': 'b@inline', 'scope': 'session', 'confirmed': True}, '随会话'),
            (F.api_plugin_market_add, {'source': '--evil', 'confirmed': True}, '市场地址'),
            (F.api_plugin_market_add, {'source': 'a b', 'confirmed': True}, '市场地址'),
            (F.api_plugin_market_add, {'source': 'x/y'}, '确认'),
        ]
        for fn, b, word in bad:
            try:
                fn(b)
                check(False, f'应拒绝：{b}')
            except ValueError as e:
                check(word in str(e), f'拒绝 {b}：{e}')
    finally:
        F.PLUGIN_DRYRUN = False
        shutil.rmtree(proj, ignore_errors=True)


def test_plugin_live_list():
    """只读：真实 claude plugin list --json、marketplace list --json、details（不发模型请求、不改任何状态）。"""
    exe = shutil.which('claude')
    if not exe:
        check(True, '本机没有 claude，跳过只读实测')
        return
    orig = F.NS
    F.NS = dict(orig, CLAUDE=exe)
    try:
        t0 = time.time()
        r = F.api_plugin_list({'cwd': ''})
        check(isinstance(r['plugins'], list) and not r['error'] and all(p['id'] and 'enabled' in p for p in r['plugins']),
              f"读到已装插件 {len(r['plugins'])} 个（{time.time() - t0:.1f}s）")
        check(isinstance(r['markets'], list), f"读到市场 {len(r['markets'] or [])} 个")
        check(all(isinstance(p.get('counts'), dict) for p in r['plugins']), '每个插件都拿到组件数量')
        F.NS = dict(orig, CLAUDE='/nonexistent/claude-r15e')
        F.PL['details'].clear()
        r = F.api_plugin_list({'cwd': ''})
        check(r['plugins'] is None and r['error'] == '找不到 claude 命令' and r['markets'] is None, '子命令不存在：返回中文错误，不抛异常')
    finally:
        F.NS = orig


if __name__ == '__main__':
    for fn in (test_slots, test_due, test_clean, test_history_and_due, test_start_paths, test_watch_awake, test_resume,
               test_rules, test_mask, test_plugin_parse, test_plugin_dryrun, test_plugin_live_list):
        print('==', fn.__name__, flush=True)
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append(f'{fn.__name__} 异常：{e!r}')
    print('\n全部通过' if not FAIL else '\n失败 %d 项：\n' % len(FAIL) + '\n'.join(FAIL))
    sys.exit(1 if FAIL else 0)
