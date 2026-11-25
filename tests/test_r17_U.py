"""第十七轮 U 单元测试汇总：额度（tests/r17u_quota_cases.py）＋用量与速度（tests/r17u_usage_cases.py）。
不启动 claude；不读真实 ~/.claude/projects 全量；Codex、agy 用假进程；数据都放临时目录。
运行：.venv/bin/python -I tests/test_r17_U.py
"""
import os
import sys
import tempfile
import traceback

TMP = tempfile.mkdtemp(prefix='cwb-r17u-')
os.environ['CWB_DATA_DIR'] = os.path.join(TMP, 'data')
os.environ['CWB_PORT'] = '47969'
os.environ['CWB_QUOTA_NO_LOOP'] = '1'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))
import server as S  # noqa: E402,F401

FAIL = []
N = [0]


def check(cond, msg):
    N[0] += 1
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


def main():
    import shutil
    S.CONF_PATH = os.path.join(TMP, 'config.json')
    # 用户 11-22 晚拍板：Claude 用量保持第十四轮现状（只算工作台），usage.py 必须和本轮基准逐字节相同
    base = os.path.join(os.path.dirname(ROOT), 'base', 'usage.py')
    if os.path.exists(base):
        check(open(base, 'rb').read() == open(os.path.join(ROOT, 'usage.py'), 'rb').read(), 'usage.py 与基准（~/cwb-dev/r17/base）逐字节相同')
    # 11-23 用户要求：侧栏额度小条去掉（不创建 #uuStrip、不为它定时读取）；Claude 额度卡恢复第十四轮老额度卡（去掉 5 小时、条宽＝剩余）
    js = open(os.path.join(ROOT, 'static', 'ux-u.js'), encoding='utf-8').read()
    check("el.id = 'uuStrip'" not in js and 'stripLoad' not in js and 'stripRender' not in js and "getElementById('uuStrip'); if (old) old.remove()" in js,
          '前端：侧栏不再创建额度小条 #uuStrip，已有的移除，没有为它做的启动和定时读取')
    check("w.key !== '5h'" in js and '剩余 ${Math.round(rem)}%' in js and 'width:${rem}%' in js and 'class="sj-qcard uu-qold"' in js,
          '前端：Claude 卡用第十四轮老额度卡的写法，去掉 5 小时，条宽＝剩余，右侧“剩余 X%”')
    import usage as US
    check(US.CACHE_VER == 2 and S.USAGE.__class__ is US.Usage and S.USAGE.mark == US.WB_MARK,
          'server 的 USAGE 还是原来的实例和参数（只算工作台、缓存版本不变，上线不重扫）')
    for name in ('r17u_quota_cases', 'r17u_usage_cases'):
        print(f'==== {name}', flush=True)
        try:
            mod = __import__(name)
            mod.run(check)
        except Exception as e:
            traceback.print_exc()
            check(False, f'{name} 出错：{e!r}')
    shutil.rmtree(TMP, ignore_errors=True)
    print(f'\n共 {N[0]} 项，失败 {len(FAIL)} 项')
    for m in FAIL:
        print('  失败：', m)
    sys.stdout.flush()
    os._exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
