"""11-18 个性化（persona.py）单元测试：临时主目录、临时配置，不碰 ~/.claude。
运行：.venv/bin/python -I tests/test_persona.py
"""
import json, os, shutil, sys, tempfile, threading
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.dirname(HERE))
import persona as PS
FAIL = []
def check(c, m):
    print(('通过 ' if c else '失败 ') + m, flush=True)
    if not c: FAIL.append(m)
def raises(fn, word):
    try: fn()
    except ValueError as e: return word in str(e)
    return False

tmp = tempfile.mkdtemp(prefix='cwb-ps-')
H = os.path.join(tmp, 'home'); os.makedirs(os.path.join(H, '.claude'))
PROJ = os.path.join(tmp, 'proj'); os.makedirs(PROJ)
CONF = os.path.join(tmp, 'config.json')
TR = []
def load_conf():
    try: return json.load(open(CONF))
    except Exception: return {}
def save_conf(c): json.dump(c, open(CONF, 'w'))
def trash(ps):
    for p in ps: shutil.move(p, os.path.join(tmp, 'trash-' + os.path.basename(p))); TR.append(p)
    return True
PS.NS = {'load_conf': load_conf, 'save_conf': save_conf, 'G': threading.Lock(), 'trash': trash}
PS.home = lambda: H
try:
    # 风格和语气
    r = PS.api_persona({})
    check(r['persona'] == {} and r['preview'] == '' and PS.launch_args() == [], '默认什么都不加')
    check([x['id'] for x in r['styles']][:5] == ['default', 'Concise', 'Proactive', 'Explanatory', 'Learning'], '内置输出风格')
    r = PS.api_persona({'set': {'style': 'Explanatory', 'tone': 'roast', 'emoji': True, 'brief': False, 'extra': '  先说结论  '}})
    check(r['persona'] == {'style': 'Explanatory', 'tone': 'roast', 'emoji': True, 'extra': '先说结论'}, f"保存后清洗：{r['persona']}")
    a = PS.launch_args()
    check(a[0] == '--append-system-prompt' and '吐槽' in a[1] and '表情符号' in a[1] and '先说结论' in a[1] and '简短' not in a[1], '附加提示词内容')
    s = {}; PS.inject(s); check(s == {'outputStyle': 'Explanatory'}, '输出风格进 --settings')
    r = PS.api_persona({'set': {'style': 'default', 'tone': 'default', 'emoji': False, 'extra': ''}})
    check(r['persona'] == {} and 'persona' not in load_conf(), '全部恢复默认后配置里不留')
    s = {}; PS.inject(s); check(s == {}, '默认风格不注入')
    check(raises(lambda: PS.api_persona({'set': {'tone': 'xx'}}), '语气'), '非法语气被拒')
    check(raises(lambda: PS.api_persona({'set': {'style': 'a;b'}}), '风格'), '非法风格名被拒')
    check(raises(lambda: PS.api_persona({'set': {'extra': 'x' * 4001}}), '4000'), '补充说明超长被拒')
    os.makedirs(os.path.join(H, '.claude', 'output-styles'))
    open(os.path.join(H, '.claude', 'output-styles', 'mine.md'), 'w').write('---\nname: 我的风格\ndescription: 测试\n---\n内容')
    st = PS.api_persona({})['styles']
    check(st[-1] == {'id': '我的风格', 'label': '我的风格', 'desc': '测试', 'custom': True}, '列出自定义输出风格')

    # 全局指令
    r = PS.api_claude_md({})
    check(r['exists'] is False and r['text'] == '' and r['mtime'] is None, '没有 CLAUDE.md 时为空')
    r = PS.api_claude_md({'text': '# 规则\n用中文\n', 'mtime': None})
    check(r['exists'] and r['text'] == '# 规则\n用中文\n', '新建 CLAUDE.md')
    r2 = PS.api_claude_md({'text': '改', 'mtime': r['mtime'] - 10})
    check(r2.get('conflict') is True and open(os.path.join(H, '.claude', 'CLAUDE.md')).read() == '# 规则\n用中文\n', '修改时间不对报冲突、不写入')
    r3 = PS.api_claude_md({'text': '改好了', 'mtime': r['mtime']})
    check(r3['text'] == '改好了', '按修改时间保存')

    # 自定义命令
    r = PS.api_cmd_save({'cwd': PROJ, 'scope': 'user', 'name': 'review', 'desc': '审查: 代码', 'hint': '[文件]', 'body': '请审查 $ARGUMENTS'})
    fp = os.path.join(H, '.claude', 'commands', 'review.md')
    txt = open(fp).read()
    check(txt.startswith('---\ndescription: "审查: 代码"\nargument-hint: "[文件]"\n---\n\n请审查 $ARGUMENTS'), f'命令文件格式：{txt!r}')
    check(r['desc'] == '审查: 代码' and r['hint'] == '[文件]' and r['body'].startswith('请审查'), '读回字段一致')
    open(fp, 'w').write('---\ndescription: 旧\nmodel: haiku\n---\n旧内容\n')
    g = PS.api_cmd_get({'path': fp, 'cwd': PROJ})
    r = PS.api_cmd_save({'cwd': PROJ, 'scope': 'user', 'path': fp, 'mtime': g['mtime'], 'name': 'rv', 'desc': '新', 'hint': '', 'body': '新内容'})
    t2 = open(os.path.join(H, '.claude', 'commands', 'rv.md')).read()
    check(not os.path.exists(fp) and 'model: haiku' in t2 and 'description: 新' in t2 and 'argument-hint' not in t2, f'改名并保留其他前置项：{t2!r}')
    check(raises(lambda: PS.api_cmd_save({'cwd': PROJ, 'scope': 'user', 'name': '中文', 'body': 'x'}), '命令名'), '非法命令名被拒')
    check(raises(lambda: PS.api_cmd_save({'cwd': PROJ, 'scope': 'user', 'name': 'rv', 'body': 'x'}), '同名'), '同名被拒')
    check(raises(lambda: PS.api_cmd_save({'cwd': PROJ, 'scope': 'user', 'name': 'a', 'body': ' '}), '不能为空'), '空内容被拒')
    PS.api_cmd_save({'cwd': PROJ, 'scope': 'project', 'name': 'deploy', 'body': '部署'})
    L = PS.api_cmd_list({'cwd': PROJ})
    check([c['name'] for c in L['user']] == ['rv'] and [c['name'] for c in L['project']] == ['deploy'], '全局、项目命令分开列出')
    check(raises(lambda: PS.api_cmd_get({'path': os.path.join(tmp, 'x.md'), 'cwd': PROJ}), '找不到'), '范围外路径不读')
    check(raises(lambda: PS.api_cmd_save({'cwd': '', 'scope': 'project', 'name': 'x', 'body': 'x'}), '项目文件夹'), '没有项目时不能存项目命令')
    PS.api_cmd_delete({'path': L['project'][0]['path'], 'cwd': PROJ})
    check(PS.api_cmd_list({'cwd': PROJ})['project'] == [] and len(TR) == 1, '删除移到废纸篓')
    g = PS.api_cmd_get({'path': L['user'][0]['path'], 'cwd': PROJ})
    r = PS.api_cmd_save({'cwd': PROJ, 'scope': 'user', 'path': g['path'], 'mtime': g['mtime'] - 5, 'name': 'rv', 'body': 'x'})
    check(r.get('conflict') is True, '命令修改时间不对报冲突')

    # 技能
    r = PS.api_skill_save({'cwd': PROJ, 'scope': 'user', 'name': 'weekly-report', 'desc': '写周报时使用', 'body': '# 步骤\n1. 汇总'})
    sk = os.path.join(H, '.claude', 'skills', 'weekly-report')
    t = open(os.path.join(sk, 'SKILL.md')).read()
    check(t.startswith('---\nname: weekly-report\ndescription: 写周报时使用\n---\n\n# 步骤'), f'技能文件格式：{t!r}')
    open(os.path.join(sk, 'helper.py'), 'w').write('x')
    r = PS.api_skill_save({'cwd': PROJ, 'scope': 'user', 'dir': sk, 'mtime': r['mtime'], 'name': 'weekly', 'desc': '新说明', 'body': '新'})
    nk = os.path.join(H, '.claude', 'skills', 'weekly')
    check(not os.path.exists(sk) and os.path.exists(os.path.join(nk, 'helper.py')) and r['name'] == 'weekly' and r['desc'] == '新说明', '技能改名整夹移动、附件保留')
    check(raises(lambda: PS.api_skill_save({'cwd': PROJ, 'scope': 'user', 'name': 'Bad Name', 'desc': 'x', 'body': 'x'}), '技能名'), '非法技能名被拒')
    check(raises(lambda: PS.api_skill_save({'cwd': PROJ, 'scope': 'user', 'name': 'ok', 'desc': '', 'body': 'x'}), '说明'), '技能缺说明被拒')
    check(raises(lambda: PS.api_skill_get({'dir': os.path.join(tmp, 'zz'), 'cwd': PROJ}), '找不到'), '范围外技能不读')
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print('\n全部通过' if not FAIL else f'\n失败 {len(FAIL)} 项：' + '；'.join(FAIL))
sys.exit(1 if FAIL else 0)
