"""第十五轮 A1：上下文用量（按对话注入的状态行）、上下文明细、记忆与指令文件、保存文本文件、用别的应用打开、标记未读。

上下文用量：偏好 prefs.ctxStatus（默认开）打开时，--settings 里注入 statusLine，命令是 statusline.sh（极简 POSIX shell）：
它把 CLI 给的 JSON 原子写到 .run/ctx/<启动代次>.json，再把同一份内容交给用户全局设置里原来的状态行命令并原样输出。
原来的命令只从 ~/.claude/settings.json 读（测试可用环境变量 CWB_USER_SETTINGS 换成别的文件），绝不写它。
后台线程每 0.5 秒看一眼在跑的对话的状态文件，变了就更新缓存并推 {type: 'ctx', sid, ctx}；运行状态里的 ctx 取缓存。
出错只影响本功能：读不到、解析不了就当没有（ctx 为 null），线程里的异常全部吞掉并记日志。
"""
import json
import os
import re
import shlex
import subprocess
import threading
import time

NS = {}
ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(ROOT, 'statusline.sh')
GEN_RE = re.compile(r'^[\w-]{1,80}$')
CACHE = {}  # 启动代次 → {key: (mtime_ns, size), ctx}
PREF = {'t': 0.0, 'on': True}
STARTED = [False]
DETAIL_LOCK = threading.Lock()
TEXT_EXT = ('.md', '.markdown', '.mdx', '.txt')
SAVE_CAP = 1024 * 1024


def log(*a):
    f = NS.get('log')
    if f:
        f(*a)


def ctx_dir():
    return os.path.join(NS.get('RUN_DIR') or os.path.join(ROOT, '.run'), 'ctx')


def user_settings_path():
    return os.environ.get('CWB_USER_SETTINGS') or os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')


def user_statusline():
    """用户全局设置里原来的 statusLine（只读）；没有或读不了返回 {}。"""
    try:
        with open(user_settings_path(), encoding='utf-8') as f:
            d = json.load(f)
        sl = d.get('statusLine') if isinstance(d, dict) else None
        return sl if isinstance(sl, dict) else {}
    except Exception:
        return {}


def enabled():
    """偏好“上下文用量”（prefs.ctxStatus，默认开），缓存 2 秒。"""
    now = time.time()
    if now - PREF['t'] > 2:
        try:
            p = (NS['load_conf']().get('prefs') or {}) if NS.get('load_conf') else {}
            PREF['on'] = p.get('ctxStatus') is not False
        except Exception:
            PREF['on'] = True
        PREF['t'] = now
    return PREF['on']


def statusline_setting(orig=None):
    """要注入的 statusLine：沿用用户原来的 padding 等显示选项，命令换成 statusline.sh（把原命令作为参数带上）。"""
    orig = user_statusline() if orig is None else orig
    cmd = orig.get('command') if orig.get('type', 'command') == 'command' and isinstance(orig.get('command'), str) else ''
    sl = {k: orig[k] for k in ('padding', 'refreshInterval', 'hideVimModeIndicator') if k in orig}
    sl.update(type='command', command='/bin/sh %s %s %s' % (shlex.quote(SCRIPT), shlex.quote(ctx_dir()), shlex.quote(cmd.strip())))
    return sl


def inject(s):
    """build_settings 里调用：偏好开着就往 --settings 里加 statusLine。任何异常都不影响启动。"""
    try:
        if not enabled() or not os.path.isfile(SCRIPT):
            return s
        os.makedirs(ctx_dir(), exist_ok=True)
        s['statusLine'] = statusline_setting()
    except Exception as e:
        log('注入状态行失败', repr(e))
    return s


def to_ms(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v if v > 1e11 else v * 1000)
    if isinstance(v, str) and v:
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp() * 1000)
        except ValueError:
            return None
    return None


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse(raw, at):
    """状态行 JSON → {pct, used, window, cacheWarm, cacheExpiresAt, cost, at}；拿不到的给 None。"""
    d = json.loads(raw)
    if not isinstance(d, dict):
        return None
    cw = d.get('context_window') if isinstance(d.get('context_window'), dict) else {}
    cu = cw.get('current_usage')
    used = None
    if isinstance(cu, dict):
        used = sum(int(num(cu.get(k)) or 0) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
    window = num(cw.get('context_window_size'))
    pct = num(cw.get('used_percentage'))
    if pct is None and used is not None and window:
        pct = round(used * 100 / window)
    pc = d.get('prompt_cache') if isinstance(d.get('prompt_cache'), dict) else {}
    warm = pc.get('warm') if isinstance(pc.get('warm'), bool) else None
    cost = num((d.get('cost') or {}).get('total_cost_usd')) if isinstance(d.get('cost'), dict) else None
    # 修复线：缓存三种状态。warm 有效（带到期时间 cacheExpiresAt）；expired 观察到过缓存、现在已过期；
    # unknown 从没观察到缓存（caching_observed 为假或缺失）或拿不到，这时不能说“已过期”，cacheWarm 也给 null
    cache = 'warm' if warm is True else 'expired' if warm is False and pc.get('caching_observed') is True else 'unknown'
    return {'pct': pct, 'used': used, 'window': window, 'cacheWarm': warm if cache != 'unknown' else None,
            'cacheExpiresAt': to_ms(pc.get('expires_at')) if cache == 'warm' else None, 'cache': cache, 'cost': cost, 'at': int(at)}


def read_ctx(gen):
    """读一次状态文件（不走缓存）。"""
    p = os.path.join(ctx_dir(), gen + '.json')
    st = os.stat(p)
    with open(p, encoding='utf-8') as f:
        return parse(f.read(), st.st_mtime_ns // 1_000_000)


def ctx_of(r):
    """Runner.public_state() 里调用：偏好关了或还没有数据时为 None。"""
    try:
        if not enabled():
            return None
        c = CACHE.get(getattr(r, 'gen', '') or '')
        return c['ctx'] if c else None
    except Exception:
        return None


def scan():
    on = enabled()
    d = ctx_dir()
    for r in list((NS.get('RUNNERS') or {}).values()):
        gen = getattr(r, 'gen', '') or ''
        if not GEN_RE.match(gen):
            continue
        p = os.path.join(d, gen + '.json')
        try:
            st = os.stat(p)
        except OSError:
            continue
        key = (st.st_mtime_ns, st.st_size)
        old = CACHE.get(gen)
        if old and old['key'] == key:
            continue
        try:
            with open(p, encoding='utf-8') as f:
                ctx = parse(f.read(), st.st_mtime_ns // 1_000_000)
        except Exception:
            ctx = old['ctx'] if old else None  # 半截或坏的文件：保留上一次的
        CACHE[gen] = {'key': key, 'ctx': ctx}
        if on and ctx and (not old or old['ctx'] != ctx) and NS.get('BUS'):
            NS['BUS'].emit({'type': 'ctx', 'sid': r.sid, 'ctx': ctx})


def cleanup():
    """不在跑的对话的状态文件放一天后删掉；残留的临时文件放一小时后删掉。"""
    d = ctx_dir()
    live = {getattr(r, 'gen', '') for r in list((NS.get('RUNNERS') or {}).values())}
    now = time.time()
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        p = os.path.join(d, n)
        try:
            age = now - os.path.getmtime(p)
            if n.endswith('.tmp') and n.startswith('.') and age > 3600:
                os.remove(p)
            elif n.endswith('.json') and n[:-5] not in live and age > 86400:
                os.remove(p)
                CACHE.pop(n[:-5], None)
        except OSError:
            pass


def loop():
    k = 0
    while True:
        time.sleep(0.5)
        k += 1
        try:
            scan()
            if k % 600 == 1:
                cleanup()
        except Exception as e:
            if k % 120 == 1:
                log('读取上下文状态异常', repr(e))


def start():
    if STARTED[0]:
        return
    STARTED[0] = True
    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- 上下文明细（/context）
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07')
OUT_RE = re.compile(r'<local-command-stdout>(.*?)</local-command-stdout>', re.S)


def find_detail(path, off):
    """记录文件 off 之后 /context 的输出：优先 Markdown 版（isMeta 的 user 记录），其次终端版去掉颜色。返回 (md, 终端版)。"""
    md = term = None
    try:
        with open(path, 'rb') as f:
            f.seek(off)
            data = f.read()
    except OSError:
        return None, None
    for line in data.split(b'\n'):
        if b'Context Usage' not in line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('type') == 'user':
            c = (r.get('message') or {}).get('content')
            t = c if isinstance(c, str) else '\n'.join(b.get('text') or '' for b in c or [] if isinstance(b, dict))
            m = OUT_RE.search(t)
            t = ANSI.sub('', m.group(1) if m else t).strip()
            if '## Context Usage' in t:
                md = t
                break
            term = term or t
        elif r.get('type') == 'system' and isinstance(r.get('content'), str):
            m = OUT_RE.search(r['content'])
            if m:
                term = term or ANSI.sub('', m.group(1)).strip()
    return md, term


def api_context_detail(b):
    sid = NS['resolve_sid'](b.get('sid') or '')
    r = (NS.get('RUNNERS') or {}).get(sid)
    if not r or not r.state.get('alive') or getattr(r, 'prewarm', False):
        raise ValueError('这个对话现在没有在运行，发一条消息后再查看明细')
    if not DETAIL_LOCK.acquire(blocking=False):
        raise ValueError('正在读取明细，请稍候')
    try:
        st = r.state
        if not st.get('ready') or st.get('dialog'):
            raise ValueError('终端现在不在输入框状态，稍后再查看')
        if st.get('busy') or r.turn or r.attention() or r.queue or r.sending:
            raise ValueError('对话正在回复或在等你确认，等它空闲后再查看')
        if NS['input_box_text'](r.screen_lines()).strip():
            raise ValueError('终端输入框里还有没发出的文字，先发出或清空再查看')
        path = NS['find_transcript'](sid)
        try:
            off = os.path.getsize(path) if path else 0
        except OSError:
            off = 0
        r.send('/context')
        end, term = time.time() + 40, None
        while time.time() < end:
            time.sleep(0.4)
            path = path or NS['find_transcript'](sid)
            if not path:
                continue
            md, t = find_detail(path, off)
            if md:
                return {'md': md}
            if t and not term:
                term, end = t, min(end, time.time() + 3)  # 终端版到了，Markdown 版一般紧跟着写
        if term:
            return {'md': term}
        raise ValueError('没等到明细，请稍后再试')
    finally:
        DETAIL_LOCK.release()


# ---------------------------------------------------------------- 记忆与指令文件
def _sanitize(p):
    return re.sub(r'[^A-Za-z0-9]', '-', p)


def _finfo(p, name):
    st = os.stat(p)
    return {'path': p, 'name': name, 'mtime': st.st_mtime, 'size': st.st_size}


def _short(p):
    home = os.path.expanduser('~')
    return '~' + p[len(home):] if p.startswith(home + '/') else p


def _md_in(d, limit=200):
    out = []
    try:
        for root, dirs, files in os.walk(d):
            dirs[:] = sorted(x for x in dirs if not x.startswith('.'))
            for n in sorted(files):
                if n.lower().endswith('.md'):
                    out.append(os.path.join(root, n))
                    if len(out) >= limit:
                        return out
    except OSError:
        pass
    return out


def memory_files(cwd, home=None, projects_dir=None, settings=None):
    home = home or os.path.expanduser('~')
    cc = os.path.join(home, '.claude')
    projects_dir = projects_dir or os.path.join(cc, 'projects')
    groups = []
    # 全局指令
    files = []
    p = os.path.join(cc, 'CLAUDE.md')
    if os.path.isfile(p):
        files.append(_finfo(p, 'CLAUDE.md'))
    for x in _md_in(os.path.join(cc, 'rules')):
        files.append(_finfo(x, os.path.relpath(x, cc)))
    groups.append({'key': 'user', 'label': '全局指令', 'files': files})
    # 项目指令：从对话文件夹往上到根目录（CLI 会逐级读取），本文件夹另读 .claude/rules
    files, seen = [], set()
    if cwd and os.path.isdir(cwd):
        real = os.path.realpath(cwd)
        d = real
        while True:
            names = ['CLAUDE.md', 'CLAUDE.local.md', os.path.join('.claude', 'CLAUDE.md')]
            for n in names:
                x = os.path.join(d, n)
                if os.path.isfile(x) and os.path.realpath(x) not in seen and x != os.path.join(cc, 'CLAUDE.md'):
                    seen.add(os.path.realpath(x))
                    files.append(_finfo(x, os.path.relpath(x, real) if (x + '/').startswith(real + '/') else _short(x)))
            if d == real:
                for x in _md_in(os.path.join(d, '.claude', 'rules')):
                    files.append(_finfo(x, os.path.relpath(x, real)))
            up = os.path.dirname(d)
            if up == d:
                break
            d = up
    groups.append({'key': 'project', 'label': '项目指令', 'files': files})
    # 自动记忆：~/.claude/projects/<文件夹路径换成横线>/memory/，设置里 autoMemoryDirectory 优先
    files, dirs = [], []
    st = settings if settings is not None else {}
    amd = st.get('autoMemoryDirectory') if isinstance(st, dict) else None
    if isinstance(amd, str) and amd.strip():
        dirs.append(os.path.expanduser(amd.strip()))
    if cwd:
        cands = [os.path.realpath(cwd), cwd.rstrip('/') or '/']
        top = NS['git_top'](cwd) if NS.get('git_top') and os.path.isdir(cwd) else None
        if top:
            cands.append(os.path.realpath(top))
        for c in cands:
            dirs.append(os.path.join(projects_dir, _sanitize(c), 'memory'))
    seen = set()
    for d in dirs:
        rd = os.path.realpath(d)
        if rd in seen or not os.path.isdir(d):
            continue
        seen.add(rd)
        xs = _md_in(d)
        xs.sort(key=lambda x: (os.path.basename(x) != 'MEMORY.md', x.lower()))
        for x in xs:
            files.append(_finfo(x, os.path.relpath(x, d)))
    groups.append({'key': 'auto', 'label': '自动记忆', 'files': files})
    return {'groups': groups}


def api_memory_files(b):
    cwd = b.get('cwd') or ''
    if cwd and not os.path.isabs(cwd):
        raise ValueError('文件夹需要是完整路径')
    st = {}
    try:
        with open(user_settings_path(), encoding='utf-8') as f:
            st = json.load(f)
    except Exception:
        pass
    return memory_files(cwd, settings=st)


def api_file_save(b):
    """保存已存在的 Markdown / 纯文本文件（1MB 以内）；mtime 与磁盘不一致时报冲突。符号链接写到它指向的文件。"""
    p, text, mt = b.get('path'), b.get('text'), b.get('mtime')
    if not isinstance(p, str) or not os.path.isabs(p):
        raise ValueError('文件路径不对')
    if not isinstance(text, str):
        raise ValueError('内容为空')
    if not isinstance(mt, (int, float)) or isinstance(mt, bool):
        raise ValueError('缺少文件的修改时间，请重新打开后再保存')
    real = os.path.realpath(p)
    if os.path.splitext(real)[1].lower() not in TEXT_EXT or os.path.splitext(p)[1].lower() not in TEXT_EXT:
        raise ValueError('只能保存 Markdown 或纯文本文件')
    if not os.path.isfile(real):
        raise ValueError('文件不存在')
    st = os.stat(real)
    data = text.encode('utf-8')
    if st.st_size > SAVE_CAP or len(data) > SAVE_CAP:
        raise ValueError('文件超过 1MB，不能在这里编辑')
    want = mt / 1000.0 if mt > 1e11 else float(mt)
    if abs(st.st_mtime - want) > 0.0015:
        return {'error': '文件在别处被改过，请重新打开后再保存', 'conflict': True, 'mtime': st.st_mtime}
    with open(real, 'rb') as f:
        old = f.read()
    try:
        old.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('文件不是 UTF-8 文本，不能在这里编辑')
    if b'\0' in old[:8000]:
        raise ValueError('不是文本文件')
    d = os.path.dirname(real)
    tmp = os.path.join(d, '.%s.cwb-save-%d.tmp' % (os.path.basename(real)[:80], os.getpid()))
    try:
        with open(tmp, 'wb') as f:
            f.write(data)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, real)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return {'ok': True, 'mtime': os.stat(real).st_mtime}


# ---------------------------------------------------------------- 用别的应用打开
APPS = [('finder', '访达', None), ('vscode', 'VS Code', 'Visual Studio Code'), ('cursor', 'Cursor', 'Cursor'), ('terminal', '终端', 'Terminal')]
APP_DIRS = ('/Applications', os.path.expanduser('~/Applications'), '/System/Applications', '/System/Applications/Utilities',
            '/Applications/Utilities')


def app_installed(name):
    return name is None or any(os.path.isdir(os.path.join(d, name + '.app')) for d in APP_DIRS)


def api_open_apps(b):
    return {'apps': [{'key': k, 'name': n} for k, n, a in APPS if app_installed(a)]}


def api_open_with(b):
    cwd, key = b.get('cwd') or '', b.get('app') or ''
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.exists(cwd):
        raise ValueError('文件夹不存在')
    ent = next((x for x in APPS if x[0] == key), None)
    if not ent or not app_installed(ent[2]):
        raise ValueError('这台电脑上没有这个应用')
    args = ['open', cwd] if ent[2] is None else ['open', '-a', ent[2], cwd]
    p = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        raise ValueError('打开失败：' + (p.stderr or '').strip()[:200])
    return {'ok': True}


# ---------------------------------------------------------------- 标记未读
def api_mark_unread(b):
    sid = NS['resolve_sid'](b.get('sid') or '')
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', sid or ''):
        raise ValueError('会话 ID 不对')
    NS['mark_done'](sid, time.time())  # 沿用未读机制：结束时间晚于看过的时间即未读
    NS['BUS'].emit({'type': 'sessions'})
    return {'ok': True}
