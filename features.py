"""第十一轮 B：全文搜索、网页预览的端口、MCP 管理、技能管理与技能市场。

server.py 在 API 字典之后执行 features.register(globals())。这里用到的 server.py 函数和变量都从 NS 里按名字取；
LOGIN_ENV、CLAUDE 在 server.main() 里才赋值，所以一律在调用时再取。
从网上下载的技能只当数据保存和展示，安装过程中不执行其中任何脚本。
"""
import base64
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import threading
import time
import zipfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

NS = {}
DEVNULL = subprocess.DEVNULL


def register(ns):
    global NS
    NS = ns
    ns['API'].update({
        'grep': api_grep, 'ports': api_ports, 'port-kill': api_port_kill,
        'mcp-list': api_mcp_list, 'mcp-add': api_mcp_add, 'mcp-remove': api_mcp_remove, 'mcp-test': api_mcp_test,
        'skills': api_skills, 'skill-read': api_skill_read, 'skill-delete': api_skill_delete, 'skill-pick': api_skill_pick,
        'skill-scan': api_skill_scan, 'skill-preview': api_skill_preview, 'skill-install': api_skill_install,
        'market': api_market,
        'session-path': api_session_path,
    })


def api_session_path(b):
    """对话记录文件的完整路径，供“复制记录文件路径”用（在新对话里引用之前的会话）。"""
    p = NS['find_transcript'](b.get('sid') or '')
    if not p:
        raise ValueError('这个对话还没有记录文件')
    return {'path': p}


def home():
    return NS.get('HOME') or os.path.expanduser('~')


def login_env():
    return dict(NS.get('LOGIN_ENV') or {}) or dict(os.environ)


def sh(args, timeout=8, cwd=None):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=DEVNULL, cwd=cwd).stdout
    except Exception:
        return ''


def in_dir(p, root):
    try:
        rp, rr = os.path.realpath(p), os.path.realpath(root)
    except Exception:
        return False
    return rp == rr or rp.startswith(rr.rstrip('/') + '/')


# ================================================================ 全文搜索
GREP_MAX = 500
GREP_TIMEOUT = 12
GREP_SKIP = ('.git', 'node_modules', '.venv', 'dist', 'build')
MAX_FILE = 2 * 1024 * 1024
_RG = {'path': None, 'at': 0.0}


def find_rg():
    """找真正的 rg 可执行文件（shell 里的 rg 可能只是函数，不算）。"""
    if time.time() - _RG['at'] < 300:
        return _RG['path']
    cands = [os.environ.get('CWB_RG') or '']  # 仅测试用
    cands.append(shutil.which('rg', path=login_env().get('PATH') or os.defpath) or '')
    cands += ['/opt/homebrew/bin/rg', '/usr/local/bin/rg', os.path.join(home(), '.cargo/bin/rg'), '/usr/bin/rg']
    found = None
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK) and os.path.basename(os.path.realpath(c)).startswith('rg'):
            found = c
            break
    _RG.update(path=found, at=time.time())
    return found


def crop(text, ranges, width=240):
    text = text.rstrip('\r\n')
    if len(text) <= width:
        return text, ranges
    s0 = ranges[0][0] if ranges else 0
    start = max(0, s0 - 60)
    end = min(len(text), start + width)
    out = ('…' if start else '') + text[start:end] + ('…' if end < len(text) else '')
    shift = start - (1 if start else 0)
    rr = [[max(0, a - shift), min(len(out), b - shift)] for a, b in ranges if b > start and a < end]
    return out, rr


class Hits:
    def __init__(self, cwd):
        self.cwd, self.files, self.n, self.closed, self.lock = cwd, {}, 0, False, threading.Lock()

    def add(self, rel, line, text, ranges):
        with self.lock:
            if self.closed or self.n >= GREP_MAX:
                return False
            f = self.files.get(rel)
            if not f:
                f = self.files[rel] = {'rel': rel, 'path': os.path.join(self.cwd, rel), 'hits': []}
            t, r = crop(text, ranges)
            f['hits'].append({'n': line, 't': t, 'r': r})
            self.n += 1
            return self.n < GREP_MAX

    def close(self):
        with self.lock:
            self.closed = True
            return [dict(f, hits=list(f['hits'])) for f in self.files.values()], self.n


def py_ranges(rx, line):
    return [[m.start(), m.end()] for m in rx.finditer(line) if m.end() > m.start()][:20]


def run_stream(args, cwd, deadline, on_line):
    """逐行读子进程输出；到时间就结束进程。返回 (是否超时, 返回码, 标准错误)。"""
    p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=DEVNULL)
    flag = {'timeout': False}

    def kill():
        flag['timeout'] = True
        try:
            p.kill()
        except Exception:
            pass
    t = threading.Timer(max(0.5, deadline - time.time()), kill)
    t.start()
    try:
        for raw in p.stdout:
            if on_line(raw) is False:
                p.kill()
                break
    finally:
        t.cancel()
        err = b''
        try:
            err = p.stderr.read(4000)
        except Exception:
            pass
        p.wait()
    return flag['timeout'], p.returncode, err.decode('utf-8', 'replace')


def grep_rg(rg, cwd, q, case, regex, word, globs, deadline, hits):
    args = [rg, '--json', '--no-config', '--no-messages', '--max-filesize', '2M', '--max-count', '200', '-s' if case else '-i']
    if not regex:
        args.append('-F')
    if word:
        args.append('-w')
    for d in GREP_SKIP:
        args += ['-g', '!' + d]
    for g in globs:
        args += ['-g', g]
    args += ['-e', q, '--', '.']

    def on_line(raw):
        try:
            ev = json.loads(raw)
        except Exception:
            return True
        if ev.get('type') != 'match':
            return True
        d = ev.get('data') or {}
        path = (d.get('path') or {}).get('text')
        if path is None:
            return True
        lines = d.get('lines') or {}
        if 'text' in lines:
            lt = lines['text']
            lb = lt.encode('utf-8', 'surrogatepass')
        else:
            lb = base64.b64decode(lines.get('bytes') or '')
            lt = lb.decode('utf-8', 'replace')
        rr = [[len(lb[:m['start']].decode('utf-8', 'replace')), len(lb[:m['end']].decode('utf-8', 'replace'))]
              for m in (d.get('submatches') or [])[:20]]
        rel = path[2:] if path.startswith('./') else path
        return hits.add(rel, d.get('line_number') or 0, lt, rr)
    timed, rc, err = run_stream(args, cwd, deadline, on_line)
    if rc == 2 and not hits.n and not timed and err.strip():
        raise ValueError('搜索出错：' + err.strip().split('\n')[-1][:200])
    return timed


def grep_git(cwd, q, case, regex, word, globs, rx, deadline, hits, rmode='-P'):
    args = ['git', '-C', cwd, 'grep', '-n', '-I', '-z', '--no-color', '--untracked']
    if not case:
        args.append('-i')
    args.append(rmode if regex else '-F')  # 正则优先用 PCRE（和 Python 高亮的写法一致），git 没编译 PCRE 时退回 -E
    if word:
        args.append('-w')
    args += ['-e', q, '--']
    pos = []
    for g in globs:
        neg = g.startswith('!')
        g = g[1:] if neg else g
        if not g:
            continue
        pat = g if '/' in g else '**/' + g
        (args.append(':(exclude,glob)' + pat) if neg else pos.append(':(glob)' + pat))
    args += pos or ['.']
    for d in GREP_SKIP:
        args.append(':(exclude,glob)**/%s/**' % d)

    big = {}

    def on_line(raw):
        parts = raw.rstrip(b'\n').split(b'\0', 2)
        if len(parts) < 3:
            return True
        rel = parts[0].decode('utf-8', 'replace')
        if rel not in big:
            try:
                big[rel] = os.path.getsize(os.path.join(cwd, rel)) > MAX_FILE
            except OSError:
                big[rel] = False
        if big[rel]:
            return True
        try:
            n = int(parts[1])
        except ValueError:
            return True
        text = parts[2].decode('utf-8', 'replace')
        return hits.add(rel, n, text, py_ranges(rx, text) if rx else [])
    timed, rc, err = run_stream(args, cwd, deadline, on_line)
    if rc not in (0, 1) and not hits.n and not timed:
        if regex and rmode == '-P' and re.search(r'perl|pcre', err, re.I):
            return grep_git(cwd, q, case, regex, word, globs, rx, deadline, hits, rmode='-E')
        raise ValueError('搜索出错：' + (err.strip().split('\n')[-1][:200] or str(rc)))
    return timed


def grep_py(cwd, globs, rx, deadline, hits):
    inc = [g for g in globs if not g.startswith('!')]
    exc = [g[1:] for g in globs if g.startswith('!') and len(g) > 1]
    skip = set(NS.get('SKIP_DIRS') or ()) | set(GREP_SKIP)
    for root, dirs, files in os.walk(cwd):
        if time.time() > deadline or hits.closed:
            return True
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith('.') and not any(fnmatch.fnmatch(d, e) for e in exc))
        for n in sorted(files):
            if time.time() > deadline or hits.closed:
                return True
            if n.startswith('.'):
                continue
            fp = os.path.join(root, n)
            rel = os.path.relpath(fp, cwd)
            if inc and not any(fnmatch.fnmatch(n, g) or fnmatch.fnmatch(rel, g) for g in inc):
                continue
            if exc and any(fnmatch.fnmatch(n, g) or fnmatch.fnmatch(rel, g) for g in exc):
                continue
            try:
                st = os.lstat(fp)
                if not stat.S_ISREG(st.st_mode) or not st.st_size or st.st_size > MAX_FILE:
                    continue
                with open(fp, 'rb') as f:
                    data = f.read()
            except OSError:
                continue
            if b'\0' in data[:8000]:
                continue
            text = data.decode('utf-8', 'replace')
            if not rx.search(text):
                continue
            for i, line in enumerate(text.split('\n'), 1):
                if rx.search(line) and not hits.add(rel, i, line, py_ranges(rx, line)):
                    return False
    return False


def in_git(cwd):
    return sh(['git', '-C', cwd, 'rev-parse', '--is-inside-work-tree'], timeout=5).strip() == 'true'


def api_grep(b):
    cwd = b.get('cwd') or ''
    if not os.path.isdir(cwd):
        raise ValueError('先打开一个项目')
    q = b.get('q') or ''
    if not q.strip():
        raise ValueError('请输入要搜索的内容')
    if len(q) > 500 or '\n' in q:
        raise ValueError('搜索内容太长或包含换行')
    case, regex, word = bool(b.get('case')), bool(b.get('regex')), bool(b.get('word'))
    globs = [g for g in re.split(r'[,\s]+', b.get('glob') or '') if g][:12]
    src = q if regex else re.escape(q)
    if word:
        src = r'\b(?:%s)\b' % src
    rx = None
    try:
        rx = re.compile(src, 0 if case else re.I)
    except re.error as e:
        if not find_rg():
            raise ValueError('正则写法有误：%s' % e)
    t0 = time.time()
    deadline = t0 + GREP_TIMEOUT
    hits = Hits(cwd)
    rg = find_rg()
    if rg:
        engine, timed = 'rg', grep_rg(rg, cwd, q, case, regex, word, globs, deadline, hits)
    elif in_git(cwd):
        engine, timed = 'git', grep_git(cwd, q, case, regex, word, globs, rx, deadline, hits)
    else:
        # 遍历放在后台线程：个别目录读得很慢（或在等系统授权）时，到时间先把已找到的结果返回
        engine, box = 'python', {}
        th = threading.Thread(target=lambda: box.update(t=grep_py(cwd, globs, rx, deadline, hits)), daemon=True)
        th.start()
        th.join(max(0.1, deadline - time.time()) + 0.3)
        timed = box.get('t', True)
    files, n = hits.close()
    files.sort(key=lambda f: (f['rel'].count('/'), f['rel'].lower()))
    for f in files:
        f['hits'].sort(key=lambda h: h['n'])
    return {'engine': engine, 'files': files, 'total': n, 'full': n >= GREP_MAX, 'timeout': bool(timed),
            'ms': int((time.time() - t0) * 1000)}


# ================================================================ 网页预览：本项目的监听端口
def ppid_map():
    m = {}
    for l in sh(['ps', '-axo', 'pid=,ppid=']).splitlines():
        p = l.split()
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            m[int(p[0])] = int(p[1])
    return m


def is_workbench(cmdline):
    """工作台自己的后台服务（正式版和各个测试副本）不列出、不允许停。"""
    for tok in cmdline.split():
        if tok.endswith('server.py'):
            d = os.path.dirname(tok)
            if os.path.isfile(os.path.join(d, 'hook.py')) and os.path.isfile(os.path.join(d, 'static', 'app2.js')):
                return True
    return False


NOT_WEB = re.compile(r'(?i)(?:\bmcp\b|[-_]mcp\b|\bmcp[-_]|modelcontextprotocol|agent-browser|Chrome for Testing|--remote-debugging-port|chromedriver|playwright)')


def project_listeners(cwd):
    if not cwd or not os.path.isdir(cwd):
        return []
    socks, pid, cmd = {}, None, ''
    for l in sh(['lsof', '+c', '0', '-nP', '-iTCP', '-sTCP:LISTEN', '-F', 'pcn']).splitlines():
        if not l:
            continue
        k, v = l[0], l[1:]
        if k == 'p':
            pid, cmd = (int(v) if v.isdigit() else None), ''
        elif k == 'c':
            cmd = v
        elif k == 'n' and pid:
            m = re.match(r'^(.*):(\d+)$', v)
            if not m:
                continue
            e = socks.setdefault((pid, int(m.group(2))), {'pid': pid, 'port': int(m.group(2)), 'name': cmd, 'hosts': []})
            if m.group(1) not in e['hosts']:
                e['hosts'].append(m.group(1))
    if not socks:
        return []
    pids = sorted({k[0] for k in socks})
    cwds, cur = {}, None
    for l in sh(['lsof', '-a', '-p', ','.join(map(str, pids)), '-d', 'cwd', '-Fpn']).splitlines():
        if l.startswith('p'):
            cur = int(l[1:]) if l[1:].isdigit() else None
        elif l.startswith('n') and cur:
            cwds[cur] = l[1:]
    cmdl = {}
    for l in sh(['ps', '-o', 'pid=,command=', '-p', ','.join(map(str, pids))]).splitlines():
        p = l.strip().split(None, 1)
        if p and p[0].isdigit():
            cmdl[int(p[0])] = p[1] if len(p) > 1 else ''
    pmap = ppid_map()
    clis = {r.pid for r in list((NS.get('RUNNERS') or {}).values())
            if getattr(r, 'pid', 0) and r.state.get('alive') and in_dir(r.cwd, cwd)}
    me, lib = os.getpid(), os.path.join(home(), 'Library')
    res = []
    for (pid, port), e in sorted(socks.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        cl = cmdl.get(pid, '')
        if pid == me or pid in clis or is_workbench(cl) or pmap.get(pid) in clis or NOT_WEB.search(cl):
            continue  # CLI 直接带起来的（MCP 服务）、浏览器自动化和 MCP 仪表盘之类不是项目的网页服务
        why, c = '', cwds.get(pid) or ''
        if c and in_dir(c, cwd) and not (in_dir(c, lib) and not in_dir(cwd, lib)):  # 应用沙盒（~/Library）里的进程不算项目的
            why = 'cwd'
        else:
            x, k = pmap.get(pid), 0
            while x and x > 1 and k < 64:
                if x in clis:
                    why = 'cli'
                    break
                x, k = pmap.get(x), k + 1
        if not why:
            continue
        hs = e['hosts']
        host = '127.0.0.1' if any(h in ('*', '127.0.0.1', '0.0.0.0') for h in hs) else 'localhost'
        res.append(dict(e, cwd=c, why=why, cmd=cl[:300], url='http://%s:%d/' % (host, port)))
    return res


def api_ports(b):
    return {'ports': project_listeners(b.get('cwd') or '')}


def api_port_kill(b):
    try:
        pid = int(b.get('pid') or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 1 or not any(x['pid'] == pid for x in project_listeners(b.get('cwd') or '')):
        raise ValueError('只能停止属于当前项目的进程')
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        raise ValueError('进程已经退出')
    except PermissionError:
        raise ValueError('没有权限停止这个进程')
    return {'ok': True}


# ================================================================ MCP 管理（写入走 CLI 原生的 claude mcp 子命令）
MCP_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
MCP_SCOPES = ('user', 'project', 'local')


def claude_cli(args, cwd, timeout=60):
    """在项目目录下运行 claude 子命令，环境和工作台启动 CLI 时用的那份干净环境一致。"""
    exe = NS.get('CLAUDE') or 'claude'
    wd = cwd if cwd and os.path.isdir(cwd) else home()
    try:
        p = subprocess.run([exe] + args, cwd=wd, env=login_env(), stdin=DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError('命令超时')
    except OSError as e:
        raise ValueError('无法运行 claude：%s' % e)
    out = (p.stdout + b'\n' + p.stderr).decode('utf-8', 'replace')
    ansi = NS.get('ANSI_RE')
    if ansi:
        out = ansi.sub('', out)
    return p.returncode, out.strip()


def last_line(s):
    ls = [x.strip() for x in (s or '').split('\n') if x.strip()]
    return (ls[-1] if ls else '')[:300]


def load_json(p):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def mcp_servers(cwd):
    out = []

    def add(scope, servers):
        for name, cfg in (servers or {}).items():
            if not isinstance(cfg, dict):
                continue
            env, hd = cfg.get('env'), cfg.get('headers')
            out.append({'name': name, 'scope': scope, 'type': cfg.get('type') or ('http' if cfg.get('url') else 'stdio'),
                        'command': str(cfg.get('command') or ''), 'args': [str(x) for x in (cfg.get('args') or [])][:40],
                        'url': re.sub(r'[?#].*$', '', str(cfg.get('url') or '')),
                        'env': sorted(env) if isinstance(env, dict) else [], 'headers': sorted(hd) if isinstance(hd, dict) else []})
    cj = load_json(os.path.join(home(), '.claude.json'))
    add('user', cj.get('mcpServers'))
    if cwd and os.path.isdir(cwd):
        projs = cj.get('projects') or {}
        for key in dict.fromkeys([cwd, os.path.realpath(cwd)]):
            if isinstance(projs.get(key), dict) and projs[key].get('mcpServers'):
                add('local', projs[key]['mcpServers'])
                break
        add('project', load_json(os.path.join(cwd, '.mcp.json')).get('mcpServers'))
    return out


def api_mcp_list(b):
    return {'servers': mcp_servers(b.get('cwd') or '')}


def parse_kv(text, sep, what):
    out = {}
    for i, l in enumerate((text or '').split('\n'), 1):
        l = l.strip()
        if not l:
            continue
        if sep not in l or not l.split(sep, 1)[0].strip():
            raise ValueError('%s第 %d 行格式有误' % (what, i))
        k, v = l.split(sep, 1)
        out[k.strip()] = v.strip()
    return out


def check_mcp(b):
    cwd, name, scope = b.get('cwd') or '', (b.get('name') or '').strip(), b.get('scope') or ''
    if not MCP_NAME.match(name):
        raise ValueError('名称只能用字母、数字、点、横线和下划线')
    if scope not in MCP_SCOPES:
        raise ValueError('范围不对')
    if scope != 'user' and not os.path.isdir(cwd):
        raise ValueError('先打开一个项目')
    return cwd, name, scope


def api_mcp_add(b):
    cwd, name, scope = check_mcp(b)
    typ = b.get('type') or 'stdio'
    if typ not in ('stdio', 'http', 'sse'):
        raise ValueError('类型不对')
    cfg = {'type': typ}
    if typ == 'stdio':
        cmd = (b.get('command') or '').strip()
        try:
            args = shlex.split(b.get('args') or '')
            if not args and ' ' in cmd and not os.path.exists(cmd):
                cmd, args = shlex.split(cmd)[0], shlex.split(cmd)[1:]
        except (ValueError, IndexError):
            raise ValueError('命令或参数格式有误（引号没有配对）')
        if not cmd:
            raise ValueError('请填写命令')
        cfg.update(command=cmd, args=args)
        env = parse_kv(b.get('env'), '=', '环境变量')
        if env:
            cfg['env'] = env
    else:
        url = (b.get('url') or '').strip()
        if not re.match(r'^https?://\S+$', url, re.I):
            raise ValueError('请填写以 http:// 或 https:// 开头的地址')
        cfg['url'] = url
        hd = parse_kv(b.get('headers'), ':', '请求头')
        if hd:
            cfg['headers'] = hd
    if any(s['name'] == name and s['scope'] == scope for s in mcp_servers(cwd)):
        raise ValueError('这个范围里已经有同名服务')
    rc, out = claude_cli(['mcp', 'add-json', name, json.dumps(cfg, ensure_ascii=False), '-s', scope], cwd)
    if rc != 0:
        raise ValueError('添加失败：' + (last_line(out) or str(rc)))
    return {'ok': True, 'text': out[-500:]}


def api_mcp_remove(b):
    cwd, name, scope = check_mcp(b)
    rc, out = claude_cli(['mcp', 'remove', name, '-s', scope], cwd)
    if rc != 0:
        raise ValueError('删除失败：' + (last_line(out) or str(rc)))
    return {'ok': True}


MCP_STATE = {'ok': '已连接', 'auth': '需要登录', 'pending': '待批准', 'fail': '连接失败', 'unknown': '无法判断'}


def api_mcp_test(b):
    cwd, name = b.get('cwd') or '', (b.get('name') or '').strip()
    if not MCP_NAME.match(name):
        raise ValueError('名称不对')
    rc, out = claude_cli(['mcp', 'get', name], cwd, timeout=90)
    st = re.search(r'Status:\s*(.+)', out)
    issue = re.search(r'Issue:\s*(.+)', out)
    s = st.group(1).strip() if st else ''
    if not s and rc != 0:
        raise ValueError('测试失败：' + (last_line(out) or str(rc)))
    low = s.lower()
    kind = ('ok' if ('connected' in low and 'fail' not in low) or '✓' in s else 'auth' if 'auth' in low else
            'pending' if 'pending' in low or 'approv' in low else 'fail' if ('fail' in low or '✘' in s) else 'unknown')
    return {'state': kind, 'label': MCP_STATE[kind], 'issue': issue.group(1).strip()[:300] if issue else '', 'raw': s[:200]}


# ================================================================ 技能：本地列表、查看、删除
SKILL_NAME = re.compile(r'^[\w一-鿿][\w.\-一-鿿]{0,63}$')
SKIP_COPY = {'.git', 'node_modules', '__pycache__', '.venv', '__MACOSX'}


def user_root():
    return os.path.join(home(), '.claude', 'skills')


def proj_root(cwd):
    return os.path.join(cwd, '.claude', 'skills') if cwd and os.path.isdir(cwd) else None


def stage_root():
    return os.path.join(NS.get('RUN_DIR') or '/tmp', 'skill-stage')


def market_dir():
    d = os.path.join(NS.get('RUN_DIR') or '/tmp', 'market')
    os.makedirs(d, exist_ok=True)
    return d


def parse_front(text):
    m = re.match(r'^﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)', text or '', re.S)
    if not m:
        return {}
    out, lines, i = {}, m.group(1).split('\n'), 0
    while i < len(lines):
        mm = re.match(r'^([A-Za-z_][\w-]*):\s*(.*?)\s*$', lines[i])
        i += 1
        if not mm:
            continue
        k, v = mm.group(1), mm.group(2)
        if v in ('>', '|', '>-', '|-', '>+', '|+'):
            buf = []
            while i < len(lines) and (lines[i][:1] in (' ', '\t') or not lines[i].strip()):
                buf.append(lines[i].strip())
                i += 1
            v = (' ' if v.startswith('>') else '\n').join(buf).strip()
        else:
            while i < len(lines) and lines[i][:1] in (' ', '\t') and lines[i].strip():
                v += ' ' + lines[i].strip()
                i += 1
            if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
                v = v[1:-1]
        if k in ('name', 'description', 'license'):
            out[k] = v.strip()
    return out


def strip_front(text):
    return re.sub(r'^﻿?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)', '', text or '', count=1, flags=re.S)


def read_text(p, limit=300_000):
    try:
        with open(p, 'rb') as f:
            return f.read(limit).decode('utf-8', 'replace')
    except OSError:
        return ''


LIC_PATS = (('apache license', 'Apache-2.0'), ('mit license', 'MIT'), ('permission is hereby granted, free of charge', 'MIT'),
            ('bsd 3-clause', 'BSD-3-Clause'), ('bsd 2-clause', 'BSD-2-Clause'), ('redistribution and use in source and binary', 'BSD'),
            ('gnu affero', 'AGPL'), ('gnu lesser', 'LGPL'), ('gnu general public license', 'GPL'), ('mozilla public license', 'MPL-2.0'),
            ('creative commons', 'CC'), ('unlicense', 'Unlicense'), ('all rights reserved', '专有'), ('proprietary', '专有'))


def license_of_text(t):
    low = (t or '')[:4000].lower()
    for pat, lab in LIC_PATS:
        if pat in low:
            return lab
    return '其他' if low.strip() else ''


def license_of_front(v):
    v = (v or '').strip()
    if not v:
        return ''
    if re.fullmatch(r'[A-Za-z0-9.+-]{2,30}', v):
        return v
    return license_of_text(v) if re.search(r'proprietary|rights reserved|apache|mit\b|bsd|gpl', v, re.I) else ''


LIC_FILE = re.compile(r'^(license|licence|copying)(\.(txt|md))?$', re.I)


def skill_entry(d, scope, plugin=''):
    fm = parse_front(read_text(os.path.join(d, 'SKILL.md'), 12000))
    return {'dir': d, 'folder': os.path.basename(d), 'name': fm.get('name') or os.path.basename(d),
            'desc': (fm.get('description') or '')[:400], 'scope': scope, 'plugin': plugin, 'readonly': scope == 'plugin'}


def list_dir_skills(root, scope, plugin=''):
    out = []
    try:
        names = sorted(os.listdir(root), key=str.lower)
    except OSError:
        return out
    for n in names:
        d = os.path.join(root, n)
        if not n.startswith('.') and os.path.isfile(os.path.join(d, 'SKILL.md')):
            out.append(skill_entry(d, scope, plugin))
    return out


def plugin_roots():
    """插件带的技能：settings.json 里 CLAUDE_CODE_PLUGIN_DIRS 指定的目录，以及已安装插件的缓存目录。"""
    dirs = []
    st = load_json(os.path.join(home(), '.claude', 'settings.json'))
    pd = (st.get('env') or {}).get('CLAUDE_CODE_PLUGIN_DIRS') or ''
    dirs += [x.strip() for x in re.split(r'[:,]', pd) if x.strip()]
    ip = load_json(os.path.join(home(), '.claude', 'plugins', 'installed_plugins.json')).get('plugins') or {}
    for v in ip.values():
        for e in (v if isinstance(v, list) else [v]):
            if isinstance(e, dict) and e.get('installPath'):
                dirs.append(e['installPath'])
    import glob as _g
    for pj in _g.glob(os.path.join(home(), '.claude', 'plugins', 'cache', '*', '*', '*', '.claude-plugin', 'plugin.json')):
        dirs.append(os.path.dirname(os.path.dirname(pj)))
    out, seen = [], set()
    for d in dirs:
        rp = os.path.realpath(os.path.expanduser(d))
        if rp in seen or not os.path.isdir(os.path.join(rp, 'skills')):
            continue
        seen.add(rp)
        name = load_json(os.path.join(rp, '.claude-plugin', 'plugin.json')).get('name') or os.path.basename(rp.rstrip('/'))
        out.append((os.path.join(rp, 'skills'), name))
    return out


def api_skills(b):
    cwd = b.get('cwd') or ''
    pr = proj_root(cwd)
    plugin = []
    for root, name in plugin_roots():
        plugin += list_dir_skills(root, 'plugin', name)
    return {'user': list_dir_skills(user_root(), 'user'), 'project': list_dir_skills(pr, 'project') if pr else [],
            'plugin': plugin, 'userRoot': user_root(), 'projectRoot': pr or ''}


def lexical(p):
    return os.path.normpath(os.path.abspath(os.path.expanduser(p or '')))


def allowed_skill_dir(d, cwd):
    p = lexical(d)
    roots = [user_root(), proj_root(cwd) or '', stage_root()] + [r for r, _ in plugin_roots()]
    return any(r and (p + '/').startswith(lexical(r) + '/') and p != lexical(r) for r in roots)


def file_list(d, limit=300):
    out = []
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_COPY)
        for n in sorted(files):
            if n == '.DS_Store':
                continue
            fp = os.path.join(root, n)
            try:
                sz = os.lstat(fp).st_size
            except OSError:
                sz = 0
            out.append({'p': os.path.relpath(fp, d), 's': sz})
            if len(out) >= limit:
                return out
    return out


def local_license(d):
    for e in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if LIC_FILE.match(e):
            return license_of_text(read_text(os.path.join(d, e), 6000))
    return ''


def api_skill_read(b):
    d, cwd = b.get('dir') or '', b.get('cwd') or ''
    if not allowed_skill_dir(d, cwd) or not os.path.isfile(os.path.join(d, 'SKILL.md')):
        raise ValueError('找不到这个技能')
    text = read_text(os.path.join(d, 'SKILL.md'))
    fm = parse_front(text)
    return {'dir': d, 'name': fm.get('name') or os.path.basename(d), 'desc': fm.get('description') or '', 'text': text,
            'license': local_license(d) or license_of_front(fm.get('license')), 'files': file_list(d)}


def api_skill_delete(b):
    d, cwd = lexical(b.get('dir') or ''), b.get('cwd') or ''
    parents = [lexical(user_root())] + ([lexical(proj_root(cwd))] if proj_root(cwd) else [])
    if os.path.dirname(d) not in parents:
        raise ValueError('只能删除用户级或项目级的技能')
    if not os.path.lexists(d):
        raise ValueError('找不到这个技能')
    if not NS['trash']([d]):
        raise ValueError('移到废纸篓失败')
    return {'ok': True}


def api_skill_pick(b):
    osa = NS['osa']
    if b.get('kind') == 'zip':
        p = osa('tell me to activate\nPOSIX path of (choose file with prompt "选择 zip 文件" of type {"public.zip-archive"})')
    else:
        p = osa('tell me to activate\nPOSIX path of (choose folder with prompt "选择技能文件夹")')
    return {'path': p.rstrip('/') if p else ''}


# ================================================================ 网络（GitHub、技能目录）
def proxies():
    env, px = login_env(), {}
    for sc in ('http', 'https'):
        v = env.get(sc + '_proxy') or env.get(sc.upper() + '_PROXY') or env.get('all_proxy') or env.get('ALL_PROXY') or ''
        if re.match(r'^https?://', v):
            px[sc] = v
    return px


def http_get(url, timeout=15, max_bytes=20 << 20, accept=None):
    px = proxies()
    # 登录环境里设了代理就用它；没设时 urllib 默认读进程环境变量和 macOS 系统代理
    opener = build_opener(ProxyHandler(px)) if px else build_opener()
    host = urlparse(url).hostname or url
    h = {'User-Agent': 'claude-workbench-skills/1.0'}
    if accept:
        h['Accept'] = accept
    tok = login_env().get('GITHUB_TOKEN') or login_env().get('GH_TOKEN')
    if tok and host == 'api.github.com':
        h['Authorization'] = 'Bearer ' + tok
    try:
        with opener.open(Request(url, headers=h), timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except HTTPError as e:
        if e.code in (403, 429) and (e.headers.get('X-RateLimit-Remaining') == '0' or e.code == 429):
            reset = int(e.headers.get('X-RateLimit-Reset') or 0)
            mins = max(1, int((reset - time.time()) / 60) + 1) if reset else 60
            raise ValueError('GitHub 接口访问次数已达上限，请约 %d 分钟后再试' % mins)
        if e.code == 404:
            raise ValueError('找不到：' + re.sub(r'^https?://', '', url)[:160])
        raise ValueError('%s 返回错误 %d' % (host, e.code))
    except (socket.timeout, TimeoutError):
        raise ValueError('连接 %s 超时，请检查网络或代理设置' % host)
    except (URLError, ConnectionError, ssl.SSLError, OSError) as e:
        reason = getattr(e, 'reason', e)
        if isinstance(reason, (socket.timeout, TimeoutError)) or 'timed out' in str(reason):
            raise ValueError('连接 %s 超时，请检查网络或代理设置' % host)
        raise ValueError('连不上 %s，请检查网络或代理设置' % host)
    if len(data) > max_bytes:
        raise ValueError('文件太大，没有下载')
    return data


def http_json(url, **kw):
    try:
        return json.loads(http_get(url, accept='application/json', **kw))
    except ValueError as e:
        if isinstance(e, json.JSONDecodeError):
            raise ValueError('%s 返回的内容无法识别' % (urlparse(url).hostname or url))
        raise


def cache_get(name, ttl):
    p = os.path.join(market_dir(), name)
    try:
        if ttl is None or time.time() - os.path.getmtime(p) < ttl:
            with open(p, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def cache_put(name, obj):
    p = os.path.join(market_dir(), name)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, p)


def safe_name(s):
    return re.sub(r'[^\w.-]+', '_', s)[:120]


GH = {'tree': {}, 'repo': {}}
GH_LOCK = threading.Lock()


def gh_repo(o, r):
    k = '%s/%s' % (o, r)
    if k in GH['repo']:
        return GH['repo'][k]
    c = cache_get('repo-%s.json' % safe_name(k), 86400)
    if c is None:
        j = http_json('https://api.github.com/repos/%s/%s' % (quote(o), quote(r)))
        c = {'default_branch': j.get('default_branch') or 'main', 'license': ((j.get('license') or {}).get('spdx_id') or '')}
        cache_put('repo-%s.json' % safe_name(k), c)
    GH['repo'][k] = c
    return c


def gh_tree(o, r, ref, refresh=False):
    k = '%s/%s@%s' % (o, r, ref)
    with GH_LOCK:
        if not refresh and k in GH['tree'] and time.time() - GH['tree'][k][0] < 3600:
            return GH['tree'][k][1]
    name = 'tree-%s.json' % safe_name(k)
    c = None if refresh else cache_get(name, 3600)
    if c is None:
        j = http_json('https://api.github.com/repos/%s/%s/git/trees/%s?recursive=1' % (quote(o), quote(r), quote(ref, safe='')))
        c = [{'path': e['path'], 'mode': e.get('mode') or '', 'size': e.get('size') or 0}
             for e in j.get('tree') or [] if e.get('type') == 'blob']
        cache_put(name, c)
    with GH_LOCK:
        GH['tree'][k] = (time.time(), c)
    return c


def raw_url(o, r, ref, path):
    return 'https://raw.githubusercontent.com/%s/%s/%s/%s' % (quote(o), quote(r), quote(ref, safe=''), quote(path))


GH_URL = re.compile(r'^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/(?:tree|blob)/([^/?#]+)(?:/([^?#]*))?)?/?(?:[?#].*)?$')


def parse_gh(u):
    m = GH_URL.match((u or '').strip())
    if not m:
        return None
    o, r, ref, path = m.groups()
    path = (path or '').strip('/')
    if path.endswith('SKILL.md'):
        path = os.path.dirname(path)
    return o, r, ref, path


def gh_cand(o, r, ref, path):
    return {'kind': 'gh', 'o': o, 'r': r, 'ref': ref, 'path': path}


def gh_skill_dirs(tree, path):
    pre = path + '/' if path else ''
    if any(e['path'] == pre + 'SKILL.md' for e in tree):
        return [path]
    ds = sorted({os.path.dirname(e['path']) for e in tree if e['path'].startswith(pre) and os.path.basename(e['path']) == 'SKILL.md'})
    # 技能里面嵌套的 SKILL.md（示例、模板）不单独算
    out = []
    for d in ds:
        if not any(d.startswith(x + '/') for x in out):
            out.append(d)
    return out


def pmap(fn, items, n=8):
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(fn, items))


def gh_front(o, r, ref, d):
    try:
        return parse_front(http_get(raw_url(o, r, ref, (d + '/' if d else '') + 'SKILL.md'), max_bytes=400_000).decode('utf-8', 'replace'))
    except ValueError:
        return {}


def gh_license(o, r, ref, d, tree, repo_fallback=True):
    pre = d + '/' if d else ''
    for e in tree:
        rel = e['path'][len(pre):] if e['path'].startswith(pre) else None
        if rel and '/' not in rel and LIC_FILE.match(rel):
            try:
                return license_of_text(http_get(raw_url(o, r, ref, e['path']), max_bytes=200_000).decode('utf-8', 'replace'))
            except ValueError:
                return ''
    if repo_fallback:
        try:
            return gh_repo(o, r).get('license') or ''
        except ValueError:
            return ''
    return ''


# ================================================================ 安装来源：本地文件夹、zip、GitHub
def clean_stage():
    root = stage_root()
    try:
        for n in os.listdir(root):
            p = os.path.join(root, n)
            if time.time() - os.path.getmtime(p) > 86400:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def new_stage(tag):
    clean_stage()
    d = os.path.join(stage_root(), '%s-%s-%s' % (tag, time.strftime('%Y%m%d%H%M%S'), secrets.token_hex(3)))
    os.makedirs(d)
    return d


def safe_rel(p):
    p = (p or '').replace('\\', '/')
    if not p or p.startswith('/') or '\0' in p or re.match(r'^[A-Za-z]:', p):
        return None
    parts = [x for x in p.split('/') if x not in ('', '.')]
    if not parts or any(x == '..' for x in parts):
        return None
    return '/'.join(parts)


LIMIT_FILES, LIMIT_BYTES, LIMIT_ONE = 600, 60 << 20, 15 << 20


def unzip_safe(zp, dest):
    """只解压普通文件：跳过绝对路径、..、符号链接，限制数量和大小。"""
    try:
        z = zipfile.ZipFile(zp)
    except (zipfile.BadZipFile, OSError):
        raise ValueError('不是有效的 zip 文件')
    with z:
        infos = z.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > 200 << 20:
            raise ValueError('zip 太大，没有解压')
        n = total = 0
        for i in infos:
            name = i.filename
            if name.endswith('/') or name.startswith('__MACOSX/') or '/__MACOSX/' in name or name.endswith('.DS_Store'):
                continue
            mode = (i.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                continue
            rel = safe_rel(name)
            if not rel:
                continue
            n += 1
            if n > LIMIT_FILES * 4:
                raise ValueError('zip 里文件太多')
            dp = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            with z.open(i) as s, open(dp, 'wb') as d:
                while True:
                    chunk = s.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 200 << 20:
                        raise ValueError('zip 太大，没有解压')
                    d.write(chunk)
            os.chmod(dp, 0o755 if (i.external_attr >> 16) & 0o111 else 0o644)


def scan_local(d, depth=3):
    if os.path.isfile(os.path.join(d, 'SKILL.md')):
        return [d]
    out = []

    def walk(x, lv):
        if lv > depth or len(out) >= 100:
            return
        try:
            names = sorted(os.listdir(x), key=str.lower)
        except OSError:
            return
        for n in names:
            p = os.path.join(x, n)
            if n.startswith('.') or n in SKIP_COPY or os.path.islink(p) or not os.path.isdir(p):
                continue
            if os.path.isfile(os.path.join(p, 'SKILL.md')):
                out.append(p)
            else:
                walk(p, lv + 1)
    walk(d, 1)
    return out


def local_item(d, folder=None):
    e = skill_entry(d, 'local')
    return {'cand': {'kind': 'local', 'dir': d}, 'folder': folder or os.path.basename(d), 'name': e['name'], 'desc': e['desc'],
            'src': shorten(d)}


def shorten(p):
    h = home()
    return '~' + p[len(h):] if p.startswith(h + '/') else p


def api_skill_scan(b):
    src = (b.get('src') or '').strip()
    if not src:
        raise ValueError('请填写来源')
    gh = parse_gh(src)
    if gh:
        o, r, ref, path = gh
        if not ref:
            ref = gh_repo(o, r)['default_branch']
        tree = gh_tree(o, r, ref)
        dirs = gh_skill_dirs(tree, path)
        if not dirs:
            raise ValueError('这个位置没有找到技能（缺少 SKILL.md）')
        dirs = dirs[:80]
        fms = pmap(lambda d: gh_front(o, r, ref, d), dirs) if len(dirs) <= 40 else [{} for _ in dirs]
        items = [{'cand': gh_cand(o, r, ref, d), 'folder': os.path.basename(d) or r, 'name': fm.get('name') or os.path.basename(d) or r,
                  'desc': (fm.get('description') or '')[:400], 'src': 'github.com/%s/%s%s' % (o, r, '/' + d if d else '')}
                 for d, fm in zip(dirs, fms)]
        return {'items': items}
    p = lexical(src)
    if os.path.isfile(p) and p.lower().endswith('.zip'):
        stage = new_stage('zip')
        unzip_safe(p, stage)
        found = scan_local(stage, depth=4)
        stem = re.sub(r'\.zip$', '', os.path.basename(p), flags=re.I)
        items = [local_item(d, stem if d == stage else None) for d in found]
        for it in items:
            it['src'] = os.path.basename(p) + ('/' + os.path.relpath(it['cand']['dir'], stage) if it['cand']['dir'] != stage else '')
    elif os.path.isdir(p):
        items = [local_item(d) for d in scan_local(p)]
    else:
        raise ValueError('无法识别的来源：请填本地文件夹、zip 文件或 GitHub 链接')
    if not items:
        raise ValueError('没有找到技能（缺少 SKILL.md）')
    return {'items': items}


def check_cand(c):
    if not isinstance(c, dict):
        raise ValueError('来源不对')
    if c.get('kind') == 'local':
        d = c.get('dir') or ''
        if not os.path.isfile(os.path.join(d, 'SKILL.md')):
            raise ValueError('找不到 SKILL.md')
        return c
    if c.get('kind') == 'gh':
        for k in ('o', 'r', 'ref'):
            if not re.fullmatch(r'[\w.\-/]+', str(c.get(k) or '')):
                raise ValueError('来源不对')
        if '..' in str(c.get('path') or '').split('/'):
            raise ValueError('来源不对')
        return c
    raise ValueError('来源不对')


def api_skill_preview(b):
    c = check_cand(b.get('cand'))
    if c['kind'] == 'local':
        d = c['dir']
        text = read_text(os.path.join(d, 'SKILL.md'))
        fm = parse_front(text)
        return {'name': fm.get('name') or os.path.basename(d), 'desc': fm.get('description') or '', 'text': text,
                'files': file_list(d), 'license': local_license(d) or license_of_front(fm.get('license')), 'src': shorten(d)}
    o, r, ref, d = c['o'], c['r'], c['ref'], c.get('path') or ''
    tree = gh_tree(o, r, ref)
    pre = d + '/' if d else ''
    files = [{'p': e['path'][len(pre):], 's': e['size']} for e in tree if e['path'].startswith(pre)]
    if not any(f['p'] == 'SKILL.md' for f in files):
        raise ValueError('找不到 SKILL.md')
    text = http_get(raw_url(o, r, ref, pre + 'SKILL.md'), max_bytes=400_000).decode('utf-8', 'replace')
    fm = parse_front(text)
    lic = gh_license(o, r, ref, d, tree, repo_fallback=False) or license_of_front(fm.get('license'))
    if not lic:
        try:
            lic = gh_repo(o, r).get('license') or ''
            lic = '' if lic == 'NOASSERTION' else lic
        except ValueError:
            lic = ''
    return {'name': fm.get('name') or os.path.basename(d) or r, 'desc': fm.get('description') or '', 'text': text,
            'files': files[:300], 'nfiles': len(files), 'license': lic, 'src': 'github.com/%s/%s%s' % (o, r, '/' + d if d else '')}


def copy_local(src, dest):
    n = total = 0
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [x for x in dirs if x not in SKIP_COPY and not os.path.islink(os.path.join(root, x))]
        for f in files:
            sp = os.path.join(root, f)
            if f == '.DS_Store' or os.path.islink(sp) or not os.path.isfile(sp):
                continue
            n += 1
            total += os.path.getsize(sp)
            if n > LIMIT_FILES or total > LIMIT_BYTES:
                raise ValueError('技能文件太多或太大，没有安装')
            dp = os.path.join(dest, os.path.relpath(sp, src))
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.copy2(sp, dp, follow_symlinks=False)


def fetch_gh(c, dest):
    o, r, ref, d = c['o'], c['r'], c['ref'], c.get('path') or ''
    tree = gh_tree(o, r, ref)
    pre = d + '/' if d else ''
    files = [e for e in tree if e['path'].startswith(pre) and e['mode'] not in ('120000', '160000')
             and not e['path'][len(pre):].startswith('.git/')]
    if len(files) > LIMIT_FILES or sum(e['size'] for e in files) > LIMIT_BYTES or any(e['size'] > LIMIT_ONE for e in files):
        raise ValueError('技能文件太多或太大，没有安装')

    def dl(e):
        rel = safe_rel(e['path'][len(pre):])
        if not rel:
            return
        data = http_get(raw_url(o, r, ref, e['path']), timeout=30, max_bytes=LIMIT_ONE)
        dp = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        with open(dp, 'wb') as f:
            f.write(data)
        os.chmod(dp, 0o755 if e['mode'] == '100755' else 0o644)
    pmap(dl, files)


def rename_front(p, name):
    t = read_text(p, 2_000_000)
    m = re.match(r'^(﻿?---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*(?:\r?\n|$))', t, re.S)
    if not m or not re.search(r'^name:', m.group(2), re.M):
        return
    head = re.sub(r'^name:.*$', 'name: ' + name, m.group(2), count=1, flags=re.M)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(m.group(1) + head + m.group(3) + t[m.end():])


INSTALL_LOCK = threading.Lock()


def api_skill_install(b):
    c = check_cand(b.get('cand'))
    target, cwd, mode = b.get('target'), b.get('cwd') or '', b.get('mode') or ''
    root = user_root() if target == 'user' else proj_root(cwd) if target == 'project' else None
    if not root:
        raise ValueError('先打开一个项目' if target == 'project' else '安装位置不对')
    folder = ((b.get('newName') if mode == 'rename' else b.get('folder')) or '').strip()
    if not folder:
        folder = os.path.basename(c.get('dir') or c.get('path') or '') or c.get('r') or ''
    if not SKILL_NAME.match(folder) or folder in ('.', '..'):
        raise ValueError('名称只能用字母、数字、中文、点、横线和下划线')
    final = os.path.join(root, folder)
    with INSTALL_LOCK:
        if os.path.lexists(final) and mode != 'overwrite':
            return {'conflict': True, 'name': folder}
    stage = new_stage('install')
    try:
        dest = os.path.join(stage, folder)
        os.makedirs(dest)
        if c['kind'] == 'local':
            copy_local(c['dir'], dest)
        else:
            fetch_gh(c, dest)
        if not os.path.isfile(os.path.join(dest, 'SKILL.md')):
            raise ValueError('下载的内容里没有 SKILL.md，没有安装')
        if mode == 'rename':
            rename_front(os.path.join(dest, 'SKILL.md'), folder)
        with INSTALL_LOCK:
            os.makedirs(root, exist_ok=True)
            if os.path.lexists(final):
                if mode != 'overwrite':
                    return {'conflict': True, 'name': folder}
                if not NS['trash']([final]):
                    raise ValueError('旧技能移到废纸篓失败，没有覆盖')
            shutil.move(dest, final)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {'ok': True, 'dir': final, 'name': folder}


# ================================================================ 技能市场
ANTH = ('anthropics', 'skills', 'main', 'skills')


def market_anthropic(refresh):
    c = None if refresh else cache_get('anthropic.json', 86400)
    if c:
        return c, False
    try:
        o, r, ref, base = ANTH
        tree = gh_tree(o, r, ref, refresh=refresh)
        dirs = sorted({os.path.dirname(e['path']) for e in tree
                       if e['path'].startswith(base + '/') and e['path'].count('/') == 2 and e['path'].endswith('/SKILL.md')})

        def one(d):
            fm = gh_front(o, r, ref, d)
            lic = gh_license(o, r, ref, d, tree, repo_fallback=False) or license_of_front(fm.get('license')) or '未声明'
            return {'id': '%s/%s/%s' % (o, r, d), 'name': fm.get('name') or os.path.basename(d), 'folder': os.path.basename(d),
                    'desc': (fm.get('description') or '')[:500], 'license': lic, 'cand': gh_cand(o, r, ref, d),
                    'src': 'anthropics/skills'}
        c = {'at': time.time(), 'items': pmap(one, dirs)}
        cache_put('anthropic.json', c)
        return c, False
    except ValueError:
        old = cache_get('anthropic.json', None)
        if old:
            return old, True
        raise


def market_community(q, offset, refresh):
    key = 'community-%s.json' % hashlib.sha1(('%s|%d' % (q, offset)).encode()).hexdigest()[:16]
    c = None if refresh else cache_get(key, 3600)
    if c:
        return c, False
    try:
        j = http_json('https://claude-plugins.dev/api/skills?' + urlencode({'q': q, 'limit': 40, 'offset': offset}))
    except ValueError:
        old = cache_get(key, None)
        if old:
            return old, True
        raise
    items = []
    for s in j.get('skills') or []:
        md = s.get('metadata') or {}
        o, r, d, raw = md.get('repoOwner') or '', md.get('repoName') or '', (md.get('directoryPath') or '').strip('/'), md.get('rawFileUrl') or ''
        pre = 'https://raw.githubusercontent.com/%s/%s/' % (o, r)
        tail = ('/' + d if d else '') + '/SKILL.md'
        if not (o and r and raw.startswith(pre) and raw.endswith(tail)):
            continue
        ref = raw[len(pre):len(raw) - len(tail)]
        if not ref or not re.fullmatch(r'[\w.\-/]+', ref):
            continue
        items.append({'id': s.get('id') or '%s/%s/%s' % (o, r, d), 'name': s.get('name') or os.path.basename(d) or r,
                      'folder': os.path.basename(d) or r, 'desc': (s.get('description') or '')[:500], 'license': '',
                      'stars': s.get('stars') or 0, 'installs': s.get('installs') or 0, 'cand': gh_cand(o, r, ref, d),
                      'src': '%s/%s' % (o, r)})
    c = {'at': time.time(), 'items': items, 'total': j.get('total') or len(items)}
    cache_put(key, c)
    return c, False


def api_market(b):
    src, q = b.get('source') or 'anthropic', (b.get('q') or '').strip()[:100]
    refresh = bool(b.get('refresh'))
    if src == 'community':
        c, stale = market_community(q, max(0, int(b.get('offset') or 0)), refresh)
        return {'items': c['items'], 'total': c.get('total', len(c['items'])), 'stale': stale, 'at': c.get('at')}
    c, stale = market_anthropic(refresh)
    items = c['items']
    if q:
        ql = q.lower()
        items = [x for x in items if ql in (x['name'] + ' ' + x['desc'] + ' ' + x['folder']).lower()]
    return {'items': items, 'total': len(items), 'stale': stale, 'at': c.get('at')}


# ================= 第十三轮 G：让 Claude 操作工作台（bin/cwb.py） =================
# cwb.py 在工作台启动的 CLI 里运行，从环境变量拿端口、令牌、sid、代次，调这里的 ui-* 接口。
# 只开放三个动作：右栏打开文件（可跳行）、网页预览打开网址、给网址截图。界面动作通过 SSE 发给页面（static/ux-g.js），
# 页面只对发起请求的对话直接执行，其他对话弹提示；截图由原生 App 外壳完成（native/shot.swift），页面转交，结果回到 ui-shot-done。
UI_SHOTS = {}
UI_LOCK = threading.Lock()
SHOT_TIMEOUT = 30
SHOT_ACK = 4  # 秒：这段时间里没有原生 App 页面认领，说明只开了浏览器或 App 太旧


def ui_sid(b):
    """发起请求的对话：先按代次找 Runner（/clear 后环境变量里还是旧 ID），再按 ID（含别名）找运行中的或有记录的对话。"""
    sid, gen = str(b.get('sid') or ''), str(b.get('gen') or '')
    if not re.fullmatch(r'[0-9A-Fa-f-]{8,64}', sid):
        raise ValueError('对话 ID 无效')
    runners = NS.get('RUNNERS') or {}
    if gen:
        for r in list(runners.values()):
            if getattr(r, 'gen', None) == gen:
                return r.sid
    s = NS['resolve_sid'](sid) if NS.get('resolve_sid') else sid
    if s in runners or s in (NS.get('NEW_SESS') or {}):
        return s
    if NS.get('find_transcript') and NS['find_transcript'](s):
        return s
    raise ValueError('找不到这个对话')


def ui_url(u):
    u = str(u or '').strip()
    p = urlparse(u)
    if p.scheme not in ('http', 'https') or not p.netloc or len(u) > 4000:
        raise ValueError('只接受 http:// 或 https:// 开头的网址')
    return u


def api_ui_open(b):
    sid = ui_sid(b)
    path = str(b.get('path') or '')
    if not os.path.isabs(path):
        raise ValueError('需要绝对路径')
    path = os.path.normpath(path)
    if not os.path.exists(path):
        raise ValueError('文件不存在：' + path)
    d = NS['api_preview']({'path': path, 'cwd': os.path.dirname(path)})
    if d.get('kind') == 'other':
        raise ValueError('右栏不能预览这种文件：' + path)
    try:
        line = max(0, int(b.get('line') or 0))
    except (TypeError, ValueError):
        raise ValueError('行号无效')
    if line and d.get('kind') not in ('text', 'md', 'csv'):
        line = 0
    NS['BUS'].emit({'type': 'ui-req', 'act': 'open', 'sid': sid, 'path': path, 'line': line})
    return {'ok': True, 'path': path, 'line': line}


def api_ui_web(b):
    sid = ui_sid(b)
    url = ui_url(b.get('url'))
    NS['BUS'].emit({'type': 'ui-req', 'act': 'web', 'sid': sid, 'url': url})
    return {'ok': True, 'url': url}


def shot_dir():
    d = os.path.join(NS.get('RUN_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '.run'), 'shots')
    os.makedirs(d, exist_ok=True)
    return d


def shot_out(out):
    """截图保存位置：默认 .run/shots/（顺手清掉 7 天前的）；--out 给目录就放进去，给文件必须是 .png。"""
    name = time.strftime('shot-%Y%m%d-%H%M%S-') + secrets.token_hex(2) + '.png'
    if not out:
        d = shot_dir()
        old = time.time() - 7 * 86400
        for n in os.listdir(d):
            p = os.path.join(d, n)
            try:
                if n.endswith('.png') and os.path.getmtime(p) < old:
                    os.remove(p)
            except OSError:
                pass
        return os.path.join(d, name)
    out = str(out)
    if not os.path.isabs(out):
        raise ValueError('--out 需要绝对路径')
    out = os.path.normpath(out)
    if os.path.isdir(out):
        return os.path.join(out, name)
    if not out.lower().endswith('.png'):
        raise ValueError('--out 只能是 .png 文件或已有的文件夹')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    return out


def png_size(data):
    if len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        return None
    return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')


def api_ui_shot(b):
    sid = ui_sid(b)
    url = ui_url(b.get('url'))
    try:
        w, h = int(b.get('w') or 1280), int(b.get('h') or 800)
    except (TypeError, ValueError):
        raise ValueError('尺寸无效')
    if not (320 <= w <= 3840 and 240 <= h <= 4000):
        raise ValueError('尺寸超出范围：宽 320～3840，高 240～4000')
    out = shot_out(b.get('out'))
    bus = NS['BUS']
    if not bus.clients:
        raise ValueError('工作台窗口没有打开：截图需要打开 Claude 工作台 App')
    sh_id = secrets.token_hex(8)
    rec = {'ack': threading.Event(), 'done': threading.Event(), 'acked': False, 'png': '', 'err': ''}
    with UI_LOCK:
        UI_SHOTS[sh_id] = rec
    t0 = time.time()
    try:
        bus.emit({'type': 'ui-shot', 'id': sh_id, 'sid': sid, 'url': url, 'w': w, 'h': h})
        if not rec['ack'].wait(SHOT_ACK):
            raise ValueError('截图需要 Claude 工作台 App：现在只在浏览器里打开了界面，或者 App 版本太旧，需要重新编译')
        if not rec['done'].wait(max(1.0, SHOT_TIMEOUT - (time.time() - t0))):
            raise ValueError('截图超时（%d 秒）' % SHOT_TIMEOUT)
        if rec['err']:
            raise ValueError('截图失败：' + rec['err'])
        try:
            data = base64.b64decode(rec['png'], validate=True)
        except Exception:
            data = b''
        size = png_size(data)
        if not size:
            raise ValueError('截图失败：外壳返回的不是 PNG')
        tmp = out + '.part'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, out)
        return {'path': out, 'w': size[0], 'h': size[1], 'bytes': len(data)}
    finally:
        with UI_LOCK:
            UI_SHOTS.pop(sh_id, None)


def api_ui_shot_ack(b):
    """页面（原生 App）认领截图请求；多个窗口同时收到时只有第一个去截。"""
    with UI_LOCK:
        rec = UI_SHOTS.get(str(b.get('id') or ''))
        if not rec or rec['acked']:
            return {'go': False}
        rec['acked'] = True
    rec['ack'].set()
    return {'go': True}


def api_ui_shot_done(b):
    with UI_LOCK:
        rec = UI_SHOTS.get(str(b.get('id') or ''))
    if not rec or not rec['acked']:
        return {'ok': False}
    png, err = b.get('png') or '', str(b.get('error') or '')[:300]
    if not err and (not isinstance(png, str) or len(png) > 60 * 1024 * 1024):
        err = '图片太大'
    rec['png'], rec['err'] = ('' if err else png), (err if err or png else '外壳没有返回图片')
    rec['done'].set()
    return {'ok': True}


_register_r11 = register


def register(ns):  # 第十三轮 G：在原有登记之后追加 ui-* 接口
    _register_r11(ns)
    ns['API'].update({'ui-open': api_ui_open, 'ui-web': api_ui_web, 'ui-shot': api_ui_shot,
                      'ui-shot-ack': api_ui_shot_ack, 'ui-shot-done': api_ui_shot_done})


# ================================================================ 第十五轮 E：插件管理（全部经 claude plugin 子命令）、钩子与权限规则只读查看
# 插件的安装、启用、停用、卸载、添加市场会影响所有 Claude Code 会话：接口只在请求里带 confirmed: true（界面确认框点了“确定”）时执行。
# CWB_PLUGIN_DRYRUN=1 只在测试里设：改动类操作只返回拼好的命令行，不执行。
PLUGIN_ID = re.compile(r'^[A-Za-z0-9][\w.\-]{0,127}(@[A-Za-z0-9][\w.\-]{0,127})?$')
PLUGIN_SCOPES = ('user', 'project', 'local')
PLUGIN_DRYRUN = os.environ.get('CWB_PLUGIN_DRYRUN') == '1'
PLUGIN_LOCK = threading.Lock()
PL_AV_LOCK = threading.Lock()
PL = {'avail': None, 'avail_at': 0.0, 'details': {}}
PL_AVAIL_TTL = 600
PL_TIMEOUT = {'list': 20, 'details': 15, 'markets': 20, 'available': 90, 'install': 240, 'enable': 45, 'disable': 45,
              'uninstall': 90, 'market-add': 240}
NET_ERR = re.compile(r'(?i)ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|getaddrinfo|'
                     r'could not resolve|unable to access|failed to connect|fetch failed|network|socket hang up|timed out|'
                     r'certificate|ssl|tls handshake|proxy|rate limit|HTTP 5\d\d')
DET_KEYS = {'skills': 'skills', 'agents': 'agents', 'hooks': 'hooks', 'mcp servers': 'mcp', 'lsp servers': 'lsp',
            'commands': 'commands', 'output styles': 'styles'}
DET_ROW = re.compile(r'^\s*(Skills|Agents|Hooks|MCP servers|LSP servers|Commands|Output styles)\s*\((\d+)\)\s*(.*)$', re.I)


def pl_exec(args, cwd, timeout):
    """运行 claude 子命令，stdout、stderr 分开拿（--json 的输出只在 stdout）。超时、找不到程序都转成中文。"""
    exe = NS.get('CLAUDE') or 'claude'
    wd = cwd if cwd and os.path.isdir(cwd) else home()
    try:
        p = subprocess.run([exe] + args, cwd=wd, env=login_env(), stdin=DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError('等了 %d 秒还没完成，可能是网络不通，稍后再试' % timeout)
    except OSError:
        raise ValueError('找不到 claude 命令')
    ansi = NS.get('ANSI_RE')
    dec = lambda b: (ansi.sub('', b.decode('utf-8', 'replace')) if ansi else b.decode('utf-8', 'replace')).strip()
    return p.returncode, dec(p.stdout), dec(p.stderr)


def pl_json_line(out):
    """--json 的改动类命令在 stdout 打一行 JSON（市场相关的是最后一行）：从后往前找第一行能解析成对象的。"""
    for l in reversed((out or '').split('\n')):
        l = l.strip()
        if l.startswith('{'):
            try:
                d = json.loads(l)
                if isinstance(d, dict):
                    return d
            except ValueError:
                continue
    return None


def pl_err(res, out, err):
    """从命令输出里挑一句人能看懂的错误；联网问题统一说成中文。"""
    msg = ''
    if isinstance(res, dict):
        e = res.get('error')
        if isinstance(e, dict):
            e = e.get('message') or e.get('code')
        msg = str(e or res.get('message') or res.get('reason') or '')
    if not msg:
        msg = last_line(err) or last_line(out)
    if NET_ERR.search(msg + '\n' + (err or '')[-2000:]):
        return '网络连接失败，检查网络或代理后再试'
    return msg[:300] or '未知原因'


def pl_split(pid):
    name, _, mk = pid.partition('@')
    return name, mk


def pl_parse_list(out):
    d = json.loads(out)
    items = d.get('installed') if isinstance(d, dict) else d
    if not isinstance(items, list):
        raise ValueError('输出格式变了')
    res = []
    for x in items:
        if not isinstance(x, dict):
            continue
        pid = str(x.get('id') or x.get('pluginId') or '')
        if not pid:
            continue
        name, mk = pl_split(pid)
        scope = str(x.get('scope') or '')
        res.append({'id': pid, 'name': str(x.get('name') or name), 'market': mk, 'version': str(x.get('version') or ''),
                    'scope': scope, 'enabled': x.get('enabled') is not False, 'path': str(x.get('installPath') or ''),
                    'project': str(x.get('projectPath') or ''), 'readonly': scope not in PLUGIN_SCOPES})
    return res


def pl_parse_details(out):
    counts, names, desc, tokens = {}, {}, '', None
    for l in (out or '').split('\n'):
        m = DET_ROW.match(l)
        if m:
            k = DET_KEYS[m.group(1).lower()]
            counts[k] = int(m.group(2))
            names[k] = [s.strip() for s in m.group(3).split(',') if s.strip()][:200]
            continue
        s = l.strip()
        if s.startswith('Description:') and not desc:
            desc = s[len('Description:'):].strip()
        elif s.startswith('Always-on:') and tokens is None:
            t = re.search(r'~?\s*([\d.]+)\s*(k?)\s*tok', s)
            if t:
                tokens = int(float(t.group(1)) * (1000 if t.group(2) else 1))
    return {'counts': counts, 'names': names, 'desc': desc[:400], 'tokens': tokens}


def pl_command_names(path):
    """插件自带的命令文件（commands/**/*.md）：CLI 的组件清单把命令和技能都算在 Skills 里，这里按文件名分出命令。"""
    root = os.path.join(path or '', 'commands')
    out = set()
    if not path or not os.path.isdir(root):
        return out
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith('.')][:50]
        for f in fns:
            if f.endswith('.md'):
                out.add(f[:-3])
        if len(out) > 500:
            break
    return out


def pl_details(p):
    """组件数量：claude plugin details 的文本输出（这条子命令没有 --json）。按 id、版本、目录修改时间缓存。"""
    try:
        mt = os.path.getmtime(p['path']) if p['path'] else 0
    except OSError:
        mt = 0
    key = (p['id'], p['version'], p['path'], mt)
    hit = PL['details'].get(p['id'])
    if hit and hit[0] == key:
        return hit[1]
    try:
        rc, out, err = pl_exec(['plugin', 'details', p['id']], p.get('project') or home(), PL_TIMEOUT['details'])
    except ValueError:
        return None
    d = pl_parse_details(out)
    if rc != 0 or not d['counts']:
        return None
    sk = d['counts'].get('skills')
    if sk:
        cmds = pl_command_names(p['path'])
        nm = d['names'].get('skills') or []
        c_names = [x for x in nm if x in cmds] if nm else []
        nc = len(c_names) if nm else min(len(cmds), sk)
        d['counts']['commands'] = d['counts'].get('commands', 0) + nc
        d['counts']['skills'] = sk - nc
        if nm:
            d['names']['commands'] = c_names
            d['names']['skills'] = [x for x in nm if x not in cmds]
    PL['details'][p['id']] = (key, d)
    return d


def pl_markets(cwd):
    rc, out, err = pl_exec(['plugin', 'marketplace', 'list', '--json'], cwd, PL_TIMEOUT['markets'])
    if rc != 0:
        raise ValueError(pl_err(None, out, err))
    try:
        d = json.loads(out)
    except ValueError:
        d = None
    if not isinstance(d, list):
        raise ValueError('市场列表的格式和预期不一样')
    res = []
    for x in d:
        if isinstance(x, dict) and x.get('name'):
            src = str(x.get('repo') or x.get('url') or x.get('path') or x.get('source') or '')
            res.append({'name': str(x['name']), 'source': src, 'kind': str(x.get('source') or ''), 'path': str(x.get('installLocation') or '')})
    return res


def plugin_desc(path):
    pj = load_json(os.path.join(path, '.claude-plugin', 'plugin.json')) if path else {}
    return str(pj.get('description') or '')[:400] if isinstance(pj, dict) else ''


def api_plugin_list(b):
    """已装插件（名称、来源市场、版本、范围、启用状态、组件数量）和已添加的市场。两部分各自出错互不影响。"""
    cwd = b.get('cwd') or ''
    cwd = cwd if os.path.isdir(cwd) else ''
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fm = ex.submit(pl_markets, cwd)
        plugins, perr = None, ''
        try:
            rc, out, err = pl_exec(['plugin', 'list', '--json'], cwd, PL_TIMEOUT['list'])
            if rc != 0:
                raise ValueError(pl_err(None, out, err))
            try:
                plugins = pl_parse_list(out)
            except ValueError:
                raise ValueError('插件列表的格式和预期不一样')
        except ValueError as e:
            perr = str(e)
        except Exception:
            perr = '读取插件列表出错'
        try:
            markets, merr = fm.result(), ''
        except Exception as e:
            markets, merr = None, str(e) if isinstance(e, ValueError) else '读取市场列表出错'
    if plugins:
        dets = pmap(lambda p: pl_details(p) if p['path'] or p['id'] else None, plugins, n=4)
        for p, d in zip(plugins, dets):
            p['counts'] = (d or {}).get('counts')
            p['names'] = (d or {}).get('names') or {}
            p['tokens'] = (d or {}).get('tokens')
            p['desc'] = (d or {}).get('desc') or plugin_desc(p['path'])
    return {'plugins': plugins, 'error': perr, 'markets': markets, 'marketsError': merr, 'dry': PLUGIN_DRYRUN}


def pl_slim(x):
    pid = str(x.get('pluginId') or x.get('id') or '')
    name, mk = pl_split(pid)
    n = x.get('installCount')
    return {'id': pid, 'name': str(x.get('name') or name), 'market': str(x.get('marketplaceName') or mk),
            'desc': re.sub(r'\s+', ' ', str(x.get('description') or ''))[:300], 'version': str(x.get('version') or ''),
            'n': n if isinstance(n, int) else 0}


def pl_load_available(refresh):
    with PL_AV_LOCK:
        if not refresh and PL['avail'] is not None and time.time() - PL['avail_at'] < PL_AVAIL_TTL:
            return PL['avail'], False
        try:
            rc, out, err = pl_exec(['plugin', 'list', '--json', '--available'], home(), PL_TIMEOUT['available'])
            if rc != 0:
                raise ValueError(pl_err(None, out, err))
            try:
                d = json.loads(out)
                av = d.get('available') if isinstance(d, dict) else None
                if not isinstance(av, list):
                    raise ValueError
            except ValueError:
                raise ValueError('可安装插件的格式和预期不一样')
            items = [pl_slim(x) for x in av if isinstance(x, dict) and (x.get('pluginId') or x.get('id'))]
            items.sort(key=lambda x: (-x['n'], x['name'].lower()))
            PL['avail'], PL['avail_at'] = items, time.time()
            try:
                cache_put('plugins-available.json', items)
            except Exception:
                pass
            return items, False
        except ValueError:
            old = PL['avail'] if PL['avail'] is not None else cache_get('plugins-available.json', None)
            if old:
                PL['avail'] = old
                return old, True
            raise


def api_plugin_available(b):
    """从已添加的市场浏览可安装的插件：按名称和说明搜索、按市场过滤，每页 40 个。"""
    items, stale = pl_load_available(bool(b.get('refresh')))
    mk = str(b.get('market') or '')
    terms = [t for t in str(b.get('q') or '').lower().split() if t]
    sel = [x for x in items if (not mk or x['market'] == mk) and all(t in (x['name'] + ' ' + x['desc']).lower() for t in terms)]
    try:
        off = max(0, int(b.get('offset') or 0))
    except (TypeError, ValueError):
        off = 0
    cnt = {}
    for x in items:
        cnt[x['market']] = cnt.get(x['market'], 0) + 1
    return {'items': sel[off:off + 40], 'total': len(sel), 'offset': off, 'stale': stale,
            'markets': [{'name': k, 'n': v} for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])]}


def pl_check_id(pid):
    pid = (pid or '').strip()
    if not PLUGIN_ID.match(pid):
        raise ValueError('插件名称不对')
    return pid


def pl_cmd(action, pid='', scope='', source='', accept=''):
    """拼改动类命令的参数（不含 claude 本身）。参数逐个传给子进程，不经 shell。"""
    sc = ['-s', scope] if scope else []
    if action == 'install':
        return ['plugin', 'install', pid, '--json'] + sc + (['--accept-command', accept] if accept else [])
    if action in ('enable', 'disable', 'uninstall'):
        return ['plugin', action, pid, '--json'] + sc
    if action == 'market-add':
        return ['plugin', 'marketplace', 'add', source, '--json']
    raise ValueError('不支持的操作')


def pl_mutate(action, b):
    if b.get('confirmed') is not True:
        raise ValueError('这个操作会影响所有 Claude Code 会话，需要先确认')
    cwd = b.get('cwd') or ''
    pid = source = accept = scope = ''
    if action == 'market-add':
        source = (b.get('source') or '').strip()
        if not source or len(source) > 500 or source.startswith('-') or re.search(r'[\x00-\x1f\s]', source):
            raise ValueError('市场地址不对：填 GitHub 仓库（owner/repo）、git 或 https 地址、本地路径')
    else:
        pid = pl_check_id(b.get('id'))
        scope = b.get('scope') or ('user' if action == 'install' else '')
        if scope and scope not in PLUGIN_SCOPES:
            raise ValueError('这个插件随会话加载，不能在这里改' if scope == 'session' else '范围不对')
        if scope in ('project', 'local') and not os.path.isdir(cwd):
            raise ValueError('先打开一个项目')
        accept = (b.get('accept') or '').strip() if action == 'install' else ''
        if accept and not re.match(r'^[0-9a-f]{64}$', accept):
            raise ValueError('确认码不对')
    args = pl_cmd(action, pid, scope, source, accept)
    wd = cwd if scope in ('project', 'local') else home()
    if PLUGIN_DRYRUN:
        return {'ok': True, 'dry': True, 'cmd': ['claude'] + args, 'cwd': wd}
    if not PLUGIN_LOCK.acquire(blocking=False):
        raise ValueError('上一个插件操作还没完成')
    try:
        rc, out, err = pl_exec(args, wd, PL_TIMEOUT[action])
    finally:
        PLUGIN_LOCK.release()
    res = pl_json_line(out)
    log = NS.get('log')
    if log:
        log('插件操作', action, pid or source, 'rc=%s' % rc)
    PL['details'].pop(pid, None)
    sc = res.get('shownCommand') if isinstance(res, dict) else None
    if action == 'install' and isinstance(sc, dict) and sc.get('sha256') and not accept and (rc != 0 or res.get('ok') is False):
        cmd = sc.get('command') or sc.get('display') or sc.get('text') or sc.get('argv')
        if isinstance(cmd, list):
            cmd = ' '.join(shlex.quote(str(x)) for x in cmd)
        return {'needAccept': {'command': str(cmd or json.dumps(sc, ensure_ascii=False))[:4000], 'sha256': str(sc['sha256'])}}
    failed = rc != 0 or (isinstance(res, dict) and (res.get('ok') is False or res.get('success') is False))
    if failed:
        what = {'install': '安装', 'enable': '启用', 'disable': '停用', 'uninstall': '卸载', 'market-add': '添加市场'}[action]
        raise ValueError('%s失败：%s' % (what, pl_err(res, out, err)))
    if action in ('install', 'uninstall', 'market-add'):
        PL['avail_at'] = 0.0
    return {'ok': True, 'text': (last_line(out) if not res else str(res.get('message') or ''))[:300]}


def api_plugin_install(b):
    return pl_mutate('install', b)


def api_plugin_enable(b):
    return pl_mutate('enable' if b.get('on', True) else 'disable', b)


def api_plugin_uninstall(b):
    return pl_mutate('uninstall', b)


def api_plugin_market_add(b):
    return pl_mutate('market-add', b)


# ---------------------------------------------------------------- 钩子与权限规则（只读）
SECRET_PRE = re.compile(r'(?<![\w-])(sk-ant-[a-z0-9]*-?|sk-proj-|sk-|rk-|sk_live_|pk_live_|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|'
                        r'xox[abposr]-|xapp-|npm_|hf_|AIza|AKIA|ASIA|ya29\.)([\w\-.]{8,})')
SECRET_JWT = re.compile(r'\beyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]{4,}')
SECRET_BEARER = re.compile(r'(?i)\b(bearer|basic|token)(\s+)([\w\-.~+/]{8,}=*)')
SECRET_URL = re.compile(r'(?i)(\b[a-z][\w+.-]*://[^/\s:@]+:)([^@\s/]+)(@)')
SECRET_KV = re.compile(r'(?i)((?:^|(?<=[\s"\'{,;&?(]))[\w.-]*?(?:token|secret|passw(?:or)?d|pwd|api[_-]?key|apikey|access[_-]?key|private[_-]?key|'
                       r'auth(?:orization)?|credentials?|cookie)[\w.-]*["\']?\s*[=:]\s*["\']?(?:(?:bearer|basic|token)\s+)?)(?!\$)([^\s"\'&;,|)}]{4,})')
SECRET_FLAG = re.compile(r'(?i)((?<![\w-])--?[\w-]*(?:token|secret|passw(?:or)?d|api-?key|apikey|access-?key|auth)[\w-]*(?:\s+|=)["\']?)(?![$-])([^\s"\']{4,})')
SECRET_QS = re.compile(r'(?i)([?&][\w.-]*(?:key|token|secret|sig|signature|passw(?:or)?d|auth)[\w.-]*=)(?!\$)([^&\s"\'#]{3,})')
SECRET_LONG = re.compile(r'(?<![\w/.\-•])([A-Za-z0-9_+\-]{32,}={0,2})(?![\w/])')
DOTS = '••••'


def mask_secrets(s):
    """显示前打码：常见令牌前缀、Bearer、URL 里的密码、名字像密钥的赋值和参数、长串随机字符。只留开头几个字符。"""
    if not s:
        return ''
    s = str(s)
    s = SECRET_PRE.sub(lambda m: m.group(1) + DOTS, s)
    s = SECRET_JWT.sub('eyJ' + DOTS, s)
    s = SECRET_BEARER.sub(lambda m: m.group(1) + m.group(2) + DOTS, s)
    s = SECRET_URL.sub(lambda m: m.group(1) + DOTS + m.group(3), s)
    s = SECRET_KV.sub(lambda m: m.group(1) + (m.group(2) if m.group(2) == DOTS else DOTS), s)
    s = SECRET_QS.sub(lambda m: m.group(1) + DOTS, s)
    s = SECRET_FLAG.sub(lambda m: m.group(1) + (m.group(2) if m.group(2) == DOTS else DOTS), s)

    def long(m):
        v = m.group(1)
        if any(c.isdigit() for c in v) and any(c.isalpha() for c in v) and v.count('-') <= 2:
            return v[:4] + DOTS
        return v
    return SECRET_LONG.sub(long, s)


def rules_files(cwd):
    fs = [('user', os.path.join(home(), '.claude', 'settings.json'))]
    if cwd and os.path.isdir(cwd):
        fs += [('project', os.path.join(cwd, '.claude', 'settings.json')), ('local', os.path.join(cwd, '.claude', 'settings.local.json'))]
    out, seen = [], set()
    for k, p in fs:
        rp = os.path.realpath(p)
        if rp in seen:
            continue
        seen.add(rp)
        out.append((k, p))
    return out


def hook_text(hk):
    typ = str(hk.get('type') or 'command')
    if typ == 'command':
        cmd, args = hk.get('command'), hk.get('args')
        if isinstance(args, list) and args:
            return ' '.join(shlex.quote(str(x)) for x in [cmd or ''] + args).strip()
        return str(cmd or '')
    if typ == 'http':
        u = str(hk.get('url') or '')
        hd = hk.get('headers')
        return u + ('  ' + ', '.join('%s: %s' % (k, DOTS) for k in hd) if isinstance(hd, dict) and hd else '')
    if typ in ('prompt', 'agent'):
        return str(hk.get('prompt') or '')
    rest = {k: v for k, v in hk.items() if k not in ('type', 'timeout', 'async', 'if', 'statusMessage')}
    return json.dumps(rest, ensure_ascii=False)


def parse_hooks(h, src, path):
    out = []
    if not isinstance(h, dict):
        return out
    for ev, groups in h.items():
        if not isinstance(groups, list):
            continue
        for g in groups:
            if not isinstance(g, dict):
                continue
            matcher = g.get('matcher')
            for hk in g.get('hooks') or []:
                if not isinstance(hk, dict):
                    continue
                to = hk.get('timeout')
                out.append({'event': str(ev), 'matcher': mask_secrets(matcher if isinstance(matcher, str) else ''),
                            'type': str(hk.get('type') or 'command'), 'text': mask_secrets(hook_text(hk))[:2000],
                            'timeout': to if isinstance(to, (int, float)) and not isinstance(to, bool) else None,
                            'async': hk.get('async') is True, 'if': mask_secrets(hk.get('if') if isinstance(hk.get('if'), str) else ''),
                            'src': src, 'path': path})
                if len(out) >= 500:
                    return out
    return out


def parse_perms(p, src, path):
    out = []
    if not isinstance(p, dict):
        return out
    for kind in ('allow', 'ask', 'deny'):
        rs = p.get(kind)
        for r in rs if isinstance(rs, list) else []:
            if isinstance(r, str) and r.strip():
                out.append({'kind': kind, 'rule': mask_secrets(r.strip())[:1000], 'src': src, 'path': path})
    return out[:1500]


def api_settings_rules(b):
    """读用户级、项目级、项目本机三份设置文件，列出钩子和权限规则（允许 / 询问 / 拒绝）。只读；命令里像密钥的部分打码。"""
    cwd = b.get('cwd') or ''
    files, hooks, perms = [], [], []
    for key, p in rules_files(cwd):
        ent = {'key': key, 'path': p, 'exists': os.path.isfile(p), 'error': '', 'disableAllHooks': False}
        files.append(ent)
        if not ent['exists']:
            continue
        try:
            if os.path.getsize(p) > 2 * 1024 * 1024:
                raise ValueError('文件太大')
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError('不是 JSON 对象')
        except ValueError as e:
            ent['error'] = '格式有误，读不出来' if not str(e) or 'JSON' in str(e) or 'Expecting' in str(e) else str(e)
            continue
        except OSError:
            ent['error'] = '没有权限读取'
            continue
        ent['disableAllHooks'] = d.get('disableAllHooks') is True
        hooks += parse_hooks(d.get('hooks'), key, p)
        perms += parse_perms(d.get('permissions'), key, p)
    return {'files': files, 'hooks': hooks, 'perms': perms}


_register_r13 = register


def register(ns):  # 第十五轮 E：插件、钩子与权限规则
    _register_r13(ns)
    ns['API'].update({'plugin-list': api_plugin_list, 'plugin-available': api_plugin_available, 'plugin-install': api_plugin_install,
                      'plugin-enable': api_plugin_enable, 'plugin-uninstall': api_plugin_uninstall,
                      'plugin-market-add': api_plugin_market_add, 'settings-rules': api_settings_rules})
