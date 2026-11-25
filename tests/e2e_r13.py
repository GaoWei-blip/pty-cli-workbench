"""第十三轮（开发线 E）端到端场景。用法：.venv/bin/python -I tests/e2e_r13.py <场景>
场景：start / stop / s1（回复中重启 server）/ s2（授权卡等待中重启）/ s3（重启后发消息、排队、立即发送、中断、/clear）/
     s4（kill -9 ptyd、继续、熔断）/ s5（版本不一致走升级）/ s6（200KB 粘贴不卡其他会话）
"""
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_lib import *  # noqa: E402,F403
import e2e_lib as L  # noqa: E402


def busy_live(sid):
    st = runner_of(sid)
    return st if st and st.get('busy') and st.get('live') else None


def idle_done(sid, needle):
    st = runner_of(sid)
    return st if st and not st.get('busy') and not st.get('turn') and needle in tx_text(sid) else None


def s1():
    """回复进行中重启 server：CLI 不中断，重启后忙碌、实时预览、转圈正确，这一轮正常结束，记录完整。"""
    n0 = len(gw_rows())
    sid = new_session()
    api('send', {'sid': sid, 'text': 'SLOWTEST:30 请慢慢回复'})
    st = wait_until(lambda: busy_live(sid), 30)
    check(bool(st), f"重启前：回复进行中，转圈“{(st or {}).get('spinner')}”，预览 {len((st or {}).get('live') or '')} 字")
    cli = (screen(sid).get('ptyd') or {}).get('pid')
    pp = ptyd_pid()
    old, new, dt = restart_server()
    ev = Events()
    st2 = wait_until(lambda: busy_live(sid), 15)
    check(bool(st2) and alive(cli) and ptyd_pid() == pp, f'重启后（{dt:.1f}s）：同一个 CLI（{cli}）、同一个 ptyd（{pp}）还在跑，接管后仍在忙')
    check(bool(st2) and bool(st2.get('spinner')) and bool(st2.get('turn')) and st2.get('since', 0) > 0,
          f"重启后：转圈“{(st2 or {}).get('spinner')}”、回合中、since 还原为 {(st2 or {}).get('since')}")
    o = api('open', {'sid': sid})
    check(o.get('runner', {}).get('busy') and '慢速流式' in (o.get('runner', {}).get('live') or ''), '重启后：打开对话拿到忙碌状态和实时预览正文')
    sc = screen(sid)
    check((sc.get('ptyd') or {}).get('seq', 0) >= (sc.get('ptyd') or {}).get('server_seq', 1) > 0, f"重启后：server 屏幕接到 seq {(sc.get('ptyd') or {}).get('server_seq')}")
    lv = ev.wait(lambda e: e.get('type') == 'runner' and e.get('sid') == sid and e['state'].get('busy') and e['state'].get('live'), 10)
    check(bool(lv), '重启后：界面事件流继续推送忙碌状态和实时预览')
    st3 = wait_until(lambda: idle_done(sid, '第40段'), 60)
    tx = tx_text(sid)
    n_par = len(set(re.findall(r'第(\d+)段', tx)))
    check(bool(st3) and n_par == 40, f'这一轮正常结束：记录里 40 段齐全（{n_par}），回合结束')
    check(marks().get('done_at', {}).get(sid, 0) > 0 and not marks().get('interrupted', {}).get(sid), '结束后记下 done_at（未读），没有中断标记')
    ua_check(n0)
    d = st_load()
    d['s1'] = sid
    st_save(d)


def s2():
    """授权卡等待中重启 server：重启后授权卡重新出现，点允许后工具照常执行。"""
    n0 = len(gw_rows())
    try:
        os.remove(os.path.join(PROJ, 'perm_ok.txt'))
    except OSError:
        pass
    sid = new_session({'mode': 'manual mode'})
    ev = Events()
    t0 = time.time()
    api('send', {'sid': sid, 'text': 'TOOLTEST 请写一个文件'})
    p1 = ev.wait(lambda e: e.get('type') == 'perm' and e['req']['sid'] == sid, 40, t0)
    check(bool(p1), f"重启前：授权卡出现（{(p1 or {}).get('req', {}).get('tool')}）")
    old, new, dt = restart_server()
    t1 = time.time()
    p2 = ev.wait(lambda e: e.get('type') == 'perm' and e['req']['sid'] == sid, 45, t1)
    o = api('open', {'sid': sid})
    check(bool(p2) and p2['req']['rid'] != p1['req']['rid'], f"重启后 {time.time() - t1:.1f}s：授权卡重新出现（新 rid {(p2 or {}).get('req', {}).get('rid')}），打开对话也带着 {len(o.get('perms') or [])} 张")
    check(runner_of(sid).get('attention') and runner_of(sid).get('turn'), '重启后：这个对话显示“等你确认”、回合中')
    api('perm', {'rid': p2['req']['rid'], 'decision': 'allow'})
    st = wait_until(lambda: idle_done(sid, '工具已经执行完毕'), 40)
    ok = os.path.exists(os.path.join(PROJ, 'perm_ok.txt')) and open(os.path.join(PROJ, 'perm_ok.txt')).read().strip() == 'cwb-perm-ok'
    check(bool(st) and ok, '点允许后工具照常执行：perm_ok.txt 写入成功，这一轮正常结束')
    ua_check(n0)


def s3():
    """重启后发消息、排队、立即发送、中断、/clear 都正常。"""
    n0 = len(gw_rows())
    N = time.strftime('%H%M%S')  # 每次运行的消息都不一样，避免沿用旧记录里的文字
    sid = st_load().get('s1') or new_session()
    restart_server()
    ev = Events()
    api('send', {'sid': sid, 'text': f'重启后的第一条消息{N}'})
    check(bool(wait_until(lambda: idle_done(sid, f'收到：重启后的第一条消息{N}'), 40)), '重启后发消息：正常回复')
    # 排队：回复进行中再发一条
    api('send', {'sid': sid, 'text': 'SLOWTEST:12 慢一点'})
    wait_until(lambda: busy_live(sid), 20)
    api('send', {'sid': sid, 'text': f'排队的消息{N}'})
    q = wait_until(lambda: f'排队的消息{N}' in tx_text(sid), 40)
    check(bool(q), '排队：回复进行中发的消息被 CLI 收下（插入当前回复或下一轮）')
    wait_until(lambda: idle_done(sid, f'收到：排队的消息{N}') or idle_done(sid, f'排队的消息{N}'), 60)
    # 立即发送
    api('send', {'sid': sid, 'text': 'SLOWTEST:20 再慢一点'})
    wait_until(lambda: busy_live(sid), 20)
    t0 = time.time()
    api('send', {'sid': sid, 'text': f'立即发送的消息{N}', 'now': True})
    got = wait_until(lambda: f'立即发送的消息{N}' in tx_text(sid), 20)
    check(bool(got) and time.time() - t0 < 15, f'立即发送：{time.time() - t0:.1f}s 内进了记录（没等慢回复结束）')
    wait_until(lambda: runner_of(sid) and not runner_of(sid).get('busy') and not runner_of(sid).get('turn'), 60)
    # 中断
    api('send', {'sid': sid, 'text': 'SLOWTEST:30 这条会被中断'})
    wait_until(lambda: busy_live(sid), 20)
    k0 = tx_text(sid).count('Request interrupted')
    api('interrupt', {'sid': sid})
    st = wait_until(lambda: (lambda s: s if s and not s.get('busy') and not s.get('turn') else None)(runner_of(sid)), 15)
    k1 = wait_until(lambda: tx_text(sid).count('Request interrupted') > k0, 10)
    check(bool(st) and bool(k1), '中断：Esc 后回合结束，记录里多了一条“已中断”')
    check(not marks().get('interrupted', {}).get(sid), '中断：用户主动中断不记“上次回复被中断”')
    # 中断后再重启：仍然不算被中断
    restart_server()
    time.sleep(4)
    check(not marks().get('interrupted', {}).get(sid) and not runner_of(sid).get('turn'), '中断后重启 server：接管后不在回合中，也不记中断')
    # /clear
    t1 = time.time()
    api('send', {'sid': sid, 'text': '/clear'})
    rk = ev.wait(lambda e: e.get('type') == 'rekey' and e.get('old') == sid, 30, t1)
    nsid = (rk or {}).get('new')
    check(bool(nsid), f'/clear：换了会话 ID {sid[:8]} → {(nsid or "")[:8]}')
    time.sleep(1)
    meta = (screen(nsid).get('ptyd') or {}).get('meta') or {}
    check(meta.get('sid') == nsid, 'clear 后：真实 sid 写回 ptyd 的 meta')
    restart_server()
    time.sleep(2)
    check(bool(runner_of(nsid)) and not runner_of(sid), 'clear 后重启 server：按 meta 接管到新 ID 上，旧 ID 不再有 CLI')
    api('send', {'sid': nsid, 'text': f'clear 之后的消息{N}'})
    check(bool(wait_until(lambda: idle_done(nsid, f'收到：clear 之后的消息{N}'), 40)), 'clear 后重启再发消息：钩子按代次认得，正常回复')
    d = st_load()
    d['s3'] = nsid
    d.setdefault('sids', []).append(nsid)
    st_save(d)
    ua_check(n0)


def s4():
    """kill -9 ptyd：标出“可继续”，点继续能续接；熔断生效。"""
    n0 = len(gw_rows())
    sid = st_load().get('s3') or new_session()
    ev = Events()
    api('send', {'sid': sid, 'text': 'SLOWTEST:30 这条会因为 ptyd 崩溃被打断'})
    check(bool(wait_until(lambda: busy_live(sid), 20)), '崩溃前：回复进行中')
    cli = (screen(sid).get('ptyd') or {}).get('pid')
    t0 = time.time()
    old = kill_ptyd()
    mk = ev.wait(lambda e: e.get('type') == 'marks' and e.get('sid') == sid and e.get('interrupted'), 10, t0)
    check(bool(old) and bool(mk) and mk['interrupted']['why'] == 'ptyd', f'kill -9 ptyd（{old}）：这个对话记为被中断（ptyd）')
    check(bool(wait_until(lambda: not alive(cli), 5)), 'kill -9 ptyd：CLI 跟着退出')
    newp = wait_until(lambda: (lambda p: p if p and p != old and alive(p) else None)(ptyd_pid()), 10)
    check(bool(newp), f'自动重新拉起 ptyd（{newp}）')
    o = api('open', {'sid': sid})
    sess = [s for p in api('projects')['projects'] for s in p['sessions'] if s['sid'] == sid]
    check(bool(o.get('interrupted')) and bool(sess) and sess[0].get('resumable'), '打开对话带“上次回复被中断”，左栏数据标“可继续”')
    t1 = time.time()
    api('send', {'sid': sid, 'text': '继续'})
    clr = ev.wait(lambda e: e.get('type') == 'marks' and e.get('sid') == sid and 'interrupted' in e and not e['interrupted'], 10, t1)
    st = wait_until(lambda: idle_done(sid, '收到：继续'), 60)
    check(bool(clr) and bool(st) and not marks().get('interrupted', {}).get(sid), '点继续：用 --resume 续接，回复正常，中断标记清掉')
    # 熔断：短时间内连续崩溃 3 次就不再自动拉起
    pids = [old]
    for i in range(2):
        p = wait_until(lambda: (lambda x: x if x and x not in pids and alive(x) else None)(ptyd_pid()), 10)
        if not p:
            break
        time.sleep(1)
        kill_ptyd()
        pids.append(p)
    time.sleep(3)
    dm = screen(sid).get('daemon') or api('screen', {}).get('daemon')
    cur = ptyd_pid()
    check(len(pids) == 3 and dm.get('broken') and not alive(cur), f'熔断：5 分钟内第 3 次崩溃后不再自动拉起（{pids}，状态 {dm.get("crashes")} 次）')
    r = api('send', {'sid': sid, 'text': '熔断时发消息'})
    check(r.get('_http') == 400 and '反复崩溃' in (r.get('error') or ''), f"熔断时发消息给出中文提示：{r.get('error')}")
    restart_server()  # 新服务从头计数（App 看护重启服务也是这样），后面的场景接着用
    ua_check(n0)


def s5():
    """ptyd 版本不一致：标记待升级，等所有对话空闲时关掉 CLI、换新 ptyd；之后发消息自动 --resume。"""
    n0 = len(gw_rows())
    sid = st_load().get('s3') or new_session()
    api('send', {'sid': sid, 'text': '升级前先说一句'})
    wait_until(lambda: idle_done(sid, '收到：升级前先说一句'), 40)
    path = os.path.join(L.ROOT, 'ptyd.py')
    src = open(path, encoding='utf-8').read()
    old_pid, cli = ptyd_pid(), (screen(sid).get('ptyd') or {}).get('pid')
    api('send', {'sid': sid, 'text': 'SLOWTEST:15 升级前的长回复'})
    wait_until(lambda: busy_live(sid), 20)
    try:
        open(path, 'w', encoding='utf-8').write(src.replace('PROTO = 1  #', 'PROTO = 2  #', 1))
        restart_server()
        dm = api('screen', {}).get('daemon')
        check('协议版本 1，需要 2' in (dm.get('upgrade') or ''), f"新 server 发现版本不一致，标记待升级：{dm.get('upgrade')}")
        time.sleep(5)
        check(ptyd_pid() == old_pid and alive(cli) and runner_of(sid).get('busy'), '有对话在回复：不升级，CLI 照常运行')
        wait_until(lambda: idle_done(sid, '升级前的长回复') and '第40段' in tx_text(sid), 40)
        newp = wait_until(lambda: (lambda p: p if p and p != old_pid and alive(p) else None)(ptyd_pid()), 25)
        dm = api('screen', {}).get('daemon')
        check(bool(newp) and not alive(old_pid) and not alive(cli) and dm.get('proto') == 2 and not dm.get('upgrade'),
              f'回复结束、全部空闲后：关掉 CLI、旧 ptyd（{old_pid}）退出、新 ptyd（{newp}，协议 {dm.get("proto")}）接上')
        check(not marks().get('interrupted', {}).get(sid), '升级关掉的 CLI 不算被中断')
        api('send', {'sid': sid, 'text': '升级之后的消息'})
        check(bool(wait_until(lambda: idle_done(sid, '收到：升级之后的消息'), 40)), '升级后发消息：自动 --resume 续接，对话不丢')
    finally:
        open(path, 'w', encoding='utf-8').write(src)
    # 改回去：再走一次（这次没有回复在跑，空闲即换）
    restart_server()
    p3 = wait_until(lambda: (lambda p: p if p and p != newp and alive(p) and not api('screen', {}).get('daemon', {}).get('upgrade') else None)(ptyd_pid()), 30)
    check(bool(p3) and api('screen', {}).get('daemon', {}).get('proto') == 1, f'恢复原版本后：空闲时又换回协议 1 的 ptyd（{p3}）')
    ua_check(n0)


def s6():
    """大段粘贴（200KB）不卡住其他会话。"""
    n0 = len(gw_rows())
    a = st_load().get('s3') or new_session()
    b = new_session()
    ev = Events()
    api('send', {'sid': a, 'text': 'SLOWTEST:20 另一个会话在慢慢输出'})
    wait_until(lambda: busy_live(a), 20)
    big = ('这是一段很长的粘贴内容，用来测试大段粘贴。' * 20 + '\n') * 480
    size = len(big.encode())
    sent = '大段粘贴开始\n' + big + '大段粘贴结束'
    t0 = time.time()
    r = api('send', {'sid': b, 'text': sent}, timeout=60)
    t_api = time.time() - t0
    time.sleep(6)
    evs = ev.since(t0, lambda e: e.get('type') == 'runner' and e.get('sid') == a and e['state'].get('live'))
    gaps = [b2[0] - b1[0] for b1, b2 in zip(evs, evs[1:])]
    lives = {e['state']['live'][-30:] for _, e in evs}
    check(r.get('ok') and t_api < 2 and len(lives) >= 5 and (max(gaps) if gaps else 99) < 2.5,
          f'粘贴 {size // 1024}KB：接口 {t_api:.2f}s 返回；期间另一会话预览更新 {len(lives)} 次，最长间隔 {max(gaps) if gaps else 0:.2f}s')
    # 修复线：CLI 2.1.321 把大段粘贴包进带属性的 <pasted_content id=…> 标签，假网关回显的首行是标签而不是“大段粘贴开始”；
    # 改成等“收到：…（共 N 字）”这条回复，再核对网关收到的字数不少于发出的（开头、结尾都在）
    rep = lambda: (lambda st, m: m if st and not st.get('busy') and not st.get('turn') and m else None)(
        runner_of(b), re.search(r'收到：.{0,120}?（共 (\d+) 字）', tx_text(b)))
    m = wait_until(rep, 90)
    rows = [x for x in gw_rows()[n0:] if x.get('tools') and '大段粘贴结束' in (x.get('last') or '')]
    got = rows[-1]['len'] if rows else 0
    check(bool(m) and int(m.group(1)) == got and got >= len(sent) > 190000,
          f"粘贴的内容完整送到：发出 {len(sent)} 字，网关收到 {got} 字（含 CLI 加的粘贴标签），回复里的字数 {m.group(1) if m else '无'}")
    wait_until(lambda: idle_done(a, '另一个会话在慢慢输出') and '第40段' in tx_text(a), 40)
    api('stop', {'sid': b})
    ua_check(n0)




def s7():
    """接管的几条路径：server 不在时这一轮结束（Stop 钩子没送到）、模拟关机、server 不在时 CLI 崩溃、server 在时 ptyd 收到停止信号。"""
    n0 = len(gw_rows())
    sid = new_session()
    # A：server 不在时这一轮结束：hook.py 重试 5 秒后放弃，靠 .run/hooks/<代次>.json 还原
    api('send', {'sid': sid, 'text': 'SLOWTEST:8 server 不在时结束'})
    wait_until(lambda: busy_live(sid), 20)
    gen = ((screen(sid).get('ptyd') or {}).get('meta') or {}).get('gen')
    stop_server()
    hf = os.path.join(L.RUN, 'hooks', gen + '.json')
    ev_idle = wait_until(lambda: (lambda d: d if d.get('ev') == 'idle' else None)(json.load(open(hf))) if os.path.exists(hf) else None, 30)
    time.sleep(6)  # 等 hook.py 放弃
    start_server()
    st = runner_of(sid)
    check(bool(ev_idle) and st and not st.get('busy') and not st.get('turn') and abs(marks()['done_at'].get(sid, 0) - ev_idle['t']) < 0.01,
          'server 不在时这一轮结束：事件文件记着 idle，接管后不在回合中，done_at 按事件时间补记')
    # B：模拟关机：server 先停，ptyd 收到停止信号结束所有 CLI；下次启动按事件文件记成被中断
    api('send', {'sid': sid, 'text': 'SLOWTEST:30 关机时还在回复'})
    wait_until(lambda: busy_live(sid), 20)
    cli = (screen(sid).get('ptyd') or {}).get('pid')
    stop_server()
    pp = kill_ptyd(signal.SIGTERM)
    wait_until(lambda: not alive(pp) and not alive(cli), 15)
    start_server()
    m = marks().get('interrupted', {}).get(sid)
    o = api('open', {'sid': sid})
    check(bool(pp) and not alive(cli) and bool(m) and m['why'] == 'shutdown' and o.get('interrupted'),
          f'模拟关机（server 停 + ptyd 收到 SIGTERM）：启动后记成被中断（{(m or {}).get("why")}），打开对话带“上次回复被中断”')
    api('send', {'sid': sid, 'text': '继续'})
    check(bool(wait_until(lambda: idle_done(sid, '收到：继续'), 40)) and not marks().get('interrupted', {}).get(sid), '关机后点继续：续接成功，标记清掉')
    # C：server 不在时 CLI 自己崩溃（SIGKILL 它）：ptyd 留着退出码，接管后显示“CLI 已退出”并记成被中断
    api('send', {'sid': sid, 'text': 'SLOWTEST:30 CLI 会崩溃'})
    wait_until(lambda: busy_live(sid), 20)
    cli = (screen(sid).get('ptyd') or {}).get('pid')
    stop_server()
    if cli and 'claude' in cmd_of(cli):
        os.kill(cli, signal.SIGKILL)
    time.sleep(2)
    start_server()
    time.sleep(1)
    o = api('open', {'sid': sid})
    m = marks().get('interrupted', {}).get(sid)
    check((o.get('exit') or {}).get('code') == -9 and bool(m) and m['why'] == 'exit',
          f"server 不在时 CLI 崩溃：接管后显示退出码 {(o.get('exit') or {}).get('code')}，记成被中断（{(m or {}).get('why')}）")
    # D：server 在时 ptyd 收到停止信号（stop.sh 的做法）：回合中的记成被中断，不算崩溃、不立即重拉
    api('send', {'sid': sid, 'text': 'SLOWTEST:30 ptyd 被停止'})
    wait_until(lambda: busy_live(sid), 20)
    c0 = api('screen', {}).get('daemon', {}).get('crashes')
    pp = kill_ptyd(signal.SIGTERM)
    time.sleep(5)
    dm = api('screen', {}).get('daemon', {})
    m = marks().get('interrupted', {}).get(sid)
    check(bool(m) and m['why'] == 'shutdown' and dm.get('crashes') == c0 and not dm.get('connected'),
          f'server 在时 ptyd 收到停止信号：记成被中断（{(m or {}).get("why")}），不计崩溃（{dm.get("crashes")}），不自动重拉')
    api('send', {'sid': sid, 'text': '继续'})
    check(bool(wait_until(lambda: idle_done(sid, '收到：继续') and tx_text(sid).count('收到：继续') >= 2, 40)) and ptyd_pid() != pp,
          f'之后发消息时才拉起新的 ptyd（{ptyd_pid()}），续接成功')
    ua_check(n0)


def s8():
    """长时间没有窗口连接：有对话在跑时不退出；全部空闲后先关 CLI、再让 ptyd 退出、最后自己退出（测试把时限调成 5 秒，每 60 秒检查一次）。"""
    sid = st_load().get('s3') or new_session()
    stop_server()
    start_server(CWB_IDLE_EXIT='5')
    api('send', {'sid': sid, 'text': 'SLOWTEST:70 长回复期间不退出'})
    wait_until(lambda: busy_live(sid), 20)
    cli = (screen(sid).get('ptyd') or {}).get('pid')
    srv, pp = st_load()['server'], ptyd_pid()
    time.sleep(62)
    check(alive(srv) and alive(pp) and alive(cli), '有对话在回复：60 秒检查时不退出')
    gone = wait_until(lambda: not alive(srv) and not alive(pp) and not alive(cli), 130, 1)
    check(bool(gone) and os.path.exists(os.path.join(L.RUN, f'stopped-{L.PORT}')) and '第40段' in tx_text(sid),
          '回复结束、全部空闲后：先关 CLI、ptyd 退出、server 退出并留停止标记；回复完整')
    try:
        os.remove(os.path.join(L.RUN, f'stopped-{L.PORT}'))
    except OSError:
        pass


def s9():
    """部署脚本 --server-now：回复进行中立即重启 server（测试里由脚本代替 App 看护拉起）；/api/screen 的重画兜底。"""
    n0 = len(gw_rows())
    if not ping().startswith('ok'):
        start_server()
    sid = st_load().get('s3') or new_session()
    api('send', {'sid': sid, 'text': 'SLOWTEST:20 部署时还在回复'})
    wait_until(lambda: busy_live(sid), 30)
    cli, srv = (screen(sid).get('ptyd') or {}).get('pid'), st_load()['server']
    env = dict(os.environ, CWB_PORT=str(L.PORT))
    subprocess.run([os.path.join(L.ROOT, '.venv/bin/python'), '-I', os.path.join(L.ROOT, 'restart_when_idle.py'), '--server-now'], env=env, timeout=10)
    gone = wait_until(lambda: not alive(srv), 10, 0.1)
    start_server()  # 代替 App 的看护（start.sh --ensure）
    st = wait_until(lambda: busy_live(sid), 10)
    check(bool(gone) and bool(st) and alive(cli), f'--server-now：不等空闲直接结束 server（{srv}），新 server 接管后同一个 CLI（{cli}）还在回复')
    wait_until(lambda: idle_done(sid, '部署时还在回复') and '第40段' in tx_text(sid), 40)
    time.sleep(1)
    lg = open(os.path.join(L.RUN, 'restart_when_idle.log'), encoding='utf-8').read().splitlines()[-3:]
    check(any('立即结束旧服务' in x for x in lg), '部署脚本日志：' + ' | '.join(lg))
    sc0 = screen(sid)
    s0 = (sc0.get('ptyd') or {}).get('seq', 0)
    sc1 = screen(sid, redraw=True)
    p1 = sc1.get('ptyd') or {}
    check(p1.get('seq', 0) - s0 > 500 and p1.get('same'), f"重画兜底：缩一行再还原后 CLI 整屏重画（多收 {p1.get('seq', 0) - s0} 字节），server 屏幕与 ptyd 快照一致")
    check(runner_of(sid).get('ready') and not runner_of(sid).get('busy'), '重画后：输入框认得出、状态正常')
    ua_check(n0)


if __name__ == '__main__':
    sc = sys.argv[1] if len(sys.argv) > 1 else ''
    if sc == 'start':
        start_server()
    elif sc == 'stop':
        stop_server()
    else:
        globals()[sc]()
        say('全部通过' if not L.FAIL else f'{len(L.FAIL)} 项失败：' + '；'.join(L.FAIL))
