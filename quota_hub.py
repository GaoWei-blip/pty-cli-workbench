"""第十七轮 U：三家额度快照（Claude、Codex、Antigravity）、定时读取、预警。

窗口归一化、匀速线和节奏按 T3 Code 移植（MIT，Copyright (c) 2026 T3 Tools Inc.）：
- 移植自 T3 Code packages/provider-core/src/server/usageLimits.ts：clamp_percent（clampPercent）、窗口排序（sortWindows／WINDOW_KIND_ORDER）、
  失败时保留上次的窗口（resolveUsageLimitsAfterProbe）；
- 移植自 T3 Code packages/shared/src/usageLimits.ts：elapsed_share（elapsedShare）、pace_of（paceOf／paceOfShares，±5 个点）、
  agy 窗口显示顺序（displayLimitWindows：Gemini 组在前）；
- 移植自 T3 Code apps/server/src/provider/codexUsageLimits.ts：codex_windows（codexRateLimitsToWindows／codexRateLimitsToLimits）、
  codex_reset_credits（codexResetCreditsToContract）；
- 移植自 T3 Code apps/server/src/provider/antigravityUsageLimits.ts：agy_windows（summaryWindows／summaryResetTime／quotaWindow）；
- 移植自 T3 Code apps/server/src/provider/claudeUsageLimits.ts：Claude 窗口的 id 和种类（five_hour／seven_day／按模型的 seven_day_<名>）。

偏离 T3 的地方（理由只有三类，见 NOTES-U）：
- Claude：T3 经 Agent SDK 的 get_usage 和 rate_limit_event 取额度；工作台只读状态栏脚本写的 ~/.claude/.crs-quota.json（G2／G2a，
  不为拿数据启动 claude，不用 SDK、-p、/api/oauth/usage；也是第十四轮已有链路）。
- Antigravity：T3 用 OAuth 令牌调 Google 内部接口 v1internal:retrieveUserQuotaSummary（要读钥匙串）；工作台改用命令行
  `agy -p "/quota" --output-format json`（调研 §6.3 不采用逆向接口；PLAN U3）。返回的 groups/buckets 结构与 T3 解析的 Summary 一致（蛇形命名），
  解析逻辑照 summaryWindows。
- Codex：T3 在提供方健康检查里顺带读；工作台复用第十六轮 P 的提供方进程（prov_codex.CodexAdapter.read_quota，写 data/quota-codex.json），
  进程不在时临时起一个 `codex app-server` 只做 initialize＋config/read＋account/rateLimits/read（已有链路＋PLAN U3）。
- 窗口文字用中文（工作台界面是中文）；对外结构按 PLAN X1（kind 用 5h/week/month/model/other，节奏用 fast/ok/slow）。

对外接口（PLAN X1，M 线会 import）：
  snapshot(max_age=None) -> {"providers": [P…]}
  get(pid, max_age=None) -> P
  tightest(pid) -> {"usedPercent", "label", "resetsAt"} | None
P = {id, label, ok, ts(毫秒), stale, src, plan?, windows:[W…], credits?, resetCredits?, unavailable?:{reason,text}, viaProxy?, lastGood?, checking?}
W = {id, label, kind, group?, usedPercent, windowMins?, resetsAt(毫秒)?, elapsed?, pace?}
max_age 秒：传了且这家的快照比它旧，就当场刷新一次（最多等 20 秒，超时先返回已有的，刷新在后台继续）。

【红线】Codex 额度只读：只调 account/rateLimits/read；“消耗重置券”的方法（account/rateLimitResetCredit 下的 consume）绝不调用。
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time

NS = {}
PIDS = ('claude', 'codex', 'agy')
LABEL = {'claude': 'Claude', 'codex': 'Codex', 'agy': 'Antigravity'}
STALE_MS = 15 * 60 * 1000          # U2：超过 15 分钟没更新标“可能已过时”
EVERY = {'codex': 600, 'agy': 900}  # U3：Codex 每 10 分钟、agy 每 15 分钟
OPEN_THROTTLE = 60                  # 打开页面时的“立即读”：同一家 60 秒内不重复
WAIT_MAX = 20                       # X1：max_age 当场刷新最多等 20 秒
AGY_TIMEOUT = float(os.environ.get('CWB_QUOTA_AGY_TIMEOUT') or 30)   # T3 antigravityUsageLimits：30 秒
CODEX_TIMEOUT = float(os.environ.get('CWB_QUOTA_CODEX_TIMEOUT') or 20)
TICK = float(os.environ.get('CWB_QUOTA_TICK') or 30)
ALERT_USED = 80                     # U9：已用 ≥80%
ALERT_FAST_LEFT = 30                # U9：节奏偏快且剩余不到 30%
KEEP_ACTIVE = []                    # 协作线（M）可以登记一个返回 True/False 的函数：协作开着时没有窗口也定时读

SESSION_MINS, WEEK_MINS, MONTH_MINS = 5 * 60, 7 * 24 * 60, 30 * 24 * 60
KIND_ORDER = {'5h': 0, 'week': 1, 'month': 2, 'model': 3, 'other': 4}   # T3 WINDOW_KIND_ORDER（session、weekly、monthly、other）
T3_KIND = {'session': '5h', 'weekly': 'week', 'monthly': 'month', 'other': 'other'}
KIND_LABEL = {'5h': '5 小时', 'week': '每周', 'month': '每月'}

_lock = threading.RLock()
_state = {}        # pid -> 最近一次的 P（不含按当前时间算的 elapsed/pace）
_busy = {}         # pid -> threading.Event（正在刷新；完成时 set）
_last_try = {}     # pid -> 最近一次开始刷新的时间（秒）
_codex_raw = {}    # {'ts': 毫秒, 'raw': account/rateLimits/read 的原始结果}：临时进程读到的
_sig = {}          # pid -> 上次推送的指纹
_started = [False]


def log(*a):
    try:
        NS['log']('[额度]', *a)
    except Exception:
        pass


def now_ms():
    return int(time.time() * 1000)


def data_dir():
    try:
        import providers as PV
        return PV.DATA_DIR
    except Exception:
        return os.environ.get('CWB_DATA_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')


def run_dir():
    return NS.get('RUN_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '.run')


def home():
    return NS.get('HOME') or os.path.expanduser('~')


def read_json(p):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def write_json(p, doc):
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        log('写文件失败', p, repr(e))


# ---------------------------------------------------------------- T3：通用（provider-core usageLimits.ts、shared usageLimits.ts）
def clamp_percent(v):
    """T3 clampPercent。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float('inf'), float('-inf')):
        return 0.0
    return max(0.0, min(100.0, v))


def sort_windows(ws):
    """T3 sortWindows：按种类（5 小时、每周、每月、其他），同种按 id。"""
    return sorted(ws, key=lambda w: (KIND_ORDER.get(w.get('kind'), 9), str(w.get('id'))))


def elapsed_share(w, now=None):
    """T3 elapsedShare：窗口已过去的比例；没有重置时间或窗口长度返回 None。"""
    r, mins = w.get('resetsAt'), w.get('windowMins')
    if not r or not mins:
        return None
    length = mins * 60000
    if length <= 0:
        return None
    now = now_ms() if now is None else now
    return max(0.0, min(1.0, (length - (r - now)) / length))


def pace_of(w, now=None):
    """T3 paceOf／paceOfShares：已用％比时间进度高出 5 个点以上偏快，低 5 个点以上偏慢（ahead/on/under → fast/ok/slow）。"""
    el = elapsed_share(w, now)
    if el is None:
        return None
    gap = float(w.get('usedPercent') or 0) - el * 100
    return 'fast' if gap > 5 else 'slow' if gap < -5 else 'ok'


def with_pace(p, now=None):
    """给窗口补上按当前时间算的 elapsed、pace（不改缓存里的原件）。"""
    now = now_ms() if now is None else now
    out = dict(p)

    def deco(ws):
        res = []
        for w in ws or []:
            w = dict(w)
            el = elapsed_share(w, now)
            if el is not None:
                w['elapsed'] = round(el, 4)
                w['pace'] = pace_of(w, now)
            res.append(w)
        return res
    out['windows'] = deco(p.get('windows'))
    if p.get('lastGood'):
        out['lastGood'] = dict(p['lastGood'], windows=deco(p['lastGood'].get('windows')))
    ts = p.get('ts') or 0
    good_ts = ts if p.get('ok') else (p.get('lastGood') or {}).get('ts') or 0
    out['stale'] = bool(good_ts) and now - good_ts > STALE_MS
    return out


def empty_p(pid, src):
    return {'id': pid, 'label': LABEL[pid], 'ok': False, 'ts': 0, 'stale': False, 'src': src, 'windows': []}


def keep_last_good(p, prev):
    """T3 resolveUsageLimitsAfterProbe：这次没读到，保留上次读到的窗口（放在 lastGood，界面显示它和“x 分钟前”）。"""
    if p.get('ok'):
        p['lastGood'] = {'ts': p['ts'], 'windows': p['windows']}
    elif prev:
        lg = prev.get('lastGood') or ({'ts': prev.get('ts'), 'windows': prev.get('windows')} if prev.get('ok') else None)
        if lg and lg.get('windows'):
            p['lastGood'] = lg
    return p


# ---------------------------------------------------------------- Claude：~/.claude/.crs-quota.json（claudeUsageLimits.ts 的窗口 id）
def claude_file():
    return os.path.join(home(), '.claude', '.crs-quota.json')


def claude_windows(d):
    """crs 文件 {ts, w:[{lbl, u 已用%, r 重置时间（秒）, t 窗口秒数}], err?} → 窗口。
    5h → five_hour（T3 Session），7d → seven_day（T3 Weekly），其余（如 Fable）→ 按模型的每周额度 seven_day_<名>（T3 scopedWindow）。"""
    ws = []
    for w in d.get('w') or []:
        if not isinstance(w, dict) or not isinstance(w.get('u'), (int, float)):
            continue
        lbl = str(w.get('lbl') or '').strip()
        r, t = w.get('r'), w.get('t')
        mins = int(t // 60) if isinstance(t, (int, float)) and t > 0 else None
        if lbl == '5h':
            win = {'id': 'five_hour', 'kind': '5h', 'label': KIND_LABEL['5h'], 'windowMins': mins or SESSION_MINS}
        elif lbl == '7d':
            win = {'id': 'seven_day', 'kind': 'week', 'label': KIND_LABEL['week'], 'windowMins': mins or WEEK_MINS}
        elif lbl:
            tok = ''.join(ch if ch.isalnum() else '_' for ch in lbl.lower()).strip('_')
            win = {'id': 'seven_day_' + tok, 'kind': 'model', 'group': lbl, 'label': f'每周 · {lbl}', 'windowMins': mins or WEEK_MINS}
        else:
            continue
        win['usedPercent'] = clamp_percent(w['u'])
        if isinstance(r, (int, float)) and r > 0:
            win['resetsAt'] = int(r * 1000)
        ws.append(win)
    return sort_windows(ws)


def read_claude():
    prev = _state.get('claude')
    p = empty_p('claude', 'file')
    d = read_json(claude_file())
    if d is None:
        if os.path.exists(claude_file()):
            p['unavailable'] = {'reason': 'probeFailed', 'text': '额度文件读不出来'}
        else:
            p['unavailable'] = {'reason': 'none', 'text': '没有额度数据（状态栏脚本没运行）'}
        return keep_last_good(p, prev)
    ts = d.get('ts') if isinstance(d.get('ts'), (int, float)) else 0
    p['ts'] = int(ts * 1000) if ts else 0
    ws = claude_windows(d)
    if d.get('err'):
        p['unavailable'] = {'reason': 'probeFailed', 'text': '网关查询失败'}
        log('Claude 额度：状态栏脚本报错', str(d.get('err'))[:120])
        if ws:
            p['lastGood'] = {'ts': p['ts'], 'windows': ws}
        return keep_last_good(p, prev)
    if not ws:
        p['unavailable'] = {'reason': 'probeFailed', 'text': '额度文件里没有窗口数据'}
        return keep_last_good(p, prev)
    p.update(ok=True, windows=ws)
    return keep_last_good(p, prev)


# ---------------------------------------------------------------- Codex（codexUsageLimits.ts）
def _iso_s(v):
    return int(v * 1000) if isinstance(v, (int, float)) and v > 0 else None


def _kind_for_duration(mins):
    """T3 kindForDuration。"""
    if mins >= MONTH_MINS:
        return 'month'
    if mins >= WEEK_MINS:
        return 'week'
    return '5h'


def codex_windows(snapshot):
    """T3 codexRateLimitsToWindows：primary/secondary 是位置；没有 windowDurationMins 时，付费计划按 5 小时／每周，Free/Go 按每月。
    按模型的额度（limitId 不是 codex）不进主窗口。"""
    snapshot = snapshot or {}
    if snapshot.get('limitId') and snapshot.get('limitId') != 'codex':
        return []
    monthly = snapshot.get('planType') in ('free', 'go')
    ws = []
    for wid, w, fallback in (('primary', snapshot.get('primary'), MONTH_MINS if monthly else SESSION_MINS),
                             ('secondary', snapshot.get('secondary'), WEEK_MINS)):
        if not isinstance(w, dict) or not isinstance(w.get('usedPercent'), (int, float)):
            continue
        mins = w.get('windowDurationMins') if isinstance(w.get('windowDurationMins'), (int, float)) else fallback
        kind = _kind_for_duration(mins)
        win = {'id': wid, 'kind': kind, 'label': KIND_LABEL[kind], 'usedPercent': clamp_percent(w['usedPercent']), 'windowMins': int(mins)}
        r = _iso_s(w.get('resetsAt'))
        if r:
            win['resetsAt'] = r
        ws.append(win)
    il = snapshot.get('individualLimit')
    if isinstance(il, dict) and isinstance(il.get('remainingPercent'), (int, float)):
        win = {'id': 'individual', 'kind': 'month', 'label': '每月额度上限', 'usedPercent': clamp_percent(100 - il['remainingPercent'])}
        r = _iso_s(il.get('resetsAt'))
        if r:
            win['resetsAt'] = r
        ws.append(win)
    return ws


def codex_reset_credits(summary):
    """T3 codexResetCreditsToContract：只显示张数和最近的过期时间（绝不兑换）。"""
    if not isinstance(summary, dict):
        return None
    exp = [c.get('expiresAt') for c in summary.get('credits') or [] if isinstance(c, dict) and c.get('status') == 'available'
           and isinstance(c.get('expiresAt'), (int, float))]
    out = {'availableCount': max(0, int(summary.get('availableCount') or 0))}
    if exp:
        out['nextExpiresAt'] = int(min(exp) * 1000)
    return out


def codex_from_raw(res):
    """T3 codexRateLimitsToLimits：优先 rateLimitsByLimitId.codex，其次 rateLimits。"""
    snap = (res.get('rateLimitsByLimitId') or {}).get('codex') or res.get('rateLimits') or {}
    return sort_windows(codex_windows(snap)), snap


def codex_from_doc_windows(ws):
    """没有原始结果时（比如刚启动），用 P 线写的 data/quota-codex.json 里已转换的窗口；按模型的（kind=model）不进主窗口，和 T3 一致。"""
    out = []
    for w in ws or []:
        if not isinstance(w, dict) or w.get('kind') == 'model' or not isinstance(w.get('usedPercent'), (int, float)):
            continue
        w = dict(w)
        if w.get('kind') in KIND_LABEL:
            w['label'] = KIND_LABEL[w['kind']]
        w['usedPercent'] = clamp_percent(w['usedPercent'])
        out.append(w)
    return sort_windows(out)


def codex_adapter():
    try:
        import providers as PV
        return PV.ADAPTERS.get('codex')
    except Exception:
        return None


def codex_file():
    return os.path.join(data_dir(), 'quota-codex.json')


def read_codex():
    """组装 Codex 的 P：状态以 data/quota-codex.json 为准（P 线和这里都往里写）；窗口用 T3 的规则从原始结果重算。"""
    prev = _state.get('codex')
    p = empty_p('codex', 'protocol')
    a = codex_adapter()
    try:
        st = a.status() if a else {'installed': False}
    except Exception:
        st = {'installed': True}
    doc = read_json(codex_file()) or {}
    if not st.get('installed') and not doc:
        p['unavailable'] = {'reason': 'none', 'text': '没有安装 Codex'}
        return p
    ts = int(doc.get('ts') or 0)
    p['ts'] = ts
    raw = None
    if _codex_raw.get('ts') == ts and _codex_raw.get('raw'):
        raw = _codex_raw['raw']
    elif a is not None and getattr(a, 'quota_raw', None) and isinstance(getattr(a, 'quota', None), dict) and a.quota.get('ts') == ts:
        raw = a.quota_raw
    if raw:
        ws, snap = codex_from_raw(raw)
        if snap.get('planType'):
            p['plan'] = snap['planType']
        if isinstance(snap.get('credits'), dict):
            p['credits'] = snap['credits']
        rc = codex_reset_credits(raw.get('rateLimitResetCredits'))
    else:
        ws = codex_from_doc_windows(doc.get('windows'))
        if doc.get('plan'):
            p['plan'] = doc['plan']
        if isinstance(doc.get('credits'), dict):
            p['credits'] = doc['credits']
        rc = codex_reset_credits(doc.get('resetCredits'))
    if rc:
        p['resetCredits'] = rc
    if doc.get('viaProxy'):
        p['viaProxy'] = True
    un = doc.get('unavailable')
    if isinstance(un, dict):
        reason = un.get('reason')
        if reason == 'notLoggedIn':  # -32600：API key 或没用 ChatGPT 登录（T3 记 unsupported）
            p['unavailable'] = {'reason': 'unsupported', 'text': '这种登录方式没有额度'}
        elif reason == 'signedOut':
            p['unavailable'] = {'reason': 'none', 'text': 'Codex 还没登录'}
        else:
            p['unavailable'] = {'reason': 'probeFailed', 'text': '这次没读到 Codex 额度'}
        lg = doc.get('lastGood') or {}
        if lg.get('windows'):
            p['lastGood'] = {'ts': int(lg.get('ts') or 0), 'windows': codex_from_doc_windows(lg.get('windows'))}
        return keep_last_good(p, prev)
    if not doc:
        p['unavailable'] = {'reason': 'none', 'text': '还没读过 Codex 额度'}
        return keep_last_good(p, prev)
    if not ws:
        p['unavailable'] = {'reason': 'probeFailed', 'text': 'Codex 没有返回额度窗口'}
        return keep_last_good(p, prev)
    p.update(ok=True, windows=ws)
    return keep_last_good(p, prev)


def refresh_codex():
    """Codex 进程在：用它读（prov_codex.CodexAdapter.read_quota，写 data/quota-codex.json）；
    不在：临时起一个 codex app-server，只做 initialize＋config/read＋account/rateLimits/read，然后关掉。
    临时进程不登记到适配器（不算进 prov-busy 的进程和 restart_when_idle 的忙闲判断），单独进程组，结束时整组收掉。"""
    a = codex_adapter()
    if a is None:
        return
    try:
        if not a.status().get('installed'):
            return
    except Exception:
        pass
    r = getattr(a, 'rpc', None)
    if r is not None and r.alive():
        try:
            a.read_quota()
        except Exception as e:
            log('Codex 额度：用现有进程读失败', repr(e))
        return
    try:
        signed_out = not a.logged_in_files()
    except Exception:
        signed_out = False
    if signed_out:  # 没登录：不为查额度去起进程
        try:
            a.save_quota_doc({'v': 1, 'provider': 'codex', 'ts': now_ms(), 'src': 'protocol', 'windows': [],
                              'unavailable': {'reason': 'signedOut', 'text': 'Codex 还没登录'}})
        except Exception:
            pass
        return
    probe_codex_temp(a)


# 临时进程不需要 MCP 服务和插件（实测关掉后进程组里只剩 app-server 自己，读额度不受影响）；起不来就去掉这两项再试一次
TEMP_ARGS = ['-c', 'mcp_servers={}', '-c', 'features.plugins=false']


def probe_codex_temp(a, extra=True):
    import providers as PV
    argv = a.argv() + (TEMP_ARGS if extra else [])
    exe = argv[0]
    if not (os.path.isabs(exe) and os.path.exists(exe)) and not PV.which(exe):
        return
    logpath = os.path.join(run_dir(), f'quota-codex-{NS.get("PORT", 0)}.log')
    holder = {}

    def on_request(rid, method, params):
        rpc = holder.get('r')
        if rpc:
            rpc.respond(rid, error={'code': -32601, 'message': 'not supported'})
    try:
        rpc = PV.Rpc('codex-quota', argv, a.env(), home(), lambda m, p: None, on_request, lambda code: None, logpath)
    except OSError as e:
        log('Codex 额度：临时进程起不来', repr(e))
        return
    holder['r'] = rpc
    t0 = time.time()
    doc = None
    try:
        try:
            rpc.request('initialize', {'clientInfo': {'name': 'claude_workbench', 'title': 'Claude 工作台', 'version': '1.0'}}, CODEX_TIMEOUT)
        except PV.RpcError:
            if extra and not rpc.alive():
                rpc.close(1)
                log('Codex 额度：带精简参数起不来，改用原参数再试')
                return probe_codex_temp(a, extra=False)
            raise
        rpc.notify('initialized')
        provider = ''
        try:
            cfg = (rpc.request('config/read', {}, CODEX_TIMEOUT) or {}).get('config') or {}
            provider = str(cfg.get('model_provider') or '')
        except Exception:
            pass
        try:
            res = rpc.request('account/rateLimits/read', {}, CODEX_TIMEOUT) or {}
        except PV.RpcError as e:
            reason = 'notLoggedIn' if e.code == -32600 or 'authentication' in str(e.message).lower() else 'probeFailed'
            log('Codex 额度读不出来（临时进程）', e.code, str(e.message)[:200])
            doc = {'v': 1, 'provider': 'codex', 'ts': now_ms(), 'src': 'protocol', 'windows': [],
                   'unavailable': {'reason': reason, 'text': str(e.message)[:300]}}
        else:
            doc = a.quota_doc(res)
            doc.pop('viaProxy', None)
            if provider and provider != 'openai':
                doc['viaProxy'] = True  # 请求转给了别的提供方：账号额度和实际用量可能不符
            _codex_raw.update(ts=doc['ts'], raw=res)
    except Exception as e:
        log('Codex 额度：临时进程出错', repr(e))
        doc = {'v': 1, 'provider': 'codex', 'ts': now_ms(), 'src': 'protocol', 'windows': [],
               'unavailable': {'reason': 'probeFailed', 'text': str(e)[:300]}}
    finally:
        rpc.close(3)
    if doc is not None:
        try:
            a.save_quota_doc(doc)  # 现有对象的方法：保留 lastGood，写 data/quota-codex.json
        except Exception:
            write_json(codex_file(), doc)
    log('Codex 额度：临时进程读完', f'{time.time() - t0:.1f}s')


# ---------------------------------------------------------------- Antigravity（antigravityUsageLimits.ts 的 summaryWindows）
SUMMARY_WINDOW_MINS = {'5h': SESSION_MINS, 'weekly': WEEK_MINS}


def _iso_ms(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)
    except Exception:
        return None


def agy_windows(data, requested_at):
    """T3 summaryWindows：按 bucket id 归组（gemini-argon-/gemini-carbon- 用组名，gemini- → Gemini，3p- → Claude 与 GPT（共享）），
    窗口 weekly → 每周、5h → 5 小时；已用％＝(1−remaining_fraction)×100（T3 quotaWindow）；
    没动过的桶（剩余 1、没有说明、重置时间≈请求时刻＋整窗）不给重置时间（T3 summaryResetTime）。"""
    wins = {}
    groups = data.get('groups') if isinstance(data, dict) else None
    if not (isinstance(groups, list) and any(isinstance(g, dict) and g.get('buckets') for g in groups)):
        groups = [{'buckets': (data or {}).get('buckets') or []}]
    for g in groups:
        if not isinstance(g, dict):
            continue
        gname = str(g.get('name') or g.get('displayName') or '').strip()
        for b in g.get('buckets') or []:
            if not isinstance(b, dict) or b.get('disabled'):
                continue
            bid = str(b.get('id') or b.get('bucketId') or '').strip()
            if not bid:
                continue
            rf = b.get('remaining_fraction', b.get('remainingFraction'))
            if not isinstance(rf, (int, float)):
                continue
            if bid.startswith('gemini-argon-'):
                fam = gname or 'Gemini Argon'
            elif bid.startswith('gemini-carbon-'):
                fam = gname or 'Gemini Carbon'
            elif bid.startswith('gemini-'):
                fam = 'Gemini'
            elif bid.startswith('3p-'):
                fam = 'Claude 与 GPT（共享）'
            else:
                fam = gname or bid
            window = str(b.get('window') or '')
            weekly, session = window == 'weekly', window == '5h'
            mins = SUMMARY_WINDOW_MINS.get(window)
            reset = _iso_ms(b.get('reset_time', b.get('resetTime')))
            untouched = (rf == 1 and not b.get('description') and mins is not None and reset is not None
                         and abs(reset - requested_at - mins * 60000) <= 60000)
            kind = 'week' if weekly else '5h' if session else 'other'
            w = {'id': bid, 'kind': kind, 'group': fam,
                 'label': fam + ('· ' if fam.endswith('）') else ' · ') + ('每周' if weekly else '5 小时' if session else str(b.get('name') or b.get('displayName') or '额度').strip()),
                 'usedPercent': (1 - max(0.0, min(1.0, float(rf)))) * 100}
            if mins is not None:
                w['windowMins'] = mins
            if reset and not untouched:
                w['resetsAt'] = reset
            wins[bid] = w
    # T3 displayLimitWindows：Gemini 组在前，同组按去掉后缀的 id，再按种类
    import re as _re
    return sorted(wins.values(), key=lambda w: (not w['id'].startswith('gemini-'), _re.sub(r'-(5h|weekly|quota)$', '', w['id']),
                                                KIND_ORDER.get(w['kind'], 9)))


def agy_file():
    return os.path.join(data_dir(), 'quota-agy.json')


def agy_bin():
    if os.environ.get('CWB_AGY_BIN'):
        return os.environ['CWB_AGY_BIN']
    try:
        import providers as PV
        return PV.which('agy')
    except Exception:
        return shutil.which('agy') or ''


def read_agy():
    """从 data/quota-agy.json 读（结构就是 X1 的 P）；没有文件时看装没装。"""
    d = read_json(agy_file())
    if isinstance(d, dict) and d.get('id') == 'agy':
        p = {k: v for k, v in d.items() if k != 'v'}
        p['windows'] = sort_agy(p.get('windows') or [])
        return p
    p = empty_p('agy', 'cli')
    p['unavailable'] = ({'reason': 'none', 'text': '没有安装 Antigravity'} if not agy_bin()
                        else {'reason': 'none', 'text': '还没读过 Antigravity 额度'})
    return p


def sort_agy(ws):
    import re as _re
    return sorted(ws, key=lambda w: (not str(w.get('id')).startswith('gemini-'), _re.sub(r'-(5h|weekly|quota)$', '', str(w.get('id'))),
                                     KIND_ORDER.get(w.get('kind'), 9)))


def refresh_agy():
    """agy -p "/quota" --output-format json：命令行默认数据目录（不带 --app-data-dir，不碰 antigravity-cwb），只读、不开回合、不耗额度。
    有超时（30 秒，T3 同值），单独进程组，超时整组结束。结果写 data/quota-agy.json。"""
    exe = agy_bin()
    prev = read_json(agy_file())
    prev = prev if isinstance(prev, dict) and prev.get('id') == 'agy' else None
    p = empty_p('agy', 'cli')
    p['ts'] = now_ms()
    if not exe:
        p['unavailable'] = {'reason': 'none', 'text': '没有安装 Antigravity'}
        write_json(agy_file(), dict(keep_last_good(p, prev), v=1))
        return
    cwd = os.path.join(run_dir(), 'quota-agy-cwd')
    os.makedirs(cwd, exist_ok=True)
    env = dict(NS.get('LOGIN_ENV') or os.environ)
    if not env.get('PATH'):
        env['PATH'] = os.environ.get('PATH', '/usr/bin:/bin')
    out, code, err = '', None, ''
    try:
        proc = subprocess.Popen([exe, '-p', '/quota', '--output-format', 'json'], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as e:
        p['unavailable'] = {'reason': 'probeFailed', 'text': 'Antigravity 命令运行不了'}
        log('agy 额度：起不来', repr(e))
        write_json(agy_file(), dict(keep_last_good(p, prev), v=1))
        return
    try:
        o, e = proc.communicate(timeout=AGY_TIMEOUT)
        out, err, code = o.decode('utf-8', 'replace'), e.decode('utf-8', 'replace'), proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        code = 'timeout'
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # 整组收掉，不留进程（组长已退出时组号可能空了，忽略）
        except OSError:
            pass
    if code == 'timeout':
        p['unavailable'] = {'reason': 'probeFailed', 'text': 'Antigravity 没有及时返回额度'}
        log('agy 额度：超时', AGY_TIMEOUT)
    elif code != 0:
        # 用 API key 登录时 /quota 不可用（退出码 2）：T3 记 unsupported
        p['unavailable'] = ({'reason': 'unsupported', 'text': '这种登录方式没有额度'} if code == 2
                            else {'reason': 'probeFailed', 'text': '这次没读到 Antigravity 额度'})
        log('agy 额度：退出码', code, (err or out)[-300:])
    else:
        try:
            d = json.loads(out.strip().splitlines()[-1] if out.strip() else '{}')
        except Exception:
            try:
                d = json.loads(out)
            except Exception:
                d = {}
        data = ((d.get('command') or {}).get('data')) if isinstance(d, dict) else None
        ws = agy_windows(data or {}, p['ts']) if isinstance(data, dict) else []
        if ws:
            p.update(ok=True, windows=ws)
        else:
            p['unavailable'] = {'reason': 'probeFailed', 'text': 'Antigravity 没有返回可用的额度信息'}
            log('agy 额度：没有窗口', out[:300])
    write_json(agy_file(), dict(keep_last_good(p, prev), v=1))


# ---------------------------------------------------------------- 刷新调度
READERS = {'claude': read_claude, 'codex': read_codex, 'agy': read_agy}
REFRESHERS = {'codex': refresh_codex, 'agy': refresh_agy}


def _load(pid):
    try:
        p = READERS[pid]()
    except Exception as e:
        log('读额度出错', pid, repr(e))
        p = empty_p(pid, 'file')
        p['unavailable'] = {'reason': 'probeFailed', 'text': '读取出错'}
    with _lock:
        _state[pid] = p
        if pid in _busy and not _busy[pid].is_set():
            p = dict(p, checking=True)
    return p


def start_refresh(pid):
    """后台刷新一家（同一家同时只跑一个）；返回这次刷新的完成事件。"""
    if pid not in REFRESHERS:
        ev = threading.Event()
        ev.set()
        return ev
    with _lock:
        ev = _busy.get(pid)
        if ev is not None and not ev.is_set():
            return ev
        ev = _busy[pid] = threading.Event()
        _last_try[pid] = time.time()

    def run():
        try:
            REFRESHERS[pid]()
        except Exception as e:
            log('刷新额度出错', pid, repr(e))
        finally:
            ev.set()
            _load(pid)
            push()
    threading.Thread(target=run, daemon=True, name=f'quota-{pid}').start()
    return ev


def get(pid, max_age=None):
    """X1：一家的快照。max_age（秒）：快照比它旧就当场刷新一次，最多等 20 秒。"""
    if pid not in PIDS:
        raise ValueError('没有这个提供方')
    p = _load(pid)
    if max_age is not None and pid in REFRESHERS:
        ts = p.get('ts') or 0
        if now_ms() - ts > float(max_age) * 1000:
            ev = start_refresh(pid)
            ev.wait(WAIT_MAX)
            p = _load(pid)
    return with_pace(p)


def snapshot(max_age=None):
    """X1：三家的快照。max_age 时几家并行刷新，总共最多等 20 秒。"""
    if max_age is not None:
        evs = []
        for pid in REFRESHERS:
            p = _load(pid)
            if now_ms() - (p.get('ts') or 0) > float(max_age) * 1000:
                evs.append(start_refresh(pid))
        t_end = time.time() + WAIT_MAX
        for ev in evs:
            ev.wait(max(0, t_end - time.time()))
    return {'providers': [with_pace(_load(pid)) for pid in PIDS]}


def _live_windows(p, now=None):
    now = now_ms() if now is None else now
    if not p.get('ok'):
        return []
    return [w for w in p.get('windows') or [] if not (w.get('resetsAt') and w['resetsAt'] <= now)]


def tightest(pid):
    """X1：这家已用最多的窗口（已过重置时间的不算）；拿不到额度返回 None。"""
    try:
        p = get(pid)
    except ValueError:
        return None
    ws = _live_windows(p)
    if not ws:
        return None
    w = max(ws, key=lambda x: x.get('usedPercent') or 0)
    return {'usedPercent': w.get('usedPercent'), 'label': w.get('label'), 'resetsAt': w.get('resetsAt')}


def tightest_all(ps):
    """侧栏小条（U8）：三家里已用最多的那个窗口，带是哪家。"""
    best = None
    for p in ps:
        for w in _live_windows(p):
            if best is None or (w.get('usedPercent') or 0) > best['usedPercent']:
                best = {'provider': p['id'], 'providerLabel': p['label'], 'id': w.get('id'), 'label': w.get('label'), 'usedPercent': w.get('usedPercent') or 0,
                        'resetsAt': w.get('resetsAt'), 'kind': w.get('kind')}
                if w.get('pace'):
                    best['pace'] = w['pace']
    return best


def push(force=False):
    """额度有变化就推 quota 事件（接口三）。"""
    try:
        ps = [with_pace(_load(pid)) for pid in PIDS]
        # 复核 REVIEW-U 第 2 条：指纹去掉按当前时间算的 elapsed（每个节拍都变），额度没变就不推、页面不重画；节奏档位变了照推
        sig = json.dumps([[p.get('ts'), p.get('ok'), [{k: v for k, v in w.items() if k != 'elapsed'} if isinstance(w, dict) else w
                                                     for w in (p.get('windows') or [])],
                           p.get('unavailable'), p.get('checking')] for p in ps], sort_keys=True, default=str)
        if not force and _sig.get('all') == sig:
            return
        _sig['all'] = sig
        NS['BUS'].emit({'type': 'quota', 'providers': ps, 'tightest': tightest_all(ps)})
    except Exception as e:
        log('推送额度出错', repr(e))


# ---------------------------------------------------------------- 预警（U9）
def alerts_path():
    return os.path.join(run_dir(), 'quota-alerts.json')


def fmt_left(ms):
    """离重置还有多久（T3 formatDuration 的中文：x 天 x 小时／x 小时 x 分钟／x 分钟）。"""
    s = max(0, int(ms // 1000))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f'{d} 天 {h} 小时'
    if h:
        return f'{h} 小时 {m} 分钟'
    return f'{max(1, m)} 分钟'


def check_alerts(ps=None, now=None):
    """任一窗口已用 ≥80%，或节奏“偏快”且剩余不到 30%：每个窗口每个重置周期只发一次通知（沿用 notify 事件；远程窗口由 ux-r.js 改成页面内提示）。
    发过的记在 .run/quota-alerts.json（键：提供方:窗口:重置时间），过了重置时间的记录清掉。返回这次发出的通知。"""
    now = now_ms() if now is None else now
    ps = ps if ps is not None else [with_pace(_load(pid), now) for pid in PIDS]
    sent = read_json(alerts_path()) or {}
    sent = {k: v for k, v in sent.items() if isinstance(v, (int, float)) and (v == 0 or v > now - 86400000)}
    out = []
    for p in ps:
        for w in _live_windows(p, now):
            used = float(w.get('usedPercent') or 0)
            left = 100 - used
            fast = w.get('pace') == 'fast' or pace_of(w, now) == 'fast'
            if not (used >= ALERT_USED or (fast and left < ALERT_FAST_LEFT)):
                continue
            r = w.get('resetsAt') or 0
            key = f"{p['id']}:{w.get('id')}:{r // 60000 if r else 'na'}"
            if key in sent:
                continue
            sent[key] = r or 0
            text = f"{p['label']} · {w.get('label')}：剩余 {round(left)}%"
            if r:
                text += f'，{fmt_left(r - now)}后重置'
            if used < ALERT_USED:
                text += '（用得偏快）'
            out.append({'type': 'notify', 'title': '额度提醒', 'text': text, 'sid': ''})
    if out:
        write_json(alerts_path(), sent)
        for ev in out:
            try:
                NS['BUS'].emit(ev)
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- 定时读取（U4）
def active():
    """只在有窗口连接、或协作开关打开时才定时读（没人看就不跑）。"""
    try:
        if NS['BUS'].clients:
            return True
    except Exception:
        pass
    for fn in list(KEEP_ACTIVE):
        try:
            if fn():
                return True
        except Exception:
            pass
    try:
        c = NS['load_conf']().get('collab') or {}
        return bool(isinstance(c, dict) and (c.get('enabled') or c.get('on')))
    except Exception:
        return False


def loop():
    while True:
        time.sleep(TICK)
        try:
            if not active():
                continue
            t = time.time()
            for pid, every in EVERY.items():
                if t - _last_try.get(pid, 0) >= every:
                    start_refresh(pid)
            push()
            if NS['BUS'].clients:
                check_alerts()
        except Exception as e:
            log('定时读取出错', repr(e))


def open_refresh(pids):
    """打开额度页（quota 接口带 refresh）：各家 60 秒内没读过就立即在后台读一次。"""
    t = time.time()
    for pid in pids:
        if pid in REFRESHERS and t - _last_try.get(pid, 0) >= OPEN_THROTTLE:
            start_refresh(pid)


# ---------------------------------------------------------------- 接口
def setup(ns):
    """server.py import 之后调用一次：包 quota 接口（原有字段不变，加 providers、tightest），起定时线程。"""
    global NS
    NS = ns
    API = ns['API']
    orig = API.get('quota')

    def quota(b):
        b = b or {}
        d = orig(b) if orig else {}
        prov = str(b.get('provider') or 'all')
        pids = list(PIDS) if prov == 'all' else [prov] if prov in PIDS else []
        if b.get('refresh'):
            open_refresh(pids)
        ps = [with_pace(_load(pid)) for pid in PIDS]
        d = dict(d)
        d['providers'] = [p for p in ps if p['id'] in pids]
        d['tightest'] = tightest_all(ps)
        return d
    API['quota'] = quota
    if not _started[0] and not os.environ.get('CWB_QUOTA_NO_LOOP'):
        _started[0] = True
        threading.Thread(target=loop, daemon=True, name='quota-loop').start()
