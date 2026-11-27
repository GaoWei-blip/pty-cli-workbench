#!/usr/bin/env python3
"""Claude 工作台后端。

- 在伪终端里运行官方交互式 claude，请求头与在终端里手动运行完全一致：claude-cli/<版本> (external, cli)
- 读取 ~/.claude/projects 下的会话记录，转成时间线条目推给界面
- 授权请求通过官方 PermissionRequest 钩子转到界面；其余终端弹窗按屏幕内容识别
"""
import base64
import glob
import hashlib
import itertools
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import pyte
from wcwidth import wcswidth, wcwidth

ROOT = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser('~')
PROJECTS_DIR = os.path.join(HOME, '.claude', 'projects')
RUN_DIR = os.path.join(ROOT, '.run')
CONF_PATH = os.path.join(ROOT, 'config.json')
STATIC_DIR = os.path.join(ROOT, 'static')
HOOK = os.path.join(ROOT, 'hook.py')
PY = sys.executable
PORT = int(os.environ.get('CWB_PORT', '47821'))
TOKEN = secrets.token_urlsafe(24)  # 启动时若已有令牌文件则沿用，重启后已打开的窗口无需重开
BOOT = str(int(time.time()))
EXTRA_SETTINGS = json.loads(os.environ.get('CWB_EXTRA_SETTINGS') or '{}')  # 仅测试用
SHOW_TMP = os.environ.get('CWB_SHOW_TMP') == '1'
COLS, ROWS = int(os.environ.get('CWB_COLS') or 160), 50
MAX_RUNNERS = 4
HIST_LIMIT = 600
IDLE_EXIT = int(os.environ.get('CWB_IDLE_EXIT') or 1800)  # 第十三轮 E：测试时可调短
HIDE_PREFIX = ('/tmp/', '/private/tmp/', '/private/var/folders/', '/var/folders/')
MODE_FLAG = {'auto mode': 'auto', 'manual mode': 'manual', 'accept edits': 'acceptEdits',
             'plan mode': 'plan', 'bypass permissions': 'bypassPermissions'}

LOGIN_ENV = {}
CLAUDE = 'claude'
RUNNERS = {}
NEW_SESS = {}
TRANSCRIPTS = {}
PERMS = {}
STATUS = {'text': ''}
G = threading.RLock()
ALIAS = {}  # /clear 换了会话 ID：旧 ID → 新 ID（钩子环境变量里还是旧 ID）
EXITS = {}  # CLI 意外退出的记录：sid → {code, tail}，再打开这个对话时还能看到
GEN_SEQ = [0]
PAGE = 300  # “显示更早”每次多取的条数

# 第十三轮 E：CLI 由独立的终端守护进程 ptyd.py 持有，server 重启时 CLI 不断；协议版本常量直接从 ptyd.py 取
if ROOT not in sys.path:
    sys.path.append(ROOT)  # -I 模式下脚本目录不在 sys.path
import ptyd as PT  # noqa: E402

import usage as US  # noqa: E402  第十四轮 I：用量统计

PTYD_PY = os.path.join(ROOT, 'ptyd.py')
RUNS_PATH = os.path.join(RUN_DIR, 'usage-runs.jsonl')  # 第十四轮 I：工作台启动 CLI 到 CLI 退出的时间段
KEYS = {}  # ptyd 会话 key（＝启动代次）→ Runner；/clear 换 sid 不影响 key
HOOKS_DIR = os.path.join(RUN_DIR, 'hooks')  # hook.py 发出事件前把最后一次事件写在这里：<代次>.json
MARKS_PATH = os.path.join(RUN_DIR, f'marks-{PORT}.json')
MARKS = {'done_at': {}, 'interrupted': {}}  # done_at：一轮结束的时间（未读）；interrupted：上次回复被非正常结束
SHUTTING = [False]


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 事件总线
class Bus:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.last_seen = time.time()

    def add(self):
        q = queue.Queue(maxsize=20000)
        with self.lock:
            self.clients.append(q)
        return q

    def remove(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)
        self.last_seen = time.time()

    def emit(self, ev):
        data = json.dumps(ev, ensure_ascii=False)
        with self.lock:
            for q in self.clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass


BUS = Bus()


# ---------------------------------------------------------------- 配置与环境
def load_conf():
    try:
        with open(CONF_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'folders': []}


def save_conf(c):
    tmp = CONF_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONF_PATH)


def capture_login_env():
    """取一份和新开终端一致的环境变量，并去掉宿主程序注入的 claude 相关变量。"""
    shell = os.environ.get('SHELL') or '/bin/zsh'
    user = os.environ.get('USER') or os.path.basename(HOME)
    base = {'HOME': HOME, 'USER': user, 'LOGNAME': user, 'SHELL': shell, 'TERM': 'xterm-256color',
            'LANG': os.environ.get('LANG') or 'zh_CN.UTF-8', 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}
    env = None
    try:
        p = subprocess.run([shell, '-l', '-i', '-c', 'printf __CWB__; env -0'], env=base,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        out = p.stdout.decode('utf-8', 'ignore')
        if '__CWB__' in out:
            out = out.split('__CWB__', 1)[1]
            env = dict(x.split('=', 1) for x in out.split('\0') if '=' in x)
    except Exception as e:
        log('读取登录环境失败', e)
    if not env:
        env = dict(base)
        env['PATH'] = os.environ.get('PATH', base['PATH'])
    for k in list(env):
        if k.startswith(('CLAUDE_CODE_', 'CLAUDE_AGENT_SDK', 'VSCODE_')) or k in (
                'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_EFFORT', 'TERM_PROGRAM', 'TERM_PROGRAM_VERSION'):
            env.pop(k, None)
    env.update(TERM='xterm-256color', COLORTERM='truecolor')
    env.setdefault('LANG', 'zh_CN.UTF-8')
    return env


def build_settings(opts=None):
    """--settings 的内容：钩子；第十四轮 I：对话开了 Fast、模型又支持时加 fastMode（CLI 的 IH() 见到
    flagSettings.fastMode===true 会跳过组织状态检查）。只按对话生效，不写 ~/.claude/settings.json。"""
    def h(ev, timeout):
        return {'type': 'command', 'command': f'{shlex.quote(PY)} -I {shlex.quote(HOOK)} {ev}', 'timeout': timeout}
    s = {'hooks': {
        'PermissionRequest': [{'matcher': '*', 'hooks': [h('permission', 3600)]}],
        'UserPromptSubmit': [{'hooks': [h('busy', 10)]}],
        'Stop': [{'hooks': [h('idle', 10)]}],
        'StopFailure': [{'hooks': [h('fail', 10)]}],  # 接口出错结束本轮：载荷 error=rate_limit/authentication_failed/server_error/…
        # matcher 匹配的是 notification_type（实测），空闲提醒 idle_prompt 不要
        'Notification': [{'matcher': 'permission_prompt|elicitation_dialog', 'hooks': [h('notification', 10)]}],
        'SessionStart': [{'hooks': [h('session', 10)]}],
        # 第十五轮 A2：旁问答案（SubagentStop）、添加文件夹确认、压缩开始与结束；只做观测，后台跑不拖 CLI
        'SubagentStop': [{'hooks': [dict(h('subagent', 10), **{'async': True})]}],
        'DirectoryAdded': [{'hooks': [dict(h('dir', 10), **{'async': True})]}],
        'PreCompact': [{'hooks': [dict(h('compact-start', 10), **{'async': True})]}],
        'PostCompact': [{'hooks': [dict(h('compact-end', 10), **{'async': True})]}],
    }}
    if opts and opts.get('fast') and fast_ok(opts):
        s['fastMode'] = True
    CX.inject(s)  # 第十五轮 A1：偏好开着时注入状态行（上下文用量），原来的状态行照常输出
    PS.inject(s)  # 11-18 个性化：选了输出风格就按对话注入（不写全局设置）
    for k, v in EXTRA_SETTINGS.items():
        s[k] = v
    return s


# ---------------------------------------------------------------- 第十四轮 I：对话选项与偏好
OPT_KEYS = ('model', 'effort', 'mode', 'fast')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


def merge_opts(*layers):
    """按顺序叠加对话选项（后面的覆盖前面的）：null、空串不覆盖（偏好里选“默认”存的是 null）；值为 default 表示“不指定”（清掉下层的）；
    fast 只认布尔值，给了就覆盖，结果里只留 True。"""
    out = {}
    for lay in layers:
        for k in OPT_KEYS:
            if not isinstance(lay, dict) or k not in lay:
                continue
            v = lay[k]
            if k == 'fast':
                if isinstance(v, bool):  # 只认布尔值：null、空串等算未设置
                    out['fast'] = v
            elif v == 'default':
                out.pop(k, None)
            elif v:
                out[k] = v
    if not out.get('fast'):
        out.pop('fast', None)
    return out


def opts_out(opts):
    """返回给界面的对话选项：fast 总是带上（布尔）。"""
    return dict(opts or {}, fast=bool((opts or {}).get('fast')))


def opts_key_of(opts):
    return json.dumps({k: opts.get(k) for k in OPT_KEYS if opts.get(k)}, sort_keys=True)


MODEL_ID_RE = re.compile(r'^[\w.\-\[\]:/@]{1,100}$')


def load_prefs(c=None):
    p = (c if c is not None else load_conf()).get('prefs') or {}
    d = p.get('defaults') if isinstance(p.get('defaults'), dict) else {}
    out = {'defaults': {'model': d.get('model') or None, 'effort': d.get('effort') or None, 'mode': d.get('mode') or None,
                        'fast': bool(d.get('fast'))},
           'defaultCwd': p.get('defaultCwd') or None,
           'extraModels': [x for x in (p.get('extraModels') or []) if isinstance(x, str)]}
    if p.get('ctxStatus') is False:  # 第十五轮 A1：上下文用量（状态行）默认开，只在关掉时带 ctxStatus: false
        out['ctxStatus'] = False
    return out


MODE_OF_FLAG = {v: k for k, v in MODE_FLAG.items()}


def cli_default_mode(st=None):
    """CLI 全局默认权限：~/.claude/settings.json 的 permissions.defaultMode 换成前端 #mode 的取值；没设置或认不出时为 None。只读。"""
    st = st if st is not None else user_settings()
    perms = st.get('permissions') if isinstance(st.get('permissions'), dict) else {}
    v = perms.get('defaultMode')
    if v == 'default':
        return 'default'
    return MODE_OF_FLAG.get(v) if isinstance(v, str) else None


def pref_defaults():
    """新对话默认值（对话选项格式）。"""
    return merge_opts(load_prefs()['defaults'])


QUOTA_FILE = os.path.join(HOME, '.claude', '.crs-quota.json')
QUOTA_LBL = {'5h': '5 小时', '7d': '7 天'}


def api_quota(b):
    """第十四轮补：额度。只读用户状态栏脚本（~/.claude/crs-statusline.sh）刷新时顺带写的 ~/.claude/.crs-quota.json，
    里面只有数字（w: [{lbl, u 已用%, r 重置时间, t 窗口秒数}]），不含令牌；工作台自己不向网关查询。
    余裕＝窗口已过去的百分比－已用百分比（与状态栏的“余裕”一致）。"""
    try:
        with open(QUOTA_FILE, encoding='utf-8') as f:
            d = json.load(f)
    except FileNotFoundError:
        return {'ok': False, 'reason': 'none', 'windows': []}
    except Exception:
        return {'ok': False, 'reason': 'bad', 'windows': []}
    now = time.time()
    ts = d.get('ts') if isinstance(d.get('ts'), (int, float)) else 0
    ws = []
    for w in d.get('w') or []:
        if not isinstance(w, dict) or not isinstance(w.get('u'), (int, float)):
            continue
        lbl = str(w.get('lbl') or '')
        r, t = w.get('r'), w.get('t')
        el = None
        if isinstance(r, (int, float)) and isinstance(t, (int, float)) and t > 0:
            el = max(0.0, min(100.0, (t - max(0, r - now)) / t * 100))
        ws.append({'key': lbl, 'label': QUOTA_LBL.get(lbl) or lbl,
                   'used': w['u'], 'resetAt': r if isinstance(r, (int, float)) else None, 'window': t,
                   'headroom': round(el - w['u']) if el is not None else None})
    return {'ok': not d.get('err'), 'err': d.get('err') or '', 'ts': ts, 'age': round(now - ts) if ts else None,
            'stale': bool(ts) and now - ts > 900, 'windows': ws}


def api_prefs(b):
    """偏好：{} 读取；{set: {...}} 合并保存。defaults 按字段合并，空值表示清除；extraModels 整体替换。"""
    st = b.get('set')
    if isinstance(st, dict):
        with G:
            c = load_conf()
            cur = load_prefs(c)
            if isinstance(st.get('defaults'), dict):
                for k, v in st['defaults'].items():
                    if k == 'fast':
                        cur['defaults']['fast'] = v is True  # null 算未设置（＝不开）
                    elif k == 'model':
                        if v and (not isinstance(v, str) or not MODEL_ID_RE.match(v)):
                            raise ValueError('模型 ID 不合法')
                        cur['defaults']['model'] = v or None
                    elif k == 'effort':
                        if v and v not in EFFORTS:
                            raise ValueError('思考强度不合法')
                        cur['defaults']['effort'] = v or None
                    elif k == 'mode':
                        if v and v != 'default' and v not in MODE_FLAG:
                            raise ValueError('权限模式不合法')
                        cur['defaults']['mode'] = None if v in (None, '', 'default') else v
            if 'defaultCwd' in st:
                v = st.get('defaultCwd') or None
                if v and (not isinstance(v, str) or not os.path.isabs(v)):
                    raise ValueError('默认文件夹需要是完整路径')
                cur['defaultCwd'] = (v.rstrip('/') or '/') if v else None
            if 'extraModels' in st:
                xs = st.get('extraModels') or []
                if not isinstance(xs, list) or any(not isinstance(x, str) or not MODEL_ID_RE.match(x.strip()) for x in xs):
                    raise ValueError('模型 ID 不合法')
                cur['extraModels'] = list(dict.fromkeys(x.strip() for x in xs if x.strip()))
            if 'ctxStatus' in st:  # 第十五轮 A1：false 关掉，其余（true、null）回到默认开
                cur.pop('ctxStatus', None)
                if st.get('ctxStatus') is False:
                    cur['ctxStatus'] = False
                CX.PREF['t'] = 0
            c['prefs'] = cur
            save_conf(c)
        if 'defaults' in st or 'ctxStatus' in st:  # 默认值变了：按旧默认值预热的 CLI 不再适用
            for k, r in list(PREWARM.items()):
                PREWARM.pop(k, None)
                r.stop()
    return {'prefs': load_prefs(), 'cli': {'mode': cli_default_mode()}}


# ---------------------------------------------------------------- 会话记录解析
TAG_RE = re.compile(r'<(system-reminder|ide_selection|ide_opened_file|ide_diagnostics|command-message|'
                    r'local-command-caveat|user-prompt-submit-hook)>.*?</\1>', re.S)
PASTE_RE = re.compile(r'<pasted_content[^>]*>(.*?)</pasted_content(?:\s[^>]*)?>', re.S)
CMD_NAME_RE = re.compile(r'<command-name>(.*?)</command-name>', re.S)
CMD_ARGS_RE = re.compile(r'<command-args>(.*?)</command-args>', re.S)
CMD_OUT_RE = re.compile(r'<local-command-(stdout|stderr)>(.*?)</local-command-\1>', re.S)
ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
SKIP_ROW_RE = re.compile(rb'^\{"type":"(?!user"|assistant"|attachment")')
IMG_TAG_RE = re.compile(r'\[Image(?: #\d+)?(?::[^\]]*)?\]\s*')
# 第十五轮 A2：钩子执行成功的提示行（如压缩后的“PreCompact [<钩子命令完整路径>] completed successfully”），不显示到时间线
HOOK_OK_RE = re.compile(r'^[ \t]*[A-Z][A-Za-z]+ \[[^\n]*\] completed successfully[ \t]*$\n?', re.M)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
    return ''


CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
INPUT_CTRL_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def safe_input(t):
    """要写进 CLI 的用户文字：去掉 ESC、C0、C1 控制字符，只留换行和制表符。
    否则文字里的 ESC[201~ 会提前结束粘贴，后面的字被 CLI 当成按键执行。"""
    t = (t or '').replace('\r\n', '\n').replace('\r', '\n')
    return INPUT_CTRL_RE.sub('', t)


def paste_bytes(t):
    return b'\x1b[200~' + safe_input(t).encode('utf-8') + b'\x1b[201~'


def clean_text(t):
    t = PASTE_RE.sub(lambda m: m.group(1), t or '')
    t = TAG_RE.sub('', t)
    t = ANSI_RE.sub('', t)
    return CTRL_RE.sub('', t).strip()


def base_tool(name):
    """mcp__服务名__工具名 → 工具名，便于按原生工具渲染。"""
    if name.startswith('mcp__'):
        parts = name.split('__', 2)
        if len(parts) == 3:
            return parts[2]
    return name


ERR_ZH = {'rate_limit': '接口限流', 'authentication_failed': '鉴权失败', 'oauth_org_not_allowed': '鉴权失败',
          'verification_required': '鉴权失败', 'account_on_hold': '鉴权失败', 'cloud_credential_error': '鉴权失败',
          'billing_error': '额度不足', 'invalid_request': '请求被拒绝', 'model_not_found': '请求被拒绝',
          'server_error': '网关错误', 'max_output_tokens': '输出超长'}


def err_label(kind, text=''):
    """接口错误归类：CLI 在 StopFailure 钩子和记录的错误行里都给 error（rate_limit、server_error、unknown…），unknown 再看状态码。"""
    if kind in ERR_ZH:
        return ERR_ZH[kind]
    m = re.search(r'\b([45]\d\d)\b', text or '')
    code = int(m.group(1)) if m else 0
    if code == 429:
        return '接口限流'
    if code in (401, 403):
        return '鉴权失败'
    if 400 <= code < 500:
        return '请求被拒绝'
    if code >= 500:
        return '网关错误'
    return '接口出错'


def ts_of(r):
    try:
        return datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def edit_lines(name, inp):
    """第十二轮 D：改文件工具的增删行数 [+, -]，按完整输入算（界面上的差异框只带前 3000 字）。"""
    import difflib

    def count(old, new):
        a, b = (old or '').splitlines(), (new or '').splitlines()
        i = 0
        while i < len(a) and i < len(b) and a[i] == b[i]:
            i += 1
        j = 0
        while j < len(a) - i and j < len(b) - i and a[-1 - j] == b[-1 - j]:
            j += 1
        a, b = a[i:len(a) - j], b[i:len(b) - j]
        if not a or not b or len(a) * len(b) > 250000:
            return [len(b), len(a)]
        add = rm = 0
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag != 'equal':
                rm += i2 - i1
                add += j2 - j1
        return [add, rm]
    if name == 'Write':
        return [len((inp.get('content') or '').splitlines()), 0]
    if name == 'Edit':
        return count(inp.get('old_string'), inp.get('new_string'))
    tot = [0, 0]
    for e in inp.get('edits') or []:
        if isinstance(e, dict):
            c = count(e.get('old_string'), e.get('new_string'))
            tot = [tot[0] + c[0], tot[1] + c[1]]
    return tot


def tool_view(name, inp):
    v = {'desc': '', 'in': '', 'path': None}
    if name in ('Edit', 'Write', 'MultiEdit'):
        v['ln'] = edit_lines(name, inp)  # 第十二轮 D
    if name == 'Bash':
        v['desc'] = inp.get('description') or ''
        v['in'] = inp.get('command', '')
    elif name in ('Read', 'Write', 'Edit', 'NotebookEdit'):
        v['path'] = inp.get('file_path') or inp.get('notebook_path')
        if name == 'Write':
            v['in'] = (inp.get('content') or '')[:3000]
        if name == 'Edit':
            v['diff'] = {'old': (inp.get('old_string') or '')[:3000], 'new': (inp.get('new_string') or '')[:3000]}
    elif name in ('Grep', 'Glob'):
        v['desc'] = inp.get('pattern', '')
        v['path'] = inp.get('path')
    elif name == 'WebSearch':
        v['desc'] = inp.get('query', '')
    elif name == 'WebFetch':
        v['desc'] = inp.get('url', '')
    elif name in ('Agent', 'Task'):
        v['desc'] = inp.get('description', '')
        v['in'] = (inp.get('prompt') or '')[:2000]
    elif name == 'TodoWrite':
        v['todos'] = [{'c': x.get('content') or x.get('activeForm') or '', 's': x.get('status')} for x in inp.get('todos', [])]
    elif name == 'Skill':
        v['desc'] = inp.get('skill', '')
    elif name == 'AskUserQuestion':
        v['desc'] = '；'.join(q.get('question', '') for q in inp.get('questions', []))
    elif name == 'ExitPlanMode':
        v['in'] = (inp.get('plan') or '')[:4000]
    elif name == 'ToolSearch':
        v['desc'] = inp.get('query', '')
    else:
        v['in'] = json.dumps(inp, ensure_ascii=False)[:800]
    return v


def result_text(b, tur):
    if isinstance(tur, dict) and ('stdout' in tur or 'stderr' in tur):
        o = tur.get('stdout') or ''
        if tur.get('stderr'):
            o += ('\n' if o else '') + tur['stderr']
        return clean_text(o)
    c = b.get('content')
    if isinstance(c, str):
        return clean_text(c)
    if isinstance(c, list):
        parts = []
        for x in c:
            if isinstance(x, dict):
                if x.get('type') == 'text':
                    parts.append(x.get('text', ''))
                elif x.get('type') == 'image':
                    parts.append('[图片]')
        return clean_text('\n'.join(parts))
    return ''


class Conv:
    """把会话记录逐行转成时间线条目。"""

    def __init__(self, sidechain=False):
        self.items, self.pending, self.prev_t, self.n, self.model = [], {}, None, 0, None
        self.sidechain, self.turn = sidechain, None
        self.open = False  # 这一轮是否还没答完（看 stop_reason）

    def _close_pending(self, out, why):
        """这一轮已经结束（或被中断、开始了新一轮），还没拿到结果的工具不再算“运行中”。"""
        for it in list(self.pending.values()):
            it['done'], it['noresult'] = True, why
            out['patch'].append({'id': it['id'], 'done': True, 'noresult': why})
        self.pending.clear()

    def _stat(self, out, t, usage=None, mid=None):
        """每轮统计：从你发出这条消息到最后一步的用时、输出 token、上下文大小。"""
        tr = self.turn
        if not tr or not t:
            return
        tr['t1'] = t
        if usage and mid:
            tr['msgs'][mid] = usage
        u = list(tr['msgs'].values())
        last = u[-1] if u else {}
        st = {'secs': max(0, round(tr['t1'] - tr['t0'])), 'out': sum(x.get('output_tokens') or 0 for x in u),
              'ctx': (last.get('input_tokens') or 0) + (last.get('cache_read_input_tokens') or 0) + (last.get('cache_creation_input_tokens') or 0)}
        tr['item']['stat'] = st
        out['patch'].append({'id': tr['item']['id'], 'stat': st})

    def _new(self, it, out):
        if not it.get('id'):
            self.n += 1
            it['id'] = 'i%d' % self.n
        self.items.append(it)
        out['new'].append(it)

    def _fork_at(self, out, u):
        """这一轮答完了：记下最后一条记录的 uuid，界面上“从这里分叉”用；本轮又继续、出错或中断就收回。"""
        tr = self.turn
        if not tr or self.sidechain or tr['item'].get('fork') == u:
            return
        tr['item']['fork'] = u
        out['patch'].append({'id': tr['item']['id'], 'fork': u})

    def feed(self, r, out, off=None):
        typ, t = r.get('type'), ts_of(r)
        if typ == 'attachment':
            # 回复进行中发的消息：CLI 在下一步工具结果时把它插进当前回复，记录成 queued_command 附件，不是普通用户消息
            a = r.get('attachment')
            if isinstance(a, dict) and a.get('type') == 'queued_command' and (self.sidechain or not r.get('isSidechain')):
                pr = a.get('prompt')
                raw = pr if isinstance(pr, str) else text_of(pr if isinstance(pr, list) else [])
                nimg = sum(1 for x in pr if isinstance(x, dict) and x.get('type') == 'image') if isinstance(pr, list) else 0
                o = a.get('origin') if isinstance(a.get('origin'), dict) else None
                if (a.get('commandMode') or 'prompt') == 'prompt' and not (o and o.get('kind') not in (None, 'human')):
                    txt = clean_text(IMG_TAG_RE.sub('', raw))
                    if txt or nimg:
                        self._new({'k': 'user', 'text': txt, 'imgs': nimg, 'stat': None, 'mid': True}, out)
                else:  # 子代理交回结果、后台任务通知等：不是你发的
                    self._new(sys_note(o or {'kind': a.get('commandMode')}, raw), out)
                self.open = True
                self._fork_at(out, None)
            return
        if typ not in ('user', 'assistant') or (r.get('isSidechain') and not self.sidechain) or r.get('isCompactSummary'):
            return
        msg = r.get('message') or {}
        content = msg.get('content')
        if typ == 'assistant':
            if msg.get('model') and not str(msg['model']).startswith('<'):
                self.model = msg['model']
            if isinstance(msg.get('usage'), dict):
                self._stat(out, t, msg['usage'], msg.get('id'))
            sr = msg.get('stop_reason')
            if sr in ('end_turn', 'stop_sequence', 'max_tokens', 'refusal') and not r.get('isApiErrorMessage'):
                self.open = False
                self._close_pending(out, '没有返回结果')
                if r.get('uuid'):
                    self._fork_at(out, r['uuid'])
            elif sr:
                self.open = True
                self._fork_at(out, None)
            if r.get('isApiErrorMessage'):
                self.open = False
                self._close_pending(out, '出错，未完成')
                self._fork_at(out, None)
                txt = text_of(content)
                self._new({'k': 'error', 'text': txt, 'etype': err_label(r.get('error'), txt)}, out)
            else:
                for c in content or []:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get('type')
                    if ct in ('thinking', 'redacted_thinking'):
                        secs = round(t - self.prev_t) if (t and self.prev_t) else 0
                        self._new({'k': 'thought', 'secs': max(1, secs), 'text': (c.get('thinking') or '')[:20000]}, out)
                    elif ct == 'text' and (c.get('text') or '').strip():
                        self._new({'k': 'text', 'text': c['text']}, out)
                    elif ct == 'tool_use':
                        inp = c.get('input') or {}
                        it = {'k': 'tool', 'id': c.get('id'), 'name': c.get('name') or '', 'done': False,
                              'out': '', 'error': False, 'bg': bool(inp.get('run_in_background'))}
                        it['name'] = base_tool(it['name'])
                        it.update(tool_view(it['name'], inp))
                        it['in'] = CTRL_RE.sub('', it.get('in') or '')
                        self.pending[c.get('id')] = it
                        self._new(it, out)
        else:
            if r.get('isMeta'):
                if t:
                    self.prev_t = t
                return
            blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content or ''}]
            results = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_result']
            for bi, b in enumerate(blocks):
                if not (isinstance(b, dict) and b.get('type') == 'tool_result'):
                    continue
                it = self.pending.pop(b.get('tool_use_id'), None)
                if not it:
                    continue
                it['out'] = result_text(b, r.get('toolUseResult'))[:8000]
                it['error'] = bool(b.get('is_error'))
                it['done'] = True
                tur = r.get('toolUseResult')
                if isinstance(tur, dict) and tur.get('agentId'):
                    it['agent'] = tur['agentId']
                sub = b.get('content') if isinstance(b.get('content'), list) else []
                it['imgs'] = [{'o': off, 'p': [bi, j]} for j, x in enumerate(sub)
                              if off is not None and isinstance(x, dict) and x.get('type') == 'image'][:6]
                out['patch'].append({'id': it['id'], 'out': it['out'], 'error': it['error'], 'done': True, 'imgs': it['imgs'],
                                     'agent': it.get('agent')})
            if results:
                self._stat(out, t)
            so = None if results else system_origin(r)
            if so:
                # 不是你发的：CLI 自动插入的通知，模型会接着回应，算新一轮但不画成你的消息
                it = sys_note(so, text_of(blocks))
                it['turn'] = True  # 这条通知开启了新的一轮（模型会接着回应）
                it['uuid'] = r.get('uuid') or ''  # 第十五轮 A2：和 turn-changes 对上
                self._close_pending(out, '没有返回结果')
                self._new(it, out)
                self.turn = {'item': it, 't0': t, 't1': t, 'msgs': {}}
                self.open = True
            elif not results:
                raw = text_of(blocks)
                imgs = [{'o': off, 'p': [bi]} for bi, b in enumerate(blocks)
                        if off is not None and isinstance(b, dict) and b.get('type') == 'image']
                if imgs:
                    raw = IMG_TAG_RE.sub('', raw)
                if '<command-name>' in raw:
                    name = CMD_NAME_RE.search(raw)
                    args = CMD_ARGS_RE.search(raw)
                    txt = ((name.group(1) if name else '') + ' ' + (args.group(1) if args else '')).strip()
                    if txt:
                        self._new({'k': 'cmd', 'text': txt, 'uuid': r.get('uuid') or ''}, out)  # 第十五轮 A2：uuid
                elif '<local-command-' in raw:
                    for m in CMD_OUT_RE.finditer(raw):
                        txt = HOOK_OK_RE.sub('', ANSI_RE.sub('', m.group(2))).strip()  # 第十五轮 A2：去掉钩子执行成功的提示行
                        if txt:
                            it = {'k': 'note', 'text': txt[:2000]}
                            if t and txt.startswith('Compacted'):
                                it['at'] = int(t * 1000)  # 修复线：压缩完成的时间，界面据此对上落盘的压缩摘要
                            self._new(it, out)
                else:
                    txt = clean_text(raw)
                    if txt.startswith('[Request interrupted'):
                        self.open = False
                        self._close_pending(out, '已中断')
                        self._fork_at(out, None)
                        self._new({'k': 'note', 'text': '已中断'}, out)
                    elif txt or imgs:
                        self._close_pending(out, '没有返回结果')
                        it = {'k': 'user', 'text': txt, 'imgs': imgs, 'stat': None, 'uuid': r.get('uuid') or ''}  # 第十五轮 A2：uuid
                        self._new(it, out)
                        self.turn = {'item': it, 't0': t, 't1': t, 'msgs': {}}
                        self.open = True
        if t:
            self.prev_t = t


OWN_PATH = os.path.join(RUN_DIR, 'own-mtimes.json')
try:
    with open(OWN_PATH) as _f:
        OWN = json.load(_f)
except Exception:
    OWN = {}


def note_own(sid):
    """工作台自己的 CLI 退出（含服务重启）时记下记录文件的修改时间：之后打开不再误判成“在别的窗口里运行”。"""
    try:
        p = find_transcript(sid)
        if not p:
            return
        OWN[sid] = os.path.getmtime(p)
        for k in sorted(OWN, key=OWN.get)[:-300]:
            OWN.pop(k, None)
        tmp = OWN_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(OWN, f)
        os.replace(tmp, OWN_PATH)
    except Exception:
        pass


def find_transcript(sid):
    try:
        for d in os.listdir(PROJECTS_DIR):
            p = os.path.join(PROJECTS_DIR, d, sid + '.jsonl')
            if os.path.exists(p):
                return p
    except OSError:
        pass
    return None


class Transcript:
    def __init__(self, sid, path=None):
        self.sid, self.path = sid, path
        self.conv, self.offset, self.buf = Conv(), 0, b''
        self.lock = threading.Lock()
        self.touched = time.time()

    def poll(self, emit=True):
        with self.lock:
            if not self.path or not os.path.exists(self.path):
                self.path = find_transcript(self.sid)
                if not self.path:
                    return
            try:
                size = os.path.getsize(self.path)
            except OSError:
                return
            if size < self.offset:
                self.conv, self.offset, self.buf = Conv(), 0, b''
                if emit:
                    BUS.emit({'type': 'reset', 'sid': self.sid})
                    emit = False
            if size == self.offset:
                return
            with open(self.path, 'rb') as f:
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
            pos = self.offset - len(self.buf)
            self.offset = size
            lines = (self.buf + chunk).split(b'\n')
            self.buf = lines.pop()
            out = {'new': [], 'patch': []}
            for line in lines:
                off, pos = pos, pos + len(line) + 1
                if not line.strip() or SKIP_ROW_RE.match(line):
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                self.conv.feed(r, out, off)
            if emit and (out['new'] or out['patch']):
                BUS.emit({'type': 'items', 'sid': self.sid, 'new': out['new'], 'patch': out['patch'], 'open': self.conv.open})


def get_transcript(sid, path=None):
    with G:
        t = TRANSCRIPTS.get(sid)
        if not t:
            t = TRANSCRIPTS[sid] = Transcript(sid, path)
        elif path and not t.path:
            t.path = path
        t.touched = time.time()
        return t


# ---------------------------------------------------------------- 对话列表
def iter_rows(buf):
    for line in buf.split(b'\n'):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def system_origin(r):
    """CLI 自己插进对话的消息（后台任务通知、子代理交回结果等）：origin.kind 不是 human，或 promptSource 是 system。"""
    o = r.get('origin')
    kind = o.get('kind') if isinstance(o, dict) else None
    if (kind and kind != 'human') or r.get('promptSource') == 'system':
        return o if isinstance(o, dict) else {'kind': 'system'}
    return None


SYS_TAG_RE = re.compile(r'<(summary|status|task-id)>(.*?)</\1>', re.S)
STATUS_ZH = {'completed': '已完成', 'stopped': '已停止', 'failed': '失败', 'killed': '已停止', 'error': '出错'}


def sys_note(o, raw):
    """把系统插入的消息整理成一行提示 + 可展开的原文。"""
    kind = o.get('kind') or 'system'
    tags = {m.group(1): m.group(2).strip() for m in SYS_TAG_RE.finditer(raw)}
    body = clean_text(raw)
    if kind == 'task-notification':
        st = STATUS_ZH.get(tags.get('status', ''), tags.get('status', ''))
        text = '后台任务' + (st and ' ' + st) + (('：' + tags['summary']) if tags.get('summary') else '')
        tag = '后台任务通知'
    elif kind == 'peer':
        text = '子代理交回结果' + (('（' + o['name'] + '）') if o.get('name') else '')
        tag = '子代理'
    else:
        text = body.split('\n')[0][:120]
        tag = '系统消息'
    return {'k': 'sys', 'tag': tag, 'text': text[:300], 'body': body[:20000]}


def first_prompt(d):
    if d.get('type') != 'user' or d.get('isMeta') or d.get('isSidechain') or system_origin(d):
        return None
    c = (d.get('message') or {}).get('content')
    if isinstance(c, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c):
        return None
    t = text_of(c)
    if '<command-name>' in t or '<local-command' in t:
        return None
    t = clean_text(t)
    return t[:80] if t else None


HEAD_META = {}  # 记录文件 -> (开头的 cwd, 第一条提问)，写下后不会再变


def head_meta(fp, limit=64 << 20):
    """从文件开头逐行找第一条带 cwd 的记录和第一条提问。
    11-17：贴了图的第一条提问单行可达几百 KB，超过 read_meta 读的头部 256KB，头部里一条完整的带 cwd 的记录都没有，原来就拿尾部
    第一条记录的 cwd 顶替——CLI 中途 cd 进了子目录的话，这条对话会被归到子目录名的“项目”下，原项目里又多出一条“新对话”。"""
    c = HEAD_META.get(fp)
    if c:
        return c
    cwd = first = None
    read = 0
    try:
        with open(fp, 'rb') as f:
            for line in f:
                read += len(line)
                if line.strip():
                    try:
                        d = json.loads(line)
                    except Exception:
                        d = None
                    if isinstance(d, dict):
                        cwd = cwd or d.get('cwd') or None
                        first = first or first_prompt(d)
                if (cwd and first) or read > limit:
                    break
    except OSError:
        return None, None
    if cwd:
        HEAD_META[fp] = (cwd, first)
    return cwd, first


def read_meta(fp, size):
    try:
        with open(fp, 'rb') as f:
            if size <= 800_000:
                parts = [f.read()]
            else:
                head = f.read(262_144)
                f.seek(size - 524_288)
                parts = [head, f.read()]
    except OSError:
        return None
    m = {'cwd': None, 'first': None, 'custom': None, 'ai': None, 'last': None, 'last_at': None}
    for i, part in enumerate(parts):
        if i == 1 and not (m['cwd'] and m['first']):  # 11-17：头部没读到，从文件开头逐行找，不能拿尾部的顶替（见 head_meta）
            c0, f0 = head_meta(fp)
            m['cwd'], m['first'] = m['cwd'] or c0, m['first'] or f0
        for d in iter_rows(part):
            if not m['cwd'] and d.get('cwd'):
                m['cwd'] = d['cwd']
            if not m['first']:
                m['first'] = first_prompt(d)
            t = d.get('type')
            if t in ('user', 'assistant') and not d.get('isSidechain') and not d.get('isMeta') and d.get('timestamp'):
                m['last_at'] = d['timestamp']
            if t == 'custom-title' and d.get('customTitle'):
                m['custom'] = d['customTitle']
            elif t == 'ai-title' and d.get('aiTitle'):
                m['ai'] = d['aiTitle']
            elif t == 'last-prompt' and d.get('lastPrompt'):
                m['last'] = d['lastPrompt']
    m['title'] = ' '.join((m['custom'] or m['ai'] or m['first'] or (m['last'] or '')[:80] or '').split())
    m['last_at'] = iso_epoch(m['last_at'])
    return m


def iso_epoch(ts):
    """记录里的 ISO 时间（…Z）→ 秒；认不出返回 None。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


class Index:
    def __init__(self):
        self.cache, self.sessions, self.t = {}, [], 0
        self.lock = threading.Lock()

    def scan(self, force=False):
        with self.lock:
            if not force and time.time() - self.t < 3:
                return self.sessions
            out = []
            try:
                dirs = os.listdir(PROJECTS_DIR)
            except OSError:
                dirs = []
            for d in dirs:
                p = os.path.join(PROJECTS_DIR, d)
                if not os.path.isdir(p):
                    continue
                try:
                    names = os.listdir(p)
                except OSError:
                    continue
                for n in names:
                    if not n.endswith('.jsonl'):
                        continue
                    fp = os.path.join(p, n)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    c = self.cache.get(fp)
                    if c and c[0] == st.st_mtime and c[1] == st.st_size:
                        m = c[2]
                    else:
                        m = read_meta(fp, st.st_size)
                        self.cache[fp] = (st.st_mtime, st.st_size, m)
                    if not m or not m['cwd'] or not m['title']:
                        continue
                    t = m['title'] if (m['custom'] or m['ai']) else (auto_title(n[:-6]) or m['title'])  # 11-11：没有 CLI 标题时用自动标题
                    out.append({'sid': n[:-6], 'path': fp, 'cwd': remap_cwd(m['cwd']), 'title': t,
                                'mtime': m.get('last_at') or st.st_mtime})  # 11-11：项目改过名的换成新路径
                    # 11-18：时间取最后一条提问或回复的时间。CLI 续接、退出时会往记录里补写 cost-state 等行，文件修改时间跟着变，
                    # 原来左栏显示的是这个时间，没说话的对话也跑到“今天”
            self.sessions, self.t = out, time.time()
            return out

    def get(self, sid, force=False):
        for s in self.scan(force):
            if s['sid'] == sid:
                return s
        if not force:
            return self.get(sid, True)
        return None


INDEX = Index()


def runner_flags(sid):
    r = RUNNERS.get(sid)
    if not r or not r.state['alive']:
        return {'running': False, 'busy': False, 'attention': False}
    return {'running': True, 'busy': r.state['busy'], 'attention': r.attention()}


def projects_payload():
    groups = {}
    for s in INDEX.scan():
        if s['cwd'] == '/' or (not SHOW_TMP and s['cwd'].startswith(HIDE_PREFIX)):
            continue
        cwd, wt = s['cwd'], None
        if '/.claude/worktrees/' in cwd:  # 独立工作树里的对话归到原项目下
            cwd, wt = cwd.split('/.claude/worktrees/', 1)
            wt = wt.split('/', 1)[0]
        groups.setdefault(cwd, []).append(dict(s, wt=wt) if wt else s)
    folders = load_conf().get('folders', [])
    for f in folders:
        groups.setdefault(f, [])
    # 第十四轮 FJ：经符号链接指向同一文件夹的（/tmp/x 与 /private/tmp/x）合成一组；用户添加过的文件夹路径优先，其次先出现的
    rp, real = {k: os.path.realpath(k) for k in groups}, {}
    for k in sorted(groups, key=lambda k: k not in folders):
        real.setdefault(rp[k], k)
    for k, r in rp.items():
        if real[r] != k:
            groups[real[r]].extend(groups.pop(k))
    for r in list(RUNNERS.values()):
        if not r.state['alive'] or r.prewarm:
            continue
        ss = groups.setdefault(real.get(os.path.realpath(r.cwd), r.cwd), [])
        if not any(x['sid'] == r.sid for x in ss):
            ss.append({'sid': r.sid, 'cwd': r.cwd, 'title': r.title or '新对话', 'mtime': r.started})
    conf = load_conf()
    archived = set(conf.get('archived', []))
    pinned = conf.get('pinned', [])
    names = conf.get('names', {})
    hidden = set(conf.get('hidden_projects', []))
    res = []
    for cwd, ss in groups.items():
        ss.sort(key=lambda x: -x['mtime'])
        live = [x for x in ss if x['sid'] not in archived]
        keep = [x for x in ss if x['sid'] in pinned] + [x for x in ss[:120] if x['sid'] not in pinned]
        res.append({'cwd': cwd, 'name': os.path.basename(cwd.rstrip('/')) or cwd, 'hidden': cwd in hidden,
                    'mtime': (live or ss)[0]['mtime'] if ss else 0,
                    'sessions': [dict({'sid': x['sid'], 'title': names.get(x['sid']) or x['title'], 'mtime': x['mtime'],
                                       'archived': x['sid'] in archived, 'renamed': x['sid'] in names,
                                       'pinned': pinned.index(x['sid']) + 1 if x['sid'] in pinned else 0,
                                       'wt': x.get('wt') or WT_SESS.get(x['sid']),
                                       # 第十三轮 E：一轮结束时间（未读）、上次回复被中断（可继续）
                                       'done_at': MARKS['done_at'].get(x['sid'], 0), 'resumable': x['sid'] in MARKS['interrupted']},
                                      **runner_flags(x['sid']))
                                 for x in keep]})
    res.sort(key=lambda p: -p['mtime'])
    return res


# ---------------------------------------------------------------- 终端屏幕识别
RULE_RE = re.compile(r'^[─━]{8,}(?:\s[^─━]{1,40}\s[─━]+)?$')  # 第十四轮 I：Fast 开着时输入框上沿带“ ↯ ─”
BG_RE = re.compile(r'(?<!\w)(\d+)\s+(?:shells?|bash(?:es)?|background (?:tasks?|shells?|agents?)|local agents?)\b', re.I)
MODE_RE = re.compile(r"(auto mode|manual mode|accept edits|plan mode|bypass permissions|default mode|"
                     r"normal mode|don't ask mode|dont ask mode)\s+on", re.I)
SPIN_RE = re.compile(r'^\s*[✻✳✶✽✢·*✺✹✸✷]\s{1,2}(\S.*….*|(?:Waiting for API response|No response from the API|Retrying in|picking the thought back up|deep in thought|still thinking|thinking (?:some )?more)\b.*)$')
# 只认 CLI 转圈字符，正文里的“- 第二项……”不算。11-12：网关迟迟不回内容时 CLI 状态行变成“✻ Waiting for API response · will retry in …”，
# 没有省略号，原来认不出，45 秒后误判空闲、界面显示“已完成”；这几句等待/重试提示也算在忙
EFFORT_RE = re.compile(r'\b(low|medium|high|xhigh|max)\s*·\s*/effort')
SPIN_TAIL_RE = re.compile(r'^(.*?…\s*\([^()]*\))\s*\S.*$')


def clean_spin(t):
    """11-26：CLI 局部重绘时状态行后半截可能残留之前屏幕上的字（如“(25s · ↓ 1.6k tokens · thinking) 397 try:”），
    括号闭合后的内容一律丢掉；没有括号的状态行原样返回。"""
    m = SPIN_TAIL_RE.match(t)
    return m.group(1) if m else t
HINT_WORDS = ('Enter to confirm', 'Esc to cancel', 'to select', 'to navigate', 'Esc to', 'Enter to')
# 11-25 选择题连点：终端弹窗点完一项后 CLI 要过一会儿才翻页，期间同一个弹窗会再报给界面、出现在鼠标下面；
# 授权卡交了之后 CLI 也要过一会儿才收起终端里的同一个提问。这段时间内不报这些旧画面。
DIALOG_HOLD = 1.5
# AskUserQuestion 正常走授权钩子（一张卡列出全部题目）；钩子慢了半拍时终端里先画出第一题，等它这么久再当弹窗报
ASK_WAIT = 1.2
ASK_OPT_RE = re.compile(r'^\d+\.\s+Type something\.?$')


def norm_line(l):
    l = l.rstrip()
    s = l.strip()
    if s and s[0] in '╭╰' and s[-1] in '╮╯':
        return ''
    l = re.sub(r'^(\s*)[│┃]', lambda m: m.group(1) + ' ', l)
    l = re.sub(r'[│┃]\s*$', '', l)
    return l.rstrip()


TOOL_LINE_RE = re.compile(r'^[\w:.\-]+\(|^(Read|Wrote|Updated|Searched|Listed|Found|Ran)\b')
# 工具调用在屏幕上的样子：进行中“Reading 路径”“Running…”，收拢后“Ran 2 shell commands”“Read 1 file”，常缩进在正文下面
TOOL_SUM_RE = re.compile(r'^(?:(?:Ran|Read|Wrote|Updated|Edited|Searched|Listed|Fetched|Created|Found|Deleted|Searching|Reading)\s+\d+\s|'
                         r'(?:Running|Reading|Writing|Updating|Editing|Searching|Listing|Fetching|Creating|Read|Wrote|Updated|Edited)\s+\S)')
SPIN_LINE_RE = re.compile(r'^\s*[✻✳✶✽✢·*]\s+\S.*(…|\(\d)')


def safe_display(screen, crop=None):
    """等同 pyte 的 screen.display，但容忍孤立的宽字符占位格（CLI 用窄字符覆盖了宽字符时 pyte 会留下空格子，
    原版遇到就抛 IndexError，导致整个对话读不了屏幕、发不出消息）。"""
    out = []
    for y in range(screen.lines):
        line, row, wide = screen.buffer[y], [], False
        width = crop[0] if crop and crop[1] - 1 <= y <= crop[2] + 1 else screen.columns
        for x in range(width):
            if wide:
                wide = False
                continue
            ch = line[x].data
            if not ch:
                row.append(' ')
                continue
            row.append(ch)
            wide = wcwidth(ch[0]) == 2
        out.append(''.join(row))
    return out


def panel_col(screen):
    """CLI 在宽终端里有时会在右边画侧栏（改动文件、diff），用竖线 │ 隔开。找出这条分界列和它占的行，
    条件：至少 4 行在同一列出现本行第一个 │，且这一列在右半边（表格的 │ 从行首附近就开始，不会误判）。"""
    cnt, rows = {}, {}
    for y in range(screen.lines):
        line = screen.buffer[y]
        for x in range(screen.columns):
            if line[x].data == '│':
                if screen.columns * 0.4 <= x < screen.columns - 10:
                    cnt[x] = cnt.get(x, 0) + 1
                    rows.setdefault(x, []).append(y)
                break
    if not cnt:
        return None
    x, n = max(cnt.items(), key=lambda kv: kv[1])
    return (x, min(rows[x]), max(rows[x])) if n >= 4 else None


LIVE_BLOCK_RE = re.compile(r'^\s*(?:[-*•]\s|\d+[.)、]\s?|#{1,6}\s|>|\||```)')


def live_text(lines, top, cols=None):
    """回复生成中：取输入框上方最后一个 ⏺ 开头的正文块（屏幕上正在逐字出现的内容），工具调用块不取。"""
    # 回复进行中排队的消息显示在输入框正上方（❯ 消息 … ctrl+x ctrl+s to send now），先跳过这一块
    for h in range(top - 1, max(-1, top - 12), -1):
        if 'ctrl+x ctrl+s to send now' in lines[h]:
            i0, stop = None, ''
            for k in range(h - 1, max(-1, h - 60), -1):
                if lines[k].startswith('❯'):
                    i0 = k
                elif not (lines[k].startswith('  ') or not lines[k].strip()):
                    stop = lines[k]
                    break
            if not stop.startswith('⏺'):
                return ''  # 排队块上面紧挨着的是上一轮的“✻ … done”：这一轮（比如 /compact）还没出正文
            top = i0 if i0 is not None else h
            break
    start = None
    for i in range(top - 1, -1, -1):
        if lines[i].startswith('❯'):  # 先碰到用户这一轮的提问：本轮还没出正文，上面的是上一轮的回复
            return ''
        if lines[i].startswith('⏺'):
            start = i
            break
    if start is None:
        # 一段很长的回复，⏺ 那行已经滚出屏幕：取屏幕顶部到输入框之间的正文（下面遇到 ⎿ 说明是工具输出，不取）
        start = next((i for i in range(top) if lines[i].strip()), None)
        if start is None or not lines[start].startswith('  '):
            return ''
        if any(re.match(r'^\s{3,}\d+\s', lines[i]) for i in range(start, min(top, start + 8))):
            return ''  # 屏幕顶部是改文件的 diff（行号开头），不是正文
        first = lines[start].strip()
    else:
        first = lines[start][1:].strip()
    if not first or TOOL_LINE_RE.match(first) or TOOL_SUM_RE.match(first):
        return ''
    out, prev_full = [], False
    for i in range(start, top):
        raw = lines[i]
        # 正文续行和折行都缩进两格；顶格的（下一块、✻ 完成行、❯ 提问）说明这一块结束了
        if raw.strip().startswith('⎿'):
            return ''  # ⏺ 下面紧跟 ⎿ 的是工具块（进行中的工具标题就是它的说明文字），不是正文
        if i > start and ((raw.strip() and not raw.startswith(' ')) or SPIN_LINE_RE.match(raw) or EFFORT_RE.search(raw)
                          or TOOL_SUM_RE.match(raw.strip())):
            break
        txt = first if i == start else (raw[2:] if raw.startswith('  ') else raw).rstrip()
        if not txt.strip():
            out.append('')
            prev_full = False
            continue
        # 自动折行接回去：上一行接近写满（CLI 在空格处折行，不一定顶到边），这一行又不是新的列表项或标题
        if prev_full and out and out[-1] and not LIVE_BLOCK_RE.match(txt):
            sep = ' ' if re.search(r'[A-Za-z0-9,.;:!?)]$', out[-1]) and re.match(r'\s*[A-Za-z0-9(`"]', txt) else ''
            out[-1] += sep + txt.lstrip()
        else:
            out.append(txt)
        prev_full = wcswidth(raw) >= (cols or COLS) * 0.6
    s = re.sub(r'\n{3,}', '\n\n', '\n'.join(out)).strip()
    return s[-6000:]


def parse_dialog(lines):
    cur = None
    for i, l in enumerate(lines):
        if l.lstrip().startswith('❯'):
            cur = i
    if cur is None:
        return None
    l = lines[cur]
    col = len(l) - len(l.lstrip()) + 2

    def kind(i):
        s = lines[i]
        if not s.strip():
            return 'blank'
        if s[:col].strip() not in ('', '❯'):
            return 'other'
        if len(s) > col and s[col] != ' ':
            return 'opt'
        if len(s) > col and s[:col + 1].strip() == '':
            return 'cont'
        return 'other'

    idx = [cur]
    i = cur - 1
    while i >= 0 and kind(i) in ('opt', 'cont'):
        idx.insert(0, i)
        i -= 1
    i = cur + 1
    while i < len(lines) and kind(i) in ('opt', 'cont'):
        idx.append(i)
        i += 1
    while idx and kind(idx[0]) == 'cont':
        idx.pop(0)
    opts, curo = [], 0
    for i in idx:
        if kind(i) == 'opt':
            if i == cur:
                curo = len(opts)
            opts.append({'label': lines[i][col:].strip(), 'desc': ''})
        elif opts:
            opts[-1]['desc'] = (opts[-1]['desc'] + ' ' + lines[i].strip()).strip()
    if len(opts) < 2:
        return None
    first, last = (idx[0], idx[-1]) if idx else (cur, cur)
    after = [lines[i].strip() for i in range(last + 1, len(lines)) if lines[i].strip()]
    numbered = all(re.match(r'^\d+\.', o['label']) for o in opts)
    if not numbered and not any(w in ' '.join(after) for w in HINT_WORDS):
        return None
    text = [lines[i].strip() for i in range(0, first) if lines[i].strip()][-12:]
    joined = '\n'.join(text) + '\n' + '\n'.join(o['label'] for o in opts)
    # 11-25：弹窗身份＝选项上方最近三行（选择题是页签行和题目）＋选项文字，不含光标位置。界面点选时带回来，
    # 后台只在屏幕上还是这个弹窗时才按键，免得点击落到已经翻到的下一题上
    tab = next((l for l in reversed(text) if '☐' in l or '☒' in l), '')  # 选择题的页签行（题干折行时可能不在最后三行里）
    did = hashlib.sha1((tab + '\n' + '\n'.join(text[-3:]) + '\n' + '\n'.join(o['label'] for o in opts)).encode('utf-8')).hexdigest()[:12]
    ask = any(ASK_OPT_RE.match(o['label']) for o in opts)  # AskUserQuestion 的提问（最后有“Type something.”）
    return {'text': '\n'.join(text), 'options': opts, 'cur': curo, 'hint': ' · '.join(after[:2]),
            'kind': 'trust' if 'trust this folder' in joined else 'generic', 'id': did,
            'ask_first': ask and '☒' not in tab}  # 第一题（页签里还没有答过的题）


# ---------------------------------------------------------------- 第十三轮 E：未读、中断标记、钩子事件文件
def load_marks():
    try:
        with open(MARKS_PATH, encoding='utf-8') as f:
            d = json.load(f)
        MARKS['done_at'] = dict(d.get('done_at') or {})
        MARKS['interrupted'] = dict(d.get('interrupted') or {})
    except Exception:
        pass


def save_marks():
    try:
        da = MARKS['done_at']
        for k in sorted(da, key=da.get)[:-500]:
            da.pop(k, None)
        tmp = MARKS_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(MARKS, f, ensure_ascii=False)
        os.replace(tmp, MARKS_PATH)
    except Exception as e:
        log('保存未读和中断标记失败', repr(e))


def mark_done(sid, t=None):
    """一轮结束（Stop / StopFailure）：记下时间，界面按 done_at > seen_at 判断未读。"""
    if not sid:
        return
    t = float(t or time.time())
    with G:
        if MARKS['done_at'].get(sid, 0) >= t:
            return
        MARKS['done_at'][sid] = t
        save_marks()
    BUS.emit({'type': 'marks', 'sid': sid, 'done_at': t})


def mark_interrupted(sid, why):
    """进行中的一轮没有正常 Stop 就被结束了（服务崩溃、关机、ptyd 崩溃、CLI 意外退出）：下次打开显示“上次回复被中断”。"""
    if not sid:
        return
    info = {'t': time.time(), 'why': why}
    with G:
        MARKS['interrupted'][sid] = info
        save_marks()
    log('记下中断的回复', sid[:8], why)
    BUS.emit({'type': 'marks', 'sid': sid, 'interrupted': info})
    BUS.emit({'type': 'sessions'})


def clear_interrupted(sid):
    with G:
        if MARKS['interrupted'].pop(sid, None) is None:
            return
        save_marks()
    BUS.emit({'type': 'marks', 'sid': sid, 'interrupted': None})
    BUS.emit({'type': 'sessions'})


def hook_path(gen):
    return os.path.join(HOOKS_DIR, re.sub(r'[^\w.-]', '_', gen) + '.json')


def read_hook(gen):
    try:
        with open(hook_path(gen), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def drop_hook(gen):
    try:
        os.remove(hook_path(gen))
    except OSError:
        pass


def transcript_open(sid):
    """记录文件里这一轮还没答完（有提问、没有最终回复），且记录文件 3 秒内没写过就算 False 的依据。返回 (open, 距上次写入秒数)。"""
    p = find_transcript(sid)
    if not p:
        return None, 0
    t = get_transcript(sid, p)
    try:
        t.poll()
    except Exception:
        pass
    try:
        age = time.time() - os.path.getmtime(p)
    except OSError:
        age = 0
    return t.conv.open, age


# ---------------------------------------------------------------- 第十三轮 E：终端守护进程 ptyd 的连接
class PtydDown(Exception):
    pass


class PtydClient:
    """server 和 ptyd 之间一条 Unix 套接字连接。请求带 id 等回复；out / snap / exit 按会话 key 投到对应 Runner 的收件箱，
    由 Runner 自己的线程喂屏幕、分析，和原来每个 Runner 读自己的伪终端一样。"""
    CRASH_WINDOW, CRASH_MAX = 300, 3  # 熔断：5 分钟内意外退出 3 次就不再自动拉起

    def __init__(self):
        self.p = PT.paths(RUN_DIR, PORT)
        self.sock = None
        self.wlock = threading.Lock()
        self.clock = threading.RLock()
        self.pending = {}
        self.ids = itertools.count(1)
        self.info = {}
        self.proc = None
        self.crashes = []
        self.bye = False
        self.upgrade = ''  # 待升级的原因：版本或代码指纹和 server 期望的不一致
        self.upgrading = False

    def broken(self):
        now = time.time()
        self.crashes = [t for t in self.crashes if now - t < self.CRASH_WINDOW]
        return len(self.crashes) >= self.CRASH_MAX

    def ensure(self, spawn=True, user=False):
        """连上 ptyd，连不上就拉起一个。熔断后不自动拉起；用户操作（发消息、新对话）给出中文提示。"""
        with self.clock:
            if self.sock:
                return True
            if self._connect():
                return True
            if not spawn or SHUTTING[0]:
                return False
            if self.broken():
                if user:
                    raise ValueError('终端守护进程短时间内反复崩溃，已暂停自动启动，请过几分钟再试')
                return False
            self._spawn()
            for _ in range(60):
                time.sleep(0.1)
                if self.proc and self.proc.poll() is not None and not os.path.exists(self.p['sock']):
                    break
                if self._connect():
                    return True
            self.crashes.append(time.time())
            log('终端守护进程没能启动，见', self.p['log'])
            if user:
                raise ValueError('终端守护进程没能启动')
            return False

    def _spawn(self):
        # start_new_session：自己一个会话，server 退出不影响它；它的“负责进程”仍随 server 归到 App
        with open(self.p['log'], 'ab') as lf:
            self.proc = subprocess.Popen([PY, '-I', PTYD_PY, '--port', str(PORT), '--run-dir', RUN_DIR], cwd=ROOT,
                                         stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True, close_fds=True)
        log('拉起终端守护进程', self.proc.pid)

    def _connect(self):
        if self.sock:
            return True
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.p['sock'])
        except OSError:
            s.close()
            return False
        s.settimeout(30)
        self.sock, self.bye = s, False
        threading.Thread(target=self._reader, args=(s,), daemon=True).start()
        try:
            h = self.request({'op': 'hello', 'proto': PT.PROTO, 'build': PT.BUILD, 'who': f'server-{os.getpid()}'}, timeout=5)
        except (PtydDown, ValueError):
            self._drop(s)
            return False
        if h.get('quitting'):
            self._drop(s)
            return False
        self.info = h
        if h.get('proto') != PT.PROTO:
            self.upgrade = f"协议版本 {h.get('proto')}，需要 {PT.PROTO}"
        elif h.get('build') != PT.BUILD:
            self.upgrade = f"代码 {h.get('build')}，磁盘上是 {PT.BUILD}"
        else:
            self.upgrade = ''
        log('已连上终端守护进程', 'pid', h.get('pid'), 'proto', h.get('proto'), 'build', h.get('build'),
            ('待升级：' + self.upgrade) if self.upgrade else '')
        return True

    def _drop(self, s):
        if self.sock is s:
            self.sock = None
        try:
            s.close()
        except OSError:
            pass

    def _reader(self, sock):
        fr = PT.Framer()
        try:
            while True:
                try:
                    data = sock.recv(1 << 18)
                except (TimeoutError, socket.timeout):
                    if self.sock is not sock:
                        break
                    continue
                if not data:
                    break
                for hdr, body in fr.feed(data):
                    self._dispatch(hdr, body)
        except Exception as e:
            if self.sock is sock:
                log('终端守护进程连接异常', repr(e))
        self._lost(sock)

    def _dispatch(self, hdr, body):
        op = hdr.get('op')
        if op in ('ok', 'err'):
            ent = self.pending.pop(hdr.get('id'), None)
            if ent:
                ent[1] = (hdr, body)
                ent[0].set()
        elif op in ('out', 'snap', 'exit'):
            r = KEYS.get(hdr.get('key'))
            if r:
                r.inbox.put((op, hdr, body))
        elif op == 'bye':
            self.bye = True
            log('终端守护进程说它要退出', hdr.get('why'))

    def _lost(self, sock):
        with self.clock:
            if self.sock is not sock:
                return
            self.sock = None
        try:
            sock.close()
        except OSError:
            pass
        for ent in list(self.pending.values()):
            ent[0].set()
        self.pending.clear()
        if SHUTTING[0]:
            return
        threading.Thread(target=self._after_lost, args=(self.info.get('pid'), self.bye), daemon=True).start()

    def _after_lost(self, old_pid, bye):
        if self.proc:
            self.proc.poll()  # 自己拉起的，收掉僵尸
        time.sleep(0.2)
        with self.clock:
            ok = self._connect()
        if ok and self.info.get('pid') == old_pid:
            log('和终端守护进程的连接断了又连上，重新订阅')
            for r in list(KEYS.values()):
                self.subscribe(r)
            return
        rs = list(KEYS.values())
        for r in rs:
            r.inbox.put(('lost', {'bye': bye}, b''))
        if bye or self.upgrading:
            return
        self.crashes.append(time.time())
        log('终端守护进程意外退出', old_pid, '断开的对话', len(rs), '近 5 分钟第', len(self.crashes), '次')
        if self.broken():
            log('终端守护进程反复崩溃，停止自动拉起')
            BUS.emit({'type': 'toast', 'text': '终端守护进程短时间内反复崩溃，已暂停自动启动'})
            return
        if not ok:
            time.sleep(0.5)
            try:
                self.ensure()
            except Exception as e:
                log('重新拉起终端守护进程失败', repr(e))

    def request(self, hdr, body=b'', timeout=10):
        sock = self.sock
        if not sock:
            raise PtydDown('终端守护进程没有连上')
        i = next(self.ids)
        ent = [threading.Event(), None]
        self.pending[i] = ent
        try:
            self._send(sock, dict(hdr, id=i), body)
        except OSError as e:
            self.pending.pop(i, None)
            raise PtydDown(str(e))
        if not ent[0].wait(timeout):
            self.pending.pop(i, None)
            raise PtydDown('终端守护进程没有响应')
        if ent[1] is None:
            raise PtydDown('和终端守护进程的连接断了')
        h, b = ent[1]
        if h.get('op') == 'err':
            raise ValueError(h.get('error') or '终端守护进程出错')
        if b:
            h['_body'] = b
        return h

    def send(self, hdr, body=b''):
        sock = self.sock
        if not sock:
            return False
        try:
            self._send(sock, hdr, body)
            return True
        except OSError:
            return False

    def _send(self, sock, hdr, body):
        data = PT.pack(hdr, body)
        with self.wlock:
            sock.sendall(data)

    def subscribe(self, r):
        # 屏幕已经有了就从 r.seq 之后补（环形缓冲还在的话），否则要快照
        self.send({'op': 'subscribe', 'key': r.pkey, 'since': r.seq if r.have_screen else None})

    def status(self):
        return {'pid': self.info.get('pid'), 'proto': self.info.get('proto'), 'build': self.info.get('build'), 'connected': bool(self.sock),
                'want': {'proto': PT.PROTO, 'build': PT.BUILD}, 'upgrade': self.upgrade, 'upgrading': self.upgrading,
                'crashes': len(self.crashes), 'broken': self.broken()}


PTYD = PtydClient()


# ---------------------------------------------------------------- 伪终端里的 claude（第十三轮 E：伪终端由 ptyd 持有，这里只收发字节、维护自己那份屏幕）
class Runner:
    def __init__(self, sid, cwd, new, opts=None, title='', fork_from=None, worktree=None, adopt=None):
        opts = opts or {}
        self.sid, self.cwd, self.title = sid, cwd, title
        self.started = self.last_used = time.time()
        self.lock = threading.RLock()
        self.wlock = threading.Lock()
        self.dlock = threading.Lock()  # 11-25：终端弹窗的点选、按键一次一个，按之前现读屏幕核对是不是界面点的那个弹窗
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.state = {'alive': True, 'ready': False, 'busy': False, 'spinner': '', 'status': '',
                      'mode': '', 'effort': '', 'dialog': None}
        self.queue, self.sending, self.dirty, self.last_an, self.emitted = [], False, False, 0, None
        self.restart_when_idle = False
        self.prewarm = self.unattended = False
        # 一轮回复从提交（UserPromptSubmit 钩子）到结束（Stop 钩子）算忙碌：CLI 出正文时会把状态行藏起来，不能只看状态行
        self.turn, self.turn_at, self.last_spin, self.last_live = False, 0.0, '', ''
        self.stopping, self.qlock = False, threading.Lock()
        self.notice = None  # Notification 钩子：CLI 在等人处理、屏幕又没认出弹窗
        self.since = 0.0  # 第十二轮 C：本轮开始时间（UserPromptSubmit 钩子），左栏显示已用时间
        # 第十三轮 E：ptyd 推来的输出放进收件箱，由本 Runner 的线程按顺序处理；seq＝已画到屏幕上的累计字节数
        self.inbox, self.seq, self.have_screen, self.pid = queue.Queue(), 0, False, 0
        self.turn_t, self.meta_sent, self.adopted, self.redrawn = 0.0, None, False, False
        self.opts = {k: opts.get(k) for k in OPT_KEYS if opts.get(k)}  # 第十四轮 I：加 fast
        self.opts_key = opts_key_of(self.opts)
        self.dirs, self.panel_op = [], None  # 第十五轮 A2：本会话追加的工作文件夹；正在进行的代按键操作
        self.base_mode = opts.get('mode') if opts.get('mode') != 'plan mode' else None  # 计划卡“沿用”项用
        if adopt:
            self._adopt(adopt)
            return
        with G:
            GEN_SEQ[0] += 1
            self.gen = f'{BOOT}-{GEN_SEQ[0]}'  # 启动代次：钩子带回来，旧进程迟到的事件按它丢弃
        self.pkey = self.gen
        EXITS.pop(sid, None)
        if fork_from:
            args = [CLAUDE, '--resume', fork_from, '--fork-session', '--session-id', sid]
        else:
            args = [CLAUDE] + (['--session-id', sid] if new else ['--resume', sid])
        if worktree:
            args += ['--worktree', worktree]
        if opts.get('model'):
            args += ['--model', opts['model']]
        if opts.get('effort'):
            args += ['--effort', opts['effort']]
        if opts.get('mode') in MODE_FLAG:
            args += ['--permission-mode', MODE_FLAG[opts['mode']]]
        args += ['--settings', json.dumps(build_settings(opts), ensure_ascii=False)]  # 第十四轮 I：Fast 按对话注入
        args += PS.launch_args()  # 11-18 个性化：语气、排版开关、补充说明附加到系统提示
        args = CB.claude_args(sid, args)  # 第十七轮 M：协作开着时（父对话）加 --mcp-config、叫醒钩子、说明并进 --append-system-prompt；关着或子对话原样返回
        env = dict(LOGIN_ENV)
        env.update(CWB_PORT=str(PORT), CWB_TOKEN=TOKEN, CWB_SID=sid, CWB_GEN=self.gen)
        KEYS[self.pkey] = self
        try:
            h = PTYD.request({'op': 'open', 'key': self.pkey, 'argv': args, 'env': env, 'cwd': cwd, 'rows': ROWS, 'cols': COLS,
                              'meta': self.meta()}, timeout=15)
        except (PtydDown, ValueError) as e:
            KEYS.pop(self.pkey, None)
            raise ValueError('启动 claude 失败：' + str(e))
        self.pid = h.get('pid') or 0
        US.log_run(RUNS_PATH, 'start', self.pkey, sid)  # 第十四轮 I：用量统计的兜底时间段
        log('启动 claude', sid, cwd, '新建' if new else '续接', 'pid', self.pid)
        PTYD.subscribe(self)
        threading.Thread(target=self._reader, daemon=True).start()

    def _adopt(self, ent):
        """server 重启后接管 ptyd 里还在跑的 CLI：代次、since、turn、opts 等从 meta 还原，再用钩子事件文件补上 server 不在时发生的事。"""
        m = ent.get('meta') or {}
        self.adopted = True
        self.pkey, self.pid = ent['key'], ent.get('pid') or 0
        self.gen = m.get('gen') or ent['key']
        self.started = float(m.get('started') or ent.get('started') or time.time())
        self.last_used = float(m.get('last_used') or self.started)
        self.title = m.get('title') or ''
        self.opts = dict(m.get('opts') or {})
        self.opts_key = m.get('opts_key') or json.dumps(self.opts, sort_keys=True)
        self.prewarm, self.unattended = bool(m.get('prewarm')), bool(m.get('unattended'))
        self.restart_when_idle = bool(m.get('restart_when_idle'))
        self.turn, self.since, self.turn_t = bool(m.get('turn')), float(m.get('since') or 0), float(m.get('turn_t') or 0)
        self.stopping = bool(ent.get('closing'))
        self.last_spin = m.get('last_spin') or ''
        self.dirs, self.base_mode = list(m.get('dirs') or []), m.get('base_mode')  # 第十五轮 A2
        hf = read_hook(self.gen)
        self.hook_sid = ''
        if hf and float(hf.get('t') or 0) > self.turn_t:
            ev, t = hf.get('ev'), float(hf.get('t') or 0)
            if ev in ('busy', 'permission'):
                if not self.turn and ev == 'busy':
                    self.since, self.last_spin = t, ''
                self.turn = True
            elif ev in ('idle', 'fail'):
                self.turn, self.since = False, 0.0
                mark_done(self.sid, t)  # server 不在时这一轮结束了：Stop 钩子没送到，按事件文件补记
            self.turn_t = t
            nsid = str(hf.get('session_id') or '')
            if nsid != self.sid and re.fullmatch(r'[0-9a-fA-F-]{36}', nsid):
                self.hook_sid = nsid  # server 不在时 /clear 换了 ID，接管后再迁
        self.turn_at = time.time()
        self.meta_sent = m
        KEYS[self.pkey] = self
        log('接管 claude', self.sid, 'pid', self.pid, '代次', self.gen, '回合中' if self.turn else '空闲', '已退出' if not ent.get('alive') else '')
        US.log_run(RUNS_PATH, 'start', self.pkey, self.sid, t=self.started)  # 第十四轮 I：升级前就在跑的 CLI 也补上时间段（重复的取最早）
        PTYD.subscribe(self)
        threading.Thread(target=self._reader, daemon=True).start()
        if ent.get('alive'):
            threading.Thread(target=self._verify, daemon=True).start()

    def _verify(self):
        """接管 3 秒后校正：屏幕上既没有输入框也没有弹窗，说明快照可能不准，缩一行再还原让 CLI 整屏重画；
        钩子说还在回合中、屏幕上却没有转圈、记录文件里这一轮已经答完，就当这一轮已结束。"""
        time.sleep(3)
        if not self.state.get('alive'):
            return
        try:
            self.analyze(force=True)
            if not self.state.get('ready') and not self.state.get('dialog'):
                self.redraw()
                time.sleep(1.5)
                self.analyze(force=True)
            if self.turn and not self.state.get('spinner') and not self.attention():
                op, age = transcript_open(self.sid)
                if op is False and age > 3:
                    log('接管校正：这一轮已经答完', self.sid[:8])
                    self.turn, self.since, self.turn_t = False, 0.0, time.time()
                    self.analyze(force=True)
        except Exception as e:
            log('接管校正异常', self.sid, repr(e))

    def redraw(self):
        self.redrawn = True
        log('请 CLI 整屏重画', self.sid[:8])
        PTYD.send({'op': 'redraw', 'key': self.pkey})

    def meta(self):
        """写进 ptyd 的不透明 meta：server 重启后按它把 Runner 还原。"""
        return {'sid': self.sid, 'gen': self.gen, 'cwd': self.cwd, 'title': self.title, 'opts': self.opts, 'opts_key': self.opts_key,
                'since': self.since, 'turn': self.turn, 'turn_t': self.turn_t, 'prewarm': self.prewarm, 'unattended': self.unattended,
                'restart_when_idle': self.restart_when_idle, 'started': self.started, 'last_used': self.last_used,
                'last_spin': self.last_spin,  # CLI 出正文时状态行藏起来，转圈文字沿用上一次看到的
                'dirs': list(getattr(self, 'dirs', None) or []), 'base_mode': getattr(self, 'base_mode', None),  # 第十五轮 A2
                'new_cwd': NEW_SESS.get(self.sid), 'fork_from': FORK_SRC.get(self.sid), 'wt': WT_SESS.get(self.sid)}

    def sync_meta(self):
        if not self.state.get('alive'):
            return
        m = self.meta()
        if m != self.meta_sent and PTYD.send({'op': 'set-meta', 'key': self.pkey, 'meta': m}):
            self.meta_sent = m

    # ---- 输入输出
    def write(self, b):
        """写给 CLI：经 ptyd 的非阻塞写队列，等它回执“已全部写进终端”再返回（保持原来写完才返回的节奏）。"""
        if not b:
            return
        with self.wlock:
            if not self.state.get('alive'):
                return
            try:
                PTYD.request({'op': 'input', 'key': self.pkey}, b, timeout=5 + len(b) / 20000)
            except (PtydDown, ValueError):
                pass

    def key(self, name):
        seq = {'up': b'\x1b[A', 'down': b'\x1b[B', 'enter': b'\r', 'esc': b'\x1b', 'shift-tab': b'\x1b[Z',
               'send-now': b'\x18\x13', 'kill-line': b'\x15'}.get(name)  # send-now：CLI 自带的 ctrl+x ctrl+s，排队的消息立即发送
        if seq:
            self.write(seq)

    def _paint(self, hdr):
        """subscribe 的回复：新建一块屏幕，用“清屏 + 逐行定位写入 + 还原光标”把 ptyd 的快照画上去，之后接实时输出。"""
        if hdr.get('missing'):
            self.inbox.put(('lost', {'missing': True}, b''))
            return
        if hdr.get('mode') == 'replay':
            self.seq = int(hdr.get('seq') or self.seq)
            return
        snap = hdr.get('screen') or {}
        with self.lock:
            self.screen = pyte.Screen(int(snap.get('cols') or COLS), int(snap.get('rows') or ROWS))
            self.stream = pyte.ByteStream(self.screen)
            self.stream.feed(PT.paint_bytes(snap))
        self.seq, self.have_screen, self.dirty = int(hdr.get('seq') or 0), True, True

    def _reader(self):
        end = None
        while end is None:
            try:
                item = self.inbox.get(timeout=0.25)
            except queue.Empty:
                item = None
            n = 0
            while item is not None:
                op, hdr, body = item
                if op == 'out':
                    with self.lock:
                        try:
                            self.stream.feed(body)
                        except Exception as e:
                            log('屏幕解析异常', e)
                    self.seq = int(hdr.get('seq') or self.seq + len(body))
                    self.dirty = True
                elif op == 'snap':
                    self._paint(hdr)
                elif op in ('exit', 'lost'):
                    end = (op, hdr)
                    break
                n += 1
                if n >= 64:
                    break
                try:
                    item = self.inbox.get_nowait()
                except queue.Empty:
                    item = None
            if end is None and self.dirty and time.time() - self.last_an > 0.12:
                try:
                    self.analyze()
                except Exception as e:
                    log('分析屏幕异常', self.sid, repr(e))
        self._exited(*end)

    def _exited(self, op, info):
        """CLI 退出（ptyd 发来 exit），或 ptyd 本身不在了（lost）：走原来的退出处理，加上中断标记。"""
        lost = op == 'lost'
        try:
            tail = '\n'.join(PT.tail_lines(self.screen_lines())) if self.have_screen else ''
        except Exception:
            tail = ''
        tail = tail or info.get('tail') or ''
        code, closed_by = info.get('code'), info.get('closed_by') or ''
        in_turn = bool(self.turn or self.state.get('busy'))
        user_quit = time.time() - getattr(self, 'answered_at', 0) < 8  # 刚在启动弹窗里选了退出（如信任弹窗点取消），是用户主动的
        intended = self.stopping or closed_by == 'close' or user_quit
        ex = None
        if lost:
            log('终端守护进程不在了，CLI 跟着断开', self.sid, '回合中' if in_turn else '')
            if in_turn:
                mark_interrupted(self.sid, 'ptyd')
        elif not intended:
            if closed_by == 'ptyd-exit':  # 守护进程收到停止信号（关机、stop.sh）结束了它
                if in_turn:
                    mark_interrupted(self.sid, 'shutdown')
            else:  # 不是我们结束的：记下退出码和退出前屏幕最后 20 行
                ex = {'code': code, 'tail': tail, 't': time.time()}
                EXITS[self.sid] = ex
                log('claude 意外退出', self.sid, '退出码', code)
                if in_turn:
                    mark_interrupted(self.sid, 'exit')
        self.notice = None
        self.state.update(alive=False, ready=False, busy=False, spinner='', dialog=None, exit=ex)
        drop_perms(self.sid)
        note_own(self.sid)
        for k, v in list(PREWARM.items()):
            if v is self:
                PREWARM.pop(k, None)
        with G:
            if RUNNERS.get(self.sid) is self:
                RUNNERS.pop(self.sid, None)
            if KEYS.get(self.pkey) is self:
                KEYS.pop(self.pkey, None)
        if not lost:
            PTYD.send({'op': 'forget', 'key': self.pkey})
        drop_hook(self.gen)
        US.log_run(RUNS_PATH, 'end', self.pkey, self.sid)  # 第十四轮 I
        log('claude 已退出', self.sid)
        BUS.emit({'type': 'runner', 'sid': self.sid, 'state': self.public_state()})
        BUS.emit({'type': 'sessions'})

    def active(self):
        """有事在做就不能结束：回复中、后台命令或子代理在跑、等你确认、还有没发出的消息、正在退出。"""
        st = self.state
        return bool(st.get('busy') or st.get('bg') or self.queue or self.sending or self.attention() or self.stopping
                    or getattr(self, 'panel_op', None))  # 第十五轮 A2：正在代按键（旁问等）也算有事

    def attention(self):
        return bool(self.state['dialog']) or getattr(self, 'dialog_held', False) or any(e['req']['sid'] == self.sid for e in list(PERMS.values()))

    def public_state(self):
        s = dict(self.state)
        s['attention'] = self.attention()
        s['sid'] = self.sid
        s['turn'] = self.turn  # 钩子记录的回合：提交后到 Stop 之前都算这一轮没结束
        s['notice'] = self.notice['text'] if self.notice else ''
        s['since'] = self.since  # 第十二轮 C：本轮开始时间（秒），0 表示不在一轮里
        s['dirs'] = list(getattr(self, 'dirs', None) or [])  # 第十五轮 A2：本会话追加的工作文件夹
        s['done_at'] = MARKS['done_at'].get(self.sid, 0)  # 第十三轮 E：这个对话上一轮结束的时间（未读）
        s['ctx'] = CX.ctx_of(self)  # 第十五轮 A1：上下文用量（状态行落盘），拿不到为 None
        s['pending'] = self.pending_view()  # 11-14：已选但要等空闲才生效的模型、思考强度
        return s

    def pending_view(self):
        """11-14：回复进行中换了模型或思考强度，要等空闲才重启生效。返回正在用的和已选的（模型给实际 ID），没有待生效的返回 None。"""
        if not self.restart_when_idle:
            return None
        want = session_opts(self.sid)
        return {'model': eff_model(want), 'effort': want.get('effort') or '',
                'run_model': eff_model(self.opts), 'run_effort': self.opts.get('effort') or ''}

    def screen_lines(self):
        with self.lock:
            crop = panel_col(self.screen)
            self.view_cols = crop[0] if crop else COLS
            return [norm_line(l) for l in safe_display(self.screen, crop)]

    def analyze(self, force=False):
        self.dirty, self.last_an = False, time.time()
        lines = self.screen_lines()
        held = False  # 11-25：屏幕上有弹窗但暂时不报（hold_dialog），这期间也算在等人，不被当成空闲回收
        rules = [i for i, l in enumerate(lines) if RULE_RE.match(l.strip())]
        box = None
        for a, b in zip(rules, rules[1:]):
            if 1 <= b - a - 1 <= 30 and lines[a + 1].lstrip().startswith('❯'):
                box = (a, b)
        st = dict(self.state)
        if box:
            a, b = box
            mode, status, bg = None, '', 0
            for l in lines[b + 1:]:
                s = l.strip()
                if not s:
                    continue
                mb = BG_RE.search(s)
                if mb:
                    bg += int(mb.group(1))
                if s[:1] in ('◯', '◉', '◎'):  # 后台子代理面板里正在跑的子代理
                    bg += 1
                    continue
                m = MODE_RE.search(s)
                if m:
                    mode = m.group(1).lower()
                    continue
                if not status and not s.startswith(('?', '!', 'esc ', 'ctrl', 'Press', '⏵', '⏸')):
                    status = s
            spinner = ''
            for k in range(max(0, a - 4), a):
                m = SPIN_RE.match(lines[k])
                if m:
                    spinner = clean_spin(m.group(1).strip())
            if not spinner:
                # 状态行下面可能挂着待办清单、提示、排队的消息、重试提示，离输入框远：往上找顶格的状态行，碰到正文块就停
                for k in range(a - 1, max(-1, a - 40), -1):
                    l = lines[k]
                    if not l.startswith(' '):
                        m = SPIN_RE.match(l)
                        if m:
                            spinner = clean_spin(m.group(1).strip())
                            break
                    if l.strip() and not l.startswith((' ', '❯')):
                        break
            eff = None
            for k in range(max(0, a - 3), a):
                m = EFFORT_RE.search(lines[k])
                if m:
                    eff = m.group(1)
            now = time.time()
            if spinner:
                self.turn_at, self.last_spin = now, re.sub(r'\s*\(.*$', '', spinner)
            in_turn = self.turn and now - self.turn_at < 45
            live = live_text(lines, a, getattr(self, 'view_cols', COLS)) if spinner or in_turn else ''
            if live and live != self.last_live:
                self.turn_at = now
                if live[:40] != self.last_live[:40]:  # 排查串台：记下每段预览出现时这块屏幕的样子
                    self.live_hist = (getattr(self, 'live_hist', []) + [{'t': time.strftime('%H:%M:%S'), 'cols': getattr(self, 'view_cols', COLS),
                                                                        'live': live[:200], 'screen': lines[max(0, a - 30):a]}])[-20:]
            self.last_live = live
            busy = bool(spinner) or in_turn
            self.ask_seen = None  # 11-25：输入框回来了，上一次提问结束
            st.update(ready=True, dialog=None, spinner=spinner or (self.last_spin if busy else ''), busy=busy, bg=bg,
                      mode=mode or 'default', status=status or st['status'], effort=eff or st['effort'], live=live)
        else:
            pending_perm = any(e['req']['sid'] == self.sid for e in list(PERMS.values()))
            # 第十五轮 A2：代按键操作期间（旁问、后台任务、添加文件夹的面板）不把面板当弹窗报给界面
            dl = None if pending_perm or getattr(self, 'panel_op', None) else parse_dialog(lines)
            held = bool(dl) and self.hold_dialog(dl)
            if held:
                dl = None
            st.update(ready=False, dialog=dl, spinner='', busy=False, live='')
        self.dialog_held = held
        track_mode(self, st)  # 第十五轮 A2
        if self.notice and (st['dialog'] or (st['ready'] and time.time() - self.notice['t'] > 2)):
            self.notice = None  # 输入框回来了或认出了弹窗：等待提示收起
        self.state = st
        if st['status'] and st['status'] != STATUS['text']:
            STATUS['text'] = st['status']
            BUS.emit({'type': 'status', 'text': st['status']})
        pub = self.public_state()
        if self.unattended and st['dialog'] and not st['ready']:
            self.key('esc')
        dl = st['dialog']
        if (dl and dl.get('kind') == 'trust' and not st['ready'] and trust_carried(self.cwd)
                and time.time() - getattr(self, 'trust_auto_at', 0) > 3):
            # 11-11：改名前已信任的项目，改名后第一次弹信任框时替用户点“信任”（还是同一个文件夹，只是换了名字）
            self.trust_auto_at = time.time()
            idx = next((i for i, o in enumerate(dl['options']) if 'yes' in o['label'].lower()), 0)
            threading.Thread(target=self.choose, args=(idx,), daemon=True).start()
            log('改名后的项目自动确认信任', self.cwd)
        if (force or pub != self.emitted) and not self.prewarm:
            self.emitted = pub
            BUS.emit({'type': 'runner', 'sid': self.sid, 'state': pub})
        go = False
        if st['ready'] and self.queue:
            with self.qlock:
                go = bool(self.queue) and not self.sending and not getattr(self, 'panel_op', None)  # 第十五轮 A2：面板开着时不往终端打字
                if go:
                    self.sending = True
        if go:
            self._flush()
        elif self.restart_when_idle and st['ready'] and not self.active():
            self.restart_when_idle = False
            restart_runner(self)

    # ---- 发送
    def send(self, text, images=None, now=False):
        self.last_used = time.time()
        self.queue.append((text, list(images or []), now))
        self.analyze(force=True)

    def _flush(self):
        self.sending = True
        text, images, now = self.queue.pop(0)
        now = now and self.state.get('busy')

        def run():
            try:
                # 图片路径单独粘贴，CLI 会把它识别成 [Image #N] 附件
                for p in images:
                    self.write(paste_bytes(p))
                    time.sleep(0.6)
                data = safe_input(text)
                if data:
                    self.write(paste_bytes(data))
                    time.sleep(0.3 + min(len(data), 40000) / 40000)
                self.write(b'\r')
                time.sleep(0.4)
                if now:  # 回复进行中要求立即发送：CLI 自带的 ctrl+x ctrl+s，不等下一步
                    time.sleep(0.4)
                    self.key('send-now')
            finally:
                self.sending = False
                self.analyze(force=True)
        threading.Thread(target=run, daemon=True).start()

    def hold_dialog(self, dl):
        """11-25：这些终端弹窗先不报给界面（返回 True），到点由定时器再看一次屏幕：
        - 刚点过的同一个弹窗：CLI 还没翻页，再报一次会在鼠标下面冒出来，再点就落到下一题上
        - 授权卡刚交：CLI 还没收起终端里的同一个提问或授权提示
        - 选择题第一题刚画出来、授权钩子还没到：等钩子出一张列全部题目的卡；钩子一直没来再当弹窗报"""
        now = time.time()
        cid, cat = getattr(self, 'chosen', (None, 0.0))
        if dl.get('id') == cid and now - cat < DIALOG_HOLD:
            return True
        if now - getattr(self, 'perm_done_at', 0) < DIALOG_HOLD:
            return True
        if dl.get('ask_first'):
            seen = getattr(self, 'ask_seen', None)
            if not seen or seen[0] != dl['id']:
                seen = self.ask_seen = (dl['id'], now)
                self.recheck(ASK_WAIT)
            if now - seen[1] < ASK_WAIT:
                return True
        return False

    def recheck(self, delay):
        t = threading.Timer(delay + 0.05, lambda: self.state.get('alive') and self.analyze(force=True))
        t.daemon = True
        t.start()

    def live_dialog(self, want=None):
        """11-25：按键前现读屏幕（state 是上一次读屏的快照，CLI 可能已经翻到下一题）。
        返回屏幕上的弹窗；界面点的弹窗（want，没给就用快照里的）已经不在、或刚点过还没翻页时返回 None。"""
        snap = self.state.get('dialog')
        if not snap:  # 授权卡在等、代按键面板、压住的旧画面：界面上不该有这个弹窗
            return None
        want = want or snap.get('id')
        cid, cat = getattr(self, 'chosen', (None, 0.0))
        if want == cid and time.time() - cat < DIALOG_HOLD:
            return None
        d = parse_dialog(self.screen_lines())
        return d if d and d.get('id') == want else None

    def choose(self, idx, want=None):
        """选终端弹窗的第 idx 项。want：界面点的那个弹窗的身份；屏幕上已经不是它了就不按（11-25 选择题连点）。"""
        with self.dlock:
            d = self.live_dialog(want)
            if not d:
                log('弹窗已变，忽略这次点选', self.sid[:8], want)
                return False
            self.chosen = (d.get('id'), time.time())
            delta = idx - d['cur']
            for _ in range(abs(delta)):
                self.key('down' if delta > 0 else 'up')
                time.sleep(0.06)
            time.sleep(0.12)
            self.key('enter')
        self.recheck(DIALOG_HOLD)
        return True

    def dialog_key(self, name, want):
        """弹窗上的按键（取消＝esc）：同样先核对屏幕上还是界面点的那个弹窗。"""
        with self.dlock:
            d = self.live_dialog(want)
            if not d:
                log('弹窗已变，忽略这次按键', self.sid[:8], want, name)
                return False
            self.chosen = (d.get('id'), time.time())
            self.key(name)
        self.recheck(DIALOG_HOLD)
        return True

    def set_mode(self, target):
        if self.state.get('mode') not in (None, '', 'plan mode', 'default'):
            self.base_mode = self.state['mode']  # 第十五轮 A2：切走之前的模式（切到计划模式时“沿用”它）
        for _ in range(8):
            self.analyze()
            if self.state['mode'] == target:
                return True
            self.key('shift-tab')
            time.sleep(0.6)
        self.analyze()
        if self.state['mode'] != target:
            BUS.emit({'type': 'toast', 'text': '这个对话里切换不到该权限模式'})
        return self.state['mode'] == target

    def stop(self):
        if not self.state.get('alive') or self.stopping:
            return  # 进程已经退出或正在结束
        self.stopping = True
        threading.Thread(target=self.terminate, daemon=True).start()

    def terminate(self, grace=3.0):
        """结束 CLI（第十三轮 E：由 ptyd 执行，规则不变）：先记下它的后代进程，给它的进程组发 SIGHUP（相当于关掉终端，
        CLI 自己收尾，它管理的后台任务也会收掉），超时再 SIGKILL；最后清理还活着、仍在它会话里的后代进程。
        setsid 另起会话、nohup 忽略 SIGHUP 的常驻服务不动。这里等到 ptyd 报告退出为止。"""
        self.stopping = True
        if not PTYD.send({'op': 'close', 'key': self.pkey, 'grace': grace}):
            return
        end = time.time() + grace + 5
        while self.state.get('alive') and time.time() < end:
            time.sleep(0.1)


# ---------------------------------------------------------------- 结束 CLI 时清理它留下的子进程：第十三轮 E 起在 ptyd.py 里执行（规则不变），这里保留同名引用给测试和旧代码
_ps_rows, _gen_marked, sighup_ignored, cli_family, reap_family = PT._ps_rows, PT._gen_marked, PT.sighup_ignored, PT.cli_family, PT.reap_family


def reap_idle(keep=None):
    alive = [r for r in RUNNERS.values() if r.state['alive'] and r.sid != keep]
    over = len(alive) - (MAX_RUNNERS - 1)
    if over <= 0:
        return
    cands = sorted([r for r in alive if not r.active()], key=lambda r: (not r.prewarm, r.last_used))  # 先回收预热的
    for r in cands[:over]:
        log('回收空闲 claude', r.sid)
        r.stop()


def session_opts(sid):
    return dict(load_conf().get('session_opts', {}).get(sid, {}))


def save_session_opts(sid, opts):
    with G:
        c = load_conf()
        so = c.setdefault('session_opts', {})
        if opts:
            so[sid] = opts
        else:
            so.pop(sid, None)
        save_conf(c)


FORK_SRC, WT_SESS = {}, {}


def wait_upgrade():
    """第十三轮 E：正在更换 ptyd（升级）时，新的启动等它换完。"""
    end = time.time() + 30
    while PTYD.upgrading and time.time() < end:
        time.sleep(0.1)


def ensure_runner(sid, cwd=None, new=False, opts=None, fork_from=None, worktree=None):
    wait_upgrade()
    old = RUNNERS.get(sid)
    if old and old.stopping:
        # 刚停止或正在重启：等旧进程退出再起新的，免得消息写进正在退出的进程里丢掉
        for _ in range(40):
            if not old.state['alive']:
                break
            time.sleep(0.1)
        if old.state['alive']:
            PTYD.send({'op': 'close', 'key': old.pkey, 'grace': 0})  # 第十三轮 E：由 ptyd 强制结束
            time.sleep(0.3)
    r = RUNNERS.get(sid)
    if not (r and r.state['alive'] and not r.stopping):
        PTYD.ensure(user=True)  # 第十三轮 E：ptyd 不在就拉起（熔断时抛出中文提示）
    with G:
        r = RUNNERS.get(sid)
        if r and r.state['alive'] and not r.stopping:
            return r
        if r and r.stopping:
            RUNNERS.pop(sid, None)
        merged = merge_opts(session_opts(sid), opts or {})  # 第十四轮 I：fast 给了 False 也要能覆盖
        if opts:
            save_session_opts(sid, merged)
        opts = merged
        info = None if (new or fork_from) else INDEX.get(sid)
        if not new and not fork_from and not info and not find_transcript(sid):
            # 还没发过消息的新对话或分叉对话：按原方式重新启动
            if sid in FORK_SRC:
                fork_from, cwd = FORK_SRC[sid], cwd or NEW_SESS.get(sid)
            elif sid in NEW_SESS:
                new, cwd, worktree = True, cwd or NEW_SESS[sid], worktree or WT_SESS.get(sid)
        if not new and not fork_from and not info:
            raise ValueError('找不到这个对话')
        cwd = remap_cwd(cwd or info['cwd'])  # 11-11：项目改过名
        if new or fork_from:
            NEW_SESS[sid] = cwd
        if fork_from:
            FORK_SRC[sid] = fork_from
        if worktree:
            WT_SESS[sid] = worktree
        if not os.path.isdir(cwd):
            raise ValueError('项目文件夹不存在：' + cwd)
        reap_idle(keep=sid)
        r = Runner(sid, cwd, new, opts, title=(info or {}).get('title', ''), fork_from=fork_from, worktree=worktree)
        RUNNERS[sid] = r
        ALIAS.pop(sid, None)  # 这个 ID 自己有 CLI 了，不再指向别处
        get_transcript(sid, (info or {}).get('path'))
        return r


def restart_runner(r):
    """第十四轮 I：换了模型、思考强度或 Fast：结束这个 CLI，等它退出后立即带 --resume 重启（新选项从 session_opts 取）。"""
    if r.stopping or not r.state.get('alive'):
        return

    def go():
        r.terminate()
        if SHUTTING[0]:
            return
        cur = RUNNERS.get(r.sid)
        if cur and cur is not r and cur.state.get('alive'):
            return  # 别处已经重新启动了
        try:
            ensure_runner(r.sid)
        except Exception as e:
            log('换选项后重启 claude 失败', r.sid, repr(e))
        BUS.emit({'type': 'sessions'})
    threading.Thread(target=go, daemon=True).start()


def eff_model(opts):
    """11-14：选项里的模型换成实际 ID（别名展开；没指定就是 CLI 的默认模型）。"""
    return resolve_alias((opts or {}).get('model') or '') or cli_default_model()


def run_differs(r, opts):
    """11-14：对话选的模型、思考强度和正在运行的 CLI 启动时用的是否不同。"""
    return eff_model(opts) != eff_model(r.opts) or (opts.get('effort') or '') != (r.opts.get('effort') or '')


def api_apply_opts(b):
    """11-14：已选但要等空闲才生效的模型或思考强度，立即生效：结束当前 CLI（正在进行的回复和后台任务一起停掉），用新选项续接重启。"""
    r = RUNNERS.get(live_sid(b.get('sid') or ''))
    if not r or not r.state.get('alive'):
        raise ValueError('这个对话没有在运行')
    if not r.restart_when_idle:
        return {'ok': True}
    if r.queue or r.sending:
        raise ValueError('还有消息没发出，等发出后再切换')
    r.restart_when_idle = False
    r.turn = False
    r.turn_t = time.time()  # 用户主动停的，不算“被中断”
    r.sync_meta()
    drop_perms(r.sid)
    log('立即切换模型或思考强度，重启 claude', r.sid)
    restart_runner(r)
    return {'ok': True}


# ---------------------------------------------------------------- 钩子
def drop_perms(sid):
    """这个对话里还挂着的授权请求作废（CLI 已经不等了），界面上的授权卡跟着关掉。"""
    for rid, ent in list(PERMS.items()):
        if ent['req']['sid'] == sid:
            ent['res'] = None
            ent['ev'].set()


def resolve_sid(sid):
    seen = set()
    while sid in ALIAS and sid not in seen:
        seen.add(sid)
        sid = ALIAS[sid][0]
    return sid


def live_sid(sid):
    """刚 /clear 换了 ID 的那几秒，界面可能还拿旧 ID 发请求：转给新 ID 的 CLI，免得为旧 ID 再起一份。"""
    a = ALIAS.get(sid)
    if a and time.time() - a[1] < 15 and not RUNNERS.get(sid) and RUNNERS.get(a[0]):
        return a[0]
    return sid


def hook_runner(sid, gen):
    """钩子对应的 Runner。带代次的按代次找（/clear 换 ID 后环境变量里还是旧 ID）；找不到说明是旧进程迟到的事件。
    旧版 hook.py 不带代次，按 ID（含别名）找，兼容接收。"""
    if gen:
        for r in list(RUNNERS.values()):
            if r.gen == gen:
                return r, True
        return None, True
    return RUNNERS.get(resolve_sid(sid)), False


def rekey(r, new, source=''):
    """/clear（以及 CLI 里的 /resume）会换会话 ID：后台迁到新 ID，旧 ID 留作别名，推送事件让界面切过去。"""
    old = r.sid
    with G:
        if RUNNERS.get(old) is r:
            RUNNERS.pop(old, None)
        RUNNERS[new] = r
        r.sid, r.title, r.notice = new, '', None
        ALIAS[old] = (new, time.time())
        ALIAS.pop(new, None)
        NEW_SESS[new] = NEW_SESS.get(old) or r.cwd
        if old in WT_SESS:
            WT_SESS[new] = WT_SESS[old]
        for ent in list(PERMS.values()):
            if ent['req']['sid'] == old:
                ent['req']['sid'] = new
        so = session_opts(old)
        if so:
            save_session_opts(new, so)
    for k, v in list(PREWARM.items()):
        if v is r:
            PREWARM.pop(k, None)  # 预热的 CLI 换了 ID 就不再当预热用
    US.log_run(RUNS_PATH, 'sid', r.pkey, new)  # 第十四轮 I：/clear 后的新 ID 也算这个 CLI 的时间段
    log('会话换了 ID', old, '→', new, source)
    BUS.emit({'type': 'rekey', 'old': old, 'new': new, 'source': source or ''})
    BUS.emit({'type': 'sessions'})
    r.analyze(force=True)
    r.sync_meta()  # 第十三轮 E：真实 sid 写回 ptyd 的 meta


def turn_has_error(conv):
    for it in reversed(conv.items):
        if it.get('k') == 'error':
            return True
        if (it.get('k') == 'user' and not it.get('mid')) or (it.get('k') == 'sys' and it.get('turn')):
            return False
    return False


def note_failure(sid, body):
    """StopFailure：本轮因接口出错结束。记录里一般已有 API 错误行（实测比钩子早约 35 毫秒写入，Conv 已按 error 归类），
    有就不重复显示；等 2 秒还没有才在时间线补一条。"""
    kind = str(body.get('error') or '')
    msg = clean_text(str(body.get('last_assistant_message') or ''))
    t = get_transcript(sid)
    for _ in range(8):
        try:
            t.poll()
        except Exception:
            pass
        with t.lock:
            if turn_has_error(t.conv):
                return
        time.sleep(0.25)
    out = {'new': [], 'patch': []}
    with t.lock:
        t.conv.open = False
        t.conv._close_pending(out, '出错，未完成')
        t.conv._fork_at(out, None)
        t.conv._new({'k': 'error', 'etype': err_label(kind, msg), 'text': (msg or kind or '接口出错')[:600]}, out)
    BUS.emit({'type': 'items', 'sid': sid, 'new': out['new'], 'patch': out['patch'], 'open': False})


NOTICE_ZH = [(re.compile(r'^Claude needs your permission to use (.+)$'), r'Claude 需要授权使用 \1'),
             (re.compile(r'^Claude needs your permission$'), 'Claude 需要你授权'),
             (re.compile(r'^Claude (?:Code )?needs your (?:input|attention)$'), 'Claude 需要你处理')]


def notice_text(msg):
    msg = clean_text(msg)[:300]
    for rx, zh in NOTICE_ZH:
        if rx.match(msg):
            return rx.sub(zh, msg)
    return msg or 'CLI 在等你处理'


def handle_hook(ev, sid, body, gen=''):
    r, by_gen = hook_runner(sid, gen)
    if by_gen and not r:
        if ev in ('busy', 'idle', 'fail', 'notification', 'session'):
            log('丢弃旧进程的钩子', ev, sid[:8], gen)
        return {}
    sid = r.sid if r else resolve_sid(sid)
    if ev == 'session':
        nsid = str(body.get('session_id') or '')
        if r and nsid and nsid != r.sid and re.fullmatch(r'[0-9a-fA-F-]{36}', nsid):
            rekey(r, nsid, body.get('source'))
            sid = nsid
        tp = body.get('transcript_path')
        if tp:
            t = get_transcript(sid)
            with t.lock:
                if t.path != tp:
                    t.path, t.conv, t.offset, t.buf = tp, Conv(), 0, b''
                    BUS.emit({'type': 'reset', 'sid': sid})
        return {}
    if ev in ('busy', 'idle'):
        drop_perms(sid)
        if r:
            # 第十二轮 C：记本轮开始时间；同一轮里再来的 busy（还在忙）不重置，结束时清零
            if ev == 'idle':
                r.since = 0.0
            elif not (r.turn and r.since and r.state.get('busy')):
                r.since = time.time()
            r.turn, r.turn_at, r.notice = ev == 'busy', time.time(), None
            r.turn_t = time.time()  # 第十三轮 E：回合状态的时间，接管时和钩子事件文件比新旧
            if ev == 'busy':
                r.last_spin = ''
            r.analyze(force=True)
            r.sync_meta()
            if ev == 'idle' and r.unattended:
                BUS.emit({'type': 'notify', 'sid': sid, 'title': 'Claude 工作台', 'text': '定时任务已完成'})
        if ev == 'idle' and r:
            r.stops, r.stop_at = getattr(r, 'stops', 0) + 1, time.time()  # 第十五轮 A2：旁问按 Stop 的先后区分输入建议
        if ev == 'idle':
            mark_done(sid)  # 第十三轮 E：未读由服务端记录
            title_maybe(sid)  # 11-11：还没有标题就排队自动起
        else:
            clear_interrupted(sid)  # 第十三轮 E：新的一轮开始了，“上次回复被中断”不再显示
        BUS.emit({'type': 'sessions'})
        return {}
    if ev == 'fail':
        # 接口出错，本轮立即结束：不再等 Stop 钩子，等待中的授权作废
        log('本轮接口出错', sid[:8], body.get('error'))
        drop_perms(sid)
        if r:
            r.turn, r.turn_at, r.notice = False, time.time(), None
            r.since = 0.0  # 第十二轮 C
            r.turn_t = time.time()  # 第十三轮 E
            r.analyze(force=True)
            r.sync_meta()
        mark_done(sid)  # 第十三轮 E：出错结束也算这一轮结束了
        threading.Thread(target=note_failure, args=(sid, body), daemon=True).start()
        BUS.emit({'type': 'sessions'})
        return {}
    if ev == 'notification':
        # CLI 在等人（授权、MCP 提问）：界面上已有授权卡或认出了弹窗就不用提示
        log('CLI 等待通知', sid[:8], body.get('notification_type'), '有授权卡' if any(e['req']['sid'] == sid for e in list(PERMS.values())) else '')
        if r and not r.unattended and not any(e['req']['sid'] == sid for e in list(PERMS.values())) \
                and time.time() - getattr(r, 'perm_done_at', 0) > 3:
            r.analyze(force=True)
            if not r.state.get('dialog') and not getattr(r, 'dialog_held', False):  # 11-25：压住的弹窗过一会儿会报出来，不另出提示
                r.notice = {'text': notice_text(str(body.get('message') or '')), 't': time.time(),
                            'type': str(body.get('notification_type') or '')}
                r.analyze(force=True)
                BUS.emit({'type': 'notice', 'sid': sid, 'title': r.title or '', 'text': r.notice['text']})
        return {}
    if ev == 'permission' and r and r.unattended:
        return {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {
            'behavior': 'deny', 'message': '这是无人值守的定时任务，需要确认的操作已自动拒绝。请只做不需要确认的事，并在结尾说明哪些步骤被跳过。'}}}
    if ev in OBSERVE_HOOKS:
        try:
            OBSERVE_HOOKS[ev](r, sid, body)  # 第十五轮 A2：只观测的钩子，出错只记日志，不影响 CLI
        except Exception as e:
            log('处理钩子异常', ev, sid[:8], repr(e))
        return {}
    if ev == 'permission':
        rid = uuid.uuid4().hex[:12]
        req = {'rid': rid, 'sid': sid, 'tool': body.get('tool_name') or '', 'input': body.get('tool_input') or {},
               'suggestions': body.get('permission_suggestions') or [], 'title': r.title if r else '',
               'cwd': body.get('cwd') or (r.cwd if r else '')}
        keep = plan_fields(req, r) if req['tool'] == 'ExitPlanMode' else None  # 第十五轮 A2
        ent = {'req': req, 'ev': threading.Event(), 'res': None, 'keep': keep}
        PERMS[rid] = ent
        BUS.emit({'type': 'perm', 'req': req})
        if r:
            r.analyze(force=True)
        ent['ev'].wait(3500)
        if r:
            r.perm_done_at = time.time()  # 11-25：先记时间再撤卡，撤卡后的读屏不会把终端里还没收起的提问当弹窗报出去
        PERMS.pop(rid, None)
        BUS.emit({'type': 'perm-done', 'rid': rid})
        if r:
            r.analyze(force=True)
            r.recheck(DIALOG_HOLD)
        return ent['res'] or {}
    return {}


def decide(rid, b):
    ent = PERMS.get(rid)
    if not ent:
        return False
    d, req = b.get('decision'), ent['req']
    dec = plan_decision(ent, b) if b.get('plan') else deny_with_message(b)  # 第十五轮 A2：计划三种决定、拒绝并说明
    if dec:
        pass
    elif d in ('allow', 'always'):
        dec = {'behavior': 'allow'}
        if b.get('updatedInput') is not None:
            dec['updatedInput'] = b['updatedInput']
        elif req['tool'] in ('AskUserQuestion', 'ExitPlanMode'):
            dec['updatedInput'] = req['input']
        if d == 'always' and req['suggestions']:
            dec['updatedPermissions'] = req['suggestions']
    else:
        dec = {'behavior': 'deny', 'message': b.get('message') or '用户在界面上拒绝了这次操作。'}
        if b.get('interrupt'):
            dec['interrupt'] = True
    ent['res'] = {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': dec}}
    ent['ev'].set()
    return True


# ---------------------------------------------------------------- 第十五轮 A2：计划决定、拒绝并说明
# 计划卡的选项：按计划执行后切到哪种权限模式（钩子返回 setMode，只对本会话）。对话原本是跳过确认或自动模式时多一项“沿用”。
PLAN_KEEP = {'bypass permissions': ('bypassPermissions', '跳过确认'), 'auto mode': ('auto', '自动模式')}
PLAN_OPTS = [('accept', '按计划执行（自动处理修改）', 'acceptEdits'), ('ask', '按计划执行（每处修改问我）', 'default')]


def plan_fields(req, r):
    """ExitPlanMode 的授权请求：带上计划全文、计划文件路径和选项。返回“沿用”对应的模式（没有为 None）。"""
    inp = req['input'] if isinstance(req['input'], dict) else {}
    req['plan'] = str(inp.get('plan') or '')
    req['planPath'] = str(inp.get('planFilePath') or '')
    keep = PLAN_KEEP.get(getattr(r, 'base_mode', None) or '') if r else None
    req['options'] = ([{'key': 'keep', 'label': f'按计划执行（沿用{keep[1]}）'}] if keep else []) + \
        [{'key': k, 'label': lbl} for k, lbl, _ in PLAN_OPTS]
    return keep[0] if keep else None


def plan_decision(ent, b):
    req, key = ent['req'], str(b.get('plan') or '')
    if req['tool'] != 'ExitPlanMode':
        raise ValueError('这不是计划确认')
    if key == 'feedback':
        msg = str(b.get('message') or '').strip()
        if not msg:
            raise ValueError('请写下要怎么改')
        return {'behavior': 'deny', 'message': '用户没有批准这个计划，修改意见：' + msg[:4000]}
    mode = ent.get('keep') if key == 'keep' else next((m for k, _, m in PLAN_OPTS if k == key), None)
    if not mode:
        raise ValueError('没有这个选项')
    return {'behavior': 'allow', 'updatedInput': req['input'],
            'updatedPermissions': [{'type': 'setMode', 'mode': mode, 'destination': 'session'}]}


def deny_with_message(b):
    """授权卡“拒绝并说明”：perm {rid, allow: false, message}，说明随拒绝交给模型。"""
    if b.get('allow') is not False:
        return None
    msg = str(b.get('message') or '').strip()
    dec = {'behavior': 'deny', 'message': ('用户拒绝了这次操作，说明：' + msg[:4000]) if msg else '用户在界面上拒绝了这次操作。'}
    if b.get('interrupt'):
        dec['interrupt'] = True
    return dec


def track_mode(r, st):
    """记下对话“原本”的权限模式（不是计划模式、且稳定了 1.5 秒以上），计划卡的“沿用”项用。"""
    m = st.get('mode') if st.get('ready') else None
    if not m:
        return
    now = time.time()
    seen = getattr(r, 'mode_seen', None)
    if not seen or seen[0] != m:
        r.mode_seen = (m, now)
    elif m not in ('plan mode', 'default') and now - seen[1] >= 1.5:  # default＝底栏没认出模式（回复中常见），不算
        r.base_mode = m


# ---------------------------------------------------------------- 第十五轮 A2：读屏代按键的公用小函数
# 安全原则：每发一个键前读屏确认在预期的面板、预期的位置；确认不了就不发并返回错误；有授权弹窗等待时不代按键。
PANEL_LOCK = threading.Lock()
OPT_NUM_RE = re.compile(r'^(\d+)\.\s+(.*)$')


def perm_pending(sid):
    return any(e['req']['sid'] == sid for e in list(PERMS.values()))


def screen_wait(r, pred, timeout=5.0, step=0.1):
    """等屏幕出现预期的样子：pred(屏幕行) 返回真值就返回它，超时返回 None。CLI 退出了立即返回 None。"""
    end = time.time() + timeout
    while True:
        if not r.state.get('alive'):
            return None
        try:
            v = pred(r.screen_lines())
        except Exception:
            v = None
        if v:
            return v
        if time.time() >= end:
            return None
        time.sleep(step)


def screen_options(lines):
    """屏幕上可见的编号选项：[{n: 数字键, label: 去掉编号的文字, cur: 是否光标所在}]；认不出返回 []。"""
    d = parse_dialog(lines)
    out = []
    for i, o in enumerate((d or {}).get('options') or []):
        m = OPT_NUM_RE.match(o['label'])
        if not m:
            return []
        out.append({'n': m.group(1), 'label': m.group(2).strip(), 'cur': i == d['cur']})
    return out


def option_key(lines, text):
    """按文字全等找选项，返回它的数字键；没有或不止一个返回 None。"""
    hit = [o['n'] for o in screen_options(lines) if o['label'] == text]
    return hit[0] if len(hit) == 1 and len(hit[0]) == 1 else None


def press_if(r, key, check):
    """读屏确认 check(屏幕行) 为真才发这个键（bytes）。有授权弹窗在等、CLI 不在时不发。返回是否发了。"""
    if not r.state.get('alive') or perm_pending(r.sid):
        return False
    try:
        ok = check(r.screen_lines())
    except Exception:
        ok = False
    if not ok:
        return False
    r.write(key)
    return True


def squash(s):
    return re.sub(r'\s+', '', s or '')


def panel_begin(r, name):
    """开始一段代按键操作：对话要在运行、输入框可见、没有弹窗和授权在等、没有没发出的消息；同一对话同时只做一件。
    panel_op 在 qlock 里设置，和 analyze 里“发出排队消息”的判断互斥：设上之后不会再有消息往终端里打。"""
    if not r or not r.state.get('alive'):
        raise ValueError('这个对话没有在运行')
    if perm_pending(r.sid) or r.state.get('dialog'):
        raise ValueError('对话里有待确认的弹窗，先处理它')
    wait_until_true(lambda: not r.queue and not r.sending, 5, 0.1)  # 刚发的消息还在往终端里打字：等它打完
    if not r.state.get('ready'):
        raise ValueError('对话暂时不能操作，稍后再试')
    with PANEL_LOCK, r.qlock:
        if getattr(r, 'panel_op', None):
            raise ValueError('正在处理上一个操作，稍后再试')
        if r.queue or r.sending:
            raise ValueError('还有消息没发出，稍后再试')
        r.panel_op = name


def panel_end(r):
    r.panel_op = None
    try:
        r.analyze(force=True)  # 期间排队的消息此时发出
    except Exception as e:
        log('分析屏幕异常', r.sid, repr(e))


def type_command(r, text):
    """把一条斜杠命令打进输入框，读屏确认输入框里正好是它再按回车。确认不了就清掉输入框（只在输入框可见时）并返回 False。"""
    want = squash(text)
    if perm_pending(r.sid) or any_panel(r.screen_lines()):
        return False  # 已有面板开着时焦点在面板上，打字会被当成面板按键
    r.write(paste_bytes(text))  # 空输入框里显示的灰色提示、输入建议不算内容，粘贴后就没了
    if not screen_wait(r, lambda L: squash(input_box_text(L)) == want, 3.0):
        lines = r.screen_lines()
        if squash(input_box_text(lines)) and not any_panel(lines) and not perm_pending(r.sid):
            for _ in range(40):  # 清掉刚粘贴进输入框的字（Ctrl+U、退格只作用于输入框）
                r.key('kill-line')
                r.write(b'\x7f')
                time.sleep(0.05)
                if not input_box_text(r.screen_lines()):
                    break
        return False
    return press_if(r, b'\r', lambda L: squash(input_box_text(L)) == want)


def any_panel(lines):
    """输入框上方或代替输入框的终端面板（旁问、后台任务、添加文件夹）是否开着。"""
    j = '\n'.join(lines)
    return bool(btw_panel(lines) or tasks_open(lines) or ('Add directory to workspace' in j and 'Esc to cancel' in j))


def close_panel(r, is_open, timeout=3.0):
    """读屏确认面板开着才发 Esc（面板没开时 Esc 会中断正在进行的回复），再等它关掉。返回面板是否已关。"""
    if not press_if(r, b'\x1b', is_open):
        return not is_open(r.screen_lines())
    return bool(screen_wait(r, lambda L: not is_open(L), timeout))


def finish_panel(r, is_open, what):
    """关面板并结束这段代按键操作。有授权在等时不按键：后台等授权处理完再关，期间仍不往终端打字（排队的消息等面板关了再发）。"""
    def go(wait):
        try:
            end = time.time() + wait
            while perm_pending(r.sid) and time.time() < end and r.state.get('alive'):
                time.sleep(0.5)
            if r.state.get('alive') and not close_panel(r, is_open):
                log(what + '面板没能关上', r.sid[:8])
        except Exception as e:
            log(what + '面板关闭异常', r.sid[:8], repr(e))
        finally:
            panel_end(r)
    if perm_pending(r.sid):
        threading.Thread(target=go, args=(3600,), daemon=True).start()
    else:
        go(0)


# ---------------------------------------------------------------- 第十五轮 A2：旁问（/btw）
# 答案只在终端临时面板里、不进会话记录；全文从 SubagentStop 钩子的 last_assistant_message 拿。
# 同样 agent_type 为空的 SubagentStop 还有“输入建议”（固定在 Stop 之后）和压缩，区分办法见 btw_judge。
BTW_DIR = os.path.join(RUN_DIR, 'btw')
BTW_LOCK = threading.Lock()
BTW_WAIT = 180
BTW_MAX = 700  # 实测粘贴超过约 800 字时 CLI 输入框折叠成“[Pasted text …]”，读屏核对不了


def btw_path(sid):
    return os.path.join(BTW_DIR, re.sub(r'[^\w-]', '_', sid) + '.json')


def btw_load(sid):
    try:
        with open(btw_path(sid), encoding='utf-8') as f:
            d = json.load(f)
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
    except Exception:
        return []


def btw_save(sid, item):
    with BTW_LOCK:
        items = [x for x in btw_load(sid) if x.get('id') != item['id']] + [item]
        os.makedirs(BTW_DIR, exist_ok=True)
        tmp = btw_path(sid) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(items[-200:], f, ensure_ascii=False)
        os.replace(tmp, btw_path(sid))


def btw_panel(lines, q=None):
    """旁问面板：最后一个“/btw 问题”标题行（不是输入框那行）到“Esc to close”提示行之间。返回 (标题行号, 提示行号) 或 None。"""
    hint = next((i for i in range(len(lines) - 1, -1, -1) if 'Esc to close' in lines[i] and 'c to copy' in lines[i]), None)
    if hint is None:
        return None
    for i in range(hint - 1, -1, -1):
        s = lines[i].strip()
        if s.startswith('/btw') and not lines[i].lstrip().startswith('❯'):
            if q is not None and squash(q)[:30] not in squash(s):
                return None
            return i, hint
    return None


def btw_on_screen(lines, q, ans):
    p = btw_panel(lines, q)
    a = re.sub(r'[\W_]+', '', ans or '')[:16]
    if not p or len(a) < 2:
        return False
    return a in re.sub(r'[\W_]+', '', ''.join(lines[p[0] + 1:p[1]]))


def on_subagent(r, sid, body):
    """SubagentStop：只在有旁问在等答案时处理，其余（子代理、输入建议、压缩）直接忽略。"""
    bt = getattr(r, 'btw', None) if r else None
    if not bt or bt['ev'].is_set() or body.get('agent_type'):
        return
    ans = str(body.get('last_assistant_message') or '')
    if ans.strip():
        threading.Thread(target=btw_judge, args=(r, bt, ans), daemon=True).start()


def btw_judge(r, bt, ans):
    """认作旁问答案的条件（任一）：① 旁问面板里正显示着这段文字；② 发出旁问后没有过 Stop，且发出时离上次 Stop 已超过 20 秒
    （输入建议只跟在 Stop 后面；刚结束一轮就问，上一轮的输入建议可能还没回来，只认 ①）。"""
    try:
        ok = bool(screen_wait(r, lambda L: btw_on_screen(L, bt['q'], ans), 2.5, 0.15))
        if not ok and getattr(r, 'stops', 0) == bt['stops'] and bt['t'] - getattr(r, 'stop_at', 0) > 20:
            ok = True
        if not ok:
            log('旁问：这条 SubagentStop 不是旁问的答案，忽略', r.sid[:8])
            return
        with BTW_LOCK:
            if bt['ev'].is_set():
                return
            bt['a'] = ans
            bt['ev'].set()
    except Exception as e:
        log('旁问判断异常', repr(e))


def btw_run(r, bt):
    sid, item, err = r.sid, None, ''
    try:
        if not type_command(r, '/btw ' + bt['q']):
            err = '没能把问题交给 Claude'
        elif not screen_wait(r, lambda L: btw_panel(L, bt['q']), 10):
            err = '旁问面板没有出现'
        else:
            end = time.time() + BTW_WAIT
            while not bt['ev'].wait(0.5):
                if time.time() > end or not r.state.get('alive'):
                    break
                if not btw_panel(r.screen_lines(), bt['q']) and not screen_wait(r, lambda L: btw_panel(L, bt['q']), 1.5):
                    break  # 面板被别处关掉了
            if not bt['ev'].is_set():
                err = '没有拿到回答'
    except Exception as e:
        err = err or '旁问出错'
        log('旁问异常', sid[:8], repr(e))
    finally:
        with BTW_LOCK:
            bt['ev'].set()  # 之后到的 SubagentStop 不再认
        if getattr(r, 'btw', None) is bt:
            r.btw = None
        finish_panel(r, lambda L: btw_panel(L, bt['q']) is not None, '旁问')
    item = {'id': bt['id'], 'q': bt['q'], 'a': bt.get('a') or '', 't': bt['ms']}
    if err and not item['a']:
        item['error'] = err
    try:
        btw_save(sid, item)
    except Exception as e:
        log('保存旁问记录失败', repr(e))
    BUS.emit(dict(item, type='btw', sid=sid))


def api_btw(b):
    q = re.sub(r'\s+', ' ', safe_input(str(b.get('q') or ''))).strip()
    if not q:
        raise ValueError('问题为空')
    if len(q) > BTW_MAX:
        raise ValueError(f'问题太长了，最多 {BTW_MAX} 字')
    r = RUNNERS.get(live_sid(b.get('sid') or ''))
    panel_begin(r, 'btw')
    bt = {'id': uuid.uuid4().hex[:12], 'q': q, 't': time.time(), 'ms': int(time.time() * 1000),
          'stops': getattr(r, 'stops', 0), 'ev': threading.Event()}
    r.btw = bt
    threading.Thread(target=btw_run, args=(r, bt), daemon=True).start()
    return {'id': bt['id']}


def api_btw_list(b):
    return {'items': btw_load(b.get('sid') or '')}


# ---------------------------------------------------------------- 第十五轮 A2：停止后台任务（/tasks 面板里按 x）
TASK_ROW_RE = re.compile(r'^\s*(❯)?\s*⏺\s+(.*?)(?:\s{2,}(running|completed|failed|killed|stopped|done)\s*)?$')


def tasks_detail(lines):
    """详情页（只有一个后台任务时 /tasks 直接进这里）：返回 Command 文字，不在详情页返回 None。"""
    hint = next((i for i in range(len(lines) - 1, -1, -1) if 'x to stop' in lines[i] and 'to close' in lines[i]), None)
    top = next((i for i in range(len(lines)) if lines[i].strip() == 'Shell details'), None)
    if hint is None or top is None or top > hint:
        return None
    for i in range(top, hint):
        s = lines[i].strip()
        if s.startswith('Command:'):
            parts = [s[len('Command:'):].strip()]
            for k in range(i + 1, hint):
                t = lines[k].strip()
                if not t or re.match(r'^(Output|Status|Runtime):', t):
                    break
                parts.append(t)
            return ' '.join(parts)
    return None


def tasks_list(lines):
    """列表页：返回 {rows: [{text, cur}], stop: 能否按 x}；不在列表页返回 None。"""
    hint = next((i for i in range(len(lines) - 1, -1, -1) if 'Esc to close' in lines[i] and '↑/↓ to select' in lines[i]), None)
    top = next((i for i in range(len(lines)) if lines[i].strip() == 'Background'), None)
    if hint is None or top is None or top > hint:
        return None
    rows = []
    for i in range(top + 1, hint):
        m = TASK_ROW_RE.match(lines[i])
        if m:
            rows.append({'text': m.group(2).strip(), 'cur': bool(m.group(1)), 'st': m.group(3) or ''})
    return {'rows': rows, 'stop': 'x to stop' in lines[hint]}


def same_cmd(shown, cmd):
    """屏幕上的命令和记录里的命令是否一致：去掉空白后全等；屏幕上截断（…结尾）时按前缀比，至少 8 个字。"""
    a, b2 = squash(shown), squash(cmd)
    if not a or not b2:
        return False
    if a == b2:
        return True
    if a.endswith('…'):
        a = a[:-1]
        return len(a) >= 8 and b2.startswith(a)
    return False


def tasks_open(lines):
    return tasks_detail(lines) is not None or tasks_list(lines) is not None


def task_stop_run(r, k):
    cmd = k['cmd']
    if not type_command(r, '/tasks'):
        return '没能打开后台任务面板'
    page = screen_wait(r, lambda L: ('detail', tasks_detail(L)) if tasks_detail(L) is not None else
                       ('list', tasks_list(L)) if tasks_list(L) else None, 6)
    if not page:
        return '后台任务面板没有出现'
    if page[0] == 'detail':
        if not same_cmd(page[1], cmd):
            return '面板里的命令和要停的不一致，没有操作'
        if not press_if(r, b'x', lambda L: same_cmd(tasks_detail(L) or '', cmd)):
            return '面板变了，没有操作'
    else:
        for _ in range(30):
            lst = tasks_list(r.screen_lines())
            if not lst:
                return '面板变了，没有操作'
            hit = [i for i, x in enumerate(lst['rows']) if same_cmd(x['text'], cmd)]
            cur = next((i for i, x in enumerate(lst['rows']) if x['cur']), None)
            if len(hit) != 1:
                return '面板里找不到这条命令，或有同样的命令，没有操作' if not hit else '有不止一条同样的命令，没有操作'
            if cur is None:
                return '认不出面板里的光标位置，没有操作'
            if cur == hit[0]:
                def on_row(L):
                    x = tasks_list(L)
                    return bool(x and x['stop'] and any(y['cur'] and same_cmd(y['text'], cmd) for y in x['rows']))
                if not press_if(r, b'x', on_row):
                    return '面板变了，没有操作'
                break
            key = b'\x1b[B' if hit[0] > cur else b'\x1b[A'
            before = cur
            if not press_if(r, key, lambda L, c=before: (tasks_list(L) or {}).get('rows') and
                            next((i for i, x in enumerate(tasks_list(L)['rows']) if x['cur']), None) == c):
                return '面板变了，没有操作'
            if not screen_wait(r, lambda L, c=before: next((i for i, x in enumerate((tasks_list(L) or {}).get('rows') or []) if x['cur']), c) != c, 2):
                return '光标没有移动，没有操作'
        else:
            return '没能选中这条命令'
    return ''


def api_task_stop(b):
    sid, tid = live_sid(b.get('sid') or ''), str(b.get('id') or '')
    r = RUNNERS.get(sid)
    fp = find_transcript(sid)
    k = fp and shell_scan(sid, fp)['tasks'].get(tid)
    if not k:
        raise ValueError('找不到这个后台命令')
    if k['state']:
        return {'ok': True, 'already': True}
    if not k['cmd'].strip():
        raise ValueError('认不出这个后台命令')
    panel_begin(r, 'tasks')  # 实测回复进行中 /tasks 也立即打开同样的面板（在输入框上方），Esc 只关面板
    err = ''
    try:
        err = task_stop_run(r, k)
        if not err:
            # 按 x 后：面板整个关掉（实测只有一个任务时），或回到列表页且这条命令不再是运行中
            gone = screen_wait(r, lambda L: (not tasks_open(L) and any(l.lstrip().startswith('❯') for l in L)) or (
                tasks_list(L) is not None and not any(same_cmd(x['text'], k['cmd']) and x['st'] in ('', 'running')
                                                      for x in tasks_list(L)['rows'])), 4)
            if not gone:
                err = '按了停止，但面板里这条命令还在'
    except Exception as e:
        err = err or '停止后台任务出错'
        log('停止后台任务异常', sid[:8], repr(e))
    finally:
        finish_panel(r, tasks_open, '后台任务')
    if err:
        raise ValueError(err)
    killed = wait_until_true(lambda: (shell_scan(sid, fp)['tasks'].get(tid) or {}).get('state'), 4)
    return {'ok': True, 'confirmed': bool(killed)}


def wait_until_true(fn, timeout, step=0.2):
    end = time.time() + timeout
    while True:
        try:
            v = fn()
        except Exception:
            v = None
        if v or time.time() >= end:
            return v
        time.sleep(step)


# ---------------------------------------------------------------- 第十五轮 A2：添加文件夹（/add-dir，只选“仅本次会话”）
ADD_DIR_OPT = 'Yes, for this session'  # 第 2 项 “Yes, and remember this directory” 会写设置文件，绝不选


def adddir_panel(lines, path):
    j = '\n'.join(lines)
    return 'Add directory to workspace' in j and 'Esc to cancel' in j and squash(path) in squash(j)


def adddir_msg(lines, path):
    """命令执行后没弹面板时，CLI 在命令下面回的那行（⎿ 开头）。"""
    for i in range(len(lines) - 1, -1, -1):
        if '/add-dir' in lines[i] and lines[i].lstrip().startswith(('❯', '>')) and squash(path) in squash(''.join(lines[i:i + 3])):
            for k in range(i + 1, min(len(lines), i + 8)):
                s = lines[k].strip()
                if s.startswith('⎿'):
                    return s[1:].strip()
            return None
    return None


def api_add_dir(b):
    sid = live_sid(b.get('sid') or '')
    r = RUNNERS.get(sid)
    path = str(b.get('path') or '').strip()
    if not path:
        if not r or not r.state.get('alive'):
            raise ValueError('这个对话没有在运行')
        path = osa('tell me to activate\nPOSIX path of (choose folder with prompt "选择要添加的文件夹")')
        if not path:
            return {'ok': False, 'cancel': True}
    path = os.path.abspath(os.path.expanduser(path)).rstrip('/') or '/'
    if INPUT_CTRL_RE.search(path) or '\n' in path:
        raise ValueError('路径里有不支持的字符')
    if not os.path.isdir(path):
        raise ValueError('文件夹不存在')
    if r and path in (getattr(r, 'dirs', None) or []):
        return {'ok': True, 'dir': path}
    panel_begin(r, 'add-dir')
    err, ok, already = '', False, False
    try:
        r.dir_ev = threading.Event()
        if not type_command(r, '/add-dir ' + path):
            err = '没能把命令交给 Claude'
        else:
            got = screen_wait(r, lambda L: 'panel' if adddir_panel(L, path) else
                              ('msg', adddir_msg(L, path)) if adddir_msg(L, path) else None, 8)
            if not got:
                err = '添加文件夹的确认面板没有出现'
            elif got != 'panel':
                # 实测：已在工作范围里（当前文件夹或已添加的文件夹之内）时不弹面板，直接回一行说明
                m = got[1].lower()
                if 'already' in m or 'inside the current working directory' in m:
                    already = True
                else:
                    err = adddir_err(got[1])
            else:
                key = option_key(r.screen_lines(), ADD_DIR_OPT)
                if not key or not press_if(r, key.encode(), lambda L: adddir_panel(L, path) and option_key(L, ADD_DIR_OPT) == key):
                    err = '面板里找不到“仅本次会话”这一项，已取消'  # 面板由下面 finish_panel 按 Esc 取消
                else:
                    ok = r.dir_ev.wait(6) or bool(screen_wait(r, lambda L: any('as a working directory' in l and squash(path) in squash(l)
                                                                              for l in L), 1))
                    if not ok:
                        err = '没有确认到文件夹已添加'
    except Exception as e:
        err = err or '添加文件夹出错'
        log('添加文件夹异常', sid[:8], repr(e))
    finally:
        finish_panel(r, lambda L: adddir_panel(L, path), '添加文件夹')
    if err:
        raise ValueError(err)
    if already:
        return {'ok': True, 'dir': path, 'already': True}
    note_dir(r, path)
    return {'ok': True, 'dir': path}


def adddir_err(msg):
    m = (msg or '').lower()
    if 'not exist' in m or 'not found' in m or 'no such' in m:
        return '文件夹不存在'
    if 'not a directory' in m:
        return '这不是文件夹'
    return '没有添加成功：' + (msg or '')[:200]


def note_dir(r, path):
    if not r or not path:
        return
    path = path.rstrip('/') or '/'
    ds = list(getattr(r, 'dirs', None) or [])
    if path not in ds and os.path.realpath(path) not in [os.path.realpath(x) for x in ds]:
        r.dirs = ds + [path]
        r.analyze(force=True)
        r.sync_meta()


def on_dir(r, sid, body):
    d = str(body.get('directory') or '')
    if r and d:
        note_dir(r, d)
        ev = getattr(r, 'dir_ev', None)
        if ev:
            ev.set()


# ---------------------------------------------------------------- 第十五轮 A2：压缩开始与结束
def on_compact(phase):
    def f(r, sid, body):
        ev = {'type': 'compact', 'sid': sid, 'phase': phase, 'trigger': str(body.get('trigger') or ''),
              'summary': str(body.get('compact_summary') or '')[:200000] if phase == 'end' else ''}
        if phase == 'start':
            ev['instructions'] = str(body.get('custom_instructions') or '')[:4000]
        elif ev['summary'].strip():
            compact_save(sid, {'t': int(time.time() * 1000), 'trigger': ev['trigger'], 'summary': ev['summary']})
        BUS.emit(ev)
    return f


# 修复线：压缩摘要落盘（.run/compact/<sid>.json，每个对话只留最近几条），重开对话时随 open 带给界面，“已压缩上下文”那一行仍可展开
COMPACT_DIR = os.path.join(RUN_DIR, 'compact')
COMPACT_KEEP = 5
COMPACT_LOCK = threading.Lock()


def compact_path(sid):
    return os.path.join(COMPACT_DIR, re.sub(r'[^\w-]', '_', sid) + '.json')


def compact_load(sid):
    try:
        with open(compact_path(sid), encoding='utf-8') as f:
            d = json.load(f)
        return [x for x in d if isinstance(x, dict) and isinstance(x.get('summary'), str)] if isinstance(d, list) else []
    except Exception:
        return []


def compact_save(sid, item):
    try:
        with COMPACT_LOCK:
            items = (compact_load(sid) + [item])[-COMPACT_KEEP:]
            os.makedirs(COMPACT_DIR, exist_ok=True)
            tmp = compact_path(sid) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(items, f, ensure_ascii=False)
            os.replace(tmp, compact_path(sid))
    except Exception as e:
        log('保存压缩摘要失败', sid[:8], repr(e))


def compacts_of(sid):
    """打开对话时带给界面的压缩摘要；分叉出来的对话再带上原对话的（分叉前的压缩行也能展开）。"""
    out = compact_load(sid)
    try:
        fi = CK.fork_info(sid)
        if fi and fi.get('src'):
            out = [x for x in compact_load(fi['src']) if (x.get('t') or 0) < fi['t']] + out
    except Exception:
        pass
    return out


OBSERVE_HOOKS = {'subagent': on_subagent, 'dir': on_dir, 'compact-start': on_compact('start'), 'compact-end': on_compact('end')}


# ---------------------------------------------------------------- 第十五轮 A2：被拦截的命令
# 会改全局配置、脱离工作台或打乱读屏的命令：输入框里发出时不交给 CLI，提示在工作台哪里做。
BLOCKED_CMDS = [
    ('model', '换模型请用输入框下方的模型菜单，只对这个对话生效'), ('effort', '思考强度请用输入框下方的菜单调整'),
    ('fast', 'Fast 请用输入框下方的开关'), ('config', '会改全局设置，请在工作台“设置”里调整'),
    ('settings', '会改全局设置，请在工作台“设置”里调整'), ('theme', '会改全局主题设置，工作台不代发'),
    ('autocompact', '会写入全局设置，工作台不代发'), ('statusline', '会改全局状态栏设置，工作台不代发'),
    ('output-style', '会改输出样式设置，工作台不代发'), ('plugin', '插件请在“设置 → 扩展”里管理'),
    ('plugins', '插件请在“设置 → 扩展”里管理'), ('skills', '技能请在“设置 → 扩展”里查看'),
    ('background', '会让对话脱离工作台，不支持'), ('bg', '会让对话脱离工作台，不支持'),
    ('fork', '分叉请用每轮结束处的“从这里分叉”'), ('tui', '会切换终端界面，工作台读不了，不支持'),
    ('focus', '会改全局显示设置，工作台不代发'), ('permissions', '会改权限设置，请用输入框下方的权限菜单'),
    ('allowed-tools', '会改权限设置，请用输入框下方的权限菜单'), ('terminal-setup', '会改终端程序设置，工作台不代发'),
    ('login', '账户操作请在终端里运行 claude 完成'), ('logout', '账户操作请在终端里运行 claude 完成'),
    ('exit', '关闭对话请用工作台里的结束按钮'), ('quit', '关闭对话请用工作台里的结束按钮'),
]
BLOCKED_MAP = dict(BLOCKED_CMDS)
CMD_HEAD_RE = re.compile(r'^/([A-Za-z][\w-]*)(?=\s|$)')


def blocked_cmd(text):
    """消息开头是被拦截的命令（大小写不敏感，带参数也算）时返回 (命令, 提示)，否则 None。"""
    m = CMD_HEAD_RE.match((text or '').lstrip())
    if not m:
        return None
    name = m.group(1).lower()
    return ('/' + name, BLOCKED_MAP[name]) if name in BLOCKED_MAP else None


# ---------------------------------------------------------------- 访达与系统
def resolve_path(path, cwd, sid=None):
    p = (path or '').strip().strip('"\'')
    if p.startswith('@'):
        p = p[1:]
    if p.startswith('file://'):
        p = urlparse(p).path
    p = unquote(p)
    p = re.sub(r'#L\d+(-L?\d+)?$', '', p)
    p = re.sub(r':\d+(-\d+)?(:\d+)?$', '', p)
    p = os.path.expanduser(p)
    rel = None if os.path.isabs(p) else p
    if rel is not None:
        p = os.path.join(cwd or HOME, p)
    p = os.path.normpath(p)
    if os.path.exists(p):
        return p
    # 11-23：远程页面会把相对路径先拼成“对话文件夹/相对路径”再发来，拼出来不存在时退回相对路径接着找
    if rel is None and cwd:
        base = os.path.normpath(cwd).rstrip(os.sep) + os.sep
        if p.startswith(base):
            rel = p[len(base):]
    if not rel:
        return None
    return find_in_conv(rel, sid) or (find_rel(rel, cwd) if cwd else None)


CONV_PATH_BOUND = set(' \t\'"`=(<>[{,:;|，。：；、（“‘「【')


def find_in_conv(rel, sid):
    """11-23：回复里常只写文件名（如“文件夹里的 `改动说明.md`”），文件却不在对话文件夹下，按对话文件夹拼不出来、也搜不到。
    这时在这条对话出现过的完整路径里找：工具卡的文件、命令和输出、前面的回复。从最近往前找，取第一个存在的；
    只查已载入的记录，最多核对 300 个候选，不额外读盘。"""
    t = TRANSCRIPTS.get(sid) if sid else None
    if not t:
        return None
    parts = [x for x in rel.replace('\\', '/').split('/') if x not in ('', '.', '..')]
    if not parts:
        return None
    tail = '/' + '/'.join(parts)
    seen, n = set(), 0

    def ok(c):
        nonlocal n
        c = os.path.normpath(os.path.expanduser(c))
        if c in seen or not c.endswith(tail):
            return None
        seen.add(c)
        n += 1
        return c if os.path.exists(c) else None

    for it in reversed(list(t.conv.items)[-3000:]):
        if n > 300:
            break
        p = it.get('path')
        if isinstance(p, str) and os.path.isabs(p):
            r = ok(p)
            if r:
                return r
        for k in ('in', 'out', 'text', 'desc'):
            s = it.get(k)
            if not isinstance(s, str) or tail not in s:
                continue
            i = s.find(tail)
            while i >= 0 and n <= 300:
                end, lo = i + len(tail), max(s.rfind('\n', 0, i) + 1, i - 600)
                for j in range(lo, i + 1):  # 往前找路径开头：行首或分隔符后面的 / 或 ~/
                    if s[j] in '/~' and (j == lo or s[j - 1] in CONV_PATH_BOUND) and (s[j] == '/' or s[j:j + 2] == '~/'):
                        r = ok(s[j:end])
                        if r:
                            return r
                i = s.find(tail, end)
    return None


def find_rel(rel, cwd):
    """11-11：回复里常写相对于某个子文件夹的路径（例如在 02_产出 里说“A批/x.docx”），按对话文件夹拼不出来。
    这时在对话文件夹里按“路径结尾”找回，有多个取最近修改的。先问 Spotlight，没有索引再有限遍历（2 秒、2 万个目录封顶）。"""
    rel = os.path.normpath(rel).lstrip('./')
    name = os.path.basename(rel.rstrip('/'))
    if not name or not os.path.isdir(cwd):
        return None
    tail = os.sep + rel.rstrip('/')
    cands = []
    try:
        q = 'kMDItemFSName == "%s"' % name.replace('\\', '\\\\').replace('"', '\\"')
        out = subprocess.run(['mdfind', '-onlyin', cwd, q], capture_output=True, text=True, timeout=3).stdout.split('\n')
        cands = [x for x in out if x.endswith(tail) and os.path.exists(x)]
    except Exception:
        pass
    if not cands:
        t0, n = time.time(), 0
        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.venv', 'venv')]
            n += 1
            if n > 20000 or time.time() - t0 > 2:
                break
            if name in files or name in dirs:
                x = os.path.join(root, name)
                if x.endswith(tail):
                    cands.append(x)
    if not cands:
        return None
    return max(cands, key=lambda x: os.path.getmtime(x))


def reveal(path, cwd, sid=None):
    p = resolve_path(path, cwd, sid)
    if not p:
        return False
    subprocess.Popen(['open', p] if os.path.isdir(p) else ['open', '-R', p])
    return True


def open_path(path, cwd, sid=None):
    p = resolve_path(path, cwd, sid)
    if not p:
        return False
    subprocess.Popen(['open', p])
    return True


IMG_EXT = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
           '.webp': 'image/webp', '.bmp': 'image/bmp'}


def read_image(sid, off, path):
    t = TRANSCRIPTS.get(sid)
    fp = (t.path if t else None) or find_transcript(sid)
    if not fp:
        return None
    with open(fp, 'rb') as f:
        f.seek(off)
        r = json.loads(f.readline())
    b = (r.get('message') or {}).get('content')[path[0]]
    if len(path) > 1:
        b = b['content'][path[1]]
    src = b.get('source') or {}
    if src.get('type') == 'base64':
        return base64.b64decode(src['data']), src.get('media_type') or 'image/png'
    return None


def trash(paths):
    js = ('ObjC.import("Foundation");function run(argv){var fm=$.NSFileManager.defaultManager,n=0;'
          'argv.forEach(function(p){if(fm.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(p),null,null))n++;});'
          'return String(n);}')
    p = subprocess.run(['osascript', '-l', 'JavaScript', '-e', js] + paths, capture_output=True, text=True, timeout=30)
    return p.returncode == 0 and p.stdout.strip() == str(len(paths))


def model_label(i):
    if i == 'opusplan':
        return 'Opus 规划 + Sonnet 执行'
    s = re.sub(r'^claude-', '', i or '')
    big = '[1m]' in s
    parts = [x for x in s.replace('[1m]', '').split('-') if x and not re.fullmatch(r'\d{8}', x)]
    fam = ' '.join(x.capitalize() for x in parts if not x.isdigit())
    ver = '.'.join(x for x in parts if x.isdigit())
    return (fam + (' ' + ver if ver else '') + (' (1M)' if big else '')).strip() or i


# 第十四轮 I：模型清单按“推荐 / 更多”分组；fast 与 CLI 内部判断一致（oy()：includes("opus-4-8")||includes("opus-5")）
MODEL_REC = [('claude-opus-5-5', '日常主力，复杂任务首选'), ('claude-fable-5-1', '最强推理，适合难题，费用较高'),
             ('claude-sonnet-5-5', '更快更省，适合大多数改动'), ('claude-haiku-5-5', '最快，适合简单任务')]
MODEL_MORE = ['claude-fable-5-2', 'claude-opus-5', 'claude-sonnet-5', 'claude-fable-5', 'claude-opus-4-8', 'claude-opus-4-7',
              'claude-opus-4-6', 'claude-sonnet-4-6', 'claude-opus-4-5-20251101', 'claude-haiku-4-5-20251001', 'opusplan']
MODEL_FAMS = ('OPUS', 'FABLE', 'SONNET', 'HAIKU')


def model_fast(i):
    s = (i or '').lower()
    return 'opus-5' in s or 'opus-4-8' in s


def user_settings():
    try:
        with open(os.path.join(HOME, '.claude', 'settings.json'), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def settings_env(key, st=None):
    """CLI 实际会用到的环境变量：settings.json 的 env 优先，其次登录环境。"""
    env = (st if st is not None else user_settings()).get('env') or {}
    return env.get(key) or LOGIN_ENV.get(key) or ''


def resolve_alias(m, st=None):
    """opus / sonnet 这类别名：settings.json 或环境变量里配了 ANTHROPIC_DEFAULT_<系列>_MODEL 就换成具体 ID，否则原样。"""
    if m and m.upper() in MODEL_FAMS:
        return settings_env(f'ANTHROPIC_DEFAULT_{m.upper()}_MODEL', st) or m
    return m


def cli_default_model(st=None):
    """不带 --model 启动时 CLI 用的模型：ANTHROPIC_MODEL，其次 settings.json 的 model，再次推荐列表第一个。"""
    st = st if st is not None else user_settings()
    m = settings_env('ANTHROPIC_MODEL', st) or (st.get('model') if isinstance(st.get('model'), str) else '')
    return resolve_alias(m, st) or MODEL_REC[0][0]


def fast_ok(opts):
    """这个对话的模型支持 Fast：没指定模型时按 CLI 默认模型判断。
    第十四轮补（11-11）：Fast 功能去掉。公司网关对带 speed:fast 的请求一律返回 402，用户决定不要这个功能，这里恒为 False：
    不再注入 fastMode，切换 fast 也不会重启 CLI。"""
    return False
    return model_fast(resolve_alias((opts or {}).get('model') or '') or cli_default_model())


def model_list():
    st = user_settings()
    prefs = load_prefs()
    out = [{'id': i, 'label': model_label(i), 'desc': d, 'group': 'rec', 'fast': model_fast(i)} for i, d in MODEL_REC]
    have = {x['id'] for x in out}

    def more(i):
        if i and isinstance(i, str) and i not in have:
            have.add(i)
            out.append({'id': i, 'label': model_label(i), 'desc': '', 'group': 'more', 'fast': model_fast(i)})
    for i in MODEL_MORE:
        more(i)
    for i in prefs.get('extraModels') or []:
        more(i)
    for k in MODEL_FAMS:
        more(settings_env(f'ANTHROPIC_DEFAULT_{k}_MODEL', st))
    d = resolve_alias(prefs['defaults'].get('model') or '', st) or cli_default_model(st)
    more(d)  # 默认模型是清单外的 ID 或别名：放进“更多”，界面才找得到
    return {'default': model_label(d), 'defaultId': d, 'list': out}


def osa(script):
    try:
        p = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=900)
        return p.stdout.strip() if p.returncode == 0 else ''
    except Exception:
        return ''


# ---------------------------------------------------------------- 接口
def api_projects(b):
    return {'projects': projects_payload(), 'status': STATUS['text'], 'home': HOME, 'boot': BOOT, 'models': model_list(),
            'blockedCmds': [{'cmd': '/' + c, 'hint': h} for c, h in BLOCKED_CMDS]}  # 第十五轮 A2


def window_lim(items, b):
    """一次给界面多少条：默认最后 HIST_LIMIT 条；重新载入同一对话时按界面已加载的条数；带搜索词时一直给到第一处命中。"""
    if b.get('all'):
        return len(items)
    lim = max(HIST_LIMIT, min(int(b.get('n') or 0), len(items)))
    q = str(b.get('q') or '').strip().lower()
    if q:
        for i, it in enumerate(items):
            if any(q in str(it.get(k) or '').lower() for k in ('text', 'desc', 'path')):
                lim = max(lim, len(items) - i + 3)
                break
    return lim


def hidden_index(items):
    """还没加载的那部分：每次提问和改过的文件。顶部“目录”“改动”按完整历史列，点到没加载的再往前加载。"""
    toc, chg = [], []
    for it in items:
        if it.get('k') == 'user':
            toc.append({'id': it['id'], 'text': (it.get('text') or '')[:120], 'stat': it.get('stat'), 'imgs': bool(it.get('imgs')),
                        'uuid': it.get('uuid') or ''})
        elif it.get('k') == 'tool' and it.get('path') and it.get('name') in ('Edit', 'Write', 'NotebookEdit') and not it.get('error'):
            chg.append({'id': it['id'], 'path': it['path']})
    return toc, chg


def fork_preview(sid):
    """分叉后还没发消息：先显示原对话的历史。"""
    src = INDEX.get(FORK_SRC[sid])
    info = dict(src or {}, path=(src or {}).get('path'))
    t = Transcript(sid, info.get('path'))
    t.poll(emit=False)
    return t, info


def api_open(b):
    sid = b.get('sid') or ''
    info = INDEX.get(sid)
    r = RUNNERS.get(sid)
    if not info and not r and sid in NEW_SESS:
        info = {'cwd': NEW_SESS[sid], 'title': '新对话', 'path': None}
    if not info and not r:
        raise ValueError('找不到这个对话')
    if sid in FORK_SRC and not find_transcript(sid):
        t, info = fork_preview(sid)
        items = t.conv.items
        hid = max(0, len(items) - window_lim(items, b))
        toc, chg = hidden_index(items[:hid])
        return {'sid': sid, 'cwd': NEW_SESS.get(sid) or info.get('cwd'), 'title': load_conf().get('names', {}).get(sid) or '分叉对话',
                'items': items[hid:], 'hidden': hid, 'toc': toc, 'chg': chg, 'runner': r.public_state() if r else None,
                'model': t.conv.model, 'recent': False, 'opts': opts_out(session_opts(sid)), 'perms': []}
    t = get_transcript(sid, info and info['path'])
    t.poll(emit=False)
    items = list(t.conv.items)
    hid = max(0, len(items) - window_lim(items, b))
    toc, chg = hidden_index(items[:hid])
    recent = False
    if t.path and not (r and r.state['alive']):
        try:
            mt = os.path.getmtime(t.path)
            recent = time.time() - mt < 30 and OWN.get(sid, 0) < mt - 1  # 是工作台自己的 CLI 刚写的（比如服务刚重启）不算
        except OSError:
            pass
    title = load_conf().get('names', {}).get(sid) or (info or {}).get('title') or (r.title if r else '') or '新对话'
    return {'sid': sid, 'cwd': (info or {}).get('cwd') or r.cwd, 'title': title,
            'items': items[hid:], 'hidden': hid, 'toc': toc, 'chg': chg,
            'runner': r.public_state() if r else None, 'model': t.conv.model, 'recent': recent,
            'opts': opts_out(session_opts(sid)), 'perms': [e['req'] for e in list(PERMS.values()) if e['req']['sid'] == sid],
            # 第十三轮 E：被中断或 CLI 已退出、又没有在跑的 CLI 时，这一轮不会再继续，界面别按“别处在运行”显示运行中
            'open': t.conv.open and not (not (r and r.state['alive']) and (sid in MARKS['interrupted'] or sid in EXITS)),
            'mtime': os.path.getmtime(t.path) if t.path and os.path.exists(t.path) else 0,
            'exit': None if (r and r.state['alive']) else EXITS.get(sid),
            'interrupted': MARKS['interrupted'].get(sid),  # 第十三轮 E：上次回复被中断
            'compacts': compacts_of(sid)}  # 修复线：落盘的压缩摘要


def api_page(b):
    """长会话分页：给某一条之前的一页（默认 300 条），界面插在顶部。"""
    sid, before = b.get('sid') or '', b.get('before') or ''
    n = max(1, min(int(b.get('n') or PAGE), 5000))
    if sid in FORK_SRC and not find_transcript(sid):
        t = fork_preview(sid)[0]
    else:
        info = INDEX.get(sid)
        t = get_transcript(sid, info and info['path'])
        if not t.offset:
            t.poll(emit=False)  # 只在还没读过时读；平时由后台轮询读，这里再读会吞掉要推给界面的新条目
    items = list(t.conv.items)
    idx = next((i for i in range(len(items) - 1, -1, -1) if items[i].get('id') == before), None)
    if idx is None:
        raise ValueError('这段记录已经变了，请重新打开这个对话')
    lo = max(0, idx - n)
    return {'sid': sid, 'items': items[lo:idx], 'hidden': lo}


def api_new(b):
    cwd = (b.get('cwd') or '').rstrip('/') or '/'
    if not os.path.isdir(cwd):
        raise ValueError('文件夹不存在')
    opts = merge_opts(pref_defaults(), project_opts(cwd), b.get('opts') or {})  # 第十四轮 I：默认值 < 项目选项 < 本次选项
    wt = None
    if b.get('worktree'):
        if git(cwd, 'rev-parse', '--is-inside-work-tree')[0] != 0:
            raise ValueError('这个文件夹不是 git 仓库，不能用独立工作树')
        if not is_trusted(cwd):
            raise ValueError('这个项目还没信任过：先在这里正常开一次对话并点"信任"，再用独立工作树')
    pre = PREWARM.pop(cwd, None)
    key = opts_key_of(opts)
    if pre and not b.get('worktree') and pre.state['alive'] and pre.opts_key == key and not pre.state['dialog']:
        r, sid = pre, pre.sid
        r.prewarm = False
        if opts:
            save_session_opts(sid, opts)
        r.analyze(force=True)
    else:
        if pre:
            pre.stop()
        sid = str(uuid.uuid4())
        if b.get('worktree'):
            wt = 'cwb-' + sid[:8]
        r = ensure_runner(sid, cwd=cwd, new=True, opts=opts, worktree=wt)
    BUS.emit({'type': 'sessions'})
    return {'sid': sid, 'cwd': cwd, 'title': '新对话' + ('（独立工作树）' if wt else ''), 'items': [], 'hidden': 0,
            'runner': r.public_state(), 'model': None, 'recent': False, 'opts': opts_out(session_opts(sid)), 'perms': []}


def api_send(b):
    text = (b.get('text') or '').strip()
    images = [p for p in (b.get('images') or []) if isinstance(p, str) and os.path.isfile(p)]
    if not text and not images:
        raise ValueError('消息为空')
    bc = blocked_cmd(text)  # 第十五轮 A2：会改全局配置等的命令不交给 CLI
    if bc:
        return {'error': f'{bc[0]} 不能在工作台里发送', 'blocked': True, 'cmd': bc[0], 'hint': bc[1]}
    m = re.match(r'^/(btw|add-dir)(?:\s+(.*))?$', text, re.S | re.I)  # 这两个要读屏关面板、选“仅本次会话”，走专门的接口
    if m:
        arg = (m.group(2) or '').strip()
        rr = RUNNERS.get(live_sid(b.get('sid') or ''))
        if not rr or not rr.state.get('alive'):
            raise ValueError('对话还没有在运行，先发一条消息再用 /' + m.group(1).lower())
        if m.group(1).lower() == 'btw':
            if not arg:
                raise ValueError('在 /btw 后面写上要问的问题')
            return dict(api_btw({'sid': b.get('sid'), 'q': arg}), ok=True, btw=True)
        return dict(api_add_dir({'sid': b.get('sid'), 'path': arg.strip('"\'')}), addDir=True)
    r = ensure_runner(live_sid(b.get('sid') or ''), opts=b.get('opts'))
    clear_interrupted(r.sid)  # 第十三轮 E：继续或发了新消息，中断提示收起
    r.send(text, images, now=bool(b.get('now')))
    return {'ok': True}


def api_interrupt(b):
    r = RUNNERS.get(live_sid(b.get('sid') or ''))
    if r:
        r.turn = False
        r.turn_t = time.time()  # 第十三轮 E：用户主动中断，不算“被中断”
        r.sync_meta()
        drop_perms(r.sid)
        r.key('esc')
    return {}


def api_watch(b):
    """界面开着某个对话时定期续期，让在别处（终端、VSCode）运行的对话一直保持刷新。"""
    sid = b.get('sid') or ''
    if sid:
        t = TRANSCRIPTS.get(sid) or get_transcript(sid)
        t.touched = time.time()
    return {}


def api_send_now(b):
    """排队中的消息立即发送：用 CLI 自带的 ctrl+x ctrl+s。"""
    r = RUNNERS.get(b.get('sid') or '')
    if not r or not r.state.get('alive'):
        raise ValueError('这个对话没有在运行')
    r.key('send-now')
    return {}


def input_box_text(lines):
    """CLI 输入框里现在的文字（两条横线之间，首行去掉 ❯，续行去掉缩进）。"""
    rules = [i for i, l in enumerate(lines) if RULE_RE.match(l.strip())]
    for a, b2 in zip(rules[::-1][1:], rules[::-1]):
        if 1 <= b2 - a - 1 <= 30 and lines[a + 1].lstrip().startswith('❯'):
            body = [lines[a + 1].lstrip()[1:].strip()] + [l.strip() for l in lines[a + 2:b2]]
            return '\n'.join(x for x in body if x)
    return ''


def api_unqueue(b):
    """撤回排队中的消息：按 ↑ 把 CLI 队列里的消息收回它的输入框，确认是这几条后清空输入框，由界面放回自己的输入框。"""
    r = RUNNERS.get(b.get('sid') or '')
    if not r or not r.state.get('alive'):
        raise ValueError('这个对话没有在运行')
    want = [re.sub(r'\s+', '', t) for t in (b.get('texts') or []) if t.strip()]
    with r.qlock:
        local = [q for q in r.queue]
        r.queue.clear()
    if local and len(local) >= len(want):
        return {'ok': True}  # 还没送进 CLI，直接从本地队列拿掉
    r.key('up')
    got = ''
    for _ in range(10):
        time.sleep(0.15)
        got = input_box_text(r.screen_lines())
        if got:
            break
    flat = re.sub(r'\s+', '', got)
    if not flat or not any(w[:20] in flat for w in want):
        if flat:  # 收回来的是历史提问（排队的已经发出去了），清掉，别留在 CLI 输入框里
            for _ in range(60):
                r.key('kill-line'); r.write(b'\x7f')
                time.sleep(0.05)
                if not input_box_text(r.screen_lines()):
                    break
        return {'ok': False, 'error': '已经发给 Claude 了，撤回不了'}
    for _ in range(80):
        r.key('kill-line'); r.write(b'\x7f')
        time.sleep(0.05)
        if not input_box_text(r.screen_lines()):
            break
    return {'ok': not input_box_text(r.screen_lines())}


def api_control(b):
    """模型和思考强度用启动参数在本对话内生效（/model、/effort 会改全局默认，不用）。"""
    sid = b.get('sid') or ''
    opts = session_opts(sid)
    old_fast = bool(opts.get('fast')) and fast_ok(opts)
    restart = False
    for k in ('model', 'effort'):
        if k in b:
            v = b.get(k) or None
            if opts.get(k) != v:
                restart = True
            if v:
                opts[k] = v
            else:
                opts.pop(k, None)
    if 'mode' in b:
        if b.get('mode') and b['mode'] != 'default':
            opts['mode'] = b['mode']
        else:
            opts.pop('mode', None)  # “默认”＝不指定，下次启动按 CLI 的全局默认
    if isinstance(b.get('fast'), bool):  # 第十四轮 I：Fast 经 --settings 的 fastMode 注入，和换模型一样要重启；模型不支持时注入不变，不用重启
        if b['fast']:
            opts['fast'] = True
        else:
            opts.pop('fast', None)
    if (bool(opts.get('fast')) and fast_ok(opts)) != old_fast:
        restart = True
    save_session_opts(sid, opts)
    r = RUNNERS.get(sid)
    if r and r.state['alive']:
        if restart and not run_differs(r, opts) and bool(r.opts.get('fast')) == (bool(opts.get('fast')) and fast_ok(opts)):
            r.restart_when_idle = False  # 11-14：选回了 CLI 正在用的模型和强度，不用重启，取消待生效
        elif restart:
            if r.active():
                r.restart_when_idle = True
            else:
                restart_runner(r)
        elif 'mode' in b and b.get('mode') and b['mode'] != 'default':
            threading.Thread(target=r.set_mode, args=(b['mode'],), daemon=True).start()
    return {'opts': opts_out(opts), 'pending': bool(r and r.state['alive'] and r.restart_when_idle)}


PRESS_KEYS = {'1': b'1', '2': b'2', '3': b'3', 'up': b'\x1b[A', 'down': b'\x1b[B', 'enter': b'\r', 'esc': b'\x1b'}


def api_press(b):
    """CLI 在等人处理、界面又没认出弹窗时，提示条上的按键。只允许这几个键。"""
    r = RUNNERS.get(live_sid(b.get('sid') or ''))
    k = str(b.get('key') or '')
    if k not in PRESS_KEYS:
        raise ValueError('不支持这个按键')
    if not r or not r.state.get('alive'):
        raise ValueError('这个对话没有在运行')
    r.write(PRESS_KEYS[k])
    r.answered_at = time.time()
    if r.notice and k not in ('up', 'down'):
        r.notice['t'] = 0  # 选定或取消之后，输入框一回来就收起提示
    threading.Timer(0.8, lambda: r.analyze(force=True)).start()
    return {}


def api_dialog(b):
    r = RUNNERS.get(live_sid(b.get('sid') or ''))
    if not r:
        return {}
    want = str(b.get('id') or '')  # 11-25：界面点的是哪个弹窗；屏幕上已经换成别的弹窗时不按
    if want and (r.state.get('dialog') or {}).get('id') != want:
        log('弹窗已变，忽略这次操作', r.sid[:8], want)
        return {'stale': True}
    if 'option' in b or b.get('key'):
        r.answered_at = time.time()
    if 'option' in b:
        threading.Thread(target=r.choose, args=(int(b['option']), want or None), daemon=True).start()
    elif b.get('key') and want:
        threading.Thread(target=r.dialog_key, args=(b['key'], want), daemon=True).start()
    elif b.get('key'):
        r.key(b['key'])
    return {}


def api_perm(b):
    return {'ok': decide(b.get('rid') or '', b)}


def api_reveal(b):
    return {'ok': reveal(b.get('path') or '', b.get('cwd'), b.get('sid'))}


def api_open_url(b):
    url = b.get('url') or ''
    if re.match(r'^https?://', url, re.I):
        subprocess.Popen(['open', url])
        return {'ok': True}
    return {'ok': False}


def api_add_folder(b):
    p = osa('tell me to activate\nPOSIX path of (choose folder with prompt "选择项目文件夹")')
    p = p.rstrip('/')
    if not p:
        return {'cwd': None}
    c = load_conf()
    if p not in c.setdefault('folders', []):
        c['folders'].append(p)
        save_conf(c)
    return {'cwd': p}


def api_pick_files(b):
    out = osa('tell me to activate\nset fs to choose file with prompt "选择文件" with multiple selections allowed\n'
              'set out to ""\nrepeat with f in fs\nset out to out & POSIX path of f & linefeed\nend repeat\nreturn out')
    return {'paths': [x for x in out.split('\n') if x.strip()]}


def api_stop(b):
    r = RUNNERS.get(b.get('sid') or '')
    if r:
        r.stop()
    return {}


def api_screen(b):
    r = RUNNERS.get(b.get('sid') or '')
    out = {'lines': r.screen_lines() if r else [], 'live_hist': getattr(r, 'live_hist', []) if r else [], 'daemon': PTYD.status()}
    if r:
        # 第十三轮 E：同时给 ptyd 那份屏幕快照，方便对照排查；redraw=1 时先请 CLI 整屏重画
        if b.get('redraw'):
            r.redraw()
            time.sleep(1)
        try:
            h = PTYD.request({'op': 'snapshot', 'key': r.pkey}, timeout=5)
            sc = h.get('screen') or {}
            with r.lock:
                mine = PT.screen_rows(r.screen)
            out['ptyd'] = {'lines': sc.get('lines'), 'cursor': sc.get('cursor'), 'seq': h.get('seq'), 'server_seq': r.seq,
                           'same': mine == sc.get('lines') and [r.screen.cursor.x, r.screen.cursor.y] == sc.get('cursor'),
                           'pid': h.get('pid'), 'alive': h.get('alive'), 'pending': h.get('pending'), 'meta': h.get('meta'),
                           'ring_tail': (h.get('_body') or b'')[-2000:].decode('utf-8', 'replace')}
        except Exception as e:
            out['ptyd'] = {'error': str(e)}
    return out


def api_open_file(b):
    return {'ok': open_path(b.get('path') or '', b.get('cwd'), b.get('sid'))}


def api_upload(b):
    """粘贴或拖入的文件内容存成本地文件，供 CLI 以路径方式读取。"""
    raw = base64.b64decode(b.get('data') or '')
    if not raw or len(raw) > 40 * 1024 * 1024:
        raise ValueError('文件为空或超过 40MB')
    name = re.sub(r'[\\/:\0]', '_', os.path.basename(b.get('name') or 'file')) or 'file'
    if '.' not in name and (b.get('type') or '').startswith('image/'):
        name += '.' + b['type'].split('/', 1)[1].split('+')[0]
    d = os.path.join(RUN_DIR, 'uploads', time.strftime('%Y%m%d'))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, time.strftime('%H%M%S-') + secrets.token_hex(2) + '-' + name)
    with open(p, 'wb') as f:
        f.write(raw)
    return {'path': p}


def api_locate(b):
    """拖入的文件浏览器不给路径：按文件名、大小、修改时间用 Spotlight 找回原文件。"""
    out = []
    for it in (b.get('items') or [])[:20]:
        name, size, mt = it.get('name') or '', it.get('size'), (it.get('mtime') or 0) / 1000
        found = None
        if name:
            q = 'kMDItemFSName == "%s"' % name.replace('\\', '\\\\').replace('"', '\\"')
            try:
                res = subprocess.run(['mdfind', q], capture_output=True, text=True, timeout=6).stdout.split('\n')
            except Exception:
                res = []
            for p in res:
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if os.path.isfile(p) and (size is None or st.st_size == size) and (not mt or abs(st.st_mtime - mt) < 3):
                    found = p
                    break
        out.append(found)
    return {'paths': out}


def api_archive(b):
    sid = b.get('sid') or ''
    with G:
        c = load_conf()
        a = [x for x in c.get('archived', []) if x != sid]
        if b.get('on', True):
            a.append(sid)
        c['archived'] = a
        save_conf(c)
    BUS.emit({'type': 'sessions'})
    return {'ok': True}


def api_delete(b):
    """删除对话：会话记录移到废纸篓（可从废纸篓恢复）。"""
    sid = b.get('sid') or ''
    r = RUNNERS.get(sid)
    if r and r.state['alive']:
        r.stop()
        time.sleep(0.8)
    fp = find_transcript(sid)
    if not fp:
        raise ValueError('找不到这个对话的记录')
    paths = [fp] + ([fp[:-6]] if os.path.isdir(fp[:-6]) else [])
    if not trash(paths):
        raise ValueError('移到废纸篓失败')
    with G:
        c = load_conf()
        c['archived'] = [x for x in c.get('archived', []) if x != sid]
        c.get('session_opts', {}).pop(sid, None)
        save_conf(c)
        TRANSCRIPTS.pop(sid, None)
    INDEX.scan(force=True)
    BUS.emit({'type': 'sessions'})
    return {'ok': True}


def api_delete_project(b):
    """11-23 删除项目（已归档项目菜单里的“删除项目”）：项目下的对话记录全部移到废纸篓，项目从左栏去掉
    （手动添加、归档、新对话默认设置一并清掉）。访达里的文件夹本身不动；~/.claude/projects 里的 memory 等非对话文件也不动。"""
    cwd = b.get('cwd') or ''
    if not cwd.startswith('/'):
        raise ValueError('项目路径不对')
    key = os.path.realpath(cwd)
    same = lambda c: bool(c) and os.path.realpath(c.split('/.claude/worktrees/', 1)[0]) == key  # 独立工作树的对话归原项目，和左栏分组一致
    mine = [s for s in INDEX.scan(force=True) if same(s['cwd'])]
    sids = {s['sid'] for s in mine}
    stopped = False
    for r in list(RUNNERS.values()):  # 还在跑的（含还没写记录的新对话）先停掉
        if r.state['alive'] and (r.sid in sids or (not r.prewarm and same(r.cwd))):
            r.stop()
            stopped = True
            sids.add(r.sid)
    if stopped:
        time.sleep(0.8)
    root = os.path.realpath(PROJECTS_DIR) + os.sep
    paths = []
    for s in mine:
        fp = s.get('path') or find_transcript(s['sid'])
        if not fp or not os.path.exists(fp) or not os.path.realpath(fp).startswith(root):
            continue  # 只动 ~/.claude/projects 里的记录，别处一概不碰
        paths += [fp] + ([fp[:-6]] if os.path.isdir(fp[:-6]) else [])
    if paths and not trash(paths):
        raise ValueError('移到废纸篓失败')
    for d in {os.path.dirname(p) for p in paths}:
        try:
            os.rmdir(d)  # 记录目录空了就顺手删掉；里面还有 memory 等文件时删不掉，保留
        except OSError:
            pass
    err = ''
    for c in list(getattr(globals().get('PV'), 'CONVS', {}).values()):  # Codex、Antigravity 对话走它们自己的删除
        if getattr(c, 'dropped', False) or not same(remap_cwd(c.cwd)):
            continue
        try:
            API['delete']({'sid': c.sid})
            sids.add(c.sid)
        except Exception as e:
            err = str(e)
    with G:
        c = load_conf()
        for k in ('archived', 'pinned'):
            if isinstance(c.get(k), list):
                c[k] = [x for x in c[k] if x not in sids]
        for k in ('names', 'session_opts'):
            if isinstance(c.get(k), dict):
                c[k] = {x: v for x, v in c[k].items() if x not in sids}
        if not err:  # 有对话没删掉时项目留着（仍在“已归档”里），免得剩下的对话跑回主列表
            for k in ('folders', 'hidden_projects'):
                if isinstance(c.get(k), list):
                    c[k] = [x for x in c[k] if os.path.realpath(x) != key]
            if isinstance(c.get('project_opts'), dict):
                c['project_opts'] = {x: v for x, v in c['project_opts'].items() if os.path.realpath(x) != key}
        save_conf(c)
        for sid in sids:
            TRANSCRIPTS.pop(sid, None)
    INDEX.scan(force=True)
    BUS.emit({'type': 'sessions'})
    if err:
        raise ValueError('有对话没删掉：' + err)
    return {'ok': True, 'sessions': len(sids)}


# ---------------------------------------------------------------- 第三轮：搜索、文件补全、命令、子代理、改名置顶
class Search:
    """对话内容索引：只收用户消息、回复正文、工具说明和路径，不收工具输出和图片。"""

    def __init__(self):
        self.docs, self.lock, self.ready = {}, threading.Lock(), False

    @staticmethod
    def extract(fp):
        parts, total = [], 0
        try:
            f = open(fp, 'rb')
        except OSError:
            return ''
        with f:
            for line in f:
                if SKIP_ROW_RE.match(line) or total > 3_000_000:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('isSidechain') or r.get('isMeta') or r.get('type') not in ('user', 'assistant'):
                    continue
                c = (r.get('message') or {}).get('content')
                if r['type'] == 'user':
                    if isinstance(c, list) and any(isinstance(x, dict) and x.get('type') == 'tool_result' for x in c):
                        continue
                    t = clean_text(text_of(c))
                    if '<command-' in t or '<local-command' in t:
                        continue
                else:
                    seg = []
                    for x in c or []:
                        if not isinstance(x, dict):
                            continue
                        if x.get('type') == 'text':
                            seg.append(x.get('text') or '')
                        elif x.get('type') == 'tool_use':
                            v = tool_view(base_tool(x.get('name') or ''), x.get('input') or {})
                            seg.append(' '.join(filter(None, [v.get('desc'), v.get('path'), (v.get('in') or '')[:300]])))
                    t = '\n'.join(seg)
                if t:
                    parts.append(t)
                    total += len(t)
        return '\n'.join(parts)

    def refresh(self):
        for s in INDEX.scan():
            try:
                st = os.stat(s['path'])
            except OSError:
                continue
            d = self.docs.get(s['path'])
            if d and d[0] == st.st_mtime:
                continue
            txt = self.extract(s['path'])
            with self.lock:
                self.docs[s['path']] = (st.st_mtime, txt, txt.lower())
        self.ready = True

    def query(self, q, limit=40):
        ql = q.lower().strip()
        if len(ql) < 2:
            return []
        by_path = {s['path']: s for s in INDEX.scan()}
        names = load_conf().get('names', {})
        res = []
        with self.lock:
            items = list(self.docs.items())
        for p, (mt, txt, low) in items:
            i = low.find(ql)
            s = by_path.get(p)
            if i < 0 or not s:
                continue
            a = max(0, i - 24)
            snip = ' '.join(txt[a:i + len(ql) + 56].split())
            res.append({'sid': s['sid'], 'cwd': s['cwd'], 'title': names.get(s['sid']) or s['title'], 'mtime': s['mtime'],
                        'snip': ('…' if a else '') + snip, 'hits': low.count(ql)})
        res.sort(key=lambda x: -x['mtime'])
        return res[:limit]


SEARCH = Search()


def search_loop():
    while True:
        try:
            SEARCH.refresh()
        except Exception as e:
            log('建立搜索索引异常', repr(e))
        time.sleep(30)


def api_search(b):
    if SEARCH.ready:
        threading.Thread(target=SEARCH.refresh, daemon=True).start()
    return {'results': SEARCH.query(b.get('q') or ''), 'ready': SEARCH.ready}


FILES_CACHE = {}
SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.next', 'dist', 'build', '.cache', 'Library',
             '.Trash', '.npm', '.cargo', '.rustup', '.local', '.vscode', '.idea', 'Pods', 'DerivedData'}


def list_files(cwd):
    c = FILES_CACHE.get(cwd)
    if c and time.time() - c[0] < 30:
        return c[1]
    files = []
    try:
        p = subprocess.run(['git', '-C', cwd, 'ls-files', '-co', '--exclude-standard', '-z'], capture_output=True, timeout=8)
        if p.returncode == 0:
            files = [x for x in p.stdout.decode('utf-8', 'ignore').split('\0') if x]
    except Exception:
        files = []
    if not files:
        for root, dirs, names in os.walk(cwd):
            depth = root[len(cwd):].count(os.sep)
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')] if depth < 7 else []
            for n in names:
                if not n.startswith('.'):
                    files.append(os.path.relpath(os.path.join(root, n), cwd))
            if len(files) > 30000:
                break
    dirs = sorted({x.rsplit('/', 1)[0] + '/' for x in files if '/' in x})
    allp = dirs + files
    FILES_CACHE[cwd] = (time.time(), allp)
    return allp


def fuzzy_score(q, p):
    pl = p.lower()
    base = pl.rstrip('/').rsplit('/', 1)[-1]
    if q in base:
        return (0 if base.startswith(q) else 1) + len(p) / 1000
    if q in pl:
        return 2 + len(p) / 1000
    i, gaps, last = 0, 0, -1
    for k, ch in enumerate(pl):
        if i < len(q) and ch == q[i]:
            if last >= 0:
                gaps += k - last - 1
            last, i = k, i + 1
    return 4 + gaps / 50 + len(p) / 1000 if i == len(q) else None


def api_files(b):
    cwd = b.get('cwd') or ''
    if not os.path.isdir(cwd):
        return {'files': []}
    q = (b.get('q') or '').lower().strip()
    files = list_files(cwd)
    if not q:
        return {'files': [x for x in files if '/' not in x.rstrip('/')][:40]}
    scored = []
    for p in files:
        s = fuzzy_score(q, p)
        if s is not None:
            scored.append((s, p))
    scored.sort()
    return {'files': [p for _, p in scored[:40]]}


# 第十五轮 A2：内置命令只放实测能直接执行、不改配置的；group：chat 对话 / review 审查
BUILTIN_CMDS = [('compact', '压缩上下文，释放空间，后面可写侧重说明', 'chat'), ('plan', '进入规划模式，后面可写要规划的任务', 'chat'),
                ('btw', '旁问：不打断当前回复，问答不进对话记录', 'chat'), ('context', '查看上下文占用明细', 'chat'),
                ('recap', '用一句话总结这次对话', 'chat'), ('export', '把对话导出成文本文件，后面写文件路径', 'chat'),
                ('init', '为项目生成 CLAUDE.md 说明文件', 'chat'),
                ('code-review', '审查当前改动，找出问题', 'review'), ('security-review', '安全审查当前分支的改动', 'review'),
                ('simplify', '检查改动代码的复用与精简，并直接修改', 'review')]


def read_front(p):
    try:
        with open(p, encoding='utf-8', errors='ignore') as f:
            head = f.read(4000)
    except OSError:
        return {}
    m = re.match(r'^---\s*\n(.*?)\n---', head, re.S)
    out = {}
    if m:
        for k in ('name', 'description'):
            mm = re.search(r'^%s:\s*(.+)$' % k, m.group(1), re.M)
            if mm:
                out[k] = mm.group(1).strip().strip('"\'')
    return out


def api_commands(b):
    cwd = b.get('cwd') or ''
    seen, out = set(), []

    def add(name, desc, src, group=None):
        if name and name not in seen and name.lower() not in BLOCKED_MAP:
            seen.add(name)
            out.append(dict({'name': name, 'desc': (desc or '')[:90], 'src': src}, **({'group': group} if group else {})))
    for n, d, g in BUILTIN_CMDS:
        add(n, d, '内置', g)
    plugin_dirs = []
    try:
        with open(os.path.join(HOME, '.claude', 'settings.json'), encoding='utf-8') as f:
            pd = (json.load(f).get('env') or {}).get('CLAUDE_CODE_PLUGIN_DIRS') or ''
        plugin_dirs = [x for x in re.split(r'[:,]', pd) if x.strip()]
    except Exception:
        pass
    roots = [(os.path.join(cwd, '.claude'), '项目', ''), (os.path.join(HOME, '.claude'), '个人', '')]
    for d in plugin_dirs:
        name = read_front(os.path.join(d, '.claude-plugin', 'plugin.json')).get('name') or os.path.basename(d.rstrip('/'))
        try:
            with open(os.path.join(d, '.claude-plugin', 'plugin.json'), encoding='utf-8') as f:
                name = json.load(f).get('name') or name
        except Exception:
            pass
        roots.append((d, '插件', name + ':'))
    for root, src, prefix in roots:
        for sk in sorted(glob.glob(os.path.join(root, 'skills', '*', 'SKILL.md'))):
            fm = read_front(sk)
            add(prefix + (fm.get('name') or os.path.basename(os.path.dirname(sk))), fm.get('description'), src)
        for cm in sorted(glob.glob(os.path.join(root, 'commands', '*.md'))):
            fm = read_front(cm)
            add(prefix + os.path.splitext(os.path.basename(cm))[0], fm.get('description'), src)
    return {'commands': out}


def api_subagent(b):
    sid, agent = b.get('sid') or '', re.sub(r'[^\w-]', '', b.get('agent') or '')
    fp = find_transcript(sid)
    if not fp or not agent:
        return {'items': []}
    p = os.path.join(fp[:-6], 'subagents', f'agent-{agent}.jsonl')
    if not os.path.isfile(p):
        return {'items': []}
    conv, out = Conv(sidechain=True), {'new': [], 'patch': []}
    with open(p, 'rb') as f:
        for line in f:
            if SKIP_ROW_RE.match(line):
                continue
            try:
                conv.feed(json.loads(line), out)
            except Exception:
                continue
    return {'items': conv.items[-400:]}


AGENT_CACHE = {}


def _iso(t):
    try:
        return datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def agent_scan(p):
    """增量读子代理记录：开始时间、工具次数、输出 token、最近一步、最后的结束标记。"""
    st = os.stat(p)
    c = AGENT_CACHE.get(p)
    if not c or st.st_size < c['off']:
        c = AGENT_CACHE[p] = {'off': 0, 'buf': b'', 'tools': 0, 'out': 0, 'start': None, 'last': '', 'stop': None}
    if st.st_size > c['off']:
        with open(p, 'rb') as f:
            f.seek(c['off'])
            data = c['buf'] + f.read(st.st_size - c['off'])
        c['off'] = st.st_size
        rows = data.split(b'\n')
        c['buf'] = rows.pop()
        for l in rows:
            if c['start'] is not None and b'"assistant"' not in l[:4000]:
                continue
            try:
                r = json.loads(l)
            except Exception:
                continue
            if c['start'] is None and r.get('timestamp'):
                c['start'] = _iso(r['timestamp'])
            if r.get('type') != 'assistant':
                continue
            m = r.get('message') or {}
            c['out'] += (m.get('usage') or {}).get('output_tokens') or 0
            c['stop'] = m.get('stop_reason')
            for x in m.get('content') or []:
                if not isinstance(x, dict):
                    continue
                if x.get('type') == 'tool_use':
                    c['tools'] += 1
                    inp = x.get('input') or {}
                    d = inp.get('description') or inp.get('file_path') or inp.get('pattern') or inp.get('url') or inp.get('command') or ''
                    if isinstance(d, str) and d.startswith('/'):
                        d = shorten_home(d)
                    c['last'] = (base_tool(x.get('name') or '') + ' ' + str(d).strip().split('\n')[0][:80]).strip()
                elif x.get('type') == 'text' and (x.get('text') or '').strip():
                    c['last'] = x['text'].strip().split('\n')[0][:80]
    c['mtime'] = st.st_mtime
    return c


def shorten_home(p):
    return '~' + p[len(HOME):] if HOME and p.startswith(HOME + '/') else p


def api_agents(b):
    """当前对话的子代理：读 <会话>/subagents/agent-*.meta.json 和对应记录。"""
    sid = b.get('sid') or ''
    fp = find_transcript(sid)
    if not fp:
        return {'agents': []}
    r = RUNNERS.get(sid)
    alive = bool(r and r.state.get('alive'))
    now, out = time.time(), []
    for mp in glob.glob(os.path.join(fp[:-6], 'subagents', 'agent-*.meta.json')):
        jp = mp[:-len('.meta.json')] + '.jsonl'
        if not os.path.isfile(jp):
            continue
        try:
            meta = json.load(open(mp, encoding='utf-8'))
        except Exception:
            meta = {}
        try:
            c = agent_scan(jp)
        except Exception:
            continue
        done = bool(meta.get('finished')) or c['stop'] in ('end_turn', 'stop_sequence', 'max_tokens', 'refusal')
        if done and re.match(r'(API Error|Error:|Request timed out)', c['last'] or ''):
            state = 'err'
        elif done:
            state = 'done'
        elif now - c['mtime'] < (900 if alive else 180):
            state = 'run'
        else:
            state = 'stale'
        out.append({'id': os.path.basename(jp)[6:-6], 'desc': meta.get('description') or '', 'type': meta.get('agentType') or '',
                    'bg': meta.get('requestShape') == 'background', 'tool_id': meta.get('toolUseId') or '',
                    'state': state, 'start': c['start'], 'end': None if state == 'run' else c['mtime'],
                    'tools': c['tools'], 'out': c['out'], 'last': c['last']})
    out.sort(key=lambda a: (a['state'] != 'run', -(a['start'] or 0)))
    return {'agents': out[:60], 'shells': shell_list(sid, fp, r)}


# ---------------------------------------------------------------- 11-13：后台命令（Bash 带 run_in_background，或超过 2 分钟自动转后台）
# CLI 记录里：Bash 的 tool_use（说明、命令）→ tool_result 的 toolUseResult.backgroundTaskId 和“Output is being written to: …”
# → 结束时一条 <task-notification>（queue-operation enqueue / attachment queued_command，带 status 和退出码）；TaskStop 停掉的看它的结果。
# 按会话增量读记录，只看含关键字的行。没有结束记录时：本次 CLI 进程还活着且命令是这个进程里启动的算运行中，否则算未完成。
SHELLS, SH_LOCK = {}, threading.Lock()
TASK_TAG_RE = re.compile(r'<(task-id|output-file|status|summary)>(.*?)</\1>', re.S)
ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07')


def iso_ts(v):
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def shell_scan(sid, fp):
    with SH_LOCK:
        c = SHELLS.get(sid)
        if not c or c['path'] != fp:
            c = SHELLS[sid] = {'path': fp, 'off': 0, 'tools': {}, 'tasks': {}}
        try:
            size = os.path.getsize(fp)
        except OSError:
            return c
        if size < c['off']:
            c.update(off=0, tools={}, tasks={})
        if size == c['off']:
            return c
        with open(fp, 'rb') as f:
            f.seek(c['off'])
            data = f.read(size - c['off'])
        end = data.rfind(b'\n') + 1
        c['off'] += end
        for raw in data[:end].splitlines():
            if not any(k in raw for k in (b'"Bash"', b'backgroundTaskId', b'<task-notification>', b'"task_id"')):
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            typ, t = o.get('type'), iso_ts(o.get('timestamp'))
            msg = o.get('message') if isinstance(o.get('message'), dict) else {}
            blocks = msg.get('content') if isinstance(msg.get('content'), list) else []
            if typ == 'assistant':
                for b in blocks:
                    if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'Bash':
                        inp = b.get('input') if isinstance(b.get('input'), dict) else {}
                        c['tools'][b.get('id')] = (str(inp.get('description') or '')[:120], str(inp.get('command') or '')[:400], t)
                if len(c['tools']) > 3000:
                    for k in list(c['tools'])[:1000]:
                        c['tools'].pop(k, None)
            elif typ == 'user' and isinstance(o.get('toolUseResult'), dict):
                r = o['toolUseResult']
                tid = str(r.get('backgroundTaskId') or '')
                if re.fullmatch(r'[A-Za-z0-9_-]{4,40}', tid) and tid not in c['tasks']:
                    res = next((b for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_result'), {})
                    txt = res.get('content') if isinstance(res.get('content'), str) else ' '.join(
                        x.get('text', '') for x in (res.get('content') or []) if isinstance(x, dict))
                    m = re.search(r'Output is being written to: (\S+?\.output)', txt or '')
                    desc, cmd, t0 = c['tools'].get(res.get('tool_use_id'), ('', '', None))
                    c['tasks'][tid] = {'id': tid, 'tool_id': res.get('tool_use_id') or '', 'desc': desc, 'cmd': cmd,
                                       'start': t0 or t, 'auto': bool(r.get('timedOutAfterMs')), 'out': m.group(1) if m else '',
                                       'state': '', 'end': None, 'code': None}
                elif r.get('task_id') and 'stopped' in str(r.get('message') or '').lower():
                    k = c['tasks'].get(str(r['task_id']))
                    if k and not k['state']:
                        k.update(state='killed', end=t)
            if b'<task-notification>' in raw and typ in ('queue-operation', 'attachment'):
                txt = o.get('content') if typ == 'queue-operation' else (o.get('attachment') or {}).get('prompt')
                if isinstance(txt, str) and txt.lstrip().startswith('<task-notification>'):
                    f = dict(TASK_TAG_RE.findall(txt))
                    k = c['tasks'].get(f.get('task-id', ''))
                    if k and not k['state']:
                        m = re.search(r'exit code (-?\d+)', f.get('summary', ''))
                        code, st = (int(m.group(1)) if m else None), f.get('status', '')
                        k.update(state='done' if st == 'completed' and not code else 'killed' if st in ('killed', 'stopped') else 'err',
                                 end=t, code=code)
                        if f.get('output-file') and not k['out']:
                            k['out'] = f['output-file']
        return c


def shell_path(sid, k):
    """输出文件只认 CLI 临时目录里这个会话的 tasks/<id>.output。"""
    p = os.path.realpath(k.get('out') or '') if k.get('out') else ''
    if p and '/claude-' in p and p.endswith(os.sep.join(['', sid, 'tasks', k['id'] + '.output'])) and os.path.isfile(p):
        return p
    for g in glob.glob(os.path.join(tempfile.gettempdir(), f'claude-{os.getuid()}', '*', sid, 'tasks', k['id'] + '.output')) + \
            glob.glob(f'/tmp/claude-{os.getuid()}/*/{sid}/tasks/{k["id"]}.output'):
        return os.path.realpath(g)
    return ''


def tail_text(p, n=80, cap=48000):
    try:
        size = os.path.getsize(p)
        with open(p, 'rb') as f:
            f.seek(max(0, size - cap))
            data = f.read(cap)
    except OSError:
        return '', 0, False
    txt = ANSI_RE.sub('', data.decode('utf-8', 'replace'))
    lines = [l.rsplit('\r', 1)[-1].rstrip() for l in txt.replace('\r\n', '\n').split('\n')]
    if size > cap:
        lines = lines[1:]  # 截断处的半行不要
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines[-n:]), size, size > cap or len(lines) > n


def shell_list(sid, fp, r=None):
    c = shell_scan(sid, fp)
    alive = bool(r and r.state.get('alive'))
    born = float(getattr(r, 'started', 0) or 0)
    now, out = time.time(), []
    for k in list(c['tasks'].values())[-40:]:
        state = k['state'] or ('run' if alive and (k['start'] or now) >= born - 5 else 'stale')
        p = shell_path(sid, k)
        last, size = '', 0
        if p:
            tx, size, _ = tail_text(p, 6, 4000)
            last = next((l.strip() for l in reversed(tx.split('\n')) if l.strip()), '')[:200]
        end = k['end'] if state != 'run' else None
        if state == 'stale' and p:
            end = os.path.getmtime(p)
        out.append({'id': k['id'], 'desc': k['desc'], 'cmd': k['cmd'][:200], 'tool_id': k['tool_id'], 'auto': k['auto'],
                    'state': state, 'start': k['start'], 'end': end, 'code': k['code'], 'size': size, 'last': last, 'has_out': bool(p)})
    out.sort(key=lambda a: (a['state'] != 'run', -(a['start'] or 0)))
    return out


def api_shell_output(b):
    sid, tid = b.get('sid') or '', str(b.get('id') or '')
    fp = find_transcript(sid)
    k = fp and shell_scan(sid, fp)['tasks'].get(tid)
    if not k:
        return {'error': '找不到这个后台命令'}
    p = shell_path(sid, k)
    if not p:
        return {'text': '', 'size': 0, 'more': False, 'missing': True}
    txt, size, more = tail_text(p)
    return {'text': txt, 'size': size, 'more': more, 'path': p}


def conf_list_toggle(key, val, on):
    with G:
        c = load_conf()
        a = [x for x in c.get(key, []) if x != val]
        if on:
            a.insert(0, val)
        c[key] = a
        save_conf(c)
    BUS.emit({'type': 'sessions'})
    return {'ok': True}


def api_pin(b):
    return conf_list_toggle('pinned', b.get('sid') or '', b.get('on', True))


# ---------------------------------------------------------------- 11-11：项目改名（访达里的文件夹一起改）
# Claude Code 按文件夹路径存对话记录（~/.claude/projects/<路径里非字母数字都换成 -> /<会话>.jsonl），记录里每行带 cwd。
# 改名时：先确认没有程序在这个文件夹里运行（工作台自己空闲的 CLI 先结束；回复中的、终端或 VSCode 里的都不改），
# 再改文件夹名，把 cwd 在旧路径下的记录按文件逐个移到新路径对应的记录目录（中文名会编码成同一个目录，所以不能整个搬），
# 记录内容不改；工作台用 config 里的 moved 映射把旧 cwd 换成新的。
MOVED = {}


def load_moved():
    MOVED.clear()
    MOVED.update({k: v for k, v in (load_conf().get('moved') or {}).items() if isinstance(k, str) and isinstance(v, str)})


def remap_cwd(cwd):
    for _ in range(8):
        hit = next((o for o in MOVED if cwd == o or cwd.startswith(o + '/')), None) if cwd else None
        if not hit:
            break
        cwd = MOVED[hit] + cwd[len(hit):]
    return cwd


def cc_dir_of(path):
    return os.path.join(PROJECTS_DIR, re.sub(r'[^a-zA-Z0-9]', '-', path))


def first_cwd(fp):
    return head_meta(fp)[0]  # 11-17：原来只在开头 300KB 里找，第一条提问带图时找不到


def cwd_users(paths):
    """当前目录在这些文件夹里的进程 [(pid, 程序名)]；查不了时返回 None。"""
    try:
        out = subprocess.run(['lsof', '-n', '-d', 'cwd', '-F', 'pcn'], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    res, pid, cmd = [], None, ''
    for line in out.split('\n'):
        if line.startswith('p'):
            pid, cmd = int(line[1:] or 0), ''
        elif line.startswith('c'):
            cmd = line[1:]
        elif line.startswith('n') and pid and pid != os.getpid():
            n = line[1:]
            if any(n == p or n.startswith(p + '/') for p in paths):
                res.append((pid, cmd))
    return res


def under(p, roots):
    return any(p == r or p.startswith(r + '/') for r in roots)


def migrate_cc_sessions(olds, new):
    """olds：旧路径（含真实路径）。返回移动的记录数。"""
    moved = 0
    pres = {re.sub(r'[^a-zA-Z0-9]', '-', o) for o in olds}
    try:
        dirs = os.listdir(PROJECTS_DIR)
    except OSError:
        return 0
    for d in dirs:
        if not any(d.startswith(p) for p in pres):
            continue
        dp = os.path.join(PROJECTS_DIR, d)
        if not os.path.isdir(dp):
            continue
        mine, target = [], None
        for n in os.listdir(dp):
            if not n.endswith('.jsonl'):
                continue
            c = first_cwd(os.path.join(dp, n))
            o = next((o for o in olds if c and (c == o or c.startswith(o + '/'))), None)
            if o:
                mine.append((n, new + c[len(o):]))
        for n, nc in mine:
            td = cc_dir_of(nc)
            target = target or td
            if td == dp:
                continue
            os.makedirs(td, exist_ok=True)
            for item in (n, n[:-6]):  # 记录文件和同名目录（子代理、工具输出）
                src, dst = os.path.join(dp, item), os.path.join(td, item)
                if os.path.lexists(src) and not os.path.lexists(dst):
                    os.rename(src, dst)
            moved += 1
        # 这个记录目录只属于旧文件夹（没剩别的记录）：剩下的东西（自动记忆 memory 等）也搬过去
        if mine and target and target != dp and not any(n.endswith('.jsonl') for n in os.listdir(dp)):
            merge_move(dp, target)
            try:
                os.rmdir(dp)
            except OSError:
                pass
    return moved


def merge_move(src_dir, dst_dir):
    """把 src_dir 里的东西移到 dst_dir：目标没有的直接移；两边都是文件夹的往里合并；同名文件不覆盖、留在原处。"""
    for item in os.listdir(src_dir):
        src, dst = os.path.join(src_dir, item), os.path.join(dst_dir, item)
        if not os.path.lexists(dst):
            os.rename(src, dst)
        elif os.path.isdir(src) and not os.path.islink(src) and os.path.isdir(dst) and not os.path.islink(dst):
            merge_move(src, dst)
            try:
                os.rmdir(src)
            except OSError:
                pass


def trust_carried(cwd):
    rp = os.path.realpath(cwd or '')
    return any(rp == x or rp.startswith(x + '/') for x in load_conf().get('trust_carry') or [])


def api_rename_project(b):
    old = (b.get('cwd') or '').rstrip('/')
    name = ' '.join((b.get('name') or '').split())
    if not name or name in ('.', '..') or '/' in name or ':' in name or name.startswith('.') or len(name.encode()) > 200:
        raise ValueError('名称不合法：不能为空、不能以点开头、不能含 / 或 :')
    new = os.path.join(os.path.dirname(old), name) if old else ''
    # 11-11：已经在访达里改过名——原文件夹不在了、同一位置有这个新名字的文件夹：不再改名，只把对话记录和设置关联过去
    linked = bool(old) and not os.path.isdir(old) and os.path.isdir(new)
    if not old or (not os.path.isdir(old) and not linked):
        raise ValueError('原文件夹已经不在了。如果在访达里改过名，这里填访达里的新名字，就能把对话关联过去')
    if new == old:
        return {'ok': True, 'cwd': old}
    if old in ('/', HOME) or under(ROOT, [old]) or under(PROJECTS_DIR, [old]):
        raise ValueError('这个文件夹不能改名')
    if not linked and os.path.lexists(new) and os.path.realpath(new) != os.path.realpath(old):
        raise ValueError('同一位置已经有叫「%s」的文件或文件夹' % name)
    olds = list(dict.fromkeys([old, os.path.realpath(old)]))
    # 工作台自己在这个文件夹里的 CLI：回复中就不改；空闲的先结束（下次发消息会自动续接）
    rs = [r for r in list(RUNNERS.values()) if r.state.get('alive') and under(os.path.realpath(r.cwd), olds + [os.path.realpath(o) for o in olds])]
    if any(r.active() for r in rs):
        raise ValueError('这个项目里有对话正在回复或等你确认，等它结束再改名')
    for k, r in list(PREWARM.items()):
        if under(k, olds):
            PREWARM.pop(k, None)
            r.stop()
    for r in rs:
        r.stop()
    users = None
    for _ in range(30):
        users = cwd_users(olds + ([new, os.path.realpath(new)] if linked else []))  # 关联时：新文件夹里有 CLI 在跑，它还往旧位置写记录，也不能搬
        if users is not None and not users:
            break
        time.sleep(0.2)
    if users is None:
        raise ValueError('没法确认这个文件夹有没有程序在用，先不改名')
    if users:
        raise ValueError('有程序正在这个文件夹里运行（%s），先关掉再改名' % '、'.join(sorted({c or str(p) for p, c in users}))[:120])
    was_trusted = is_trusted(old)
    if not linked:
        os.rename(old, new)
    log('项目改名' if not linked else '项目关联到访达里改过名的文件夹', old, '→', new)
    try:
        n = migrate_cc_sessions(olds, new)
    except Exception as e:
        n = -1
        log('迁移对话记录出错', repr(e))
    rep = lambda p: (new + p[len(next(o for o in olds if p == o or p.startswith(o + '/'))):]) if isinstance(p, str) and under(p, olds) else p
    with G:
        c = load_conf()
        for k in ('folders', 'hidden_projects'):
            if isinstance(c.get(k), list):
                c[k] = list(dict.fromkeys(rep(x) for x in c[k]))
        if isinstance(c.get('project_opts'), dict):
            c['project_opts'] = {rep(k): v for k, v in c['project_opts'].items()}
        if isinstance(c.get('prefs'), dict) and c['prefs'].get('defaultCwd'):
            c['prefs']['defaultCwd'] = rep(c['prefs']['defaultCwd'])
        for t in c.get('schedules') or []:
            if isinstance(t, dict) and t.get('cwd'):
                t['cwd'] = rep(t['cwd'])
        mv = c.setdefault('moved', {})
        for k in list(mv):
            mv[k] = rep(mv[k])
        for o in olds:
            mv[o] = new
        if was_trusted:  # 改名前信任过：改名后第一次的信任弹窗自动确认
            c['trust_carry'] = list(dict.fromkeys([rep(x) for x in c.get('trust_carry') or []] + [os.path.realpath(new)]))
        save_conf(c)
    load_moved()
    for sid, cw in list(NEW_SESS.items()):
        NEW_SESS[sid] = rep(cw)
    INDEX.scan(force=True)
    BUS.emit({'type': 'sessions'})
    return {'ok': True, 'cwd': new, 'old': old, 'moved': n, 'linked': linked}


def api_hide_project(b):
    return conf_list_toggle('hidden_projects', b.get('cwd') or '', b.get('on', True))


def api_rename(b):
    sid, name = b.get('sid') or '', ' '.join((b.get('name') or '').split())[:80]
    with G:
        c = load_conf()
        names = c.setdefault('names', {})
        if name:
            names[sid] = name
        else:
            names.pop(sid, None)
        save_conf(c)
    BUS.emit({'type': 'sessions'})
    return {'ok': True, 'name': name}


# ---------------------------------------------------------------- 第四轮：审阅、Git、多任务、工作树、分叉、预热、定时、预览、扩展
def git(cwd, *args, timeout=20):
    try:
        p = subprocess.run(['git', '-C', cwd, *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout.decode('utf-8', 'replace'), p.stderr.decode('utf-8', 'replace')
    except Exception as e:
        return 1, '', str(e)


def is_trusted(cwd):
    try:
        with open(os.path.join(HOME, '.claude.json'), encoding='utf-8') as f:
            projects = json.load(f).get('projects') or {}
    except Exception:
        return False
    for base in {cwd, os.path.realpath(cwd)}:
        p = base
        while p and p != '/':
            if (projects.get(p) or {}).get('hasTrustDialogAccepted'):
                return True
            p = os.path.dirname(p)
    return False


def git_top(cwd):
    rc, out, _ = git(cwd, 'rev-parse', '--show-toplevel')
    return out.strip() if rc == 0 else None


KIND = {'M': '修改', 'A': '新增', 'D': '删除', 'R': '重命名', 'C': '复制', 'U': '冲突', '?': '未跟踪'}


def api_git_status(b):
    cwd = b.get('cwd') or ''
    top = git_top(cwd) if os.path.isdir(cwd) else None
    if not top:
        return {'git': False}
    branch = git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD')[1].strip()
    rc, ab, _ = git(cwd, 'rev-list', '--left-right', '--count', '@{upstream}...HEAD')
    behind = ahead = None
    if rc == 0 and len(ab.split()) == 2:
        behind, ahead = map(int, ab.split())
    _, st, _ = git(cwd, 'status', '--porcelain=v1', '-z', '--untracked-files=normal')
    ent, files, i = st.split('\0'), [], 0
    while i < len(ent):
        e = ent[i]
        i += 1
        if len(e) < 4:
            continue
        x, y, path = e[0], e[1], e[3:]
        if x in 'RC':
            i += 1
        code = x if x not in ' ?' else y
        files.append({'path': path, 'x': x, 'y': y, 'kind': KIND.get(code, '修改'), 'untracked': x == '?', 'dir': path.endswith('/')})
    _, ns, _ = git(cwd, 'diff', 'HEAD', '--numstat')
    stat = {}
    for line in ns.splitlines():
        p = line.split('\t')
        if len(p) == 3:
            stat[p[2]] = (p[0], p[1])
    for f in files:
        a, d = stat.get(f['path'], ('', ''))
        f['add'], f['del'] = a, d
    remote = git(cwd, 'remote', 'get-url', 'origin')[1].strip()
    return {'git': True, 'top': top, 'branch': branch, 'ahead': ahead, 'behind': behind, 'upstream': rc == 0,
            'files': files[:400], 'more': max(0, len(files) - 400), 'remote': remote}


def api_git_diff(b):
    cwd, path = b.get('cwd') or '', b.get('path') or ''
    top = git_top(cwd)
    if not top:
        raise ValueError('不是 git 仓库')
    full = os.path.realpath(os.path.join(top, path))
    if not full.startswith(os.path.realpath(top) + os.sep):
        raise ValueError('路径不在仓库内')
    rc, out, _ = git(top, 'ls-files', '--error-unmatch', '--', path)
    if rc != 0:  # 未跟踪的新文件：整份当作新增
        try:
            with open(full, 'rb') as f:
                raw = f.read(400_000)
        except OSError:
            return {'diff': '', 'binary': False, 'new': True}
        if b'\0' in raw[:8000]:
            return {'diff': '', 'binary': True, 'new': True}
        lines = raw.decode('utf-8', 'replace').split('\n')[:3000]
        return {'diff': f'@@ -0,0 +1,{len(lines)} @@\n' + '\n'.join('+' + l for l in lines), 'binary': False, 'new': True}
    _, out, _ = git(top, 'diff', 'HEAD', '--no-color', '--no-ext-diff', '-U3', '--', path)
    if 'Binary files' in out[:2000] and '@@' not in out:
        return {'diff': '', 'binary': True}
    return {'diff': out[:800_000], 'binary': False}


def api_git_revert(b):
    cwd, path = b.get('cwd') or '', b.get('path') or ''
    top = git_top(cwd)
    if not top:
        raise ValueError('不是 git 仓库')
    full = os.path.realpath(os.path.join(top, path))
    if not full.startswith(os.path.realpath(top) + os.sep):
        raise ValueError('路径不在仓库内')
    tracked_in_head = git(top, 'cat-file', '-e', 'HEAD:' + path.rstrip('/'))[0] == 0
    if tracked_in_head:
        rc, _, err = git(top, 'restore', '--source=HEAD', '--staged', '--worktree', '--', path)
        if rc != 0:
            raise ValueError(err.strip()[:300] or '撤销失败')
    else:
        git(top, 'rm', '-r', '--cached', '-q', '--ignore-unmatch', '--', path)
        if os.path.exists(full) and not trash([full]):
            raise ValueError('移到废纸篓失败')
    return {'ok': True}


def api_git_commit(b):
    cwd, msg, paths = b.get('cwd') or '', (b.get('message') or '').strip(), b.get('paths') or []
    top = git_top(cwd)
    if not top or not msg or not paths:
        raise ValueError('缺少提交说明或文件')
    rc, _, err = git(top, 'add', '-A', '--', *paths)
    if rc != 0:
        raise ValueError(err.strip()[:300])
    rc, out, err = git(top, 'commit', '-m', msg, '--', *paths, timeout=60)
    if rc != 0:
        raise ValueError((err or out).strip()[:400] or '提交失败')
    return {'ok': True, 'out': out.strip()[:400]}


def api_git_push(b):
    cwd = b.get('cwd') or ''
    top = git_top(cwd)
    if not top:
        raise ValueError('不是 git 仓库')
    has_up = git(top, 'rev-parse', '--abbrev-ref', '@{upstream}')[0] == 0
    rc, out, err = git(top, *(['push'] if has_up else ['push', '-u', 'origin', 'HEAD']), timeout=180)
    if rc != 0:
        raise ValueError((err or out).strip()[-400:] or '推送失败')
    return {'ok': True, 'out': (err + out).strip()[-400:]}


def api_git_pr(b):
    cwd = b.get('cwd') or ''
    gh = shutil.which('gh', path=LOGIN_ENV.get('PATH')) or shutil.which('gh')
    if not gh:
        raise ValueError('没有安装 gh 命令，无法发起合并请求')
    env = dict(LOGIN_ENV)
    if subprocess.run([gh, 'auth', 'status'], cwd=cwd, env=env, capture_output=True, timeout=20).returncode != 0:
        raise ValueError('gh 还没登录：请先在终端运行 gh auth login')
    p = subprocess.run([gh, 'pr', 'create', '--fill'], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise ValueError((p.stderr or p.stdout).strip()[-400:])
    url = (p.stdout.strip().splitlines() or [''])[-1]
    return {'ok': True, 'url': url}


def api_open_editor(b):
    p = resolve_path(b.get('path') or '', b.get('cwd'), b.get('sid'))
    if not p:
        return {'ok': False}
    for app in ('Visual Studio Code', 'Cursor'):
        if os.path.isdir(f'/Applications/{app}.app'):
            subprocess.Popen(['open', '-a', app, p])
            return {'ok': True}
    subprocess.Popen(['open', p])
    return {'ok': True}


def api_tasks(b):
    names = load_conf().get('names', {})
    out = []
    for r in list(RUNNERS.values()):
        if not r.state['alive'] or r.prewarm:
            continue
        info = INDEX.get(r.sid) if find_transcript(r.sid) else None
        out.append({'sid': r.sid, 'title': names.get(r.sid) or (info or {}).get('title') or r.title or '新对话',
                    'cwd': r.cwd, 'state': r.public_state(), 'started': r.started, 'unattended': r.unattended})
    out.sort(key=lambda x: (not x['state'].get('attention'), not x['state'].get('busy'), -x['started']))
    return {'tasks': out}


def api_fork(b):
    sid = b.get('sid') or ''
    info = INDEX.get(sid)
    if not info:
        raise ValueError('这个对话还没有记录，不能分叉')
    new = str(uuid.uuid4())
    CK.note_fork(new, sid)  # 修复线：记下分叉，分叉前的各轮不算改动、不能回退
    with G:
        c = load_conf()
        c.setdefault('names', {})[new] = (c.get('names', {}).get(sid) or info['title'])[:60] + '（分叉）'
        save_conf(c)
    ensure_runner(new, cwd=info['cwd'], opts=session_opts(sid), fork_from=sid)
    BUS.emit({'type': 'sessions'})
    return api_open({'sid': new})


def fork_transcript(src, at, new):
    """从中间分叉（兜底方案）。实测交互模式下 CLI 会忽略 --resume-session-at，新会话照样带着全部历史，
    所以自己按记录格式截：沿 parentUuid 链（压缩边界接 logicalParentUuid）取到 at 这一条为止，并行工具调用挂在旁支上的结果一并带上，
    sessionId 改成新 ID，写成新的记录文件，再用 --resume 新 ID 续接。源文件只读不写。"""
    sp = find_transcript(src)
    if not sp:
        raise ValueError('找不到原对话的记录')
    rows, found = [], False
    with open(sp, 'rb') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            rows.append(r)
            if r.get('uuid') == at:
                found = True
                break
    if not found:
        raise ValueError('找不到分叉的位置，这一段记录可能已经变了')
    by = {r['uuid']: r for r in rows if r.get('uuid')}
    chain, u = set(), at
    while u and u in by and u not in chain:
        chain.add(u)
        u = by[u].get('parentUuid') or by[u].get('logicalParentUuid')

    def is_result(r):
        c = (r.get('message') or {}).get('content')
        return r.get('type') == 'user' and isinstance(c, list) and any(isinstance(x, dict) and x.get('type') == 'tool_result' for x in c)
    keep, agents = [], set()
    for r in rows:
        u = r.get('uuid')
        if r.get('isSidechain'):
            ok = True  # 旧版把子代理过程写在主记录里，原样带上
        elif u:
            ok = u in chain or (r.get('parentUuid') in chain and is_result(r))
        else:
            t = r.get('type')
            ok = (t == 'file-history-snapshot' and r.get('messageId') in chain) or t in ('session-meta', 'mode', 'permission-mode')
        if not ok:
            continue
        if 'sessionId' in r:
            r['sessionId'] = new
        tur = r.get('toolUseResult')
        if isinstance(tur, dict) and tur.get('agentId'):
            agents.add(re.sub(r'[^\w-]', '', str(tur['agentId'])))
        keep.append(r)
    dp = os.path.join(os.path.dirname(sp), new + '.jsonl')
    if os.path.exists(dp):
        raise ValueError('新对话的记录已存在')
    tmp = dp + '.tmp'
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as f:
        for r in keep:
            f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + '\n')
    os.replace(tmp, dp)
    # 子代理过程：把这段里用到的复制过去，分叉后的对话里也能展开查看
    sd, dd = os.path.join(sp[:-6], 'subagents'), os.path.join(dp[:-6], 'subagents')
    for a in agents:
        for ext in ('.jsonl', '.meta.json'):
            fp = os.path.join(sd, f'agent-{a}{ext}')
            if a and os.path.isfile(fp):
                os.makedirs(dd, exist_ok=True)
                shutil.copy2(fp, dd)
    log('从中间分叉', src[:8], '→', new[:8], '保留', len(keep), '行，共读', len(rows), '行')
    return dp


def api_fork_at(b):
    """从中间某一轮分叉：新对话只含到这一轮为止的内容，原对话不受影响。"""
    sid, at = b.get('sid') or '', str(b.get('uuid') or '')
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', at):
        raise ValueError('分叉的位置不对')
    info = INDEX.get(sid)
    if not info:
        raise ValueError('这个对话还没有记录，不能分叉')
    new = str(uuid.uuid4())
    CK.note_fork(new, sid, at)  # 修复线：记下分叉，分叉点和之前的各轮不算改动、不能回退
    fork_transcript(sid, at, new)
    with G:
        c = load_conf()
        c.setdefault('names', {})[new] = (c.get('names', {}).get(sid) or info['title'])[:60] + '（分叉）'
        save_conf(c)
    INDEX.scan(force=True)
    ensure_runner(new, cwd=info['cwd'], opts=session_opts(sid))
    BUS.emit({'type': 'sessions'})
    return api_open({'sid': new})


PREWARM = {}


def project_opts(cwd):
    return dict(load_conf().get('project_opts', {}).get(cwd, {}))


def api_project_opts(b):
    cwd = b.get('cwd') or ''
    if 'opts' in b:
        with G:
            c = load_conf()
            o = merge_opts(b.get('opts') or {})  # 第十四轮 I：只留 model/effort/mode/fast，fast 只存 True
            if o:
                c.setdefault('project_opts', {})[cwd] = o
            else:
                c.get('project_opts', {}).pop(cwd, None)
            save_conf(c)
        pre = PREWARM.pop(cwd, None)
        if pre:
            pre.stop()
    return {'opts': opts_out(project_opts(cwd))}


def api_prewarm(b):
    """提前在后台启动一个新对话的 CLI，点"新对话"时直接用，省掉启动时间。"""
    cwd = (b.get('cwd') or '').rstrip('/')
    with G:
        pre = PREWARM.get(cwd)
        if pre and pre.state['alive']:
            return {'ok': True}
        if not os.path.isdir(cwd) or not is_trusted(cwd):
            return {'ok': False}
        if sum(1 for r in RUNNERS.values() if r.state['alive']) >= MAX_RUNNERS:
            return {'ok': False}
        if PTYD.upgrading or not PTYD.ensure():  # 第十三轮 E
            return {'ok': False}
        for k, r in list(PREWARM.items()):
            r.stop()
            PREWARM.pop(k, None)
        sid = str(uuid.uuid4())
        r = Runner(sid, cwd, True, merge_opts(pref_defaults(), project_opts(cwd)))  # 第十四轮 I：和新对话用同样的合并结果
        r.prewarm = True
        r.sync_meta()  # 第十三轮 E
        RUNNERS[sid] = r
        NEW_SESS[sid] = cwd
        PREWARM[cwd] = r
    return {'ok': True}


# ---------------------------------------------------------------- 定时任务
# 第十五轮 E：频率加每小时、每周（选星期几）、自定义间隔（N 小时）；每个任务记最近 20 次运行（.run/sched-history.json）；
# 错过后补跑（工作台启动或电脑唤醒后，只补最近错过的一次，7 天内，可按任务关闭）；运行期间保持唤醒（caffeinate，记下进程，只结束自己起的）。
# 旧数据照常工作：没有 freq 的按 days（daily / weekdays）；last_run 仍然写；排程字段只增不改名。
SCHED_FREQS = ('hourly', 'daily', 'weekdays', 'weekly', 'interval')
SCHED_KEYS = ('id', 'name', 'cwd', 'prompt', 'time', 'days', 'model', 'effort', 'mode', 'fast', 'enabled', 'last_run',
              'freq', 'minute', 'weekdays', 'every', 'anchor', 'catchup', 'awake', 'since', 'last_slot')
SCHED_SERVER_KEYS = ('last_run', 'since', 'last_slot', 'anchor')  # 只由后台写
SCHED_HIST = os.path.join(RUN_DIR, 'sched-history.json')
SCHED_HIST_MAX = 20
SCHED_LOOKBACK = 7 * 86400
SCHED_TICK = float(os.environ.get('CWB_SCHED_TICK') or 30)  # 测试可调短
SCHED_START_WAIT = 300  # 起了对话这么久还没开始回复，算失败
SCHED_MAX_RUN = 6 * 3600  # 跟踪上限
SCHED_LOCK = threading.Lock()
SCHED_ACTIVE = {}  # 任务 id -> {'rid', 'sid', 'caf'}：正在运行、正在跟踪的那一次


def sched_freq(t):
    f = t.get('freq')
    return f if f in SCHED_FREQS else ('weekdays' if t.get('days') == 'weekdays' else 'daily')


def sched_int(v, lo, hi, d=None):
    if isinstance(v, bool):
        return d
    try:
        v = int(v)
    except (TypeError, ValueError):
        return d
    return v if lo <= v <= hi else d


def sched_hm(t):
    m = re.match(r'^(\d{1,2}):(\d{2})$', str(t.get('time') or ''))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def sched_days(t):
    ds = t.get('weekdays')
    return sorted({x for x in ds if isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 6}) if isinstance(ds, list) else []


def sched_period(t):
    f = sched_freq(t)
    return 3600 if f == 'hourly' else (sched_int(t.get('every'), 1, 168, 24) * 3600 if f == 'interval' else 86400)


def sched_slots(t, now):
    """now 之前（含）最近的一个排定时刻和之后的下一个（本地时间）；没有就是 None。"""
    f = sched_freq(t)
    if f == 'hourly':
        base = now.replace(minute=sched_int(t.get('minute'), 0, 59, 0), second=0, microsecond=0)
        prev = base if base <= now else base - timedelta(hours=1)
        return prev, prev + timedelta(hours=1)
    hm = sched_hm(t)
    if not hm:
        return None, None
    if f == 'interval':
        try:
            a = datetime.strptime(str(t.get('anchor') or ''), '%Y-%m-%d').replace(hour=hm[0], minute=hm[1])
        except ValueError:
            return None, None
        step = timedelta(hours=sched_int(t.get('every'), 1, 168, 24))
        if now < a:
            return None, a
        prev = a + ((now - a) // step) * step
        return prev, prev + step
    days = set(range(7)) if f == 'daily' else set(range(5)) if f == 'weekdays' else set(sched_days(t))
    prev = nxt = None
    d0 = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    for i in range(-8, 9):
        c = d0 + timedelta(days=i)
        if c.weekday() not in days:
            continue
        if c <= now:
            prev = c
        elif nxt is None:
            nxt = c
    return prev, nxt


def sched_done_mark(t):
    """已处理到哪个时刻：last_slot；旧数据只有 last_run（到分钟），那一分钟及以前的时刻都算处理过。"""
    v = t.get('last_slot')
    m = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    try:
        m = max(m, datetime.strptime(str(t.get('last_run') or '')[:16], '%Y-%m-%d %H:%M').timestamp() + 59)
    except ValueError:
        pass
    return m


def sched_fill(t, ts):
    """旧数据没有 since：按一小时前起算（和旧版到点后 60 分钟内仍会运行一致），不翻出更早的错过。"""
    v = t.get('since')
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return False
    t['since'] = ts - 3600
    return True


def sched_due(t, now):
    """这一刻该不该跑：返回 (时刻, 'auto' | 'catchup' | 'missed')，不该跑返回 None。"""
    prev, _ = sched_slots(t, now)
    if not prev:
        return None
    st = prev.timestamp()
    if st < float(t.get('since') or 0) or st <= sched_done_mark(t):
        return None
    late = now.timestamp() - st
    if late <= min(3600, sched_period(t) / 2):
        return st, 'auto'
    if late <= SCHED_LOOKBACK and t.get('catchup') is not False:
        return st, 'catchup'
    return st, 'missed'


def sched_hist_load():
    try:
        with open(SCHED_HIST, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def sched_hist_edit(fn):
    with SCHED_LOCK:
        h = sched_hist_load()
        fn(h)
        try:
            tmp = SCHED_HIST + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(h, f, ensure_ascii=False)
            os.replace(tmp, SCHED_HIST)
        except Exception as e:
            log('定时任务历史写不进去', repr(e))


def sched_hist_add(tid, ent):
    def f(h):
        lst = h.get(tid) if isinstance(h.get(tid), list) else []
        h[tid] = ([ent] + lst)[:SCHED_HIST_MAX]
    sched_hist_edit(f)


def sched_hist_set(tid, rid, **kw):
    def f(h):
        for e in h.get(tid) or []:
            if isinstance(e, dict) and e.get('id') == rid:
                e.update(kw)
                break
    sched_hist_edit(f)


def sched_awake_start():
    """运行期间保持唤醒：caffeinate 跟着本服务进程（-w），服务意外退出它也会自己结束。"""
    try:
        return subprocess.Popen(['/usr/bin/caffeinate', '-i', '-s', '-w', str(os.getpid())], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        log('保持唤醒没有启动', repr(e))
        return None


def sched_awake_stop(p):
    """只结束自己起的那个：手里拿着 Popen 对象、还没回收，PID 不会被别的进程复用。"""
    if not p:
        return
    try:
        if p.poll() is None:
            p.terminate()
            p.wait(3)
    except Exception:
        try:
            p.kill()
            p.wait(2)
        except Exception:
            pass


def sched_turn_error(sid):
    try:
        tr = get_transcript(sid)
        tr.poll()
        with tr.lock:
            return turn_has_error(tr.conv)
    except Exception:
        return False


def sched_wait(sid, t0):
    """等这次运行的一轮回复结束：Stop / StopFailure 钩子会记 done_at（mark_done）。"""
    started, gone = False, 0.0
    while True:
        time.sleep(1)
        now = time.time()
        r = RUNNERS.get(sid)
        if r and (r.turn or (r.since and r.since >= t0 - 1)):
            started = True
        if MARKS['done_at'].get(sid, 0) >= t0 - 1 and not (r and r.turn):
            time.sleep(1.5)  # 出错行可能比钩子晚一点写进记录
            return ('error', '接口出错') if sched_turn_error(sid) else ('done', '')
        if not r or not r.state.get('alive'):
            gone += 1
            if gone >= 20:
                return 'error', '对话进程已退出'
        else:
            gone = 0
        if not started and now - t0 > SCHED_START_WAIT:
            return 'error', '没有开始运行'
        if now - t0 > SCHED_MAX_RUN:
            return 'error', '运行超过 6 小时，不再跟踪'


def sched_watch(tid, rid, sid, t0, awake):
    caf = sched_awake_start() if awake else None
    with SCHED_LOCK:
        a = SCHED_ACTIVE.get(tid)
        if a and a.get('rid') == rid:
            a['caf'] = caf.pid if caf else 0
    res, note = 'error', ''
    try:
        res, note = sched_wait(sid, t0)
    except Exception as e:
        res, note = 'error', '跟踪运行状态出错'
        log('定时任务跟踪出错', repr(e))
    finally:
        sched_awake_stop(caf)
        with SCHED_LOCK:
            if (SCHED_ACTIVE.get(tid) or {}).get('rid') == rid:
                SCHED_ACTIVE.pop(tid, None)
    sched_hist_set(tid, rid, result=res, note=note, end=time.time())
    BUS.emit({'type': 'sched', 'id': tid})
    log('定时任务结束', tid, sid[:8], res, note)


def sched_claim(tid, rid):
    with SCHED_LOCK:
        if tid in SCHED_ACTIVE:
            return False
        SCHED_ACTIVE[tid] = {'rid': rid, 'sid': '', 'caf': 0}
        return True


def start_scheduled(t, kind='auto', slot=None, rid=None):
    """在后台线程里运行：新建一个无人值守的对话发出提示，再一直跟踪到这一轮结束。调用前已 sched_claim。"""
    tid = t.get('id') or ''
    rid = rid or uuid.uuid4().hex[:10]
    ent = {'id': rid, 't': time.time(), 'slot': slot, 'kind': kind, 'result': 'running', 'sid': ''}
    added = False
    try:
        cwd = t['cwd']
        if not os.path.isdir(cwd):
            raise ValueError('项目文件夹不在了')
        if not is_trusted(cwd):
            ent['result'], ent['note'] = 'untrusted', '项目还没信任'
            BUS.emit({'type': 'notify', 'title': 'Claude 工作台', 'text': '项目还没信任，定时任务未执行'})
            return
        sid = str(uuid.uuid4())
        ent['sid'] = sid
        with G:
            c = load_conf()
            c.setdefault('names', {})[sid] = '定时 · ' + (t.get('name') or t['prompt'][:20])
            save_conf(c)
        with SCHED_LOCK:
            if (SCHED_ACTIVE.get(tid) or {}).get('rid') == rid:
                SCHED_ACTIVE[tid]['sid'] = sid
        sched_hist_add(tid, ent)
        added = True
        opts = merge_opts({k: t.get(k) for k in OPT_KEYS})  # 第十四轮 I：带上 fast
        r = ensure_runner(sid, cwd=cwd, new=True, opts=opts)
        r.unattended = True
        r.send(t['prompt'])
        BUS.emit({'type': 'sessions'})
        BUS.emit({'type': 'sched', 'id': tid})
        log('定时任务已启动', t.get('name') or t['prompt'][:20], sid, kind)
    except Exception as e:
        ent['result'], ent['note'] = 'failed', (str(e) if isinstance(e, ValueError) else '启动出错')[:200]
        log('定时任务启动失败', tid, repr(e))
        return
    finally:
        if ent['result'] != 'running':
            with SCHED_LOCK:
                if (SCHED_ACTIVE.get(tid) or {}).get('rid') == rid:
                    SCHED_ACTIVE.pop(tid, None)
            if added:
                sched_hist_set(tid, rid, result=ent['result'], note=ent.get('note', ''), end=time.time())
            else:
                sched_hist_add(tid, ent)
            BUS.emit({'type': 'sched', 'id': tid})
    sched_watch(tid, rid, sid, ent['t'], t.get('awake') is True)


def run_due(force_id=None):
    if force_id:
        return sched_run_now(force_id)
    now = datetime.now()
    ts = now.timestamp()
    due, notes = [], []
    with G:
        c = load_conf()
        changed = False
        for t in c.get('schedules') or []:
            if not isinstance(t, dict) or not t.get('id') or t.get('enabled') is False:
                continue
            changed = sched_fill(t, ts) or changed
            d = sched_due(t, now)
            if not d:
                continue
            st, kind = d
            t['last_slot'] = st
            changed = True
            if kind == 'missed':
                notes.append((t['id'], {'id': uuid.uuid4().hex[:10], 't': ts, 'slot': st, 'kind': 'auto', 'result': 'missed', 'sid': ''}))
                continue
            rid = uuid.uuid4().hex[:10]
            if not sched_claim(t['id'], rid):
                notes.append((t['id'], {'id': rid, 't': ts, 'slot': st, 'kind': kind, 'result': 'busy', 'sid': ''}))
                continue
            t['last_run'] = now.strftime('%Y-%m-%d %H:%M')
            due.append((dict(t), kind, st, rid))
        if changed:
            save_conf(c)
    for tid, ent in notes:
        sched_hist_add(tid, ent)
    for t, kind, st, rid in due:
        threading.Thread(target=start_scheduled, args=(t, kind, st, rid), daemon=True).start()
    if notes:
        BUS.emit({'type': 'sched'})


def sched_run_now(tid):
    with G:
        c = load_conf()
        t = next((x for x in c.get('schedules') or [] if isinstance(x, dict) and x.get('id') == tid), None)
        if not t:
            raise ValueError('找不到这个定时任务')
        rid = uuid.uuid4().hex[:10]
        if not sched_claim(tid, rid):
            raise ValueError('这个任务正在运行')
        t['last_run'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        save_conf(c)
        t = dict(t)
    threading.Thread(target=start_scheduled, args=(t, 'manual', None, rid), daemon=True).start()


def sched_clean(b, old):
    """保存前整理：未知字段丢掉，后台字段不让前端改；排程改了、新建或从暂停恢复，从现在起算（之前的时刻不算错过）。"""
    t = dict(old or {})
    for k in SCHED_KEYS:
        if k in b and k not in SCHED_SERVER_KEYS:
            t[k] = b[k]
    t['cwd'] = str(t.get('cwd') or '')
    t['prompt'] = str(t.get('prompt') or '').strip()
    if not t['cwd'] or not t['prompt']:
        raise ValueError('请填写项目和提示词')
    f = t.get('freq') if t.get('freq') in SCHED_FREQS else ('weekdays' if t.get('days') == 'weekdays' else 'daily')
    t['freq'] = f
    if f == 'hourly':
        m = sched_int(t.get('minute'), 0, 59)
        if m is None:
            raise ValueError('请选择每小时的第几分钟')
        t['minute'] = m
        if not sched_hm(t):
            t['time'] = '09:00'  # 旧版只认 time，留一个合法值
    elif not sched_hm(t):
        raise ValueError('请填写时间')
    if f == 'weekly':
        t['weekdays'] = sched_days(t)
        if not t['weekdays']:
            raise ValueError('请选择星期几')
    if f == 'interval':
        e = sched_int(t.get('every'), 1, 168)
        if e is None:
            raise ValueError('间隔要在 1 到 168 小时之间')
        t['every'] = e
    t['days'] = 'weekdays' if f == 'weekdays' else 'daily'  # 旧版只认 days
    t['fast'] = bool(t.get('fast'))  # 第十四轮 I
    t['enabled'] = t.get('enabled') is not False
    t['catchup'] = t.get('catchup') is not False
    t['awake'] = t.get('awake') is True
    t['id'] = str(t.get('id') or uuid.uuid4().hex[:8])
    sig = lambda x: (sched_freq(x), sched_hm(x) if sched_freq(x) != 'hourly' else None, sched_int(x.get('minute'), 0, 59) if sched_freq(x) == 'hourly' else None,
                     tuple(sched_days(x)) if sched_freq(x) == 'weekly' else (), sched_int(x.get('every'), 1, 168) if sched_freq(x) == 'interval' else None)
    if not old or sig(old) != sig(t) or (old.get('enabled') is False and t['enabled']):
        t['since'] = time.time()
        if f == 'interval':
            t['anchor'] = datetime.now().strftime('%Y-%m-%d')
    if f == 'interval' and not t.get('anchor'):
        t['anchor'] = datetime.fromtimestamp(float(t.get('since') or time.time())).strftime('%Y-%m-%d')
    return {k: t[k] for k in SCHED_KEYS if k in t}


def sched_public():
    c, h, now = load_conf(), sched_hist_load(), datetime.now()
    out = []
    for t in c.get('schedules') or []:
        if not isinstance(t, dict) or not t.get('id'):
            continue
        x = dict(t)
        x['freq'] = sched_freq(t)
        x['catchup'] = t.get('catchup') is not False
        x['awake'] = t.get('awake') is True
        nxt = None
        if t.get('enabled') is not False:
            try:
                nxt = sched_slots(t, now)[1]
            except Exception:
                nxt = None
        x['next_at'] = nxt.timestamp() if nxt else None
        x['running'] = t['id'] in SCHED_ACTIVE
        runs = h.get(t['id'])
        x['runs'] = [e for e in runs if isinstance(e, dict)][:SCHED_HIST_MAX] if isinstance(runs, list) else []
        out.append(x)
    return out


def api_schedules(b):
    act = b.get('action') or 'list'
    if act == 'run':
        sched_run_now(b.get('id'))
    elif act == 'history':
        runs = sched_hist_load().get(b.get('id') or '')
        return {'runs': runs if isinstance(runs, list) else []}
    elif act in ('save', 'delete'):
        with G:
            c = load_conf()
            ss = c.setdefault('schedules', [])
            if act == 'save':
                tin = b.get('task') or {}
                old = next((x for x in ss if isinstance(x, dict) and tin.get('id') and x.get('id') == tin.get('id')), None)
                t = sched_clean(tin, old)
                if old:
                    ss[ss.index(old)] = t  # 原地替换：开关、编辑不改变顺序
                else:
                    ss.append(t)
            else:
                ss[:] = [x for x in ss if not (isinstance(x, dict) and x.get('id') == b.get('id'))]
            if not ss:
                c.pop('schedules', None)
            save_conf(c)
        if act == 'delete' and b.get('id'):
            sched_hist_edit(lambda h: h.pop(b.get('id'), None))
    return {'schedules': sched_public()}


def sched_resume():
    """服务重启后：记成“运行中”的那次，对话还在就接着跟踪，不在了就记成“结果未知”。"""
    conf = {t.get('id'): t for t in load_conf().get('schedules') or [] if isinstance(t, dict)}
    for tid, runs in sched_hist_load().items():
        for e in runs if isinstance(runs, list) else []:
            if not isinstance(e, dict) or e.get('result') != 'running':
                continue
            sid, rid = e.get('sid') or '', e.get('id') or ''
            if sid and RUNNERS.get(sid) and tid in conf and sched_claim(tid, rid):
                t0 = float(e.get('t') or time.time())
                threading.Thread(target=sched_watch, args=(tid, rid, sid, t0, conf[tid].get('awake') is True), daemon=True).start()
            else:
                sched_hist_set(tid, rid, result='unknown', note='工作台重启过，没跟踪到结果', end=time.time())


def sched_loop():
    first = True
    while True:
        time.sleep(SCHED_TICK)
        try:
            if first:
                first = False
                sched_resume()
            run_due()
        except Exception as e:
            log('定时任务异常', repr(e))
        try:
            for cwd, r in list(PREWARM.items()):  # 预热的进程 20 分钟没用就关掉
                if time.time() - r.started > 1200:
                    r.stop()
                    PREWARM.pop(cwd, None)
        except Exception as e:
            log('预热清理异常', repr(e))


TEXT_LANG = {'.py': 'python', '.js': 'javascript', '.mjs': 'javascript', '.ts': 'typescript', '.tsx': 'typescript',
             '.jsx': 'javascript', '.json': 'json', '.md': 'markdown', '.sh': 'bash', '.zsh': 'bash', '.css': 'css',
             '.html': 'xml', '.htm': 'xml', '.xml': 'xml', '.svg': 'xml', '.yaml': 'yaml', '.yml': 'yaml', '.toml': 'ini',
             '.ini': 'ini', '.sql': 'sql', '.go': 'go', '.rs': 'rust', '.java': 'java', '.kt': 'kotlin', '.swift': 'swift',
             '.c': 'c', '.h': 'c', '.cpp': 'cpp', '.cs': 'csharp', '.rb': 'ruby', '.php': 'php', '.vue': 'xml', '.txt': 'plaintext'}
OFFICE_EXT = {'.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt', '.key', '.pages', '.numbers', '.zip', '.dmg', '.app', '.mp4', '.mov', '.mp3'}


def api_preview(b):
    p = resolve_path(b.get('path') or '', b.get('cwd'), b.get('sid'))
    if not p:
        raise ValueError('找不到：' + (b.get('path') or ''))
    st = os.stat(p)
    base = {'path': p, 'name': os.path.basename(p) or p, 'size': st.st_size, 'mtime': st.st_mtime}
    if os.path.isdir(p):
        try:
            names = sorted(os.listdir(p), key=lambda n: (not os.path.isdir(os.path.join(p, n)), n.lower()))
        except OSError:
            names = []
        ents = [{'name': n, 'dir': os.path.isdir(os.path.join(p, n))} for n in names if not n.startswith('.')][:400]
        return dict(base, kind='dir', entries=ents)
    ext = os.path.splitext(p)[1].lower()
    if ext in IMG_EXT:
        return dict(base, kind='image')
    if ext == '.pdf':
        return dict(base, kind='pdf')
    if ext in OFFICE_EXT or st.st_size > 20 * 1024 * 1024:
        return dict(base, kind='other')
    with open(p, 'rb') as f:
        raw = f.read(512_000)
    if b'\0' in raw[:8000]:
        return dict(base, kind='other')
    kind = 'md' if ext in ('.md', '.markdown') else 'csv' if ext in ('.csv', '.tsv') else 'text'
    return dict(base, kind=kind, text=raw.decode('utf-8', 'replace'), truncated=st.st_size > 512_000,
                lang=TEXT_LANG.get(ext, ''), sep='\t' if ext == '.tsv' else ',')


def api_extensions(b):
    cwd = b.get('cwd') or ''
    skills = api_commands({'cwd': cwd})['commands']
    mcp = []

    def add(src, servers):
        for name, cfg in (servers or {}).items():
            if not isinstance(cfg, dict):
                continue
            typ = cfg.get('type') or ('http' if cfg.get('url') else 'stdio')
            target = cfg.get('url') or os.path.basename(str(cfg.get('command') or ''))
            mcp.append({'name': name, 'scope': src, 'type': typ, 'target': re.sub(r'[?#].*$', '', str(target))[:120]})
    try:
        with open(os.path.join(HOME, '.claude.json'), encoding='utf-8') as f:
            cj = json.load(f)
        add('个人', cj.get('mcpServers'))
        add('本项目', ((cj.get('projects') or {}).get(cwd) or {}).get('mcpServers'))
    except Exception:
        pass
    try:
        with open(os.path.join(cwd, '.mcp.json'), encoding='utf-8') as f:
            add('项目文件', json.load(f).get('mcpServers'))
    except Exception:
        pass
    return {'skills': skills, 'mcp': mcp}


def api_notify(b):
    # 系统通知由界面窗口自己发（浏览器通知），点击会回到工作台；不用 osascript，否则通知归到"脚本编辑器"，点开会打开它
    BUS.emit({'type': 'notify', 'sid': b.get('sid') or '', 'title': str(b.get('title') or 'Claude 工作台')[:60], 'text': str(b.get('text') or '')[:120]})
    return {}


# ---------------------------------------------------------------- 第十四轮 I：用量统计、关于、请求头自检、重启服务
def usage_titles(sids):
    names = load_conf().get('names', {})
    idx = {s['sid']: s['title'] for s in INDEX.scan()}
    return {sid: names.get(sid) or idx.get(sid) or '' for sid in sids}


USAGE = US.Usage(PROJECTS_DIR, cache_path=os.path.join(RUN_DIR, 'usage-cache.json'), runs_path=RUNS_PATH,
                 titles=usage_titles, label=model_label, live_keys=lambda: list(KEYS), log=log)


def claude_scope(b):
    """11-26：Claude 用量口径。'all' 终端、VS Code 里跑的也算（界面默认传这个）；不传或 'wb' 只算工作台（原口径，老调用方不变）。"""
    return 'all' if (b or {}).get('claudeScope') == 'all' else 'wb'


def api_usage(b):
    return USAGE.query(str(b.get('range') or 'today'), scope=claude_scope(b))


def api_usage_summary(b):
    return USAGE.summary(scope=claude_scope(b))


APP_VERSION = '1.0'
UA_RE = re.compile(r'^claude-cli/\S+ \(external, cli\)$')
UA_PATH = os.path.join(RUN_DIR, 'ua-check.json')
UA_DIR = '/tmp/cwb-uacheck'
UA_LOCK = threading.Lock()
CLI_VER = {}


def cli_version():
    real = os.path.realpath(CLAUDE)
    if real in CLI_VER:
        return CLI_VER[real]
    m = re.fullmatch(r'\d+\.\d+\.\d+', os.path.basename(real))
    v = m.group(0) if m else ''
    if not v:
        try:
            out = subprocess.run([CLAUDE, '--version'], capture_output=True, text=True, timeout=15, env=LOGIN_ENV or None).stdout
            mm = re.search(r'\d+\.\d+\.\d+', out)
            v = mm.group(0) if mm else ''
        except Exception:
            v = ''
    CLI_VER[real] = v
    return v


def mask_token(t):
    """令牌只显示前 4 位。"""
    return (t[:4] + '•' * 10) if len(t or '') >= 12 else ('•' * 10 if t else '')


def gateway_info():
    """CLI 实际用的网关：--settings 里的（测试）优先，其次 settings.json 的 env，再次登录环境。"""
    st = user_settings()
    env = dict(LOGIN_ENV)
    env.update(st.get('env') or {})
    env.update((EXTRA_SETTINGS.get('env') or {}) if isinstance(EXTRA_SETTINGS.get('env'), dict) else {})
    url = env.get('ANTHROPIC_BASE_URL') or 'https://api.anthropic.com'
    return {'host': urlparse(url).netloc or url, 'tokenMasked': mask_token(env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY') or '')}


def load_ua_check():
    try:
        with open(UA_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def api_about(b):
    backups = []
    for p in glob.glob(os.path.join(RUN_DIR, 'bak-*')):
        try:
            backups.append({'name': os.path.basename(p), 'path': p, 'mtime': os.path.getmtime(p)})
        except OSError:
            pass
    backups.sort(key=lambda x: -x['mtime'])
    return {'app': {'version': APP_VERSION, 'round': 15}, 'cli': {'version': cli_version(), 'path': CLAUDE},
            'gateway': gateway_info(), 'port': PORT,
            'logs': {'server': os.path.join(RUN_DIR, 'server.log'), 'ptyd': PT.paths(RUN_DIR, PORT)['log'],
                     'restart': os.path.join(RUN_DIR, 'restart_when_idle.log')},
            'backups': backups, 'uaCheck': load_ua_check()}


def ua_cleanup(sid):
    """只删这次自检的会话 ID 产生的记录。"""
    if not re.fullmatch(r'[0-9a-f-]{36}', sid):
        return
    paths = glob.glob(os.path.join(PROJECTS_DIR, '*', sid + '.jsonl')) + glob.glob(os.path.join(PROJECTS_DIR, '*', sid))
    paths += [os.path.join(HOME, '.claude', d, sid) for d in ('session-env', 'file-history', 'tasks')]
    for p in paths:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.remove(p)
        except OSError as e:
            log('清理自检会话失败', p, repr(e))


def ua_check_run(limit=50.0, extra=None):
    """用本机临时抓包网关跑一次真实 CLI（伪终端、交互模式，环境沿用 LOGIN_ENV，只用 --settings 的 env 改网关地址）：
    处理信任弹窗，输入框出现后发一句话，抓到第一条 /v1/messages 的请求头就结束 CLI，再删掉这次的会话记录。
    等待最多 50 秒，加上结束进程和清理，整个过程在 60 秒内。"""
    import fcntl
    import select
    import termios
    t0 = time.time()
    res = {'ok': False, 'ua': '', 'xapp': '', 'ms': 0, 'at': int(t0 * 1000)}
    gw = US.CaptureGateway()
    sid, p, master = str(uuid.uuid4()), None, None
    try:
        os.makedirs(UA_DIR, exist_ok=True)
        argv = [CLAUDE, '--session-id', sid, '--settings', json.dumps({'env': {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{gw.port}'}})]
        if extra:  # 第十七轮 M：“协作开”的用例：同一种启动，--settings 多叫醒钩子，另加 --mcp-config（collab.ua_check）
            argv = [CLAUDE, '--session-id', sid, '--settings', json.dumps(dict(extra.get('settings') or {}, env={'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{gw.port}'}))] + list(extra.get('args') or [])
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
        # 先 setsid 并把伪终端设成控制终端，再 exec 成 claude（PID 不变）
        boot = 'import fcntl,os,sys,termios\nos.setsid()\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\nos.execvp(sys.argv[1], sys.argv[1:])'
        p = subprocess.Popen([PY, '-I', '-c', boot] + argv, stdin=slave, stdout=slave, stderr=slave, cwd=UA_DIR,
                             env=dict(LOGIN_ENV or os.environ), close_fds=True)
        os.close(slave)
        screen = pyte.Screen(COLS, ROWS)
        stream = pyte.ByteStream(screen)
        typed_at = acted = 0.0
        while time.time() - t0 < limit and not gw.got.is_set():
            rd, _, _ = select.select([master], [], [], 0.2)
            if rd:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b''
                if not data:
                    res['error'] = 'CLI 提前退出了'
                    break
                stream.feed(data)
            if time.time() - acted < 1.5:
                continue
            lines = [norm_line(l) for l in safe_display(screen, panel_col(screen))]
            dl = parse_dialog(lines)
            if dl:
                if dl['kind'] == 'trust':
                    idx = next((i for i, o in enumerate(dl['options']) if 'yes' in o['label'].lower()), 0)
                    keys = (b'\x1b[B' if idx > dl['cur'] else b'\x1b[A') * abs(idx - dl['cur']) + b'\r'
                    os.write(master, keys)
                    acted = time.time()
                    continue
                res['error'] = '出现了没认出的弹窗：' + (dl.get('text') or '')[-80:]
                break
            rules = [i for i, l in enumerate(lines) if RULE_RE.match(l.strip())]
            ready = any(lines[a + 1].lstrip().startswith('❯') for a, b2 in zip(rules, rules[1:]) if 1 <= b2 - a - 1 <= 30)
            if ready and (not typed_at or time.time() - typed_at > 10):
                os.write(master, b'ok')
                time.sleep(0.3)
                os.write(master, b'\r')
                typed_at = acted = time.time()
        if gw.first:
            res.update(ua=gw.first['ua'], xapp=gw.first['xapp'])
            res['ok'] = bool(UA_RE.match(res['ua']))
            res.pop('error', None)
            if not res['ok']:
                res['error'] = '请求头不是 claude-cli/<版本> (external, cli)'
        elif not res.get('error'):
            res['error'] = '规定时间内没有抓到请求'
    except Exception as e:
        res['error'] = '自检失败：' + str(e)
    finally:
        if p and p.poll() is None:
            for sig, wait in ((signal.SIGHUP, 3), (signal.SIGKILL, 3)):
                try:
                    os.killpg(p.pid, sig)  # p.pid 是 setsid 后的进程组长，即这次启动的 CLI
                except OSError:
                    pass
                try:
                    p.wait(wait)
                    break
                except subprocess.TimeoutExpired:
                    pass
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        gw.close()
        time.sleep(0.3)
        ua_cleanup(sid)
    res['ms'] = int((time.time() - t0) * 1000)
    return res


# ---------------------------------------------------------------- 11-11：自动给对话起标题
# 网关接入脚本 11-06 在全局设置里加了 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1，CLI 从此不再自动起标题（ai-title）。
# 工作台自己补：一轮结束后，用一个隐藏的交互式 CLI（Haiku、--tools "" 不带任何工具、不连 MCP、不加载技能）
# 按第一问和第一段回复起一个 12 字以内的中文标题。请求头与平时一样是 claude-cli/<版本> (external, cli)。
# 标题只存在 .run/titles.json，不改对话记录；优先级：工作台里改的名字 > CLI 的标题 > 自动标题 > 第一句话。
TITLE_DIR = '/tmp/cwb-titler'
TITLE_PATH = os.path.join(RUN_DIR, 'titles.json')
TITLE_MODEL = 'claude-haiku-5-5'
TITLE_DAYS = 14          # 启动一分钟后，给最近 14 天、还没有标题的对话补标题（最多 40 个）
TITLE_RETRY = 86400      # 没起成功的，隔一天再试
TITLE_LOCK = threading.Lock()
TITLE_Q, TITLE_QSET = [], set()
TITLE_EV = threading.Event()
TITLE_STRIP = '「」『』“”"\'《》<>【】[]（）()。，、；：！？!?,.;:…—·*#` '
TITLE_PROMPT = ('给下面这段对话起一个中文标题，不超过 12 个字，概括用户要做的事。'
                '只输出标题本身，不要引号、标点或任何解释。\n\n用户：{q}\n\n助手：{a}')


def _load_titles():
    try:
        with open(TITLE_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


AUTO_TITLES = _load_titles()  # {sid: {'t': 标题（没起成功为空）, 'at': 时间}}


def auto_title(sid):
    v = AUTO_TITLES.get(sid)
    return (v.get('t') or None) if isinstance(v, dict) else None


def save_titles():
    tmp = TITLE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(AUTO_TITLES, f, ensure_ascii=False)
    os.replace(tmp, TITLE_PATH)


def title_maybe(sid):
    """一轮结束后调用：工作台里没改过名、也没有自动标题（或上次失败已过一天）就排队；CLI 自己有标题的在执行时再跳过。"""
    if not re.fullmatch(r'[0-9a-f-]{36}', sid or '') or load_conf().get('names', {}).get(sid):
        return
    v = AUTO_TITLES.get(sid)
    if isinstance(v, dict) and (v.get('t') or time.time() - (v.get('at') or 0) < TITLE_RETRY):
        return
    with TITLE_LOCK:
        if sid in TITLE_QSET:
            return
        TITLE_QSET.add(sid)
        TITLE_Q.append(sid)
    TITLE_EV.set()


def title_material(path):
    """第一问（去掉提醒、命令块）和第一段文字回复，各截一段。"""
    q = a = ''
    try:
        with open(path, 'rb') as f:
            head = f.read(600_000)
    except OSError:
        return '', ''
    for d in iter_rows(head):
        if not q:
            if first_prompt(d):
                q = clean_text(text_of((d.get('message') or {}).get('content')))
            continue
        if d.get('type') == 'assistant' and not d.get('isSidechain'):
            t = clean_text(text_of((d.get('message') or {}).get('content'))).strip()
            if t:
                a = t
                break
    return ' '.join(q.split())[:500], ' '.join(a.split())[:300]


def clean_title(s):
    s = (s or '').strip().split('\n')[0].strip()
    s = re.sub(r'^(标题|题目)\s*[:：]\s*', '', s).strip(TITLE_STRIP)
    s = ' '.join(s.split())
    if not s or len(s) > 30 or re.search(r'抱歉|无法|不能|sorry|cannot', s, re.I):
        return ''
    return s[:20]


def title_reply(sid):
    for fp in glob.glob(os.path.join(PROJECTS_DIR, '*', sid + '.jsonl')):
        try:
            with open(fp, 'rb') as f:
                buf = f.read()
        except OSError:
            continue
        for d in iter_rows(buf):
            if d.get('type') == 'assistant':
                t = text_of((d.get('message') or {}).get('content')).strip()
                if t:
                    return t
    return ''


def title_cli_run(prompt, limit=75.0):
    """和请求头自检同样的方式起一个隐藏的交互式 CLI，粘贴提示、回车，等记录里出现回复，然后结束它并删掉这次的记录。"""
    import fcntl
    import select
    import termios
    t0 = time.time()
    sid, p, master, out = str(uuid.uuid4()), None, None, ''
    try:
        os.makedirs(TITLE_DIR, exist_ok=True)
        argv = [CLAUDE, '--session-id', sid, '--model', TITLE_MODEL, '--effort', 'low', '--tools', '',
                '--strict-mcp-config', '--disable-slash-commands']
        if EXTRA_SETTINGS:  # 仅测试：指向假网关
            argv += ['--settings', json.dumps(EXTRA_SETTINGS, ensure_ascii=False)]
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
        boot = 'import fcntl,os,sys,termios\nos.setsid()\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\nos.execvp(sys.argv[1], sys.argv[1:])'
        p = subprocess.Popen([PY, '-I', '-c', boot] + argv, stdin=slave, stdout=slave, stderr=slave, cwd=TITLE_DIR,
                             env=dict(LOGIN_ENV or os.environ), close_fds=True)
        os.close(slave)
        screen = pyte.Screen(COLS, ROWS)
        stream = pyte.ByteStream(screen)
        sent_at = acted = checked = 0.0
        while time.time() - t0 < limit:
            rd, _, _ = select.select([master], [], [], 0.2)
            if rd:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b''
                if not data:
                    break
                stream.feed(data)
            if sent_at:
                if time.time() - checked > 0.5:
                    checked = time.time()
                    out = title_reply(sid)
                    if out:
                        break
                continue
            if time.time() - acted < 1.5:
                continue
            lines = [norm_line(l) for l in safe_display(screen, panel_col(screen))]
            dl = parse_dialog(lines)
            if dl:
                if dl['kind'] == 'trust':
                    idx = next((i for i, o in enumerate(dl['options']) if 'yes' in o['label'].lower()), 0)
                    os.write(master, (b'\x1b[B' if idx > dl['cur'] else b'\x1b[A') * abs(idx - dl['cur']) + b'\r')
                    acted = time.time()
                    continue
                log('起标题：出现了没认出的弹窗', (dl.get('text') or '')[-80:])
                break
            rules = [i for i, l in enumerate(lines) if RULE_RE.match(l.strip())]
            if any(lines[a + 1].lstrip().startswith('❯') for a, b2 in zip(rules, rules[1:]) if 1 <= b2 - a - 1 <= 30):
                os.write(master, b'\x1b[200~' + safe_input(prompt).encode('utf-8') + b'\x1b[201~')
                time.sleep(0.6)
                os.write(master, b'\r')
                sent_at = acted = time.time()
    except Exception as e:
        log('起标题失败', repr(e))
    finally:
        if p and p.poll() is None:
            for sig, wait in ((signal.SIGHUP, 3), (signal.SIGKILL, 3)):
                try:
                    os.killpg(p.pid, sig)
                except OSError:
                    pass
                try:
                    p.wait(wait)
                    break
                except subprocess.TimeoutExpired:
                    pass
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        time.sleep(0.3)
        ua_cleanup(sid)
    return clean_title(out)


def title_one(sid):
    if load_conf().get('names', {}).get(sid) or auto_title(sid):
        return
    fp = find_transcript(sid)
    if not fp:
        return
    try:
        m = read_meta(fp, os.path.getsize(fp))
    except OSError:
        return
    if not m or m.get('custom') or m.get('ai'):
        return  # CLI 自己有标题
    cwd = m.get('cwd') or ''
    if not SHOW_TMP and (cwd.startswith(HIDE_PREFIX) or cwd.startswith(TITLE_DIR) or cwd.startswith(UA_DIR)):
        return
    q, a = title_material(fp)
    if not q or not a:
        return  # 还没有完整的一轮
    t = title_cli_run(TITLE_PROMPT.format(q=q, a=a))
    AUTO_TITLES[sid] = {'t': t, 'at': int(time.time())}
    save_titles()
    log('自动标题', sid, t or '（没起成功）')
    if t:
        BUS.emit({'type': 'sessions'})


def title_loop():
    time.sleep(float(os.environ.get('CWB_TITLE_DELAY') or 60))  # 测试时可调短
    try:
        now = time.time()
        names = load_conf().get('names', {})
        olds = sorted((s for s in INDEX.scan(force=True) if now - s['mtime'] < TITLE_DAYS * 86400 and not names.get(s['sid'])),
                      key=lambda s: -s['mtime'])
        for s in olds[:40]:
            title_maybe(s['sid'])
    except Exception as e:
        log('补标题出错', repr(e))
    while True:
        TITLE_EV.wait(600)
        TITLE_EV.clear()
        while True:
            with TITLE_LOCK:
                if not TITLE_Q:
                    break
                sid = TITLE_Q.pop(0)
                TITLE_QSET.discard(sid)
            try:
                title_one(sid)
            except Exception as e:
                log('起标题出错', sid, repr(e))


def api_ua_check(b):
    if not UA_LOCK.acquire(blocking=False):
        raise ValueError('正在自检，请稍候')
    try:
        res = ua_check_run()
    finally:
        UA_LOCK.release()
    try:
        tmp = UA_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(res, f, ensure_ascii=False)
        os.replace(tmp, UA_PATH)
    except OSError:
        pass
    log('请求头自检', '通过' if res['ok'] else '未通过', res.get('ua'), res.get('xapp'), res.get('error') or '', f"{res['ms']}ms")
    return res


def api_restart_service(b):
    """后台脱离运行 restart_when_idle.py --server-now：立即重启后台服务（CLI 在 ptyd 里不受影响），由 App 的看护拉起新服务。
    脚本按 CWB_PORT 读令牌、按本目录的 server.py 路径找进程，所以只会结束本目录这一份服务。"""
    p = subprocess.Popen([PY, '-I', os.path.join(ROOT, 'restart_when_idle.py'), '--server-now'], cwd=ROOT,
                         env=dict(os.environ, CWB_PORT=str(PORT)), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    threading.Thread(target=p.wait, daemon=True).start()  # 脚本自己两次 fork 脱离，第一层马上退出，收掉它
    log('请求重启后台服务（--server-now）')
    return {'ok': True}


API = {'projects': api_projects, 'open': api_open, 'new': api_new, 'send': api_send, 'interrupt': api_interrupt, 'send-now': api_send_now, 'watch': api_watch, 'unqueue': api_unqueue,
       'control': api_control, 'apply-opts': api_apply_opts, 'dialog': api_dialog, 'perm': api_perm, 'reveal': api_reveal,
       'open-url': api_open_url, 'add-folder': api_add_folder, 'pick-files': api_pick_files, 'stop': api_stop,
       'screen': api_screen, 'open-file': api_open_file, 'upload': api_upload, 'locate': api_locate,
       'archive': api_archive, 'delete': api_delete, 'delete-project': api_delete_project, 'notify': api_notify, 'search': api_search, 'files': api_files,
       'commands': api_commands, 'subagent': api_subagent, 'agents': api_agents, 'shell-output': api_shell_output, 'pin': api_pin, 'hide-project': api_hide_project, 'rename-project': api_rename_project,
       'rename': api_rename, 'git-status': api_git_status, 'git-diff': api_git_diff, 'git-revert': api_git_revert,
       'git-commit': api_git_commit, 'git-push': api_git_push, 'git-pr': api_git_pr, 'open-editor': api_open_editor,
       'tasks': api_tasks, 'fork': api_fork, 'fork-at': api_fork_at, 'page': api_page, 'press': api_press, 'project-opts': api_project_opts, 'prewarm': api_prewarm,
       'schedules': api_schedules, 'preview': api_preview, 'extensions': api_extensions,
       # 第十四轮 I
       'prefs': api_prefs, 'quota': api_quota, 'usage': api_usage, 'usage-summary': api_usage_summary, 'about': api_about, 'ua-check': api_ua_check,
       'restart-service': api_restart_service}
sys.path.append(ROOT); import features; features.register(globals())  # 第十一轮 B：搜索、端口、MCP、技能接口在 features.py（-I 模式下脚本目录不在 sys.path）
# 第十五轮 A2：旁问、停止后台任务、添加文件夹
API.update({'btw': api_btw, 'btw-list': api_btw_list, 'task-stop': api_task_stop, 'add-dir': api_add_dir})

# 第十五轮 A1：检查点与回退（checkpoints.py）；上下文用量、上下文明细、记忆文件、打开方式、标记未读（ctxstat.py）
import checkpoints as CK  # noqa: E402
import ctxstat as CX  # noqa: E402
CK.NS = CX.NS = globals()
import persona as PS  # 11-18 个性化
PS.NS = globals()
API.update({'persona': PS.api_persona, 'claude-md': PS.api_claude_md, 'cmd-list': PS.api_cmd_list, 'cmd-get': PS.api_cmd_get,
            'cmd-save': PS.api_cmd_save, 'cmd-delete': PS.api_cmd_delete, 'skill-get': PS.api_skill_get, 'skill-save': PS.api_skill_save})
API.update({'turn-changes': CK.api_turn_changes, 'turn-diff': CK.api_turn_diff, 'rewind-preview': CK.api_rewind_preview,
            'rewind-apply': CK.api_rewind_apply, 'rewind-undo': CK.api_rewind_undo,
            'context-detail': CX.api_context_detail, 'memory-files': CX.api_memory_files, 'file-save': CX.api_file_save,
            'open-apps': CX.api_open_apps, 'open-with': CX.api_open_with, 'mark-unread': CX.api_mark_unread})
import webpick; webpick.setup(PORT, STATIC_DIR, log); API.update({'wp-pick': webpick.api_wp_pick})  # 第十五轮 G：网页预览点选元素（只转发本机回环地址，见 webpick.py）
import remote as RM; RM.setup(globals())  # 第十六轮 R：Tailscale 远程访问（鉴权、配对、设备、开关、防休眠），见 remote.py
import providers as PV; PV.setup(globals())  # 第十六轮 P：Codex、Antigravity 对话（providers.py、prov_codex.py、prov_acp.py），接管这两家对话的接口，Claude 不变
import quota_hub as QH; QH.setup(globals())  # 第十七轮 U：三家额度快照（quota_hub.py）、定时读取、预警，扩展 quota 接口
import usage_p as UP; UP.setup(globals())  # 第十七轮 U：用量含终端和其他客户端（usage_p.py 管 Codex、Antigravity，Claude 仍是 usage.py），扩展 usage 接口
import collab as CB; CB.setup(globals())  # 第十七轮 M：多智能体协作（委派子任务，移植自 T3 Code），见 collab.py

CTYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
          '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon'}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _host_ok(self):
        return (self.headers.get('Host') or '').rsplit(':', 1)[0] in ('127.0.0.1', 'localhost')

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def _file(self, rel):
        p = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not p.startswith(STATIC_DIR + os.sep) or not os.path.isfile(p):
            return self._json(404, {})
        with open(p, 'rb') as f:
            self._send(200, f.read(), CTYPES.get(os.path.splitext(p)[1], 'application/octet-stream'))

    def _sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        q = BUS.add()
        try:
            self.wfile.write(b': hi\n\n')
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(b'data: ' + data.encode('utf-8') + b'\n\n')
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except Exception:
            pass
        finally:
            BUS.remove(q)
            self.close_connection = True

    def do_GET(self):
        if RM.gate(self, 'GET'):  # 第十六轮 R：ts.net 来的请求在这里鉴权（只认设备凭据）；本机请求原样往下走
            return
        if CB.http(self, 'GET'):  # 第十七轮 M：/mcp（协作服务，只认本机＋对话令牌）
            return
        if not self._host_ok():
            return self._json(403, {})
        u = urlparse(self.path)
        if u.path == '/ping':
            return self._send(200, b'ok %d' % len(BUS.clients), 'text/plain')
        if u.path == '/events':
            if parse_qs(u.query).get('t', [''])[0] != TOKEN:
                return self._json(403, {})
            return self._sse()
        if u.path in ('/img', '/file'):
            q = parse_qs(u.query)
            if q.get('t', [''])[0] != TOKEN:
                return self._json(403, {})
            try:
                if u.path == '/img':
                    res = read_image(q['sid'][0], int(q['o'][0]), [int(x) for x in q['p'][0].split(',')])
                else:
                    p = resolve_path(q['p'][0], q.get('cwd', [''])[0])
                    ct = dict(IMG_EXT, **{'.pdf': 'application/pdf'}).get(os.path.splitext(p or '')[1].lower())
                    res = None
                    if p and ct and os.path.isfile(p) and os.path.getsize(p) < 60 * 1024 * 1024:
                        with open(p, 'rb') as f:
                            res = (f.read(), ct)
            except Exception as e:
                log('读取图片失败', repr(e))
                res = None
            if not res:
                return self._json(404, {})
            self.send_response(200)
            self.send_header('Content-Type', res[1])
            self.send_header('Content-Length', str(len(res[0])))
            self.send_header('Cache-Control', 'private, max-age=86400')
            self.end_headers()
            self.wfile.write(res[0])
            return
        if u.path == '/':
            return self._file('index.html')
        if u.path.startswith('/static/'):
            return self._file(unquote(u.path[len('/static/'):]))
        return self._json(404, {})

    def do_POST(self):
        if RM.gate(self, 'POST'):  # 第十六轮 R：同上
            return
        if CB.http(self, 'POST'):  # 第十七轮 M：/mcp（协作服务，只认本机＋对话令牌，不用主令牌）
            return
        if not self._host_ok() or self.headers.get('X-Token') != TOKEN:
            return self._json(403, {})
        u = urlparse(self.path)
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        except Exception:
            body = {}
        if u.path.startswith('/hook/'):
            return self._json(200, handle_hook(u.path[len('/hook/'):], self.headers.get('X-Sid') or '', body, self.headers.get('X-Gen') or ''))
        fn = API.get(u.path[len('/api/'):]) if u.path.startswith('/api/') else None
        if not fn:
            return self._json(404, {})
        try:
            return self._json(200, fn(body) or {})
        except ValueError as e:
            return self._json(400, {'error': str(e)})
        except Exception as e:
            log('接口异常', u.path, repr(e))
            return self._json(500, {'error': str(e)})


# ---------------------------------------------------------------- 主循环
def poll_loop():
    k = 0
    while True:
        time.sleep(0.4)
        k += 1
        now = time.time()
        RM.tick(now)  # 第十六轮 R：远程开着时刷新 BUS.last_seen（不因“30 分钟没有窗口连接”退出），防休眠、写回设备最近访问
        for sid, t in list(TRANSCRIPTS.items()):
            r = RUNNERS.get(sid)
            if (r and r.state['alive']) or now - t.touched < 900:
                try:
                    t.poll()
                except Exception as e:
                    log('读取会话记录异常', sid, repr(e))
            elif now - t.touched > 3600:
                TRANSCRIPTS.pop(sid, None)
        for r in list(RUNNERS.values()):
            if r.notice and r.state.get('alive'):
                try:
                    r.analyze()  # 等待提示挂着时屏幕可能不再变化，定期看一眼输入框回来没有
                except Exception:
                    pass
            r.sync_meta()  # 第十三轮 E：meta 有变化就写回 ptyd（预热、无人值守、空闲后重启等标记）
        if k % 3 == 0:
            maybe_upgrade()
        if k % 150 == 0 and not BUS.clients and now - BUS.last_seen > IDLE_EXIT:
            if not PERMS and not any(r.active() for r in RUNNERS.values()) and not load_conf().get('schedules'):
                # 第十三轮 E：CLI 在 ptyd 里，server 退出不会带走它们：先关掉空闲的 CLI，再让 ptyd 退出，最后自己退出
                log('长时间无窗口连接，先关掉空闲的 CLI，再退出')
                rs = [r for r in list(KEYS.values()) if r.state.get('alive')]
                for r in rs:
                    r.stop()
                end = time.time() + 10
                while time.time() < end and any(r.state.get('alive') for r in rs):
                    time.sleep(0.2)
                if any(r.state.get('alive') or r.active() for r in RUNNERS.values()) or PERMS:
                    log('关闭途中又有事要做，不退出')
                    continue
                try:
                    PTYD.request({'op': 'shutdown', 'close': True, 'why': 'idle'}, timeout=5)
                except Exception:
                    pass
                try:
                    open(os.path.join(RUN_DIR, 'stopped-%d' % PORT), 'w').close()  # 告诉启动器这是主动退出，别再拉起
                except OSError:
                    pass
                os.kill(os.getpid(), signal.SIGTERM)


UPG = {'ok': 0}


def maybe_upgrade():
    """第十三轮 E：ptyd 的版本或代码和 server 期望的不一致时“待升级”：等所有对话空闲（连续 3 次、约 3.6 秒），
    关掉 CLI（不丢对话，下次发消息自动 --resume），让旧 ptyd 退出，再拉起新的。不做 fd 热交接。"""
    if not PTYD.upgrade or PTYD.upgrading or not PTYD.sock:
        UPG['ok'] = 0
        return
    busy = PERMS or any(r.active() or r.turn for r in list(KEYS.values()) if r.state.get('alive'))
    UPG['ok'] = 0 if busy else UPG['ok'] + 1
    if UPG['ok'] < 3:
        return
    UPG['ok'] = 0
    PTYD.upgrading = True
    threading.Thread(target=do_upgrade, daemon=True).start()


def do_upgrade():
    try:
        log('终端守护进程待升级（' + PTYD.upgrade + '），所有对话空闲，开始更换')
        rs = [r for r in list(KEYS.values()) if r.state.get('alive')]
        for r in rs:
            r.stop()
        end = time.time() + 12
        while time.time() < end and any(r.state.get('alive') for r in rs):
            time.sleep(0.1)
        old = PTYD.info.get('pid')
        try:
            PTYD.request({'op': 'shutdown', 'close': True, 'why': 'upgrade'}, timeout=5)
        except Exception as e:
            log('请旧的终端守护进程退出失败', repr(e))
        end = time.time() + 15
        while time.time() < end:
            if PTYD.proc and PTYD.proc.pid == old:
                PTYD.proc.poll()
            try:
                os.kill(old, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            log('旧的终端守护进程没有退出，强制结束', old)
            try:
                os.kill(old, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.5)
        for _ in range(50):
            if not PTYD.sock:
                break
            time.sleep(0.1)
        ok = PTYD.ensure()
        log('终端守护进程已更换', old, '→', PTYD.info.get('pid'), '成功' if ok and not PTYD.upgrade else '失败')
        BUS.emit({'type': 'sessions'})
    except Exception as e:
        log('更换终端守护进程异常', repr(e))
    finally:
        PTYD.upgrading = False


def adopt_all():
    """第十三轮 E：启动时连上 ptyd（连不上就拉起），按 meta 把里面的会话恢复成 Runner；server 不在时已退出的也走一遍退出处理。
    再看 .run/hooks 里没有对应 CLI 的事件文件：最后一次是提交或授权（这一轮没正常 Stop）且记录里这一轮没答完，记成被中断。"""
    ents = []
    if PTYD.ensure():
        try:
            ents = PTYD.request({'op': 'list'}, timeout=10).get('sessions') or []
        except Exception as e:
            log('读取终端守护进程会话失败', repr(e))
    live = set()
    for ent in sorted(ents, key=lambda e: e.get('started') or 0):
        m = ent.get('meta') or {}
        sid = m.get('sid')
        if not sid:
            continue
        try:
            r = Runner(sid, m.get('cwd') or '/', False, adopt=ent)
        except Exception as e:
            log('接管会话失败', sid, repr(e))
            continue
        live.add(r.gen)
        if m.get('new_cwd'):
            NEW_SESS[sid] = m['new_cwd']
        if m.get('fork_from'):
            FORK_SRC[sid] = m['fork_from']
        if m.get('wt'):
            WT_SESS[sid] = m['wt']
        with G:
            cur = RUNNERS.get(sid)
            if not cur or cur.stopping or not cur.state.get('alive'):
                RUNNERS[sid] = r
        if r.prewarm and ent.get('alive') and not r.stopping:
            PREWARM[r.cwd] = r
        get_transcript(sid)
        if r.hook_sid and ent.get('alive'):
            rekey(r, r.hook_sid, 'clear')
    try:
        files = glob.glob(os.path.join(HOOKS_DIR, '*.json'))
    except Exception:
        files = []
    for fp in files:
        gen = os.path.basename(fp)[:-5]
        if gen in live:
            continue
        try:
            with open(fp, encoding='utf-8') as f:
                d = json.load(f)
            sid = str(d.get('session_id') or d.get('sid') or '')
            if d.get('ev') in ('busy', 'permission') and time.time() - float(d.get('t') or 0) < 7 * 86400 \
                    and re.fullmatch(r'[0-9a-fA-F-]{36}', sid) and transcript_open(sid)[0]:
                mark_interrupted(sid, 'shutdown')
        except Exception as e:
            log('读取钩子事件文件失败', fp, repr(e))
        try:
            os.remove(fp)
        except OSError:
            pass
    if ents:
        log('接管完成：', len([e for e in ents if e.get('alive')]), '个在跑，', len([e for e in ents if not e.get('alive')]), '个已退出')


def shutdown(*_):
    """第十三轮 E：只断开和 ptyd 的连接，不关 CLI；对话在 ptyd 里照常进行，新的 server 起来后接管。"""
    SHUTTING[0] = True
    log('收到停止信号，退出；CLI 留在终端守护进程里继续运行：', ','.join(sid[:8] for sid in RUNNERS) or '无')
    for r in list(RUNNERS.values()):
        try:
            r.sync_meta()
        except Exception:
            pass
    for sid in list(RUNNERS):
        note_own(sid)
    os._exit(0)


def main():
    global LOGIN_ENV, CLAUDE, TOKEN
    os.makedirs(RUN_DIR, exist_ok=True)
    try:
        with open(os.path.join(RUN_DIR, f'token-{PORT}')) as f:
            old = f.read().strip()
        if len(old) >= 24:
            TOKEN = old
    except OSError:
        pass
    LOGIN_ENV = capture_login_env()
    CLAUDE = shutil.which('claude', path=LOGIN_ENV.get('PATH')) or shutil.which('claude') or os.path.join(HOME, '.local/bin/claude')
    os.makedirs(HOOKS_DIR, exist_ok=True)  # 第十三轮 E
    load_marks()
    try:
        os.remove(os.path.join(RUN_DIR, 'stopped-%d' % PORT))  # 重新启动了，启动器的看护恢复
    except OSError:
        pass
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    srv.daemon_threads = True
    tok = os.path.join(RUN_DIR, f'token-{PORT}')
    with open(os.open(tok, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        f.write(TOKEN)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    load_moved()  # 11-11：项目改名的路径映射（接管 CLI 和处理请求之前）
    adopt_all()  # 第十三轮 E：接管 ptyd 里的 CLI（在开始处理请求之前，免得钩子被当成旧进程的事件丢掉）
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=search_loop, daemon=True).start()
    threading.Thread(target=sched_loop, daemon=True).start()
    threading.Thread(target=title_loop, daemon=True).start()  # 11-11：自动起标题
    CX.start()  # 第十五轮 A1：盯在跑的对话的状态行文件，变了推 ctx 事件
    USAGE.start()  # 第十四轮 I：用量统计先在后台读缓存、扫一遍记录
    log('已启动', f'http://127.0.0.1:{PORT}', 'claude =', CLAUDE)
    srv.serve_forever()


if __name__ == '__main__':
    main()
