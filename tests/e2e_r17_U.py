"""第十七轮 U 端到端：真后端（quota_hub.py、usage_p.py、usage.py）＋造出来的三家记录＋界面断言。
默认运行：在本目录起测试服务 47961（CWB_TITLE_DELAY=99999），临时 HOME（tests/r17u_usage_cases.build_fixtures 造的 Claude、Codex、agy 记录
和 crs 额度文件）、临时数据目录；Codex 额度用假 app-server（tests/fakecodex.py app-server），agy 用假命令（tests/fakeagy）。
不启动 claude、真 codex、真 agy；跑完按 PID＋命令行结束测试服务和它拉起的 ptyd，并检查没有残留进程。
  api_checks(base, token, facts)：接口层（主线程写）——quota、usage 原有字段不变，新字段、三家请求数、provider 过滤、子任务汇总。
  ui_checks(base_url, token)：界面层（子代理 C 写），用无头浏览器（agent-browser）做 DOM 断言，返回 (通过数, 失败清单)：
    A. 真后端：设置“额度与速度”页名、三块、顶部筛选；额度卡和 quota 返回一致（剩余％、匀速刻度、灰卡原因）；速度表的“—”和“约”；
       子任务勾选跟 hasTasks 走；侧栏没有额度小条（11-23 去掉）；切提供方后记住选择。
    B. 页面里替换 CWB.api 返回固定数据（不依赖后端状态）：灰卡、x 分钟前、可能已过时、代理提示、套餐、积分、重置券（只显示）、
       用完提示、节奏图标、悬停原因、折线和指标切换、最近 20 轮打开对话、指标卡（Codex 不显示钱、占周额度）、总览、明细、
       对话排行和子任务勾选、扫描进度和 2 秒轮询、quota 事件、Claude 老额度卡（没有 5 小时、条宽＝剩余）、关掉设置后不再轮询。
    C. 手机宽度（390px，含远程页面的样式）不出横向滚动。  D. 深浅两套主题下文字对比度。  最后看页面有没有脚本错误。
用法：
  .venv/bin/python -I tests/e2e_r17_U.py                                              # 全部（接口＋界面）
  .venv/bin/python -I tests/e2e_r17_U.py --url http://127.0.0.1:47961 --token <令牌>   # 只跑界面，用已经起好的服务
截图（自查用）：设环境变量 CWB_UI_SHOTS=<目录>。
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SESSION = 'r17u'
PORT = 47961


def _ab(*a, timeout=40):
    p = subprocess.run(['agent-browser', '--session', SESSION] + list(a), capture_output=True, text=True, timeout=timeout)
    return p.returncode, ((p.stdout or '') + (p.stderr or '')).strip()


def _ev(js):
    rc, out = _ab('eval', 'JSON.stringify((() => { ' + js + ' })())')
    try:
        v = json.loads(out)
        return json.loads(v) if isinstance(v, str) else v
    except Exception:
        return {'_raw': out[:400]}


def _wait(js, timeout=15, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        if _ev('return !!(' + js + ');') is True:
            return True
        time.sleep(step)
    return False


# ---------------------------------------------------------------- 固定数据（在页面里按当前时间生成）
STUB_JS = r'''
const p2 = n => String(n).padStart(2, '0'), dk = d => d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate());
window.__uuMk = () => {
  const now = Date.now(), M = 60000;
  const w = (id, label, kind, used, mins, rm, group) => Object.assign({id, label, kind, usedPercent: used, windowMins: mins, resetsAt: Math.round(now + rm * M)}, group ? {group} : {});
  return {
    claude: {id: 'claude', label: 'Claude', ok: true, ts: now - 40000, stale: false, src: 'file',
      windows: [w('5h', '5 小时', '5h', 80, 300, 125.5), w('7d', '每周', 'week', 20, 10080, 4000.5), w('7d-fable', '每周 · Fable', 'model', 61, 10080, 4000.5)]},
    codex: {id: 'codex', label: 'Codex', ok: false, ts: now - 30000, stale: true, src: 'protocol', plan: 'pro',
      credits: {hasCredits: true, unlimited: false, balance: '120'}, resetCredits: {availableCount: 2, nextExpiresAt: now + (3 * 24 + 5) * 60 * M + 30000},
      windows: [], viaProxy: true, unavailable: {reason: 'probeFailed', text: '这次没读到 Codex 额度'},
      lastGood: {ts: now - 22.2 * M, windows: [w('secondary', '每周', 'week', 72, 10080, 2000)]}},
    agy: {id: 'agy', label: 'Antigravity', ok: true, ts: now - 5000, stale: false, src: 'cli',
      windows: [w('5h', '5 小时', '5h', 80, 300, 125.5), w('7d', '每周', 'week', 20, 10080, 4000.5), w('7d-fable', '每周 · Fable', 'model', 61, 10080, 4000.5),
        w('gemini-5h', 'Gemini · 5 小时', '5h', 12, 300, 200, 'Gemini'), w('3p-5h', 'Claude 与 GPT · 5 小时', '5h', 100, 300, 47, 'Claude 与 GPT')]},
    // 11-23：Claude 卡用 quota 接口原有字段（第十四轮老额度卡）
    legacy: {ok: true, err: '', ts: Math.round(now / 1000) - 40, age: 40, stale: false, windows: [
      {key: '5h', label: '5 小时', used: 30, resetAt: Math.round(now / 1000) + 3600, window: 18000, headroom: 50},
      {key: '7d', label: '7 天', used: 82, resetAt: Math.round(now / 1000) + 3 * 86400, window: 604800, headroom: -25},
      {key: 'Fable', label: 'Fable', used: 96, resetAt: Math.round(now / 1000) + 3 * 86400, window: 604800, headroom: 4}]},
    tightest: {provider: 'codex', providerLabel: 'Codex', label: '每周', usedPercent: 93, resetsAt: Math.round(now + 2000 * M), kind: 'week'},
  };
};
window.__uuUse = window.__uuUse || {hasTasks: true, scan: 0};
window.__uuUsage = b => {
  const S = window.__uuUse, now = Date.now(), range = b.range || 'today', prov = b.provider || 'all', roll = !!b.rollup;
  const L = {claude: 'Claude', codex: 'Codex', agy: 'Antigravity'};
  const pids = ['claude', 'codex', 'agy'].filter(p => prov === 'all' || p === prov);
  const MD = {claude: [['claude-opus-4-7', 'Opus 4.7'], ['claude-sonnet-4-6', 'Sonnet 4.6']], codex: [['gpt-5.2-codex', 'GPT-5.2 Codex'], ['gpt-5.2', 'GPT-5.2']], agy: [['gemini-3-pro', 'Gemini 3 Pro']]};
  const base = new Date(now); base.setHours(0, 0, 0, 0);
  const keys = [];
  if (range === 'today') { const h = new Date(now).getHours(); for (let i = 0; i <= h; i++) keys.push({day: dk(base), hourStart: dk(base) + 'T' + p2(i) + ':00'}); }
  else { const n = range === '7d' ? 7 : 30; for (let i = n - 1; i >= 0; i--) { const t = new Date(base); t.setDate(t.getDate() - i); keys.push({day: dk(t)}); } }
  const ub = [], kp = {};
  pids.forEach((pid, pi) => {
    const k = kp[pid] = {requests: 0, turns: 0, sessions: 3 + pi, uncachedInput: 0, cachedInput: 0, cacheWrite: 0, output: 0, reasoning: 0, tokens: 0,
      cost: pid === 'codex' ? null : pid === 'agy' ? null : 0, costSource: pid === 'claude' ? 'modelPriced' : 'unpriced', cacheHit: 0.8,
      saved: pid === 'claude' ? 1.5 : null, weekQuota: pid === 'codex' ? 12.4 : null};
    keys.forEach((kk, i) => MD[pid].forEach(([m], j) => {
      const bs = 1000 * (pi + 1) * (j + 1) * (i % 3 + 1), unc = bs, ca = bs * 4, cw = bs >> 1, out = bs >> 2, cost = pid === 'claude' ? (unc * 5 + out * 25) / 1e6 : 0;
      ub.push(Object.assign({provider: pid, model: m, totals: {uncachedInputTokens: unc, cachedInputTokens: ca, cacheCreationTokens: cw, outputTokens: out, reasoningTokens: 0},
        costUsd: cost, cacheSavingsUsd: 0, costSource: pid === 'claude' ? 'modelPriced' : 'unpriced', records: 2, unpricedRecords: pid === 'claude' ? 0 : 2, sessions: 1}, kk));
      k.requests += 2; k.turns += 1; k.uncachedInput += unc; k.cachedInput += ca; k.cacheWrite += cw; k.output += out; k.tokens += unc + ca + cw + out;
      if (pid === 'claude') k.cost += cost;
    }));
  });
  const scanning = S.scan > 0;
  if (S.scan > 0) S.scan--;
  const providers = pids.map(pid => Object.assign({id: pid, label: L[pid], scope: pid === 'claude' ? 'workbench' : 'all', scanning: pid === 'agy' && scanning, kpi: kp[pid]},
    pid === 'agy' && scanning ? {progress: {files: 1, totalFiles: 3, bytes: 420, totalBytes: 1000}} : {}));
  const CW = {ttft: '只算工作台的 Claude 统计没有记首字时间', turn: '只算工作台的 Claude 统计没有记每轮用时'};
  const sp = pids.map(pid => pid === 'claude'
    ? {id: pid, label: L[pid], tps: 52.3, ttft: null, turn: null, turnP90: null, approx: {tps: true}, why: CW,
      models: MD[pid].map(([m, l], j) => ({model: m, label: l, requests: 10, turns: 4, tps: 60 - j, ttft: null, turn: null, turnP90: null, approx: {tps: true}, why: CW}))}
    : {id: pid, label: L[pid], tps: pid === 'agy' ? null : 52.3, ttft: pid === 'codex' ? null : 2100, turn: 28000, turnP90: 70000,
      approx: {}, why: pid === 'codex' ? {ttft: '终端里的 Codex 记录没有首字时间'} : {},
      models: MD[pid].map(([m, l], j) => ({model: m, label: l, requests: 10, turns: 4, tps: pid === 'agy' ? null : 60 - j, ttft: 1800, turn: 24000,
        turnP90: j === 1 ? null : 61000, approx: {}, why: pid === 'agy' ? {tps: '这个模型的记录里没有 token 数'} : {}}))});
  const unit = range === '30d' ? 'day' : 'hour', pts = [];
  const np = range === 'today' ? 24 : range === '7d' ? 168 : 30;
  for (let i = 0; i < np; i++) {
    const t = new Date(base); if (unit === 'day') t.setDate(t.getDate() - (29 - i)); else t.setHours(t.getHours() + i - (range === '7d' ? 144 : 0));
    const pt = {key: unit === 'day' ? dk(t) : dk(t) + 'T' + p2(t.getHours()) + ':00', label: unit === 'day' ? dk(t).slice(5) : p2(t.getHours()) + ':00'};
    pids.forEach((pid, pi) => { pt[pid] = pid === 'claude' ? {tps: 40 + (i % 5) * 3} : {tps: 40 + pi * 8 + (i % 5) * 3, ttft: 1200 + pi * 400 + (i % 4) * 300, turn: 20000 + pi * 5000 + (i % 6) * 2000}; });
    pts.push(pt);
  }
  const recent = [], rp = pids.filter(p => p !== 'claude');
  for (let i = 0; rp.length && i < 24; i++) {
    const pid = rp[i % rp.length], [m, l] = MD[pid][i % MD[pid].length];
    const r = {ts: now - i * 600000, provider: pid, model: m, label: l, title: '演示对话 ' + (i + 1), ttft: pid === 'codex' && i % 2 ? null : 1500 + i * 30,
      turn: 20000 + i * 400, output: 800 + i * 10, tps: 50 + i % 7, approx: pid === 'claude' ? {ttft: true} : {}, where: i % 2 ? 'other' : 'workbench'};
    if (!(i % 2)) r.sid = 'stub-sid-' + i;
    recent.push(r);
  }
  const ranking = [];
  for (let i = 0; i < 12; i++) {
    const pid = pids[i % pids.length];
    const r = {key: 'k' + i, provider: pid, title: '排行对话 ' + (i + 1), requests: 100 - i * 6, tokens: 9e6 - i * 6e5, cost: pid === 'claude' ? 12.5 - i * 0.8 : null};
    if (!(i % 2)) r.sid = 'stub-rank-' + i;
    if (roll && i === 0) r.children = 3;
    ranking.push(r);
  }
  return {scanning: false, updated: now, range, unit, kpi: {requests: 0, tokens: 0}, buckets: [], models: [], sessions: [], recent: [],
    providers, usageBuckets: ub, speed: {providers: sp, series: {unit, points: pts}, recent}, ranking, hasTasks: !!S.hasTasks};
};
/* 11-23：Codex、agy 的老样式页取 usage {legacy}，字段同 usage.py query()（Codex 不算钱、有占周额度；agy 有提供方报的费用） */
window.__uuLegacy = b => {
  const now = Date.now(), range = b.range || 'today', p = b.legacy, day0 = new Date(); day0.setHours(0, 0, 0, 0);
  const n = range === 'today' ? 24 : range === '7d' ? 7 : 30, cost = p === 'codex' ? null : 0.03;
  const ms = p === 'codex' ? [['gpt-5.2-codex', 'GPT-5.2 Codex'], ['gpt-5.2', 'GPT-5.2']] : [['gemini-3.8-flash', 'Gemini 3.8 Flash'], ['claude-opus-5-5', 'Opus 5.5']];
  const buckets = [];
  for (let i = 0; i < n; i++) {
    const key = range === 'today' ? dk(day0) + ' ' + p2(i) : dk(new Date(day0.getTime() - (n - 1 - i) * 86400000));
    const t1 = 1000 * (i % 3 + 1), t2 = 400 * (i % 2), mm = {[ms[0][0]]: {tokens: t1, cost: 0, requests: 2}};
    if (t2) mm[ms[1][0]] = {tokens: t2, cost: 0, requests: 1};
    buckets.push({key, label: range === 'today' ? p2(i) + ':00' : key.slice(5), requests: t2 ? 3 : 2, tokens: t1 + t2, output: 100, cost: cost == null || i % 2 ? null : 0.001, models: mm});
  }
  const heat = [];
  for (let k = 370; k >= 0; k--) heat.push({date: dk(new Date(day0.getTime() - k * 86400000)), tokens: k < 30 ? 1000 * (k % 4) : 0, requests: k < 30 ? k % 4 : 0});
  const kpi = {requests: buckets.reduce((a, x) => a + x.requests, 0), input: 1000, output: 300, cacheRead: 5000, cacheWrite: 0,
    tokens: buckets.reduce((a, x) => a + x.tokens, 0), cost, cacheHit: 0.8, speed: 42.5, saved: null, pricedRequests: cost == null ? 0 : 2};
  if (p === 'codex') { kpi.weekQuota = 12.4; kpi.approxSpeed = true; }
  return {scanning: false, updated: now, range, unit: range === 'today' ? 'hour' : 'day', provider: p, kpi, today: {requests: 3, tokens: 1000, cost},
    buckets, models: ms.map(([id, label], i) => ({id, label, requests: i ? 10 : 20, tokens: i ? 2000 : 8000, output: 100, cost, share: i ? 0.2 : 0.8, speed: i ? null : 40})),
    heat, heatSince: dk(new Date(day0.getTime() - 29 * 86400000)),
    sessions: [{sid: 'stub-wb-1', title: p + ' 工作台对话', requests: 9, tokens: 9000, cost}, {sid: '', title: p + ' 终端对话', requests: 5, tokens: 5000, cost},
      {sid: '', title: '未命名对话', requests: 1, tokens: 100, cost}],
    recent: [0, 1, 2, 3, 4].map(i => ({ts: now - i * 60000, model: ms[0][0], label: ms[0][1], fast: false, input: 10, output: 50, cacheRead: 1000, cacheWrite: 0,
      ms: i % 2 ? null : 2000, speed: i % 2 ? null : 25, cost}))};
};
window.__uuReal = window.__uuReal || CWB.api;
window.__uuCalls = [];
CWB.api = (n, b) => {
  if (n === 'quota') {
    window.__uuCalls.push(['quota', JSON.parse(JSON.stringify(b || {}))]);
    const f = window.__uuMk(), prov = (b && b.provider) || 'all';
    return Promise.resolve(Object.assign({}, f.legacy, {providers: [f.claude, f.codex, f.agy].filter(p => prov === 'all' || p.id === prov), tightest: f.tightest}));
  }
  if (n === 'usage') { window.__uuCalls.push(['usage', JSON.parse(JSON.stringify(b || {}))]); return Promise.resolve(b && b.legacy ? window.__uuLegacy(b) : window.__uuUsage(b || {})); }
  return window.__uuReal(n, b);
};
/* 后端推的 quota 事件在 B 段里先拦下（避免真数据覆盖固定数据）；测事件时直接调原来的钩子 */
window.__uuOnEv = window.__uuOnEv || CWB.onEvent;
CWB.onEvent = ev => { if (ev && ev.type === 'quota') return; return window.__uuOnEv(ev); };
return 1;
'''

# 页面里独立算 T3 的剩余％和匀速刻度（不调被测代码）
CALC_JS = r'''
window.__uuExp = (w, now) => {
  const rem = Math.round(100 - Math.max(0, Math.min(100, +w.usedPercent || 0)));
  let el = null;
  const r = typeof w.resetsAt === 'number' && w.resetsAt > 0 ? (w.resetsAt < 1e12 ? w.resetsAt * 1000 : w.resetsAt) : null;
  if (r !== null && +w.windowMins > 0) { const len = w.windowMins * 60000; el = Math.max(0, Math.min(1, (len - (r - now)) / len)); }
  else if (typeof w.elapsed === 'number') el = Math.max(0, Math.min(1, w.elapsed));
  return {rem, tl: el === null ? null : Math.round((1 - el) * 100), pace: el === null ? null : (() => { const g = (+w.usedPercent || 0) - el * 100; return g > 5 ? 'ahead' : g < -5 ? 'under' : 'on'; })()};
};
return 1;
'''

# 11-23：只看某一家时的第十四轮老样式页，读出页面上的关键内容
OLD_PAGE_JS = r'''
const T = s => (document.querySelector(s) || {}).textContent || '', qc = document.querySelector('.uu-qbody-old .uu-qold');
return {newSecs: document.querySelectorAll('.uu-sec, .uu-page, .uu-spd, .uu-pk').length, h2: T('.sj-body .sj-hd h2'), lead: T('.sj-body .sj-lead'),
  prov: T('.uu-top-old [data-k=uprov] .on'), cards: [...document.querySelectorAll('.sj-body .sj-qcard')].map(c => c.dataset.p),
  qstale: !!(qc && qc.querySelector('.sj-qh span.stale')), qupd: qc ? (qc.querySelector('.sj-qh span') || {}).textContent : '', plan: T('.uu-qold .uu-qplan'),
  qerr: T('.uu-qold .sj-qerr'), wide: !!(qc && qc.classList.contains('uu-qwide')), foot: T('.uu-qold .uu-qfoot'),
  qrows: qc ? [...qc.querySelectorAll('.sj-q')].map(x => [x.dataset.k, x.querySelector('.sj-qu').textContent, x.querySelector('.sj-qbar i').style.width, x.querySelector('.sj-qm').textContent, x.className]) : [],
  kpis: [...document.querySelectorAll('.sj-kpi')].map(x => [x.querySelector('.k').textContent, x.querySelector('.v').textContent, x.querySelector('.d').textContent]),
  metric: [...document.querySelectorAll('.sj-chart [data-k=metric] button')].map(x => x.textContent), bars: document.querySelectorAll('.sj-bars .sj-bar').length,
  top4: [...document.querySelectorAll('.sj-top4 .tr:not(.hd) .nm')].map(x => x.textContent), hnote: T('.sj-hnote'), na: document.querySelectorAll('.sj-heat i[data-na]').length,
  links: document.querySelectorAll('.sj-sess .sj-link[data-sid]').length, plain: document.querySelectorAll('.sj-sess .sj-snm').length,
  scost: [...document.querySelectorAll('.sj-sess .n.c')].map(x => x.textContent), rec: document.querySelectorAll('.sj-recent tbody tr').length,
  calls: window.__uuCalls || []};
'''

# 对照 DOM 和页面拿到的额度数据（同一次同步执行里读，事件不会插进来）
QUOTA_CMP_JS = r'''
const now = Date.now(), map = CWB.uu.quota.map, out = [];
const ids = CWB.uu.state.prov === 'all' ? ['claude', 'codex', 'agy'] : [CWB.uu.state.prov];
if (ids.includes('claude')) {  // 11-23：Claude 是第十四轮老额度卡：没有 5 小时，条宽＝剩余，右侧“剩余 X%”
  const lg = CWB.uu.quota.legacy, card = document.querySelector('.uu-qold[data-p="claude"]');
  if (!card) out.push('claude：没有老额度卡');
  else if (lg && !lg.err) {
    const ws = (lg.windows || []).filter(w => w.key !== '5h'), rows = card.querySelectorAll('.sj-q');
    if (card.querySelector('.sj-q[data-k="5h"]')) out.push('claude：不该有 5 小时');
    if (rows.length !== ws.length) out.push(`claude：窗口条数 ${rows.length} ≠ ${ws.length}`);
    ws.forEach((w, i) => { const row = rows[i]; if (!row) return; const rem = 100 - Math.max(0, Math.min(100, +w.used || 0));
      const u = (row.querySelector('.sj-qu') || {}).textContent, bw = (row.querySelector('.sj-qbar i') || {style: {}}).style.width;
      if (u !== `剩余 ${Math.round(rem)}%` || bw !== `${rem}%`) out.push(`claude/${w.label}：${u} 条宽 ${bw} ≠ 剩余 ${rem}%`); });
  }
}
for (const id of ids) {
  const p = map[id]; if (!p || id === 'claude') continue;
  const card = document.querySelector(`.uu-qc[data-p="${id}"]`);
  if (!card) { out.push(id + '：没有卡片'); continue; }
  const off = card.classList.contains('uu-off');
  if (off !== !p.ok) out.push(id + '：灰卡状态不对');
  if (!p.ok) { const why = (card.querySelector('.uu-why') || {}).textContent || ''; const want = (p.unavailable && p.unavailable.text) || '无法读取限额。'; if (why !== want) out.push(`${id}：原因 ${why} ≠ ${want}`); }
  const ws = (p.windows && p.windows.length) ? p.windows : (!p.ok && p.lastGood && p.lastGood.windows) || [];
  const rows = card.querySelectorAll('.uu-w');
  if (rows.length !== ws.length) out.push(`${id}：窗口条数 ${rows.length} ≠ ${ws.length}`);
  ws.forEach((w, i) => {
    const row = rows[i]; if (!row) return;
    const e = window.__uuExp(w, now);
    const lt = (row.querySelector('.uu-left') || {}).textContent;
    if (lt !== `剩余 ${e.rem}%`) out.push(`${id}/${w.label}：${lt} ≠ 剩余 ${e.rem}%`);
    const tk = row.querySelector('.uu-tick');
    if (e.tl === null) { if (tk) out.push(`${id}/${w.label}：不该有刻度`); }
    else if (!tk) out.push(`${id}/${w.label}：缺刻度`);
    else { const tl = +tk.dataset.tl; if (Math.abs(tl - e.tl) > 1 || !tk.getAttribute('style').includes(`, ${tl}%,`)) out.push(`${id}/${w.label}：刻度 ${tl} ≠ ${e.tl}`); }
    const pc = row.querySelector('.uu-pace');
    if ((pc ? pc.dataset.pace : null) !== e.pace && e.pace !== null) out.push(`${id}/${w.label}：节奏 ${pc && pc.dataset.pace} ≠ ${e.pace}`);
  });
}
return {n: ids.filter(id => map[id]).length, cards: document.querySelectorAll('.uu-qc, .uu-qold').length, bad: out};
'''

# 11-22 晚口径：providers[].scope 为 workbench 的（Claude）在指标卡标题和速度表那一行注明“只算工作台”
SCOPE_CMP_JS = r'''
const d = CWB.uu.last() || {}, want = id => CWB.uu.state.prov === 'all' || CWB.uu.state.prov === id;
const ps = (d.providers || []).filter(p => want(p.id)), wb = ps.filter(p => p.scope === 'workbench').map(p => p.id);
return {wb, cards: [...document.querySelectorAll('.uu-pk')].filter(c => c.querySelector('.uu-pkh .uu-scope')).map(c => c.dataset.p),
  wbRows: wb.filter(id => document.querySelector(`.uu-spd tr.pv[data-p="${id}"]`)),
  rows: [...document.querySelectorAll('.uu-spd tr.pv')].filter(t => t.querySelector('.uu-scope')).map(t => t.dataset.p),
  txt: [...document.querySelectorAll('.uu-pkh .uu-scope, .uu-spd .uu-scope')].map(x => x.textContent),
  hasScope: (d.providers || []).some(p => 'scope' in p), claude: ((d.providers || []).find(p => p.id === 'claude') || {}).scope};
'''

# 速度表：null 显示“—”且悬停是 why 里的原因；估算值有“约”
SPEED_CMP_JS = r'''
const d = CWB.uu.last(), out = [], want = id => CWB.uu.state.prov === 'all' || CWB.uu.state.prov === id;
if (!d) return {bad: ['没有用量数据'], n: 0};
const ps = ((d.speed || {}).providers || []).filter(p => want(p.id));
let nNa = 0, nApprox = 0;
const cmp = (o, tr, who) => {
  const tds = tr.querySelectorAll('td');
  [['tps', 2], ['ttft', 3], ['turn', 4], ['turnP90', 5]].forEach(([k, i]) => {
    const td = tds[i]; if (!td) { out.push(who + '缺格子'); return; }
    const ak = k === 'turnP90' ? 'turn' : k, v = o[k];
    if (v == null) { const na = td.querySelector('.uu-na'); nNa++; const w = (o.why && o.why[ak]) || '暂无数据';
      if (!na || na.textContent !== '—' || na.getAttribute('data-uutip') !== w) out.push(`${who}${k}：“—”或原因不对（${na && na.getAttribute('data-uutip')} ≠ ${w}）`); }
    else if (o.approx && o.approx[ak]) { nApprox++; if (!td.textContent.startsWith('约 ')) out.push(`${who}${k}：估算值没有“约”`); }
    else if (td.textContent.startsWith('约')) out.push(`${who}${k}：不该有“约”`);
  });
};
for (const p of ps) {
  const tr = document.querySelector(`.uu-spd tr.pv[data-p="${p.id}"]`);
  if (!tr) { out.push(p.id + '：速度表缺这一家'); continue; }
  cmp(p, tr, p.id + ' ');
  const mrs = document.querySelectorAll(`.uu-spd tr.mr[data-p="${p.id}"]`);
  if (mrs.length !== (p.models || []).length) out.push(`${p.id}：模型行 ${mrs.length} ≠ ${(p.models || []).length}`);
  (p.models || []).forEach((m, i) => mrs[i] && cmp(m, mrs[i], `${p.id}/${m.model} `));
}
return {n: ps.length, nNa, nApprox, bad: out, hasTasks: !!d.hasTasks, roll: !!document.querySelector('.uu-roll input'),
  pk: [...document.querySelectorAll('.uu-pk')].map(x => x.dataset.p), provs: (d.providers || []).filter(p => want(p.id)).map(p => p.id),
  codexMoney: [...document.querySelectorAll('.uu-pk[data-p=codex] .uu-k .k')].some(k => /费用/.test(k.textContent)),
  codexWq: (() => { const p = (d.providers || []).find(x => x.id === 'codex'); return p && p.kpi && p.kpi.weekQuota != null; })(),
  codexWqShown: [...document.querySelectorAll('.uu-pk[data-p=codex] .uu-k .k')].some(k => k.textContent === '占周额度')};
'''

LUM_JS = r'''
const rgb = s => { const m = (s || '').match(/[\d.]+/g); return m ? m.slice(0, 3).map(Number) : [0, 0, 0]; };
const L = c => { const [r, g, b] = rgb(c).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
const cr = (a, b) => { const x = L(a), y = L(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
const bg = getComputedStyle(document.querySelector('.sj-panel')).backgroundColor;
const pick = s => { const e = document.querySelector(s); return e ? getComputedStyle(e).color : ''; };
return {bg, left: cr(pick('.uu-left'), bg), big: cr(pick('.uu-big'), bg), why: cr(pick('.uu-why'), bg), td: cr(pick('.uu-spd tr.mr td'), bg),
  tick: cr(getComputedStyle(document.querySelector('.uu-tick')).backgroundColor, bg)};
'''


def ui_checks(base_url, token, shots=None):
    """界面 DOM 断言。base_url 例如 http://127.0.0.1:47961；token 是页面地址里的 ?t= 令牌；shots 给目录时顺便截图（只供自查）。
    返回 (通过数, 失败清单)。只操作浏览器，不改后端数据（测试里会点一下“刷新”，quota 接口会带 refresh:true）。"""
    res = {'ok': 0, 'fail': []}

    def check(cond, msg):
        print(('通过 ' if cond else '失败 ') + msg, flush=True)
        if cond:
            res['ok'] += 1
        else:
            res['fail'].append(msg)
        return cond

    def shot(name):
        if shots:
            os.makedirs(shots, exist_ok=True)
            _ab('screenshot', os.path.join(shots, name))

    ev, wait = _ev, _wait
    url = '%s/?t=%s' % (base_url.rstrip('/'), token)
    try:
        _ab('set', 'viewport', '1280', '860')
        _ab('open', url)
        check(wait('window.CWB && CWB.uu && CWB.sj && document.getElementById("hMe")', 25), '页面加载，额度与速度模块已挂上')
        theme0 = ev("return localStorage.getItem('cwb.pref.theme');")
        ev("for (const k of Object.keys(localStorage)) if (k.startsWith('cwb.uu.')) localStorage.removeItem(k); return 1;")
        _ab('reload')
        wait('window.CWB && CWB.uu && CWB.sj', 25)
        ev(CALC_JS)
        # ---------------- A. 真后端
        ev("CWB.openSettings('usage'); return 1;")
        check(wait("document.querySelector('.uu-page')", 10), '打开设置的 usage 分区，渲染新页面')
        r = ev("""return {nav: (document.querySelector('#sjSet .sj-nav [data-s=usage]') || {}).textContent, h2: document.querySelector('.sj-body .sj-hd h2').textContent,
          secs: [...document.querySelectorAll('.uu-sec')].map(s => [s.dataset.b, s.querySelector('.uu-h').firstChild.textContent]),
          prov: [...document.querySelectorAll('.uu-top [data-k=uprov] button')].map(b => b.textContent), on: (document.querySelector('.uu-top [data-k=uprov] .on') || {}).textContent,
          range: [...document.querySelectorAll('.uu-top [data-k=urange] button')].map(b => b.textContent), old: !!document.querySelector('.sj-body .sj-ubar')};""")
        check(r.get('nav') == '额度与速度' and r.get('h2') == '额度与速度', f'导航和标题改名为“额度与速度”（{r.get("nav")}／{r.get("h2")}）')
        check(r.get('secs') == [['quota', '额度'], ['speed', '速度'], ['usage', '用量']], f'三块：额度、速度、用量（{r.get("secs")}）')
        check(r.get('prov') == ['全部', 'Claude', 'Codex', 'Antigravity'] and r.get('on') == '全部', f'提供方：全部／Claude／Codex／Antigravity，默认全部（{r.get("prov")}）')
        check(r.get('range') == ['今天', '7 天', '30 天'] and not r.get('old'), f'时段：今天／7 天／30 天，旧的用量页不再渲染（{r.get("range")}）')
        check(wait("document.querySelector('.uu-qc, .uu-qold') || /暂无额度数据/.test(document.querySelector('.uu-qbody').textContent)", 25), '额度块读到数据')
        ev("window.__uuQ = null; CWB.api('quota', {provider: 'all'}).then(d => { window.__uuQ = d; }); return 1;")
        wait('window.__uuQ', 25)
        wait("!document.querySelector('.uu-qc .uu-chk')", 25)  # 后台刷新的转圈结束（或超时）后再对照
        time.sleep(0.6)
        qids = ev("return ((window.__uuQ || {}).providers || []).map(p => p.id);")
        r = ev(QUOTA_CMP_JS)
        check(isinstance(qids, list) and sorted(qids) == sorted(ev("return [...document.querySelectorAll('.uu-qc, .uu-qold')].map(c => c.dataset.p);") or []),
              f'quota 返回的每一家都有一张卡（{qids}）')
        check(isinstance(r, dict) and not r.get('bad') and r.get('cards') == r.get('n'), f'额度卡和数据一致：Claude 老额度卡（没有 5 小时、条宽＝剩余），另两家剩余％、匀速刻度、节奏、灰卡原因（{r}）')
        check(wait("CWB.uu.last() && !/读取中/.test(document.querySelector('.uu-ubody').textContent)", 30), '速度、用量读到数据')
        r = ev(SPEED_CMP_JS)
        check(isinstance(r, dict) and not r.get('bad'), f'速度表：null 显示“—”且悬停原因来自 why，估算值加“约”（{r.get("n")} 家，— {r.get("nNa")} 格，约 {r.get("nApprox")} 格，{r.get("bad")}）')
        check(r.get('roll') == r.get('hasTasks'), f'“子任务算到父对话头上”跟 hasTasks 走（hasTasks={r.get("hasTasks")}）')
        check(r.get('pk') == r.get('provs'), f'用量指标卡按提供方分栏（{r.get("pk")}）')
        check(not r.get('codexMoney') and (r.get('codexWqShown') == bool(r.get('codexWq'))), 'Codex 指标卡不显示钱；占周额度有数才显示')
        r = ev(SCOPE_CMP_JS)
        check(r.get('cards') == r.get('wb') and r.get('rows') == r.get('wbRows') and (not r.get('hasScope') or r.get('claude') == 'workbench')
              and all(t == '只算工作台' for t in r.get('txt') or []),
              f'scope 为 workbench 的提供方（Claude）在指标卡和速度表注明“只算工作台”，其他家不注明（{r}）')
        r = ev("""const me = document.getElementById('hMe');
          return {strip: !!document.getElementById('uuStrip'), use: (document.getElementById('hUse') || {}).textContent || '', mood: !!document.getElementById('hMood'),
            kids: me ? me.querySelector('.h-me-t').children.length : -1};""")
        check(not r.get('strip') and r.get('mood') and r.get('kids') == 2, f'侧栏账号行恢复上线前：没有额度小条，只有心情和今天的请求、费用（{r}）')
        shot('a-real.png')
        # 切提供方：只剩这一家，选择记住
        ev("document.querySelector('.uu-top [data-k=uprov] [data-v=codex]').click(); return 1;")
        # 11-23：只看某一家是第十四轮老样式页；真后端的 legacy 数据和页面上的请求数一致
        wait("document.querySelector('.uu-top-old') && document.querySelector('.uu-qold[data-p=codex]') && document.querySelector('.sj-ubody .sj-kpis, .sj-ubody .sj-none')", 25)
        ev("window.__uuL = null; CWB.api('usage', {range: CWB.uu.state.range, legacy: 'codex'}).then(d => { window.__uuL = d; }); return 1;")
        wait('window.__uuL', 25)
        r = ev("""return {cards: [...document.querySelectorAll('.sj-body .sj-qcard')].map(c => c.dataset.p), ls: localStorage.getItem('cwb.uu.prov'), newSecs: document.querySelectorAll('.uu-sec').length,
          req: (document.querySelector('.sj-kpi .v') || {}).textContent || null, want: window.__uuL.kpi.requests, none: !!document.querySelector('.sj-ubody .sj-none'),
          ks: [...document.querySelectorAll('.sj-kpi .k')].map(x => x.textContent)};""")
        okreq = r.get('none') if r.get('want') == 0 else (r.get('req') == f"{r.get('want'):,}" and '占周额度' in (r.get('ks') or []))
        check(r.get('cards') == ['codex'] and r.get('ls') == '"codex"' and r.get('newSecs') == 0 and okreq,
              f'选 Codex：整页老样式，只显示 Codex，请求数和接口一致，并记住（{r}）')
        shot('a-real-codex-old.png')
        ev("document.querySelector('.uu-top [data-k=uprov] [data-v=all]').click(); return 1;")
        time.sleep(0.3)

        # ---------------- B. 固定数据
        ev("CWB.closeSettings(); localStorage.setItem('cwb.uu.range', '\"today\"'); return 1;")
        _ab('reload')
        wait('window.CWB && CWB.uu && CWB.sj', 25)
        ev(CALC_JS)
        ev(STUB_JS)
        ev("CWB.uu.state.range = 'today'; CWB.uu.state.prov = 'all'; CWB.uu.state.rollup = false; window.__uuCalls = []; CWB.openSettings('usage'); return 1;")
        check(wait("document.querySelectorAll('.uu-qc, .uu-qold').length === 3 && document.querySelector('.uu-qold[data-p=claude]') && document.querySelector('.uu-spd') && document.querySelector('.uu-pk')", 10), '固定数据：三张额度卡（Claude 是老额度卡）、速度表、指标卡')
        r = ev("return window.__uuCalls.filter(c => c[0] === 'quota')[0] || null;")
        check(isinstance(r, list) and r[1] == {'provider': 'all', 'refresh': True}, f'打开额度块时调一次 quota {{provider, refresh:true}}（{r}）')
        r = ev(QUOTA_CMP_JS)
        check(not r.get('bad'), f'固定数据：剩余％、刻度位置、节奏和独立算的一致（{r.get("bad")}）')
        r = ev("""const c = document.querySelector('.uu-qold[data-p=claude]'), rows = [...c.querySelectorAll('.sj-q')];
          return {h: c.querySelector('.sj-qh').childNodes[0].textContent, upd: c.querySelector('.sj-qh span').textContent, ks: rows.map(r => r.dataset.k),
            lbl: rows.map(r => r.querySelector('.sj-qn').textContent), u: rows.map(r => r.querySelector('.sj-qu').textContent),
            w: rows.map(r => r.querySelector('.sj-qbar i').style.width), lvl: rows.map(r => r.className.replace('sj-q ', '')), m: rows.map(r => r.querySelector('.sj-qm').textContent),
            newcard: !!document.querySelector('.uu-qc[data-p=claude]')};""")
        check(r.get('h') == 'Claude' and r.get('upd', '').startswith('更新于 ') and not r.get('newcard'), f'Claude 卡恢复第十四轮老样式，标题“Claude”，右侧“更新于 hh:mm”（{r.get("h")}，{r.get("upd")}）')
        check(r.get('ks') == ['7d', 'Fable'] and r.get('lbl') == ['7 天', 'Fable'], f'Claude 卡去掉 5 小时，标签沿用老卡（{r.get("lbl")}）')
        check(r.get('u') == ['剩余 18%', '剩余 4%'] and r.get('w') == ['18%', '4%'], f'进度条显示剩余：条宽＝100−已用，右侧“剩余 X%”（{r.get("u")}，{r.get("w")}）')
        check(r.get('lvl') == ['amber', 'red'] and r.get('m') == ['约 3 天后重置 · 无余裕', '约 3 天后重置 · 余裕 4'], f'颜色分档、重置时间和余裕照老卡（{r.get("lvl")}，{r.get("m")}）')
        r = ev("""const f = window.__uuMk(); window.__uuOnEv({type: 'quota', providers: [f.claude, f.codex, f.agy], tightest: f.tightest});
          return document.querySelectorAll('.uu-qold[data-p=claude] .sj-q').length;""")
        check(r == 2, f'quota 事件不带老字段：Claude 卡沿用上次读到的（{r} 条）')
        r = ev("""const c = s => document.querySelector(s), t = s => (c(s) || {}).textContent || '';
          const w5 = c('.uu-qc[data-p=agy] .uu-w[data-w="5h"]');
          return {rem: t('.uu-qc[data-p=agy] .uu-w[data-w="5h"] .uu-left'), reset: w5.querySelector('.uu-reset').textContent,
            tick: w5.querySelector('.uu-tick').getAttribute('style'), gap: w5.querySelector('.uu-gap').getAttribute('style'), fill: w5.querySelector('.fill').style.width,
            paces: [...document.querySelectorAll('.uu-qc[data-p=agy] .uu-pace')].slice(0, 3).map(p => p.dataset.pace),
            paceTip: c('.uu-qc[data-p=agy] .uu-pace[data-pace=ahead]').getAttribute('data-uutip'),
            barTip: w5.querySelector('.uu-bar').getAttribute('data-uutip'), aria: w5.querySelector('.uu-bar').getAttribute('aria-label')};""")
        check(r.get('rem') == '剩余 20%' and r.get('fill') == '20%', f'5 小时窗口已用 80%：显示“剩余 20%”，条长 20%（{r.get("rem")}）')
        check(r.get('reset') == '2 小时 5 分钟 后重置', f'重置倒计时按 T3 文案（{r.get("reset")}）')
        check('clamp(3px, 42%, calc(100% - 3px))' in (r.get('tick') or '') and 'clip-path' in (r.get('gap') or ''), f'匀速刻度在窗口剩余 42% 处，条上挖出缺口（{r.get("tick")}）')
        check(r.get('paces') == ['ahead', 'under', 'on'], f'节奏三档：偏快／偏慢／适中（{r.get("paces")}）')
        check(r.get('paceTip') == '消耗偏快：用量消耗进度超过本周期的时间进度', f'节奏图标悬停文案（{r.get("paceTip")}）')
        check(r.get('barTip', '').startswith('剩余 20% · 窗口剩余 42%\n竖线表示匀速消耗时的位置。') and '重置 · 2 小时 5 分钟 后重置' in r.get('barTip', ''),
              f'额度条悬停：剩余、窗口剩余、竖线说明、重置时间（{r.get("barTip")!r}）')
        check(r.get('aria') == '5 小时：剩余 20%，窗口剩余 42%，2 小时 5 分钟 后重置', f'额度条读屏文案（{r.get("aria")}）')
        r = ev("""const c = document.querySelector('.uu-qc[data-p=codex]'), t = s => (c.querySelector(s) || {}).textContent || '';
          return {off: c.classList.contains('uu-off'), why: t('.uu-why'), ago: t('.uu-ago'), stale: t('.uu-stale'), proxy: t('.uu-proxy'), plan: t('.uu-plan'),
            credits: t('.uu-credits'), rc: t('.uu-rc'), left: t('.uu-left'), btn: c.querySelectorAll('button').length,
            gray: getComputedStyle(c.querySelector('.uu-ws')).filter};""")
        check(r.get('off') and r.get('why') == '这次没读到 Codex 额度' and 'grayscale' in (r.get('gray') or ''), f'拿不到：卡片变灰，写一句原因（{r.get("why")}）')
        check(r.get('left') == '剩余 28%' and r.get('ago') == '22 分钟前', f'保留最近一次的值和“x 分钟前”（{r.get("left")}，{r.get("ago")}）')
        check(r.get('stale') == '可能已过时' and r.get('proxy') == '账号额度，和实际用量可能不符', '标“可能已过时”和代理提示')
        check(r.get('plan') == 'Pro' and r.get('credits') == '积分余额 120' and r.get('rc') == '已存有 2 个重置额度 · 下一个将在 3 天 5 小时 后过期' and r.get('btn') == 0,
              f'套餐、积分、重置券只显示、没有兑换按钮（{r.get("plan")}，{r.get("credits")}，{r.get("rc")}，按钮 {r.get("btn")}）')
        r = ev("""const c = document.querySelector('.uu-qc[data-p=agy]');
          return {out: (c.querySelector('.uu-out') || {}).textContent || '', fill: !!c.querySelector('.uu-w[data-w="3p-5h"] .fill')};""")
        check(r.get('out', '').startswith('Antigravity 用量已用完 · ') and r.get('out', '').endswith(' 重置') and not r.get('fill'), f'某个窗口用完：“{r.get("out")}”')
        # 悬停：“—”显示原因
        ev("const e = document.querySelector('.uu-spd tr.pv[data-p=codex] .uu-na'); e.dispatchEvent(new MouseEvent('mouseover', {bubbles: true})); return 1;")
        ok = wait("(() => { const t = document.querySelector('.uu-tip'); return t && !t.hidden && t.textContent === '终端里的 Codex 记录没有首字时间'; })()", 3)
        check(ok, '悬停“—”显示原因')
        ev("document.querySelector('.uu-spd').dispatchEvent(new MouseEvent('mouseout', {bubbles: true, relatedTarget: document.body})); return 1;")
        r = ev(SPEED_CMP_JS)
        check(not r.get('bad') and r.get('nNa', 0) >= 4 and r.get('nApprox', 0) >= 1, f'固定数据：速度表“—”{r.get("nNa")} 格、“约”{r.get("nApprox")} 格（{r.get("bad")}）')
        r = ev("""return {paths: document.querySelectorAll('.uu-sch path[data-s]').length, lg: [...document.querySelectorAll('.uu-sch .uu-lg span')].map(s => [...s.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('')),
          title: document.querySelector('[data-b=speed] .uu-ch h4').textContent, rec: document.querySelectorAll('.uu-rec tbody tr').length,
          links: document.querySelectorAll('.uu-rec .sj-link[data-sid]').length};""")
        check(r.get('paths') == 3 and r.get('lg') == ['Claude', 'Codex', 'Antigravity'] and r.get('title') == '输出速度（按小时）', f'速度折线：三家各一条（{r}）')
        check(r.get('rec') == 20 and r.get('links') == 10, f'最近 20 轮，工作台的对话能点开（{r.get("rec")} 行，{r.get("links")} 个链接）')
        ev("document.querySelector('[data-b=speed] [data-k=smetric] [data-v=ttft]').click(); return 1;")
        r = ev("return {title: document.querySelector('[data-b=speed] .uu-ch h4').textContent, ls: localStorage.getItem('cwb.uu.smetric'), paths: document.querySelectorAll('.uu-sch path[data-s]').length};")
        check(r.get('title') == '首字等待（按小时）' and r.get('ls') == '"ttft"' and r.get('paths') == 2, f'折线指标切到首字等待：Claude 没有这项，只画两家（{r}）')
        # 折线悬停：竖线和提示
        ev("""const p = document.querySelector('.uu-sch .uu-plot'), b = p.getBoundingClientRect();
          p.dispatchEvent(new PointerEvent('pointermove', {bubbles: true, clientX: b.left + b.width / 2, clientY: b.top + 20})); return 1;""")
        r = ev("const t = document.querySelector('.uu-sch .uu-ctip'); return {vis: !t.hidden, n: t.querySelectorAll('.r').length, hl: document.querySelector('.uu-sch .uu-hl').classList.contains('on')};")
        check(r.get('vis') and r.get('n') == 2 and r.get('hl'), f'折线悬停：竖线和各家的值（{r}）')
        # 用量：指标卡
        r = ev("""const k = id => [...document.querySelectorAll(`.uu-pk[data-p=${id}] .uu-k`)].map(x => [x.querySelector('.k').textContent, x.querySelector('.v').textContent]);
          return {claude: k('claude'), codex: k('codex'), agy: k('agy')};""")
        ck = dict(r.get('claude') or [])
        cx = dict(r.get('codex') or [])
        ag = dict(r.get('agy') or [])
        check(set(ck) == {'请求', 'Token', '估算费用', '缓存命中'} and ck.get('估算费用', '').startswith('$') and ck.get('缓存命中') == '80%',
              f'Claude：请求、Token、费用、缓存命中（{r.get("claude")}）')
        check(set(cx) == {'请求', 'Token', '占周额度', '缓存命中'} and cx.get('占周额度') == '约 12%', f'Codex：不显示钱，显示 token 和“占周额度约 x%”（{r.get("codex")}）')
        check(set(ag) == {'请求', 'Token', '缓存命中'}, f'Antigravity：没有费用就不显示（{r.get("agy")}）')
        r = ev(SCOPE_CMP_JS)
        r2 = ev("""return {share: [...document.querySelectorAll('.uu-ovl .uu-slg li')].filter(li => li.querySelector('.uu-scope')).map(li => li.querySelector('.l').textContent),
          lg: [...document.querySelectorAll('.uu-uch .uu-lg span')].filter(x => x.querySelector('.uu-scope')).map(x => x.dataset.s),
          rank: (document.querySelector('.uu-rkh .uu-scope') || {}).textContent || ''};""")
        check(r.get('cards') == ['claude'] and r.get('rows') == ['claude'] and r2.get('share') == ['Claude'] and r2.get('lg') == ['claude'] and r2.get('rank') == 'Claude 只算工作台',
              f'全部视图：Claude 的指标卡、速度表、占比条、图例、对话排行注明“只算工作台”，Codex、Antigravity 不注明（{r.get("cards")}，{r2}）')
        r = ev("""const d = CWB.uu.last(), tok = d.providers.reduce((a, p) => a + p.kpi.tokens, 0);
          return {big: document.querySelector('.uu-big').textContent, want: CWB.uu.formatTokens(tok), bigs: document.querySelector('.uu-bigs').textContent,
            seg: document.querySelectorAll('.uu-ovl .uu-sbarw i').length, paths: document.querySelectorAll('.uu-uch path[data-s]').length,
            title: document.querySelector('.uu-ovr .uu-ch h4').textContent, tot: [...document.querySelectorAll('.uu-tot dt')].map(x => x.textContent),
            types: [...document.querySelectorAll('.uu-ubody > .uu-share .uu-slg .l')].map(x => x.textContent),
            mrows: document.querySelectorAll('.uu-mt tbody tr').length, codexCost: (document.querySelector('.uu-mt tr[data-p=codex] .uu-na') || {}).textContent};""")
        check(r.get('big') == r.get('want') and r.get('bigs') == '12 个会话', f'总量大数字和会话数（{r.get("big")}，{r.get("bigs")}）')
        h0 = ev("return new Date().getHours() === 0;") is True  # 零点刚过：“今天”只有第 0 个小时一个点，按设计不画线
        check(r.get('seg') == 3 and r.get('paths') == (0 if h0 else 3) and r.get('title') == '每小时处理的 token', f'提供方占比条、按提供方分层的图（{r.get("seg")} 段，{r.get("paths")} 条）')
        check(r.get('tot') == ['处理的 token', '缓存的输入', '未缓存的输入', '输出', '缓存节省'] and r.get('types') == ['输入', '缓存读取', '缓存写入', '输出'],
              f'总计和按类型统计的 token（{r.get("tot")}）')
        check(r.get('mrows') == 5 and r.get('codexCost') == '—', f'明细按模型：5 个模型，Codex 不显示钱（{r.get("mrows")}）')
        ev("document.querySelector('[data-k=ubrk] [data-v=time]').click(); return 1;")
        r = ev("return {h: [...document.querySelectorAll('.uu-tt thead th')].map(x => x.textContent), n: document.querySelectorAll('.uu-tt tbody tr').length};")
        check(r.get('h', [])[:1] == ['小时'] and 'Codex' in r.get('h', []) and r.get('n', 0) >= 1, f'明细按时间（{r.get("h")}，{r.get("n")} 行）')
        ev("document.querySelector('[data-k=umetric] [data-v=cost]').click(); return 1;")
        r = ev("return {title: document.querySelector('.uu-ovr .uu-ch h4').textContent, big: document.querySelector('.uu-big').textContent, bigs: document.querySelector('.uu-bigs').textContent, paths: [...document.querySelectorAll('.uu-uch path[data-s]')].map(p => p.dataset.s)};")
        check(r.get('title') == '每小时费用' and r.get('big', '').startswith('$') and 'API 估算' in r.get('bigs', '') and r.get('paths') == ([] if h0 else ['claude']),
              f'图表切到费用：只画有费用的提供方（{r}）')
        ev("document.querySelector('[data-k=umetric] [data-v=tokens]').click(); document.querySelector('[data-k=ubrk] [data-v=model]').click(); return 1;")
        # 对话排行和子任务勾选
        r = ev("return {n: document.querySelectorAll('.uu-rank .sr').length, roll: !!document.querySelector('.uu-roll input'), link: document.querySelectorAll('.uu-rank .sj-link[data-sid]').length, tag: document.querySelectorAll('.uu-rank .sj-tag').length};")
        check(r.get('n') == 10 and r.get('roll') and r.get('link') == 5 and r.get('tag') == 0, f'对话排行前 10，有委派记录时显示勾选（{r}）')
        ev("window.__uuCalls = []; document.querySelector('.uu-roll input').click(); return 1;")
        wait("document.querySelectorAll('.uu-rank .sj-tag').length === 1", 5)
        r = ev("return {call: (window.__uuCalls.filter(c => c[0] === 'usage').pop() || [])[1], tag: (document.querySelector('.uu-rank .sj-tag') || {}).textContent, ls: localStorage.getItem('cwb.uu.rollup')};")
        check((r.get('call') or {}).get('rollup') is True and r.get('tag') == '含 3 个子任务' and r.get('ls') == 'true', f'勾上后带 rollup:true 重新取（{r}）')
        ev("window.__uuUse.hasTasks = false; document.querySelector('.uu-top [data-k=urange] [data-v=\"7d\"]').click(); return 1;")
        wait("document.querySelector('.uu-ovr .uu-ch h4') && document.querySelector('.uu-ovr .uu-ch h4').textContent === '每天处理的 token'", 5)
        r = ev("return {roll: !!document.querySelector('.uu-roll'), call: (window.__uuCalls.filter(c => c[0] === 'usage').pop() || [])[1], ls: localStorage.getItem('cwb.uu.range'), stitle: document.querySelector('[data-b=speed] .uu-ch h4').textContent};")
        check(not r.get('roll'), '没有委派记录时不显示勾选')
        check((r.get('call') or {}).get('range') == '7d' and r.get('ls') == '"7d"' and r.get('stitle') == '首字等待（按小时）', f'时段切到 7 天：重新取并记住（{r}）')
        ev("window.__uuUse.hasTasks = true; return 1;")
        # 扫描中：显示进度，每 2 秒刷新直到扫完
        ev("window.__uuUse.scan = 2; window.__uuCalls = []; document.querySelector('.uu-top [data-k=urange] [data-v=today]').click(); return 1;")
        ok = wait("(() => { const s = document.querySelector('.uu-scan'); return s && !s.hidden && s.textContent === '正在统计… 42%'; })()", 5)
        check(ok, '扫描中：用量标题旁显示“正在统计… 42%”')
        t0 = time.time()
        ok = wait("document.querySelector('.uu-scan').hidden", 9)
        n = ev("return window.__uuCalls.filter(c => c[0] === 'usage').length;")
        check(ok and n == 3 and time.time() - t0 > 2.5, f'每 2 秒刷新一次，扫完就停（共 {n} 次，{time.time() - t0:.1f}s）')
        time.sleep(2.4)
        check(ev("return window.__uuCalls.filter(c => c[0] === 'usage').length;") == 3, '扫完后不再轮询')
        # 关掉设置后不再轮询
        ev("window.__uuUse.scan = 99; window.__uuCalls = []; document.querySelector('.uu-top [data-k=urange] [data-v=\"30d\"]').click(); return 1;")
        wait("!document.querySelector('.uu-scan').hidden", 5)
        ev("CWB.closeSettings(); window.__uuN0 = window.__uuCalls.length; return 1;")
        time.sleep(4.5)
        check(ev("return window.__uuCalls.length - window.__uuN0;") == 0, '关掉设置后停止轮询')
        ev("window.__uuUse.scan = 0; return 1;")
        # 选一家（11-23 起只看某一家时是第十四轮老样式页）：Codex
        ev("CWB.openSettings('usage'); return 1;")
        wait("document.querySelector('.uu-pk')", 5)
        ev("window.__uuCalls = []; document.querySelector('.uu-top [data-k=uprov] [data-v=codex]').click(); return 1;")
        wait("document.querySelector('.uu-top-old') && document.querySelector('.uu-qbody-old .uu-qold[data-p=codex]') && document.querySelector('.sj-ubody .sj-kpis')", 5)
        r = ev(OLD_PAGE_JS)
        calls = r.get('calls') or []
        check(r.get('newSecs') == 0 and r.get('h2') == '额度与速度' and r.get('lead', '').startswith('统计本机所有 Codex 对话') and r.get('prov') == 'Codex' and r.get('cards') == ['codex'],
              f'只看 Codex：整页是老样式，没有速度块和新样式用量块，说明文字写明含终端、不算钱（{r.get("lead")}）')
        q0 = (r.get('qrows') or [[None] * 5])[0]
        check(r.get('qstale') and r.get('qupd') == '22 分钟前的数据' and r.get('plan') == 'Pro' and r.get('qerr') == '这次没读到 Codex 额度' and len(r.get('qrows') or []) == 1
              and q0[1] == '剩余 28%' and q0[2] == '28%' and '后重置' in q0[3] and '余裕 8' in q0[3],
              f'Codex 老额度卡：读不到时列出上次的窗口并标过时，条宽＝剩余，重置时间和余裕照老卡（{r.get("qupd")}，{q0}）')
        check('账号额度，和实际用量可能不符' in r.get('foot', '') and '积分余额 120' in r.get('foot', '') and '已存有 2 个重置额度' in r.get('foot', ''),
              f'Codex 老额度卡底部：代理提示、积分、重置额度（{r.get("foot")}）')
        kp = {x[0]: x for x in r.get('kpis') or []}
        check([x[0] for x in r.get('kpis') or []] == ['请求', 'Token', '占周额度', '缓存命中', '输出速度'] and kp['占周额度'][1] == '约 12%' and kp['占周额度'][2] == '订阅计费，不算钱'
              and kp['缓存命中'][2].startswith('缓存读 ') and kp['输出速度'][2] == '中位数 · 估算',
              f'Codex 指标卡：“估算费用”换成“占周额度”，缓存命中不折算钱，速度标估算（{r.get("kpis")}）')
        check(r.get('metric') == ['Token', '请求'] and r.get('bars') == 30 and r.get('top4') == ['GPT-5.2 Codex', 'GPT-5.2'] and r.get('rec') == 5,
              f'Codex 图表没有“费用”，30 天 30 根柱子，模型 Top 4、最近请求照老页面（{r.get("metric")}，{r.get("bars")}）')
        check(r.get('hnote', '').startswith('只统计 ') and r.get('hnote', '').endswith(' 以后') and r.get('na', 0) > 300,
              f'活跃度注明只统计近 30 天，之前的格子标成不在范围（{r.get("hnote")}，{r.get("na")} 格）')
        check(r.get('links') == 1 and r.get('plain') == 2 and r.get('scost') == ['—', '—', '—'], f'用量最多的对话：工作台里的能点开，终端的只显示名字，不显示钱（{r.get("scost")}）')
        check(['quota', {'provider': 'codex', 'refresh': True}] in calls and ['usage', {'range': '30d', 'legacy': 'codex'}] in calls, f'切换后按这一家取额度和老样式用量（{calls}）')
        ev("""const b = document.querySelector('.sj-bars .sj-bar'); b.dispatchEvent(new MouseEvent('mouseover', {bubbles: true})); return 1;""")
        tipr = ev("const t = document.querySelector('#sjSet .sj-tip'); return {vis: !t.hidden, txt: t.textContent};")
        check(tipr.get('vis') and '请求 ' in tipr.get('txt', '') and '$' not in tipr.get('txt', ''), f'柱子悬停：Codex 不显示钱（{tipr.get("txt")}）')
        # Antigravity
        ev("window.__uuCalls = []; document.querySelector('.uu-top-old [data-k=uprov] [data-v=agy]').click(); return 1;")
        wait("document.querySelector('.uu-qbody-old .uu-qold[data-p=agy]') && document.querySelector('.sj-ubody .sj-kpis')", 5)
        r = ev(OLD_PAGE_JS)
        rows = r.get('qrows') or []
        kp = {x[0]: x for x in r.get('kpis') or []}
        check(r.get('cards') == ['agy'] and r.get('lead', '').startswith('统计本机所有 Antigravity 对话') and r.get('wide') and len(rows) == 5 and not r.get('qstale')
              and rows[4][1] == '剩余 0%' and 'red' in rows[4][4] and rows[3][1] == '剩余 88%' and r.get('foot') == '',
              f'Antigravity 老额度卡：5 个窗口，长名字放宽第一列，用完的标红（{[x[:2] for x in rows]}）')
        check([x[0] for x in r.get('kpis') or []] == ['请求', 'Token', '费用', '缓存命中', '输出速度'] and kp['费用'][1] == '$0.03' and kp['费用'][2] == '今天 $0.03' and kp['输出速度'][2] == '中位数'
              and r.get('metric') == ['Token', '费用', '请求'], f'Antigravity：有提供方报的费用就显示金额，图表有“费用”（{r.get("kpis")}）')
        # 复核 11-23：只有部分请求报了费用时不摊“每次”；没报费用的小时悬停写“费用 —”，不写 $0.00
        ev("document.querySelector('.sj-ubar [data-k=range] [data-v=today]').click(); return 1;")
        wait("document.querySelectorAll('.sj-kpi').length === 5 && /报了费用/.test(document.querySelectorAll('.sj-kpi')[2].textContent)", 5)
        r = ev("""const k = document.querySelectorAll('.sj-kpi')[2], b = document.querySelectorAll('.sj-bars .sj-bar')[1];
          b.dispatchEvent(new MouseEvent('mouseover', {bubbles: true})); return {d: k.querySelector('.d').textContent, tip: document.querySelector('#sjSet .sj-tip').textContent};""")
        check(r.get('d') == '2 次报了费用' and '费用 —' in r.get('tip', '') and '$0.00' not in r.get('tip', ''), f'Antigravity 今天：写“2 次报了费用”，没报费用的小时悬停写“费用 —”（{r}）')
        ev("document.querySelector('.sj-ubar [data-k=range] [data-v=\"30d\"]').click(); return 1;")
        ev("CWB.closeSettings(); CWB.openSettings('usage'); return 1;")
        wait("document.querySelector('.uu-top-old')", 5)
        check(ev("return (document.querySelector('.uu-top [data-k=uprov] .on') || {}).textContent;") == 'Antigravity', '重新打开：记住上次的提供方')
        # 11-23 用户要求：只看 Claude 时整页是第十四轮老“用量”页（ux-j.js rUsage），没有速度块和新样式的用量块
        ev("window.__uuCalls = []; document.querySelector('.uu-top [data-k=uprov] [data-v=claude]').click(); return 1;")
        wait("document.querySelector('.sj-ubar') && document.querySelector('.uu-qbody-old .uu-qold') && document.querySelector('.sj-ubody .sj-none, .sj-ubody .sj-kpis, .sj-ubody .sj-card')", 5)
        r = ev("""const qc = document.querySelector('.uu-qbody-old .uu-qold');
          return {h2: document.querySelector('.sj-body .sj-hd h2').textContent, lead: (document.querySelector('.sj-body .sj-lead') || {}).textContent || '',
            newSecs: document.querySelectorAll('.uu-sec, .uu-page, .uu-spd, .uu-pk').length, oldCards: document.querySelectorAll('.sj-body .sj-qcard').length,
            qp: qc && qc.dataset.p, q5h: qc ? qc.querySelectorAll('.sj-q[data-k="5h"]').length : -1, qrem: qc ? [...qc.querySelectorAll('.sj-qu')].every(x => x.textContent.startsWith('剩余 ')) : false,
            prov: (document.querySelector('.uu-top-old [data-k=uprov] .on') || {}).textContent, rng: (document.querySelector('.sj-ubar [data-k=range] .on') || {dataset: {}}).dataset.v,
            st: CWB.uu.state.range, ucall: (window.__uuCalls.filter(c => c[0] === 'usage').pop() || [])[1]};""")
        check(r.get('h2') == '额度与速度' and r.get('lead', '').startswith('只统计在工作台里运行的对话') and r.get('newSecs') == 0,
              f'只看 Claude：整页是第十四轮老用量页，没有速度块和新样式用量块（{r.get("h2")}，{r.get("newSecs")}）')
        check(r.get('oldCards') == 1 and r.get('qp') == 'claude' and r.get('q5h') == 0 and r.get('qrem'),
              f'只看 Claude：只有一张 11-23 版老额度卡（没有 5 小时、显示剩余）（{r.get("oldCards")}，{r.get("q5h")}）')
        check(r.get('prov') == 'Claude' and r.get('rng') == r.get('st') and not ({'provider', 'legacy'} & set(r.get('ucall') or {'provider': 1})),
              f'只看 Claude：提供方切换还在，时段和其他几栏一致，用量按老页面取（不带 provider）（{r}）')
        ev("document.querySelector('.sj-ubar [data-k=range] [data-v=\"7d\"]').click(); return 1;")
        r = ev("return {st: CWB.uu.state.range, ls: localStorage.getItem('cwb.uu.range')};")
        check(r.get('st') == '7d' and r.get('ls') == '"7d"', f'老页面里切时段，其他几栏跟着记住（{r}）')
        ev("document.querySelector('.sj-ubar [data-k=range] [data-v=\"30d\"]').click(); return 1;")
        ev("document.querySelector('.uu-top [data-k=uprov] [data-v=all]').click(); return 1;")
        wait("document.querySelectorAll('.uu-qc, .uu-qold').length === 3", 5)
        shot('b-stub-top.png')
        # quota 事件：更新卡片（侧栏小条 11-23 已去掉）
        ev("""const f = window.__uuMk(); f.agy.windows[0].usedPercent = 10;
          window.__uuOnEv({type: 'quota', providers: [f.claude, f.codex, f.agy], tightest: f.tightest});
          return 1;""")
        r = ev("return {rem: document.querySelector('.uu-qc[data-p=agy] .uu-w[data-w=\"5h\"] .uu-left').textContent, strip: !!document.getElementById('uuStrip')};")
        check(r.get('rem') == '剩余 90%' and not r.get('strip'), f'quota 事件：卡片跟着变，侧栏不出现额度小条（{r}）')
        ev("CWB.closeSettings(); const f = window.__uuMk(); window.__uuOnEv({type: 'quota', providers: [f.claude, f.codex, f.agy], tightest: f.tightest}); CWB.openSettings('usage'); return 1;")
        wait("document.querySelector('.uu-page')", 5)
        check(ev("return !document.getElementById('uuStrip');") is True, '关掉设置、收到 quota 事件后侧栏也没有额度小条')
        # 最近 20 轮：点工作台的对话，先关设置再打开
        wait("document.querySelector('.uu-rec .sj-link[data-sid]')", 5)
        ev("window.__uuOpen = CWB.openSession; window.__uuOpened = null; CWB.openSession = sid => { window.__uuOpened = sid; }; document.querySelector('.uu-rec .sj-link[data-sid]').click(); CWB.openSession = window.__uuOpen; return 1;")
        r = ev("return {sid: window.__uuOpened, open: CWB.settingsOpen()};")
        check(r.get('sid') == 'stub-sid-0' and not r.get('open'), f'点最近一轮的对话：关设置、打开对话（{r}）')
        # ---------------- C. 手机宽度
        _ab('set', 'viewport', '390', '800')
        time.sleep(0.4)
        for remote in (False, True):
            if remote:
                ev("document.documentElement.classList.add('cwb-remote'); document.body.classList.add('rr-m'); return 1;")
            ev("CWB.closeSettings(); CWB.openSettings('usage'); return 1;")
            wait("document.querySelector('.uu-pk') && document.querySelector('.uu-rank')", 6)
            time.sleep(0.3)
            r = ev("""const b = document.querySelector('.sj-body'), br = b.getBoundingClientRect(), bad = [];
              for (const e of document.querySelectorAll('.uu-page *')) { if (!e.offsetParent && e.tagName !== 'svg') continue; const r = e.getBoundingClientRect();
                if (r.width && r.right > br.right + 1) bad.push((e.className && e.className.baseVal === undefined ? e.className : e.tagName) + ':' + Math.round(r.right - br.right)); }
              return {sw: b.scrollWidth, cw: b.clientWidth, doc: document.documentElement.scrollWidth, vw: innerWidth, bad: bad.slice(0, 6)};""")
            # 整页宽度只在远程页面里查：本机窗口拉到 390px 时首页的提供方切换（#pvSeg，第十六轮）本来就会超出，和本页无关
            check(r.get('sw', 99) <= r.get('cw', 0) + 1 and (not remote or r.get('doc', 99) <= r.get('vw', 0)) and not r.get('bad'),
                  f'手机宽度{"（远程页面样式）" if remote else ""}不出横向滚动（{r}）')
            shot('c-mobile%s.png' % ('-remote' if remote else ''))
            if remote:  # 11-23：只看某一家的老样式页（手机远程页面）也不横向滚动；本机 390px 窗口下老页面“最近请求”表本来就会超出（第十四轮原样）
                for pv in ('codex', 'agy', 'claude'):
                    ev("document.querySelector('.uu-top [data-k=uprov] [data-v=%s]').click(); return 1;" % pv)
                    wait("document.querySelector('.uu-top-old .on') && document.querySelector('.uu-top-old .on').dataset.v === '%s' && document.querySelector('.sj-ubody .sj-kpis, .sj-ubody .sj-none')" % pv, 6)
                    time.sleep(0.3)
                    r = ev("const b = document.querySelector('.sj-body'); return {sw: b.scrollWidth, cw: b.clientWidth, doc: document.documentElement.scrollWidth, vw: innerWidth};")
                    check(r.get('sw', 99) <= r.get('cw', 0) + 1 and r.get('doc', 99) <= r.get('vw', 0), f'手机宽度（远程页面样式）：只看 {pv} 的老样式页不出横向滚动（{r}）')
                ev("document.querySelector('.uu-top [data-k=uprov] [data-v=all]').click(); return 1;")
                wait("document.querySelector('.uu-page')", 6)
        ev("document.documentElement.classList.remove('cwb-remote'); document.body.classList.remove('rr-m'); return 1;")
        _ab('set', 'viewport', '1280', '860')
        # ---------------- D. 深浅色
        for th in ('light', 'dark'):
            ev("CWB.sj.setPref('theme', %s); CWB.closeSettings(); CWB.openSettings('usage'); return 1;" % json.dumps(th))
            wait("document.querySelector('.uu-big')", 5)
            time.sleep(0.3)
            r = ev(LUM_JS)
            check(min(r.get('left', 0), r.get('big', 0), r.get('td', 0)) >= 4.5 and r.get('why', 0) >= 3 and r.get('tick', 0) >= 3,
                  f'{"浅色" if th == "light" else "深色"}：文字和刻度对比度够（{ {k: round(v, 1) if isinstance(v, float) else v for k, v in r.items()} }）')
            shot('d-%s.png' % th)
        ev("CWB.sj.setPref('theme', %s); return 1;" % json.dumps(json.loads(theme0) if theme0 else 'system'))
        rc, out = _ab('errors')
        bad = [ln for ln in out.splitlines() if ln.strip() and 'No page errors' not in ln and 'no errors' not in ln.lower()]
        check(not bad, f'页面没有脚本错误：{bad[:3]}')
    except Exception as e:  # 浏览器出状况时也要交回结果
        check(False, f'界面测试中断：{e!r}')
    finally:
        try:
            _ev("CWB.closeSettings && CWB.closeSettings(); for (const k of Object.keys(localStorage)) if (k.startsWith('cwb.uu.')) localStorage.removeItem(k); return 1;")
        except Exception:
            pass
        _ab('close')
    return res['ok'], res['fail']


# ---------------------------------------------------------------- 单独运行：在本目录起测试服务（用副本里的假数据）
def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    st = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith('Z')


def _cmd(pid):
    return subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()


def _cwd(pid):
    out = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], capture_output=True, text=True).stdout
    return next((ln[1:] for ln in out.splitlines() if ln.startswith('n')), '')


def _stop(pid, needle, cwd=None):
    """按 PID＋命令行（必要时加工作目录）核对后才结束。"""
    if pid and _alive(pid) and needle in _cmd(pid) and (cwd is None or os.path.realpath(_cwd(pid)) == os.path.realpath(cwd)):
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not _alive(pid):
                return True
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
    return False


def _api(base, tok, name, body=None, timeout=30):
    req = urllib.request.Request(f'{base}/api/{name}', data=json.dumps(body or {}).encode(),
                                 headers={'Content-Type': 'application/json', 'X-Token': tok}, method='POST')
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
        return json.loads(r.read())


def api_checks(base, tok, facts):
    """第十七轮 U（主线程）：真后端（quota_hub.py、usage_p.py、usage.py）＋造出来的三家记录，接口层断言。返回 (通过数, 失败清单)。"""
    ok, fails = [0], []

    def check(cond, msg):
        print(('通过 ' if cond else '失败 ') + msg, flush=True)
        if cond:
            ok[0] += 1
        else:
            fails.append(msg)
    q = _api(base, tok, 'quota', {'refresh': True})
    check({'ok', 'err', 'ts', 'age', 'stale', 'windows'} <= set(q) and [w['key'] for w in q['windows']] == ['5h', '7d'],
          'quota：第十四轮的字段原样（Claude 两个窗口）')
    check([x['id'] for x in q['providers']] == ['claude', 'codex', 'agy'], 'quota：三家都在')
    for _ in range(80):
        q = _api(base, tok, 'quota', {})
        if all(x['ok'] for x in q['providers']) and not any(x.get('checking') for x in q['providers']):
            break
        time.sleep(0.25)
    st = {x['id']: x['ok'] for x in q['providers']}
    check(all(st.values()), f'打开页面后台读完：三家都读到额度（假 Codex app-server、假 agy；{st}）')
    cx = next(x for x in q['providers'] if x['id'] == 'codex')
    check([w['label'] for w in cx['windows']] == ['5 小时', '每周'] and cx.get('plan') == 'pro', f'Codex 临时进程读到的窗口按 T3 规则转换：{[w["label"] for w in cx["windows"]]}')
    t = q.get('tightest') or {}
    check(t.get('provider') == 'agy' and t.get('id') == 'gemini-weekly', f'tightest：最紧的是 Antigravity Gemini 每周（{t.get("providerLabel")} {t.get("label")} {t.get("usedPercent")}）')
    for _ in range(150):
        u = _api(base, tok, 'usage', {'range': 'today'})
        if not u.get('scanning') and not any(x.get('scanning') for x in u.get('providers') or []):
            break
        time.sleep(0.2)
    check({'kpi', 'buckets', 'models', 'heat', 'recent', 'sessions', 'today', 'range', 'unit'} <= set(u) and len(u['buckets']) == 24,
          'usage：第十四轮的字段都在（今天 24 个小时桶）')
    check({'providers', 'usageBuckets', 'speed', 'ranking', 'hasTasks'} <= set(u), 'usage：新字段 providers、usageBuckets、speed、ranking、hasTasks')
    got = {x['id']: x['kpi']['requests'] for x in u['providers']}
    want = {k: facts[k]['today_requests'] for k in ('claude', 'codex', 'agy')}
    check(got == want, f'usage 今天：三家请求数 {got}（造了 {want}；Claude 只算工作台，Codex、agy 含终端）')
    check(u['kpi']['requests'] == facts['claude']['today_requests'], 'usage：原有 kpi 仍是 Claude、只算工作台（第十四轮口径）')
    check({x['id']: x.get('scope') for x in u['providers']} == {'claude': 'workbench', 'codex': 'all', 'agy': 'all'},
          'usage：Claude 一栏标“只算工作台”（scope=workbench），Codex、agy 含终端和其他客户端')
    u2 = _api(base, tok, 'usage', {'range': 'today', 'provider': 'codex'})
    check([x['id'] for x in u2['providers']] == ['codex'] and u2['kpi'] == u['kpi'], 'usage：provider 只过滤新字段，原有字段不变')
    check(u.get('hasTasks') is True, 'usage：有委派记录 → hasTasks')
    r1 = _api(base, tok, 'usage', {'range': 'today', 'rollup': True})
    par = next((x for x in r1['ranking'] if x.get('sid') == facts['tasks']['parent']), None)
    check(par and par.get('children') == 1 and not any(x.get('sid') == facts['tasks']['child'] for x in r1['ranking']),
          '对话排行：勾“子任务算到父对话头上”后子对话并进父对话')
    sm = _api(base, tok, 'usage-summary', {})
    check(set(sm) == {'scanning', 'today'}, 'usage-summary 字段不变')
    return ok[0], fails


def _leftovers(tmp):
    out = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout.splitlines()
    me = {os.getpid(), os.getppid()}
    res = []
    for l in out:
        f = l.strip().split(None, 1)
        if len(f) < 2 or int(f[0]) in me or f[1].startswith(('/bin/zsh -c', '/bin/bash -c', 'zsh -c', 'bash -c')) or 'e2e_r17_U' in f[1]:
            continue
        if tmp in f[1] or 'tests/fakecodex.py app-server' in f[1] or 'tests/fakeagy -p' in f[1]:
            res.append(l)
    return res


def main():
    args = sys.argv[1:]
    if '--url' in args:
        base, tok = args[args.index('--url') + 1], args[args.index('--token') + 1]
        ok, fails = ui_checks(base, tok, shots=os.environ.get('CWB_UI_SHOTS'))
        print(f'\n界面断言：通过 {ok}，失败 {len(fails)}' + ('' if not fails else '：' + '；'.join(fails)))
        sys.exit(1 if fails else 0)
    # 默认：真后端＋造出来的三家记录（tests/r17u_usage_cases.build_fixtures）＋crs 额度文件；
    # Codex 用假 app-server（tests/fakecodex.py app-server），agy 用假命令（tests/fakeagy）；不启动 claude、真 codex、真 agy
    sys.path.insert(0, HERE)
    import r17u_usage_cases as UC
    import tempfile
    x = tempfile.mkdtemp(prefix='cwb-r17u-e2e-', dir='/tmp')  # 每次新的 HOME：用量缓存按文件路径记，固定目录会带进上次删掉的文件
    home, data = x + '/home', x + '/data'
    os.makedirs(home + '/.claude', exist_ok=True)
    facts = UC.build_fixtures(home, data)
    now = time.time()
    with open(home + '/.claude/.crs-quota.json', 'w') as f:
        json.dump({'ts': int(now), 'w': [{'lbl': '5h', 'u': 35, 'r': int(now + 7200), 't': 18000},
                                         {'lbl': '7d', 'u': 62, 'r': int(now + 3 * 86400), 't': 604800}]}, f)
    cxhome = x + '/codexhome'
    os.makedirs(cxhome)
    open(cxhome + '/auth.json', 'w').write('{}')
    run = os.path.join(ROOT, '.run')
    os.makedirs(run, exist_ok=True)
    py = os.path.join(ROOT, '.venv/bin/python')
    env = dict(os.environ, HOME=home, CWB_PORT=str(PORT), CWB_TITLE_DELAY='99999', CWB_DATA_DIR=data, CWB_QUOTA_NO_LOOP='1',
               CWB_AGY_BIN=os.path.join(HERE, 'fakeagy'), CWB_CODEX_HOME=cxhome,
               CWB_CODEX_CMD=json.dumps([py, '-I', os.path.join(HERE, 'fakecodex.py'), 'app-server']))
    env.pop('CODEX_HOME', None)
    env.pop('ANTIGRAVITY_DATA_DIR', None)
    lf = open(x + '/server.log', 'ab')
    p = subprocess.Popen([py, '-I', os.path.join(ROOT, 'server.py')], cwd=ROOT, env=env,
                         stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, start_new_session=True)
    ok1 = ok2 = 0
    fails = []
    try:
        up = False
        for _ in range(300):
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f'http://127.0.0.1:{PORT}/ping', timeout=1) as r:
                    if r.read().decode().startswith('ok'):
                        up = True
                        break
            except Exception:
                time.sleep(0.1)
        if not up:
            fails.append(f'测试服务没起来（见 {x}/server.log）')
        else:
            tok = open(os.path.join(run, f'token-{PORT}')).read().strip()
            base = f'http://127.0.0.1:{PORT}'
            ok1, f1 = api_checks(base, tok, facts)
            ok2, f2 = ui_checks(base, tok, shots=os.environ.get('CWB_UI_SHOTS'))
            fails = f1 + f2
    finally:
        _stop(p.pid, 'server.py', ROOT)
        try:  # 测试服务拉起的 ptyd：锁文件里的 PID，命令行带本目录和端口才结束
            pp = int(open(os.path.join(run, f'ptyd-{PORT}.lock')).read().split()[0])
            c = _cmd(pp)
            if os.path.join(ROOT, 'ptyd.py') in c and f'--port {PORT}' in c:
                _stop(pp, 'ptyd.py')
        except Exception:
            pass
        time.sleep(0.5)
        left = _leftovers(x)
        if left:
            fails.append(f'测试结束还有残留进程：{left[:3]}')
        else:
            ok2 += 1
            print('通过 测试结束没有残留进程（测试服务、ptyd、假 Codex、假 agy）', flush=True)
    if not fails:
        subprocess.run(['rm', '-rf', x])
    print(f'\n接口断言通过 {ok1}，界面断言通过 {ok2}，失败 {len(fails)}' + ('' if not fails else '：' + '；'.join(fails)))
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
