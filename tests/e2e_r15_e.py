"""第十五轮 E 端到端：真实 claude + 假网关（tests/fakegw.py，端口 18805）+ 测试服务（端口 47955，CWB_SCHED_TICK=3、CWB_PLUGIN_DRYRUN=1）。
用法（在副本根目录）：.venv/bin/python -I tests/e2e_r15_e.py [all|serve|cleanup]
  all      跑定时任务：到点触发、立即运行（带保持唤醒）、重启后补跑；最后清理
  serve    只起假网关和测试服务、建好测试项目（给浏览器 DOM 测试用），不清理
  cleanup  按会话 ID 结束并删除测试会话（移到废纸篓），结束自己起的测试服务、ptyd、假网关
只在 /tmp/cwb-r15-e-proj 里新建对话；只结束自己记下 PID 的进程；插件操作一律空跑。
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta

os.environ.setdefault('CWB_E2E_PORT', '47955')
os.environ.setdefault('CWB_E2E_GW', '18805')
os.environ.setdefault('CWB_E2E_PROJ', '/tmp/cwb-r15-e-proj')
os.environ.setdefault('CWB_E2E_STATE', '/tmp/cwb-r15-e-exp/e2e-state.json')
os.environ.setdefault('CWB_E2E_GWLOG', '/tmp/cwb-r15-e-gw/requests.jsonl')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_lib as L  # noqa: E402
from e2e_lib import api, check, say, st_load, st_save, wait_until, alive, cmd_of  # noqa: E402

HOME = os.path.expanduser('~')
TRASH = os.path.join(HOME, '.Trash')
EXTRA = {'CWB_SCHED_TICK': '3', 'CWB_PLUGIN_DRYRUN': '1'}


def ensure_gw():
    try:
        socket.create_connection(('127.0.0.1', L.GW), timeout=1).close()
        return
    except OSError:
        pass
    logdir = os.path.dirname(L.GWLOG)
    os.makedirs(logdir, exist_ok=True)
    p = subprocess.Popen([os.path.join(L.ROOT, '.venv/bin/python'), '-I', os.path.join(L.ROOT, 'tests/fakegw.py'), str(L.GW), logdir],
                         stdin=subprocess.DEVNULL, stdout=open(os.path.join(logdir, 'gw.out'), 'ab'), stderr=subprocess.STDOUT, start_new_session=True)
    d = st_load()
    d['gw'] = p.pid
    st_save(d)
    def up():
        try:
            socket.create_connection(('127.0.0.1', L.GW), timeout=1).close()
            return True
        except OSError:
            return False
    wait_until(up, 5, 0.1)
    say('假网关已启动', p.pid)


def serve():
    os.makedirs(L.PROJ, exist_ok=True)
    ensure_gw()
    if not L.ping().startswith('ok'):
        L.start_server(**EXTRA)
    return st_load().get('server')


def trust_project():
    """第一次进测试文件夹：新建一个对话，在 CLI 的信任弹窗里选“信任”（CLI 自己记下）。"""
    try:
        cj = json.load(open(os.path.join(HOME, '.claude.json'), encoding='utf-8'))
        if (cj.get('projects') or {}).get(os.path.realpath(L.PROJ), {}).get('hasTrustDialogAccepted'):
            return
    except Exception:
        pass
    sid = L.new_session()
    api('stop', {'sid': sid})


def caf_pids(server_pid):
    out = subprocess.run(['pgrep', '-f', f'caffeinate -i -s -w {server_pid}'], capture_output=True, text=True).stdout.split()
    return [int(x) for x in out if x.isdigit()]


def task_of(tid):
    for t in api('schedules', {'action': 'list'}).get('schedules', []):
        if t['id'] == tid:
            return t
    return None


def remember(sid):
    if sid:
        d = st_load()
        if sid not in d.setdefault('sids', []):
            d['sids'].append(sid)
            st_save(d)


def wait_run(tid, pred, timeout):
    def f():
        t = task_of(tid)
        runs = (t or {}).get('runs') or []
        for r in runs:
            remember(r.get('sid'))
        return runs[0] if runs and pred(runs[0]) else None
    return wait_until(f, timeout, 0.5)


def run_all():
    server = serve()
    trust_project()
    n0 = len(L.gw_rows())
    # 1. 到点触发：每天，时间是下一分钟
    nxt = (datetime.now() + timedelta(minutes=1)).replace(second=0, microsecond=0)
    r = api('schedules', {'action': 'save', 'task': {'name': 'E2E 到点', 'cwd': L.PROJ, 'prompt': 'r15e scheduled hello', 'freq': 'daily',
                                                       'time': nxt.strftime('%H:%M'), 'mode': 'default'}})
    t1 = next(t for t in r['schedules'] if t['name'] == 'E2E 到点')
    check(abs(t1['next_at'] - nxt.timestamp()) < 1, f"保存后下次运行时间是 {nxt.strftime('%H:%M')}")
    say('等到点', nxt.strftime('%H:%M:%S'))
    e = wait_run(t1['id'], lambda x: x['result'] == 'running', (nxt - datetime.now()).total_seconds() + 30)
    check(bool(e) and e['kind'] == 'auto' and e['sid'], f'到点后启动，历史里有一条“运行中”（{e}）')
    e = wait_run(t1['id'], lambda x: x['result'] != 'running', 90)
    check(bool(e) and e['result'] == 'done', f'这一轮结束后历史记成“完成”（{e}）')
    sid1 = (e or {}).get('sid')
    check(bool(sid1 and L.transcript(sid1) and 'r15e scheduled hello' in L.tx_text(sid1)), '对应对话的记录里有这条提示')
    t = task_of(t1['id'])
    check(t and t.get('last_slot') == nxt.timestamp() and t['next_at'] > nxt.timestamp(), '记下已处理的时刻，下次运行顺延到明天')

    # 2. 立即运行 + 运行期间保持唤醒（慢速回复 8 秒）
    r = api('schedules', {'action': 'save', 'task': {'name': 'E2E 立即', 'cwd': L.PROJ, 'prompt': 'r15e SLOWTEST:8', 'freq': 'weekly',
                                                       'weekdays': [0], 'time': '03:00', 'awake': True}})
    t2 = next(t for t in r['schedules'] if t['name'] == 'E2E 立即')
    check(t2['awake'] is True and t2['catchup'] is True and t2['freq'] == 'weekly', '保存了每周任务（保持唤醒开、补跑默认开）')
    check(not caf_pids(server), '运行前没有本服务起的 caffeinate')
    api('schedules', {'action': 'run', 'id': t2['id']})
    e = wait_run(t2['id'], lambda x: x['result'] == 'running' and x['sid'], 20)
    check(bool(e) and e['kind'] == 'manual', '立即运行：历史里记“手动”')
    rr = api('schedules', {'action': 'run', 'id': t2['id']})
    check('正在运行' in (rr.get('error') or ''), '运行中再点立即运行：拒绝（' + str(rr.get('error')) + '）')
    pids = wait_until(lambda: caf_pids(server), 15, 0.5) or []
    check(len(pids) == 1, f'运行期间有一个 caffeinate（{[cmd_of(p) for p in pids]}）')
    check(task_of(t2['id'])['running'] is True, '列表里标出运行中')
    e = wait_run(t2['id'], lambda x: x['result'] != 'running', 90)
    check(bool(e) and e['result'] == 'done', f'慢速回复结束后记成“完成”（{e}）')
    time.sleep(1)
    check(not caf_pids(server) and not any(alive(p) for p in pids), '运行结束后 caffeinate 已结束')

    # 3. 重启后补跑：停服务，把一个任务改成“2 小时前该跑、没跑”，再启动
    sid_ids = [x for x in (st_load().get('sids') or [])]
    for s in sid_ids:
        api('stop', {'sid': s})
    time.sleep(2)
    L.stop_server()
    conf_path = os.path.join(L.ROOT, 'config.json')
    c = json.load(open(conf_path, encoding='utf-8'))
    past = datetime.now() - timedelta(hours=2)
    c['schedules'].append({'id': 'e2ecatch', 'name': 'E2E 补跑', 'cwd': L.PROJ, 'prompt': 'r15e catchup hello', 'freq': 'interval', 'every': 168,
                           'time': past.strftime('%H:%M'), 'anchor': past.strftime('%Y-%m-%d'), 'days': 'daily', 'enabled': True,
                           'since': time.time() - 86400, 'catchup': True, 'awake': False})
    c['schedules'].append({'id': 'e2emiss', 'name': 'E2E 不补', 'cwd': L.PROJ, 'prompt': 'r15e should not run', 'freq': 'interval', 'every': 168,
                           'time': past.strftime('%H:%M'), 'anchor': past.strftime('%Y-%m-%d'), 'days': 'daily', 'enabled': True,
                           'since': time.time() - 86400, 'catchup': False})
    json.dump(c, open(conf_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    server = L.start_server(**EXTRA)
    e = wait_run('e2ecatch', lambda x: x['result'] != 'running', 90)
    check(bool(e) and e['kind'] == 'catchup' and e['result'] == 'done', f'启动后补跑最近错过的一次，并完成（{e}）')
    e = wait_run('e2emiss', lambda x: True, 10)
    check(bool(e) and e['result'] == 'missed' and not e.get('sid'), f'关了补跑的任务：记“已错过”，没有起对话（{e}）')
    L.ua_check(n0)


def cleanup():
    d = st_load()
    sids = d.get('sids', [])
    if L.ping().startswith('ok'):
        for t in api('schedules', {'action': 'list'}).get('schedules', []):
            for r in t.get('runs') or []:  # 定时任务起的对话也算测试会话
                if r.get('sid') and r['sid'] not in sids:
                    sids.append(r['sid'])
            if (t.get('name') or '').startswith('E2E '):
                api('schedules', {'action': 'delete', 'id': t['id']})
        for s in sids:
            api('stop', {'sid': s})
        time.sleep(2)
    L.stop_server()
    pp = L.kill_ptyd(signal.SIGTERM)
    if pp:
        wait_until(lambda: not alive(pp), 10)
        say('测试 ptyd 已结束', pp)
    gw = d.get('gw')
    if gw and alive(gw) and 'fakegw.py' in cmd_of(gw):
        os.kill(gw, signal.SIGTERM)
        say('假网关已结束', gw)
    n = 0
    stamp = str(int(time.time()))
    for s in sids:
        for p in (os.path.join(L.PROJ_DIR, s + '.jsonl'), os.path.join(L.PROJ_DIR, s),
                  os.path.join(HOME, '.claude', 'session-env', s), os.path.join(HOME, '.claude', 'file-history', s)):
            if os.path.exists(p):
                os.rename(p, os.path.join(TRASH, 'cwb-r15e-%s-%s' % (stamp, os.path.basename(p))))
                n += 1
    say('测试会话记录已移到废纸篓', len(sids), '个会话，', n, '个文件或目录')
    d['sids'] = []
    st_save(d)


if __name__ == '__main__':
    sc = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if sc == 'cleanup':
        cleanup()
    elif sc == 'serve':
        serve()
        trust_project()
        say('测试服务就绪', L.PORT)
    else:
        try:
            run_all()
        finally:
            cleanup()
        say('全部通过' if not L.FAIL else f'{len(L.FAIL)} 项失败：' + '；'.join(L.FAIL))
