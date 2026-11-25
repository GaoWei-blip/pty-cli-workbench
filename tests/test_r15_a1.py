"""第十五轮 A1 单元测试：检查点推算、回退与撤销、状态行脚本与注入、ctx 解析、记忆文件、保存文件、标记未读。
不启动 claude、不碰 ~/.claude：会话记录、快照目录、项目文件夹、设置文件都造在临时目录里。
运行：.venv/bin/python -I tests/test_r15_a1.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid as U
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import server as S  # noqa: E402
import checkpoints as CK  # noqa: E402
import ctxstat as CX  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % int((t % 1) * 1000)


class Fx:
    """造一份会话记录 + 快照目录 + 项目文件夹。"""

    def __init__(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='cwb-r15a1-'))
        self.proj = os.path.join(self.tmp, 'proj')
        os.makedirs(self.proj)
        self.sid = str(U.uuid4())
        CK.FH_ROOT = os.path.join(self.tmp, 'fh')
        CK.REWIND_DIR = os.path.join(self.tmp, 'rewind')
        CK.HIST.clear()
        CK.STATS.clear()
        self.fh = os.path.join(CK.FH_ROOT, self.sid)
        os.makedirs(self.fh)
        self.jsonl = os.path.join(self.tmp, self.sid + '.jsonl')
        self.rows, self.last, self.ver = [], None, {}
        self.T0 = time.time() - 3600

    def p(self, name):
        return os.path.join(self.proj, name)

    def disk(self, name, text, t=None):
        p = self.p(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
        if t:
            os.utime(p, (t, t))

    def read(self, name):
        try:
            return open(self.p(name), encoding='utf-8').read()
        except OSError:
            return None

    def bk(self, name, content, t):
        """备份：content 为 None 表示当时不存在。"""
        p = self.p(name)
        if content is None:
            return {'backupFileName': None, 'version': 1, 'backupTime': iso(t), 'realParentDir': os.path.dirname(p)}
        v = self.ver.get(name, 0) + 1
        self.ver[name] = v
        fn = hashlib.sha256(p.encode()).hexdigest()[:16] + f'@v{v}'
        with open(os.path.join(self.fh, fn), 'w', encoding='utf-8') as f:
            f.write(content)
        return {'backupFileName': fn, 'version': v, 'backupTime': iso(t), 'realParentDir': os.path.dirname(p)}

    def row(self, r, chain=True, parent=None):
        if chain:
            r.setdefault('uuid', str(U.uuid4()))
            r['parentUuid'] = parent if parent is not None else self.last
            r.setdefault('isSidechain', False)
            r.setdefault('cwd', self.proj)
            r.setdefault('sessionId', self.sid)
            self.last = r['uuid']
        self.rows.append(r)
        return r.get('uuid')

    def prompt(self, text, t, parent=None):
        return self.row({'type': 'user', 'message': {'role': 'user', 'content': text}, 'timestamp': iso(t)}, parent=parent)

    def snap(self, mid, files, t):
        self.row({'type': 'file-history-snapshot', 'messageId': mid, 'isSnapshotUpdate': False,
                  'snapshot': {'messageId': mid, 'trackedFileBackups': {self.p(k): v for k, v in files.items()}, 'timestamp': iso(t)}},
                 chain=False)

    def delta(self, mid, name, b, t):
        self.row({'type': 'file-history-delta', 'messageId': str(U.uuid4()), 'snapshotMessageId': mid, 'trackingPath': self.p(name),
                  'backup': b, 'timestamp': iso(t)}, chain=False)

    def tool(self, name, path, t):
        a = self.row({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't' + str(t), 'name': name,
                                                                                         'input': {'file_path': path}}], 'stop_reason': 'tool_use'},
                      'timestamp': iso(t)})
        self.row({'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't' + str(t), 'content': 'ok'}]},
                  'toolUseResult': {'filePath': path}, 'timestamp': iso(t)})
        return a

    def end(self, t):
        self.row({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '好了'}], 'stop_reason': 'end_turn'},
                  'timestamp': iso(t)})
        return self.row({'type': 'system', 'subtype': 'turn_duration', 'durationMs': 1000, 'timestamp': iso(t)})

    def flush(self, extra=()):
        with open(self.jsonl, 'w', encoding='utf-8') as f:
            for r in self.rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
            f.write('{"type":"api-request","id":"x","params":{}}\n')

    def append(self, rows):
        with open(self.jsonl, 'a', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')

    def done(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def build():
    """R1 新建 a；R2 改 b；（/export 命令，有快照）；废弃分支改 e；R3 新建 f、Bash 追加 b 和 d；R4 改 c。"""
    x = Fx()
    T = x.T0
    x.disk('b.txt', 'B原始\n第二行\n', T - 10)
    x.disk('c.txt', 'C原始\n', T - 10)
    x.disk('d.txt', 'D原始\n', T - 10)
    # R1
    u1 = x.prompt('R1 新建 a', T)
    x.snap(u1, {}, T)
    x.delta(u1, 'a.txt', x.bk('a.txt', None, T + 1), T + 1)
    x.tool('Write', x.p('a.txt'), T + 2)
    x.disk('a.txt', 'A1\n', T + 2)
    x.end(T + 3)
    # R2
    u2 = x.prompt('R2 改 b', T + 60)
    a2 = x.bk('a.txt', 'A1\n', T + 60)
    x.snap(u2, {'a.txt': a2}, T + 60)
    b1 = x.bk('b.txt', 'B原始\n第二行\n', T + 61)
    x.delta(u2, 'b.txt', b1, T + 61)
    x.tool('Edit', x.p('b.txt'), T + 62)
    x.disk('b.txt', 'B原始\n第二行改\n', T + 62)
    x.end(T + 63)
    # /export 命令：拍了快照，不算一轮
    uc = x.prompt('<command-name>/export</command-name>\n<command-args>/tmp/x.txt</command-args>', T + 90)
    b2 = x.bk('b.txt', 'B原始\n第二行改\n', T + 90)
    x.snap(uc, {'a.txt': a2, 'b.txt': b2}, T + 90)
    x.row({'type': 'user', 'isMeta': False, 'message': {'role': 'user', 'content': '<local-command-stdout>Conversation exported</local-command-stdout>'},
           'timestamp': iso(T + 90)})
    fork_point = x.last
    # 废弃分支：CLI 回退对话后留下的
    uab = x.prompt('废弃分支 改 e', T + 100)
    x.snap(uab, {'a.txt': a2, 'b.txt': b2}, T + 100)
    x.delta(uab, 'e.txt', x.bk('e.txt', None, T + 101), T + 101)
    x.tool('Write', x.p('e.txt'), T + 102)
    x.end(T + 103)
    # R3 接在分叉点上
    u3 = x.prompt('R3 新建 f 加 Bash', T + 120, parent=fork_point)
    x.snap(u3, {'a.txt': a2, 'b.txt': b2}, T + 120)
    x.delta(u3, 'f.txt', x.bk('f.txt', None, T + 121), T + 121)
    x.tool('Write', x.p('f.txt'), T + 122)
    x.disk('f.txt', 'F1\n', T + 122)
    x.disk('b.txt', 'B原始\n第二行改\nB追加\n', T + 123)
    x.disk('d.txt', 'D原始\nD追加\n', T + 123)
    x.end(T + 124)
    # R4
    u4 = x.prompt('R4 改 c', T + 180)
    b3 = x.bk('b.txt', 'B原始\n第二行改\nB追加\n', T + 180)
    f2 = x.bk('f.txt', 'F1\n', T + 180)
    x.snap(u4, {'a.txt': a2, 'b.txt': b3, 'f.txt': f2}, T + 180)
    c1 = x.bk('c.txt', 'C原始\n', T + 181)
    x.delta(u4, 'c.txt', c1, T + 181)
    x.tool('Edit', x.p('c.txt'), T + 182)
    x.disk('c.txt', 'C改\n', T + 182)
    x.end(T + 183)
    x.flush()
    x.u = {'u1': u1, 'u2': u2, 'uc': uc, 'uab': uab, 'u3': u3, 'u4': u4}
    x.bks = {'a2': a2, 'b3': b3, 'f2': f2, 'c1': c1}
    return x


def files_of(t):
    return {f['rel']: (f['st'], f['add'], f['del']) for f in t['files']}


def test_turns():
    x = build()
    try:
        r = CK.turn_changes(x.sid, x.jsonl)
        by = {t['uuid']: t for t in r['turns']}
        u = x.u
        check([t['uuid'] for t in r['turns']] == [u['u1'], u['u2'], u['u3'], u['u4']], '只列有改动的真正提问，顺序对；命令和废弃分支不算轮')
        check(files_of(by[u['u1']]) == {'a.txt': ('new', 1, 0)}, 'R1：新建 a.txt +1')
        check(files_of(by[u['u2']]) == {'b.txt': ('mod', 1, 1)}, 'R2：改 b.txt +1 −1（命令的快照没把改动分走）')
        check(files_of(by[u['u3']]) == {'b.txt': ('mod', 1, 0), 'f.txt': ('new', 1, 0)}, 'R3：新建 f.txt，Bash 追加到已跟踪的 b.txt 也算；d.txt 不在')
        check(files_of(by[u['u4']]) == {'c.txt': ('mod', 1, 1)}, 'R4（最后一轮）和磁盘比：改 c.txt')
        check(by[u['u2']]['n'] == 1 and by[u['u3']]['n'] == 2 and by[u['u3']]['add'] == 2, '每轮的 n / add / del 汇总')
        check(not any(t['expired'] for t in r['turns']), '快照都在：expired 为假')
        check(set(files_of(r['all'])) == {'a.txt', 'b.txt', 'c.txt', 'f.txt'} and r['all']['n'] == 4, '整个对话累计：a b c f，没有废弃分支的 e.txt、Bash 的 d.txt')
        check(files_of(r['all'])['b.txt'] == ('mod', 2, 1), '累计 b.txt：最早快照对比磁盘 +2 −1')
        check(r['bashNote'] is True and r['rewinds'] == [], 'bashNote、rewinds 字段')
        # 差异：格式与 git-diff 一致
        d = CK.turn_diff(x.sid, u['u2'], x.p('b.txt'), x.jsonl)
        check('@@ -1,2 +1,2 @@' in d['diff'] and '-第二行' in d['diff'] and '+第二行改' in d['diff'] and d['binary'] is False, f'turn-diff：统一差异格式 {d["diff"][-60:]!r}')
        d = CK.turn_diff(x.sid, u['u1'], x.p('a.txt'), x.jsonl)
        check(d['new'] is True and '@@ -0,0 +1 @@' in d['diff'] and '+A1' in d['diff'], 'turn-diff：新建文件从空开始')
        d = CK.turn_diff(x.sid, '*', x.p('b.txt'), x.jsonl)
        check('+B追加' in d['diff'] and '-第二行' in d['diff'], "turn-diff uuid='*'：整个对话累计")
        d = CK.turn_diff(x.sid, u['u2'], x.p('b.txt'), x.jsonl, ctx=100000)
        check(d['diff'].count('\n ') >= 1, 'turn-diff 可要全文上下文')
        try:
            CK.turn_diff(x.sid, u['u2'], x.p('d.txt'), x.jsonl)
            check(False, 'turn-diff：不在改动里的文件报错')
        except ValueError:
            check(True, 'turn-diff：不在改动里的文件报错')
    finally:
        x.done()


def test_incremental():
    x = build()
    try:
        CK.turn_changes(x.sid, x.jsonl)
        h = CK.HIST[x.jsonl]
        off1 = h.off
        # 压缩边界（parentUuid 为空，接 logicalParentUuid）后再来一轮 R5 改 a
        T = x.T0 + 300
        x.rows = []
        cb = x.row({'type': 'system', 'subtype': 'compact_boundary', 'logicalParentUuid': x.last, 'timestamp': iso(T)}, parent='')
        x.rows[-1]['parentUuid'] = None
        x.row({'type': 'user', 'isCompactSummary': True, 'message': {'role': 'user', 'content': 'This session is being continued'}, 'timestamp': iso(T)})
        u5 = x.prompt('R5 改 a', T + 10)
        c2 = x.bk('c.txt', 'C改\n', T + 10)
        x.snap(u5, {'a.txt': x.bks['a2'], 'b.txt': x.bks['b3'], 'f.txt': x.bks['f2'], 'c.txt': c2}, T + 10)
        x.tool('Edit', x.p('a.txt'), T + 11)
        x.disk('a.txt', 'A2\n', T + 11)
        x.end(T + 12)
        x.append(x.rows)
        r = CK.turn_changes(x.sid, x.jsonl)
        check(CK.HIST[x.jsonl] is h and h.off > off1, f'增量读取：同一个解析对象，从偏移 {off1} 接着读到 {h.off}')
        ids = [t['uuid'] for t in r['turns']]
        check(len(ids) == 5 and ids[-1] == u5 and ids[0] == x.u['u1'], '压缩后沿 logicalParentUuid 接上压缩前的轮')
        by = {t['uuid']: t for t in r['turns']}
        check(files_of(by[x.u['u4']]) == {'c.txt': ('mod', 1, 1)} and files_of(by[u5]) == {'a.txt': ('mod', 1, 1)},
              'R4 不再是最后一轮后改用下一轮快照比；R5 和磁盘比')
        # 技能类斜杠命令：模型在其后回复并改了文件，单独算一轮（kind=cmd），不记到 R5 头上
        x.rows = []
        u6 = x.prompt('<command-message>simplify is running…</command-message>\n<command-name>/simplify</command-name>', T + 60)
        a3 = x.bk('a.txt', 'A2\n', T + 60)
        x.snap(u6, {'a.txt': a3, 'b.txt': x.bks['b3'], 'f.txt': x.bks['f2'], 'c.txt': c2}, T + 60)
        x.row({'type': 'user', 'isMeta': True, 'message': {'role': 'user', 'content': '技能正文……'}, 'timestamp': iso(T + 60)})
        x.tool('Edit', x.p('b.txt'), T + 61)
        x.disk('b.txt', 'B技能改\n', T + 61)
        x.end(T + 62)
        # 本地命令：拍了快照但模型没有回复，不算轮
        u7 = x.prompt('<command-name>/export</command-name>', T + 90)
        x.snap(u7, {'a.txt': a3, 'b.txt': x.bk('b.txt', 'B技能改\n', T + 90), 'f.txt': x.bks['f2'], 'c.txt': c2}, T + 90)
        x.append(x.rows)
        r = CK.turn_changes(x.sid, x.jsonl)
        by = {t['uuid']: t for t in r['turns']}
        check([t['uuid'] for t in r['turns']][-2:] == [u5, u6] and by[u6]['kind'] == 'cmd' and by[u5]['kind'] == 'prompt',
              '技能命令（模型回复过）单独算一轮，kind=cmd；之后的本地命令不算轮')
        check(files_of(by[u6]) == {'b.txt': ('mod', 1, 3)} and files_of(by[u5]) == {'a.txt': ('mod', 1, 1)}, '技能命令的改动记在它自己名下')
        check(u7 not in by, '本地命令（/export）不出现在轮次里')
        del cb
    finally:
        x.done()


def test_after_and_preview():
    x = build()
    try:
        u = x.u
        S.RUNNERS.pop(x.sid, None)
        pv = CK.preview(x.sid, u['u2'], False, x.jsonl)
        w = {f['rel'] for f in pv['write']}
        rm = {f['rel'] for f in pv['remove']}
        check(w == {'b.txt', 'c.txt'} and rm == {'f.txt'}, f'回到 R2 之前：写回 b、c，删除 R3 新建的 f（{w} {rm}）')
        check(pv['conflicts'] == [] and pv['missing'] == [] and pv['busy'] is False, '没有冲突、没有缺失、不忙')
        pv = CK.preview(x.sid, u['u2'], True, x.jsonl)
        w = {f['rel']: (f['add'], f['del']) for f in pv['write']}
        check(set(w) == {'b.txt', 'c.txt'} and {f['rel'] for f in pv['remove']} == {'f.txt'} and w['b.txt'] == (0, 1),
              f'after：回到 R2 结束时（R3 之前），b 去掉 Bash 追加的一行 {w}')
        pv = CK.preview(x.sid, u['u4'], True, x.jsonl)
        check(pv['write'] == [] and pv['remove'] == [], 'after 最后一轮＝现状：没有要做的')
        pv = CK.preview(x.sid, u['u4'], False, x.jsonl)
        check([f['rel'] for f in pv['write']] == ['c.txt'] and pv['remove'] == [], '回到 R4 之前：只写回 c')
        # 冲突：在最后一次快照之后被别的程序改过
        now = time.time()
        os.utime(x.p('b.txt'), (now, now))
        pv = CK.preview(x.sid, u['u2'], False, x.jsonl)
        check([f['rel'] for f in pv['conflicts']] == ['b.txt'], '冲突：b.txt 在最后一次快照之后被改过')
        try:
            CK.preview(x.sid, u['uc'], False, x.jsonl)
            check(False, '命令记录不能当回退点')
        except ValueError:
            check(True, '命令记录不能当回退点')
        try:
            CK.preview(x.sid, u['uab'], False, x.jsonl)
            check(False, '废弃分支上的提问不能回退')
        except ValueError:
            check(True, '废弃分支上的提问不能回退')
    finally:
        x.done()


def test_missing():
    x = build()
    try:
        os.remove(os.path.join(x.fh, x.bks['c1']['backupFileName']))
        r = CK.turn_changes(x.sid, x.jsonl)
        by = {t['uuid']: t for t in r['turns']}
        check(by.get(x.u['u4']) is None or by[x.u['u4']]['expired'], 'c.txt 的快照没了：R4 标 expired（或不再列出）')
        check(r['all']['expired'] is True, '累计也标 expired')
        pv = CK.preview(x.sid, x.u['u2'], False, x.jsonl)
        check([f['rel'] for f in pv['missing']] == ['c.txt'] and 'c.txt' not in {f['rel'] for f in pv['write']}, '预览：c.txt 列入 missing，不写')
        res = CK.apply(x.sid, x.u['u2'], False, x.jsonl)
        check(res['ok'] and [m['rel'] for m in res['missing']] == ['c.txt'] and x.read('c.txt') == 'C改\n', '执行：缺失的跳过并在结果里说明，c.txt 原样')
        check(x.read('b.txt') == 'B原始\n第二行\n' and x.read('f.txt') is None, '其余照常回退')
    finally:
        x.done()


class FakeRunner:
    def __init__(self, sid, busy):
        self.sid, self.state, self.turn, self.queue, self.sending = sid, {'alive': True, 'busy': busy}, busy, [], False

    def attention(self):
        return False


def test_apply_undo():
    x = build()
    try:
        u = x.u
        S.RUNNERS[x.sid] = FakeRunner(x.sid, True)
        try:
            CK.apply(x.sid, u['u2'], False, x.jsonl)
            check(False, '对话正在回复时拒绝回退')
        except ValueError as e:
            check('回复' in str(e), '对话正在回复时拒绝回退：' + str(e))
        check(CK.preview(x.sid, u['u2'], False, x.jsonl)['busy'] is True, '预览带 busy')
        S.RUNNERS.pop(x.sid, None)
        before = {n: x.read(n) for n in ('a.txt', 'b.txt', 'c.txt', 'd.txt', 'f.txt')}
        res = CK.apply(x.sid, u['u2'], False, x.jsonl)
        check(res['ok'] and res['written'] == 2 and res['removed'] == 1, f'回退到 R2 之前：写回 2 个、删除 1 个 {res}')
        check(x.read('b.txt') == 'B原始\n第二行\n' and x.read('c.txt') == 'C原始\n' and x.read('f.txt') is None and x.read('a.txt') == 'A1\n',
              '磁盘：b、c 回到原样，f 删除，a 保留（R2 之前就有）')
        check(x.read('d.txt') == 'D原始\nD追加\n', '只被 Bash 改的 d.txt 不动')
        saved = os.listdir(os.path.join(CK.REWIND_DIR, res['id']))
        check('meta.json' in saved and len(saved) == 4, f'回退前的现状存到 rewind/<id>/（{sorted(saved)}）')
        r = CK.turn_changes(x.sid, x.jsonl)
        check(len(r['rewinds']) == 1 and r['rewinds'][0]['id'] == res['id'] and r['rewinds'][0]['undone'] is False
              and r['rewinds'][0]['uuid'] == u['u2'], 'turn-changes 带回退记录')
        by = {t['uuid']: t for t in r['turns']}
        check(files_of(by[u['u4']]) == {'c.txt': ('mod', 1, 1)}, '回退之后最后一轮仍显示它自己的改动（取回退前存下的内容），不被算成反向改动')
        check(files_of(by[u['u2']]) == {'b.txt': ('mod', 1, 1)}, '前面的轮不受影响')
        pv = CK.preview(x.sid, u['u2'], False, x.jsonl)
        check(pv['write'] == [] and pv['remove'] == [] and pv['conflicts'] == [], '再预览同一点：已经是目标状态')
        r0 = CK.apply(x.sid, u['u2'], False, x.jsonl)
        check(r0['ok'] and r0['id'] is None and r0['written'] == 0 and len(CK.load_rewinds(x.sid)) == 1, '没有要做的：不留回退记录（id 为 null）')
        e = CK.turn_changes(x.sid, os.path.join(x.tmp, 'none.jsonl'))
        check(e['turns'] == [] and e['all']['n'] == 0, '还没有记录的新对话：空结果')
        un = CK.undo(res['id'])
        after = {n: x.read(n) for n in before}
        check(un['ok'] and un['restored'] == 3 and after == before, f'撤销：三个文件回到回退前 {un}')
        check(CK.turn_changes(x.sid, x.jsonl)['rewinds'][0]['undone'], '回退记录标为已撤销')
        try:
            CK.undo(res['id'])
            check(False, '同一次回退不能撤销两次')
        except ValueError:
            check(True, '同一次回退不能撤销两次')
        # 撤销冲突：回退之后又被改过的文件不盖掉
        res = CK.apply(x.sid, u['u2'], True, x.jsonl)
        x.disk('b.txt', '别人又改了\n')
        un = CK.undo(res['id'])
        check(un['skipped'] == ['b.txt'] and x.read('b.txt') == '别人又改了\n' and x.read('f.txt') == 'F1\n' and x.read('c.txt') == 'C改\n',
              f'撤销时回退后又被改过的 b.txt 跳过，其余恢复 {un}')
        try:
            CK.undo('../../etc')
            check(False, '撤销 id 校验')
        except ValueError:
            check(True, '撤销 id 校验')
    finally:
        S.RUNNERS.pop(x.sid, None)
        x.done()


def test_paths():
    b = {'backupFileName': None, 'realParentDir': '/private/tmp/p/sub'}
    check(CK.target_path('/tmp/p/sub/s.txt', b) == '/private/tmp/p/sub/s.txt', '绝对键：真实路径＝realParentDir/文件名')
    check(CK.target_path('sub/s.txt', b) == '/private/tmp/p/sub/s.txt', '相对键、realParentDir 是文件所在目录')
    check(CK.target_path('sub/x.txt', {'realParentDir': '/private/tmp/p'}) == '/private/tmp/p/sub/x.txt', '相对键、realParentDir 是工作目录（旧格式）')
    check(CK.target_path('a.txt', {'realParentDir': '/private/tmp/p'}) == '/private/tmp/p/a.txt', 'V4 实测格式：a.txt + 工作目录')
    check(CK.target_path('../x.txt', {'realParentDir': '/private/tmp/p'}) is None, '相对键带 .. 跳出真实目录：拒绝')
    check(CK.target_path('a.txt', {'realParentDir': 'tmp/p'}) is None, 'realParentDir 不是绝对路径：拒绝')
    check(CK.target_path('a.txt', {'realParentDir': '/tmp/../etc'}) is None, 'realParentDir 带 ..：拒绝')
    check(not CK.good_backup({'backupFileName': '../../x', 'realParentDir': '/a'}), '备份文件名带路径：忽略这条快照')


def test_statusline_script():
    tmp = tempfile.mkdtemp(prefix='cwb-r15a1-sl-')
    try:
        d = os.path.join(tmp, 'ctx')
        os.makedirs(d)
        orig = os.path.join(tmp, 'orig sl.sh')
        with open(orig, 'w') as f:
            f.write('#!/bin/sh\ncat > "%s/got.json"\necho "ORIG-OUT"\necho "第二行"\n' % tmp)
        os.chmod(orig, 0o755)
        data = json.dumps({'session_id': 'x', 'context_window': {'used_percentage': 3}}, ensure_ascii=False) + '\n'
        env = dict(os.environ, CWB_GEN='g-1')
        cmd = S.shlex.quote(orig)
        subprocess.run(['/bin/sh', CX.SCRIPT, d, cmd], input=data.encode(), capture_output=True, env=env, timeout=10)  # 新建的脚本第一次执行 macOS 要检查，先跑一次
        t0 = time.time()
        p = subprocess.run(['/bin/sh', CX.SCRIPT, d, cmd], input=data.encode(), capture_output=True, env=env, timeout=10)
        ms = (time.time() - t0) * 1000
        check(p.stdout.decode() == 'ORIG-OUT\n第二行\n', '原命令的输出原样输出')
        check(open(os.path.join(tmp, 'got.json')).read() == data, '原命令收到的标准输入和 CLI 给的完全一样（含结尾换行）')
        check(open(os.path.join(d, 'g-1.json')).read() == data and not [n for n in os.listdir(d) if n.endswith('.tmp')], '状态文件写好，没有残留临时文件')
        check(ms < 500, f'脚本耗时 {ms:.0f}ms')
        p = subprocess.run(['/bin/sh', CX.SCRIPT, d, ''], input=data.encode(), capture_output=True, env=env, timeout=10)
        check(p.stdout == b'\n', '用户没配状态行：输出空行')
        p = subprocess.run(['/bin/sh', CX.SCRIPT, os.path.join(tmp, '没有这个目录'), cmd], input=data.encode(), capture_output=True, env=env, timeout=10)
        check(p.stdout.decode().startswith('ORIG-OUT'), '状态目录不可写：照常执行原命令')
        env2 = dict(env, CWB_GEN='../evil')
        p = subprocess.run(['/bin/sh', CX.SCRIPT, d, cmd], input=data.encode(), capture_output=True, env=env2, timeout=10)
        check(p.stdout.decode().startswith('ORIG-OUT') and not os.path.exists(os.path.join(tmp, 'evil.json')), '代次带路径分隔符：不写文件，照常执行原命令')
        env3 = {k: v for k, v in env.items() if k != 'CWB_GEN'}
        p = subprocess.run(['/bin/sh', CX.SCRIPT, d, cmd], input=data.encode(), capture_output=True, env=env3, timeout=10)
        check(p.stdout.decode().startswith('ORIG-OUT'), '没有代次环境变量：照常执行原命令')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_inject_and_parse():
    tmp = tempfile.mkdtemp(prefix='cwb-r15a1-st-')
    old_env, old_load = os.environ.get('CWB_USER_SETTINGS'), S.load_conf
    try:
        fake = os.path.join(tmp, 'settings.json')
        with open(fake, 'w') as f:
            json.dump({'statusLine': {'type': 'command', 'command': "'/x/my sl.sh' --a", 'padding': 0, 'refreshInterval': 30}}, f)
        mt = os.path.getmtime(fake)
        os.environ['CWB_USER_SETTINGS'] = fake
        prefs = {}
        S.load_conf = lambda: {'prefs': prefs}
        CX.PREF['t'] = 0
        s = S.build_settings({})
        sl = s.get('statusLine') or {}
        check(sl.get('type') == 'command' and sl.get('padding') == 0 and sl.get('refreshInterval') == 30, '偏好默认开：注入 statusLine，沿用原来的 padding / refreshInterval')
        argv = S.shlex.split(sl.get('command', ''))
        check(argv[:2] == ['/bin/sh', CX.SCRIPT] and argv[2] == CX.ctx_dir() and argv[3] == "'/x/my sl.sh' --a", '命令：/bin/sh statusline.sh <状态目录> <原命令>')
        check(os.path.getmtime(fake) == mt, '用户设置文件没被写')
        prefs['ctxStatus'] = False
        CX.PREF['t'] = 0
        check('statusLine' not in S.build_settings({}), '偏好关掉：不注入状态行')
        check(S.load_prefs({'prefs': {'ctxStatus': False}})['ctxStatus'] is False and 'ctxStatus' not in S.load_prefs({}),
              'prefs.ctxStatus 默认开：只在关掉时带 false（不改变原有 prefs 返回）')
        conf = {'prefs': {}}
        S.load_conf, old_save = (lambda: conf), S.save_conf
        S.save_conf = lambda c: conf.update(c)
        try:
            check(S.api_prefs({'set': {'ctxStatus': False}})['prefs'].get('ctxStatus') is False, 'api_prefs 关掉上下文用量')
            check('ctxStatus' not in S.api_prefs({'set': {'ctxStatus': True}})['prefs'], 'api_prefs 打开（回到默认）')
        finally:
            S.save_conf = old_save
            S.load_conf = lambda: {'prefs': prefs}
        os.environ['CWB_USER_SETTINGS'] = os.path.join(tmp, 'none.json')
        prefs['ctxStatus'] = True
        CX.PREF['t'] = 0
        sl = S.build_settings({}).get('statusLine') or {}
        check(S.shlex.split(sl.get('command', ''))[3:] == [''], '用户没配状态行：原命令参数为空')
        raw = json.dumps({'context_window': {'context_window_size': 1000000, 'used_percentage': 12,
                                             'current_usage': {'input_tokens': 100, 'output_tokens': 50, 'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 118000}},
                          'prompt_cache': {'warm': True, 'expires_at': 1794571800}, 'cost': {'total_cost_usd': 0.42}})
        c = CX.parse(raw, 1794571500123)
        check(c == {'pct': 12, 'used': 118120, 'window': 1000000, 'cacheWarm': True, 'cacheExpiresAt': 1794571800000, 'cache': 'warm', 'cost': 0.42,
                    'at': 1794571500123},
              f'ctx 解析：{c}')
        c = CX.parse(json.dumps({'context_window': {'context_window_size': 200000, 'current_usage': None, 'used_percentage': None}}), 1)
        check(c['pct'] is None and c['used'] is None and c['window'] == 200000 and c['cacheWarm'] is None and c['cost'] is None, '首次请求前：拿不到的给 null')
        check(CX.to_ms('2026-11-13T12:00:00Z') == 1794571200000 and CX.to_ms(1794571200000) == 1794571200000, '缓存过期时间：秒、毫秒、ISO 都转成毫秒')
        # 运行状态里的 ctx 与事件
        d = os.path.join(tmp, 'run')
        old_run = S.RUN_DIR
        S.RUN_DIR = d
        os.makedirs(os.path.join(d, 'ctx'))
        r = FakeRunner('11111111-2222-3333-4444-555555555555', False)
        r.gen = 'g-test-1'
        S.RUNNERS[r.sid] = r
        evs = []
        old_emit = S.BUS.emit
        S.BUS.emit = evs.append
        try:
            with open(os.path.join(d, 'ctx', 'g-test-1.json'), 'w') as f:
                f.write(raw)
            CX.scan()
            check(evs and evs[-1]['type'] == 'ctx' and evs[-1]['sid'] == r.sid and evs[-1]['ctx']['pct'] == 12, '状态文件变了：推 ctx 事件')
            check(CX.ctx_of(r)['used'] == 118120, '运行状态里的 ctx 取缓存')
            n = len(evs)
            CX.scan()
            check(len(evs) == n, '没变化不重复推')
            with open(os.path.join(d, 'ctx', 'g-test-1.json'), 'w') as f:
                f.write('{"context_window": {')
            os.utime(os.path.join(d, 'ctx', 'g-test-1.json'), (time.time() + 5, time.time() + 5))
            CX.scan()
            check(CX.ctx_of(r)['used'] == 118120, '半截文件：保留上一次的，不抛异常')
            prefs['ctxStatus'] = False
            CX.PREF['t'] = 0
            check(CX.ctx_of(r) is None, '偏好关掉：运行状态的 ctx 为 null')
        finally:
            S.BUS.emit = old_emit
            S.RUNNERS.pop(r.sid, None)
            S.RUN_DIR = old_run
    finally:
        S.load_conf = old_load
        CX.PREF['t'] = 0
        if old_env is None:
            os.environ.pop('CWB_USER_SETTINGS', None)
        else:
            os.environ['CWB_USER_SETTINGS'] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


def test_memory_and_save():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix='cwb-r15a1-mem-'))
    try:
        home = os.path.join(tmp, 'home')
        proj = os.path.join(home, 'work', 'proj')
        os.makedirs(os.path.join(home, '.claude', 'rules'))
        os.makedirs(os.path.join(proj, '.claude', 'rules'))
        mem = os.path.join(home, '.claude', 'projects', CX._sanitize(proj), 'memory')
        os.makedirs(mem)
        for p, t in [(os.path.join(home, '.claude', 'CLAUDE.md'), '全局'), (os.path.join(home, '.claude', 'rules', 'r.md'), 'r'),
                     (os.path.join(proj, 'CLAUDE.md'), '项目'), (os.path.join(proj, 'CLAUDE.local.md'), '本地'),
                     (os.path.join(home, 'work', 'CLAUDE.md'), '上级'), (os.path.join(proj, '.claude', 'rules', 'x.md'), 'x'),
                     (os.path.join(mem, 'MEMORY.md'), '索引'), (os.path.join(mem, 'a_note.md'), '记忆')]:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(t)
        g = {x['key']: x for x in CX.memory_files(proj, home=home)['groups']}
        check([x['label'] for x in g.values()] == ['全局指令', '项目指令', '自动记忆'], '三组：全局指令、项目指令、自动记忆')
        check([f['name'] for f in g['user']['files']] == ['CLAUDE.md', 'rules/r.md'], f"全局：{[f['name'] for f in g['user']['files']]}")
        names = [f['name'] for f in g['project']['files']]
        check(names[:3] == ['CLAUDE.md', 'CLAUDE.local.md', '.claude/rules/x.md'] and any(n.endswith('work/CLAUDE.md') for n in names), f'项目：本文件夹和上级 {names}')
        check([f['name'] for f in g['auto']['files']] == ['MEMORY.md', 'a_note.md'], '自动记忆：MEMORY.md 排在最前')
        f0 = g['project']['files'][0]
        check(isinstance(f0['mtime'], float) and f0['size'] == len('项目'.encode()), '文件带 mtime（秒，同 preview 接口）和 size')
        # 保存
        p = f0['path']
        r = CX.api_file_save({'path': p, 'text': '项目改过\n', 'mtime': f0['mtime']})
        check(r.get('ok') and open(p, encoding='utf-8').read() == '项目改过\n', '保存成功')
        r2 = CX.api_file_save({'path': p, 'text': '又改', 'mtime': f0['mtime']})
        check(r2.get('conflict') is True and r2.get('error') and open(p, encoding='utf-8').read() == '项目改过\n', 'mtime 不一致：报冲突、不写')
        r3 = CX.api_file_save({'path': p, 'text': '再改', 'mtime': r['mtime']})
        check(r3.get('ok'), '用返回的新 mtime 接着保存')
        link = os.path.join(proj, 'AGENTS.md')
        os.symlink(p, link)
        r4 = CX.api_file_save({'path': link, 'text': '经链接改', 'mtime': os.stat(p).st_mtime})
        check(r4.get('ok') and os.path.islink(link) and open(p, encoding='utf-8').read() == '经链接改', '符号链接：写到指向的文件，链接保留')
        for body, why in [({'path': os.path.join(proj, 'x.py'), 'text': 'a', 'mtime': 1}, '非 Markdown / 纯文本'),
                          ({'path': os.path.join(proj, '不存在.md'), 'text': 'a', 'mtime': 1}, '不存在的文件'),
                          ({'path': p, 'text': 'a' * (1024 * 1024 + 1), 'mtime': os.stat(p).st_mtime}, '超过 1MB'),
                          ({'path': p, 'text': 'a'}, '缺 mtime'), ({'path': 'rel.md', 'text': 'a', 'mtime': 1}, '相对路径')]:
            if 'x.py' in body['path']:
                open(body['path'], 'w').close()
            try:
                CX.api_file_save(body)
                check(False, '拒绝：' + why)
            except ValueError:
                check(True, '拒绝：' + why)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_misc_api():
    for k in ('turn-changes', 'turn-diff', 'rewind-preview', 'rewind-apply', 'rewind-undo', 'context-detail', 'memory-files', 'file-save',
              'open-apps', 'open-with', 'mark-unread'):
        if k not in S.API:
            check(False, '接口已注册：' + k)
            return
    check(True, '11 个接口都已注册')
    apps = CX.api_open_apps({})['apps']
    check(apps and apps[0] == {'key': 'finder', 'name': '访达'} and all(a['key'] in ('finder', 'vscode', 'cursor', 'terminal') for a in apps),
          f'open-apps：{[a["name"] for a in apps]}')
    try:
        CX.api_open_with({'cwd': '/nonexistent-cwb', 'app': 'finder'})
        check(False, 'open-with：文件夹不存在报错')
    except ValueError:
        check(True, 'open-with：文件夹不存在报错')
    try:
        CX.api_open_with({'cwd': '/tmp', 'app': 'xcode'})
        check(False, 'open-with：未知应用报错')
    except ValueError:
        check(True, 'open-with：未知应用报错')
    tmp = tempfile.mkdtemp(prefix='cwb-r15a1-mk-')
    old = S.MARKS_PATH
    evs, old_emit = [], S.BUS.emit
    try:
        S.MARKS_PATH = os.path.join(tmp, 'marks.json')
        S.BUS.emit = evs.append
        sid = str(U.uuid4())
        t0 = time.time()
        r = S.API['mark-unread']({'sid': sid})
        check(r == {'ok': True} and S.MARKS['done_at'].get(sid, 0) >= t0, 'mark-unread：done_at 记为现在（晚于看过的时间即未读）')
        check(any(e.get('type') == 'marks' and e.get('sid') == sid for e in evs) and any(e.get('type') == 'sessions' for e in evs), '推 marks 和 sessions 事件')
        S.MARKS['done_at'].pop(sid, None)
    finally:
        S.MARKS_PATH = old
        S.BUS.emit = old_emit
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        S.API['context-detail']({'sid': str(U.uuid4())})
        check(False, 'context-detail：对话不在运行时报错')
    except ValueError as e:
        check('没有在运行' in str(e), 'context-detail：对话不在运行时报错')
    r = FakeRunner(str(U.uuid4()), True)
    r.state.update(ready=True)
    S.RUNNERS[r.sid] = r
    try:
        S.API['context-detail']({'sid': r.sid})
        check(False, 'context-detail：回复中不发 /context')
    except ValueError as e:
        check('回复' in str(e), 'context-detail：回复中不发 /context')
    finally:
        S.RUNNERS.pop(r.sid, None)


if __name__ == '__main__':
    for fn in (test_turns, test_incremental, test_after_and_preview, test_missing, test_apply_undo, test_paths,
               test_statusline_script, test_inject_and_parse, test_memory_and_save, test_misc_api):
        print('==', fn.__name__, flush=True)
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(False, f'{fn.__name__} 异常 {e!r}')
    print('全部通过' if not FAIL else f'{len(FAIL)} 项失败：' + '；'.join(FAIL))
    sys.exit(1 if FAIL else 0)
