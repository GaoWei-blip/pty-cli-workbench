"""11-18 个性化：风格和语气、全局指令、自定义命令、技能的新建与编辑。

- 风格和语气：存在 config.json 的 persona 里，只作用于工作台启动的 CLI：
  输出风格 → --settings 里的 outputStyle（CLI 内置 Concise / Proactive / Explanatory / Learning，或 output-styles 文件夹里的自定义风格）；
  语气、排版开关、补充说明 → --append-system-prompt。都不写 ~/.claude/settings.json，只对新开或重启的对话生效。
  实测（2.1.325，假网关）：新建、续接都带上，请求头仍是 claude-cli/<版本> (external, cli)。
- 全局指令：~/.claude/CLAUDE.md，没有就新建；按修改时间判断冲突。
- 自定义命令：~/.claude/commands、<项目>/.claude/commands 下的 *.md（只管顶层），名称即 / 后面的命令名。
- 技能：~/.claude/skills、<项目>/.claude/skills 下的 <名称>/SKILL.md；删除沿用扩展页的 skill-delete。
server.py 里 PS.NS = globals()。
"""
import json
import os
import re

NS = {}

TONES = [
    ('default', '默认', ''),
    ('pro', '专业可靠', '语气专业、严谨，结论先行，用词准确，不夸张、不渲染。'),
    ('efficient', '高效务实', '直奔主题，少铺垫，只说完成任务需要的内容。'),
    ('friendly', '亲和友善', '语气温和友好，像耐心的同事一样把事情讲清楚。'),
    ('candid', '直言不讳', '有问题直接指出，不绕弯子、不说客套话；不同意时明确说出理由。'),
    ('roast', '吐槽达人', '可以带一点幽默和吐槽，但不能影响信息准确和任务完成。'),
]
TONE_TEXT = {k: t for k, _, t in TONES}
SWITCHES = [
    ('structure', '多用标题和列表', '内容较多时用小标题和列表组织，便于扫读。'),
    ('emoji', '允许表情符号', '可以适当使用表情符号。'),
    ('brief', '回答尽量简短', '回答尽量简短，能一句说清的不展开。'),
]
BUILTIN_STYLES = [('Concise', '简洁', '先给结果，省掉铺垫和过程叙述'), ('Proactive', '主动执行', '直接动手，少打断，先做再说'),
                  ('Explanatory', '讲解思路', '边做边解释实现选择和代码里的模式'), ('Learning', '边做边学', '留一些小段代码让您自己动手写')]
EXTRA_CAP = 4000
TEXT_CAP = 1 << 20
CMD_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
SKILL_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')


def home():
    return os.path.expanduser('~')


# ================================================================ 风格和语气
def load():
    p = NS['load_conf']().get('persona')
    return p if isinstance(p, dict) else {}


def clean(p):
    out = {}
    st = p.get('style')
    if isinstance(st, str) and st and st != 'default':
        out['style'] = st
    tone = p.get('tone')
    if tone in TONE_TEXT and tone != 'default':
        out['tone'] = tone
    for k, _, _ in SWITCHES:
        if p.get(k) is True:
            out[k] = True
    ex = p.get('extra')
    if isinstance(ex, str) and ex.strip():
        out['extra'] = ex.strip()[:EXTRA_CAP]
    return out


def prompt_text(p=None):
    """拼成附加到系统提示后面的一段话；什么都没设就是空串（不加参数）。"""
    p = clean(load() if p is None else p)
    lines = []
    if p.get('tone'):
        lines.append('- 语气：' + TONE_TEXT[p['tone']])
    for k, _, t in SWITCHES:
        if p.get(k):
            lines.append('- ' + t)
    if p.get('extra'):
        lines.append('- 补充说明：' + p['extra'])
    if not lines:
        return ''
    return '# 用户设置的回答风格\n以下是用户在 Claude 工作台里设置的回答风格偏好，与其他指令冲突时以用户当次的明确要求为准：\n' + '\n'.join(lines)


def launch_args():
    try:
        t = prompt_text()
    except Exception:
        return []
    return ['--append-system-prompt', t] if t else []


def inject(s):
    """build_settings 里调用：选了输出风格就放进 --settings。"""
    try:
        st = clean(load()).get('style')
    except Exception:
        return
    if st:
        s['outputStyle'] = st


def read_front(text):
    """拆出前置信息：返回 (有序的 [键, 原始行] 列表, 正文)。"""
    m = re.match(r'^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)', text, re.S)
    if not m:
        return [], text
    rows = []
    for line in m.group(1).split('\n'):
        mm = re.match(r'^([A-Za-z][\w-]*)\s*:', line)
        rows.append([mm.group(1) if mm else None, line])
    return rows, text[m.end():]


def front_val(rows, key):
    for k, line in rows:
        if k == key:
            v = line.split(':', 1)[1].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
                try:
                    return json.loads(v) if v[0] == '"' else v[1:-1]
                except Exception:
                    return v[1:-1]
            return v
    return ''


def yaml_str(v):
    v = ' '.join(str(v or '').split())
    if not v:
        return '""'
    if re.search(r'[:#\[\]{}&*!|>\'"%@`,]', v) or v[0] in '-?' or v.lower() in ('true', 'false', 'null', 'yes', 'no'):
        return json.dumps(v, ensure_ascii=False)
    return v


def build_text(rows, updates, body):
    """改前置信息里的几项（空值删掉），其余原样保留；前置信息为空就不写。"""
    rows = [list(r) for r in rows]
    for key, val in updates:
        idx = next((i for i, r in enumerate(rows) if r[0] == key), None)
        if val:
            line = f'{key}: {yaml_str(val)}'
            if idx is None:
                rows.append([key, line])
            else:
                rows[idx][1] = line
        elif idx is not None:
            rows.pop(idx)
    body = (body or '').replace('\r\n', '\n')
    if not body.endswith('\n'):
        body += '\n'
    if not any(r[1].strip() for r in rows):
        return body
    return '---\n' + '\n'.join(r[1] for r in rows) + '\n---\n\n' + body.lstrip('\n')


def styles(cwd):
    out = [{'id': 'default', 'label': '默认', 'desc': ''}]
    out += [{'id': k, 'label': l, 'desc': d} for k, l, d in BUILTIN_STYLES]
    dirs = [os.path.join(home(), '.claude', 'output-styles')]
    if cwd and os.path.isdir(cwd):
        dirs.insert(0, os.path.join(cwd, '.claude', 'output-styles'))
    seen = {x['id'] for x in out}
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith('.md'):
                continue
            try:
                with open(os.path.join(d, n), encoding='utf-8', errors='ignore') as f:
                    rows, _ = read_front(f.read(8000))
            except OSError:
                continue
            name = front_val(rows, 'name') or n[:-3]
            if name in seen:
                continue
            seen.add(name)
            out.append({'id': name, 'label': name, 'desc': front_val(rows, 'description'), 'custom': True})
    return out


def api_persona(b):
    st = b.get('set')
    if isinstance(st, dict):
        if 'extra' in st and st['extra'] is not None and not isinstance(st['extra'], str):
            raise ValueError('补充说明格式不对')
        if isinstance(st.get('extra'), str) and len(st['extra']) > EXTRA_CAP:
            raise ValueError(f'补充说明最多 {EXTRA_CAP} 字')
        if 'tone' in st and st['tone'] not in TONE_TEXT and st['tone'] not in (None, ''):
            raise ValueError('语气不合法')
        if 'style' in st and st['style'] not in (None, '') and (not isinstance(st['style'], str) or len(st['style']) > 80
                                                              or not re.match(r'^[\w .-]+$', st['style'])):
            raise ValueError('输出风格不合法')
        with NS['G']:
            c = NS['load_conf']()
            cur = c.get('persona') if isinstance(c.get('persona'), dict) else {}
            for k in ('style', 'tone', 'extra') + tuple(k for k, _, _ in SWITCHES):
                if k in st:
                    cur[k] = st[k]
            c['persona'] = clean(cur)
            if not c['persona']:
                c.pop('persona')
            NS['save_conf'](c)
    p = clean(load())
    return {'persona': p, 'styles': styles(b.get('cwd') or ''), 'tones': [{'id': k, 'label': l} for k, l, _ in TONES],
            'switches': [{'id': k, 'label': l} for k, l, _ in SWITCHES], 'preview': prompt_text(p)}


# ================================================================ 全局指令
def claude_md_path():
    return os.path.join(home(), '.claude', 'CLAUDE.md')


def write_text(p, text, mode=0o644):
    d = os.path.dirname(p)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, '.%s.cwb-save-%d.tmp' % (os.path.basename(p)[:80], os.getpid()))
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def read_small(p):
    if os.path.getsize(p) > TEXT_CAP:
        raise ValueError('文件超过 1MB，不能在这里编辑')
    with open(p, 'rb') as f:
        raw = f.read()
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('文件不是 UTF-8 文本，不能在这里编辑')


def same_mtime(p, mt):
    """mt 是读取时拿到的修改时间（秒或毫秒）；文件不存在时 mt 应为空。"""
    if not os.path.exists(p):
        return not mt
    if not isinstance(mt, (int, float)) or isinstance(mt, bool):
        return False
    want = mt / 1000.0 if mt > 1e11 else float(mt)
    return abs(os.stat(p).st_mtime - want) <= 0.0015


def api_claude_md(b):
    p = claude_md_path()
    real = os.path.realpath(p)
    if 'text' in b:
        text = b.get('text')
        if not isinstance(text, str):
            raise ValueError('内容格式不对')
        if len(text.encode('utf-8')) > TEXT_CAP:
            raise ValueError('内容超过 1MB')
        if not same_mtime(real, b.get('mtime')):
            return {'error': '全局指令在别处被改过，请重新打开后再保存', 'conflict': True}
        mode = os.stat(real).st_mode & 0o7777 if os.path.exists(real) else 0o644
        write_text(real, text, mode)
    exists = os.path.isfile(real)
    return {'path': p, 'exists': exists, 'text': read_small(real) if exists else '', 'mtime': os.stat(real).st_mtime if exists else None}


# ================================================================ 自定义命令
def cmd_roots(cwd):
    roots = {'user': os.path.join(home(), '.claude', 'commands')}
    if cwd and os.path.isabs(cwd) and os.path.isdir(cwd):
        roots['project'] = os.path.join(cwd, '.claude', 'commands')
    return roots


def lexical(p):
    return os.path.normpath(os.path.abspath(os.path.expanduser(p or '')))


def cmd_scope(path, cwd):
    p = lexical(path)
    for scope, root in cmd_roots(cwd).items():
        if os.path.dirname(p) == lexical(root) and p.endswith('.md'):
            return scope
    return None


def cmd_info(fp):
    try:
        with open(fp, encoding='utf-8', errors='ignore') as f:
            rows, body = read_front(f.read(8000))
    except OSError:
        return None
    return {'name': os.path.basename(fp)[:-3], 'path': fp, 'desc': front_val(rows, 'description'),
            'hint': front_val(rows, 'argument-hint'), 'mtime': os.stat(fp).st_mtime}


def api_cmd_list(b):
    cwd = b.get('cwd') or ''
    out = {'user': [], 'project': [], 'userRoot': cmd_roots(cwd)['user'], 'projectRoot': cmd_roots(cwd).get('project') or ''}
    for scope, root in cmd_roots(cwd).items():
        try:
            names = sorted(os.listdir(root), key=str.lower)
        except OSError:
            continue
        for n in names:
            fp = os.path.join(root, n)
            if n.endswith('.md') and os.path.isfile(fp):
                x = cmd_info(fp)
                if x:
                    out[scope].append(x)
    return out


def api_cmd_get(b):
    p, cwd = b.get('path') or '', b.get('cwd') or ''
    scope = cmd_scope(p, cwd)
    if not scope or not os.path.isfile(p):
        raise ValueError('找不到这个命令')
    rows, body = read_front(read_small(p))
    return {'scope': scope, 'name': os.path.basename(p)[:-3], 'path': p, 'desc': front_val(rows, 'description'),
            'hint': front_val(rows, 'argument-hint'), 'body': body.lstrip('\n'), 'mtime': os.stat(p).st_mtime}


def api_cmd_save(b):
    cwd, scope = b.get('cwd') or '', b.get('scope')
    roots = cmd_roots(cwd)
    if scope not in roots:
        raise ValueError('项目文件夹不存在' if scope == 'project' else '保存位置不对')
    name = str(b.get('name') or '').strip().lstrip('/')
    if not CMD_RE.match(name) or name.endswith('.md'):
        raise ValueError('命令名只能用英文字母、数字、短横线、下划线和点，最长 64 个字符')
    body = b.get('body')
    if not isinstance(body, str) or not body.strip():
        raise ValueError('命令内容不能为空')
    if len(body.encode('utf-8')) > TEXT_CAP:
        raise ValueError('内容超过 1MB')
    dest = os.path.join(roots[scope], name + '.md')
    orig = b.get('path') or ''
    rows = []
    if orig:
        if not cmd_scope(orig, cwd) or not os.path.isfile(orig):
            raise ValueError('原来的命令已经不在了，请刷新后再试')
        if not same_mtime(orig, b.get('mtime')):
            return {'error': '这个命令在别处被改过，请重新打开后再保存', 'conflict': True}
        rows, _ = read_front(read_small(orig))
    if lexical(dest) != lexical(orig) and os.path.exists(dest):
        raise ValueError(f'已经有同名命令 /{name}')
    text = build_text(rows, [('description', b.get('desc') or ''), ('argument-hint', b.get('hint') or '')], body)
    mode = os.stat(orig).st_mode & 0o7777 if orig else 0o644
    write_text(dest, text, mode)
    if orig and lexical(dest) != lexical(orig):
        os.remove(orig)
    return dict(api_cmd_get({'path': dest, 'cwd': cwd}), ok=True)


def api_cmd_delete(b):
    p, cwd = b.get('path') or '', b.get('cwd') or ''
    if not cmd_scope(p, cwd) or not os.path.isfile(p):
        raise ValueError('找不到这个命令')
    if not NS['trash']([lexical(p)]):
        raise ValueError('移到废纸篓失败')
    return {'ok': True}


# ================================================================ 技能
def skill_roots(cwd):
    roots = {'user': os.path.join(home(), '.claude', 'skills')}
    if cwd and os.path.isabs(cwd) and os.path.isdir(cwd):
        roots['project'] = os.path.join(cwd, '.claude', 'skills')
    return roots


def skill_scope(d, cwd):
    d = lexical(d)
    for scope, root in skill_roots(cwd).items():
        if os.path.dirname(d) == lexical(root):
            return scope
    return None


def api_skill_get(b):
    d, cwd = b.get('dir') or '', b.get('cwd') or ''
    scope = skill_scope(d, cwd)
    fp = os.path.join(d, 'SKILL.md')
    if not scope or not os.path.isfile(fp):
        raise ValueError('找不到这个技能')
    rows, body = read_front(read_small(fp))
    return {'scope': scope, 'dir': d, 'name': front_val(rows, 'name') or os.path.basename(d), 'desc': front_val(rows, 'description'),
            'body': body.lstrip('\n'), 'mtime': os.stat(fp).st_mtime}


def api_skill_save(b):
    cwd, scope = b.get('cwd') or '', b.get('scope')
    roots = skill_roots(cwd)
    if scope not in roots:
        raise ValueError('项目文件夹不存在' if scope == 'project' else '保存位置不对')
    name = str(b.get('name') or '').strip()
    if not SKILL_RE.match(name):
        raise ValueError('技能名只能用小写英文字母、数字和短横线，最长 64 个字符')
    desc = ' '.join(str(b.get('desc') or '').split())
    if not desc:
        raise ValueError('请写一句说明：Claude 靠它判断什么时候用这个技能')
    if len(desc) > 1024:
        raise ValueError('说明最多 1024 个字符')
    body = b.get('body')
    if not isinstance(body, str) or not body.strip():
        raise ValueError('技能内容不能为空')
    if len(body.encode('utf-8')) > TEXT_CAP:
        raise ValueError('内容超过 1MB')
    dest = os.path.join(roots[scope], name)
    orig = b.get('dir') or ''
    rows = []
    if orig:
        fp = os.path.join(orig, 'SKILL.md')
        if not skill_scope(orig, cwd) or not os.path.isfile(fp):
            raise ValueError('原来的技能已经不在了，请刷新后再试')
        if not same_mtime(fp, b.get('mtime')):
            return {'error': '这个技能在别处被改过，请重新打开后再保存', 'conflict': True}
        rows, _ = read_front(read_small(fp))
    if lexical(dest) != lexical(orig) and os.path.exists(dest):
        raise ValueError(f'已经有同名技能 {name}')
    if orig and lexical(dest) != lexical(orig):
        os.rename(orig, dest)  # 改名：整个文件夹跟着改，附带的脚本、资料一起保留
    text = build_text(rows, [('name', name), ('description', desc)], body)
    fp = os.path.join(dest, 'SKILL.md')
    mode = os.stat(fp).st_mode & 0o7777 if os.path.exists(fp) else 0o644
    write_text(fp, text, mode)
    return dict(api_skill_get({'dir': dest, 'cwd': cwd}), ok=True)
