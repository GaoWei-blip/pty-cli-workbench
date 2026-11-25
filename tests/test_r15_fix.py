"""第十五轮修复线单元测试：计划文件不算改动、分叉对话继承的轮不算改动不回退（why: fork / gone）、缓存三种状态、压缩摘要落盘与对上。
不启动 claude、不碰 ~/.claude：会话记录、快照目录、计划文件夹、分叉记录都造在临时目录里（沿用 test_r15_a1 的 Fx / build）。
运行：.venv/bin/python -I tests/test_r15_fix.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_r15_a1 as A  # noqa: E402  （会把副本根目录加进 sys.path、导入 server）
from test_r15_a1 import build, files_of  # noqa: E402

S, CK, CX = A.S, A.CK, A.CX
FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def raises(fn, word=''):
    try:
        fn()
    except ValueError as e:
        return word in str(e)
    return False


class Tmp:
    """计划文件夹、分叉记录放在临时目录；结束时还原模块变量。"""

    def __enter__(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix='cwb-r15fix-'))
        self.old = (CK.PLANS_DIR, CK.FORKS_FILE)
        CK.FORKS_FILE = os.path.join(self.d, 'run', 'forks.json')
        CK._PLANS.clear()
        CK._FORKS.update(key=None, d={})
        return self

    def __exit__(self, *a):
        CK.PLANS_DIR, CK.FORKS_FILE = self.old
        CK._PLANS.clear()
        CK._FORKS.update(key=None, d={})
        shutil.rmtree(self.d, ignore_errors=True)


# ---------------------------------------------------------------- 1. 计划文件
def test_plan_files():
    x = build()
    with Tmp() as tm:
        try:
            real = os.path.join(tm.d, 'home', '.claude', 'plans')
            os.makedirs(real)
            link = os.path.join(tm.d, 'alias-plans')
            os.symlink(real, link)
            CK.PLANS_DIR = link  # 计划文件夹用符号链接给出，快照里记的是真实目录：要按真实路径认出来
            pf = os.path.join(real, 'shiny-plan.md')
            T = x.T0 + 400
            x.rows = []
            u5 = x.prompt('R5 写计划', T)
            x.snap(u5, {'a.txt': x.bks['a2'], 'b.txt': x.bks['b3'], 'f.txt': x.bks['f2'], 'c.txt': x.bk('c.txt', 'C改\n', T)}, T)
            x.row({'type': 'file-history-delta', 'messageId': 'm-plan', 'snapshotMessageId': u5, 'trackingPath': pf,
                   'backup': {'backupFileName': None, 'version': 1, 'backupTime': A.iso(T + 1), 'realParentDir': real}}, chain=False)
            x.tool('Write', pf, T + 2)
            with open(pf, 'w', encoding='utf-8') as f:
                f.write('# 计划\n\n1. 改 a\n')
            x.tool('Edit', x.p('a.txt'), T + 3)
            x.disk('a.txt', 'A计划后\n', T + 3)
            x.end(T + 4)
            x.append(x.rows)
            r = CK.turn_changes(x.sid, x.jsonl)
            by = {t['uuid']: t for t in r['turns']}
            check(files_of(by[u5]) == {'a.txt': ('mod', 1, 1)}, f'计划文件不进这一轮的改动胶囊：{files_of(by[u5])}')
            check(all(not f['real'].startswith(real) for t in r['turns'] for f in t['files']) and 'shiny-plan.md' not in json.dumps(r['all']),
                  '计划文件不进“整个对话”的文件列表')
            check(raises(lambda: CK.turn_diff(x.sid, u5, pf, x.jsonl)) and raises(lambda: CK.turn_diff(x.sid, '*', pf, x.jsonl)),
                  'turn-diff：计划文件按“不在改动里”拒绝')
            pv = CK.preview(x.sid, u5, False, x.jsonl)
            check({f['rel'] for f in pv['write']} == {'a.txt'} and pv['remove'] == [], f'回退预览不含计划文件：{pv}')
            res = CK.apply(x.sid, u5, False, x.jsonl)
            check(res['ok'] and os.path.exists(pf) and x.read('a.txt') == 'A1\n', '回退执行：计划文件原样保留（不会被当成“这之后新建的”删掉）')
            check(CK.plan_file(os.path.join(real, 'sub', 'x.md')) and not CK.plan_file(x.p('a.txt')) and not CK.plan_file(real + '-x/y.md'),
                  'plan_file：计划文件夹（含子目录）里才算，前缀相同的别的目录不算')
        finally:
            x.done()


# ---------------------------------------------------------------- 3. 分叉
def test_fork():
    x = build()
    with Tmp():
        try:
            u = x.u
            plain = CK.turn_changes(x.sid, x.jsonl)
            check(plain['inherited'] == [] and all(t['why'] is None for t in plain['turns']) and plain['all']['why'] is None,
                  '不是分叉对话：inherited 为空，why 为 null')
            CK.note_fork(x.sid, 'src-sid', 'at-uuid')
            info = CK.fork_info(x.sid)
            check(info and info['src'] == 'src-sid' and info['at'] == 'at-uuid' and abs(info['t'] / 1000 - time.time()) < 5, f'note_fork 记下分叉：{info}')
            # 分叉时间放在 R2 和 R3 之间：R1、R2（分叉点那一轮）继承自原对话，R3、R4 是分叉后新发生的
            d = json.load(open(CK.FORKS_FILE))
            d[x.sid]['t'] = int((x.T0 + 100) * 1000)
            with open(CK.FORKS_FILE, 'w') as f:
                json.dump(d, f)
            r = CK.turn_changes(x.sid, x.jsonl)
            check(r['inherited'] == [u['u1'], u['u2']], f'inherited：分叉前各轮（含分叉点那一轮）{r["inherited"]}')
            check([t['uuid'] for t in r['turns']] == [u['u3'], u['u4']], '继承的轮不出现在 turns 里（不显示改动数）')
            check(set(files_of(r['all'])) == {'b.txt', 'c.txt', 'f.txt'} and files_of(r['all'])['b.txt'] == ('mod', 1, 0),
                  f'“整个对话”从分叉后第一轮开始前算起：{files_of(r["all"])}')
            check(raises(lambda: CK.turn_diff(x.sid, u['u2'], x.p('b.txt'), x.jsonl), '原对话'), 'turn-diff：继承的轮拒绝')
            df = CK.turn_diff(x.sid, '*', x.p('b.txt'), x.jsonl)
            check('+B追加' in df['diff'] and '-第二行' not in df['diff'], 'turn-diff *：以分叉后第一轮开始前为底')
            S.RUNNERS.pop(x.sid, None)
            for uu, after, name in ((u['u2'], False, '分叉点那一轮之前'), (u['u2'], True, '分叉点那一轮结束时'), (u['u1'], False, '更早的轮')):
                check(raises(lambda: CK.preview(x.sid, uu, after, x.jsonl), '原对话') and raises(lambda: CK.apply(x.sid, uu, after, x.jsonl), '原对话'),
                      f'回退预览与执行：{name}拒绝')
            pv = CK.preview(x.sid, u['u3'], False, x.jsonl)
            check({f['rel'] for f in pv['write']} == {'b.txt', 'c.txt'} and {f['rel'] for f in pv['remove']} == {'f.txt'}, f'分叉后的轮照常回退：{pv}')
            # 快照被清理：why = gone
            os.remove(os.path.join(x.fh, x.bks['c1']['backupFileName']))
            CK.STATS.clear()
            r = CK.turn_changes(x.sid, x.jsonl)
            by = {t['uuid']: t for t in r['turns']}
            check(u['u4'] not in by or (by[u['u4']]['expired'] and by[u['u4']]['why'] == 'gone'), '快照已清理的轮：why = gone')
            check(r['all']['expired'] is True and r['all']['why'] == 'gone', '累计：why = gone')
            # 记录文件坏了：当作不是分叉，不抛异常
            with open(CK.FORKS_FILE, 'w') as f:
                f.write('{坏')
            check(CK.fork_info(x.sid) is None and CK.turn_changes(x.sid, x.jsonl)['inherited'] == [], '分叉记录读不了：当作不是分叉')
            # 只留最近 KEEP_FORKS 条
            old_keep, CK.KEEP_FORKS = CK.KEEP_FORKS, 3
            try:
                os.remove(CK.FORKS_FILE)
                for i in range(5):
                    CK.note_fork('s%d' % i, 'src')
                    time.sleep(0.002)
                check(sorted(json.load(open(CK.FORKS_FILE))) == ['s2', 's3', 's4'], '分叉记录只留最近几条')
            finally:
                CK.KEEP_FORKS = old_keep
        finally:
            x.done()


# ---------------------------------------------------------------- 2. 缓存三种状态
def test_cache_state():
    def pc(**k):
        return CX.parse(json.dumps({'context_window': {'context_window_size': 1000, 'used_percentage': 5}, 'prompt_cache': k}), 1)
    c = pc(warm=False, caching_observed=False, expires_at=None)
    check(c['cache'] == 'unknown' and c['cacheWarm'] is None and c['cacheExpiresAt'] is None, f'从没观察到缓存：unknown，cacheWarm 为 null {c}')
    c = pc(warm=False)
    check(c['cache'] == 'unknown' and c['cacheWarm'] is None, 'caching_observed 缺失：unknown')
    c = CX.parse(json.dumps({'context_window': {'context_window_size': 1000}}), 1)
    check(c['cache'] == 'unknown', '没有 prompt_cache：unknown')
    c = pc(warm=False, caching_observed=True, expires_at=None)
    check(c['cache'] == 'expired' and c['cacheWarm'] is False, '观察到过缓存、现在不热：expired')
    c = pc(warm=True, caching_observed=True, expires_at=1794571800)
    check(c['cache'] == 'warm' and c['cacheWarm'] is True and c['cacheExpiresAt'] == 1794571800000, '缓存有效：warm，带到期时间（毫秒）')


# ---------------------------------------------------------------- 4. 压缩摘要
def test_compacts():
    with Tmp() as tm:
        old_cd = S.COMPACT_DIR
        S.COMPACT_DIR = os.path.join(tm.d, 'compact')
        try:
            for i in range(7):
                S.compact_save('sid-a', {'t': 1000 + i, 'trigger': 'manual', 'summary': '摘要%d' % i})
            got = S.compact_load('sid-a')
            check([g['summary'] for g in got] == ['摘要%d' % i for i in range(2, 7)], f'每个对话只留最近 {S.COMPACT_KEEP} 条')
            check(S.compact_load('../x') == [] and S.compact_path('../x').startswith(S.COMPACT_DIR + '/'), 'sid 里的路径字符被换掉')
            with open(S.compact_path('sid-bad'), 'w') as f:
                f.write('[坏')
            check(S.compact_load('sid-bad') == [], '文件坏了：当作没有')
            # 分叉出来的对话：带上原对话里分叉前的摘要
            CK.note_fork('sid-f', 'sid-a')
            d = json.load(open(CK.FORKS_FILE))
            d['sid-f']['t'] = 1004
            with open(CK.FORKS_FILE, 'w') as f:
                json.dump(d, f)
            S.compact_save('sid-f', {'t': 2000, 'trigger': 'manual', 'summary': '分叉后'})
            check([g['summary'] for g in S.compacts_of('sid-f')] == ['摘要2', '摘要3', '分叉后'], '分叉对话：原对话分叉前的摘要 + 自己的')
            check([g['summary'] for g in S.compacts_of('sid-a')][-1] == '摘要6', '普通对话：只有自己的')
        finally:
            S.COMPACT_DIR = old_cd


if __name__ == '__main__':
    for fn in (test_plan_files, test_fork, test_cache_state, test_compacts):
        print('==', fn.__name__, flush=True)
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(False, f'{fn.__name__} 异常 {e!r}')
    print('全部通过' if not FAIL else f'{len(FAIL)} 项失败：' + '；'.join(FAIL))
    sys.exit(1 if FAIL else 0)
