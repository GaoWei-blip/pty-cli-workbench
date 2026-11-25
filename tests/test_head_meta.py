"""11-17 修复的单元测试：第一条提问带图、单行超过头部读取范围时，对话的项目（cwd）和第一条提问仍取文件开头的，不被尾部顶替。
不启动 claude、不碰 ~/.claude：记录文件造在临时目录里。
运行：.venv/bin/python -I tests/test_head_meta.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_r15_a1 as A  # noqa: E402  （导入 server）

S = A.S
FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def user(text, cwd, extra=''):
    return {'type': 'user', 'cwd': cwd, 'sessionId': 's', 'message': {'role': 'user', 'content': [
        {'type': 'text', 'text': text}] + ([{'type': 'image', 'source': {'type': 'base64', 'data': extra}}] if extra else [])}}


def write(fp, rows):
    with open(fp, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    return os.path.getsize(fp)


def main():
    tmp = tempfile.mkdtemp(prefix='cwb-headmeta-')
    try:
        pre = [{'type': 'mode', 'mode': 'x'}, {'type': 'permission-mode', 'permissionMode': 'default'},
               {'type': 'file-history-snapshot', 'snapshot': {}}]
        filler = [{'type': 'assistant', 'cwd': '/proj/tools/sub', 'message': {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'x' * 2000}]}} for _ in range(500)]

        # 一、第一条提问单行 45 万字节，之后 CLI 的 cwd 一直在子目录
        fp = os.path.join(tmp, 'big-first.jsonl')
        size = write(fp, pre + [user('看这张图', '/proj', 'A' * 450_000)] + filler + [user('部署', '/proj/tools/sub')])
        check(size > 800_000, f'造出的记录超过整读上限（{size} 字节）')
        m = S.read_meta(fp, size)
        check(m['cwd'] == '/proj', f"项目取开头的 cwd：{m['cwd']}")
        check(m['first'] == '看这张图', f"第一条提问取开头的：{m['first']}")
        check(S.first_cwd(fp) == '/proj', 'first_cwd 也取到开头的 cwd')
        check(S.HEAD_META.get(fp) == ('/proj', '看这张图'), '结果已缓存')

        # 二、普通大文件：头部就有 cwd，不走逐行查找
        fp2 = os.path.join(tmp, 'normal.jsonl')
        size2 = write(fp2, pre + [user('你好', '/proj2')] + filler)
        m2 = S.read_meta(fp2, size2)
        check(m2['cwd'] == '/proj2' and m2['first'] == '你好' and fp2 not in S.HEAD_META, '头部读得到时结果不变，也不额外查找')

        # 三、小文件整读：取第一条的 cwd
        fp3 = os.path.join(tmp, 'small.jsonl')
        size3 = write(fp3, pre + [user('小', '/p3'), user('后来', '/p3/sub')])
        m3 = S.read_meta(fp3, size3)
        check(m3['cwd'] == '/p3' and m3['first'] == '小', '小文件结果不变')

        # 四、头部有 cwd（附件行）但第一条提问超长：提问仍取开头的
        fp4 = os.path.join(tmp, 'att-first.jsonl')
        size4 = write(fp4, pre + [{'type': 'attachment', 'cwd': '/p4', 'attachment': {}}, user('长图', '/p4', 'B' * 450_000)]
                      + filler + [user('后来的话', '/p4/tools/sub')])
        m4 = S.read_meta(fp4, size4)
        check(m4['cwd'] == '/p4' and m4['first'] == '长图', f"头部只有 cwd 时第一条提问也取开头的：{m4['first']}")

        # 五、时间取最后一条提问或回复的，之后补写的 cost-state 等行不算；侧链、isMeta 不算
        fp5 = os.path.join(tmp, 'last-at.jsonl')
        rows = pre + [dict(user('问', '/p5'), timestamp='2026-11-17T08:44:18.079Z'),
                      {'type': 'assistant', 'cwd': '/p5', 'timestamp': '2026-11-17T08:44:37.265Z', 'message': {'role': 'assistant', 'content': []}},
                      {'type': 'assistant', 'cwd': '/p5', 'isSidechain': True, 'timestamp': '2026-11-18T01:00:00Z', 'message': {}},
                      dict(user('caveat', '/p5'), isMeta=True, timestamp='2026-11-18T01:00:01Z'),
                      {'type': 'cost-state', 'x': 1}]
        size5 = write(fp5, rows)
        import datetime as _dt
        want = _dt.datetime(2026, 11, 17, 8, 44, 37, 265000, tzinfo=_dt.timezone.utc).timestamp()
        m5 = S.read_meta(fp5, size5)
        check(abs((m5['last_at'] or 0) - want) < 0.001, f"最后活动时间取回复的时间：{m5['last_at']}")
        S.INDEX.cache.clear()
        check(S.read_meta(fp, size)['last_at'] is None, '没有时间戳时为空（列表回退用文件修改时间）')

        # 六、文件不存在
        check(S.head_meta(os.path.join(tmp, 'none.jsonl')) == (None, None), '文件不存在返回空')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n全部通过' if not FAIL else f'\n失败 {len(FAIL)} 项：' + '；'.join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
