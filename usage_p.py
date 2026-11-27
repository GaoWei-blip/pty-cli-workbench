"""第十七轮 U：用量与速度——三家（Claude、Codex、Antigravity）合到一起，扩展 usage 接口。

用户拍板（11-22 晚改定，PLAN U5）：Claude 完全保持第十四轮现状，只算工作台；Codex、agy 包括终端和其他客户端，不做开关。
- Claude：usage.py 一字不改，server 的 USAGE 对象不换、不改。Claude 一栏的 KPI、模型、排行直接取 USAGE.query(range) 的结果；
  按小时/天的分桶、速度折线、子任务汇总用同一个对象的 build() 行（query 内部也用它），按 query() 同样的时段过滤，数字和 query() 对得上。
  首字等待、每轮用时现有数据算不出，给 null 并写原因；最近 20 轮不列 Claude。
- Codex：读 <CODEX_HOME 或 ~/.codex>/sessions 下的 rollout（.jsonl 和 .jsonl.zst），照 T3 的逐行状态机算每次请求的 token。
  工作台的 Codex 对话也写在 rollout 里，所以 token 不从 data/conv 取（免得重复）；data/conv 只用来认出工作台对话（meta.native）和首字时间。
- Antigravity：读 agy 各数据目录 conversations/*.db（SQLite＋protobuf），照 T3 解码和跨库去重；工作台 agy 对话的 token
  若库里没有，用 data/conv 的 turn_end 补上（按原生会话 ID 去重）；费用只有 turn_end.cost（会话累计，按相邻两轮的差算到那一轮）。

移植自 T3 Code（MIT，Copyright (c) 2026 T3 Tools Inc.，v0.1.11）：
  apps/server/src/provider/Drivers/codexUsage.ts            → read_codex_token_usage … parse_codex_record（Codex 状态机，逐句移植）
  apps/server/src/usage/usageTranscriptReader.ts             → read_codex_rollout、fnv1a、list_transcript_files（按块读、续读、防改写校验、未完行）
  apps/server/src/usage/usageScanCache.ts                    → CodexScanner 的缓存条目与 Hub.save_cache/load_cache（大小、修改时间、偏移、续读状态、尾行）
  apps/server/src/usage/UsageService.ts                      → MTIME_SLACK_MS、保留期、readFileRecords、sharedSessions、删掉的文件保留用量
  apps/server/src/provider/Drivers/antigravityUsageReader.ts → agy_fields … agy_collect（protobuf 解码、模型名、并查集去重）
  apps/server/src/provider/Drivers/AntigravityUsage.ts       → agy_conversation_dirs（数据目录）
  apps/server/src/usage/usageAggregation.ts                  → merge_usage_copies、Aggregator（按天/小时分桶、costSource）
  packages/provider-core/src/server/usage.ts                 → total_tokens、parse_ts
  packages/contracts/src/usage.ts                            → UsageBucket 结构
T3 没有的（速度、对话排行、子任务汇总、占周额度、轮数）按调研文档 §6.2 和接口契约自建。偏离 T3 的地方在注释里写“偏离 T3”。
没移植：CodexUsageHistory.ts（读 T3 自己的编排库补 Codex 服务档，只影响定价；Codex 这里不折算钱）、CodexTurnTokenUsage.ts（协议实时
累计，工作台 P 线在 prov_codex.py 已有）、LiteLLM 联网价表、价格覆盖、模型别名、多环境。

server.py 只有一行挂接：import usage_p as UP; UP.setup(globals())。
"""
import bisect
import json
import math
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

NS = {}
HUB = None

# ---------------------------------------------------------------- 常量（取自 T3）
MTIME_SLACK_MS = 36 * 60 * 60 * 1000          # UsageService.ts：按修改时间裁剪文件时多留 36 小时
RETENTION_DAYS = 92                           # contracts settings usageHistoryRetentionDays 默认 92 天
GUARD_LENGTH = 64                             # usageTranscriptReader.ts：续读前核对偏移前 64 字节
STREAMING_THRESHOLD_BYTES = 8 * 1024 * 1024   # 超过 8 MB 的单行不整行留在内存
READ_CHUNK_BYTES = 256 * 1024
FORK_COPY_MAX_GAP_MS = 1000                   # codexUsage.ts：分叉时复制的历史在 1 秒内成批写入
GENERATION_PAGE_ROWS, STEP_PAGE_ROWS = 1, 64  # antigravityUsageReader.ts
CACHE_VERSION = 1
HEAD_BYTES = 64 * 1024

RANGES = {'today': 1, '7d': 7, '30d': 30}
ORDER = ('claude', 'codex', 'agy')
LABELS = {'claude': 'Claude', 'codex': 'Codex', 'agy': 'Antigravity'}
UNTITLED = '未命名对话'
ZSTD_MODE = None  # None 自动（标准库 → zstd 命令行 → 跳过）；测试可设 'cli' / 'none'

# 一条用量记录（T3 UsageRecord 的列表形式）：
# [时间毫秒, 模型, 会话 ID, 未缓存输入, 缓存读, 缓存写, 输出, 思考, 去重键, 提供方给的费用, 轮 ID, 请求耗时毫秒]
R_TS, R_MODEL, R_SID, R_U, R_C, R_CW, R_O, R_R, R_KEY, R_COST, R_TURN, R_MS = range(12)


def log(*a):
    try:
        NS['log']('[用量]', *a)
    except Exception:
        pass


def parse_ts(v):
    """T3 parseTimestampMs：ISO 时间 → 毫秒；认不出返回 None。"""
    if not isinstance(v, str) or not v:
        return None
    try:
        return int(datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp() * 1000)
    except Exception:
        return None


def total_tokens(r):
    """T3 totalTokens：未缓存输入＋缓存读＋缓存写＋输出（思考已含在输出里，不重复加）。"""
    return r[R_U] + r[R_C] + r[R_CW] + r[R_O]


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int0(v):
    """effect Schema.Int.check(>=0)：有限的非负整数；JSON 里的 5.0 也算 5。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float) and math.isfinite(v) and v.is_integer() and v >= 0:
        return int(v)
    return None


def _dumps(v):
    return json.dumps(v, separators=(',', ':'), ensure_ascii=False)


# ================================================================ Codex（移植 codexUsage.ts）
CODEX_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def read_codex_token_usage(value):
    """T3 readCodexTokenUsage：缺省的缓存、思考按 0，total 缺省按 输入＋输出；不合理的（缓存比输入多等）返回 None。"""
    if not isinstance(value, dict):
        return None
    inp, out = value.get('input_tokens'), value.get('output_tokens')
    if not _num(inp) or not _num(out):
        return None
    raw = (inp,
           value['cached_input_tokens'] if 'cached_input_tokens' in value else 0,
           value['cache_write_input_tokens'] if 'cache_write_input_tokens' in value else 0,
           out,
           value['reasoning_output_tokens'] if 'reasoning_output_tokens' in value else 0,
           value['total_tokens'] if 'total_tokens' in value else inp + out)
    vals = [_int0(x) for x in raw]
    if any(x is None for x in vals):
        return None
    i, c, w, o, r, t = vals
    if c + w > i or r > o or t < i + o:
        return None
    return dict(zip(CODEX_FIELDS, vals))


def _date_baseline(state, ts):
    """T3 dateCodexBaseline。"""
    state['latestCumulativeAtMs'] = max(state.get('latestCumulativeAtMs', ts), ts)


def _rebase(state, cumulative, ts):
    """T3 rebaseCodexUsage。"""
    state['lastCumulativeUsage'] = cumulative
    _date_baseline(state, ts)
    state.pop('unreportedRequests', None)
    if cumulative['input_tokens'] + cumulative['output_tokens'] == 0:
        state.pop('sequenceStartSignature', None)


def _advance(previous, last):
    """T3 advanceCodexUsage。"""
    return read_codex_token_usage({k: (previous[k] if previous else 0) + last[k] for k in CODEX_FIELDS})


def _unreported_requests(previous, cumulative, last, pending):
    """T3 unreportedRequests：中断的一轮在快照之前结束时，用 token_usage_record 补齐整段差额。"""
    if previous is None or pending is None or any(
            cumulative[k] - previous[k] != sum(q['usage'][k] for q in pending) for k in CODEX_FIELDS):
        return None
    if not pending:
        return None
    final = pending[-1]
    remainder = read_codex_token_usage({k: final['usage'][k] - last[k] for k in CODEX_FIELDS})
    if remainder is None:
        return None
    out = pending[:-1] + [dict(final, usage=remainder)]
    return [q for q in out if q['usage']['input_tokens'] + q['usage']['output_tokens'] > 0]


def _discarded_requests(pending, last):
    """T3 discardedRequests。"""
    if not pending:
        return 0
    final = pending[-1]
    reported = all(final['usage'][k] == last[k] for k in CODEX_FIELDS)
    return len(pending) - (1 if reported else 0)


def initial_codex_state():
    """T3 initialCodexScanState（另加自建的 reqStartMs：本次请求开始的时间，算输出速度用）。"""
    return {'model': '', 'speed': 'standard', 'sessionId': '', 'lastUsageSignature': None, 'lastCumulativeUsage': None,
            'malformedRecords': 0, 'sawSessionMeta': False, 'modelProvider': '', 'turnId': '', 'sawThreadSettings': False,
            'suppressingForkCopies': False, 'forkCopyAnchorMs': 0}


def valid_codex_state(s):
    """T3 CodexScanState 的结构校验（缓存里的续读状态坏了就整文件重读）。"""
    if not isinstance(s, dict):
        return False
    typ = {'model': str, 'speed': str, 'sessionId': str, 'malformedRecords': int, 'sawSessionMeta': bool, 'modelProvider': str,
           'turnId': str, 'sawThreadSettings': bool, 'suppressingForkCopies': bool}
    for k, t in typ.items():
        if not isinstance(s.get(k), t):
            return False
    if s['speed'] not in ('standard', 'fast', 'ultrafast') or not _num(s.get('forkCopyAnchorMs')):
        return False
    if s.get('lastUsageSignature') is not None and not isinstance(s['lastUsageSignature'], str):
        return False
    lc = s.get('lastCumulativeUsage')
    if lc is not None and read_codex_token_usage(lc) != lc:
        return False
    for q in s.get('unreportedRequests') or []:
        if not isinstance(q, dict) or read_codex_token_usage(q.get('usage')) is None or not _num(q.get('timestampMs')):
            return False
    return True


def _is_forked_session_meta(payload):
    """T3 isForkedSessionMeta。"""
    if isinstance(payload.get('forked_from_id'), str):
        return True
    src = payload.get('source')
    sub = src.get('subagent') if isinstance(src, dict) else None
    spawn = sub.get('thread_spawn') if isinstance(sub, dict) else None
    return isinstance(spawn, dict) and isinstance(spawn.get('parent_thread_id'), str)


def _is_fork_copy(state, ts):
    """T3 isForkCopy：分叉复制来的那一批之后的第一条用量结束屏蔽。"""
    if not state['suppressingForkCopies']:
        return False
    if ts - state['forkCopyAnchorMs'] < FORK_COPY_MAX_GAP_MS:
        state['forkCopyAnchorMs'] = ts
        return True
    state['suppressingForkCopies'] = False
    state['lastUsageSignature'] = None
    return False


def _codex_speed(tier):
    """T3 codexSpeed。"""
    if tier in ('priority', 'fast'):
        return 'fast'
    if tier == 'ultrafast':
        return 'ultrafast'
    return 'standard'


def _codex_attribution(state, ts):
    """T3 codexAttribution（另记请求耗时：本条时间减请求开始时间，自建，算输出速度用）。"""
    st = state.get('reqStartMs')
    ms = ts - st if _num(st) and 0 < ts - st <= 3600_000 else None
    return {'timestampMs': ts, 'model': state['model'], 'turnId': state['turnId'], 'speed': state['speed'],
            'sawThreadSettings': state['sawThreadSettings'], 'ms': ms}


def _codex_request_record(state, usage, attr):
    """T3 codexRequestRecord：Codex 的 input 含缓存部分，未缓存输入＝input−缓存读−缓存写。"""
    i, c, w = usage['input_tokens'], usage['cached_input_tokens'], usage['cache_write_input_tokens']
    return [attr['timestampMs'], attr['model'], state['sessionId'], i - c - w, c, w, usage['output_tokens'],
            usage['reasoning_output_tokens'], None, None, attr.get('turnId') or '', attr.get('ms')]


def parse_codex_record(parsed, state):
    """T3 parseCodexRecord（逐句移植）。返回这一行产生的用量记录。"""
    if not isinstance(parsed, dict):
        return []
    payload = parsed.get('payload')
    if not isinstance(payload, dict):
        return []
    ptype = payload.get('type')
    rtype = parsed.get('type')

    if rtype == 'session_meta':
        # 只认第一条 meta；复制来的祖先 meta 会把后面的记录归错会话
        if state['sawSessionMeta']:
            return []
        state['sawSessionMeta'] = True
        sid = payload.get('id')
        if sid is None:
            sid = payload.get('session_id')
        if isinstance(sid, str):
            state['sessionId'] = sid
        if isinstance(payload.get('model_provider'), str):
            state['modelProvider'] = payload['model_provider']
        meta_ts = parse_ts(parsed.get('timestamp'))
        if meta_ts is not None and _is_forked_session_meta(payload):
            state['suppressingForkCopies'] = True
            state['forkCopyAnchorMs'] = meta_ts
        return []

    if rtype == 'turn_context':
        state['requestLedgerActive'] = False
        m = payload.get('model')
        if isinstance(m, str):
            state['model'] = m
        tid = payload.get('turn_id')
        state['turnId'] = tid if isinstance(tid, str) and isinstance(m, str) and len(m) > 0 else ''
        return []

    # 逐次请求账（token_usage_record）含上下文快照可能漏掉的花费
    if rtype == 'token_usage_record':
        ts = parse_ts(parsed.get('timestamp'))
        usage = read_codex_token_usage(payload.get('usage'))
        if ts is None or usage is None or not state['model']:
            return []
        if _is_fork_copy(state, ts):  # 复制来的历史已在父会话计过
            return []
        rid = payload.get('response_id')
        if isinstance(rid, str) and rid:
            pending = state.get('unreportedRequests') or []
            state['requestLedgerActive'] = True
            for q in pending:
                state['lastCumulativeUsage'] = _advance(state['lastCumulativeUsage'], q['usage'])
            state['lastCumulativeUsage'] = _advance(state['lastCumulativeUsage'], usage)
            _date_baseline(state, ts)
            state.pop('unreportedRequests', None)
            out = [_codex_request_record(state, q['usage'], q) for q in pending]
            r = _codex_request_record(state, usage, _codex_attribution(state, ts))
            r[R_KEY] = _dumps(['codex', state['sessionId'], rid])
            out.append(r)
            return out
        state['requestLedgerActive'] = False
        state.setdefault('unreportedRequests', []).append(dict(_codex_attribution(state, ts), usage=usage))
        return []

    if ptype == 'thread_settings_applied':
        state['sawThreadSettings'] = True
        st = payload.get('thread_settings')
        if isinstance(st, dict):
            state['speed'] = _codex_speed(st.get('service_tier'))
        return []

    if ptype != 'token_count':
        return []
    info = payload.get('info')
    if not isinstance(info, dict):
        return []
    # 模型或时间就绪后 Codex 可能重发这些计数
    ts = parse_ts(parsed.get('timestamp'))
    if ts is None or not state['model']:
        return []
    last = read_codex_token_usage(info.get('last_token_usage'))
    raw_cum = info.get('total_token_usage')
    has_cum = raw_cum is not None
    cumulative = read_codex_token_usage(raw_cum) if has_cum else None

    if _is_fork_copy(state, ts):
        # 复制来的历史已在父会话计过，但它确立子会话的基线
        signature = None if last is None else _dumps(last)
        if cumulative is not None:
            _rebase(state, cumulative, ts)
        elif not has_cum and last is not None and signature != state['lastUsageSignature']:
            state['lastCumulativeUsage'] = _advance(state['lastCumulativeUsage'], last)
            _date_baseline(state, ts)
        state['lastUsageSignature'] = signature
        return []

    # 续接时上下文快照会回退、也会漏掉压缩的花费；有逐次请求账时以它为准
    if state.get('requestLedgerActive'):
        if cumulative is not None:
            _rebase(state, cumulative, ts)
        return []

    previous = state['lastCumulativeUsage']
    latest = state.get('latestCumulativeAtMs')
    pending = state.get('unreportedRequests')
    if cumulative is not None:  # 合法的快照即使配对的请求坏了也是下一个基线
        _rebase(state, cumulative, ts)
    if last is None or (has_cum and cumulative is None):
        state['malformedRecords'] += 1
        return []
    # 各项为 0 的计数报的是上下文大小，不是请求
    if last['input_tokens'] + last['output_tokens'] == 0:
        state['lastUsageSignature'] = None
        return []
    if cumulative is not None and previous is not None and all(cumulative[k] == previous[k] for k in CODEX_FIELDS):
        return []
    if last['total_tokens'] != last['input_tokens'] + last['output_tokens']:
        state['malformedRecords'] += 1
        return []

    signature = _dumps(last)
    unreported = []
    if cumulative is not None:
        # 在新的 Codex 进程里续接的会话，累计计数从这次请求重新开始
        restarted = all(cumulative[k] == last[k] for k in CODEX_FIELDS)
        seq_start = _dumps([ts, last])
        # 只有同一事件才是重放；之后的请求可以带着相同计数重新开始
        if restarted and seq_start == state.get('sequenceStartSignature'):
            state['lastCumulativeUsage'] = previous
            if latest is None:
                state.pop('latestCumulativeAtMs', None)
            return []
        lowered = previous is not None and any(cumulative[k] < previous[k] for k in CODEX_FIELDS)
        rewound = lowered and latest is not None and ts > latest
        if previous is None or rewound:
            reconciled = read_codex_token_usage({k: cumulative[k] - last[k] for k in CODEX_FIELDS}) is not None
        else:
            reconciled = all(cumulative[k] - previous[k] == last[k] for k in CODEX_FIELDS)
        if not restarted and not reconciled:
            bridged = _unreported_requests(previous, cumulative, last, pending)
            if bridged is None:
                # 比基线早的被拒快照是重放，不是下一个基线
                replayed = latest is not None and (ts < latest or (ts == latest and lowered and
                                                                   cumulative['input_tokens'] + cumulative['output_tokens'] > 0))
                if replayed:
                    state['lastCumulativeUsage'] = previous
                    if pending is not None:
                        state['unreportedRequests'] = pending
                state['malformedRecords'] += 1
                return []
            unreported = bridged
        elif not restarted and rewound:
            state['malformedRecords'] += _discarded_requests(pending, last)
        if restarted:
            state['sequenceStartSignature'] = seq_start
    else:
        if signature == state['lastUsageSignature']:
            return []
        advanced = _advance(previous, last)  # 推进已计的累计，免得之后重发的累计快照被当成新的
        if advanced is None:
            state['malformedRecords'] += 1
            return []
        state['lastCumulativeUsage'] = advanced
        _date_baseline(state, ts)
    state['lastUsageSignature'] = signature
    return [_codex_request_record(state, q['usage'], q) for q in unreported] + \
        [_codex_request_record(state, last, _codex_attribution(state, ts))]


# T3 codexUsageFormat.usageMarkers（元数据行也要过，它们带模型、服务档、会话）；后三个是自建的：轮次开始、结束、中断
CODEX_GATE = re.compile(rb'"token_count"|"token_usage_record"|"turn_context"|"thread_settings_applied"|"session_meta"'
                        rb'|"task_started"|"task_complete"|"turn_aborted"')
# 自建：请求开始（用户/开发者消息、工具输出）——只看行首，不解析整行
REQ_START_RE = re.compile(rb'^\{"timestamp":"([^"]{10,40})"[^{]{0,80}"type":"response_item","payload":\{"type":"([a-z_]+)"'
                          rb'(?:,"id":"[^"]{0,160}")?(?:,"role":"([a-z]+)")?')


def codex_side(parsed, state, side):
    """自建（T3 没有）：每轮（task_started / task_complete / turn_aborted 的用时、首字时间）、周额度样本（token_count.rate_limits）、
    请求开始时间（逐次请求账之后下一次请求从这里算起）。side=None 时只更新状态（未完的尾行）。"""
    if not isinstance(parsed, dict):
        return
    p = parsed.get('payload')
    if not isinstance(p, dict):
        return
    rtype, pt = parsed.get('type'), p.get('type')
    ts = parse_ts(parsed.get('timestamp'))
    if rtype == 'token_usage_record':
        if ts is not None:
            state['reqStartMs'] = ts
        return
    if rtype != 'event_msg' or side is None:
        if rtype == 'event_msg' and pt == 'task_started' and ts is not None:
            state['reqStartMs'] = ts
        return
    # 分叉时成批复制来的父会话历史（和 parse_codex_record 同一判断，但不移动锚点）：轮次、额度样本都不算
    fork_copy = ts is not None and state['suppressingForkCopies'] and ts - state['forkCopyAnchorMs'] < FORK_COPY_MAX_GAP_MS
    if pt in ('task_started', 'task_complete', 'turn_aborted') and fork_copy:
        return
    if pt == 'task_started':
        if ts is not None:
            state['reqStartMs'] = ts
        tid = p.get('turn_id')
        if isinstance(tid, str) and tid:
            sa = p.get('started_at')
            t0 = int(sa * 1000) if _num(sa) and sa > 0 else ts
            tr = side['turns'].setdefault(tid, [None, None, None, None, '', 0, ''])
            tr[0] = tr[0] or t0
            tr[6] = state['sessionId']
    elif pt in ('task_complete', 'turn_aborted'):
        tid = p.get('turn_id')
        if not (isinstance(tid, str) and tid):
            return
        tr = side['turns'].setdefault(tid, [None, None, None, None, '', 0, ''])
        sa = p.get('started_at')
        if tr[0] is None and _num(sa) and sa > 0:
            tr[0] = int(sa * 1000)
        tr[1] = ts
        d = p.get('duration_ms')
        tr[2] = int(d) if _num(d) and d >= 0 else tr[2]
        f = p.get('time_to_first_token_ms')
        tr[3] = int(f) if _num(f) and f >= 0 else tr[3]
        tr[4] = state['model'] or tr[4]
        tr[5] = 1 if pt == 'turn_aborted' else 0
        tr[6] = state['sessionId'] or tr[6]
    elif pt == 'token_count':
        rl = p.get('rate_limits')
        if not isinstance(rl, dict) or ts is None:
            return
        if fork_copy:
            return
        lid = rl.get('limit_id') if isinstance(rl.get('limit_id'), str) else ''
        for w in (rl.get('primary'), rl.get('secondary')):
            if not isinstance(w, dict) or w.get('window_minutes') != 10080:
                continue
            up, ra = w.get('used_percent'), w.get('resets_at')
            if not _num(up) or not _num(ra):
                continue
            prev = side['rlast'].get(lid)
            if prev and prev[0] == ra and prev[1] == up:
                continue
            side['rlast'][lid] = (ra, up)
            side['rl'].append([ts, lid, float(up), int(ra)])


def fnv1a(b):
    """T3 fnv1a（32 位）。"""
    h = 0x811c9dc5
    for x in b:
        h ^= x
        h = (h * 0x01000193) & 0xffffffff
    return h


def _guard_matches(fh, pos):
    """T3 guardMatches：续读偏移前的字节没变才续读，被改写的文件整文件重读。"""
    gl, o = pos.get('gl'), pos.get('o')
    if not isinstance(gl, int) or not isinstance(o, int) or gl <= 0 or gl > GUARD_LENGTH or gl > o:
        return False
    try:
        fh.seek(o - gl)
        w = fh.read(gl)
    except OSError:
        return False
    return len(w) == gl and fnv1a(w) == pos.get('gh')


def _open_zstd(path, fh):
    """.jsonl.zst：标准库 compression.zstd（Python 3.14），没有就 zstd -dc，再没有返回 (None, None)。"""
    mode = ZSTD_MODE
    if mode in (None, 'lib'):
        try:
            from compression import zstd as _z
            return _z.ZstdFile(fh, mode='rb'), None
        except ImportError:
            if mode == 'lib':
                return None, None
    if mode in (None, 'cli'):
        exe = shutil.which('zstd')
        if exe:
            proc = subprocess.Popen([exe, '-dc', '-q', '--', path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            return proc.stdout, proc
    return None, None


def read_codex_rollout(path, resume=None, side_init=None, tick=None):
    """T3 readTranscriptRecords（Codex 格式）。返回 dict（records、tail、malformed、pos、resumed、turns、rl），读失败返回 None，
    .zst 没有解压办法返回 'skip'。
    - 普通文件：从 resume 的偏移续读（偏移前 64 字节校验通过、续读状态合法才续读），每块 256 KB，只处理完整的行；
      最后没写完的一行用状态的副本另算（tail），下次从偏移处重读。
    - .jsonl.zst：整文件解压读，不续读（解压后的偏移对不上压缩文件）。
    - 超过 8 MB 的行：T3 用流式 JSON 只取需要的字段；这里只看行首 64 KB 判断是不是要的类型，是才整行解析，否则丢掉（结果相同，内存有界）。"""
    compressed = path.endswith('.jsonl.zst')
    try:
        fh = open(path, 'rb')
    except OSError:
        return None
    proc = None
    try:
        state, start, resumed = initial_codex_state(), 0, False
        if (not compressed and isinstance(resume, dict) and isinstance(resume.get('o'), int) and resume['o'] > 0
                and valid_codex_state(resume.get('cs')) and _guard_matches(fh, resume)):
            state, start, resumed = json.loads(_dumps(resume['cs'])), resume['o'], True
        side = {'turns': side_init if (resumed and side_init is not None) else {}, 'rl': [], 'rlast': {}}
        if compressed:
            stream, proc = _open_zstd(path, fh)
            if stream is None:
                return 'skip'
        else:
            fh.seek(start)
            stream = fh
        records = []

        def handle(line, st, out, sd):
            if line.endswith(b'\r'):
                line = line[:-1]
            if line.startswith(b'{"timestamp"'):
                m = REQ_START_RE.match(line[:512])
                if m:
                    pt, role = m.group(2), m.group(3)
                    if pt.endswith(b'_output') or (pt == b'message' and role in (b'user', b'developer')):
                        t = parse_ts(m.group(1).decode('ascii', 'ignore'))
                        if t is not None:
                            st['reqStartMs'] = t
                    return
            if not CODEX_GATE.search(line):
                return
            try:
                parsed = json.loads(line)
            except Exception:
                return
            out.extend(parse_codex_record(parsed, st))
            codex_side(parsed, st, sd)

        pend, plen, big, keep = [], 0, False, True
        resume_off = scan_off = start
        while True:
            chunk = stream.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            n, pos = len(chunk), 0
            while pos < n:
                nl = chunk.find(b'\n', pos)
                seg = chunk[pos:] if nl < 0 else chunk[pos:nl]
                if pend or nl < 0:
                    if not big and plen + len(seg) > STREAMING_THRESHOLD_BYTES:
                        big = True
                        head = b''.join(pend)[:HEAD_BYTES]
                        keep = bool(CODEX_GATE.search(head if len(head) >= HEAD_BYTES else head + seg[:HEAD_BYTES - len(head)]))
                        if not keep:
                            pend = []
                    if keep:
                        pend.append(seg)
                    plen += len(seg)
                if nl < 0:
                    break
                if pend or big:
                    if keep:
                        handle(b''.join(pend), state, records, side)
                    pend, plen, big, keep = [], 0, False, True
                else:
                    handle(seg, state, records, side)
                pos = nl + 1
                resume_off = scan_off + pos
            scan_off += n
            if tick and not compressed:
                tick(n)
            time.sleep(0)
        if proc is not None:
            proc.stdout.close()
            if proc.wait(timeout=30) != 0:
                return None
        if tick and compressed:
            try:
                tick(os.fstat(fh.fileno()).st_size)
            except OSError:
                pass
        # 未完的尾行用状态副本解析，下次从 resume_off 重读
        enc = _dumps(state)
        tail_state, tail = json.loads(enc), []
        if pend and keep:
            handle(b''.join(pend), tail_state, tail, None)
        gl = 0 if compressed else min(GUARD_LENGTH, resume_off)
        gh = 0
        if gl > 0:
            fh.seek(resume_off - gl)
            gh = fnv1a(fh.read(gl))
        return {'records': records, 'tail': tail, 'malformed': tail_state['malformedRecords'], 'resumed': resumed,
                'pos': {'o': 0 if compressed else resume_off, 'gl': gl, 'gh': gh, 'cs': json.loads(enc)},
                'turns': side['turns'], 'rl': side['rl']}
    except Exception as e:
        log('读取 Codex 记录失败', os.path.basename(path), repr(e))
        return None
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        fh.close()


def list_transcript_files(root, since_ms):
    """T3 listTranscriptFiles：递归列出 .jsonl / .jsonl.zst，只要修改时间不早于 since_ms 的；返回 (文件[(路径, 大小, 修改时间纳秒)], 全部候选路径, 失败数)。"""
    files, walked, failed = [], [], 0

    def walk(d):
        nonlocal failed
        try:
            ents = sorted(os.scandir(d), key=lambda x: x.name)
        except FileNotFoundError:
            return
        except OSError:
            failed += 1
            return
        for e in ents:
            try:
                if e.is_dir(follow_symlinks=False):
                    walk(e.path)
                    continue
            except OSError:
                continue
            if e.name.endswith('.jsonl') or e.name.endswith('.jsonl.zst'):
                walked.append(e.path)
                try:
                    st = os.stat(e.path)
                except FileNotFoundError:
                    continue
                except OSError:
                    failed += 1
                    continue
                if st.st_mtime_ns // 1_000_000 >= since_ms:
                    files.append((e.path, st.st_size, st.st_mtime_ns))
    if root and os.path.isdir(root):
        walk(root)
    return files, walked, failed


def merge_usage_copies(held, copy):
    """T3 mergeUsageCopies：同一键的副本，每一类 token 取最大、提供方费用取大的，时间、模型、会话用第一份的。"""
    a, b = held, copy
    hc, cc = held[R_COST], copy[R_COST]
    cost = (hc if hc is not None else cc) if (hc is None or cc is None) else max(hc, cc)
    if b[R_U] <= a[R_U] and b[R_C] <= a[R_C] and b[R_CW] <= a[R_CW] and b[R_O] <= a[R_O] and b[R_R] <= a[R_R] and cost == hc:
        return held
    r = list(held)
    for i in (R_U, R_C, R_CW, R_O, R_R):
        r[i] = max(a[i], b[i])
    r[R_COST] = cost
    if r[R_MS] is None:
        r[R_MS] = copy[R_MS]
    return r


def dedupe_within_file(records):
    """T3 dedupeWithinFile。"""
    kept, first = [], {}
    for r in records:
        k = r[R_KEY]
        if k is not None:
            i = first.get(k)
            if i is not None:
                kept[i] = merge_usage_copies(kept[i], r)
                continue
            first[k] = len(kept)
        kept.append(r)
    return kept


class CodexScanner:
    """Codex rollout 的增量扫描（T3 UsageService.scanTranscriptDir + readFileRecords + usageScanCache 的条目）。
    条目：{s 大小, m 修改时间纳秒, r 记录, t 尾行记录, mr 坏记录数, pos 续读位置, turns 每轮, rl 周额度样本}"""

    def __init__(self, root_fn):
        self.root_fn = root_fn
        self.files = {}
        self.lock = threading.RLock()
        self.rlock = threading.Lock()
        self.walked, self.live = [], set()
        self.failed = self.skipped = 0
        self.progress = {'files': 0, 'totalFiles': 0, 'bytes': 0, 'totalBytes': 0}
        self.ver = 0

    def refresh(self, since_ms, now_ms):
        with self.rlock:
            root = self.root_fn()
            listed, walked, failed = list_transcript_files(root, since_ms)
            work = []
            for path, size, mt in listed:
                c = self.files.get(path)
                if c and c['s'] == size and c['m'] == mt:
                    continue
                work.append((path, size, mt, c))
            work.sort(key=lambda w: -w[2])
            self.progress = {'files': 0, 'totalFiles': len(work), 'bytes': 0,
                             'totalBytes': sum(w[1] - (w[3]['s'] if w[3] and w[1] > w[3]['s'] and not w[0].endswith('.zst') else 0)
                                               for w in work)}

            def tick(n):
                self.progress['bytes'] += n
            skipped = 0
            live = {p for p, _, _ in listed}
            changed = False
            for path, size, mt, c in work:
                # 只有变大的文件才续读；同样大小但修改时间变了、变小了，都是改写，整文件重读
                resume = c['pos'] if c and size > c['s'] else None
                side_init = {k: list(v) for k, v in (c.get('turns') or {}).items()} if resume else None
                res = read_codex_rollout(path, resume, side_init, tick)
                self.progress['files'] += 1
                if res == 'skip':
                    skipped += 1
                    continue
                if res is None:  # 读失败不当成空文件，保留上次的
                    failed += 1
                    continue
                base = c['r'] if res['resumed'] and c else []
                ent = {'s': size, 'm': mt, 'r': dedupe_within_file(base + res['records']), 't': res['tail'], 'mr': res['malformed'],
                       'pos': res['pos'], 'turns': res['turns'],
                       'rl': ((c.get('rl') or []) if res['resumed'] and c else []) + res['rl']}
                with self.lock:
                    self.files[path] = ent
                    self.ver += 1
                    # 读成功的压缩包替换原文件保存的记录（原文件已不在）
                    if path.endswith('.jsonl.zst') and path[:-4] not in live:
                        self.files.pop(path[:-4], None)
                changed = True
            with self.lock:
                self.walked, self.live, self.failed, self.skipped = walked, live, failed, skipped
                cut = now_ms - RETENTION_DAYS * 86400_000
                for p in [p for p, e in self.files.items() if e['m'] // 1_000_000 < cut]:  # T3 pruneScanCache
                    del self.files[p]
                    changed = True
            return changed

    def file_records(self, since_ms, retention_cut):
        """参与统计的文件：这次列出的（修改时间在窗口内）按列出顺序，再加缓存里已删掉、修改时间在保留期和窗口内、
        而且在当前扫描目录下的（T3 UsageService：删掉的记录照样算，但只算 isRetained＝在这个目录里的；缓存按路径存，
        换过根目录（测试改 HOME、CODEX_HOME）时别的目录留下的条目不能算进来）。"""
        root = self.root_fn() or ''
        roots = tuple({os.path.join(root, ''), os.path.join(os.path.realpath(root), '')}) if root else ()
        with self.lock:
            out = []
            seen = set()
            for p in self.walked:
                e = self.files.get(p)
                if e and p in self.live:
                    out.append((p, e))
                    seen.add(p)
            keep_since = max(since_ms, retention_cut)
            for p in sorted(self.files):
                if (p not in seen and p not in self.live and roots and p.startswith(roots)
                        and self.files[p]['m'] // 1_000_000 >= keep_since):
                    out.append((p, self.files[p]))
            return out


def shared_sessions(file_records):
    """T3 sharedSessions：同一会话出现在多个文件里（移走、压缩后两处都有）的会话 ID。"""
    first, shared = {}, set()
    for idx, recs in enumerate(file_records):
        prev = ''
        for r in recs:
            sid = r[R_SID]
            if sid == prev or not sid:
                continue
            prev = sid
            f = first.get(sid)
            if f is None:
                first[sid] = idx
            elif f != idx:
                shared.add(sid)
    return shared


# ================================================================ Antigravity（移植 antigravityUsageReader.ts）
class _Big:
    """protobuf 里超过 2^53 的整数（T3 用 bigint，numberAt 当 0）。"""
    __slots__ = ('v',)

    def __init__(self, v):
        self.v = v


def agy_fields(b):
    """T3 fields：解一层 protobuf；只留 varint 和长度前缀的字段，定长字段跳过。坏数据抛 ValueError。
    长度前缀的字段是 memoryview（T3 subarray，不复制；对话库一行能有几十 MB）。"""
    b = b if isinstance(b, memoryview) else memoryview(b)
    off, n, res = 0, len(b), {}

    def varint():
        nonlocal off
        v, shift = 0, 0
        while shift < 70:
            if off >= n or (shift == 63 and b[off] > 1):
                raise ValueError('Invalid Antigravity protobuf varint')
            x = b[off]
            off += 1
            v |= (x & 127) << shift
            if x < 128:
                return v if v <= 9007199254740991 else _Big(v)
            shift += 7
        raise ValueError('Invalid Antigravity protobuf varint')
    while off < n:
        tag = varint()
        if isinstance(tag, _Big):
            raise ValueError('Invalid protobuf field')
        num, wire = tag // 8, tag % 8
        if num == 0:
            raise ValueError('Invalid protobuf field')
        if wire == 0:
            val = varint()
        elif wire in (1, 2, 5):
            ln = varint() if wire == 2 else (8 if wire == 1 else 4)
            if isinstance(ln, _Big):
                raise ValueError('Invalid protobuf field length')
            if ln > n - off:
                raise ValueError('Truncated protobuf field')
            val = b[off:off + ln]
            off += ln
            if wire != 2:
                continue
        else:
            raise ValueError('Unsupported protobuf wire type')
        res.setdefault(num, []).append(val)
    return res


def _agy_num(f, k):
    v = (f.get(k) or [None])[0]
    return v if isinstance(v, int) else 0


def _agy_bytes(f, k):
    v = (f.get(k) or [None])[0]
    return v if isinstance(v, memoryview) else None


def _agy_nested(f, k):
    v = _agy_bytes(f, k)
    return {} if v is None else agy_fields(v)


def _agy_text(f, k):
    v = _agy_bytes(f, k)
    return '' if v is None else str(v, 'utf-8').strip()  # T3 TextDecoder fatal：坏 UTF-8 抛错


def _agy_timestamp(f):
    s = _agy_num(f, 1)
    return s * 1000 + _agy_num(f, 2) // 1_000_000 if s > 0 else None


# T3 MODEL_IDS：没有模型名的记录离线兜底（新的从扫到的元数据里学）
MODEL_IDS = {
    246: 'gemini-2.5-pro', 312: 'gemini-2.5-flash', 313: 'gemini-2.5-flash-thinking', 329: 'gemini-2.5-flash-thinking',
    330: 'gemini-2.5-flash-lite', 281: 'claude-sonnet-4', 282: 'claude-sonnet-4', 290: 'claude-opus-4', 291: 'claude-opus-4',
    333: 'claude-sonnet-4-5', 334: 'claude-sonnet-4-5', 340: 'claude-haiku-4-5', 341: 'claude-haiku-4-5', 342: 'gpt-oss-120b-medium',
    1026: 'claude-opus-4-6', 1035: 'claude-sonnet-4-6', 1016: 'gemini-3.1-pro', 1036: 'gemini-3.1-pro', 1037: 'gemini-3.1-pro',
    1018: 'gemini-3-flash-preview', 1084: 'gemini-3-flash-preview', 1047: 'gemini-3-flash-preview', 1020: 'gemini-3.5-flash-medium',
    1021: 'gemini-3.1-flash-image', 1050: 'gemini-3.1-flash-lite', 1071: 'gemini-3.6-flash-high', 1072: 'gemini-3.6-flash-medium',
    1073: 'gemini-3.6-flash-low', 1132: 'gemini-3.5-flash-high', 1133: 'gemini-3.5-flash-high', 1187: 'gemini-3.5-flash-extra-low',
    1196: 'gemini-3.6-flash', 1266: 'gemini-3.6-flash', 1298: 'gemini-3.7-flash-high', 1299: 'gemini-3.7-flash-medium',
    1300: 'gemini-3.7-flash-low', 1301: 'gemini-3.7-flash-tiered', 1318: 'gemini-3.8-flash-high', 1319: 'gemini-3.8-flash-medium',
    1320: 'gemini-3.8-flash-low', 1322: 'gemini-3.8-flash-tiered', 1402: 'claude-opus-5-5', 1417: 'gemini-4-argon',
    1418: 'gemini-4-argon', 1419: 'gemini-4-argon',
}
_CONCRETE = re.compile(r'^(?:gemini-\d|claude-(?:sonnet|opus|haiku)-\d|gpt-)')
_ROUTER = re.compile(r'^gemini-3-flash-(?:agent(?:-[ab])?|[ab])$')


def agy_model_name(name):
    """T3 modelName：显示名 → 模型 ID（“Gemini 3.8 Flash (Low)” → gemini-3.8-flash-low）。"""
    if not name:
        return ''
    s = re.sub(r'\s*\(([^)]*)\)\s*$', r'-\1', name.lower(), count=1)
    s = re.sub(r'\s+', '-', s)
    if s.startswith('claude-'):
        return re.sub(r'^claude-(\d+(?:\.\d+)?)-(sonnet|opus|haiku)', r'claude-\2-\1', s, count=1).replace('.', '-')
    return s


def agy_is_concrete(m):
    """T3 isConcreteModel。"""
    return bool(_CONCRETE.match(m)) and not _ROUTER.match(m)


_MISSING = object()


def _remember_model(models, mid, model):
    """T3 rememberModel：同一数字 ID 对上两个名字就作废（None）。"""
    prev = models.get(mid, _MISSING)
    models[mid] = model if prev is _MISSING or prev == model else None


def agy_metadata(blob, step):
    """T3 metadata：一行 gen_metadata（step=False）或 steps.metadata（step=True）。"""
    root = agy_fields(blob)
    if not step and _agy_bytes(root, 1) is None:
        raise ValueError('Missing Antigravity generation metadata')
    data = root if step else _agy_nested(root, 1)
    model = _agy_nested(data, 24) if step else data
    usage = _agy_bytes(data, 9 if step else 4)
    # 复制 usage 那一小段（T3 usage.slice()）：留着的 usages 不能拖住整行的大块数据
    primary = None if usage is None else agy_fields(bytes(usage))
    usages = [] if primary is None else [primary]
    for retry in data.get(28 if step else 17) or []:
        if not isinstance(retry, memoryview):
            raise ValueError('Invalid retry metadata')
        ru = _agy_bytes(agy_fields(retry), 2)
        if ru is not None:
            usages.append(agy_fields(bytes(ru)))
    names = [agy_model_name(_agy_text(model, 12 if step else 19)), agy_model_name(_agy_text(model, 8 if step else 21))]
    m = next((x for x in names if agy_is_concrete(x)), None)
    if m is None:
        m = next((x for x in names if x and not re.match(r'^model[_-]', x)), '')
    mid = _agy_num(model, 1 if step else 3) or (0 if primary is None else _agy_num(primary, 1))
    if step:
        ts = _agy_timestamp(_agy_nested(data, 8)) or _agy_timestamp(_agy_nested(data, 1))
    else:
        ts = _agy_timestamp(_agy_nested(_agy_nested(data, 9), 4))
    return {'model': m, 'modelId': mid, 'ts': ts, 'usages': usages}


class _TmpCopyConn(sqlite3.Connection):
    """读临时副本的连接：关闭时连同临时目录一起删掉。"""
    _tmpdir = None

    def close(self):
        try:
            super().close()
        finally:
            d, self._tmpdir = self._tmpdir, None
            if d:
                shutil.rmtree(d, ignore_errors=True)


def _tmp_root():
    r = NS.get('RUN_DIR') if isinstance(NS, dict) else None
    return os.path.join(r, 'agy-db-tmp') if r else None


def _open_sqlite_ro(path):
    """只读打开 agy 的库。偏离 T3（只是打开方式，读到的内容一样）：T3 用 readonly 直接打开。
    这里库旁边没有 -wal、-journal（agy 没开着它）时加 immutable=1，不加锁、不留下 -shm/-wal 文件（G10 只读）；
    有 -wal 或 -journal 时，只读打开也会刷新甚至新建原库旁的 -shm（复核 REVIEW-U 第 1 条），所以先把库和 -wal 拷到
    .run/agy-db-tmp 下的临时目录（先拷库、再拷 -wal），读副本，连接关闭时删掉副本。"""
    busy = os.path.exists(path + '-wal') or os.path.exists(path + '-journal')
    if not busy:
        uri = 'file:' + urllib.parse.quote(path) + '?mode=ro&immutable=1'
        return sqlite3.connect(uri, uri=True, timeout=0.1, isolation_level=None, check_same_thread=False)
    import tempfile
    root = _tmp_root()
    if root:
        os.makedirs(root, exist_ok=True)
    d = tempfile.mkdtemp(prefix='db-', dir=root)
    try:
        cp = os.path.join(d, os.path.basename(path))
        shutil.copyfile(path, cp)
        if os.path.exists(path + '-wal'):
            shutil.copyfile(path + '-wal', cp + '-wal')
        con = sqlite3.connect(cp, timeout=0.1, isolation_level=None, check_same_thread=False, factory=_TmpCopyConn)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    con._tmpdir = d
    return con


def agy_read_db(path, sid, fallback_ts):
    """T3 readDatabase：读一个对话库。不是对话库（如 conversation_summaries.db）返回 None；坏了抛错。
    自建的附加：每一步的时间（算相邻两步的时间差＝请求耗时）、用户输入步（step_type 14，算轮数）。"""
    con = _open_sqlite_ro(path)
    try:
        con.execute('PRAGMA busy_timeout = 100')
        con.execute('BEGIN')
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if 'gen_metadata' not in tables and 'steps' not in tables:
            return None
        cols = {r[1] for r in con.execute('PRAGMA table_info(steps)')} if 'steps' in tables else set()

        def read_meta(query, page, step):
            out, after = [], float('-inf')
            while True:
                rows = con.execute(query, (after, page)).fetchall()
                for row in rows:
                    if step and row[2] is None:
                        continue
                    idx = row[1]
                    if isinstance(idx, bool) or not isinstance(idx, (int, float)):
                        raise ValueError('bad idx')
                    if not isinstance(row[2], bytes):
                        raise ValueError('Invalid Antigravity metadata blob')
                    ent = agy_metadata(row[2], step)
                    if step:
                        ent['type'] = row[3] if len(row) > 3 else None
                    out.append((idx, ent))
                if len(rows) < page:
                    break
                after = rows[-1][0]
                time.sleep(0)
            out.sort(key=lambda x: x[0])
            return out
        gens = read_meta('SELECT rowid AS row_id, idx, data FROM gen_metadata WHERE rowid > ? ORDER BY rowid LIMIT ?',
                         GENERATION_PAGE_ROWS, False) if 'gen_metadata' in tables else []
        traj_ts = None
        if 'trajectory_metadata_blob' in tables:
            for (data,) in con.execute('SELECT data FROM trajectory_metadata_blob'):
                if traj_ts is None:
                    if not isinstance(data, bytes):
                        raise ValueError('Invalid Antigravity metadata blob')
                    traj_ts = _agy_timestamp(_agy_nested(agy_fields(data), 2))
        st_col = ', step_type' if 'step_type' in cols else ''
        steps = read_meta(f'SELECT rowid AS row_id, idx, metadata AS data{st_col} FROM steps WHERE rowid > ? ORDER BY rowid LIMIT ?',
                          STEP_PAGE_ROWS, True) if 'steps' in tables else []
        models = {}
        for _, ent in gens + steps:
            if ent['modelId'] > 0 and agy_is_concrete(ent['model']):
                _remember_model(models, ent['modelId'], ent['model'])
        cands = agy_usage_candidates(sid, gens, steps, traj_ts, fallback_ts)
        inputs = [ent['ts'] for _, ent in steps if ent.get('type') == 14 and ent['ts']]
        return {'cands': cands, 'models': [[k, v] for k, v in models.items()], 'inputs': inputs}
    finally:
        try:
            con.execute('ROLLBACK')
        except Exception:
            pass
        con.close()


def agy_usage_candidates(sid, gens, steps, traj_ts, fallback_ts):
    """T3 usageCandidates。候选：[记录, 模型数字 ID, 模型可信度, 去重键列表, 时间可信度]。记录的 R_MS（自建）＝这一步减上一步的时间。"""
    out = []
    gen_by_idx = {}
    for idx, ent in gens:
        gen_by_idx[idx] = ent
    for source, entries in (('step', steps), ('generation', gens)):
        for index, (idx, ent) in enumerate(entries):
            ms = None
            if source == 'step' and index > 0 and ent['ts'] and entries[index - 1][1]['ts']:
                d = ent['ts'] - entries[index - 1][1]['ts']
                ms = d if 0 < d <= 3600_000 else None
            for ui, usage in enumerate(ent['usages']):
                model_id = _agy_num(usage, 1) or ent['modelId']
                gen = gen_by_idx.get(idx) if source == 'step' else None

                def matches(md):
                    return model_id == 0 or md['modelId'] == model_id
                if matches(ent) and ent['model']:
                    model = ent['model']
                elif gen is not None and matches(gen):
                    model = gen['model']
                else:
                    model = ''
                output = max(_agy_num(usage, 3), _agy_num(usage, 9) + _agy_num(usage, 10))
                u, c, cw = _agy_num(usage, 2), _agy_num(usage, 5), _agy_num(usage, 4)
                if u + c + cw + output == 0:
                    continue
                keys = [f'antigravity:{k}:{_agy_text(usage, k)}' for k in (11, 12, 7) if _agy_text(usage, k)]
                ts = ent['ts'] if ent['ts'] is not None else (traj_ts if traj_ts is not None else fallback_ts)
                rec = [ts, model, sid, u, c, cw, output, min(output, _agy_num(usage, 9)),
                       keys[0] if keys else f'antigravity:{sid}:{source}:{index}:{ui}', None, '', ms]
                out.append([rec, model_id, 4 if agy_is_concrete(model) else (1 if model else 0), keys,
                            2 if ent['ts'] is not None else (1 if traj_ts is not None else 0)])
    return out


def agy_conversation_dirs(home, env):
    """T3 AntigravityUsage.dataRoots＋scan：ANTIGRAVITY_DATA_DIR（逗号分隔）或默认几个目录；每个目录下有 conversations 就读它。
    偏离 T3（工作台已有链路）：T3 另读它给自己实例建的 antigravity-acp；工作台的 agy 实例用 antigravity-cwb（第十六轮 P7），这里换成它。"""
    conf = [x.strip() for x in (env.get('ANTIGRAVITY_DATA_DIR') or '').split(',') if x.strip()]
    g = os.path.join(home, '.gemini')
    roots = conf or [os.path.join(g, n) for n in ('antigravity', 'antigravity-cli', 'antigravity-ide', 'antigravity-backup')] + \
        [os.path.join(home, '.config', 'antigravity')]
    roots = roots + [os.path.join(g, 'antigravity-cwb')]
    out = []
    for r in roots:
        r = os.path.realpath(os.path.expanduser(r))
        nested = os.path.join(r, 'conversations')
        d = os.path.realpath(nested if os.path.exists(nested) else r)
        if d not in out:
            out.append(d)
    return out


class AgyScanner:
    """Antigravity 对话库（T3 readAntigravityUsage＋makeAntigravityUsageCache）：按库的大小、修改时间、ctime 和 -wal 的同样三项做指纹，
    指纹没变就用缓存的解码结果；跨库按并查集合并同一次生成。"""

    def __init__(self, dirs_fn):
        self.dirs_fn = dirs_fn
        self.dbs = {}       # 规范路径 -> {fp, sid, data（None＝不是对话库）, root}
        self.order = []     # 这次扫到的库（遍历顺序）
        self.errors = []
        self.lock = threading.RLock()
        self.rlock = threading.Lock()
        self.progress = {'files': 0, 'totalFiles': 0, 'bytes': 0, 'totalBytes': 0}
        self.ver = 0

    @staticmethod
    def _fp(path):
        st = os.stat(path)
        part = f'{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}'
        try:
            w = os.stat(path + '-wal')
            wal = f'{w.st_size}:{w.st_mtime_ns}:{w.st_ctime_ns}'
        except OSError:
            wal = '-'
        return st, part + '/' + wal

    def refresh(self):
        with self.rlock:
            found, errors = [], []

            def walk(d, root):
                try:
                    names = sorted(os.listdir(d))
                except FileNotFoundError:
                    return
                except OSError:
                    errors.append(d)
                    return
                for name in names:
                    p = os.path.join(d, name)
                    if os.path.islink(p):  # 和目录项类型一致：符号链接既不是文件也不是目录
                        continue
                    if os.path.isdir(p):
                        walk(p, root)
                    elif name.endswith('.db'):
                        found.append((p, root))
            for d in self.dirs_fn():
                walk(d, d)
            work, visited, failed = [], [], set()
            for p, root in found:
                canon = os.path.realpath(p)
                if canon in visited:
                    continue
                visited.append(canon)
                try:
                    st, fp = self._fp(p)
                except OSError:
                    errors.append(p)
                    continue
                c = self.dbs.get(canon)
                if c and c['fp'] == fp:
                    continue
                work.append((p, canon, root, st, fp))
            self.progress = {'files': 0, 'totalFiles': len(work), 'bytes': 0, 'totalBytes': sum(w[3].st_size for w in work)}
            changed = False
            for p, canon, root, st, fp in work:
                sid = os.path.basename(p)[:-3]
                try:
                    data = agy_read_db(p, sid, st.st_mtime_ns // 1_000_000)
                except Exception as e:
                    log('读取 Antigravity 对话库失败', os.path.basename(p), repr(e))
                    errors.append(p)
                    data = _MISSING
                self.progress['files'] += 1
                self.progress['bytes'] += st.st_size
                if data is _MISSING:
                    failed.add(canon)  # T3：读失败的库这次不计（缓存里上次的解码结果留着，下次指纹没变也会重读）
                    continue
                with self.lock:
                    self.dbs[canon] = {'fp': fp, 'sid': sid, 'data': data, 'root': root}
                    self.ver += 1
                changed = True
                time.sleep(0)
            with self.lock:
                for k in [k for k in self.dbs if k not in visited]:  # T3：没访问到的库从缓存里删掉
                    del self.dbs[k]
                    changed = True
                self.order, self.errors = [k for k in visited if k not in failed], errors
            return changed

    def collect(self, since_ms):
        """T3 readAntigravityUsage 的合并部分：先学全部库里的模型名，再按去重键并查集合并；返回 [(会话 ID, 记录)]、用户输入步。"""
        dirs = set(self.dirs_fn())
        with self.lock:
            # 只算当前数据目录下的库（缓存里可能有换 HOME 之前别的目录的；首扫完成前 order 来自缓存）
            items = [(k, self.dbs[k]) for k in self.order if k in self.dbs and self.dbs[k]['data'] and self.dbs[k].get('root') in dirs]
        models = {}
        for _, db in items:
            for mid, m in db['data']['models']:
                _remember_model(models, mid, m)
        groups, ident = [], {}

        def find(i):
            r = i
            while groups[r]['parent'] != r:
                r = groups[r]['parent']
            while i != r:
                nxt = groups[i]['parent']
                groups[i]['parent'] = r
                i = nxt
            return r

        def merge(left, right):
            a, b = find(left), find(right)
            if a == b:
                return a
            if groups[a]['size'] < groups[b]['size']:
                a, b = b, a
            t, s = groups[a], groups[b]
            first = t if t['owner'] < s['owner'] else s
            best_model = first if s['mq'] == t['mq'] else (s if s['mq'] > t['mq'] else t)
            best_time = s if (s['tq'] > t['tq'] or (s['tq'] == t['tq'] and s['rec'][R_TS] < t['rec'][R_TS])) else t
            x, y = t['rec'], s['rec']
            rec = list(first['rec'])
            rec[R_MODEL] = best_model['rec'][R_MODEL]
            rec[R_TS] = best_time['rec'][R_TS]
            for i in (R_U, R_C, R_CW, R_O, R_R):
                rec[i] = max(x[i], y[i])
            if rec[R_MS] is None:  # 自建：请求耗时取有的那份
                rec[R_MS] = x[R_MS] if x[R_MS] is not None else y[R_MS]
            t['rec'] = rec
            t['tq'], t['mq'] = best_time['tq'], best_model['mq']
            t['owner'], t['file'] = first['owner'], first['file']
            t['size'] += s['size']
            s['parent'] = a
            return a
        for fi, (_, db) in enumerate(items):
            for n, (rec, mid, mq, keys, tq) in enumerate(db['data']['cands']):
                learned = models.get(mid, _MISSING)
                fallback = None if learned is None else MODEL_IDS.get(mid)
                lv = None if learned is _MISSING else learned
                model = (rec[R_MODEL] if mq == 4 else '') or lv or fallback or rec[R_MODEL] or \
                    (f'antigravity-model-{mid}' if mid > 0 else 'antigravity-unknown')
                r = list(rec)
                r[R_MODEL] = model
                i = len(groups)
                groups.append({'rec': r, 'mq': 4 if mq == 4 else (3 if lv else (2 if fallback else mq)), 'tq': tq,
                               'parent': i, 'size': 1, 'owner': i, 'file': fi})
                for k in keys:
                    j = ident.get(k)
                    if j is not None:
                        merge(i, j)
                    ident[k] = i
                if n % 256 == 255:
                    time.sleep(0)
        out = []
        for i, g in enumerate(groups):
            if g['parent'] == i and g['rec'][R_TS] >= since_ms:
                out.append(g['rec'])
        inputs = {}
        for _, db in items:  # 同一对话在几个目录里都有（备份）时按时间去重
            inputs.setdefault(db['sid'], set()).update(db['data'].get('inputs') or [])
        return out, inputs


def agy_titles(dirs):
    """agy 的对话标题：各数据目录的 conversation_summaries.db（title，没有用 preview）。只读，读不到就算了。"""
    out = {}
    for d in dirs:
        p = os.path.join(os.path.dirname(d) if os.path.basename(d) == 'conversations' else d, 'conversation_summaries.db')
        if not os.path.exists(p):
            continue
        try:
            con = _open_sqlite_ro(p)
            try:
                con.execute('PRAGMA busy_timeout = 100')
                for cid, title, prev in con.execute('SELECT conversation_id, title, preview FROM conversation_summaries'):
                    t = ' '.join(str(title or prev or '').split())[:60]
                    if cid and t and cid not in out:
                        out[cid] = t
            finally:
                con.close()
        except Exception:
            continue
    return out


# ================================================================ 工作台自己的对话记录（data/conv，P 线写，只读）
CONV_SID_RE = re.compile(r'^[0-9a-fA-F-]{36}\.jsonl$')


class ConvReader:
    """读 data/conv/<sid>.jsonl：对话信息（prov、native、title、第一条提问）和每轮 turn_end。按偏移增量读。"""

    def __init__(self, dir_fn):
        self.dir_fn = dir_fn
        self.files = {}
        self.lock = threading.Lock()

    def refresh(self):
        with self.lock:
            d = self.dir_fn()
            try:
                names = [n for n in os.listdir(d) if CONV_SID_RE.match(n)]
            except OSError:
                names = []
            live = set()
            for n in names:
                p = os.path.join(d, n)
                live.add(p)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                e = self.files.get(p)
                if e and e['s'] == st.st_size and e['m'] == st.st_mtime_ns:
                    continue
                if not e or st.st_size < e['o']:
                    e = {'s': 0, 'm': 0, 'o': 0, 'info': {'sid': n[:-6], 'prov': '', 'native': '', 'title': '', 'first': '', 'turns': []}}
                try:
                    with open(p, 'rb') as f:
                        f.seek(e['o'])
                        data = f.read(st.st_size - e['o'])
                except OSError:
                    continue
                end = data.rfind(b'\n')
                if end >= 0:
                    self._parse(e['info'], data[:end + 1])
                    e['o'] += end + 1
                e['s'], e['m'] = st.st_size, st.st_mtime_ns
                self.files[p] = e
            for p in [p for p in self.files if p not in live]:
                del self.files[p]

    @staticmethod
    def _parse(info, data):
        for line in data.split(b'\n'):
            if not line.strip():
                continue
            if not (b'"meta"' in line or b'"turn_end"' in line or (not info['first'] and b'"user"' in line)):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get('t')
            if t == 'meta':
                for k in ('prov', 'native', 'title'):
                    if isinstance(d.get(k), str) and d[k]:
                        info[k] = d[k]
            elif t == 'turn_end':
                info['turns'].append({k: d.get(k) for k in ('ts', 'turnId', 'provider', 'model', 'startedAt', 'firstTokenAt',
                                                           'endedAt', 'status', 'usage', 'cost')})
            elif t == 'new' and not info['first']:
                it = d.get('it') if isinstance(d.get('it'), dict) else {}
                if it.get('k') == 'user':
                    info['first'] = ' '.join(str(it.get('text') or '').split())[:60]

    def convs(self):
        with self.lock:
            return [dict(e['info'], turns=list(e['info']['turns'])) for e in self.files.values()]


# ================================================================ 汇总（移植 usageAggregation.ts）
# 汇总用的记录：用量记录（R_*）后面再加 [定价费用或 None, 缓存省下的钱]
A_LIST, A_SAVED = 12, 13


class Aggregator:
    """T3 UsageAggregator：有去重键的记录先合并所有副本（第一份决定落在哪个桶），finish() 时再计入；按天或按小时（本机时区）分桶。
    偏离 T3（接口契约）：hourStart 用本机时区的“YYYY-MM-DDTHH:00”（T3 是 UTC ISO 时间）；提供方用 claude/codex/agy；
    费用：Claude 用 usage.py 内置价表（已有链路），Codex 未定价，agy 只有提供方给的费用（不联网拉 LiteLLM 价表）。"""

    def __init__(self, since_ms, until_ms, hourly):
        self.since, self.until, self.hourly = since_ms, until_ms, hourly
        self.buckets = {}
        self.seen = {}
        self.kept = []          # (提供方, 记录)：计入的（去重合并之后）
        self.dropped = 0
        self.out_of_window = 0

    def _bucket(self, provider, rec):
        t = rec[R_TS]
        if t < self.since or t >= self.until:
            return None
        lt = time.localtime(t / 1000)
        day = time.strftime('%Y-%m-%d', lt)
        hs = time.strftime('%Y-%m-%dT%H:00', lt) if self.hourly else ''
        key = (day, hs, provider, rec[R_MODEL])
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = {'u': 0, 'c': 0, 'cw': 0, 'o': 0, 'r': 0, 'cost': 0.0, 'saved': 0.0, 'records': 0,
                                     'unpriced': 0, 'reported': 0, 'sessions': set()}
        return b

    def add(self, provider, rec):
        """T3 add。"""
        k = rec[R_KEY]
        if k is not None:
            held = self.seen.get(k)
            if held is not None:
                self.dropped += 1
                if held[1] is not None:
                    held[2] = merge_usage_copies(held[2], rec)
                return False
        b = self._bucket(provider, rec)
        if k is not None:
            self.seen[k] = [provider, b, rec]
        if b is None:
            self.out_of_window += 1
            return False
        if k is None:
            self._accumulate(b, provider, rec)
        if rec[R_SID]:
            b['sessions'].add(rec[R_SID])
        return True

    @staticmethod
    def price(rec):
        """T3 priceUsage 的取舍：提供方给了费用就用它（providerReported），否则有价表的按价表（modelPriced），都没有是 unpriced。"""
        if rec[R_COST] is not None:
            return rec[R_COST], 'providerReported'
        if len(rec) > A_LIST and rec[A_LIST] is not None:
            return rec[A_LIST], 'modelPriced'
        return 0.0, 'unpriced'

    def _accumulate(self, b, provider, rec):
        """T3 #accumulate。"""
        cost, src = self.price(rec)
        b['u'] += rec[R_U]
        b['c'] += rec[R_C]
        b['cw'] += rec[R_CW]
        b['o'] += rec[R_O]
        b['r'] += rec[R_R]
        b['cost'] += cost
        if src == 'modelPriced' and len(rec) > A_SAVED and rec[A_SAVED]:
            b['saved'] += rec[A_SAVED]
        b['records'] += 1
        if src == 'unpriced':
            b['unpriced'] += 1
        elif src == 'providerReported':
            b['reported'] += 1
        self.kept.append((provider, rec, src))

    def finish(self):
        """T3 finish：计入合并后的有键记录，出 UsageBucket 列表（按天、小时、提供方、模型排序）。"""
        for provider, b, rec in self.seen.values():
            if b is not None:
                self._accumulate(b, provider, rec)
        self.seen = {}
        out = []
        for (day, hs, provider, model), b in self.buckets.items():
            if b['unpriced'] == b['records']:
                src = 'unpriced'
            elif b['reported'] == b['records']:
                src = 'providerReported'
            else:
                src = 'modelPriced'
            x = {'day': day}
            if hs:
                x['hourStart'] = hs
            x.update({'provider': provider, 'model': model or 'unknown',
                      'totals': {'uncachedInputTokens': b['u'], 'cachedInputTokens': b['c'], 'cacheCreationTokens': b['cw'],
                                 'outputTokens': b['o'], 'reasoningTokens': b['r']},
                      'costUsd': round(b['cost'], 6), 'cacheSavingsUsd': round(b['saved'], 6), 'costSource': src,
                      'records': b['records'], 'unpricedRecords': b['unpriced'], 'sessions': len(b['sessions'])})
            out.append(x)
        out.sort(key=lambda x: (x['day'], x.get('hourStart', ''), x['provider'], x['model']))
        return out


# ================================================================ 小工具：中位数、P90、时间
def _median(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def _p90(v):
    """第 90 百分位（最近秩法：排序后第 ceil(0.9n) 个）。"""
    v = sorted(x for x in v if x is not None)
    return v[max(0, math.ceil(0.9 * len(v)) - 1)] if v else None


def _r(x, nd=0):
    if x is None:
        return None
    return round(x, nd) if nd else int(round(x))


def day_start_ms(now, back=0):
    d = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=back)
    return int(d.timestamp() * 1000)


def _tps(out, ms):
    """和 usage.py _row_speed 同一口径：输出 token ÷ 请求耗时（含首字），耗时不到 0.2 秒不算。"""
    return out / (ms / 1000) if ms and ms >= 200 and out > 0 else None


def model_label_of(provider, model):
    """界面上的模型名：Claude 用 server 的 model_label；别家 gpt-6-astra → GPT-6 Astra、gemini-3.8-flash → Gemini 3.8 Flash。"""
    if not model:
        return '未知模型'
    if provider == 'claude' or model.startswith('claude-'):
        try:
            return NS['model_label'](model)
        except Exception:
            pass
    parts = [x for x in re.split(r'[-_]', model) if x]
    out = []
    for i, x in enumerate(parts):
        if x.lower() == 'gpt' and i + 1 < len(parts) and re.match(r'^[\d.o]+$', parts[i + 1]):
            out.append('GPT-' + parts[i + 1])
            parts[i + 1] = ''
        elif x:
            out.append(x.upper() if x.lower() in ('gpt', 'oss') else (x if re.match(r'^[\d.]+', x) else x.capitalize()))
    return ' '.join(o for o in out if o) or model


# ================================================================ 三家合起来
class Hub:
    """扫描 Codex、agy（后台、增量、带进度），汇总三家，出接口的新字段。Claude 的数据来自 usage.py 的 Usage 实例（claude()）。
    根目录都在每次刷新时现取：home()（server 的 HOME）、env（CODEX_HOME、ANTIGRAVITY_DATA_DIR）；测试可以直接传 codex_root、agy_dirs。"""

    def __init__(self, home=None, env=None, codex_root=None, agy_dirs=None, conv_dir=None, data_dir=None, cache_path=None,
                 claude=None, titles=None, names=None, conv_title=None, log_fn=None):
        self.home = home if callable(home) else (lambda: home or os.path.expanduser('~'))
        self.env = env if env is not None else os.environ
        self._codex_root = codex_root
        self._agy_dirs = agy_dirs
        self.conv_dir = conv_dir if callable(conv_dir) else (lambda: conv_dir or '')
        self.data_dir = data_dir if callable(data_dir) else (lambda: data_dir or '')
        self.cache_path = cache_path if callable(cache_path) else (lambda: cache_path)
        self.claude = claude if callable(claude) else (lambda: claude)
        self.titles = titles or (lambda sids: {})
        self.names = names or (lambda: {})
        self.conv_title = conv_title or (lambda sid: '')
        self.log = log_fn or log
        self.codex = CodexScanner(self.codex_root)
        self.agy = AgyScanner(self.agy_dirs)
        self.conv = ConvReader(lambda: self.conv_dir())
        self.started = False
        self.scanning = {'codex': False, 'agy': False}
        self.lock = threading.Lock()
        self.ref_thread = None
        self.last_ref = 0.0
        self.last_save = 0.0
        self.saved_sig = None
        self.stats = {}
        self.idx_cache = (None, {})
        self.agy_title_cache = (0.0, {})

    # ---- 根目录
    def codex_root(self):
        if self._codex_root:
            return self._codex_root() if callable(self._codex_root) else self._codex_root
        h = (self.env.get('CODEX_HOME') or '').strip() or os.path.join(self.home(), '.codex')
        return os.path.join(os.path.expanduser(h), 'sessions')

    def codex_home(self):
        return os.path.dirname(self.codex_root())

    def agy_dirs(self):
        if self._agy_dirs is not None:
            return list(self._agy_dirs() if callable(self._agy_dirs) else self._agy_dirs)
        return agy_conversation_dirs(self.home(), self.env)

    @staticmethod
    def window_start(now):
        """扫描窗口：最长时段（30 天）的起点再往前 36 小时（T3 MTIME_SLACK_MS）。"""
        return day_start_ms(now, RANGES['30d'] - 1) - MTIME_SLACK_MS

    # ---- 扫描
    def start(self):
        """后台：读缓存，扫 Codex、agy、工作台对话一遍，存缓存。只起一次。"""
        with self.lock:
            if self.started:
                return
            self.started = True
            self.scanning = {'codex': True, 'agy': True}

        def run():
            t0 = time.time()
            try:
                self.load_cache()
            except Exception as e:
                self.log('读用量缓存失败', repr(e))
            try:
                self.conv.refresh()
            except Exception as e:
                self.log('读工作台对话记录失败', repr(e))
            for name, fn in (('codex', self._refresh_codex), ('agy', self.agy.refresh)):
                t1 = time.time()
                try:
                    fn()
                except Exception as e:
                    self.log('用量扫描异常', name, repr(e))
                finally:
                    self.scanning[name] = False
                    self.stats[name + '_first_scan_s'] = round(time.time() - t1, 3)
            self.last_ref = time.time()
            self.save_cache(force=True)
            self.log('用量扫描完成（Codex、Antigravity）', f'{time.time() - t0:.1f}s')
        threading.Thread(target=run, daemon=True, name='usage-p-scan').start()

    def _refresh_codex(self):
        now = time.time()
        return self.codex.refresh(self.window_start(now), int(now * 1000))

    def refresh_all(self):
        ch = False
        try:
            self.conv.refresh()
        except Exception as e:
            self.log('读工作台对话记录失败', repr(e))
        for fn in (self._refresh_codex, self.agy.refresh):
            try:
                ch = fn() or ch
            except Exception as e:
                self.log('用量刷新异常', repr(e))
        self.last_ref = time.time()
        if ch and time.time() - self.last_save > 60:
            self.save_cache()
        return ch

    def ensure_fresh(self, wait=1.0):
        """没开始就后台开始；首次扫描中直接返回；否则后台增量刷新，最多等 wait 秒（小改动当场就有，大改动不卡接口）。"""
        if not self.started:
            self.start()
            return
        if any(self.scanning.values()):
            return
        with self.lock:
            t = self.ref_thread
            if (t is None or not t.is_alive()) and time.time() - self.last_ref > 2:
                t = self.ref_thread = threading.Thread(target=self.refresh_all, daemon=True, name='usage-p-refresh')
                t.start()
        if t is not None and t.is_alive() and wait:
            t.join(wait)

    # ---- 缓存（T3 usageScanCache：模型、会话名字表＋按文件的条目）
    def save_cache(self, force=False):
        path = self.cache_path()
        if not path:
            return
        sig = (self.codex.ver, self.agy.ver)
        if not force and sig == self.saved_sig:
            return
        models, mi, sess, si = [], {}, [], {}

        def intern(tab, idx, v):
            i = idx.get(v)
            if i is None:
                i = idx[v] = len(tab)
                tab.append(v)
            return i

        def ser(r):
            return [r[R_TS], intern(models, mi, r[R_MODEL]), intern(sess, si, r[R_SID]), r[R_U], r[R_C], r[R_CW], r[R_O], r[R_R],
                    r[R_KEY], r[R_COST], r[R_TURN], r[R_MS]]
        with self.codex.lock:
            cf = {p: {'s': e['s'], 'm': e['m'], 'r': [ser(r) for r in e['r']], 't': [ser(r) for r in e['t']], 'mr': e['mr'],
                      'o': e['pos']['o'], 'gl': e['pos']['gl'], 'gh': e['pos']['gh'], 'cs': e['pos']['cs'],
                      'turns': e['turns'], 'rl': e['rl']} for p, e in self.codex.files.items()}
        with self.agy.lock:
            af = {}
            for k, d in self.agy.dbs.items():
                data = d['data']
                af[k] = {'fp': d['fp'], 'sid': d['sid'], 'root': d['root'],
                         'data': None if data is None else {'models': data['models'], 'inputs': data['inputs'],
                                                            'cands': [[ser(c[0]), c[1], c[2], c[3], c[4]] for c in data['cands']]}}
        doc = {'version': CACHE_VERSION, 'models': models, 'sessions': sess, 'codex': cf, 'agy': af}
        tmp = path + '.tmp'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(tmp, path)
            self.saved_sig, self.last_save = sig, time.time()
        except OSError as e:
            self.log('保存用量缓存失败', repr(e))

    def load_cache(self):
        """T3 decodeScanCache：版本不对整份不用；条目坏了只丢这一条（整条丢，免得半份数据看着像读过）。"""
        path = self.cache_path()
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                doc = json.load(f)
        except Exception:
            return
        if not isinstance(doc, dict) or doc.get('version') != CACHE_VERSION:
            return
        models, sess = doc.get('models'), doc.get('sessions')
        if not isinstance(models, list) or not isinstance(sess, list):
            return

        def de(rows):
            out = []
            for x in rows:
                if not isinstance(x, list) or len(x) != 12:
                    return None
                ts, m, s = x[0], x[1], x[2]
                if not _num(ts) or not isinstance(m, int) or not (0 <= m < len(models)) or not isinstance(s, int) or not (0 <= s < len(sess)):
                    return None
                if not all(_int0(v) is not None for v in x[3:8]):
                    return None
                out.append([ts, models[m], sess[s]] + x[3:])
            return out
        cf = {}
        for p, e in (doc.get('codex') or {}).items():
            try:
                r, t = de(e['r']), de(e['t'])
                o, gl = e['o'], e['gl']
                if r is None or t is None or not isinstance(o, int) or o < 0 or not isinstance(gl, int) or not (0 <= gl <= GUARD_LENGTH) or gl > o:
                    continue
                if e['cs'] is not None and not valid_codex_state(e['cs']):
                    continue  # 续读状态坏了：接着用会把追加的用量归错模型
                cf[p] = {'s': int(e['s']), 'm': int(e['m']), 'r': r, 't': t, 'mr': int(e.get('mr') or 0),
                         'pos': {'o': o, 'gl': gl, 'gh': e['gh'], 'cs': e['cs']},
                         'turns': {k: list(v) for k, v in (e.get('turns') or {}).items() if isinstance(v, list) and len(v) == 7},
                         'rl': [x for x in (e.get('rl') or []) if isinstance(x, list) and len(x) == 4]}
            except Exception:
                continue
        af = {}
        for k, e in (doc.get('agy') or {}).items():
            try:
                data = e['data']
                if data is not None:
                    cands = []
                    for c in data['cands']:
                        rec = de([c[0]])
                        if rec is None:
                            raise ValueError
                        cands.append([rec[0], int(c[1]), int(c[2]), list(c[3]), int(c[4])])
                    data = {'models': [[int(a), b] for a, b in data['models']], 'inputs': list(data.get('inputs') or []), 'cands': cands}
                af[k] = {'fp': str(e['fp']), 'sid': str(e['sid']), 'root': e.get('root') or '', 'data': data}
            except Exception:
                continue
        with self.codex.lock:
            for p, e in cf.items():
                self.codex.files.setdefault(p, e)
            self.codex.ver += 1
        with self.agy.lock:
            for k, e in af.items():
                self.agy.dbs.setdefault(k, e)
            self.agy.order = list(self.agy.dbs)
            self.agy.ver += 1
        self.saved_sig = (self.codex.ver, self.agy.ver)

    # ---- 工作台对话：原生 ID ↔ sid、标题
    def _wb(self):
        convs = self.conv.convs()
        by_native = {}
        for c in convs:
            if c['prov'] in ('codex', 'agy') and c['native']:
                by_native[(c['prov'], c['native'])] = c
        return convs, by_native

    def _wb_title(self, c, names):
        sid = c['sid']
        try:
            t = self.conv_title(sid)
        except Exception:
            t = ''
        return names.get(sid) or t or c['title'] or c['first'] or '新对话'

    def _codex_index_titles(self):
        """Codex 终端会话的标题：<CODEX_HOME>/session_index.jsonl 的 thread_name（后写的覆盖先写的）。按大小、修改时间缓存。"""
        p = os.path.join(self.codex_home(), 'session_index.jsonl')
        try:
            st = os.stat(p)
        except OSError:
            return {}
        sig = (p, st.st_size, st.st_mtime_ns)
        if self.idx_cache[0] == sig:
            return self.idx_cache[1]
        out = {}
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(d, dict) and isinstance(d.get('id'), str) and isinstance(d.get('thread_name'), str) and d['thread_name'].strip():
                        out[d['id']] = ' '.join(d['thread_name'].split())[:60]
        except OSError:
            return {}
        self.idx_cache = (sig, out)
        return out

    def _agy_titles(self):
        t, v = self.agy_title_cache
        if time.time() - t < 30:
            return v
        v = agy_titles(self.agy_dirs())
        self.agy_title_cache = (time.time(), v)
        return v

    def tasks(self):
        """data/tasks.jsonl（M 线写，只读）：返回 (是否有内容, [(子 conv, 子提供方, 父 conv, 父提供方)])。"""
        p = os.path.join(self.data_dir() or '', 'tasks.jsonl')
        out, nonempty = [], False
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for line in f:
                    if not line.strip():
                        continue
                    nonempty = True
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(d, dict) or d.get('t') != 'task':
                        continue
                    par, ch = d.get('parent') or {}, d.get('child') or {}
                    if isinstance(par, dict) and isinstance(ch, dict) and par.get('conv') and ch.get('conv'):
                        out.append((str(ch['conv']), str(ch.get('provider') or ''), str(par['conv']), str(par.get('provider') or '')))
        except OSError:
            pass
        return nonempty, out

    # ---- 收集三家的记录
    def _claude(self, rng, start, now, claude_q, scope='wb'):
        """Claude（第十四轮现状，只算工作台）：query() 的结果（接口已经算好的就直接用，不重算）和同一对象 build() 里同一时段的行。
        build() 的行：(时间毫秒, sid, 模型, 输入, 输出, 缓存读, 缓存写, fast, 耗时毫秒, 费用, 日期, 小时, 省下的钱)；时段过滤和 query() 一样只看起点。
        能否定价用 usage.price_key(model)（认不出系列的模型 cost_of 给 0，这里记 unpriced）。"""
        u = self.claude()
        if u is None:
            return None
        import usage as US
        q = claude_q if isinstance(claude_q, dict) and 'kpi' in claude_q else u.query(rng, now=now, scope=scope)
        rows = [x for x in u.build(scope) if x[0] >= start]  # 11-26：口径跟 query() 一致（all / wb）
        recs = []
        for x in rows:
            priced = bool(US.price_key(x[2]))
            recs.append([x[0], x[2], x[1], x[3], x[5], x[6], x[4], 0, None, None, '', x[8], x[9] if priced else None, x[12] if priced else 0.0])
        speeds = [(x[0], type(u)._row_speed(x)) for x in rows]
        return {'q': q, 'rows': rows, 'recs': recs, 'speeds': speeds, 'scope': 'all' if scope == 'all' else 'wb'}

    def _codex_records(self, start, end, now):
        files = self.codex.file_records(self.window_start(now), int(now * 1000) - RETENTION_DAYS * 86400_000)
        frecs = [e['r'] + e['t'] for _, e in files]
        shared = shared_sessions(frecs)
        out = []
        for recs in frecs:
            occ = {}
            for r in recs:
                if r[R_SID] in shared:
                    # T3：移走的副本去重，但同一文件里重复的相同事件照算（按出现次数编号）
                    k = _dumps(['codex', r[R_SID], r[R_TS], r[R_MODEL], [r[R_U], r[R_C], r[R_CW], r[R_O], r[R_R]]])
                    n = occ[k] = occ.get(k, 0) + 1
                    r = list(r)
                    r[R_KEY] = k + ':' + str(n)
                out.append(r)
        turns, rl = {}, []
        for _, e in files:
            for tid, tr in (e.get('turns') or {}).items():
                old = turns.get(tid)
                if old is None or (tr[0] and old[0] and tr[0] < old[0]):  # 分叉复制来的同一轮：取开始时间最早的那份
                    turns[tid] = tr
            rl.extend(e.get('rl') or [])
        return out, turns, rl

    # ---- 接口
    def query(self, rng='today', provider='all', rollup=False, now=None, claude_q=None, raw=False, claude_scope='wb'):
        """新字段。claude_q：接口里原函数已算好的 USAGE.query(range) 结果（不传就自己调一次）。
        raw=True（11-23，给 legacy() 用）：算到“去重后计入的记录”就返回，不出新字段。"""
        self.ensure_fresh()
        rng = rng if rng in RANGES else 'today'
        provider = provider if provider in ORDER else 'all'
        want = ORDER if provider == 'all' else (provider,)
        now = now or time.time()
        days = RANGES[rng]
        start, end = day_start_ms(now, days - 1), day_start_ms(now, -1)  # 时段：起始日零点到明天零点（本机时区）
        convs, by_native = self._wb()
        names = {}
        try:
            names = self.names() or {}
        except Exception:
            pass

        agg = Aggregator(start, end, rng == 'today')
        turns = {p: [] for p in ORDER}   # 每家的轮：dict（Claude 没有，见 _claude）
        scanning, progress = {}, {}
        cl = None
        # Claude：只算工作台（第十四轮现状）
        if 'claude' in want:
            cl = self._claude(rng, start, now, claude_q, claude_scope)
            if cl is not None:
                scanning['claude'] = bool(cl['q'].get('scanning'))
                for r in cl['recs']:
                    agg.add('claude', r)
        # Codex
        wq, rl = None, []
        if 'codex' in want:
            scanning['codex'], progress['codex'] = self.scanning['codex'], dict(self.codex.progress)
            recs, cturns, rl = self._codex_records(start, end, now)
            for r in recs:
                agg.add('codex', r)
            wb_turns = {}
            for c in convs:
                if c['prov'] == 'codex':
                    for te in c['turns']:
                        if isinstance(te.get('turnId'), str):
                            wb_turns[te['turnId']] = (c, te)
            for tid, tr in cturns.items():
                t0, t1, dur, ttft, model, aborted, sid = tr
                ts = t1 or t0
                if not ts or ts < start or ts >= end:
                    continue
                wb = wb_turns.get(tid)
                c = wb[0] if wb else by_native.get(('codex', sid))
                if wb:
                    te = wb[1]
                    if _num(te.get('firstTokenAt')) and _num(te.get('startedAt')) and te['firstTokenAt'] >= te['startedAt']:
                        ttft = int(te['firstTokenAt'] - te['startedAt'])  # 工作台的轮：首字等待用 turn_end
                    if dur is None and _num(te.get('endedAt')) and _num(te.get('startedAt')):
                        dur = int(te['endedAt'] - te['startedAt'])
                turns['codex'].append({'ts': ts, 'sid': sid, 'model': model, 'ttft': ttft if not aborted else None,
                                       'turn': dur if not aborted else None, 'tid': tid, 'wbsid': c['sid'] if c else None,
                                       'where': 'workbench' if c else 'other', 'approx': {'tps': True}, 'done': not aborted,
                                       'ask': t0})
            wq = week_quota(rl, start, end)
        # Antigravity
        agy_inputs = {}
        if 'agy' in want:
            scanning['agy'], progress['agy'] = self.scanning['agy'], dict(self.agy.progress)
            arecs, agy_inputs = self.agy.collect(self.window_start(now))
            by_sid = {}
            for r in arecs:
                by_sid.setdefault(r[R_SID], []).append(r)
            extra = []
            for c in convs:
                if c['prov'] != 'agy':
                    continue
                native = c['native']
                prev_amt = None
                for te in sorted(c['turns'], key=lambda x: x.get('ts') or 0):
                    cost = te.get('cost') if isinstance(te.get('cost'), dict) else None
                    amt = cost.get('amount') if cost and _num(cost.get('amount')) else None
                    delta = None
                    if amt is not None:
                        # 会话累计：按相邻两轮的差算到这一轮（变小说明重新计了，就用当前值）
                        delta = amt - prev_amt if prev_amt is not None and amt >= prev_amt else amt
                        prev_amt = amt
                    ended = te.get('endedAt') if _num(te.get('endedAt')) else te.get('ts')
                    started = te.get('startedAt') if _num(te.get('startedAt')) else None
                    mine = by_sid.get(native) if native else None
                    if mine:
                        if delta:
                            lo, hi = (started or ended or 0) - 5000, (ended or 0) + 5000
                            inwin = [r for r in mine if lo <= r[R_TS] <= hi]
                            tgt = inwin[-1] if inwin else min(mine, key=lambda r: abs(r[R_TS] - (ended or 0)))
                            tgt[R_COST] = (tgt[R_COST] or 0.0) + delta
                    else:
                        us = te.get('usage') if isinstance(te.get('usage'), dict) else None
                        if us and _num(ended):
                            g = lambda k: _int0(us.get(k)) or 0  # noqa: E731
                            o = g('output')
                            r = [int(ended), te.get('model') or '', native or c['sid'], g('input'), g('cachedInput'), g('cacheWrite'), o,
                                 min(o, g('reasoning')), 'agyconv:%s:%s' % (c['sid'], te.get('turnId')), delta, str(te.get('turnId') or ''),
                                 None]
                            if total_tokens(r) > 0 or delta:
                                extra.append(r)
                    if te.get('status') == 'completed' and _num(ended) and start <= ended < end:
                        ttft = te['firstTokenAt'] - te['startedAt'] if _num(te.get('firstTokenAt')) and started is not None and te['firstTokenAt'] >= started else None
                        dur = ended - started if started is not None and ended >= started else None
                        us = te.get('usage') if isinstance(te.get('usage'), dict) else {}
                        o = _int0(us.get('output')) or 0
                        turns['agy'].append({'ts': int(ended), 'sid': native or c['sid'], 'model': te.get('model') or '', 'ttft': ttft,
                                             'turn': dur, 'output': o, 'tps': _tps(o, dur), 'wbsid': c['sid'], 'where': 'workbench',
                                             'approx': {'tps': True}, 'done': True, 'ask': started})
            for r in arecs + extra:
                agg.add('agy', r)
        buckets = agg.finish()

        # 每家 KPI、排行、速度
        kept = {p: [] for p in ORDER}
        for prov, r, src in agg.kept:
            kept[prov].append((r, src))
        if raw:
            return {'kept': kept, 'scanning': scanning, 'progress': progress, 'rl': rl, 'by_native': by_native, 'names': names}
        out_providers, speed_providers = [], []
        sess_tot = {}
        for p in want:
            rows = kept[p]
            if p == 'claude':
                self._claude_out(cl, rows, out_providers, speed_providers, sess_tot, rollup)
                continue
            u = sum(r[R_U] for r, _ in rows)
            c = sum(r[R_C] for r, _ in rows)
            cw = sum(r[R_CW] for r, _ in rows)
            o = sum(r[R_O] for r, _ in rows)
            rs = sum(r[R_R] for r, _ in rows)
            srcs = {s for _, s in rows}
            priced = [Aggregator.price(r)[0] for r, s in rows if s != 'unpriced']
            if not rows:
                cost_src = 'modelPriced' if p == 'claude' else 'unpriced'
            elif len(srcs) == 1:
                cost_src = next(iter(srcs))
            else:
                cost_src = 'mixed'
            cost = round(sum(priced), 4) if priced else (0.0 if p == 'claude' else None)
            if p == 'claude':
                den = u + c + cw
            elif p == 'codex':
                den = u + c + cw   # Codex 的 input 含缓存：缓存读 ÷ input
            else:
                den = u + c        # agy：cache_read ÷ (input + cache_read)
            hit = round(c / den, 4) if den else None
            saved = round(sum(r[A_SAVED] for r, s in rows if s == 'modelPriced' and len(r) > A_SAVED and r[A_SAVED]), 4) if p == 'claude' else None
            sids = {r[R_SID] for r, _ in rows if r[R_SID]}
            if p == 'agy':
                nturns = sum(1 for sid, ts in ((s, t) for s, lst in agy_inputs.items() for t in lst) if start <= ts < end)
                covered = set(agy_inputs)
                nturns += sum(1 for t in turns['agy'] if t['sid'] not in covered)
            else:
                nturns = len(turns[p])
            item = {'id': p, 'label': LABELS[p], 'scope': 'all', 'scanning': bool(scanning.get(p)),
                    'kpi': {'requests': len(rows), 'turns': nturns, 'sessions': len(sids), 'uncachedInput': u, 'cachedInput': c,
                            'cacheWrite': cw, 'output': o, 'reasoning': rs, 'tokens': u + c + cw + o, 'cost': cost,
                            'costSource': cost_src, 'cacheHit': hit, 'saved': saved, 'weekQuota': wq if p == 'codex' else None}}
            if scanning.get(p) and progress.get(p):
                item['progress'] = progress[p]
            out_providers.append(item)
            # 会话合计（排行用）
            for r, s in rows:
                a = sess_tot.setdefault((p, r[R_SID]), [0, 0, 0.0, False])
                a[0] += 1
                a[1] += total_tokens(r)
                if s != 'unpriced':
                    a[2] += Aggregator.price(r)[0]
                    a[3] = True
            # 每轮的输出、速度（Codex 从计入的记录按轮汇总）
            if p == 'codex':
                per = {}
                for r, _ in rows:
                    if r[R_TURN]:
                        a = per.setdefault(r[R_TURN], [0, 0, 0])
                        a[0] += r[R_O]
                        if r[R_MS] and r[R_MS] >= 200 and r[R_O] > 0:
                            a[1] += r[R_O]
                            a[2] += r[R_MS]
                for t in turns['codex']:
                    a = per.get(t['tid'], [0, 0, 0])
                    t['output'], t['tps'] = a[0], _tps(a[1], a[2])
            speed_providers.append(self._speed_of(p, rows, turns[p]))

        ranking = self._ranking(sess_tot, by_native, convs, names, rollup)
        has_tasks = self.tasks()[0]
        req_speeds = {p: [(r[R_TS], _tps(r[R_O], r[R_MS])) for r, _ in kept[p]] for p in want if p != 'claude'}
        if cl is not None:
            req_speeds['claude'] = cl['speeds']  # Claude 折线：build() 的行用 Usage._row_speed
        series = self._series(rng, now, start, req_speeds, turns, want)
        recent = self._recent(turns, want, by_native, names)
        return {'providers': out_providers, 'usageBuckets': buckets,
                'speed': {'providers': speed_providers, 'series': series, 'recent': recent},
                'ranking': ranking, 'hasTasks': has_tasks}

    # ---- 11-23：Codex、Antigravity 的第十四轮老样式页
    LEGACY_HEAT_DAYS = 371  # 和 usage.py HEAT_DAYS 一样

    def legacy(self, rng='today', provider='codex', now=None):
        """用户 11-23 要求 Codex、Antigravity 也用第十四轮的老“用量”页（ux-j.js rUsage）：字段和 usage.py Usage.query() 一样
        （kpi、today、buckets、models、heat、sessions、recent）。记录取 query('30d', raw=True) 去重后计入的（和新页面同一批），再按时段筛。
        和 Claude 的不同：Codex 没有价格，cost 为 None（界面不显示钱，换成 kpi.weekQuota“占周额度”）；agy 只有提供方报的费用；
        saved 为 None（没有价表算不出省下多少）；Codex 的请求耗时是估算（kpi.approxSpeed）。
        活跃度：扫描只覆盖最近 30 天（window_start），heatSince 之前的天没有统计。"""
        rng = rng if rng in RANGES else 'today'
        p = provider if provider in ('codex', 'agy') else 'codex'
        now = now or time.time()
        days = RANGES[rng]
        start, end = day_start_ms(now, days - 1), day_start_ms(now, -1)
        t_today = day_start_ms(now, 0)
        raw = self.query('30d', p, now=now, raw=True)
        allrows = sorted(((r, Aggregator.price(r)[0] if src != 'unpriced' else None) for r, src in raw['kept'][p]),
                         key=lambda x: x[0][R_TS])
        sel = [x for x in allrows if start <= x[0][R_TS] < end]

        def tot(rows):
            priced = [cst for _, cst in rows if cst is not None]
            return (len(rows), sum(r[R_U] for r, _ in rows), sum(r[R_C] for r, _ in rows), sum(r[R_CW] for r, _ in rows),
                    sum(r[R_O] for r, _ in rows), sum(priced) if priced else None)

        def speed(rows):
            v = [s for s in (_tps(r[R_O], r[R_MS]) for r, _ in rows) if s is not None]
            return round(statistics.median(v), 1) if v else None

        def money(v, nd=4):
            return round(v, nd) if v is not None else None

        def dkey(back):
            return (datetime.fromtimestamp(now) - timedelta(days=back)).strftime('%Y-%m-%d')

        n, u, c, cw, o, cost = tot(sel)
        tok = u + c + cw + o
        den = u + c + cw if p == 'codex' else u + c   # 和新页面一样：Codex 的 input 含缓存；agy 是 cache_read ÷ (input + cache_read)
        kpi = {'requests': n, 'input': u, 'output': o, 'cacheRead': c, 'cacheWrite': cw, 'tokens': tok, 'cost': money(cost),
               'cacheHit': round(c / den, 4) if den else None, 'speed': speed(sel), 'saved': None,
               'pricedRequests': sum(1 for _, cst in sel if cst is not None)}  # agy 只有部分请求报了费用：“每次”要按它算，不按全部请求摊
        if p == 'codex':
            kpi['weekQuota'] = week_quota(raw['rl'], start, end)
            kpi['approxSpeed'] = True
        tn, tu, tc, tcw, to, tcost = tot([x for x in allrows if t_today <= x[0][R_TS] < end])

        # 柱状图：今天按小时 24 个，7 天、30 天按天；每个桶带按模型拆开的 {模型: {tokens, cost, requests}}（同 usage.py）
        day = dkey(0)
        agg, aggm = {}, {}
        for r, cst in sel:
            lt = time.localtime(r[R_TS] / 1000)
            d = time.strftime('%Y-%m-%d', lt)
            if rng == 'today' and d != day:
                continue
            k = lt.tm_hour if rng == 'today' else d
            for a in (agg.setdefault(k, [0, 0, 0, 0.0, 0]), aggm.setdefault(k, {}).setdefault(r[R_MODEL] or 'unknown', [0, 0, 0, 0.0, 0])):
                a[0] += 1
                a[1] += total_tokens(r)
                a[2] += r[R_O]
                if cst is not None:
                    a[3] += cst
                    a[4] += 1

        def bucket(key, label, a, bm):
            ms = sorted(bm.items(), key=lambda kv: -kv[1][1])
            # 桶里没有一条报了费用的记录时 cost 为 None（界面写“—”，不写 $0.00）
            return {'key': key, 'label': label, 'requests': a[0], 'tokens': a[1], 'output': a[2], 'cost': round(a[3], 4) if a[4] else None,
                    'models': {m: {'tokens': v[1], 'cost': round(v[3], 4) if v[4] else None, 'requests': v[0]} for m, v in ms}}
        zero = [0, 0, 0, 0.0, 0]
        if rng == 'today':
            buckets = [bucket(f'{day} {h:02d}', f'{h:02d}:00', agg.get(h, zero), aggm.get(h, {})) for h in range(24)]
        else:
            buckets = [bucket(d, d[5:], agg.get(d, zero), aggm.get(d, {})) for d in (dkey(k) for k in range(days - 1, -1, -1))]

        # 模型：按 tokens 降序
        bym = {}
        for x in sel:
            bym.setdefault(x[0][R_MODEL] or 'unknown', []).append(x)
        models = []
        for m, xs in bym.items():
            mn, mu, mc, mcw, mo, mcost = tot(xs)
            mt = mu + mc + mcw + mo
            models.append({'id': m, 'label': model_label_of(p, '' if m == 'unknown' else m), 'requests': mn, 'tokens': mt, 'output': mo,
                           'cost': money(mcost), 'share': round(mt / tok, 4) if tok else 0, 'speed': speed(xs)})
        models.sort(key=lambda m: -m['tokens'])

        # 活跃度：只有扫描窗口里的天
        hagg = {}
        for r, _ in allrows:
            a = hagg.setdefault(time.strftime('%Y-%m-%d', time.localtime(r[R_TS] / 1000)), [0, 0])
            a[0] += total_tokens(r)
            a[1] += 1
        heat = []
        for k in range(self.LEGACY_HEAT_DAYS - 1, -1, -1):
            d = dkey(k)
            a = hagg.get(d, (0, 0))
            heat.append({'date': d, 'tokens': a[0], 'requests': a[1]})

        # 用量最多的对话（工作台里的能点开）、最近 100 条
        bys = {}
        for r, cst in sel:
            if not r[R_SID]:
                continue
            a = bys.setdefault(r[R_SID], [0, 0, 0.0, False])
            a[0] += 1
            a[1] += total_tokens(r)
            if cst is not None:
                a[2] += cst
                a[3] = True
        top = sorted(bys.items(), key=lambda kv: (-kv[1][1], kv[0]))[:10]
        tm = self._title_map([(p, s) for s, _ in top], raw['by_native'], raw['names']) if top else {}
        sessions = []
        for s, a in top:
            title, wsid = tm.get((p, s), (UNTITLED, None))
            sessions.append({'sid': wsid or '', 'title': title, 'requests': a[0], 'tokens': a[1], 'cost': round(a[2], 4) if a[3] else None})
        recent = []
        for r, cst in sel[-100:][::-1]:
            sp = _tps(r[R_O], r[R_MS])
            m = r[R_MODEL] or ''
            recent.append({'ts': r[R_TS], 'model': m, 'label': model_label_of(p, m), 'fast': False, 'input': r[R_U], 'output': r[R_O],
                           'cacheRead': r[R_C], 'cacheWrite': r[R_CW], 'ms': r[R_MS], 'speed': round(sp, 1) if sp is not None else None,
                           'cost': money(cst, 6)})
        sc = bool(raw['scanning'].get(p))
        out = {'scanning': sc, 'updated': self.last_ref or None, 'range': rng, 'unit': 'hour' if rng == 'today' else 'day', 'provider': p,
               'kpi': kpi, 'today': {'requests': tn, 'tokens': tu + tc + tcw + to, 'cost': money(tcost)},
               'buckets': buckets, 'models': models, 'heat': heat, 'heatSince': dkey(RANGES['30d'] - 1), 'recent': recent, 'sessions': sessions}
        if sc and raw['progress'].get(p):
            out['progress'] = raw['progress'][p]
        return out

    CLAUDE_WHY = {'ttft': '只算工作台的 Claude 统计没有记首字时间', 'turn': '只算工作台的 Claude 统计没有记每轮用时'}
    CLAUDE_WHY_ALL = {'ttft': 'Claude 的用量统计没有记首字时间', 'turn': 'Claude 的用量统计没有记每轮用时'}  # 11-26：含终端、VS Code 时

    def _claude_out(self, cl, kept, out_providers, speed_providers, sess_tot, rollup):
        """Claude 一栏：KPI、按模型的输出速度直接取 query() 的结果（字段换成契约的名字）；对话排行不勾“子任务”时取 query() 的
        sessions（前 10，三家合并前 10 用它就够），勾了用 build() 行按会话合计（和 query() 同一算法），子对话可能不在前 10。"""
        if cl is None:
            out_providers.append({'id': 'claude', 'label': LABELS['claude'], 'scope': 'workbench', 'scanning': False,
                                  'kpi': {'requests': 0, 'turns': None, 'sessions': 0, 'uncachedInput': 0, 'cachedInput': 0, 'cacheWrite': 0,
                                          'output': 0, 'reasoning': 0, 'tokens': 0, 'cost': 0.0, 'costSource': 'modelPriced',
                                          'cacheHit': None, 'saved': None, 'weekQuota': None}})
            speed_providers.append({'id': 'claude', 'label': LABELS['claude'], 'tps': None, 'ttft': None, 'turn': None, 'turnP90': None,
                                    'approx': {'tps': True}, 'why': dict(self.CLAUDE_WHY, tps=self.WHY['none']), 'models': []})
            return
        q, k = cl['q'], cl['q']['kpi']
        out_providers.append({'id': 'claude', 'label': LABELS['claude'], 'scope': 'all' if cl.get('scope') == 'all' else 'workbench',
                              'scanning': bool(q.get('scanning')),
                              'kpi': {'requests': k['requests'], 'turns': None, 'sessions': len({x[1] for x in cl['rows']}),
                                      'uncachedInput': k['input'], 'cachedInput': k['cacheRead'], 'cacheWrite': k['cacheWrite'],
                                      'output': k['output'], 'reasoning': 0, 'tokens': k['tokens'], 'cost': k['cost'],
                                      'costSource': 'modelPriced', 'cacheHit': k['cacheHit'], 'saved': k['saved'], 'weekQuota': None}})

        def why(tps, has_any):
            w = dict(self.CLAUDE_WHY_ALL if cl.get('scope') == 'all' else self.CLAUDE_WHY)
            if tps is None:
                w['tps'] = self.WHY['claude']['tps'] if has_any else self.WHY['none']
            return w
        sp = {'id': 'claude', 'label': LABELS['claude'], 'tps': k.get('speed'), 'ttft': None, 'turn': None, 'turnP90': None,
              'approx': {'tps': True}, 'why': why(k.get('speed'), k['requests'] > 0), 'models': []}
        for m in q.get('models') or []:
            sp['models'].append({'model': m['id'], 'label': m['label'], 'requests': m['requests'], 'turns': None, 'tps': m.get('speed'),
                                 'ttft': None, 'turn': None, 'turnP90': None, 'approx': {'tps': True}, 'why': why(m.get('speed'), True)})
        speed_providers.append(sp)
        if rollup:
            for x in cl['rows']:
                a = sess_tot.setdefault(('claude', x[1]), [0, 0, 0.0, True])
                a[0] += 1
                a[1] += x[3] + x[4] + x[5] + x[6]
                a[2] += x[9]
        else:
            for s in q.get('sessions') or []:
                sess_tot[('claude', s['sid'])] = [s['requests'], s['tokens'], s['cost'], True]
        self._claude_titles = {s['sid']: s.get('title') or '' for s in q.get('sessions') or []}

    # ---- 速度
    WHY = {
        'none': '这段时间没有用过',
        'claude': {'tps': '这段时间没有能估算速度的回复', 'ttft': '这段时间没有完整的回合', 'turn': '这段时间的回合没有记下用时'},
        'codex': {'tps': '这段时间的记录里没有逐次请求的数据', 'ttft': '这段时间的回合没有记下首字时间', 'turn': '这段时间没有完成的回合'},
        'agy': {'tps': '这段时间没有能估算速度的回复', 'ttft': '终端里的 Antigravity 对话不记首字时间，只有工作台里的对话有',
                'turn': '终端里的 Antigravity 对话不记每轮用时，只有工作台里的对话有'},
    }
    APPROX = {'claude': {'tps': True, 'ttft': True}, 'codex': {'tps': True}, 'agy': {'tps': True}}

    def _speed_cell(self, p, reqs, trs, has_any):
        tps = _median([_tps(r[R_O], r[R_MS]) for r in reqs])
        done = [t for t in trs if t.get('done')]
        ttft = _median([t['ttft'] for t in done])
        turn = _median([t['turn'] for t in done])
        p90 = _p90([t['turn'] for t in done])
        why = {}
        for k, v in (('tps', tps), ('ttft', ttft), ('turn', turn)):
            if v is None:
                why[k] = self.WHY[p][k] if has_any else self.WHY['none']
        return {'tps': _r(tps, 1), 'ttft': _r(ttft), 'turn': _r(turn), 'turnP90': _r(p90), 'approx': dict(self.APPROX[p]), 'why': why}

    def _speed_of(self, p, rows, trs):
        reqs = [r for r, _ in rows]
        has_any = bool(reqs or trs)
        out = {'id': p, 'label': LABELS[p]}
        out.update(self._speed_cell(p, reqs, trs, has_any))
        by = {}
        for r in reqs:
            by.setdefault(r[R_MODEL], [[], []])[0].append(r)
        for t in trs:
            by.setdefault(t['model'], [[], []])[1].append(t)
        models = []
        for m, (rq, tr) in by.items():
            x = {'model': m, 'label': model_label_of(p, m), 'requests': len(rq), 'turns': len(tr)}
            x.update(self._speed_cell(p, rq, tr, True))
            models.append(x)
        models.sort(key=lambda x: (-x['requests'], -x['turns'], x['model']))
        out['models'] = models
        return out

    def _series(self, rng, now, start, req_speeds, turns, want):
        """today、7d 按小时（24／168 个点），30d 按天（30 个点）；某家某点没数据就不给那家的键。"""
        hourly = rng in ('today', '7d')
        n = 24 * RANGES[rng] if hourly else RANGES[rng]
        step = 3600_000
        pts = []
        if hourly:
            # 按本机时区的整点切（夏令时那天也按真实时刻）
            t0 = datetime.fromtimestamp(start / 1000)
            for i in range(n):
                a = t0 + timedelta(hours=i)
                ms = int(a.timestamp() * 1000)
                key = a.strftime('%Y-%m-%d %H')
                pts.append([key, a.strftime('%H:00') if rng == 'today' else a.strftime('%m-%d %H:00'), ms, ms + step])
        else:
            for k in range(n - 1, -1, -1):
                a, b = day_start_ms(now, k), day_start_ms(now, k - 1)
                d = datetime.fromtimestamp(a / 1000).strftime('%Y-%m-%d')
                pts.append([d, d[5:], a, b])
        starts = [x[2] for x in pts]

        def slot(ts):
            i = bisect.bisect_right(starts, ts) - 1
            return i if 0 <= i < len(pts) and ts < pts[i][3] else None
        acc = [{} for _ in pts]
        for p in want:
            for ts, v in req_speeds.get(p) or ():
                i = slot(ts)
                if i is not None:
                    acc[i].setdefault(p, [[], [], []])[0].append(v)
            for t in turns[p]:
                if not t.get('done'):
                    continue
                i = slot(t['ts'])
                if i is not None:
                    a = acc[i].setdefault(p, [[], [], []])
                    a[1].append(t['ttft'])
                    a[2].append(t['turn'])
        out = []
        for (key, label, _, _), a in zip(pts, acc):
            pt = {'key': key, 'label': label}
            for p, (tp, tf, tu) in a.items():
                v = {'tps': _r(_median(tp), 1), 'ttft': _r(_median(tf)), 'turn': _r(_median(tu))}
                if any(x is not None for x in v.values()):
                    pt[p] = v
            out.append(pt)
        return {'unit': 'hour' if hourly else 'day', 'points': out}

    def _title_map(self, keys, by_native, names):
        """排行和最近几轮的标题：Claude 用 server 的 usage_titles；工作台对话用工作台标题；Codex 终端用 session_index；agy 用它的摘要库。"""
        csids = {sid for p, sid in keys if p == 'claude'}
        try:
            known = getattr(self, '_claude_titles', None) or {}
            rest = {s for s in csids if not known.get(s)}
            ct = dict(self.titles(rest) if rest else {}, **{s: known[s] for s in csids if known.get(s)})
        except Exception:
            ct = {}
        ci = self._codex_index_titles() if any(p == 'codex' for p, _ in keys) else {}
        ai = self._agy_titles() if any(p == 'agy' for p, _ in keys) else {}
        out = {}
        for p, sid in keys:
            c = by_native.get((p, sid)) if p != 'claude' else None
            if c is not None:
                out[(p, sid)] = (self._wb_title(c, names), c['sid'])
            elif p == 'claude':
                out[(p, sid)] = (ct.get(sid) or UNTITLED, sid)
            elif p == 'codex':
                out[(p, sid)] = (ci.get(sid) or UNTITLED, None)
            else:
                out[(p, sid)] = (ai.get(sid) or UNTITLED, None)
        return out

    def _ranking(self, tot, by_native, convs, names, rollup):
        rows = {k: {'key': f'{k[0]}:{k[1]}', 'provider': k[0], 'sid0': k[1], 'requests': a[0], 'tokens': a[1],
                    'cost': round(a[2], 4) if a[3] else None} for k, a in tot.items() if k[1]}
        if rollup:
            wb_by_sid = {c['sid']: c for c in convs}

            def key_of(conv, prov):
                c = wb_by_sid.get(conv)
                if c is not None and c['prov'] in ('codex', 'agy'):
                    return (c['prov'], c['native'] or conv)
                return (prov if prov in ORDER else 'claude', conv)
            for ch, chp, par, parp in self.tasks()[1]:
                ck, pk = key_of(ch, chp), key_of(par, parp)
                if ck == pk or ck not in rows:
                    continue
                c = rows.pop(ck)
                prow = rows.get(pk)
                if prow is None:
                    prow = rows[pk] = {'key': f'{pk[0]}:{pk[1]}', 'provider': pk[0], 'sid0': pk[1], 'requests': 0, 'tokens': 0, 'cost': None}
                prow['requests'] += c['requests']
                prow['tokens'] += c['tokens']
                if c['cost'] is not None:
                    prow['cost'] = round((prow['cost'] or 0.0) + c['cost'], 4)
                prow['children'] = prow.get('children', 0) + 1 + c.get('children', 0)
        top = sorted(rows.items(), key=lambda kv: (-kv[1]['tokens'], kv[1]['key']))[:10]
        tm = self._title_map([k for k, _ in top], by_native, names)
        out = []
        for k, v in top:
            title, sid = tm.get(k, (UNTITLED, None))
            x = {'key': v['key'], 'provider': v['provider']}
            if sid:
                x['sid'] = sid
            x.update({'title': title, 'requests': v['requests'], 'tokens': v['tokens'], 'cost': v['cost']})
            if v.get('children'):
                x['children'] = v['children']
            out.append(x)
        return out

    def _recent(self, turns, want, by_native, names):
        allt = [(p, t) for p in want for t in turns[p]]
        allt.sort(key=lambda pt: -pt[1]['ts'])
        allt = allt[:20]
        tm = self._title_map({(p, t['sid']) for p, t in allt}, by_native, names)
        out = []
        for p, t in allt:
            title, sid = tm.get((p, t['sid']), (UNTITLED, None))
            x = {'ts': t['ts'], 'provider': p, 'model': t['model'], 'label': model_label_of(p, t['model']), 'title': title}
            if sid:
                x['sid'] = sid
            x.update({'ttft': _r(t.get('ttft')), 'turn': _r(t.get('turn')), 'output': t.get('output') or 0,
                      'tps': _r(t.get('tps'), 1), 'approx': dict(t.get('approx') or {}), 'where': t['where']})
            out.append(x)
        return out


def week_quota(rl, start, end):
    """Codex“占周额度约 x%”：rollout token_count.rate_limits 里每周窗口的已用％在时段内的增量，按重置时间（同一个周窗口）分组相加。
    每组：时段内最大值 − 基线（时段开始前这一组最后一个样本；没有就用时段内最小值）。只算 limit_id 为 codex（或没写）的。没有样本返回 None。"""
    groups = {}
    for ts, lid, up, ra in rl:
        if lid not in ('', 'codex'):
            continue
        groups.setdefault(ra, []).append((ts, up))
    total, any_in = 0.0, False
    for ra, xs in groups.items():
        xs.sort()
        inr = [u for t, u in xs if start <= t < end]
        if not inr:
            continue
        any_in = True
        before = [u for t, u in xs if t < start]
        base = before[-1] if before else min(inr)
        total += max(0.0, max(inr) - base)
    return round(total, 1) if any_in else None


# ================================================================ 挂到 server 上
def setup(ns):
    """server.py import 后调用一次（导入时不扫任何目录）：
    1. 不动 server 的 USAGE（不换对象、不改参数、不包它的方法）；Claude 一栏只读它的 query()、build()。
    2. 包 API['usage']：原函数返回的字段一个不动，往里加 providers、usageBuckets、speed、ranking、hasTasks；provider 参数只过滤新字段。
    3. 包 API['usage-summary']：返回原样，只在第一次调用时后台启动 Codex、agy 的首扫（和 usage 一样懒启动）。"""
    global NS, HUB
    NS = ns
    import providers as PV

    def conv_title(sid):
        c = PV.CONVS.get(sid)
        return c.title() if c is not None else ''

    def names():
        try:
            return ns['load_conf']().get('names', {}) or {}
        except Exception:
            return {}
    HUB = Hub(home=lambda: ns.get('HOME') or os.path.expanduser('~'), conv_dir=lambda: PV.CONV_DIR, data_dir=lambda: PV.DATA_DIR,
              cache_path=lambda: os.path.join(ns['RUN_DIR'], 'usage-p-cache.json') if ns.get('RUN_DIR') else None,
              claude=lambda: ns.get('USAGE'), titles=lambda sids: ns['usage_titles'](sids), names=names, conv_title=conv_title)
    API = ns['API']
    orig = API['usage']
    orig_summary = API.get('usage-summary')

    def api_usage(b):
        b = b or {}
        if b.get('legacy') in ('codex', 'agy'):  # 11-23：Codex、Antigravity 的老样式页，只出这一家（不带 Claude 的原有字段）
            return HUB.legacy(str(b.get('range') or 'today'), b['legacy'])
        d = orig(b)
        try:
            extra = HUB.query(str(b.get('range') or 'today'), str(b.get('provider') or 'all'), bool(b.get('rollup')), claude_q=d,
                              claude_scope='all' if b.get('claudeScope') == 'all' else 'wb')
            d = dict(d)
            for k, v in extra.items():
                if k not in d:  # 原有字段一个不动
                    d[k] = v
        except Exception as e:
            log('三家用量汇总出错', repr(e))
        return d
    API['usage'] = api_usage

    def api_usage_summary(b):
        d = orig_summary(b) if orig_summary else {}
        try:
            HUB.start()  # 只起一次，后台跑；返回值原样
        except Exception as e:
            log('启动用量扫描失败', repr(e))
        return d
    if orig_summary:
        API['usage-summary'] = api_usage_summary
