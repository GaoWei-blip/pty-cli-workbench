"""移植自 T3 Code apps/server/src/mcp/OrchestratorMcpService.ts 等（MIT，Copyright (c) 2026 T3 Tools Inc.；v0.1.11，完整清单见下）。

第十七轮 M：多智能体协作（委派子任务）。对应的 T3 文件：
- apps/server/src/mcp/toolkits/orchestrator/tools.ts、handlers.ts：四个工具的名字、说明、参数（orchestrator_capabilities、
  delegate_task、task_status、task_cancel）
- packages/contracts/src/orchestratorMcp.ts：参数和返回的结构、任务状态取值、ORCHESTRATOR_MCP_RUNNING_TASK_NEXT
- apps/server/src/mcp/OrchestratorMcpService.ts：capabilities、delegateTask、taskStatus、cancelTask、readTask、providerConstraints、
  resolveTarget、listCapabilityModels（LISTED_MODEL_LIMIT=8）、clientRequestId 去重、读到结果即“已送达”
- apps/server/src/mcp/delegatedTaskModes.ts：runtimeModeRank、resolveRuntimeMode、resolveInteractionMode、taskPrompt
- apps/server/src/orchestration-v2/Orchestrator.ts：delegatedCompletionWakeDetail（叫醒正文，内联上限 16000 字节）
- packages/provider-core/src/server/orchestrationInstructions.ts：编排说明（T3_CODE_ORCHESTRATION_SECTIONS 第一段）
- apps/server/src/orchestration-v2/Adapters/CodexAdapterV2.ts：Codex 挂 MCP 的写法（config 里点号键 mcp_servers.<名>、
  tool_timeout_sec、http_headers）、运行档到 approvalPolicy/sandbox 的映射、mcpServer/elicitation/request 出授权卡
- apps/server/src/provider/acp/AntigravityAcpSupport.ts：agy 模式和运行档的对应
- apps/web/src/components/chat/ChildThreadApprovals.tsx 及中文语言包：界面文案（static/ux-w.js）

不移植：T3 经 Agent SDK 驱动 Claude 的 ClaudeAdapterV2（setMcpServers、SDK 流）。Claude 一律是伪终端里的交互式 claude：
挂工具用 --mcp-config 文件，叫醒用 --settings 里的 PostToolUse 钩子（asyncRewake，hook_wait.py），结果读会话记录。
协作关着时 Claude 的启动参数和第十六轮逐字一样（claude_args 原样返回）。

数据：data/tasks.jsonl（第十七轮 X3 约定的委派事件流，U 线只读）；data/tasks/<任务>.json（任务全文和结果，工作台自己用）；
令牌在内存和 .run/mcp-<对话>.json（0600，Claude 的 --mcp-config 就是这个文件），不进命令行参数。
"""
import json
import math
import os
import re
import threading
import time
import uuid

try:
    import quota_hub as QH  # 第十七轮 X2：U 线的额度快照模块；没有它（或读不到）就按“额度未知”照常委派
except ImportError:
    QH = None

NS = {}
LOCK = threading.RLock()
COND = threading.Condition(LOCK)
TASKS = {}       # 任务 ID -> 任务
CHILD = {}       # 子对话 ID -> 任务 ID（深度固定 1：子对话不挂 cwb）
TOK = {}         # 令牌 -> 父对话 ID
SID_TOK = {}     # 父对话 ID -> 令牌
REQKEY = {}      # (父对话, clientRequestId) -> 任务 ID（T3：同一个 clientRequestId 重试返回同一个任务）
RATE = {}        # 父对话 -> [委派时间]
RESV = {}        # 父对话 -> 已占位还没登记进 TASKS 的委派个数（复核 M-1）
LABEL = {'claude': 'Claude', 'codex': 'Codex', 'agy': 'Antigravity'}
DRIVER = {'claude': 'claudeAgent', 'codex': 'codex', 'agy': 'antigravity'}
PROVS = ('claude', 'codex', 'agy')
SERVER = 'cwb'
PROTO_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
WAKE_POLL = float(os.environ.get('CWB_COLLAB_POLL') or 25)          # 叫醒钩子每次长轮询最多等这么久
LOST_AFTER = float(os.environ.get('CWB_COLLAB_LOST') or 40)         # 子任务结束后这么久没有钩子来取，算叫醒丢失
RATE_WIN = 3600
MINUTE = float(os.environ.get('CWB_COLLAB_MINUTE') or 60)            # 测试可把“分钟”调短
DEFAULTS = {'on': False, 'provs': {'claude': True, 'codex': True, 'agy': True}, 'perm': 'same',
            'maxPer': 3, 'maxAll': 5, 'maxMins': 30, 'autoReturn': True}
RATE_MAX = 10

# ---------------------------------------------------------------- T3 contracts：状态与运行档
TERMINAL = ('completed', 'failed', 'cancelled', 'interrupted')
RUNTIME_MODES = ('approval-required', 'auto-accept-edits', 'auto', 'full-access')
RANK = {m: i for i, m in enumerate(RUNTIME_MODES)}
MODE_ZH = {'approval-required': '每步确认', 'auto-accept-edits': '自动改文件', 'auto': '自动', 'full-access': '完全放开'}
ROLES = ('implementation', 'research', 'review', 'design', 'test', 'general')
ORCHESTRATOR_MCP_RUNNING_TASK_NEXT = ('Completion wakes this thread. Do not wait on it or poll it. Continue other work, '
                                      'and end your turn when none is left.')
DELEGATED_RESULT_INLINE_BYTES = 16_000
LISTED_MODEL_LIMIT = 8
MAX_WAIT_TIMEOUT_MS, DEFAULT_WAIT_TIMEOUT_MS = 45_000, 30_000
CODEX_MCP_TOOL_TIMEOUT_SEC = 65 * 60  # T3：CODEX_T3_MCP_TOOL_TIMEOUT_SEC
# X3（data/tasks.jsonl）只有这几种状态；T3 的 starting 记 queued，cancelled、interrupted 记 stopped，到时自动停记 timeout
X3 = {'queued': 'queued', 'starting': 'queued', 'running': 'running', 'waiting': 'waiting', 'completed': 'done',
      'failed': 'failed', 'cancelled': 'stopped', 'interrupted': 'stopped'}

# 各家权限档 ↔ T3 运行档（T3：ClaudeAdapterV2 的 permissionMode、CodexAdapterV2 的 codexRuntimeModeTurnDefaults、
# AntigravityAcpSupport 的 antigravityPermissionMode）。工作台的 Claude 档位名见 server.py 的 MODE_FLAG
CLAUDE_OF = {'approval-required': 'manual mode', 'auto-accept-edits': 'accept edits', 'auto': 'auto mode',
             'full-access': 'bypass permissions'}
CODEX_OF = {'approval-required': 'untrusted', 'auto-accept-edits': 'auto', 'full-access': 'full'}
AGY_OF = {'approval-required': 'default', 'auto-accept-edits': 'accept-edits', 'auto': 'auto',
          'full-access': 'auto'}  # 11-25：agy 会话里没有放开档，full-access＝它自己的 auto 档＋工作台自动允许（见 spawn 的 autoAllow）
MODE_OF = {'claude': CLAUDE_OF, 'codex': CODEX_OF, 'agy': AGY_OF}
REVIEW_MODE = {'claude': 'manual mode', 'codex': 'read-only', 'agy': 'default'}  # 手动复核：只读或每步确认档


def claude_rt(mode):
    m = (mode or '').strip().lower()
    if m == 'bypass permissions':
        return 'full-access'
    if m == 'auto mode':
        return 'auto'
    if m == 'accept edits':
        return 'auto-accept-edits'
    return 'approval-required'  # manual / default / plan / 认不出：按最窄的算


def codex_rt(mode):
    m = mode or 'default'
    if m == 'full':
        return 'full-access'
    if m == 'auto':
        return 'auto-accept-edits'
    if m in ('read-only', 'untrusted'):
        return 'approval-required'
    return codex_cfg_rt()


def codex_cfg_rt():
    """Codex“按 Codex 设置”：读 config.toml 的 approval_policy、sandbox_mode（只读）；认不出按最窄的算。"""
    home = os.environ.get('CWB_CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')
    try:
        txt = open(os.path.join(home, 'config.toml'), encoding='utf-8').read()
    except OSError:
        return 'approval-required'
    top = re.split(r'(?m)^\s*\[', txt, 1)[0]
    ap = re.search(r'(?m)^\s*approval_policy\s*=\s*"([^"]+)"', top)
    sb = re.search(r'(?m)^\s*sandbox_mode\s*=\s*"([^"]+)"', top)
    ap, sb = ap.group(1) if ap else '', sb.group(1) if sb else ''
    if ap == 'never' and sb == 'danger-full-access':
        return 'full-access'
    if sb == 'workspace-write' and ap in ('on-request', 'on-failure'):
        return 'auto-accept-edits'
    return 'approval-required'


def agy_rt(mode):
    return {'accept-edits': 'auto-accept-edits', 'auto': 'auto'}.get(mode or 'default', 'approval-required')


def log(*a):
    try:
        NS['log']('[协作]', *a)
    except Exception:
        pass


def emit(ev):
    try:
        NS['BUS'].emit(ev)
    except Exception:
        pass


def now_ms():
    return int(time.time() * 1000)


class Fail(Exception):
    """T3 的 OrchestratorMcpFailure：工具按 failureMode "return" 把 {code, message} 返回给智能体。"""
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code, self.message = code, message


# ================================================================ 设置
def prefs(c=None):
    p = (c if c is not None else NS['load_conf']()).get('collab')
    p = p if isinstance(p, dict) else {}
    out = json.loads(json.dumps(DEFAULTS))
    if isinstance(p.get('on'), bool):
        out['on'] = p['on']
    if isinstance(p.get('provs'), dict):
        for k in PROVS:
            if isinstance(p['provs'].get(k), bool):
                out['provs'][k] = p['provs'][k]
    if p.get('perm') in ('same', 'approval-required', 'auto-accept-edits'):
        out['perm'] = p['perm']
    for k, lo, hi in (('maxPer', 1, 10), ('maxAll', 1, 20), ('maxMins', 1, 240)):
        v = p.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
            out[k] = v
    if isinstance(p.get('autoReturn'), bool):
        out['autoReturn'] = p['autoReturn']
    return out


def adapters():
    pv = NS.get('PV')
    return pv.ADAPTERS if pv else {}


def prov_status(prov, force=False):
    """{installed, ok, hint}：Claude 只看命令在不在；Codex、agy 用 P 线的 status()（不为查状态去起进程）。"""
    if prov == 'claude':
        return {'installed': True, 'ok': True, 'hint': ''}
    a = adapters().get(prov)
    if not a:
        return {'installed': False, 'ok': False, 'hint': '没有这个提供方'}
    try:
        return a.status(force=force)
    except Exception as e:
        return {'installed': False, 'ok': False, 'hint': str(e)}


def api_prefs(b):
    """设置 →“协作”：读、改设置，带上三家能不能用。"""
    if isinstance(b.get('set'), dict):
        cur = prefs()
        s = b['set']
        for k in ('on', 'autoReturn'):
            if isinstance(s.get(k), bool):
                cur[k] = s[k]
        if isinstance(s.get('provs'), dict):
            for k in PROVS:
                if isinstance(s['provs'].get(k), bool):
                    cur['provs'][k] = s['provs'][k]
        if s.get('perm') in ('same', 'approval-required', 'auto-accept-edits'):
            cur['perm'] = s['perm']
        for k, lo, hi in (('maxPer', 1, 10), ('maxAll', 1, 20), ('maxMins', 1, 240)):
            if k in s:
                try:
                    v = int(s[k])
                except (TypeError, ValueError):
                    raise ValueError('请填 %d 到 %d 之间的整数' % (lo, hi))
                if not lo <= v <= hi:
                    raise ValueError('请填 %d 到 %d 之间的整数' % (lo, hi))
                cur[k] = v
        was = prefs()['on']
        with NS['G']:
            c = NS['load_conf']()
            c['collab'] = cur
            NS['save_conf'](c)
        log('设置已改', json.dumps(cur, ensure_ascii=False))
        if was != cur['on']:
            drop_prewarm(True)  # 预热的 CLI 参数是按旧开关定的（复核 M-2，和改项目选项时一样停掉）
        emit({'type': 'collab-prefs', 'prefs': cur})
    return {'prefs': prefs(), 'provs': [dict(prov_status(k, bool(b.get('refresh'))), id=k, label=LABEL[k]) for k in PROVS]}


# ================================================================ 令牌与 MCP 配置文件
def mcp_path(sid):
    return os.path.join(NS['RUN_DIR'], f'mcp-{sid}.json')


def mcp_doc(tok):
    return {'mcpServers': {SERVER: {'type': 'http', 'url': f'http://127.0.0.1:{NS["PORT"]}/mcp',
                                    'headers': {'Authorization': 'Bearer ' + tok}}}}


def write_0600(path, text):
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode('utf-8'))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def token_for(sid):
    """每个父对话一个令牌，换模型重启、服务重启都沿用（存在 0600 文件里）；文件里的端口不对就重写。"""
    with LOCK:
        tok = SID_TOK.get(sid)
        p = mcp_path(sid)
        if not tok:
            tok = 'cwb_' + uuid.uuid4().hex + uuid.uuid4().hex[:16]
        doc = json.dumps(mcp_doc(tok), ensure_ascii=False)
        try:
            cur = open(p, encoding='utf-8').read()
        except OSError:
            cur = ''
        if cur != doc or (os.stat(p).st_mode & 0o077 if cur else True):
            os.makedirs(NS['RUN_DIR'], exist_ok=True)
            write_0600(p, doc)
        TOK[tok], SID_TOK[sid] = sid, tok
        return tok


def load_tokens():
    try:
        names = os.listdir(NS['RUN_DIR'])
    except OSError:
        return
    for n in names:
        m = re.fullmatch(r'mcp-([0-9a-fA-F-]{36})\.json', n)
        if not m:
            continue
        try:
            d = json.load(open(os.path.join(NS['RUN_DIR'], n), encoding='utf-8'))
            h = d['mcpServers'][SERVER]['headers']['Authorization']
        except Exception:
            continue
        if h.startswith('Bearer '):
            TOK[h[7:]], SID_TOK[m.group(1)] = m.group(1), h[7:]


def drop_token(sid):
    with LOCK:
        tok = SID_TOK.pop(sid, None)
        if tok:
            TOK.pop(tok, None)
    try:
        os.remove(mcp_path(sid))
    except OSError:
        pass


def caller_of(tok):
    """令牌 → 父对话（/clear 换了 ID 的按别名找到现在的 ID）。"""
    sid = TOK.get(tok or '')
    if not sid:
        return None
    try:
        return NS['resolve_sid'](sid)
    except Exception:
        return sid


# ================================================================ 编排说明（T3 orchestrationInstructions.ts 第一段，去掉工作台没有的工具）
# 11-24 偏离 T3（工作台已有链路）：T3 用 Agent SDK 驱动 Claude，禁止后台原生 Agent；工作台是交互式 CLI，后台原生子代理
# 跑完由 CLI 自己叫醒父对话、右栏“任务”页照常显示，且不额外占 MAX_RUNNERS 名额。所以同家同模型的 Claude 子代理前台、后台都用原生 Agent，
# 只有 Codex 的后台子代理仍走 delegate_task。原句：“(Claude `Agent`, `run_in_background:false`; …) … Background agent: `delegate_task`, never a background `Agent`.”
INSTRUCTIONS = """## Claude Workbench orchestration (cwb)

Ordinary coding tasks need no cwb tools.

- Subagents: use your native tool when the child uses your provider, a model it offers and no settings beyond yours (Claude `Agent`, foreground or `run_in_background:true`; Codex `spawn_agent`, then wait). Use `delegate_task` for another provider's model, user-asked settings, or `delegate_task` by name. A native type on another provider's model counts too. Claude background agent on your own provider and model: a background `Agent`, not `delegate_task`. Codex background agent: `delegate_task`.
- Call `orchestrator_capabilities` once per thread, before the first `delegate_task`. Again only after an error or a user-named provider change. Settings go in `target.options`. Provider or model unavailable: say so. Never switch unasked or fall back to native tools or CLIs.
- Every new delegate review or re-check is a new `delegate_task` (brief, findings, objections). Fix small review findings yourself; start no delegate for them. When told to proceed without a delegate, or to run fewer, `task_cancel` the extras. Start no replacement.
- Follow a running delegate's `next`. Don't poll it (`task_status`, `sleep`, watchers, Monitor, ScheduleWakeup). Stop a delegate run you replace or no longer need with `task_cancel`.
- As a subagent or child, do the work yourself. Start no subagents unless your task says to.
- A refused delegation (quota nearly used up, too many running or recent tasks, collaboration turned off in Claude Workbench): tell the user the reason as given. Never switch providers to get around it.
- cwb tools may load late. Try the name once before you call it absent."""
CLAUDE_TOOLSEARCH = ('\n- Claude Code defers MCP tools behind ToolSearch. Load them with ToolSearch '
                     '`select:mcp__cwb__orchestrator_capabilities,mcp__cwb__delegate_task,mcp__cwb__task_status,mcp__cwb__task_cancel` '
                     'before the first call.')


def instructions(prov):
    return INSTRUCTIONS + (CLAUDE_TOOLSEARCH if prov == 'claude' else '')


# ================================================================ 工具定义（T3 tools.ts，参数按 contracts 写成 JSON Schema）
_TARGET = {'type': 'object', 'properties': {
    'providerInstanceId': {'type': 'string', 'enum': list(PROVS), 'description': 'Provider instance id from `orchestrator_capabilities`.'},
    'driverKind': {'type': 'string', 'description': 'Prefer `providerInstanceId`.'},
    'model': {'type': 'string', 'minLength': 1},
    'options': {'description': "Options from `orchestrator_capabilities`, such as {effort:'max'}. Given values override inherited. [] or {} clears.",
                'anyOf': [{'type': 'object', 'additionalProperties': {'type': ['string', 'boolean', 'number']}},
                          {'type': 'array', 'items': {'type': 'object', 'properties': {'id': {'type': 'string'}, 'value': {}}, 'required': ['id', 'value']}}]},
}, 'additionalProperties': False}
TOOLS = [
    {'name': 'orchestrator_capabilities', 'title': 'Get orchestration capabilities',
     'description': 'List providers, models, options, and runtime modes for `delegate_task`. Short list. `search` finds others. Any catalog model works.',
     'inputSchema': {'type': 'object', 'properties': {
         'search': {'type': 'string', 'minLength': 1, 'maxLength': 200, 'description': 'Match model ids and labels. Replaces the short list.'}},
         'additionalProperties': False},
     'annotations': {'title': 'Get orchestration capabilities', 'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True}},
    {'name': 'delegate_task', 'title': 'Delegate a child task',
     'description': 'Start one child agent with only `task`. No parent history. Keep `taskId` for `task_status` and `task_cancel`. No secrets in `task`.',
     'inputSchema': {'type': 'object', 'properties': {
         'task': {'type': 'string', 'minLength': 1, 'maxLength': 120000},
         'target': _TARGET,
         'title': {'type': 'string', 'minLength': 1, 'maxLength': 512},
         'role': {'type': 'string', 'enum': list(ROLES)},
         'isolation': {'type': 'string', 'enum': ['shared'], 'description': "'shared' (default): parent checkout."},
         'mode': {'type': 'string', 'enum': ['async', 'wait'],
                  'description': "Default 'async'. 'wait' only if this turn needs the result. A question or approval ends the wait."},
         'timeoutMs': {'type': 'number', 'description': "Mode 'wait' only. At most 45 s. A timeout does not cancel the child."},
         'clientRequestId': {'type': 'string', 'minLength': 1, 'maxLength': 256,
                             'description': 'Reuse it to retry the same call. A new call gets a new key.'},
         'runtimeMode': {'type': 'string', 'enum': ['inherit'] + list(RUNTIME_MODES),
                         'description': 'Omit unless the user asks for a supervised child. Narrow modes pause for human approval, not read-only. Above yours is rejected.'},
         'interactionMode': {'type': 'string', 'enum': ['inherit', 'default', 'plan']},
     }, 'required': ['task'], 'additionalProperties': False},
     'annotations': {'title': 'Delegate a child task', 'destructiveHint': True, 'openWorldHint': True}},
    {'name': 'task_status', 'title': 'Get delegated task status',
     'description': ('Read one delegated task you started. Do not call it on a running task before its completion wakes you. '
                     '`workState`: working, waiting_for_children, result_available. A finished turn with live child work is not done. '
                     '`summary`: final result. Reading it marks it delivered.'),
     'inputSchema': {'type': 'object', 'properties': {'taskId': {'type': 'string', 'minLength': 1}}, 'required': ['taskId'],
                     'additionalProperties': False},
     'annotations': {'title': 'Get delegated task status', 'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}},
    {'name': 'task_cancel', 'title': 'Cancel delegated task',
     'description': 'Stop one delegated task by `taskId`. Stops its running and queued turns, including later runs in its child thread. A finished task keeps its result.',
     'inputSchema': {'type': 'object', 'properties': {
         'taskId': {'type': 'string', 'minLength': 1}, 'reason': {'type': 'string', 'maxLength': 2000},
         'clientRequestId': {'type': 'string', 'minLength': 1, 'maxLength': 256}}, 'required': ['taskId'], 'additionalProperties': False},
     'annotations': {'title': 'Cancel delegated task', 'destructiveHint': True}},
]
# T3 的这四个工具不标 anthropic/alwaysLoad（只有 request_secret 标了）：Claude Code 把它们放在 ToolSearch 后面，
# 编排说明里有一句先用 ToolSearch 加载（CLAUDE_TOOLSEARCH）。照 T3。


# ================================================================ 任务记录（X3）
def data_dir():
    pv = NS.get('PV')
    return pv.DATA_DIR if pv else os.path.join(NS['ROOT'], 'data')


def x3_path():
    return os.path.join(data_dir(), 'tasks.jsonl')


def task_file(tid):
    return os.path.join(data_dir(), 'tasks', tid + '.json')


def x3_write(row):
    try:
        os.makedirs(data_dir(), exist_ok=True)
        with LOCK:
            with open(x3_path(), 'a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError as e:
        log('写 tasks.jsonl 失败', repr(e))


def save(t):
    try:
        os.makedirs(os.path.dirname(task_file(t['id'])), exist_ok=True)
        tmp = task_file(t['id']) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({k: v for k, v in t.items() if not k.startswith('_')}, f, ensure_ascii=False)
        os.replace(tmp, task_file(t['id']))
    except OSError as e:
        log('保存任务失败', t.get('id'), repr(e))


def x3_task(t):
    x3_write({'t': 'task', 'id': t['id'], 'parent': {'conv': t['parent'], 'provider': t['parentProv']},
              'child': {'conv': t['child'], 'provider': t['childProv'], 'model': t.get('model') or ''},
              'title': t['title'], 'mode': t['runtimeMode'], 'origin': t['origin'], 'createdAt': t['createdAt']})


def set_status(t, status, reason=None):
    """改任务状态：X3 状态变了写一行 task_status；推给界面；结束时叫醒等着的钩子。"""
    with LOCK:
        if t['status'] in TERMINAL and status != t['status']:
            return False
        old = t['status']
        t['status'] = status
        if reason:
            t['reason'] = reason
        x3 = 'timeout' if status == 'interrupted' and t.get('reason') == 'timeout' else X3[status]
        if status in TERMINAL and not t.get('endedAt'):
            t['endedAt'] = now_ms()
        if status in ('running', 'waiting') and not t.get('startedAt'):
            t['startedAt'] = now_ms()
        changed = x3 != t.get('x3')
        if changed:
            t['x3'] = x3
            row = {'t': 'task_status', 'id': t['id'], 'status': x3, 'ts': now_ms()}
            if status in TERMINAL:
                row['resultBytes'] = len((t.get('summary') or '').encode('utf-8'))
                row['delivered'] = t.get('delivery') == 'acknowledged'
            x3_write(row)
        if changed or old != status or reason:
            save(t)  # 状态没变（track 每秒都会调）不重写任务文件（复核 M-7）
        COND.notify_all()
    if changed or old != status:
        push(t)
        if status in TERMINAL or status == 'waiting':
            notify(t)
    return True


def set_delivery(t, state, how=None):
    with LOCK:
        if t.get('delivery') == state:
            return
        t['delivery'] = state
        if how:
            t['wake'] = how
        if state == 'acknowledged' and t['status'] in TERMINAL:
            x3_write({'t': 'task_status', 'id': t['id'], 'status': t.get('x3') or X3[t['status']], 'ts': now_ms(),
                      'resultBytes': len((t.get('summary') or '').encode('utf-8')), 'delivered': True})
        save(t)
        COND.notify_all()
    push(t)


def push(t):
    emit({'type': 'cwb-task', 'task': pub(t)})
    emit({'type': 'sessions'})


def notify(t):
    """子任务等确认、完成、失败时发通知（远程窗口由 ux-r.js 换成页面内提示）。"""
    st = t['status']
    who = LABEL.get(t['childProv'], '')
    if st == 'waiting':
        text = f'{who} 子任务需要你确认：{t["title"]}'
    elif st == 'completed':
        text = f'{who} 子任务已完成：{t["title"]}'
    elif st == 'failed':
        text = f'{who} 子任务失败：{t["title"]}'
    else:
        return
    emit({'type': 'notify', 'sid': t['child'] if st == 'waiting' else t['parent'], 'title': 'Claude 工作台', 'text': text})


STATE_ZH = {'queued': '排队中', 'starting': '排队中', 'running': '运行中', 'waiting': '需要批准', 'completed': '已完成',
            'failed': '失败', 'cancelled': '已取消', 'interrupted': '已中断'}


def pub(t):
    """给界面的任务（委派卡、左栏、右栏“委派”分组）。"""
    st = t['status']
    zh = '超时' if st == 'interrupted' and t.get('reason') == 'timeout' else STATE_ZH.get(st, st)
    if st == 'waiting' and t.get('question'):
        zh = '需要输入'
    s = t.get('summary') or ''
    return {'id': t['id'], 'parent': t['parent'], 'parentProv': t['parentProv'], 'child': t['child'], 'childProv': t['childProv'],
            'childLabel': LABEL.get(t['childProv'], t['childProv']), 'parentLabel': LABEL.get(t['parentProv'], t['parentProv']),
            'model': t.get('model') or '', 'title': t['title'], 'origin': t['origin'], 'status': st, 'x3': t.get('x3'), 'state': zh,
            'createdAt': t['createdAt'], 'startedAt': t.get('startedAt'), 'endedAt': t.get('endedAt'),
            'summary': s[:20000], 'more': len(s) > 20000, 'delivery': t.get('delivery') or 'none', 'wake': t.get('wake'),
            'quotaUnknown': bool(t.get('quotaUnknown')), 'mode': t['runtimeMode'], 'modeLabel': MODE_ZH.get(t['runtimeMode'], ''),
            'usage': t.get('usage'), 'anchor': t.get('anchor'), 'reason': t.get('reason'),
            'task': (t.get('task') or '')[:200]}


def live(t):
    return t['status'] not in TERMINAL


def load_tasks():
    d = os.path.join(data_dir(), 'tasks')
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if not n.endswith('.json'):
            continue
        try:
            t = json.load(open(os.path.join(d, n), encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(t, dict) or not t.get('id') or not t.get('child'):
            continue
        TASKS[t['id']] = t
        CHILD[t['child']] = t['id']
        if t.get('clientRequestId'):
            REQKEY[(t['parent'], t['clientRequestId'])] = t['id']


def reconcile():
    """服务启动后（等接管完 CLI）：还没结束的子任务，子对话的这一轮已经断了（“上次回复被中断”）就记 stopped；
    Claude 子对话在 ptyd 里，server 重启不打断它，接着跟踪。只核对启动时从磁盘读到的任务（等的这几秒里新建的不算）。"""
    loaded = list(TASKS.values())
    time.sleep(float(os.environ.get('CWB_COLLAB_RECONCILE') or 4))
    marks = NS['MARKS'].get('interrupted', {})
    for t in loaded:
        if not live(t):
            if t['parentProv'] == 'claude' and t.get('delivery') in (None, 'none') and t['origin'] == 'model':
                t['_term_at'] = time.time()  # 重启前结束、还没交回：给钩子一点时间重连，否则出“交回”按钮
            continue
        child = t['child']
        if t['childProv'] == 'claude':
            r = NS['RUNNERS'].get(child)
            if r and r.state.get('alive'):
                t['_seen_busy'] = bool(r.turn) or t['status'] in ('running', 'waiting')
                continue
        if child in marks or t['childProv'] != 'claude':
            t['reason'] = 'restart'
            set_status(t, 'interrupted', 'restart')
            log('服务重启，子任务记为已停止', t['id'])
        else:
            set_status(t, 'interrupted', 'restart')


# ================================================================ 提供方信息（capabilities）
def claude_models():
    ml = NS['model_list']()
    efforts = list(NS['EFFORTS'])
    return [{'id': m['id'], 'label': m['label'], 'efforts': efforts, 'isDefault': i == 0} for i, m in enumerate(ml.get('list') or [])]


def provider_models(prov):
    if prov == 'claude':
        return claude_models(), ''
    a = adapters().get(prov)
    if not a:
        return [], '没有这个提供方'
    try:
        d = a.models()
    except Exception as e:
        return [], str(e)
    if prov == 'codex':
        lst = [{'id': m['id'], 'label': m.get('label') or m['id'], 'efforts': m.get('efforts') or [], 'isDefault': bool(m.get('isDefault')),
                'defaultEffort': m.get('defaultEffort') or ''} for m in d.get('models') or [] if m.get('id')]
        lst.sort(key=lambda m: not m['isDefault'])  # 默认（推荐）模型排第一：T3 跨家委派默认用 models[0]
        return lst, d.get('error') or ''
    effs = [e['id'] for e in d.get('efforts') or [] if e.get('id')]
    dm = d.get('defaultModel') or ''
    lst = [{'id': m['id'], 'label': m.get('label') or m['id'], 'efforts': effs, 'isDefault': m['id'] == dm} for m in d.get('models') or [] if m.get('id')]
    lst.sort(key=lambda m: not m['isDefault'])
    return lst, d.get('error') or ''


def supported_modes(prov):
    if prov == 'claude':
        return list(RUNTIME_MODES)
    if prov == 'codex':
        return ['approval-required', 'auto-accept-edits', 'full-access']  # 工作台的 Codex 档位里没有 T3 的 auto（auto_review 审核员）
    a = adapters().get('agy')
    modes = []
    try:
        modes = [m['id'] for m in (a.models().get('modes') or [])] if a else []
    except Exception:
        modes = []
    out = ['approval-required', 'auto-accept-edits']
    if 'auto' in modes:
        out.append('auto')  # T3 antigravityRuntimeModes：会话里有 agy 自己的 Auto 才有 auto
    # 11-25 偏离 T3：父对话跳过确认时，用户要 agy 子任务也全自动。ACP 会话本身没有放开档，由工作台替用户点“允许”
    # （prov_acp.on_request 看对话的 autoAllow）。仍受 ceiling 管：父对话不是跳过确认，就到不了这一档。
    out.append('full-access')
    return out


def constraints(prov, pf=None):
    """T3 providerConstraints：空表示能接子任务。"""
    pf = pf or prefs()
    out = []
    if not pf['provs'].get(prov, False):
        out.append('设置里没有勾选这一家，不能派给它。')
        return out
    st = prov_status(prov)
    if not st.get('installed'):
        out.append(st.get('hint') or f'{LABEL[prov]} 没有安装。')
    elif not st.get('ok'):
        out.append(st.get('hint') or f'{LABEL[prov]} 还没登录。')
    return out


def quota_of(prov):
    """X2：返回 (拒绝的原因, 额度说明)。拿不到额度：(None, None)；QH 是空、ok:false、没有窗口都算拿不到。"""
    if QH is None:
        return None, None
    try:
        p = QH.get(prov)
        if not p or not p.get('ok') or not p.get('windows'):
            return None, None
        tt = QH.tightest(prov)
    except Exception as e:
        log('读额度失败', prov, repr(e))
        return None, None
    if not tt or not isinstance(tt.get('usedPercent'), (int, float)):
        return None, None
    pct = float(tt['usedPercent'])
    info = {'usedPercent': round(pct, 1), 'label': tt.get('label') or '', 'resetsAt': tt.get('resetsAt')}
    if pct >= 90:
        left = ''
        ra = tt.get('resetsAt')
        if isinstance(ra, (int, float)) and ra > now_ms():
            mins = (ra - now_ms()) / 60000
            left = f'，{math.ceil(mins / 60)} 小时后重置' if mins >= 60 else f'，{max(1, math.ceil(mins))} 分钟后重置'
        return f'{LABEL[prov]} {tt.get("label") or ""}额度已用 {round(pct)}%{left}。目标额度紧张，这次不委派；请告诉用户。', info
    return None, info


def parent_info(sid):
    """父对话：(提供方, 运行档, 交互档, 模型)。"""
    pv = NS.get('PV')
    if pv and sid in pv.CONVS:
        c = pv.CONVS[sid]
        mode = c.opts().get('mode') or ''
        a = pv.ADAPTERS.get(c.prov)
        model = (a.cur_model(c) if a else '') or ''
        if c.prov == 'codex':
            return 'codex', codex_rt(mode), 'default', model
        return c.prov, agy_rt(mode), 'plan' if mode == 'plan' else 'default', model
    r = NS['RUNNERS'].get(sid)
    # 回复中底栏常常认不出模式（记成 default）：先用稳定下来的模式（base_mode），再用屏幕上的，再用对话选项、CLI 默认
    mode = (getattr(r, 'base_mode', None) or (r.state.get('mode') if r else '') or '') if r else ''
    if mode in ('', 'default'):
        mode = NS['session_opts'](sid).get('mode') or (NS['cli_default_mode']() or '')
    model = ''
    try:
        model = NS['eff_model'](r.opts if r else NS['session_opts'](sid))
    except Exception:
        pass
    return 'claude', claude_rt(mode), 'plan' if (mode or '').lower() == 'plan mode' else 'default', model


def ceiling(parent_rt, pf=None):
    """子任务权限上限：§9 用户拍板默认“和父对话一样”，设置里可以调低；永远不高于父对话。"""
    pf = pf or prefs()
    cap = parent_rt if pf['perm'] == 'same' else pf['perm']
    return cap if RANK[cap] <= RANK[parent_rt] else parent_rt


def capabilities(sid, inp):
    pf = prefs()
    prov, rt, im, model = parent_info(sid)
    terms = [x for x in str(inp.get('search') or '').lower().split() if x]
    out = []
    for p in PROVS:
        cons = constraints(p, pf)
        models, err = provider_models(p) if not cons else ([], '')
        if err and not models:
            cons.append(err)

        def hit(m):
            s = (m['id'] + ' ' + m.get('label', '')).lower()
            return all(t in s for t in terms)
        cand = [m for m in models if hit(m)] if terms else models
        listed = cand[:LISTED_MODEL_LIMIT]
        ent = {'providerInstanceId': p, 'driverKind': DRIVER[p], 'displayName': LABEL[p],
               'models': [dict({'id': m['id']}, **({'label': m['label']} if m.get('label') and m['label'].lower() != m['id'].lower() else {}),
                               **({'options': [{'id': 'effort', 'values': m['efforts'], **({'default': m['defaultEffort']} if m.get('defaultEffort') else {})}]}
                                  if m.get('efforts') else {})) for m in listed],
               'runtimeModes': supported_modes(p), 'canRunChildTask': not cons, 'canRunCrossProviderChildTask': not cons,
               'constraints': cons}
        if len(cand) > len(listed):
            ent['omittedModels'] = len(cand) - len(listed)
        if not cons:
            refuse, q = quota_of(p)
            ent['quota'] = q or 'unknown'  # 第十七轮 M2：额度余量（拿不到写 unknown）
            if refuse:
                ent['constraints'] = [refuse]
                ent['canRunChildTask'] = ent['canRunCrossProviderChildTask'] = False
        out.append(ent)
    return {'parentThreadId': sid, 'inheritedProviderInstanceId': prov, 'inheritedModel': model or None, 'inheritedOptions': [],
            'runtimeMode': ceiling(rt, pf), 'interactionMode': im, 'providers': out,
            'features': {'appOwnedSubagents': True, 'asyncPolling': True, 'cancellation': True, 'batchThreadCreation': False,
                         'threadManagement': False, 'incrementalThreadRead': False, 'scheduledTasks': False, 'maxBatchThreads': 0}}


# ================================================================ delegatedTaskModes.ts
def resolve_runtime_mode(parent, requested, supported):
    inherited = requested in (None, '', 'inherit')
    resolved = parent if inherited else requested
    if resolved not in RANK:
        raise Fail('invalid_request', f'不认识的运行档 {resolved}。')
    if RANK[resolved] > RANK[parent]:
        raise Fail('runtime_mode_escalation_denied', f'子任务的运行档 {resolved} 比父对话的 {parent} 宽，不允许。')
    if not supported or resolved in supported:
        return resolved
    if not inherited:
        raise Fail('invalid_request', f'这一家没有运行档 {resolved}，可选：{", ".join(supported)}。')
    lower = sorted([m for m in supported if RANK[m] <= RANK[resolved]], key=lambda m: -RANK[m])
    return lower[0] if lower else 'approval-required'


def resolve_interaction_mode(parent, requested):
    resolved = parent if requested in (None, '', 'inherit') else requested
    if resolved not in ('default', 'plan'):
        raise Fail('invalid_request', f'不认识的交互档 {resolved}。')
    if (0 if resolved == 'plan' else 1) > (0 if parent == 'plan' else 1):
        raise Fail('interaction_mode_escalation_denied', f'子任务的交互档 {resolved} 比父对话的 {parent} 宽，不允许。')
    return resolved


def task_prompt(inp):
    role = inp.get('role')
    return inp['task'] if role in (None, 'general') else f'Act as the {role} sub-agent for this task.\n\n{inp["task"]}'


# ================================================================ delegate_task
def norm_target(raw):
    if isinstance(raw, str):  # T3：有的提供方把 target 整个当 JSON 文本传
        try:
            raw = json.loads(raw)
        except Exception:
            raise Fail('invalid_request', 'target 不是合法的 JSON。')
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise Fail('invalid_request', 'target 要是对象。')
    return raw


def norm_options(o):
    if o is None:
        return None
    if isinstance(o, dict):
        return {str(k): v for k, v in o.items()}
    if isinstance(o, list):
        return {str(x.get('id')): x.get('value') for x in o if isinstance(x, dict) and x.get('id')}
    raise Fail('invalid_request', 'target.options 格式不对。')


def resolve_target(sid, inp, pf):
    prov, rt, im, pmodel = parent_info(sid)
    tg = norm_target(inp.get('target'))
    want = tg.get('providerInstanceId')
    if not want and tg.get('driverKind'):
        want = next((k for k, v in DRIVER.items() if v == tg['driverKind'] or k == tg['driverKind']), None)
        if not want:
            raise Fail('provider_unavailable', f'没有 {tg["driverKind"]} 这一家。')
    want = want or prov
    if want not in PROVS:
        raise Fail('provider_unavailable', f'没有 {want} 这一家；可选 {", ".join(PROVS)}。')
    cons = constraints(want, pf)
    if cons:
        raise Fail('provider_unavailable', f'{LABEL[want]} 不能接子任务：{" ".join(cons)}')
    models, err = provider_models(want)
    model = tg.get('model')
    if model:
        if models and not any(m['id'] == model for m in models):
            ids = ', '.join(m['id'] for m in models[:LISTED_MODEL_LIMIT])
            raise Fail('model_unavailable', f'{LABEL[want]} 没有模型 {model}。可选：{ids}。')
    else:
        model = pmodel if want == prov and pmodel else (models[0]['id'] if models else '')  # T3：同一家继承父对话的模型，别家用它的第一个（推荐）模型
    opts = norm_options(tg.get('options'))
    effort = None
    if opts:
        bad = [k for k in opts if k != 'effort']
        if bad:
            raise Fail('invalid_request', f'不认识的选项 {", ".join(bad)}；只支持 effort。')
        effort = str(opts.get('effort') or '') or None
        m = next((x for x in models if x['id'] == model), None)
        if effort and m and m.get('efforts') and effort not in m['efforts']:
            raise Fail('invalid_request', f'模型 {model} 的 effort 只能是：{", ".join(m["efforts"])}。')
    return want, model, effort, rt, im


def reserve(sid, pf, origin):
    """检查并占位（复核 M-1）：数上限、占一个名额、记频率在同一把锁里一次做完，同时到达的委派不会一起越过上限。
    返回的票据交给 release()：建对话成功后释放占位（任务已经在 TASKS 里算数），失败时连频率记录一起退回。"""
    with LOCK:
        mine = sum(1 for t in TASKS.values() if live(t) and t['parent'] == sid) + RESV.get(sid, 0)
        allr = sum(1 for t in TASKS.values() if live(t)) + sum(RESV.values())
        if mine >= pf['maxPer']:
            raise Fail('limit_exceeded', f'这条对话已经有 {mine} 个子任务在跑（上限 {pf["maxPer"]} 个），等它们结束或停掉一个再派。')
        if allr >= pf['maxAll']:
            raise Fail('limit_exceeded', f'工作台已经有 {allr} 个子任务在跑（上限 {pf["maxAll"]} 个），等它们结束再派。')
        stamp = None
        if origin == 'model':
            ts = [x for x in RATE.get(sid, []) if time.time() - x < RATE_WIN]
            RATE[sid] = ts
            if len(ts) >= RATE_MAX:
                raise Fail('limit_exceeded', f'这条对话一小时内已经委派了 {len(ts)} 次（上限 {RATE_MAX} 次），过一会儿再派。')
            stamp = time.time()
            ts.append(stamp)
        RESV[sid] = RESV.get(sid, 0) + 1
        return (sid, stamp)


def release(ticket, ok):
    sid, stamp = ticket
    with LOCK:
        n = RESV.get(sid, 0) - 1
        if n > 0:
            RESV[sid] = n
        else:
            RESV.pop(sid, None)
        if not ok and stamp is not None:
            try:
                RATE.get(sid, []).remove(stamp)
            except ValueError:
                pass


def delegate(sid, inp, origin='model', anchor=None):
    pf = prefs()
    if origin == 'model' and not pf['on']:
        raise Fail('capability_denied', '协作功能在工作台设置里关着，不能委派。请告诉用户：要用的话先在“设置 → 协作”里打开。')
    if sid in CHILD:
        raise Fail('capability_denied', '子任务不能再委派子任务（深度固定一层）。')
    task = inp.get('task')
    if not isinstance(task, str) or not task.strip():
        raise Fail('invalid_request', 'task 不能为空。')
    if len(task) > 120000:
        raise Fail('invalid_request', 'task 太长（最多 12 万字）。')
    if inp.get('isolation') not in (None, 'shared'):
        raise Fail('invalid_request', "这一版只支持 isolation:'shared'（子任务和父对话用同一个文件夹）。")
    crid = inp.get('clientRequestId')
    if crid:
        with LOCK:
            tid = REQKEY.get((sid, str(crid)))
        if tid and tid in TASKS:
            return read_task(sid, tid, ack=True)  # T3：同一个 clientRequestId 重试，返回已接受的那个任务
    prov, model, effort, parent_rt, parent_im = resolve_target(sid, inp, pf)
    refuse, qinfo = quota_of(prov)
    if refuse:
        raise Fail('quota_exhausted', refuse)
    cap = ceiling(parent_rt, pf)  # §9：子任务权限和父对话一样（设置里可以调低），T3 resolveRuntimeMode 按这个上限收窄
    req = inp.get('runtimeMode')
    if req in RANK and RANK[cap] < RANK[req] <= RANK[parent_rt]:
        raise Fail('runtime_mode_escalation_denied', f'工作台设置里子任务权限最高是“{MODE_ZH[cap]}”，不能用 {req}。')
    rt = resolve_runtime_mode(cap, req, supported_modes(prov))
    im = resolve_interaction_mode(parent_im, inp.get('interactionMode'))
    ptitle = (inp.get('title') or '').strip()[:512] or ' '.join(task.split())[:40]
    t = {'id': 'task_' + uuid.uuid4().hex[:16], 'parent': sid, 'parentProv': parent_info(sid)[0], 'child': '', 'childProv': prov,
         'model': model, 'effort': effort, 'title': ptitle, 'task': task_prompt(inp), 'role': inp.get('role') or 'general',
         'runtimeMode': rt, 'interactionMode': im, 'origin': origin, 'createdAt': now_ms(), 'status': 'starting',
         'x3': None, 'delivery': 'none', 'quotaUnknown': qinfo is None, 'clientRequestId': str(crid) if crid else None,
         'anchor': anchor}
    ticket = reserve(sid, pf, origin)  # 检查上限、占位、记频率一次做完，紧挨着建对话（复核 M-1）
    ok = False
    try:
        spawn(t)
        ok = True
    except Fail:
        raise
    except Exception as e:
        log('建子对话失败', repr(e))
        raise Fail('orchestration_error', '没能建出子对话：' + str(e))
    finally:
        release(ticket, ok)
    if inp.get('mode') == 'wait':
        return await_task(sid, t['id'], inp.get('timeoutMs'))
    return read_task(sid, t['id'])


def child_mode(t):
    prov, rt = t['childProv'], t['runtimeMode']
    if t['origin'] == 'manual':
        return REVIEW_MODE[prov]
    if t.get('interactionMode') == 'plan' and prov == 'claude':
        return 'plan mode'
    if t.get('interactionMode') == 'plan' and prov == 'agy':
        return 'plan'
    return MODE_OF[prov].get(rt) or MODE_OF[prov]['approval-required']


def spawn(t):
    """建子对话：完全复用现有建对话流程。Claude＝ensure_runner（伪终端里的交互式 claude，同一启动函数），
    Codex、Antigravity＝P 线的 prov-new；任务作为第一条消息照常发出。子对话不挂 cwb（CHILD 里有它，claude_args 原样返回）。"""
    cwd = parent_cwd(t['parent'])
    mode = child_mode(t)
    if t['childProv'] == 'claude':
        sid = str(uuid.uuid4())
        with LOCK:
            CHILD[sid] = t['id']
            t['child'] = sid
            TASKS[t['id']] = t
            if t.get('clientRequestId'):
                REQKEY[(t['parent'], t['clientRequestId'])] = t['id']
        opts = {'mode': mode}
        if t.get('model'):
            opts['model'] = t['model']
        if t.get('effort'):
            opts['effort'] = t['effort']
        x3_task(t)
        set_status(t, 'starting')
        try:
            NS['ensure_runner'](sid, cwd=cwd, new=True, opts=opts)
            NS['API']['rename']({'sid': sid, 'name': t['title'][:80]})
            res = NS['API']['send']({'sid': sid, 'text': t['task']})
            if isinstance(res, dict) and res.get('error'):
                raise ValueError(res['error'])
        except Exception as e:
            t['summary'] = '没能启动子对话：' + str(e)
            set_status(t, 'failed')
            raise
    else:
        pv = NS['PV']
        d = pv.api_prov_new({'prov': t['childProv'], 'cwd': cwd,
                             'opts': {k: v for k, v in (('model', t.get('model')), ('effort', t.get('effort')), ('mode', mode)) if v}})
        sid = d['sid']
        with LOCK:
            CHILD[sid] = t['id']
            t['child'] = sid
            TASKS[t['id']] = t
            if t.get('clientRequestId'):
                REQKEY[(t['parent'], t['clientRequestId'])] = t['id']
        c = pv.CONVS[sid]
        c.meta.update(parent=t['parent'], taskId=t['id'], title=t['title'][:80])  # 子对话元数据记 parent、taskId（第一次落盘时一起写）
        if t['childProv'] == 'agy' and t['runtimeMode'] == 'full-access' and t['origin'] != 'manual' and mode != 'plan':
            c.meta['autoAllow'] = True  # 11-25：agy 的所有确认由工作台直接允许，不弹卡（规划档、手动复核不放开）
        x3_task(t)
        set_status(t, 'starting')
        try:
            NS['API']['send']({'sid': sid, 'text': t['task']})
        except Exception as e:
            t['summary'] = '没能启动子对话：' + str(e)
            set_status(t, 'failed')
            raise
    log('委派', t['id'], LABEL[t['parentProv']], '→', LABEL[t['childProv']], t.get('model') or '默认模型', t['runtimeMode'], t['origin'])


def parent_cwd(sid):
    pv = NS.get('PV')
    if pv and sid in pv.CONVS:
        return pv.CONVS[sid].cwd
    r = NS['RUNNERS'].get(sid)
    if r:
        return r.cwd
    info = NS['INDEX'].get(sid)
    if info and info.get('cwd'):
        return NS['remap_cwd'](info['cwd'])
    if sid in NS['NEW_SESS']:
        return NS['NEW_SESS'][sid]
    raise Fail('orchestration_error', '找不到父对话的文件夹。')


# ================================================================ readTask / task_status / task_cancel
def pending_approval(t):
    """T3 pendingTaskApproval：最早的一张授权卡（父智能体只看得到，批不了）。"""
    best = None
    for rid, ent in list(NS['PERMS'].items()):
        req = ent.get('req') or {}
        if req.get('sid') != t['child'] or req.get('tool') == 'AskUserQuestion':
            continue
        tool = req.get('tool') or ''
        kind = 'command' if tool == 'Bash' else 'file-change' if tool in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit') else \
            'mcp-elicitation' if tool.startswith('mcp__') else 'permission'
        best = best or {'requestId': rid, 'kind': kind, 'runId': t['child'] + ':1'}
    return best


def has_question(t):
    return any((e.get('req') or {}).get('sid') == t['child'] and (e.get('req') or {}).get('tool') == 'AskUserQuestion'
               for e in list(NS['PERMS'].values()))


def read_task(sid, tid, timed_out=False, ack=False):
    t = TASKS.get(tid)
    if not t or t['parent'] != sid:
        raise Fail('task_not_found', f'委派任务 {tid} 不属于这条对话。')
    st = t['status']
    term = st in TERMINAL
    res = {'taskId': t['id'], 'childThreadId': t['child'], 'childRunId': None, 'childNodeId': t['id'], 'status': st,
           'workState': 'result_available' if term else 'working', 'hasPendingChildRuns': False,
           'latestTerminalRunId': None, 'latestTerminalStatus': None, 'latestTerminalSummary': None,
           'latestTerminalResultContextTransferId': None, 'providerInstanceId': t['childProv'], 'model': t.get('model') or None,
           'executedModel': t.get('executedModel'), 'options': [{'id': 'effort', 'value': t['effort']}] if t.get('effort') else [],
           'summary': (t.get('summary') if term else None), 'resultContextTransferId': None,
           'hasPendingQuestion': (not term) and has_question(t),
           'pendingApproval': None if term or t.get('delivery') == 'disposed' else pending_approval(t), 'waitTimedOut': timed_out,
           'runtimeMode': t['runtimeMode']}
    if t.get('quotaUnknown'):
        res['quota'] = '额度未知'  # X2：拿不到额度照常委派，工具返回里写明
    if t.get('reason') == 'timeout':
        res['note'] = '超过单个子任务的最长时间，工作台已自动停止。'
    if not (term or res['hasPendingQuestion'] or res['pendingApproval']) and t.get('delivery') != 'disposed':
        res['next'] = ORCHESTRATOR_MCP_RUNNING_TASK_NEXT
    if ack and term and t.get('delivery') not in ('acknowledged', 'disposed'):
        set_delivery(t, 'acknowledged', 'status')  # T3：读到结果就记为“已送达”，之后不再叫醒
    return res


def await_task(sid, tid, timeout_ms):
    """T3 awaitTask：mode=wait 最多等 45 秒；等到结束、提问或授权就返回，超时不停子任务。"""
    try:
        ms = float(timeout_ms) if timeout_ms is not None else DEFAULT_WAIT_TIMEOUT_MS
    except (TypeError, ValueError):
        ms = DEFAULT_WAIT_TIMEOUT_MS
    end = time.time() + min(MAX_WAIT_TIMEOUT_MS, max(1, ms)) / 1000
    t = TASKS[tid]
    with COND:
        while time.time() < end and live(t) and not pending_approval(t) and not has_question(t):
            COND.wait(min(1.0, max(0.05, end - time.time())))
    done = not live(t)
    return read_task(sid, tid, timed_out=not done and not pending_approval(t) and not has_question(t), ack=done)


def stop_child(t):
    """停子对话这一轮（和用户点“停止”一样：Claude 是 Esc，两家是协议里的中断）。Claude 子对话还没开始这一轮（CLI 在启动）时
    直接结束这个 CLI，免得排着的任务随后又发出去（对话记录不删，之后还能接着聊）。"""
    try:
        if t['childProv'] == 'claude' and t['status'] == 'starting':
            NS['API']['stop']({'sid': t['child'], '_cwb': True})
            return
        NS['API']['interrupt']({'sid': t['child'], '_cwb': True})
    except Exception as e:
        log('停子任务失败', t['id'], repr(e))


def cancel(t, why):
    """why：cancel（智能体 task_cancel，丢掉交回）、cascade（用户停了父对话，丢掉交回）、user（用户在卡片上点停止，照常交回）、
    timeout（到时自动停，照常交回）。"""
    if not live(t):
        return False
    t['_cancel'] = why
    stop_child(t)
    t['summary'] = partial_text(t) or t.get('summary')
    if why == 'timeout':
        t['summary'] = ((t.get('summary') or '') + f'\n\n（超过最长时间 {prefs()["maxMins"]} 分钟，工作台已自动停止）').strip()
    if why in ('cancel', 'cascade'):
        with LOCK:
            t['delivery'] = 'disposed'
    set_status(t, 'cancelled' if why in ('cancel', 'cascade') else 'interrupted', why)
    return True


def cancel_tool(sid, inp):
    tid = inp.get('taskId') or ''
    t = TASKS.get(tid)
    if not t or t['parent'] != sid:
        raise Fail('task_not_found', f'委派任务 {tid} 不属于这条对话。')
    if not live(t):
        return {'taskId': tid, 'status': t['status']}
    prov, rt, _, _ = parent_info(sid)
    cur_rt = child_rt(t)
    if cur_rt and RANK.get(cur_rt, 0) > RANK[rt]:
        # T3 assertStoppable：用户已把子对话调得比父对话宽，父智能体不能再动它
        raise Fail('runtime_mode_escalation_denied', '用户已经把这个子任务的权限调得比你宽，你不能停它；请告诉用户。')
    cancel(t, 'cancel')
    return {'taskId': tid, 'status': 'cancel_requested'}


def child_rt(t):
    pv = NS.get('PV')
    if t['childProv'] == 'claude':
        r = NS['RUNNERS'].get(t['child'])
        return claude_rt((r.state.get('mode') if r else '') or NS['session_opts'](t['child']).get('mode') or '')
    c = pv.CONVS.get(t['child']) if pv else None
    if not c:
        return None
    m = c.opts().get('mode') or ''
    return codex_rt(m) if t['childProv'] == 'codex' else agy_rt(m)


# ================================================================ 子对话的结果
def claude_turn_text(sid, final=True):
    """子对话第一轮的最后一条回复（读会话记录）。"""
    try:
        tr = NS['get_transcript'](sid)
        tr.poll()
    except Exception:
        return None, True
    with tr.lock:
        items = list(tr.conv.items)
        open_ = tr.conv.open
    users = [i for i, it in enumerate(items) if it.get('k') == 'user' and not it.get('mid')]
    if not users:
        return None, open_
    a = users[0]
    b = users[1] if len(users) > 1 else len(items)
    texts = [it.get('text') or '' for it in items[a + 1:b] if it.get('k') == 'text' and (it.get('text') or '').strip()]
    errs = [it.get('text') or '' for it in items[a + 1:b] if it.get('k') == 'error']
    return (texts[-1] if texts else ('\n'.join(errs) if errs else None)), open_


def pconv_turn_text(c):
    items = list(c.items)
    users = [i for i, it in enumerate(items) if it.get('k') == 'user' and not it.get('mid')]
    if not users:
        return None
    a = users[0]
    b = users[1] if len(users) > 1 else len(items)
    texts = [it.get('text') or '' for it in items[a + 1:b] if it.get('k') == 'text' and (it.get('text') or '').strip()]
    errs = [it.get('text') or '' for it in items[a + 1:b] if it.get('k') == 'error']
    return texts[-1] if texts else ('\n'.join(errs) if errs else None)


def partial_text(t):
    if t['childProv'] == 'claude':
        return claude_turn_text(t['child'])[0]
    c = NS['PV'].CONVS.get(t['child'])
    return pconv_turn_text(c) if c else None


def claude_usage(sid):
    """子对话（Claude）这一段的 token 和费用：读它自己的会话记录。"""
    try:
        fp = NS['find_transcript'](sid)
    except Exception:
        fp = None
    if not fp:
        return None
    tot = {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}
    cost, model = 0.0, ''
    seen = set()
    try:
        with open(fp, encoding='utf-8') as f:
            for line in f:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                m = r.get('message') or {}
                u = m.get('usage') or {}
                key = m.get('id') or r.get('uuid')
                if not u or key in seen:
                    continue
                seen.add(key)
                model = m.get('model') or model
                i, o = int(u.get('input_tokens') or 0), int(u.get('output_tokens') or 0)
                cr, cw = int(u.get('cache_read_input_tokens') or 0), int(u.get('cache_creation_input_tokens') or 0)
                tot['input'] += i
                tot['output'] += o
                tot['cacheRead'] += cr
                tot['cacheWrite'] += cw
                try:
                    cost += NS['US'].cost_of(model, i, o, cr, cw) or 0
                except Exception:
                    pass
    except OSError:
        return None
    return dict(tot, cost=round(cost, 4) if cost else None, model=model)


def finish_claude(t, ev, body):
    """Claude 子对话第一轮结束（Stop / StopFailure 钩子）：读记录里的最后一条回复当结果。Stop 后 1～2 秒才写完记录，最多等 10 秒。"""
    text, open_ = None, True
    for _ in range(40):
        text, open_ = claude_turn_text(t['child'])
        if text and not open_:
            break
        time.sleep(0.25)
    if not live(t):
        return
    if ev == 'fail':
        msg = str(body.get('last_assistant_message') or body.get('error') or '接口出错')
        t['summary'] = text or msg
        t['executedModel'] = None
        t['usage'] = claude_usage(t['child'])
        set_status(t, 'failed')
    else:
        t['summary'] = text or str(body.get('last_assistant_message') or '') or '（子任务没有文字回复）'
        u = claude_usage(t['child'])
        t['usage'] = u
        t['executedModel'] = (u or {}).get('model') or None
        set_status(t, 'completed')


def on_pconv_end(c, reason):
    """Codex / Antigravity 子对话一轮结束（包了 PConv.end_turn）。只认第一轮；之后用户接着聊不再回报。"""
    tid = CHILD.get(c.sid)
    t = TASKS.get(tid) if tid else None
    if not t or not live(t):
        return
    t['summary'] = pconv_turn_text(c) or ('（子任务没有文字回复）' if reason == 'done' else t.get('summary'))
    lu = c.last_usage or {}
    if lu.get('usage') or lu.get('cost'):
        u = dict(lu.get('usage') or {})
        if isinstance(lu.get('cost'), dict):
            u['cost'] = lu['cost'].get('amount')
        t['usage'] = u
    t['executedModel'] = lu.get('model') or None
    if reason == 'done':
        set_status(t, 'completed')
    elif reason == 'error':
        set_status(t, 'failed')
    else:
        set_status(t, 'interrupted', t.get('_cancel') or 'user')


# ================================================================ 叫醒（交回父对话）
def wake_detail(ts):
    """T3 delegatedCompletionWakeDetail：结果内联，合计超过 16000 字节的让智能体自己用 task_status 读。"""
    remaining = DELEGATED_RESULT_INLINE_BYTES
    out = []
    for t in ts:
        st = t['status']
        label = f'Delegated task {t["id"]} ({t["title"]}) in child thread {t["child"]} [{st}]'
        if t.get('reason') == 'timeout':
            label += ' (stopped by Claude Workbench: time limit reached)'
        if t.get('summary'):
            detail = f'{label}:\n{t["summary"]}'
            n = len(detail.encode('utf-8'))
            if n <= remaining:
                remaining -= n
                out.append(detail)
                continue
        out.append(f'{label}. Use task_status with taskId {t["id"]} to read the result.')
    return '\n\n'.join(out)


def hook_task_id(body):
    tr = body.get('tool_response')
    texts = []
    if isinstance(tr, list):
        texts = [x.get('text') or '' for x in tr if isinstance(x, dict)]
    elif isinstance(tr, dict):
        if tr.get('taskId'):
            return tr['taskId']
        texts = [x.get('text') or '' for x in tr.get('content') or [] if isinstance(x, dict)]
    elif isinstance(tr, str):
        texts = [tr]
    for s in texts:
        try:
            d = json.loads(s)
            if isinstance(d, dict) and d.get('taskId'):
                return d['taskId']
        except Exception:
            m = re.search(r'"taskId"\s*:\s*"([\w-]+)"', s)
            if m:
                return m.group(1)
    return None


def task_wait(sid, body, gen):
    """/hook/task-wait：hook_wait.py 的长轮询。返回 {deliver, text} / {skip} / {wait}。"""
    try:
        r, by_gen = NS['hook_runner'](sid, gen)
        psid = r.sid if r else NS['resolve_sid'](sid)
    except Exception:
        r, by_gen, psid = None, False, sid
    if by_gen and not r:
        return {'skip': True}  # 旧代次的钩子（那个 CLI 已经不在）：不交回（复核 M-8）
    tid = hook_task_id(body if isinstance(body, dict) else {})
    t = TASKS.get(tid) if tid else None
    if not t or t['parent'] != psid or t['origin'] != 'model':
        return {'skip': True}
    end = time.time() + WAKE_POLL
    with COND:
        t['_waiter'] = time.time()
        while live(t) and time.time() < end:
            COND.wait(min(1.0, max(0.05, end - time.time())))
            t['_waiter'] = time.time()
    if live(t):
        return {'wait': True}
    if t.get('delivery') in ('acknowledged', 'disposed'):
        return {'skip': True}
    if not prefs()['autoReturn']:
        set_delivery(t, 'pending')
        return {'skip': True}
    # 两步：先把交回内容给钩子，钩子回一次 task-ack 才记“已送达”。钩子进程在长轮询期间被结束（父 CLI 重启）时，
    # 服务端这边的长轮询还会把内容“交”给一个已经断开的连接；没有 ack 就不算送达，过 LOST_AFTER 秒记“待交回”，卡片上出按钮。
    with LOCK:
        t['_offered'] = t['_waiter'] = time.time()
    log('叫醒父对话（钩子交回）', t['id'], psid[:8])
    return {'deliver': True, 'text': wake_detail([t])}


def task_ack(sid, body, gen):
    """/hook/task-ack：hook_wait.py 拿到交回内容、写标准错误之前回报一次，这时才记已送达（X3 delivered:true）。"""
    try:
        r, by_gen = NS['hook_runner'](sid, gen)
        psid = r.sid if r else NS['resolve_sid'](sid)
    except Exception:
        r, by_gen, psid = None, False, sid
    if by_gen and not r:
        return {'ok': False}  # 旧代次的钩子不算送达（复核 M-8）
    tid = hook_task_id(body if isinstance(body, dict) else {})
    t = TASKS.get(tid) if tid else None
    if not t or t['parent'] != psid or live(t) or t.get('delivery') in ('acknowledged', 'disposed'):
        return {'ok': False}
    set_delivery(t, 'acknowledged', 'hook')
    return {'ok': True}


def codex_wake(c, ts):
    """Codex 父对话：空闲时新起一轮把结果交回（turn_end 写 trigger:"cwb_task"），时间线上是灰色系统行。"""
    pv = NS['PV']
    text = wake_detail(ts)
    note = '子任务已完成，结果已交回' if len(ts) == 1 else f'{len(ts)} 个子任务已完成，结果已交回'
    for t in ts:
        set_delivery(t, 'acknowledged', 'turn')
    c.claim()
    threading.Thread(target=pv.start_turn, args=(c, text, []),
                     kwargs={'wake': {'note': note, 'trigger': 'cwb_task', 'tag': '子任务'}}, daemon=True).start()
    log('叫醒父对话（Codex 新起一轮）', ','.join(t['id'] for t in ts), c.sid[:8])


def deliver_text(t):
    who = LABEL.get(t['childProv'], '')
    head = f'{who} 复核了这一轮改动，意见如下：' if t['origin'] == 'manual' else f'子任务《{t["title"]}》（{who}）的结果：'
    s = t.get('summary') or '（没有文字回复）'
    return f'{head}\n\n{s}'


def api_deliver(b):
    """委派卡上的“把结果交给 X”：叫醒丢失、关了自动交回或手动复核时，用户点了才发。按现有发消息流程发出，算用户发的。"""
    t = TASKS.get(b.get('id') or '')
    if not t:
        raise ValueError('找不到这个子任务')
    if live(t):
        raise ValueError('子任务还没结束')
    if t.get('delivery') == 'acknowledged':
        raise ValueError('结果已经交回过了')
    NS['API']['send']({'sid': t['parent'], 'text': deliver_text(t)})
    set_delivery(t, 'acknowledged', 'button')
    return {'ok': True, 'task': pub(t)}


# ================================================================ 后台循环：状态、超时、叫醒丢失、Codex 交回
def loop():
    while True:
        time.sleep(1.0)
        try:
            tick()
        except Exception as e:
            log('协作后台出错', repr(e))


def tick():
    pf = prefs()
    pv = NS.get('PV')
    now = time.time()
    wakes = {}
    for t in list(TASKS.values()):
        if live(t):
            track(t, pf, now)
            continue
        if t.get('delivery') not in (None, 'none'):
            continue
        if t['origin'] == 'manual':
            set_delivery(t, 'pending')  # 手动复核：不自动交回，卡片上出按钮
            continue
        if t['parentProv'] == 'claude':
            term_at = t.setdefault('_term_at', now)
            last = max(t.get('_waiter') or 0, term_at)
            if not pf['autoReturn'] or now - last > LOST_AFTER:
                set_delivery(t, 'pending')  # 钩子没来取（父 CLI 重启杀掉了钩子进程）：等用户点按钮
                log('叫醒丢失，等用户点“交回”', t['id'])
        elif t['parentProv'] == 'codex' and pv:
            if not pf['autoReturn']:
                set_delivery(t, 'pending')
                continue
            c = pv.CONVS.get(t['parent'])
            if not c:
                set_delivery(t, 'pending')
                continue
            if not c.busy and not c.queue and not c.attention():
                wakes.setdefault(t['parent'], []).append(t)
        else:
            set_delivery(t, 'pending')
    for psid, ts in wakes.items():
        c = pv.CONVS.get(psid)
        if c and not c.busy:
            codex_wake(c, ts)


def track(t, pf, now):
    """跟踪进行中的子任务：开始、等确认、超时自动停；Claude 子对话的 CLI 没了或这一轮被中断（没有 Stop 钩子）也在这里收尾。"""
    if now * 1000 - t['createdAt'] > pf['maxMins'] * MINUTE * 1000:
        log('子任务超时，自动停止', t['id'])
        cancel(t, 'timeout')
        return
    att = False
    busy = False
    if t['childProv'] == 'claude':
        r = NS['RUNNERS'].get(t['child'])
        if not r or not r.state.get('alive'):
            if t.get('_seen_busy') or now * 1000 - t['createdAt'] > 60000:
                t['summary'] = partial_text(t) or t.get('summary')
                set_status(t, 'interrupted', 'exit')
            return
        att, busy = r.attention(), bool(r.turn)
        if busy:
            t['_seen_busy'] = True
            t.pop('_idle_since', None)
        elif t.get('_seen_busy') and not t.get('_finishing'):
            s = t.setdefault('_idle_since', now)
            if now - s > 6:  # 这一轮结束了却没有 Stop 钩子（用户按了停止）：按中断收尾
                t['summary'] = partial_text(t) or t.get('summary')
                set_status(t, 'interrupted', t.get('_cancel') or 'user')
                return
    else:
        c = NS['PV'].CONVS.get(t['child'])
        if not c:
            set_status(t, 'interrupted', 'gone')
            return
        att, busy = c.attention(), bool(c.busy)
    t['question'] = has_question(t)
    if att:
        set_status(t, 'waiting')
    elif busy:
        set_status(t, 'running')


# ================================================================ /mcp（MCP streamable HTTP，JSON 应答）
def http(h, method):
    """server.py 的 do_GET / do_POST 第二行调用：不是 /mcp 返回 False。只认本机来的请求＋对话令牌；远程 Host 在 remote.gate 已经 403。"""
    path = h.path.split('?', 1)[0]
    if path != '/mcp':
        return False
    if (h.client_address or ('',))[0] not in ('127.0.0.1', '::1') or not h._host_ok():
        h._json(403, {})
        return True
    if method != 'POST':
        h.send_response(405)
        h.send_header('Allow', 'POST')
        h.send_header('Content-Length', '0')
        h.end_headers()
        return True
    auth = h.headers.get('Authorization') or ''
    sid = caller_of(auth[7:].strip()) if auth.startswith('Bearer ') else None
    try:
        n = int(h.headers.get('Content-Length') or 0)
        if n < 0 or n > 16 * 1024 * 1024:
            raise ValueError(n)
    except ValueError:
        h.close_connection = True  # 请求体长度不对：回 400（复核 M-6）
        h._json(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Bad Content-Length'}})
        return True
    raw = h.rfile.read(n) if n else b''
    if not sid:
        h._json(401, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32001, 'message': '令牌不对'}})
        return True
    try:
        msg = json.loads(raw or b'null')
    except Exception:
        h._json(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}})
        return True
    if isinstance(msg, list):
        out = [x for x in (rpc(sid, m) for m in msg) if x is not None]
        if not out:
            h.send_response(202)
            h.send_header('Content-Length', '0')
            h.end_headers()
        else:
            h._json(200, out)
        return True
    res = rpc(sid, msg)
    if res is None:
        h.send_response(202)
        h.send_header('Content-Length', '0')
        h.end_headers()
    else:
        h._json(200, res)
    return True


def rpc(sid, m):
    if not isinstance(m, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    mid, meth, params = m.get('id'), m.get('method'), m.get('params') or {}
    if mid is None or 'method' not in m:
        return None  # 通知、对方的回应：不回
    if meth == 'initialize':
        v = params.get('protocolVersion')
        return {'jsonrpc': '2.0', 'id': mid, 'result': {
            'protocolVersion': v if v in PROTO_VERSIONS else PROTO_VERSIONS[1], 'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER, 'title': 'Claude 工作台协作', 'version': '1.0'}}}
    if meth == 'ping':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {}}
    if meth == 'tools/list':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': TOOLS}}
    if meth == 'tools/call':
        name, args = params.get('name'), params.get('arguments') or {}
        return {'jsonrpc': '2.0', 'id': mid, 'result': call_tool(sid, name, args if isinstance(args, dict) else {})}
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'Method not found: {meth}'}}


def call_tool(sid, name, args):
    try:
        if sid == '__ua__':
            raise Fail('capability_denied', '自检用的令牌不能调用工具。')
        if name == 'orchestrator_capabilities':
            if not prefs()['on']:
                raise Fail('capability_denied', '协作功能在工作台设置里关着。')
            out = capabilities(sid, args)
        elif name == 'delegate_task':
            out = delegate(sid, args, 'model')
        elif name == 'task_status':
            out = read_task(sid, str(args.get('taskId') or ''), ack=True)
        elif name == 'task_cancel':
            out = cancel_tool(sid, args)
        else:
            raise Fail('invalid_request', f'没有工具 {name}')
        return {'content': [{'type': 'text', 'text': json.dumps(out, ensure_ascii=False)}], 'structuredContent': out, 'isError': False}
    except Fail as e:
        f = {'code': e.code, 'message': e.message}
        log('工具返回失败', name, e.code, e.message[:80])
        return {'content': [{'type': 'text', 'text': json.dumps(f, ensure_ascii=False)}], 'structuredContent': f, 'isError': True}
    except Exception as e:
        log('工具出错', name, repr(e))
        f = {'code': 'orchestration_error', 'message': str(e)}
        return {'content': [{'type': 'text', 'text': json.dumps(f, ensure_ascii=False)}], 'structuredContent': f, 'isError': True}


# ================================================================ Claude 父对话：启动参数（只在协作开着时加三样）
def is_child(sid):
    if sid in CHILD:
        return True
    try:
        for old, (new, _) in list(NS['ALIAS'].items()):
            if old in CHILD and NS['resolve_sid'](old) == sid:
                return True
    except Exception:
        pass
    return False


def wake_hook():
    return {'matcher': 'mcp__cwb__delegate_task', 'hooks': [{
        'type': 'command', 'command': f'{NS["shlex"].quote(NS["PY"])} -I {NS["shlex"].quote(NS["HOOK"])} wait-task',
        'timeout': int(prefs()['maxMins'] * MINUTE) + 600, 'asyncRewake': True}]}


ARGS_ON = {}  # 对话 -> 这个 CLI 启动时挂没挂 cwb（预热的 CLI 复用前要和现在的开关对上，复核 M-2）


def claude_args(sid, args):
    """协作关着、或这是子对话：原样返回同一个列表（和第十六轮逐字一样）。开着：
    1. --settings 里多一个 PostToolUse 钩子（asyncRewake）；2. 说明并进已有的 --append-system-prompt（没有就加一个）；3. --mcp-config <文件>。
    中途出任何异常都原样返回同一个列表（复核 M-5）。"""
    try:
        out = _claude_args(sid, args)
    except Exception as e:
        log('准备协作启动参数失败，按不挂处理', repr(e))
        out = args
    ARGS_ON[sid] = out is not args
    return out


def _claude_args(sid, args):
    if not prefs()['on'] or is_child(sid):
        return args
    tok = token_for(sid)
    a = list(args)
    try:
        i = a.index('--settings')
        s = json.loads(a[i + 1])
        s.setdefault('hooks', {}).setdefault('PostToolUse', []).append(wake_hook())
        a[i + 1] = json.dumps(s, ensure_ascii=False)
    except (ValueError, IndexError):
        a += ['--settings', json.dumps({'hooks': {'PostToolUse': [wake_hook()]}}, ensure_ascii=False)]
    if '--append-system-prompt' in a:
        j = a.index('--append-system-prompt')
        a[j + 1] = a[j + 1] + '\n\n' + instructions('claude')
    else:
        a += ['--append-system-prompt', instructions('claude')]
    a += ['--mcp-config', mcp_path(sid)]
    log('协作开着，挂 cwb', sid[:8], tok[:8] + '…')
    return a


def codex_thread(c, params):
    """Codex 父对话：thread/start、thread/resume 的 config 用点号键 mcp_servers.cwb（T3 的写法，只盖自己这一项，
    用户自己的 MCP 不受影响），说明放 developerInstructions（本机 Codex 0.171 的 turn/start 没有 T3 用的 additionalContext）。"""
    if not prefs()['on'] or is_child(c.sid):
        return params
    tok = token_for(c.sid)
    p = dict(params)
    cfg = dict(p.get('config') or {})
    cfg['mcp_servers.' + SERVER] = {'url': f'http://127.0.0.1:{NS["PORT"]}/mcp', 'tool_timeout_sec': CODEX_MCP_TOOL_TIMEOUT_SEC,
                                    'http_headers': {'Authorization': 'Bearer ' + tok}}
    p['config'] = cfg
    base = p.get('developerInstructions') or codex_user_dev_instructions()
    # thread/start 的 developerInstructions 会盖掉 config.toml 的 developer_instructions：先放用户自己的，再接我们的（复核 M-3）
    p['developerInstructions'] = (base.rstrip() + '\n\n' if base else '') + instructions('codex')
    return p


def codex_user_dev_instructions():
    """用户 config.toml 顶层的 developer_instructions（只读；读不出来当没有）。"""
    home = os.environ.get('CWB_CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')
    try:
        import tomllib
        with open(os.path.join(home, 'config.toml'), 'rb') as f:
            v = tomllib.load(f).get('developer_instructions')
        return v if isinstance(v, str) else ''
    except Exception:
        return ''


def codex_elicitation(adapter, r, rid, p, c):
    """Codex 调 MCP 工具要授权（mcpServer/elicitation/request）：T3 的做法是出授权卡，完全放开档直接接受。"""
    if (p.get('mode') or 'form') not in ('form', 'openai/form', 'openaiForm'):
        return False
    meta = p.get('_meta') or {}
    if c.opts().get('mode') == 'full':
        r.respond(rid, {'action': 'accept'})
        return True
    tool = str(meta.get('tool_name') or meta.get('toolName') or '')
    title = f'{p.get("serverName") or "MCP"} 想调用工具' + (f' {tool}' if tool else '')
    inp = {'message': p.get('message') or '', 'server': p.get('serverName') or ''}
    view = {'title': title, 'cmd': '', 'reason': p.get('message') or ''}
    tp = meta.get('tool_params') or meta.get('arguments')
    if p.get('serverName') == SERVER and tool == 'delegate_task' and isinstance(tp, dict):
        inp = dict(tp, _cwb_preview=preview(c.sid, tp))  # 和 Claude 的授权卡一样：“Codex 想让 X 做：…”

    def done(dec, msg):
        r.respond(rid, {'action': 'accept' if dec in ('allow', 'always') else 'decline' if dec == 'deny' else 'cancel'})
    adapter.core.ask_perm(c, 'mcp__%s__%s' % (p.get('serverName') or 'mcp', tool or 'tool'), inp, view, [], done)
    return True


# ================================================================ Claude 钩子：叫醒长轮询、子对话的轮次、委派授权卡
def preview(sid, ti):
    """授权卡上写“Claude 想让 Codex 做：…（模型、权限档、任务前 200 字）”。"""
    try:
        pf = prefs()
        tg = norm_target(ti.get('target'))
        prov, rt, im, pmodel = parent_info(sid)
        want = tg.get('providerInstanceId') or prov
        model = tg.get('model') or (pmodel if want == prov else (provider_models(want)[0][:1] or [{}])[0].get('id', ''))
        mode = ceiling(rt, pf)
        try:
            mode = resolve_runtime_mode(mode, ti.get('runtimeMode') if ti.get('runtimeMode') not in (None, 'inherit') else None, supported_modes(want))
        except Fail:
            pass
        return {'who': LABEL.get(prov, prov), 'to': LABEL.get(want, want), 'model': model or '', 'mode': MODE_ZH.get(mode, mode),
                'task': ' '.join(str(ti.get('task') or '').split())[:200], 'title': str(ti.get('title') or '')[:80]}
    except Exception:
        return None


# T3 用 SDK 的 allowedTools 预先放行自己的 MCP 工具（ClaudeAdapterV2 claudeMcpQueryOverrides）。工作台不能往 --settings 里加放行规则（G2a），
# 改在已有的 PermissionRequest 钩子里直接放行：查看能派给谁、查状态、取消自己派出的任务。委派本身照 PLAN M8 出授权卡。
AUTO_ALLOW = ('mcp__cwb__orchestrator_capabilities', 'mcp__cwb__task_status', 'mcp__cwb__task_cancel')


def auto_allow(ns, sid, body, gen):
    if str((body or {}).get('tool_name') or '') not in AUTO_ALLOW:
        return None
    try:
        r, _ = ns['hook_runner'](sid, gen)
    except Exception:
        return None
    if not r or is_child(r.sid) or getattr(r, 'unattended', False):
        return None  # 无人值守的定时任务走原有的“全部拒绝”（复核 M-10）
    if r.sid not in SID_TOK and not any(ns['resolve_sid'](k) == r.sid for k in list(SID_TOK)):
        return None  # 只放行挂了 cwb 的父对话自己的调用（/clear 换了 ID 的按别名认）
    return {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}


def wrap_hooks(ns):
    orig = ns['handle_hook']

    def handle_hook(ev, sid, body, gen=''):
        if ev == 'task-wait':
            return task_wait(sid, body, gen)
        if ev == 'task-ack':
            return task_ack(sid, body, gen)
        if ev == 'permission':
            ok = auto_allow(ns, sid, body, gen)
            if ok:
                return ok
        if ev == 'permission' and str((body or {}).get('tool_name') or '') == 'mcp__cwb__delegate_task' and isinstance(body.get('tool_input'), dict):
            r, _ = ns['hook_runner'](sid, gen)
            pv = preview(r.sid if r else sid, body['tool_input'])
            if pv:  # 只加在界面看的那份 input 里：允许时不带 updatedInput，CLI 用它自己的原参数
                body = dict(body, tool_input=dict(body['tool_input'], _cwb_preview=pv))
        res = orig(ev, sid, body, gen)
        if ev in ('busy', 'idle', 'fail'):
            try:
                r, _ = ns['hook_runner'](sid, gen)
                csid = r.sid if r else ns['resolve_sid'](sid)
                tid = CHILD.get(csid)
                t = TASKS.get(tid) if tid else None
                if t and live(t):
                    if ev == 'busy':
                        t['_seen_busy'] = True
                        set_status(t, 'running')
                    elif not t.get('_finishing'):
                        t['_finishing'] = True
                        threading.Thread(target=finish_claude, args=(t, ev, body or {}), daemon=True).start()
            except Exception as e:
                log('处理子对话钩子出错', ev, repr(e))
        return res
    ns['handle_hook'] = handle_hook

    orig_note = ns['sys_note']

    def sys_note(o, raw):
        it = orig_note(o, raw)
        if 'PostToolUse:mcp__cwb__delegate_task' in (raw or '') or 'PostToolUse:mcp__cwb__' in (raw or ''):
            it.update(tag='子任务', text='子任务已完成，结果已交回')  # 叫醒那一行：origin.kind=task-notification，画成灰色系统行
            m = re.search(r'Delegated task (task_[0-9a-f]+)', raw or '')
            if m:
                it['cwbTask'] = m.group(1)
        return it
    ns['sys_note'] = sys_note


# ================================================================ 手动复核（不经过模型，不受总开关控制）
def review_text(sid, b):
    pv = NS.get('PV')
    files, diffs = [], []
    budget = 60000
    if pv and sid in pv.CONVS:
        c = pv.CONVS[sid]
        c.ensure_loaded()
        items = list(c.items)
        uid = b.get('uid') or ''
        idx = next((i for i, it in enumerate(items) if it.get('id') == uid), None)
        if idx is None:
            raise ValueError('找不到这一轮')
        end = next((j for j in range(idx + 1, len(items)) if items[j].get('k') == 'user' and not items[j].get('mid')), len(items))
        for it in items[idx + 1:end]:
            if it.get('k') != 'tool' or it.get('name') not in ('Edit', 'Write', '删除文件') or not it.get('path'):
                continue
            ln = it.get('ln') or [0, 0]
            files.append(f'- {it["path"]}（{it["name"]}，+{ln[0]} −{ln[1]}）')
            d = it.get('diff') or {}
            body = (f'--- 改前\n{d.get("old") or ""}\n+++ 改后\n{d.get("new") or ""}' if d else (it.get('in') or ''))
            if budget > 0:
                piece = f'### {it["path"]}\n```\n{body[:budget]}\n```'
                budget -= len(piece)
                diffs.append(piece)
        cwd = c.cwd
    else:
        uuid_ = b.get('uuid') or ''
        ck = NS['CK']
        d = ck.turn_changes(sid)
        turn = next((x for x in d.get('turns') or [] if x.get('uuid') == uuid_), None)
        if not turn:
            raise ValueError('这一轮没有改动记录')
        for f in turn.get('files') or []:
            path = f.get('real') or f.get('path') or ''
            files.append(f'- {f.get("rel") or path}（+{f.get("add") or 0} −{f.get("del") or 0}）')
            if budget > 0 and path:
                try:
                    dd = ck.turn_diff(sid, uuid_, path)
                    piece = f'### {f.get("rel") or path}\n```diff\n{(dd.get("diff") or "")[:budget]}\n```'
                except Exception as e:
                    piece = f'### {f.get("rel") or path}\n（差异读不出来：{e}）'
                budget -= len(piece)
                diffs.append(piece)
        cwd = parent_cwd(sid)
    if not files:
        raise ValueError('这一轮没有改动文件')
    return ('请复核下面这一轮代码改动。只审阅，不要修改任何文件，也不要运行会改动文件的命令。'
            '列出发现的问题（写明文件和位置，按严重程度排序）；没发现问题就直接说没有问题。\n\n'
            f'项目文件夹：{cwd}\n\n改动的文件：\n' + '\n'.join(files) + '\n\n差异：\n\n' + '\n\n'.join(diffs))


def api_review(b):
    sid = b.get('sid') or ''
    prov = b.get('prov') or ''
    if prov not in PROVS:
        raise ValueError('没有这个提供方')
    pf = prefs()
    st = prov_status(prov)
    if not st.get('installed') or not st.get('ok'):
        raise ValueError(st.get('hint') or f'{LABEL[prov]} 现在用不了')
    if sid in CHILD:
        raise ValueError('子任务对话里不能再委派')
    text = review_text(sid, b)
    refuse, q = quota_of(prov)
    if refuse:
        raise ValueError(refuse.split('。')[0] + '。')
    models, _ = provider_models(prov)
    pprov, prt, _, pmodel = parent_info(sid)
    model = pmodel if prov == pprov and pmodel else (models[0]['id'] if models else '')
    anchor = b.get('uuid') or b.get('uid') or ''
    t = {'id': 'task_' + uuid.uuid4().hex[:16], 'parent': sid, 'parentProv': pprov, 'child': '', 'childProv': prov, 'model': model,
         'effort': None, 'title': '复核这一轮改动', 'task': text, 'role': 'review', 'runtimeMode': 'approval-required',
         'interactionMode': 'default', 'origin': 'manual', 'createdAt': now_ms(), 'status': 'starting', 'x3': None, 'delivery': 'none',
         'quotaUnknown': q is None, 'clientRequestId': None, 'anchor': anchor}
    try:
        ticket = reserve(sid, pf, 'manual')  # 检查上限和占位紧挨着建对话（复核 M-1）
    except Fail as e:
        raise ValueError(e.message)
    ok = False
    try:
        spawn(t)
        ok = True
    except Fail as e:
        raise ValueError(e.message)
    finally:
        release(ticket, ok)
    return {'ok': True, 'task': pub(t)}


# ================================================================ 界面用的接口
def tasks_of(sid):
    return [pub(t) for t in sorted(TASKS.values(), key=lambda x: x['createdAt']) if t['parent'] == sid]


def api_tasks(b):
    sid = b.get('sid') or ''
    out = {'tasks': tasks_of(sid)}
    kids = {t['child'] for t in TASKS.values() if t['parent'] == sid and t.get('child')}
    # T3 ChildThreadApprovals：父对话里也列出子任务等你确认的授权（用户自己点；父智能体批不了）
    out['childPerms'] = [dict(e['req'], cwbTask=CHILD.get(e['req'].get('sid'))) for e in list(NS['PERMS'].values())
                         if (e.get('req') or {}).get('sid') in kids]
    tid = CHILD.get(sid)
    if tid and tid in TASKS:
        out['task'] = pub(TASKS[tid])
        out['parentTitle'] = conv_title(TASKS[tid]['parent'])
    return out


def api_stop_task(b):
    t = TASKS.get(b.get('id') or '')
    if not t:
        raise ValueError('找不到这个子任务')
    cancel(t, 'user')
    return {'ok': True, 'task': pub(t)}


def conv_title(sid):
    pv = NS.get('PV')
    if pv and sid in pv.CONVS:
        return pv.CONVS[sid].title()
    names = NS['load_conf']().get('names', {})
    if names.get(sid):
        return names[sid]
    info = NS['INDEX'].get(sid) or {}
    try:
        return info.get('title') or NS['auto_title'](sid) or '新对话'
    except Exception:
        return info.get('title') or '新对话'


def running_kids(sid):
    with LOCK:
        return [t for t in TASKS.values() if t['parent'] == sid and live(t)]


def cascade(sid, why='cascade'):
    n = 0
    for t in running_kids(sid):
        if cancel(t, why):
            n += 1
    if n:
        log('父对话停止，连带停掉子任务', sid[:8], n)
    return n


def drop_prewarm(all_=False):
    """停掉预热的 CLI：all_ 为真全停；否则只停挂没挂 cwb 和现在开关对不上的（新建对话复用预热之前调）。"""
    pw = NS.get('PREWARM')
    if pw is None:
        return 0
    on = prefs()['on']
    n = 0
    for k, r in list(pw.items()):
        if all_ or ARGS_ON.get(r.sid, False) != on:
            pw.pop(k, None)
            try:
                r.stop()
            except Exception as e:
                log('停预热的 CLI 失败', repr(e))
            n += 1
    if n:
        log('协作开关和预热的 CLI 对不上，停掉', n)
    return n


def wrap_api(ns):
    API = ns['API']
    o_projects, o_open = API['projects'], API['open']

    def projects(b):
        d = o_projects(b)
        try:
            kids = {}
            for t in TASKS.values():
                if live(t):
                    kids[t['parent']] = kids.get(t['parent'], 0) + 1
            for p in d.get('projects') or []:
                for s in p.get('sessions') or []:
                    tid = CHILD.get(s['sid'])
                    if tid and tid in TASKS:
                        t = TASKS[tid]
                        s['cwbParent'], s['cwbTask'], s['cwbState'] = t['parent'], tid, t['status']
                    if kids.get(s['sid']):
                        s['cwbKids'] = kids[s['sid']]
        except Exception as e:
            log('左栏加协作信息出错', repr(e))
        return d
    API['projects'] = projects

    def open_(b):
        d = o_open(b)
        try:
            if isinstance(d, dict) and d.get('sid'):
                d['cwb'] = api_tasks({'sid': d['sid']})
        except Exception as e:
            log('打开对话时加协作信息出错', repr(e))
        return d
    API['open'] = open_

    for name in ('interrupt',):
        o = API[name]

        def w(b, o=o):
            if not (b or {}).get('_cwb'):
                try:
                    sid = (b or {}).get('sid') or ''
                    cascade(NS['resolve_sid'](sid) if sid else sid)  # M9：用户停父对话，连带停掉它进行中的子任务
                except Exception as e:
                    log('级联停止出错', repr(e))
            return o(b)
        API[name] = w
    for name in ('archive', 'delete'):
        o = API[name]

        def w2(b, o=o, name=name):
            sid = (b or {}).get('sid') or ''
            r = o(b)  # 先调原接口：失败（抛错）时子任务不动、令牌不清（复核 M-9）
            try:
                if name == 'delete' or (b or {}).get('on', True):
                    cascade(sid)
                if name == 'delete' and sid:
                    drop_token(sid)  # 删掉的对话不再需要令牌：内存和 .run/mcp-<对话>.json 一起清掉
            except Exception as e:
                log('归档、删除后连带处理出错', repr(e))
            return r
        API[name] = w2
    o_new = API['new']

    def new_(b):
        try:
            drop_prewarm()  # 复用预热的 CLI 除了比 opts_key，还要比协作开关（复核 M-2）
        except Exception as e:
            log('核对预热的 CLI 出错', repr(e))
        return o_new(b)
    API['new'] = new_
    o_ua = API.get('ua-check')

    def ua_check(b):
        res = o_ua(b) if o_ua else {'ok': True}
        if prefs()['on'] or (b or {}).get('collab'):
            c = ua_check_collab()
            res = dict(res, collab=c, ok=bool(res.get('ok')) and bool(c.get('ok')))
            if not c.get('ok') and not res.get('error'):
                res['error'] = '协作开的用例没通过：' + (c.get('error') or c.get('ua') or '没抓到请求')
        return res
    if o_ua:
        API['ua-check'] = ua_check
    API.update({'collab-prefs': api_prefs, 'collab-tasks': api_tasks, 'collab-stop': api_stop_task, 'collab-deliver': api_deliver,
                'collab-review': api_review, 'collab-kids': lambda b: {'n': len(running_kids(b.get('sid') or ''))},
                'collab-cascade': lambda b: {'n': cascade(b.get('sid') or '')}})


def ua_check_collab():
    """ua-check 的“协作开”用例：同一个自检函数（伪终端交互式 claude），--settings 多叫醒钩子，另加 --mcp-config。
    令牌是临时的“自检”令牌：能连上、能列工具，不能调用。"""
    tok = 'cwbua_' + uuid.uuid4().hex
    p = os.path.join(NS['RUN_DIR'], f'mcp-uacheck-{os.getpid()}.json')
    TOK[tok] = '__ua__'
    try:
        write_0600(p, json.dumps(mcp_doc(tok)))
        lock = NS.get('UA_LOCK')
        if lock and not lock.acquire(timeout=70):
            return {'ok': False, 'error': '正在自检，请稍候'}
        try:
            res = NS['ua_check_run'](extra={'settings': {'hooks': {'PostToolUse': [wake_hook()]}}, 'args': ['--mcp-config', p]})
        finally:
            if lock:
                lock.release()
        NS['log']('请求头自检（协作开）', '通过' if res.get('ok') else '未通过', res.get('ua'), res.get('error') or '')
        return res
    finally:
        TOK.pop(tok, None)
        try:
            os.remove(p)
        except OSError:
            pass


# ================================================================ 挂接
def setup(ns):
    global NS
    NS = ns
    import shlex
    ns.setdefault('shlex', shlex)
    pv = ns.get('PV')
    load_tokens()
    load_tasks()
    wrap_hooks(ns)
    wrap_api(ns)
    if pv:
        pv.HOOKS['codex_thread'] = codex_thread
        pv.HOOKS['codex_elicitation'] = codex_elicitation
        o_end = pv.PConv.end_turn

        def end_turn(self, reason, *a, **kw):
            r = o_end(self, reason, *a, **kw)
            try:
                if self.sid in CHILD:
                    on_pconv_end(self, reason)
            except Exception as e:
                log('处理子对话结束出错', repr(e))
            return r
        pv.PConv.end_turn = end_turn
    threading.Thread(target=reconcile, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
