"""11-23 删除项目（api_delete_project）单元测试。
不启动 claude、不碰 ~/.claude、不真的移到废纸篓：记录文件、配置都造在临时目录里，trash 换成搬到临时“废纸篓”目录。
运行：.venv/bin/python -I tests/test_delproj.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def touch(p, text='{}\n'):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w') as f:
        f.write(text)


def setup(root):
    proj = os.path.join(root, 'projects')
    A, B, C = '/nonexist/proj-a', '/nonexist/proj-a/.claude/worktrees/w1', '/nonexist/proj-c'
    touch(os.path.join(proj, '-a', 's1.jsonl'))
    touch(os.path.join(proj, '-a', 's1', 'subagents', 'agent-1.jsonl'))
    touch(os.path.join(proj, '-a', 'memory', 'MEMORY.md'), '记忆\n')
    touch(os.path.join(proj, '-a-wt', 's2.jsonl'))
    touch(os.path.join(proj, '-c', 's3.jsonl'))
    outside = os.path.join(root, 'elsewhere', 's4.jsonl')  # 不在 projects 目录里的记录：不该被动
    touch(outside)
    sess = [{'sid': 's1', 'cwd': A, 'path': os.path.join(proj, '-a', 's1.jsonl'), 'title': 't1', 'mtime': 3},
            {'sid': 's2', 'cwd': B, 'path': os.path.join(proj, '-a-wt', 's2.jsonl'), 'title': 't2', 'mtime': 2},
            {'sid': 's3', 'cwd': C, 'path': os.path.join(proj, '-c', 's3.jsonl'), 'title': 't3', 'mtime': 1},
            {'sid': 's4', 'cwd': A, 'path': outside, 'title': 't4', 'mtime': 0}]
    conf = {'folders': [A, C], 'hidden_projects': [A, C], 'project_opts': {A: {'model': 'x'}, C: {'model': 'y'}},
            'archived': ['s1', 's3'], 'pinned': ['s2', 's3'], 'names': {'s1': 'n1', 's3': 'n3'}, 'session_opts': {'s1': {}, 's3': {}}}
    with open(os.path.join(root, 'config.json'), 'w') as f:
        json.dump(conf, f)
    return proj, A, C, sess, outside


def run():
    root = tempfile.mkdtemp(prefix='cwb-delproj-')
    trash_dir = os.path.join(root, 'trash')
    os.makedirs(trash_dir)
    orig = (S.CONF_PATH, S.PROJECTS_DIR, S.INDEX.scan, S.trash)
    fail_trash = {'on': False}

    def fake_trash(paths):
        if fail_trash['on']:
            return False
        for i, p in enumerate(paths):
            shutil.move(p, os.path.join(trash_dir, f'{i}-{os.path.basename(p)}'))
        return True
    try:
        proj, A, C, sess, outside = setup(root)
        S.CONF_PATH, S.PROJECTS_DIR, S.trash = os.path.join(root, 'config.json'), proj, fake_trash
        S.INDEX.scan = lambda force=False: [dict(x) for x in sess if os.path.exists(x['path'])]

        # 1. 路径不对
        try:
            S.api_delete_project({'cwd': 'relative/path'})
            check(False, '相对路径应报错')
        except ValueError:
            check(True, '相对路径报错')

        # 2. 移到废纸篓失败：项目留着，配置不动
        fail_trash['on'] = True
        try:
            S.api_delete_project({'cwd': A})
            check(False, '废纸篓失败应报错')
        except ValueError:
            check(True, '废纸篓失败时报错')
        c = S.load_conf()
        check(A in c['hidden_projects'] and A in c['folders'], '废纸篓失败时项目仍在归档和手动添加列表里')
        check(os.path.exists(os.path.join(proj, '-a', 's1.jsonl')), '废纸篓失败时记录还在')
        fail_trash['on'] = False

        # 3. 正常删除
        d = S.api_delete_project({'cwd': A + '/'})  # 带结尾斜杠也认
        check(d.get('ok'), '删除返回成功')
        check(not os.path.exists(os.path.join(proj, '-a', 's1.jsonl')), 's1 记录已移走')
        check(not os.path.exists(os.path.join(proj, '-a', 's1')), 's1 的子代理目录一起移走')
        check(os.path.exists(os.path.join(proj, '-a', 'memory', 'MEMORY.md')), 'memory 目录保留')
        check(not os.path.exists(os.path.join(proj, '-a-wt')), '工作树对话的记录目录空了被删掉')
        check(os.path.exists(os.path.join(proj, '-c', 's3.jsonl')), '别的项目的记录不动')
        check(os.path.exists(outside), 'projects 目录外的记录不动')
        check(os.path.exists('/nonexist') is False, '没有在磁盘上创建或动过项目文件夹路径')
        c = S.load_conf()
        check(c['folders'] == [C] and c['hidden_projects'] == [C], '手动添加和归档列表只剩别的项目')
        check(list(c['project_opts']) == [C], '项目默认设置只剩别的项目')
        check(c['archived'] == ['s3'] and c['pinned'] == ['s3'], '归档、置顶里去掉了被删的对话')
        check(list(c['names']) == ['s3'] and list(c['session_opts']) == ['s3'], '改名、对话设置里去掉了被删的对话')
        check(len(os.listdir(trash_dir)) == 3, '废纸篓里正好 3 项（s1、s1 目录、s2）')

        # 4. 再删另一个项目：列表清空
        d = S.api_delete_project({'cwd': C})
        check(d.get('ok') and S.load_conf()['folders'] == [], '删除后手动添加列表清空')
        check(not os.path.exists(os.path.join(proj, '-c')), 'C 项目的记录目录空了被删掉')
    finally:
        S.CONF_PATH, S.PROJECTS_DIR, S.INDEX.scan, S.trash = orig
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    run()
    print('全部通过' if not FAIL else f'{len(FAIL)} 项失败', flush=True)
    sys.exit(1 if FAIL else 0)
