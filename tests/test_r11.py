"""第十一轮（开发线 A）单元测试：不启动 claude、不碰 ~/.claude，记录文件都写在临时目录里。
运行：.venv/bin/python -I tests/test_r11.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def test_paste():
    evil = '前文\x1b[201~\x03rm -rf ~\r\n\x1b[200~\x9b201~尾\t缩进\x07\x7f\u0085'
    b = S.paste_bytes(evil)
    check(b.startswith(b'\x1b[200~') and b.endswith(b'\x1b[201~') and b.count(b'\x1b') == 2, '控制字符：粘贴块里只有首尾两个 ESC，ESC[201~ 逃不出粘贴')
    t = S.safe_input(evil)
    check(not any((ord(c) < 32 and c not in '\n\t') or 0x7f <= ord(c) <= 0x9f for c in t), '控制字符：C0、C1、DEL 全部去掉，只留换行和制表符')
    check('[201~' in t and 'rm -rf ~' in t and '\r' not in t, '控制字符：可见文字保留，回车换成换行')
    # _flush 实际写进伪终端的字节
    r = S.Runner.__new__(S.Runner)
    got = []
    r.queue, r.sending, r.state = [(evil, ['/tmp/a\x1b[201~b.png'], False)], False, {'busy': False, 'alive': True}
    r.write, r.analyze, r.key = got.append, (lambda force=False: None), (lambda n: None)
    real = S.time.sleep
    S.time.sleep = lambda x: real(min(x, 0.01))
    try:
        r._flush()
        real(0.5)
    finally:
        S.time.sleep = real
    check(all(g.count(b'\x1b') in (0, 2) for g in got) and b''.join(got).count(b'\x1b[201~') == 2, '_flush：图片路径和正文各一个粘贴块，没有多余的 ESC')


def test_err_label():
    cases = [('rate_limit', '', '接口限流'), ('authentication_failed', '', '鉴权失败'), ('server_error', '', '网关错误'),
             ('unknown', 'API Error: 400 x', '请求被拒绝'), ('unknown', 'API Error: 502 x', '网关错误'), ('billing_error', '', '额度不足'),
             ('', '', '接口出错')]
    check(all(S.err_label(k, t) == w for k, t, w in cases), '错误归类：' + '、'.join(w for _, _, w in cases))


def rows_of(sid):
    """一份小记录：2 轮问答，第 1 轮有并行工具调用（结果挂在旁支上），还有一条被放弃的旁支提问和元数据行。"""
    R = []

    def add(typ, u, p, content, **kw):
        R.append(dict({'type': typ, 'uuid': u, 'parentUuid': p, 'sessionId': sid, 'isSidechain': False,
                       'timestamp': '2026-11-06T10:00:%02dZ' % len(R), 'message': {'role': typ, 'content': content}}, **kw))
    R.append({'type': 'session-meta', 'sessionId': sid, 'workDir': '/tmp'})
    add('user', 'u1', None, '第一轮提问', cwd='/tmp')
    add('assistant', 'a1', 'u1', [{'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {}}])
    add('assistant', 'a2', 'a1', [{'type': 'tool_use', 'id': 't2', 'name': 'Read', 'input': {}}])
    add('user', 'r1', 'a1', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x'}], sourceToolAssistantUUID='a1')
    add('user', 'r2', 'a2', [{'type': 'tool_result', 'tool_use_id': 't2', 'content': 'y'}], sourceToolAssistantUUID='a2')
    add('assistant', 'a3', 'r2', [{'type': 'text', 'text': '第一轮答完'}])
    R[-1]['message']['stop_reason'] = 'end_turn'
    R.append({'type': 'ai-title', 'aiTitle': '标题', 'sessionId': sid})
    add('user', 'x1', 'a3', '被放弃的旁支提问')
    add('user', 'u2', 'a3', '第二轮提问')
    add('assistant', 'b1', 'u2', [{'type': 'text', 'text': '第二轮答完'}])
    R[-1]['message']['stop_reason'] = 'end_turn'
    return R


def test_conv_and_fork():
    tmp = tempfile.mkdtemp(prefix='cwb-t11-')
    old = S.PROJECTS_DIR
    S.PROJECTS_DIR = tmp
    try:
        src = str(uuid.uuid4())
        os.makedirs(os.path.join(tmp, '-tmp'))
        sp = os.path.join(tmp, '-tmp', src + '.jsonl')
        with open(sp, 'w') as f:
            f.write('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows_of(src)) + '\n')
        c, out = S.Conv(), {'new': [], 'patch': []}
        for r in rows_of(src):
            c.feed(r, out, 0)
        us = [i for i in c.items if i['k'] == 'user']
        check([u.get('fork') for u in us] == ['a3', None, 'b1'], f'Conv：每轮答完记下最后一条记录的 uuid {[u.get("fork") for u in us]}')
        before = open(sp, 'rb').read()
        new = str(uuid.uuid4())
        dp = S.fork_transcript(src, 'a3', new)
        kept = [json.loads(l) for l in open(dp)]
        check(open(sp, 'rb').read() == before, '分叉：源记录一个字节都没变')
        check([r.get('uuid') for r in kept if r.get('uuid')] == ['u1', 'a1', 'a2', 'r1', 'r2', 'a3'],
              '分叉：只到分叉点为止，并行工具的旁支结果保留，之后的内容去掉')
        check({r.get('sessionId') for r in kept} == {new}, '分叉：每行 sessionId 换成新 ID')
        check(not any(r.get('type') == 'ai-title' for r in kept), '分叉：原标题行不带过去')
        try:
            S.fork_transcript(src, 'nope', str(uuid.uuid4()))
            check(False, '分叉：位置不存在要报错')
        except ValueError:
            check(True, '分叉：位置不存在时报错')
    finally:
        S.PROJECTS_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


FAKE_CLI = r'''
import os, sys, time, subprocess
os.setsid()                      # 像 pty.fork 一样：CLI 是会话首进程
tag = sys.argv[1]
# CLI 自己会话里的子进程（MCP、钩子之类）
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300) #%s-own' % tag])
# 工具命令：CLI 新开一个会话跑 shell，shell 起完后台进程就退出（实测 Bash 工具就是这样）
cmd = ("python3 -c 'import time;time.sleep(301)#%(t)s-bg' & "
       "nohup python3 -c 'import time;time.sleep(302)#%(t)s-nohup' >/dev/null 2>&1 & "
       "python3 -c 'import os,time;os.setsid();time.sleep(303)#%(t)s-setsid' & "
       "perl -e 'setpgrp(0,0); select(undef,undef,undef,304); #%(t)s-pgrp' & ") % {'t': tag}
subprocess.Popen(['/bin/sh', '-c', cmd], start_new_session=True).wait()
time.sleep(300)
'''


def procs(tag):
    out = {}
    for l in subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout.splitlines():
        p = l.split(None, 1)
        if len(p) == 2 and ('#' + tag + '-') in p[1] and 'ps -axo' not in p[1]:
            out[p[1].split('#' + tag + '-', 1)[1].split()[0].strip("'\")")] = int(p[0])
    return out


def test_reap():
    tag = 'cwbt' + uuid.uuid4().hex[:6]
    gen = 'test-' + tag
    env = dict(os.environ, CWB_GEN=gen, PATH='/usr/bin:/bin')
    cli = subprocess.Popen([sys.executable, '-c', FAKE_CLI, tag], env=env)
    try:
        for _ in range(50):
            if len(procs(tag)) >= 5:
                break
            time.sleep(0.1)
        before = procs(tag)
        check(set(before) == {'own', 'bg', 'nohup', 'setsid', 'pgrp'}, f'子进程：模拟出 5 种后代 {sorted(before)}')
        check(S.sighup_ignored(before.get('nohup', 0)) and not S.sighup_ignored(before.get('bg', 0)), '子进程：nohup 启动的能认出忽略了 SIGHUP')
        fam = S.cli_family(cli.pid, gen)
        os.killpg(cli.pid, signal.SIGHUP)  # 模拟：给 CLI 的进程组发 SIGHUP
        cli.wait(5)
        S.reap_family(fam, 'test')
        time.sleep(0.3)
        after = procs(tag)
        check('own' not in after and 'bg' in after and 'pgrp' in after, '子进程：CLI 自己会话里留下的被清理，工具会话里 & 起的保留')
        check('nohup' in after and 'setsid' in after, '子进程：nohup、setsid 脱离出去的不动')
    finally:
        try:
            cli.kill()
        except Exception:
            pass
        for p in procs(tag).values():  # 只结束本测试起的进程
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


def test_hook_gen():
    class R:
        gen, sid, unattended = 'g-1', 's-new', False
    r = R()
    S.RUNNERS.clear()
    S.RUNNERS['s-new'] = r
    S.ALIAS['s-old'] = ('s-new', time.time())
    check(S.hook_runner('s-old', 'g-1') == (r, True), '代次：带代次按代次找到 Runner（/clear 后环境变量里还是旧 ID）')
    check(S.hook_runner('s-new', 'g-0') == (None, True), '代次：代次对不上视为旧进程迟到的事件')
    check(S.hook_runner('s-old', '') == (r, False), '代次：不带代次（旧版 hook.py）按 ID 和别名兼容')
    check(S.handle_hook('busy', 's-new', {}, 'g-0') == {}, '代次：旧代次的 busy 直接丢弃')
    S.RUNNERS.clear()
    S.ALIAS.clear()


if __name__ == '__main__':
    test_paste()
    test_err_label()
    test_conv_and_fork()
    test_reap()
    test_hook_gen()
    print('全部通过' if not FAIL else f'{len(FAIL)} 项失败')
    sys.exit(1 if FAIL else 0)
