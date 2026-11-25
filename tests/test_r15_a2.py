"""第十五轮（开发线 A2）单元测试：命令拦截与清单、读屏匹配函数（用实测的屏幕原文做样例）、计划决定与拒绝并说明、
旁问答案判断、压缩与添加文件夹钩子、会话条目 uuid、钩子成功提示行过滤、hook.py 只观测事件不覆盖事件文件。
不启动 claude、不碰 ~/.claude。运行：.venv/bin/python -I tests/test_r15_a2.py
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S  # noqa: E402

FAIL = []
RULE = '─' * 80


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def box(text=''):
    """输入框：两条横线夹着 ❯ 行，下面是状态栏。"""
    return [RULE, '❯ ' + text if text else '❯', RULE, '  ⏵⏵ bypass permissions on (shift+tab to cycle)']


# ---------------------------------------------------------------- 屏幕样例（verify.md 与本线实测原文）
TASK_DETAIL = ['⏺ 两个后台已启动', '', '  Shell details', '', '  Status:   running', '  Runtime:  11s',
               '  Command:  sleep 300; echo bg-done', '', '  Output:', '  No output available', '',
               '  ← to go back · Esc/Enter/Space to close · x to stop']
TASK_LIST2 = ['❯ /tasks', '', RULE, '  Background', '  2 active shells', '', '  ❯ ⏺ sleep 304; echo bg-five   running',
              '    ⏺ sleep 303; echo bg-four   running', '', '      Earlier in this session (0)', '',
              '  ↑/↓ to select · Enter to view · x to stop · Esc to close']
TASK_LIST2_MOVED = TASK_LIST2[:6] + ['    ⏺ sleep 304; echo bg-five   running', '  ❯ ⏺ sleep 303; echo bg-four   running'] + TASK_LIST2[8:]
TASK_EMPTY = ['  Background', '', '  No tasks currently running', '', '  ❯ Earlier in this session (0)', '',
              '  ↑/↓ to select · Esc to close']
BTW_IDLE = ['❯ /btw 测试问题一', '', '  /btw 测试问题一', '', '    收到：测试问题一（共 5 字）', '',
            '  ↑/↓ to scroll · c to copy · f to fork · Esc to close']
BTW_BUSY = ['  第14段：这是慢速流式回复的内容，用来模拟回复进行中。', '', '  /btw 测试问题一', '  /btw 测试问题二', '',
            '    收到：测试问题二（共 5 字）', '', '  ⇧←/→ to browse · c to copy · f to fork · x to clear history · Esc to close',
            ''] + box()
ADD_DIR = ['❯ /add-dir /tmp/cwb-r15v-adddir', '', RULE, '  Add directory to workspace', '', '  /tmp/cwb-r15v-adddir', '',
           '  Claude Code will be able to read files in this directory and make edits when auto-accept edits is on.', '',
           '  ❯ 1. Yes, for this session', '    2. Yes, and remember this directory', '    3. No', '',
           '  Enter to confirm · Esc to cancel']
PLAN_PANEL = [' Ready to code?', '', " Here is Claude's plan:", '╌' * 50, ' 测试计划（文件版）', '', ' 1. 第一步：改 a.txt', '╌' * 50, '',
              ' Claude has written up a plan and is ready to execute. Would you like to proceed?', '',
              ' ❯ 1. Yes, and use auto mode', '   2. Yes, manually approve edits', '   3. Tell Claude what to change',
              '      shift+tab to approve with this feedback', '', ' ctrl+g to edit in Vim · ~/.claude/plans/p1.md']


class FakeR:
    """代替 Runner：屏幕按给定行返回；write 记下发出的字节，可选模拟 CLI 的反应。"""

    def __init__(self, lines, sid='s-fake-a2'):
        self.sid, self.lines, self.written = sid, list(lines), []
        self.state = {'alive': True, 'ready': True, 'dialog': None, 'busy': False}
        self.queue, self.sending, self.turn, self.panel_op, self.dirs = [], False, False, None, []
        self.qlock = threading.Lock()
        self.on_write = None
        self.analyzed = 0

    def screen_lines(self):
        return list(self.lines)

    def write(self, b):
        self.written.append(b)
        if self.on_write:
            self.on_write(b)

    def key(self, name):
        self.written.append(name)

    def analyze(self, force=False):
        self.analyzed += 1

    def sync_meta(self):
        pass


# ---------------------------------------------------------------- 命令拦截与清单
def test_commands():
    for t, want in [('/model', '/model'), ('/Model opus', '/model'), ('/MODEL', '/model'), ('  /config theme=dark', '/config'),
                    ('/fork 新任务', '/fork'), ('/bg', '/bg'), ('/background 跑着', '/background'), ('/effort max', '/effort'),
                    ('/fast on', '/fast'), ('/theme', '/theme'), ('/statusline', '/statusline'), ('/output-style x', '/output-style'),
                    ('/plugin install x', '/plugin'), ('/skills', '/skills'), ('/autocompact 1000', '/autocompact'),
                    ('/model\n第二行', '/model')]:
        bc = S.blocked_cmd(t)
        check(bool(bc) and bc[0] == want and bc[1], f'拦截 {t!r} → {bc and bc[0]}')
    for t in ('/models', '/compact', '/compact 侧重', 'hello /model', '/plugin-authoring', '/skills:foo', '/btw 问一下', 'model', '/model-x'):
        check(S.blocked_cmd(t) is None, f'不拦截 {t!r}')
    hard = {'model', 'effort', 'fast', 'config', 'theme', 'autocompact', 'statusline', 'output-style', 'plugin', 'skills', 'background', 'fork'}
    check(hard <= set(S.BLOCKED_MAP), '硬约束里的 12 个命令都在拦截表里')
    # send：被拦截的命令不发给 CLI（不会起 CLI），返回 {error, blocked, hint}
    n0 = len(S.RUNNERS)
    r = S.api_send({'sid': 'no-such-sid', 'text': '/Model opus'})
    check(r.get('blocked') is True and r.get('error') and r.get('hint') and r.get('cmd') == '/model' and len(S.RUNNERS) == n0,
          f'send 拦截 /Model opus：{r}')
    # /btw、/add-dir 在没有运行中的 CLI 时给出错误，不当普通消息发
    for t in ('/btw 问一下', '/add-dir /tmp'):
        try:
            S.api_send({'sid': 'no-such-sid', 'text': t})
            ok = False
        except ValueError as e:
            ok = '没有在运行' in str(e)
        check(ok and len(S.RUNNERS) == n0, f'send {t} 在对话没运行时报错、不起 CLI')
    # commands：内置命令带 group 与中文 desc，不含被拦截的
    cmds = S.api_commands({'cwd': '/nonexistent-a2'})['commands']
    built = {c['name']: c for c in cmds if c['src'] == '内置'}
    need = {'compact', 'export', 'plan', 'context', 'btw', 'code-review', 'security-review', 'simplify', 'init', 'recap'}
    check(need <= set(built), f'内置命令包含 {sorted(need)}')
    check(all(c.get('group') in ('chat', 'review') and c.get('desc') and any('一' <= ch <= '鿿' for ch in c['desc'])
              for c in built.values()), '内置命令都有 group（chat/review）和中文 desc')
    check({built[n]['group'] for n in ('code-review', 'security-review', 'simplify')} == {'review'} and built['compact']['group'] == 'chat',
          '审查类命令分在 review，其余在 chat')
    check(not any(c['name'].lower() in S.BLOCKED_MAP for c in cmds), '清单里没有被拦截的命令')
    # projects：带 blockedCmds（替换掉扫描真实目录的部分）
    pp, ml = S.projects_payload, S.model_list
    S.projects_payload, S.model_list = (lambda: []), (lambda: [])
    try:
        bl = S.api_projects({}).get('blockedCmds')
    finally:
        S.projects_payload, S.model_list = pp, ml
    check(isinstance(bl, list) and {'cmd': '/model', 'hint': S.BLOCKED_MAP['model']} in bl and all(x['cmd'].startswith('/') and x['hint'] for x in bl),
          f'projects 返回 blockedCmds（{len(bl or [])} 条）')


# ---------------------------------------------------------------- 读屏匹配
def test_screen_helpers():
    check(S.tasks_detail(TASK_DETAIL) == 'sleep 300; echo bg-done', '详情页读出 Command')
    check(S.tasks_list(TASK_DETAIL) is None and S.tasks_detail(TASK_LIST2) is None, '详情页、列表页互不误认')
    lst = S.tasks_list(TASK_LIST2)
    check(lst and [x['text'] for x in lst['rows']] == ['sleep 304; echo bg-five', 'sleep 303; echo bg-four']
          and [x['cur'] for x in lst['rows']] == [True, False] and lst['stop'] and all(x['st'] == 'running' for x in lst['rows']),
          f'列表页读出两行命令、光标在第一行：{lst}')
    e = S.tasks_list(TASK_EMPTY)
    check(e is not None and e['rows'] == [] and not e['stop'], '没有任务时的列表页：没有行、不能按 x')
    check(S.same_cmd('sleep 300; echo bg-done', 'sleep 300;  echo bg-done') and not S.same_cmd('sleep 300', 'sleep 301'),
          '命令比对：空白不计，内容不同不算')
    check(S.same_cmd('python -m http.server 8…', 'python -m http.server 8080 --bind 127.0.0.1') and not S.same_cmd('py…', 'python x'),
          '命令比对：截断时按前缀比，前缀太短不认')
    check(S.btw_panel(BTW_IDLE, '测试问题一') == (2, 6) and S.btw_panel(BTW_IDLE, '别的问题') is None, '空闲时的旁问面板（跳过输入框那行）')
    check(S.btw_panel(BTW_BUSY, '测试问题二') == (3, 7), '回复中的旁问面板（取最后一个 /btw 标题）')
    check(S.btw_on_screen(BTW_IDLE, '测试问题一', '收到：测试问题一（共 5 字）') and not S.btw_on_screen(BTW_IDLE, '测试问题一', '跑一下测试'),
          '旁问答案在面板里才算')
    check(not S.btw_panel(box('跑一下测试')) and not S.btw_panel(TASK_LIST2), '没有旁问面板时不误认')
    opts = S.screen_options(ADD_DIR)
    check([o['n'] for o in opts] == ['1', '2', '3'] and opts[0]['label'] == 'Yes, for this session' and opts[0]['cur'],
          f'添加文件夹面板的选项：{opts}')
    check(S.option_key(ADD_DIR, 'Yes, for this session') == '1' and S.option_key(ADD_DIR, 'Yes') is None
          and S.option_key(ADD_DIR, 'Yes, and remember this directory') == '2', '选项按文字全等匹配（部分文字不算）')
    check(S.adddir_panel(ADD_DIR, '/tmp/cwb-r15v-adddir') and not S.adddir_panel(ADD_DIR, '/tmp/other'), '添加文件夹面板按路径核对')
    msg = ['❯ /add-dir /tmp/p/inner', '  ⎿  /tmp/p/inner is inside the current working directory /private/tmp/p; loading its skills.', ''] + box()
    check(S.adddir_msg(msg, '/tmp/p/inner', ).startswith('/tmp/p/inner is inside') and S.adddir_msg(box('/add-dir /tmp/p/inner'), '/tmp/p/inner') is None,
          '没弹面板时读出 CLI 的回复行；输入框里还没执行的命令不算')
    old = ['❯ /add-dir /tmp/a', '  ⎿  Added /tmp/a as a working directory for this session', ''] + box()
    check(S.adddir_msg(old, '/tmp/b') is None, '上一次 /add-dir 的回复不当成这一次的')
    po = S.screen_options(PLAN_PANEL)
    check([o['label'] for o in po] == ['Yes, and use auto mode', 'Yes, manually approve edits', 'Tell Claude what to change'],
          f'计划面板选项（续行不算选项）：{[o["label"] for o in po]}')
    check(S.any_panel(TASK_DETAIL) and S.any_panel(BTW_BUSY) and S.any_panel(ADD_DIR) and not S.any_panel(box()), 'any_panel 认出三种面板')
    check(S.input_box_text(box('/btw 你好')) == '/btw 你好' and S.squash(' a b\nc ') == 'abc', '输入框文字与去空白')


def test_press_and_type():
    r = FakeR(TASK_DETAIL)
    check(S.press_if(r, b'x', lambda L: S.tasks_detail(L) == 'sleep 300; echo bg-done') and r.written == [b'x'], '读屏确认后才按键')
    r = FakeR(TASK_DETAIL)
    check(not S.press_if(r, b'x', lambda L: S.tasks_detail(L) == 'sleep 999') and r.written == [], '确认不了不按键')
    r = FakeR(TASK_DETAIL)
    S.PERMS['t-a2'] = {'req': {'sid': r.sid}, 'ev': threading.Event(), 'res': None}
    try:
        check(not S.press_if(r, b'x', lambda L: True) and r.written == [], '有授权在等时不按键')
    finally:
        S.PERMS.pop('t-a2', None)
    r = FakeR(TASK_DETAIL)
    r.state['alive'] = False
    check(not S.press_if(r, b'x', lambda L: True), 'CLI 不在时不按键')
    # Esc 只在面板开着时发
    r = FakeR(box())
    check(S.close_panel(r, S.tasks_open) and r.written == [], '面板没开：不发 Esc')
    r = FakeR(TASK_DETAIL)
    r.on_write = lambda b: setattr(r, 'lines', box()) if b == b'\x1b' else None
    check(S.close_panel(r, S.tasks_open) and r.written == [b'\x1b'], '面板开着：发一次 Esc 并确认关了')
    # type_command：粘贴后输入框里正好是这条命令才按回车
    r = FakeR(box())

    def cli(b):
        if b.startswith(b'\x1b[200~'):
            r.lines = box(b[6:-6].decode())
    r.on_write = cli
    check(S.type_command(r, '/btw 问题 一') and r.written[-1] == b'\r', '输入框核对一致后按回车')
    r = FakeR(box())
    r.on_write = lambda b: setattr(r, 'lines', box('[Pasted text #1 +3 lines]')) if b.startswith(b'\x1b[200~') else None
    t0 = time.time()
    ok = S.type_command(r, '/btw 很长的问题')
    check(not ok and b'\r' not in r.written and 'kill-line' in r.written, f'输入框里不是这条命令：不按回车并清掉（{time.time() - t0:.1f}s）')
    r = FakeR(TASK_DETAIL + box())
    check(not S.type_command(r, '/tasks') and r.written == [], '已有面板开着时不打字')


# ---------------------------------------------------------------- 计划、拒绝并说明
def test_plan_and_deny():
    class R:
        base_mode = 'bypass permissions'
        title, cwd = '', '/tmp'
    req = {'rid': 'p1', 'sid': 's-plan', 'tool': 'ExitPlanMode', 'input': {'plan': '# 计划\n1. 改 a', 'planFilePath': '/x/p.md'}}
    keep = S.plan_fields(req, R())
    check(req['plan'] == '# 计划\n1. 改 a' and req['planPath'] == '/x/p.md' and keep == 'bypassPermissions'
          and [o['key'] for o in req['options']] == ['keep', 'accept', 'ask'] and '跳过确认' in req['options'][0]['label'],
          f'计划请求带全文、路径、选项（沿用跳过确认排最前）：{req["options"]}')
    R.base_mode = 'accept edits'
    req2 = {'rid': 'p2', 'sid': 's', 'tool': 'ExitPlanMode', 'input': {}}
    check(S.plan_fields(req2, R()) is None and [o['key'] for o in req2['options']] == ['accept', 'ask'] and req2['plan'] == '',
          '原模式不是跳过确认或自动时没有“沿用”；没有计划文件时 plan 为空')
    R.base_mode = 'auto mode'
    req3 = {'rid': 'p3', 'sid': 's', 'tool': 'ExitPlanMode', 'input': {}}
    check(S.plan_fields(req3, R()) == 'auto' and '自动模式' in req3['options'][0]['label'], '原模式是自动时“沿用自动模式”')

    def run(ent_req, b, keep=None):
        rid = ent_req['rid']
        S.PERMS[rid] = {'req': ent_req, 'ev': threading.Event(), 'res': None, 'keep': keep}
        try:
            ok = S.decide(rid, b)
            return ok, (S.PERMS[rid]['res'] or {}).get('hookSpecificOutput', {}).get('decision')
        finally:
            S.PERMS.pop(rid, None)
    for key, mode in (('accept', 'acceptEdits'), ('ask', 'default'), ('keep', 'bypassPermissions')):
        ok, d = run(dict(req), {'rid': 'p1', 'plan': key}, keep='bypassPermissions')
        check(ok and d['behavior'] == 'allow' and d['updatedInput'] == req['input']
              and d['updatedPermissions'] == [{'type': 'setMode', 'mode': mode, 'destination': 'session'}],
              f'plan={key} → allow + setMode {mode}（只对本会话）')
    ok, d = run(dict(req), {'rid': 'p1', 'plan': 'feedback', 'message': '先备份 c.txt'})
    check(ok and d == {'behavior': 'deny', 'message': '用户没有批准这个计划，修改意见：先备份 c.txt'}, f'plan=feedback → deny + 意见：{d}')
    for b, why in (({'rid': 'p1', 'plan': 'feedback', 'message': '  '}, '意见为空'), ({'rid': 'p1', 'plan': 'keep'}, '没有沿用项时选 keep'),
                   ({'rid': 'p1', 'plan': 'xx'}, '未知选项')):
        try:
            run(dict(req), b)
            ok = False
        except ValueError:
            ok = True
        check(ok, f'计划决定出错时报错：{why}')
    try:
        run({'rid': 'b1', 'sid': 's', 'tool': 'Bash', 'input': {}}, {'rid': 'b1', 'plan': 'accept'})
        ok = False
    except ValueError:
        ok = True
    check(ok, '不是计划确认时不接受 plan')
    ok, d = run({'rid': 'b1', 'sid': 's', 'tool': 'Bash', 'input': {'command': 'rm x'}}, {'rid': 'b1', 'allow': False, 'message': '这个文件还要用'})
    check(ok and d == {'behavior': 'deny', 'message': '用户拒绝了这次操作，说明：这个文件还要用'}, f'拒绝并说明：{d}')
    ok, d = run({'rid': 'b2', 'sid': 's', 'tool': 'Bash', 'input': {}}, {'rid': 'b2', 'allow': False})
    check(ok and d['behavior'] == 'deny' and d['message'], '拒绝不带说明：沿用默认说明')
    ok, d = run({'rid': 'b3', 'sid': 's', 'tool': 'Bash', 'input': {}}, {'rid': 'b3', 'decision': 'allow'})
    check(ok and d == {'behavior': 'allow'}, '原有 decision=allow 不受影响')
    ok, d = run({'rid': 'b4', 'sid': 's', 'tool': 'ExitPlanMode', 'input': {'plan': 'x'}}, {'rid': 'b4', 'decision': 'deny', 'message': 'm'})
    check(ok and d == {'behavior': 'deny', 'message': 'm'}, '原有 decision=deny 不受影响')

    class M:
        base_mode = None
    m = M()
    S.track_mode(m, {'ready': True, 'mode': 'bypass permissions'})
    check(m.base_mode is None, '模式刚出现不算原模式')
    m.mode_seen = ('bypass permissions', time.time() - 2)
    S.track_mode(m, {'ready': True, 'mode': 'bypass permissions'})
    check(m.base_mode == 'bypass permissions', '模式稳定 1.5 秒以上记为原模式')
    for md in ('plan mode', 'default'):
        m.mode_seen = (md, time.time() - 5)
        S.track_mode(m, {'ready': True, 'mode': md})
        check(m.base_mode == 'bypass permissions', f'{md} 不覆盖原模式')


# ---------------------------------------------------------------- 旁问判断、钩子
def test_btw_judge_and_hooks():
    def bt(**k):
        return dict({'id': 'b1', 'q': '测试问题一', 't': time.time(), 'ms': 0, 'stops': 0, 'ev': threading.Event()}, **k)
    r = FakeR(BTW_IDLE)
    b = bt()
    r.btw, r.stops, r.stop_at = b, 3, time.time() - 5  # 刚结束一轮：只认面板里显示的
    S.btw_judge(r, b, '跑一下测试')
    check(not b['ev'].is_set(), '刚结束一轮后的输入建议（面板里没有）不认')
    S.btw_judge(r, b, '收到：测试问题一（共 5 字）')
    check(b['ev'].is_set() and b['a'] == '收到：测试问题一（共 5 字）', '面板里显示着的答案认作旁问答案')
    r = FakeR(box())
    b = bt(stops=2, t=time.time())
    r.btw, r.stops, r.stop_at = b, 2, time.time() - 60
    S.btw_judge(r, b, '读屏没对上的答案')
    check(b['ev'].is_set(), '发出后没有过 Stop、离上次 Stop 超过 20 秒：按时序认')
    r = FakeR(box())
    b = bt(stops=2)
    r.btw, r.stops, r.stop_at = b, 3, time.time()
    S.btw_judge(r, b, '跑一下测试')
    check(not b['ev'].is_set(), '发出后有过 Stop、面板里也没有：不认（输入建议）')
    # on_subagent：没有旁问在等、或是真子代理时直接忽略
    r = FakeR(BTW_IDLE)
    S.on_subagent(r, r.sid, {'agent_type': '', 'last_assistant_message': 'x'})
    check(getattr(r, 'btw', None) is None, '没有旁问在等：忽略')
    b = bt()
    r.btw = b
    S.on_subagent(r, r.sid, {'agent_type': 'general-purpose', 'last_assistant_message': '收到：测试问题一（共 5 字）'})
    time.sleep(0.3)
    check(not b['ev'].is_set(), '有 agent_type 的子代理：忽略')
    # 压缩、添加文件夹钩子
    got = []
    emit = S.BUS.emit
    S.BUS.emit = got.append
    import tempfile  # 修复线：压缩摘要落盘到临时目录，不写副本的 .run
    old_cd, S.COMPACT_DIR = S.COMPACT_DIR, tempfile.mkdtemp(prefix='cwb-fix-cmp-')
    try:
        S.handle_hook('compact-start', 'no-runner', {'trigger': 'manual', 'custom_instructions': '侧重改动'})
        S.handle_hook('compact-end', 'no-runner', {'trigger': 'manual', 'compact_summary': '摘要全文'})
        S.handle_hook('subagent', 'no-runner', {'agent_type': ''})
        S.handle_hook('dir', 'no-runner', {'directory': '/tmp/x'})
        saved = S.compact_load('no-runner')
        check(len(saved) == 1 and saved[0]['summary'] == '摘要全文' and saved[0]['trigger'] == 'manual' and saved[0]['t'] > 1e12,
              f'修复线：压缩摘要落盘（.run/compact/<sid>.json）：{saved}')
    finally:
        S.BUS.emit = emit
        import shutil
        shutil.rmtree(S.COMPACT_DIR, ignore_errors=True)
        S.COMPACT_DIR = old_cd
    check(got == [{'type': 'compact', 'sid': 'no-runner', 'phase': 'start', 'trigger': 'manual', 'summary': '', 'instructions': '侧重改动'},
                  {'type': 'compact', 'sid': 'no-runner', 'phase': 'end', 'trigger': 'manual', 'summary': '摘要全文'}],
          f'压缩钩子推 compact 事件；没有对应对话时别的钩子安静忽略：{got}')
    bad = S.OBSERVE_HOOKS['dir']
    S.OBSERVE_HOOKS['dir'] = lambda *a: 1 / 0
    try:
        out = S.handle_hook('dir', 'no-runner', {'directory': '/tmp/x'})
    finally:
        S.OBSERVE_HOOKS['dir'] = bad
    check(out == {}, '观测钩子处理出错：只记日志，返回空结果')
    r = FakeR(box())
    r.dir_ev = threading.Event()
    S.on_dir(r, r.sid, {'directory': '/tmp/cwb-a2-x/', 'source': 'slash_command'})
    S.on_dir(r, r.sid, {'directory': '/tmp/cwb-a2-x', 'source': 'slash_command'})
    check(r.dirs == ['/tmp/cwb-a2-x'] and r.dir_ev.is_set() and r.analyzed >= 1, f'DirectoryAdded：记进 dirs（去重）并通知等待方：{r.dirs}')
    st = S.build_settings()
    hk = st['hooks']
    check(all(hk[e][0]['hooks'][0].get('async') is True for e in ('SubagentStop', 'DirectoryAdded', 'PreCompact', 'PostCompact')),
          '新钩子都是 async（后台跑，不拖 CLI）')
    check(all(e in hk and not hk[e][0]['hooks'][0].get('async') for e in ('PermissionRequest', 'UserPromptSubmit', 'Stop', 'StopFailure', 'Notification', 'SessionStart')),
          '原有钩子不变')
    check(not any(e in hk for e in ('PreToolUse', 'PostToolUse', 'MessageDisplay')), '没有加工具进度和流式正文钩子')


def test_conv_uuid_and_hook_lines():
    rows = [
        {'type': 'user', 'uuid': 'u-1', 'timestamp': '2026-11-13T12:00:00Z', 'message': {'role': 'user', 'content': '第一问'}},
        {'type': 'assistant', 'uuid': 'a-1', 'timestamp': '2026-11-13T12:00:01Z', 'message': {
            'role': 'assistant', 'model': 'm', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '答'}]}},
        {'type': 'user', 'uuid': 'u-c', 'timestamp': '2026-11-13T12:00:02Z', 'message': {
            'role': 'user', 'content': '<command-name>/compact</command-name>\n<command-args>侧重</command-args>'}},
        {'type': 'user', 'uuid': 'u-o', 'timestamp': '2026-11-13T12:00:03Z', 'message': {'role': 'user', 'content':
            '<local-command-stdout>\u001b[2mCompacted (ctrl+o to see full summary)\u001b[22m\n'
            '\u001b[2mPreCompact [/Users/x/.venv/bin/python -I /Users/x/hook.py compact-start] completed successfully\u001b[22m\n'
            '\u001b[2mPostCompact [/Users/x/.venv/bin/python -I /Users/x/hook.py compact-end] completed successfully\u001b[22m</local-command-stdout>'}},
        {'type': 'user', 'uuid': 'u-h', 'timestamp': '2026-11-13T12:00:04Z', 'message': {'role': 'user', 'content':
            '<local-command-stdout>SessionStart [/x/hook.py session] completed successfully</local-command-stdout>'}},
        {'type': 'user', 'uuid': 'u-2', 'timestamp': '2026-11-13T12:00:05Z', 'message': {'role': 'user', 'content': '第二问'}},
        {'type': 'attachment', 'timestamp': '2026-11-13T12:00:06Z', 'attachment': {'type': 'queued_command', 'prompt': '插一句'}},
    ]
    c, out = S.Conv(), {'new': [], 'patch': []}
    for r in rows:
        c.feed(r, out)
    users = [(i.get('uuid'), i['text'], bool(i.get('mid'))) for i in c.items if i['k'] == 'user']
    check(users == [('u-1', '第一问', False), ('u-2', '第二问', False), (None, '插一句', True)], f'每轮开头的用户条目带 uuid，插话（mid）不带：{users}')
    notes = [i['text'] for i in c.items if i['k'] == 'note']
    check(notes == ['Compacted (ctrl+o to see full summary)'], f'钩子成功提示行不进时间线：{notes}')
    ats = [i.get('at') for i in c.items if i['k'] == 'note']
    check(ats == [1794571203000], f'修复线：“Compacted”那一行带压缩完成时间 at（毫秒），界面据此对上落盘的摘要：{ats}')
    toc, _ = S.hidden_index(c.items)
    check([t.get('uuid') for t in toc] == ['u-1', 'u-2', ''], '目录条目也带 uuid')
    cmds = [(i['text'], i.get('uuid')) for i in c.items if i['k'] == 'cmd']
    check(cmds == [('/compact 侧重', 'u-c')], f'命令条目带那条命令记录的 uuid：{cmds}')
    c2, out2 = S.Conv(), {'new': [], 'patch': []}
    c2.feed({'type': 'user', 'uuid': 'u-s', 'promptSource': 'system', 'origin': {'kind': 'task-notification'},
             'timestamp': '2026-11-13T12:00:07Z', 'message': {'role': 'user', 'content':
                 '<task-notification><status>completed</status><summary>Task done</summary></task-notification>'}}, out2)
    sy = [(i['k'], i.get('turn'), i.get('uuid')) for i in c2.items]
    check(sy == [('sys', True, 'u-s')], f'开启一轮的系统通知条目带 uuid：{sy}')


def test_hook_observe_file():
    """hook.py：只观测的事件（subagent 等）不覆盖“最后一次事件”文件；服务不在时很快退出、退出码 0、不输出。"""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    gen = f'a2test-{os.getpid()}'
    path = os.path.join(S.ROOT, '.run', 'hooks', gen + '.json')
    env = dict(os.environ, CWB_PORT=str(port), CWB_TOKEN='x', CWB_SID='s-a2', CWB_GEN=gen)
    try:
        t0 = time.time()
        p = subprocess.run([sys.executable, '-I', S.HOOK, 'subagent'], input=json.dumps({'agent_type': ''}), env=env,
                           capture_output=True, text=True, timeout=20)
        dt = time.time() - t0
        check(p.returncode == 0 and not p.stdout and not os.path.exists(path) and dt < 3,
              f'subagent：不写事件文件、{dt:.1f}s 内退出、退出码 0、无输出')
        p = subprocess.run([sys.executable, '-I', S.HOOK, 'idle'], input='{}', env=env, capture_output=True, text=True, timeout=20)
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
        check(p.returncode == 0 and rec.get('ev') == 'idle', '对照：idle 照常写事件文件')
        subprocess.run([sys.executable, '-I', S.HOOK, 'compact-end'], input='{}', env=env, capture_output=True, text=True, timeout=20)
        with open(path, encoding='utf-8') as f:
            check(json.load(f).get('ev') == 'idle', 'compact-end 之后事件文件仍是 idle')
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_btw_store():
    sid = f'a2-test-{os.getpid()}'
    try:
        S.btw_save(sid, {'id': 'x1', 'q': '问', 'a': '答', 't': 1})
        S.btw_save(sid, {'id': 'x2', 'q': '问二', 'a': '', 't': 2, 'error': '没有拿到回答'})
        S.btw_save(sid, {'id': 'x1', 'q': '问', 'a': '答（更新）', 't': 1})
        items = S.api_btw_list({'sid': sid})['items']
        check([(i['id'], i['a']) for i in items] == [('x2', ''), ('x1', '答（更新）')], f'旁问记录落盘、按 id 覆盖：{items}')
        check(S.api_btw_list({'sid': 'no-such'}) == {'items': []}, '没有记录时返回空')
        for q, why in (('', '问题为空'), ('问' * (S.BTW_MAX + 1), '太长')):
            try:
                S.api_btw({'sid': 'no-such', 'q': q})
                ok = False
            except ValueError:
                ok = True
            check(ok, f'旁问参数检查：{why}')
        try:
            S.api_btw({'sid': 'no-such', 'q': '问'})
            ok = False
        except ValueError as e:
            ok = '没有在运行' in str(e)
        check(ok, '对话没在运行时旁问报错')
    finally:
        try:
            os.remove(S.btw_path(sid))
        except OSError:
            pass


def test_panel_guard():
    r = FakeR(box())
    S.panel_begin(r, 'btw')
    try:
        S.panel_begin(r, 'tasks')
        ok = False
    except ValueError as e:
        ok = '上一个操作' in str(e)
    check(ok and r.panel_op == 'btw', '同一对话同时只做一个代按键操作')
    S.panel_end(r)
    check(r.panel_op is None and r.analyzed == 1, '结束后放开，并重新分析屏幕（排队的消息此时发出）')
    for st, why in (({'ready': False}, '输入框不可见'), ({'dialog': {'x': 1}}, '有弹窗')):
        r = FakeR(box())
        r.state.update(st)
        try:
            S.panel_begin(r, 'btw')
            ok = False
        except ValueError:
            ok = True
        check(ok and r.panel_op is None, f'{why}时不开始')
    r = FakeR(box())
    r.queue = [('x', [], False)]
    try:
        S.panel_begin(r, 'btw')
        ok = False
    except ValueError:
        ok = True
    check(ok, '有没发出的消息时不开始')


if __name__ == '__main__':
    for fn in (test_commands, test_screen_helpers, test_press_and_type, test_plan_and_deny, test_btw_judge_and_hooks,
               test_conv_uuid_and_hook_lines, test_hook_observe_file, test_btw_store, test_panel_guard):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(False, f'{fn.__name__} 异常：{e!r}')
    print('全部通过' if not FAIL else f'{len(FAIL)} 项失败：' + '；'.join(FAIL))
    sys.exit(1 if FAIL else 0)
