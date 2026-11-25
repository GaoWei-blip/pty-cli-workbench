"""第十五轮 A1 端到端：真实交互式 claude + 假网关（tests/fakegw.py 的 FAKE 关键字）+ 测试服务（端口 47951）。
验证 turn-changes / turn-diff / 回退与撤销（含回退后 CLI 继续编辑）/ 运行状态 ctx 与状态行转调 / context-detail / 忙时拒绝 / mark-unread。
“用户全局状态行”用假的设置文件（CWB_USER_SETTINGS），不读不写真实的 ~/.claude/settings.json 里的状态行脚本。
运行：.venv/bin/python -I tests/e2e_r15_a1.py；清理：.venv/bin/python -I tests/e2e_r15_a1.py cleanup
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

os.environ.setdefault('CWB_E2E_PORT', '47951')
os.environ.setdefault('CWB_E2E_GW', '18801')
os.environ.setdefault('CWB_E2E_PROJ', '/tmp/cwb-r15-a1-proj')
os.environ.setdefault('CWB_E2E_STATE', '/tmp/cwb-r15-a1-e2e/state.json')
os.environ.setdefault('CWB_E2E_GWLOG', '/tmp/cwb-r15-a1-e2e/gw/requests.jsonl')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_lib as L  # noqa: E402
from e2e_lib import api, check, say, st_load, st_save, wait_until, alive, cmd_of  # noqa: E402

HOME = os.path.expanduser('~')
P = L.PROJ
RP = os.path.realpath(P) if os.path.exists(P) else '/private' + P
WORK = os.path.dirname(os.environ['CWB_E2E_STATE'])
FAKE_SL = os.path.join(WORK, 'sl dir', 'fake user sl.sh')
FAKE_SETTINGS = os.path.join(WORK, 'fake-user-settings.json')
SL_LAST = os.path.join(WORK, 'fake-sl-last.json')
PY = os.path.join(L.ROOT, '.venv/bin/python')


def F(*steps):
    return ' '.join('FAKE[[' + json.dumps(s, ensure_ascii=False) + ']]END' for s in steps)


def rd(name):
    try:
        return open(os.path.join(P, name), encoding='utf-8').read()
    except OSError:
        return None


def wr(name, text):
    with open(os.path.join(P, name), 'w', encoding='utf-8') as f:
        f.write(text)


def idle(sid, t=60):
    time.sleep(1.5)
    return wait_until(lambda: (lambda s: s and s.get('ready') and not s.get('busy') and not s.get('turn'))(L.runner_of(sid)), t)


def prompts(sid):
    """记录里你发的提问：{开头文字: uuid}。"""
    out = {}
    for l in L.tx_text(sid).splitlines():
        try:
            r = json.loads(l)
        except Exception:
            continue
        c = (r.get('message') or {}).get('content') if r.get('type') == 'user' else None
        if isinstance(c, str) and re.match(r'^R\d', c):
            out[c.split(' ')[0]] = r['uuid']
    return out


def setup():
    os.makedirs(os.path.dirname(FAKE_SL), exist_ok=True)
    with open(FAKE_SL, 'w') as f:
        f.write('#!/bin/sh\nin=$(cat; printf x); in=${in%x}\nprintf \'%s\' "$in" > ' + SL_LAST + '\necho "FAKE-SL-OK $CWB_GEN"\n')
    os.chmod(FAKE_SL, 0o755)
    with open(FAKE_SETTINGS, 'w') as f:
        json.dump({'statusLine': {'type': 'command', 'command': "'" + FAKE_SL + "'", 'padding': 0}}, f)
    os.makedirs(os.path.join(P, 'sub'), exist_ok=True)
    for n in ('a.txt', 'f.txt', 'g.txt', 'sub/s.txt', 'sub/r.txt'):
        if os.path.exists(os.path.join(P, n)):
            os.remove(os.path.join(P, n))
    wr('b.txt', 'B原始\n第二行\n')
    wr('c.txt', 'C原始\n')
    wr('d.txt', 'D原始\n')
    d = st_load()
    gw = d.get('gw')
    if not (gw and alive(gw) and 'fakegw.py' in cmd_of(gw)):
        os.makedirs(os.path.dirname(L.GWLOG), exist_ok=True)
        lf = open(os.path.join(WORK, 'gw.out'), 'ab')
        p = subprocess.Popen([PY, '-I', os.path.join(L.ROOT, 'tests', 'fakegw.py'), str(L.GW), os.path.dirname(L.GWLOG)],
                             stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True)
        d['gw'] = p.pid
        st_save(d)
        time.sleep(0.8)
        say('假网关已启动', p.pid)
    if not L.ping().startswith('ok'):
        L.start_server(CWB_USER_SETTINGS=FAKE_SETTINGS)


def run_all():
    setup()
    n0 = len(L.gw_rows())
    ev = L.Events()
    time.sleep(0.5)
    sid = L.new_session({'mode': 'bypass permissions'})
    # ---- 状态行与 ctx
    st = wait_until(lambda: (L.runner_of(sid) or {}).get('ctx'), 20)
    check(bool(st) and 'window' in st and 'at' in st, f'运行状态里有 ctx：{st}')
    t0 = time.time()
    api('send', {'sid': sid, 'text': 'R1 新建 ' + F([{'name': 'Write', 'input': {'file_path': P + '/a.txt', 'content': 'A第一版\n'}},
                                                   {'name': 'Write', 'input': {'file_path': P + '/sub/s.txt', 'content': 'S第一版\n'}},
                                                   {'name': 'Write', 'input': {'file_path': RP + '/sub/r.txt', 'content': 'R相对键\n'}}])})
    e = ev.wait(lambda x: x.get('type') == 'ctx' and x.get('sid') == sid and x['ctx'].get('used'), 40, after=t0)
    check(bool(e) and e['ctx']['used'] > 0 and e['ctx']['window'] and isinstance(e['ctx']['at'], int), f'回复后推 ctx 事件：{e and e["ctx"]}')
    check(bool(e) and e['ctx'].get('cache') == 'unknown' and e['ctx'].get('cacheWarm') is None,
          f'修复线：从没观察到缓存时 cache=unknown、cacheWarm 为 null（界面不说“已过期”）：{e and e["ctx"]}')
    check(idle(sid), 'R1 结束')
    gen_files = [os.path.join(L.RUN, 'ctx', n) for n in os.listdir(os.path.join(L.RUN, 'ctx')) if n.endswith('.json')]
    newest = max(gen_files, key=os.path.getmtime)
    time.sleep(1)
    check(open(newest).read() == open(SL_LAST).read(), '状态行：转给原命令的标准输入和落盘的内容完全一样')
    lines = L.screen(sid).get('lines') or []
    check(any('FAKE-SL-OK' in l for l in lines), '终端状态行显示的是原命令的输出')
    # 修复线：回复带缓存 token 后，缓存状态是 warm、带到期时间
    t0 = time.time()
    api('send', {'sid': sid, 'text': 'CACHETEST 缓存计时'})
    e = ev.wait(lambda x: x.get('type') == 'ctx' and x.get('sid') == sid and x['ctx'].get('cache') == 'warm', 40, after=t0)
    check(bool(e) and (e['ctx'].get('cacheExpiresAt') or 0) > time.time() * 1000 and e['ctx'].get('cacheWarm') is True,
          f'修复线：观察到缓存后 cache=warm，带到期时间：{e and e["ctx"]}')
    check(idle(sid), 'CACHETEST 结束')
    # ---- 几轮改动
    api('send', {'sid': sid, 'text': 'R2 改 ' + F([{'name': 'Read', 'input': {'file_path': P + '/b.txt'}}],
                                                 [{'name': 'Edit', 'input': {'file_path': P + '/b.txt', 'old_string': '第二行', 'new_string': '第二行R2改'}},
                                                  {'name': 'Edit', 'input': {'file_path': P + '/sub/s.txt', 'old_string': 'S第一版', 'new_string': 'S第二版'}}])})
    check(idle(sid), 'R2 结束')
    api('send', {'sid': sid, 'text': 'R3 加 ' + F([{'name': 'Bash', 'input': {'command': f'echo D追加 >> {P}/d.txt; echo B追加 >> {P}/b.txt', 'description': '追加'}},
                                                  {'name': 'Write', 'input': {'file_path': P + '/f.txt', 'content': 'F新建\n'}}])})
    check(idle(sid), 'R3 结束')
    api('send', {'sid': sid, 'text': 'R4 改c ' + F([{'name': 'Read', 'input': {'file_path': P + '/c.txt'}}],
                                                  [{'name': 'Edit', 'input': {'file_path': P + '/c.txt', 'old_string': 'C原始', 'new_string': 'C改过'}}])})
    check(idle(sid), 'R4 结束')
    check(rd('c.txt') == 'C改过\n' and rd('b.txt') == 'B原始\n第二行R2改\nB追加\n' and rd('f.txt') == 'F新建\n', 'CLI 按假网关的工具调用改好了文件')
    u = prompts(sid)
    check(set(u) >= {'R1', 'R2', 'R3', 'R4'}, f'记录里找到四条提问 {sorted(u)}')
    # ---- context-detail（/context 在空闲时发）
    t0 = time.time()
    d = api('context-detail', {'sid': sid}, timeout=60)
    check('## Context Usage' in (d.get('md') or ''), f'context-detail 拿到 Markdown 明细（{time.time() - t0:.1f}s）')
    check(idle(sid, 20), '/context 之后回到空闲')
    items = api('open', {'sid': sid}).get('items') or []
    check(not any('context' in (it.get('text') or '').lower() for it in items if it.get('k') in ('cmd', 'note')), '/context 不在时间线里留条目')
    # ---- turn-changes
    tc = api('turn-changes', {'sid': sid})
    by = {t['uuid']: {f['rel']: (f['st'], f['add'], f['del']) for f in t['files']} for t in tc.get('turns') or []}
    check([t['uuid'] for t in tc['turns']] == [u['R1'], u['R2'], u['R3'], u['R4']], 'turn-changes：四轮都有改动、uuid 是提问记录的 uuid；/context 不算轮')
    check(by.get(u['R1']) == {'a.txt': ('new', 1, 0), 'sub/s.txt': ('new', 1, 0), 'sub/r.txt': ('new', 1, 0)}, f'R1：新建三个文件（含相对键）{by.get(u["R1"])}')
    check(by.get(u['R2']) == {'b.txt': ('mod', 1, 1), 'sub/s.txt': ('mod', 1, 1)}, f'R2：{by.get(u["R2"])}')
    check(by.get(u['R3']) == {'b.txt': ('mod', 1, 0), 'f.txt': ('new', 1, 0)}, f'R3：Bash 追加到已跟踪的 b、新建 f；d.txt 不在 {by.get(u["R3"])}')
    check(by.get(u['R4']) == {'c.txt': ('mod', 1, 1)}, f'R4：{by.get(u["R4"])}')
    check(tc['all']['n'] == 6 and tc['bashNote'] is True, f'整个对话累计 6 个文件 {[f["rel"] for f in tc["all"]["files"]]}')
    df = api('turn-diff', {'sid': sid, 'uuid': u['R2'], 'path': P + '/b.txt'})
    check('@@' in (df.get('diff') or '') and '+第二行R2改' in df['diff'], 'turn-diff：统一差异格式')
    # ---- 忙时拒绝
    api('send', {'sid': sid, 'text': 'SLOWTEST:8 慢一点'})
    wait_until(lambda: (L.runner_of(sid) or {}).get('busy'), 10)
    r = api('rewind-apply', {'sid': sid, 'uuid': u['R2']})
    check(r.get('_http') == 400 and '回复' in (r.get('error') or ''), f'回复中回退被拒绝：{r.get("error")}')
    check(api('rewind-preview', {'sid': sid, 'uuid': u['R2']}).get('busy') is True, '回复中预览带 busy')
    r = api('context-detail', {'sid': sid})
    check(r.get('_http') == 400, f'回复中 context-detail 不发命令：{r.get("error")}')
    check(idle(sid, 40), '慢回复结束')
    # ---- 回退预览、执行、撤销
    pv = api('rewind-preview', {'sid': sid, 'uuid': u['R2']})
    check({f['rel'] for f in pv['write']} == {'b.txt', 'c.txt', 'sub/s.txt'} and {f['rel'] for f in pv['remove']} == {'f.txt'}
          and not pv['conflicts'] and not pv['missing'] and pv['busy'] is False, f'预览回到 R2 之前 {pv}')
    pa = api('rewind-preview', {'sid': sid, 'uuid': u['R2'], 'after': True})
    check({f['rel'] for f in pa['write']} == {'b.txt', 'c.txt'} and {f['rel'] for f in pa['remove']} == {'f.txt'}, f'预览回到 R2 结束时 {pa}')
    check(api('rewind-preview', {'sid': sid, 'uuid': u['R4'], 'after': True}) == {'write': [], 'remove': [], 'conflicts': [], 'missing': [], 'busy': False},
          '最后一轮 after：没有要做的')
    snap = {n: rd(n) for n in ('a.txt', 'b.txt', 'c.txt', 'd.txt', 'f.txt', 'sub/s.txt', 'sub/r.txt')}
    t0 = time.time()
    ra = api('rewind-apply', {'sid': sid, 'uuid': u['R2']})
    check(ra.get('ok') and ra['written'] == 3 and ra['removed'] == 1, f'执行回退 {ra}')
    check(rd('b.txt') == 'B原始\n第二行\n' and rd('c.txt') == 'C原始\n' and rd('sub/s.txt') == 'S第一版\n' and rd('f.txt') is None
          and rd('a.txt') == 'A第一版\n' and rd('d.txt') == 'D原始\nD追加\n', '磁盘回到 R2 之前（只被 Bash 改的 d.txt 不动）')
    check(bool(ev.wait(lambda x: x.get('type') == 'rewind' and x.get('id') == ra['id'], 5, after=t0)), '推 rewind 事件')
    rw = api('turn-changes', {'sid': sid})['rewinds']
    check(rw and rw[-1]['id'] == ra['id'] and rw[-1]['written'] == 3 and not rw[-1]['undone'], 'turn-changes 列出回退记录')
    un = api('rewind-undo', {'id': ra['id']})
    check(un.get('ok') and un.get('restored') == 4 and {n: rd(n) for n in snap} == snap, f'撤销：全部回到回退前 {un}')
    # ---- 回退后 CLI 继续改同一个文件
    ra = api('rewind-apply', {'sid': sid, 'uuid': u['R4']})
    check(ra.get('ok') and rd('c.txt') == 'C原始\n', '回退到 R4 之前：c.txt 回到原样')
    api('send', {'sid': sid, 'text': 'R5 再改c ' + F([{'name': 'Read', 'input': {'file_path': P + '/c.txt'}}],
                                                   [{'name': 'Edit', 'input': {'file_path': P + '/c.txt', 'old_string': 'C原始', 'new_string': 'C第五轮'}}])})
    check(idle(sid), 'R5 结束')
    check(rd('c.txt') == 'C第五轮\n', '回退后 CLI 照常读、改同一个文件')
    u = prompts(sid)
    tc = api('turn-changes', {'sid': sid})
    by = {t['uuid']: {f['rel']: (f['st'], f['add'], f['del']) for f in t['files']} for t in tc.get('turns') or []}
    check(by.get(u['R4']) == {'c.txt': ('mod', 1, 1)} and by.get(u['R5']) == {'c.txt': ('mod', 1, 1)},
          f'回退过的 R4 仍显示它自己的改动，R5 显示本轮改动 {by.get(u["R4"])} {by.get(u["R5"])}')
    fork_checks(sid, u)
    # ---- 标记未读
    t0 = time.time()
    check(api('mark-unread', {'sid': sid}) == {'ok': True}, 'mark-unread')
    check(bool(ev.wait(lambda x: x.get('type') == 'marks' and x.get('sid') == sid and x.get('done_at', 0) >= t0 - 1, 5, after=t0)), '推 marks 事件（done_at 为现在）')
    L.ua_check(n0)
    ev.stop = True


def remember_sid(s):
    d = st_load()
    d.setdefault('sids', []).append(s)
    st_save(d)


def cache_uuid(sid):
    for l in L.tx_text(sid).splitlines():
        try:
            r = json.loads(l)
        except Exception:
            continue
        c = (r.get('message') or {}).get('content') if r.get('type') == 'user' else None
        if isinstance(c, str) and c.startswith('CACHETEST'):
            return r['uuid']
    return None


def fork_checks(sid, u):
    """修复线：分叉对话里继承自原对话的各轮（含分叉点那一轮）不显示改动、不能回退，只算分叉后新发生的轮。"""
    items = api('open', {'sid': sid}).get('items') or []
    r2 = next((it for it in items if it.get('uuid') == u['R2']), {})
    fk = api('fork-at', {'sid': sid, 'uuid': r2.get('fork') or ''}).get('sid')
    check(bool(fk), f'从 R2 结束处分叉：{fk}')
    if not fk:
        return
    remember_sid(fk)
    wait_until(lambda: (L.runner_of(fk) or {}).get('ready'), 40)
    want = [u['R1'], cache_uuid(sid), u['R2']]  # R1、CACHETEST 那一轮、R2（分叉点那一轮）
    tc = api('turn-changes', {'sid': fk})
    check(tc.get('inherited') == want and tc.get('turns') == [] and tc['all']['n'] == 0,
          f'分叉对话：R1、R2（分叉点那一轮）继承自原对话，不显示改动 {tc.get("inherited")} {[t["uuid"] for t in tc.get("turns") or []]}')
    for args, name in (({'uuid': u['R2']}, '分叉点那一轮之前'), ({'uuid': u['R2'], 'after': True}, '分叉点那一轮结束时'), ({'uuid': u['R1']}, 'R1 之前')):
        r = api('rewind-preview', dict(args, sid=fk))
        check(r.get('_http') == 400 and '原对话' in (r.get('error') or ''), f'分叉对话回退预览：{name}拒绝（{r.get("error")}）')
    r = api('rewind-apply', {'sid': fk, 'uuid': u['R2'], 'after': True})
    check(r.get('_http') == 400 and '原对话' in (r.get('error') or ''), '分叉对话回退执行：继承的轮拒绝')
    r = api('turn-diff', {'sid': fk, 'uuid': u['R2'], 'path': P + '/b.txt'})
    check(r.get('_http') == 400, '分叉对话 turn-diff：继承的轮拒绝')
    cur = rd('c.txt').strip()
    api('send', {'sid': fk, 'text': 'R6 分叉里改c ' + F([{'name': 'Read', 'input': {'file_path': P + '/c.txt'}}],
                                                      [{'name': 'Edit', 'input': {'file_path': P + '/c.txt', 'old_string': cur, 'new_string': 'C分叉'}}])})
    check(idle(fk), 'R6 结束')
    uf = prompts(fk)
    tc = api('turn-changes', {'sid': fk})
    by = {t['uuid']: {f['rel']: (f['st'], f['add'], f['del']) for f in t['files']} for t in tc.get('turns') or []}
    check(tc.get('inherited') == want and list(by) == [uf.get('R6')] and by[uf['R6']] == {'c.txt': ('mod', 1, 1)}
          and [f['rel'] for f in tc['all']['files']] == ['c.txt'], f'分叉后新发生的 R6 照常算改动，累计也只从分叉后算起 {by} {[f["rel"] for f in tc["all"]["files"]]}')
    pv = api('rewind-preview', {'sid': fk, 'uuid': uf['R6']})
    check([f['rel'] for f in pv.get('write') or []] == ['c.txt'], f'分叉后新发生的轮可以回退：{pv}')
    # 整段分叉（--fork-session）：原对话的轮都继承，只算分叉后新发生的
    wf = api('fork', {'sid': sid}).get('sid')
    check(bool(wf), f'整段分叉：{wf}')
    if not wf:
        return
    remember_sid(wf)
    wait_until(lambda: (L.runner_of(wf) or {}).get('ready'), 40)
    api('send', {'sid': wf, 'text': 'R7 整段分叉里新建 ' + F([{'name': 'Write', 'input': {'file_path': P + '/g.txt', 'content': 'G\n'}}])})
    check(idle(wf), 'R7 结束')
    uw = prompts(wf)
    tc = api('turn-changes', {'sid': wf})
    inh = tc.get('inherited') or []
    check(set(inh) >= {u['R1'], u['R2'], u['R3'], u['R4'], u['R5']} and uw.get('R7') not in inh
          and [t['uuid'] for t in tc.get('turns') or []] == [uw.get('R7')] and [f['rel'] for f in tc['all']['files']] == ['g.txt'],
          f'整段分叉：原对话的 {len(inh)} 轮都继承，只算 R7（新建 g.txt）')
    check(api('turn-changes', {'sid': sid}).get('inherited') == [], '原对话不受影响：inherited 为空')


def cleanup():
    """结束自己建的 CLI、测试服务、ptyd、假网关（都按 PID 核对命令行），测试会话记录移到废纸篓。"""
    d = st_load()
    sids = d.get('sids', [])
    if L.ping().startswith('ok'):
        for s in sids:
            try:
                api('stop', {'sid': s})
            except Exception:
                pass
        time.sleep(3)
    L.stop_server()
    pp = L.kill_ptyd(signal.SIGTERM)
    if pp:
        wait_until(lambda: not alive(pp), 10)
        say('测试 ptyd 已结束', pp)
    gw = d.get('gw')
    if gw and alive(gw) and 'fakegw.py' in cmd_of(gw):
        os.kill(gw, signal.SIGTERM)
        say('假网关已结束', gw)
    trash = os.path.join(HOME, '.Trash', 'cwb-r15-a1-' + time.strftime('%H%M%S'))
    n = 0
    for s in sids:
        for p in (os.path.join(L.PROJ_DIR, s + '.jsonl'), os.path.join(L.PROJ_DIR, s),
                  os.path.join(HOME, '.claude', 'session-env', s), os.path.join(HOME, '.claude', 'file-history', s)):
            if os.path.lexists(p):
                os.makedirs(trash, exist_ok=True)
                shutil.move(p, os.path.join(trash, os.path.basename(os.path.dirname(p)) + '__' + os.path.basename(p)))
                n += 1
    say('测试会话记录已移到废纸篓', len(sids), '个会话，', n, '项')
    d['sids'], d['gw'] = [], None
    st_save(d)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'cleanup':
        cleanup()
    else:
        try:
            run_all()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(False, f'异常 {e!r}')
        say('全部通过' if not L.FAIL else f'{len(L.FAIL)} 项失败：' + '；'.join(L.FAIL))
        sys.exit(1 if L.FAIL else 0)
