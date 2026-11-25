"""第十四轮修复线 FJ 单元测试：用量柱状图按模型拆分（buckets[].models）、prefs 带 CLI 全局默认权限、项目分组按真实路径合并。
不启动 claude、不碰 ~/.claude：记录文件、配置、文件夹都造在临时目录里。运行：.venv/bin/python -I tests/test_r14_fj.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usage as U  # noqa: E402
import server as S  # noqa: E402
from test_r14_i import build_fixture  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def test_bucket_models():
    root = tempfile.mkdtemp(prefix='cwb-r14fj-')
    try:
        now = time.time()
        build_fixture(root, now - 3 * 86400)
        u = U.Usage(root, cache_path=os.path.join(root, 'run', 'c.json'), runs_path=os.path.join(root, 'run', 'r.jsonl'), titles=lambda s: {})
        u.start()
        for _ in range(100):
            if not u.scanning:
                break
            time.sleep(0.05)
        for rng in ('7d', '30d', 'today'):
            q = u.query(rng, now=now)
            bs = q['buckets']
            ok = all(isinstance(b.get('models'), dict) for b in bs)
            for b in bs:
                ms = b['models'].values()
                ok = ok and sum(m['tokens'] for m in ms) == b['tokens'] and sum(m['requests'] for m in ms) == b['requests'] \
                    and abs(sum(m['cost'] for m in ms) - b['cost']) < 1e-3 and all(set(m) == {'tokens', 'cost', 'requests'} for m in ms)
            ids = {k for b in bs for k in b['models']}
            check(ok and ids <= {m['id'] for m in q['models']}, f'{rng}：每个桶带 models，按模型的 tokens / requests / cost 加起来等于桶合计，模型 id 与 models 列表一致')
        q = u.query('7d', now=now)
        full = [b for b in q['buckets'] if b['requests']]
        check(len(full) == 1 and list(full[0]['models']) == ['claude-opus-5-5', 'claude-haiku-5-5'],
              f"7d：有用量的那天按 tokens 降序拆成两个模型（{[list(b['models']) for b in full]}）")
        check(all(b['models'] == {} for b in q['buckets'] if not b['requests']), '没有用量的桶 models 为空对象')
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_prefs_cli_mode():
    d = tempfile.mkdtemp(prefix='cwb-r14fj-conf-')
    orig_conf, orig_us = S.CONF_PATH, S.user_settings
    S.CONF_PATH = os.path.join(d, 'config.json')
    try:
        cases = [({}, None), ({'permissions': {}}, None), ({'permissions': {'defaultMode': 'default'}}, 'default'),
                 ({'permissions': {'defaultMode': 'auto'}}, 'auto mode'), ({'permissions': {'defaultMode': 'manual'}}, 'manual mode'),
                 ({'permissions': {'defaultMode': 'acceptEdits'}}, 'accept edits'), ({'permissions': {'defaultMode': 'plan'}}, 'plan mode'),
                 ({'permissions': {'defaultMode': 'bypassPermissions'}}, 'bypass permissions'),
                 ({'permissions': {'defaultMode': 'somethingNew'}}, None), ({'permissions': 'x'}, None)]
        got = []
        for st, want in cases:
            S.user_settings = lambda st=st: st
            got.append(S.api_prefs({})['cli'] == {'mode': want})
        check(all(got), f'prefs.cli.mode：permissions.defaultMode 反查 MODE_FLAG，default 原样，没设置或认不出为 null（{got}）')
        S.user_settings = lambda: {'permissions': {'defaultMode': 'plan'}}
        r = S.api_prefs({'set': {'defaults': {'mode': 'accept edits'}}})
        check(r['cli'] == {'mode': 'plan mode'} and r['prefs']['defaults']['mode'] == 'accept edits', 'prefs 保存后也带 cli，且不影响 prefs 本身')
    finally:
        S.CONF_PATH, S.user_settings = orig_conf, orig_us
        shutil.rmtree(d, ignore_errors=True)


def test_projects_realpath():
    d = tempfile.mkdtemp(prefix='cwb-r14fj-proj-')
    orig = (S.CONF_PATH, S.INDEX.scan, S.SHOW_TMP)
    try:
        real, link, other = os.path.join(d, 'real', 'proj'), os.path.join(d, 'link'), os.path.join(d, 'other')
        os.makedirs(real)
        os.makedirs(other)
        os.symlink(os.path.join(d, 'real'), link)
        alias = os.path.join(link, 'proj')  # 经符号链接的同一个文件夹
        S.CONF_PATH = os.path.join(d, 'config.json')
        S.save_conf({'folders': [alias, other]})
        S.SHOW_TMP = True
        sess = [{'sid': 'a1', 'cwd': real, 'title': '甲', 'mtime': 300.0}, {'sid': 'a2', 'cwd': alias, 'title': '乙', 'mtime': 200.0},
                {'sid': 'b1', 'cwd': other, 'title': '丙', 'mtime': 100.0}]
        S.INDEX.scan = lambda force=False: [dict(x) for x in sess]
        ps = {p['cwd']: p for p in S.projects_payload()}
        check(sorted(ps) == sorted([alias, other]), f'真实路径相同的两组合成一组，用户添加过的文件夹路径优先（{sorted(ps)}）')
        check([x['sid'] for x in ps[alias]['sessions']] == ['a1', 'a2'] and ps[alias]['mtime'] == 300.0 and ps[alias]['name'] == 'proj',
              '合并后对话按时间排序，项目时间取最新的一条')
        check([x['sid'] for x in ps[other]['sessions']] == ['b1'], '不相关的项目不受影响')
        S.save_conf({'folders': []})
        ps = [p['cwd'] for p in S.projects_payload()]
        check(ps == [real, other] or sorted(ps) == sorted([real, other]), f'没有添加过的文件夹时保留先出现的路径（{ps}）')
        sess.pop(1)
        ps = [p['cwd'] for p in S.projects_payload()]
        check(sorted(ps) == sorted([real, other]), '没有重复时行为不变')
    finally:
        S.CONF_PATH, S.INDEX.scan, S.SHOW_TMP = orig
        shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    test_bucket_models()
    test_prefs_cli_mode()
    test_projects_realpath()
    print('全部通过' if not FAIL else f'{len(FAIL)} 项失败：' + '；'.join(FAIL))
    sys.exit(1 if FAIL else 0)
