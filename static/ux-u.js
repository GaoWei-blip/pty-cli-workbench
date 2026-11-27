(() => {
'use strict';
/* 第十七轮 U：设置“额度与速度”页（额度、速度、用量）和侧栏底部额度小条。
   移植自 T3 Code（MIT，Copyright (c) 2026 T3 Tools Inc.），React/TS 译成原生 JS，图表 SVG 手画：
     apps/web/src/components/usage/UsagePage.tsx（页面布局、UsageOverview、Metric、明细表）
     apps/web/src/components/usage/UsageLimits.tsx（LimitWindows、WindowBar、paceMarkStyles、PaceIcon）
     apps/web/src/components/usage/UsageLimitsPooled.tsx（套餐、重置券的显示）
     apps/web/src/components/usage/UsageProviderChart.tsx（单调曲线、niceScale、分层面积、悬停提示）
     apps/web/src/components/usage/UsageShareBar.tsx、usageBreakdown.ts、usageProviders.ts、UsageModelDialog.tsx
     apps/web/src/components/chat/ComposerUsageLimits.tsx、ThreadDetailsUsage.tsx（侧栏小条的细条、刻度、悬停文案）
     packages/shared/src/usageLimits.ts（remainingPercent、elapsedShare、paceRemainingPercent、paceOf、resetCountdown）
     packages/shared/src/usageLimitsCopy.ts（formatDuration、formatResetsIn）、usageFormat.ts（formatTokens、formatPercent）
   文案取自 T3 的 apps/web/src/i18n/translations/zh.json。
   接口：quota {provider, refresh}、usage {range, provider, rollup}、事件 {type:'quota', providers, tightest}（见 .u-work/contract.md）。
   设置面板沿用第十四轮 J 的框架（C.sj）：分区键仍是 usage，只换标题和页面；第十四轮的 rUsage 留在 ux-j.js 不动。 */
const C = window.CWB;
if (!C || !C.sj) return;
const sj = C.sj, esc = C.esc;
const api = (n, b) => C.api(n, b);  // 每次现取：测试时可替换 CWB.api

/* ================= 偏好（localStorage，读写都包 try/catch） ================= */
const sget = (k, d) => { try { const v = localStorage.getItem('cwb.uu.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } };
const sset = (k, v) => { try { localStorage.setItem('cwb.uu.' + k, JSON.stringify(v)); } catch {} };
const oneOf = (v, list, d) => (list.includes(v) ? v : d);
const PIDS = ['claude', 'codex', 'agy'];
const PLABEL = {claude: 'Claude', codex: 'Codex', agy: 'Antigravity'};
/* 提供方配色：Claude 用 T3 的 #d97757（即 --run）；Codex、Antigravity 沿用第十六轮左栏标识的颜色（--pv-codex、--pv-agy） */
const PCOLOR = {claude: 'var(--run)', codex: 'var(--pv-codex,#3f8f7a)', agy: 'var(--pv-agy,#5b7fd6)'};
const st = {
  prov: oneOf(sget('prov', 'all'), ['all', ...PIDS], 'all'),
  range: oneOf(sget('range', '30d'), ['today', '7d', '30d'], '30d'),
  metric: oneOf(sget('metric', 'tokens'), ['tokens', 'cost'], 'tokens'),
  smetric: oneOf(sget('smetric', 'tps'), ['tps', 'ttft', 'turn'], 'tps'),
  brk: oneOf(sget('brk', 'model'), ['model', 'time'], 'model'),
  rollup: !!sget('rollup', false),
  cscope: oneOf(sget('cscope', 'all'), ['all', 'wb'], 'all'),  // 11-26：Claude 统计范围：all 含终端、VS Code；wb 只算工作台
};
const CSCOPE_OPTS = [['all', '全部客户端'], ['wb', '只看工作台']];
const cscopeSeg = () => sj.seg('ucscope', CSCOPE_OPTS, st.cscope, 'Claude 统计范围');
const want = id => st.prov === 'all' || st.prov === id;
/* 11-22 晚口径：Claude 用量只算工作台（providers[].scope = "workbench"），Codex、Antigravity 含终端和其他客户端（"all"）。
   界面在 Claude 一栏注明“只算工作台” */
const WB = '只算工作台';
let SCOPE = {};
const isWb = id => SCOPE[id] === 'workbench';
const scopeTag = id => (isWb(id) ? `<span class="uu-scope">${WB}</span>` : '');
const plabel = (id, fb) => PLABEL[id] || fb || id || '';
const pcolor = id => PCOLOR[id] || 'var(--muted)';

/* ================= 格式 ================= */
const MINUTE = 60000, HOUR = 60 * MINUTE, DAY = 24 * HOUR;
const p2 = n => String(n).padStart(2, '0');
const toMs = t => { t = +t || 0; return t && t < 1e12 ? t * 1000 : t; };
const hm = t => { const d = new Date(toMs(t)); return p2(d.getHours()) + ':' + p2(d.getMinutes()); };
const dkey = d => d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate());
const fmtInt = n => Math.round(+n || 0).toLocaleString('en-US');
/* 金额沿用第十四轮（$1,234.56；不到一分写 <$0.01） */
const fmtUsd = n => { n = +n || 0; return n > 0 && n < 0.005 ? '<$0.01' : '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); };
const usdK = n => ((+n || 0) >= 1000 ? '$' + Math.round(+n).toLocaleString('en-US') : fmtUsd(n));
const fmtMs = ms => { ms = +ms || 0; if (ms <= 0) return '0s'; if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's'; return Math.floor(ms / 60000) + 'm' + Math.round(ms % 60000 / 1000) + 's'; };
const fmtTps = v => (v < 10 ? (+v).toFixed(1).replace(/\.0$/, '') : String(Math.round(v))) + ' tok/s';
/* 移植自 T3 usageFormat.ts：formatTokens（取整后再选单位，999,999 读作 1M）、formatPercent */
function trimNum(v) { const a = Math.abs(v), dg = a >= 100 ? 0 : a >= 10 ? 1 : 2; return v.toFixed(dg).replace(/\.0+$/, ''); }
function formatTokens(v) {
  v = +v || 0;
  if (Math.abs(Math.round(v)) < 1e3) return String(Math.round(v));
  const SUF = ['K', 'M', 'B', 'T'];
  let x = v / 1e3, u = 0;
  while (Math.abs(Number(trimNum(x))) >= 1e3 && u < SUF.length - 1) { x /= 1e3; u++; }
  return trimNum(x) + SUF[u];
}
function formatPercent(share, digits = 1) {
  const pc = (+share || 0) * 100, small = 10 ** -digits;
  if (pc > 0 && pc < small) return `<${small.toFixed(digits)}%`;
  return pc.toFixed(digits) + '%';
}
const fmtPct0 = v => (v == null || !isFinite(v) ? '—' : (v * 100).toFixed(1).replace(/\.0$/, '') + '%');
/* 移植自 T3 timestampFormat.ts formatUpcomingTimestamp：今天只写时间，明天写“明天 hh:mm”，再往后写日期 */
function formatUpcoming(ms, now) {
  const d = new Date(ms), t = hm(ms), a = new Date(now); a.setHours(0, 0, 0, 0);
  const b = new Date(ms); b.setHours(0, 0, 0, 0);
  const diff = Math.round((b - a) / DAY);
  if (diff <= 0) return t;
  if (diff === 1) return `明天 ${t}`;
  return (d.getFullYear() === a.getFullYear() ? '' : d.getFullYear() + '/') + `${d.getMonth() + 1}/${d.getDate()} ${t}`;
}
/* T3 的相对时间：刚刚、x 分钟前 */
function ago(ts, now) {
  const s = Math.max(0, now - toMs(ts));
  if (s < MINUTE) return '刚刚';
  if (s < HOUR) return `${Math.floor(s / MINUTE)} 分钟前`;
  if (s < DAY) return `${Math.floor(s / HOUR)} 小时前`;
  return `${Math.floor(s / DAY)} 天前`;
}

/* ================= 移植自 T3 packages/shared/src/usageLimits.ts ================= */
function remainingPercent(w) { return Math.round(100 - Math.max(0, Math.min(100, +w.usedPercent || 0))); }
function resetMillis(w) { const r = w && w.resetsAt; return typeof r === 'number' && isFinite(r) && r > 0 ? toMs(r) : null; }
function elapsedShare(w, now) {
  const r = resetMillis(w);
  if (r === null || !(+w.windowMins > 0)) {
    // 工作台：契约里后端也会给 elapsed（按读取时刻算的），没有窗口长度时退回用它
    return typeof w.elapsed === 'number' && isFinite(w.elapsed) ? Math.max(0, Math.min(1, w.elapsed)) : null;
  }
  const len = w.windowMins * MINUTE;
  return Math.max(0, Math.min(1, (len - (r - now)) / len));
}
// 额度条显示的是剩余，所以匀速刻度跟着“窗口剩余时间”走
function paceRemainingPercent(w, now) { const e = elapsedShare(w, now); return e === null ? null : Math.round((1 - e) * 100); }
const PACE_OF_CONTRACT = {fast: 'ahead', ok: 'on', slow: 'under'};
function paceOf(w, now) {
  const e = elapsedShare(w, now);
  if (e === null) return PACE_OF_CONTRACT[w.pace] || null;
  const gap = (+w.usedPercent || 0) - e * 100;  // paceOfShares
  return gap > 5 ? 'ahead' : gap < -5 ? 'under' : 'on';
}
function resetCountdown(w, now) {
  const r = resetMillis(w);
  if (r === null) return null;
  if (r <= now) return {kind: 'now'};
  if (r - now < MINUTE) return {kind: 'momentarily'};
  return {kind: 'in', ms: r - now};
}
/* ================= 移植自 T3 packages/shared/src/usageLimitsCopy.ts ================= */
function formatDuration(ms) {
  ms = Math.max(0, ms);
  const d = Math.floor(ms / DAY), h = Math.floor(ms % DAY / HOUR), m = Math.floor(ms % HOUR / MINUTE);
  if (d > 0) return `${d} 天 ${h} 小时`;
  if (h > 0) return `${h} 小时 ${m} 分钟`;
  return `${m} 分钟`;
}
function formatResetsIn(w, now) {
  const c = resetCountdown(w, now);
  if (c === null) return null;
  if (c.kind === 'now') return '已到重置时间';
  if (c.kind === 'momentarily') return '即将重置';
  return `${formatDuration(c.ms)} 后重置`;
}

/* ================= 图标（线条，手画） ================= */
const svg = (d, cls) => `<svg class="${cls || 'uu-i'}" viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
/* T3 PaceIcon：TrendingUp／Gauge／TrendingDown；悬停文案取 T3 中文 */
const PACE = {
  ahead: ['消耗偏快：用量消耗进度超过本周期的时间进度', svg('<path d="M22 7 13.5 15.5 8.5 10.5 2 17"/><path d="M16 7h6v6"/>')],
  on: ['与时间进度一致', svg('<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>')],
  under: ['消耗偏慢：本周期剩余时间内还有充足余量', svg('<path d="M22 17 13.5 8.5 8.5 13.5 2 7"/><path d="M16 17h6v-6"/>')],
};
const PACE_SHORT = {ahead: '用量消耗偏快', on: '用量消耗适中', under: '用量消耗偏慢'};
const IC_TICKET = svg('<path d="M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/><path d="M13 5v2M13 17v2M13 11v2"/>');
const IC_RF = svg('<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>');

/* ================= 悬停提示（设置页和侧栏小条共用） ================= */
let tipBox = null, tipEl = null, tipT = 0;
function tipNode() {
  if (!tipBox) { tipBox = document.createElement('div'); tipBox.className = 'uu-tip'; tipBox.setAttribute('role', 'tooltip'); tipBox.hidden = true; document.body.appendChild(tipBox); }
  return tipBox;
}
function showTip(el) {
  if (!el.isConnected) return;
  const text = el.getAttribute('data-uutip') || '';
  if (!text) return;
  const t = tipNode();
  t.innerHTML = text.split('\n').filter(Boolean).map((l, i) => i ? `<span>${esc(l)}</span>` : `<b>${esc(l)}</b>`).join('');
  t.hidden = false;
  const r = el.getBoundingClientRect(), w = t.offsetWidth, h = t.offsetHeight;
  let y = r.top - h - 8;
  if (y < 6) y = r.bottom + 8;
  const x = Math.max(6, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 6));
  t.style.left = Math.round(x) + 'px'; t.style.top = Math.round(y) + 'px';
}
function hideTip() {
  clearTimeout(tipT); tipT = 0;
  if (tipBox) tipBox.hidden = true;
  const me = document.getElementById('hMe');  // 侧栏账号行的“查看用量”原生提示：悬停小条时先收起，免得两层提示
  if (me && me.dataset.uutitle != null) { me.title = me.dataset.uutitle; delete me.dataset.uutitle; }
  tipEl = null;
}
document.addEventListener('mouseover', e => {
  const el = e.target && e.target.closest ? e.target.closest('[data-uutip]') : null;
  if (el === tipEl) return;
  hideTip();
  if (!el) return;
  tipEl = el;
  const me = el.closest('#hMe');
  if (me && me.title) { me.dataset.uutitle = me.title; me.removeAttribute('title'); }
  tipT = setTimeout(() => showTip(el), 90);
});
document.addEventListener('focusin', e => { const el = e.target && e.target.closest && e.target.closest('[data-uutip]'); if (el) { hideTip(); tipEl = el; showTip(el); } });
document.addEventListener('focusout', () => hideTip());
document.addEventListener('mouseout', e => { if (tipEl && !(e.relatedTarget && tipEl.contains(e.relatedTarget))) hideTip(); });
addEventListener('scroll', hideTip, true);
addEventListener('blur', hideTip);
/* 触屏没有悬停：点一下“—”、额度条、节奏图标就显示原因，3 秒后收起 */
document.addEventListener('click', e => {
  const el = e.target && e.target.closest ? e.target.closest('.uu-page [data-uutip]:not(button):not(a)') : null;
  if (!el) return;
  hideTip(); tipEl = el; showTip(el);
  tipT = setTimeout(hideTip, 3000);
});

/* ================= 额度数据 ================= */
const QS = {map: {}, tightest: null, legacy: null, at: 0, loading: false, refreshed: new Set(), gen: -1, busy: false};
function takeQuota(d) {
  if (!d || typeof d !== 'object') return;
  for (const p of Array.isArray(d.providers) ? d.providers : []) if (p && p.id) QS.map[p.id] = p;
  if ('tightest' in d) QS.tightest = d.tightest || null;
  // 11-23：Claude 卡恢复第十四轮老额度卡，用 quota 接口原有字段；quota 事件不带这些字段，Claude 卡沿用上次读到的
  if (Array.isArray(d.windows) && 'ok' in d) QS.legacy = {ok: d.ok, err: d.err || '', ts: d.ts, age: d.age, stale: d.stale, windows: d.windows};
  QS.at = Date.now();
  const b = sj.body && sj.body();
  if (b && sj.isOpen() && b.dataset.sec === 'usage') drawQuota();  // 事件推来的新数据、60 秒重读，页面开着就跟着重画
}
const live = g => g === sj.gen() && sj.isOpen() && sj.body() && sj.body().dataset.sec === 'usage';
const q = s => { const b = sj.body(); return b ? b.querySelector(s) : null; };

/* 打开额度这一块时：当前提供方刷新一次（refresh:true），之后靠 quota 事件 */
async function refreshQuota(g, force) {
  const key = st.prov;
  if (!force && QS.gen === g && QS.refreshed.has(key)) return;
  if (QS.gen !== g) { QS.gen = g; QS.refreshed = new Set(); }
  QS.refreshed.add(key);
  QS.loading = true;
  if (live(g)) drawQuota();
  let d = null;
  try { d = await api('quota', {provider: key, refresh: true}); } catch {}
  QS.loading = false;
  if (d) takeQuota(d);
  else if (live(g)) drawQuota();
}

/* ---------- 额度卡（T3 UsageLimits LimitWindows / WindowBar） ---------- */
/* T3 paceMarkStyles：刻度所在处从条上挖出 6px 的透明缺口（clip-path，不用 mask，免得圆角重新抗锯齿） */
function paceGap(tl) {
  const at = `clamp(3px, ${tl}%, calc(100% - 3px))`, l = `calc(${at} - 3px)`, r = `calc(${at} + 3px)`, n = '-1px', f = 'calc(100% + 1px)';
  return {tick: `left:${at}`, gap: `clip-path:polygon(${n} ${n}, ${l} ${n}, ${l} ${f}, ${r} ${f}, ${r} ${n}, ${f} ${n}, ${f} ${f}, ${n} ${f})`};
}
function paceIcon(pace) {
  const [label, ic] = PACE[pace];
  return `<span class="uu-pace" data-pace="${pace}" role="img" aria-label="${esc(label)}" data-uutip="${esc(label)}">${ic}</span>`;
}
function windowRow(w, now) {
  const rem = remainingPercent(w), tl = paceRemainingPercent(w, now), pace = paceOf(w, now), rin = formatResetsIn(w, now);
  const r = resetMillis(w), at = r ? formatUpcoming(r, now) : null, label = String(w.label || w.id || '');
  const summary = tl === null ? (rin ? `${label}：剩余 ${rem}%，${rin}` : `${label}：剩余 ${rem}%`)
    : rin ? `${label}：剩余 ${rem}%，窗口剩余 ${tl}%，${rin}` : `${label}：剩余 ${rem}%，窗口剩余 ${tl}%`;
  const tip = [tl !== null ? `剩余 ${rem}% · 窗口剩余 ${tl}%` : `剩余 ${rem}%`, tl !== null ? '竖线表示匀速消耗时的位置。' : '',
    at ? (rin && r > now ? `${at} 重置 · ${rin}` : `${at} 重置`) : ''].filter(Boolean).join('\n');
  const pm = tl === null ? null : paceGap(tl);
  return `<div class="uu-w" data-w="${esc(w.id || label)}">` +
    `<span class="uu-wl"><span class="n" title="${esc(label)}">${esc(label)}</span><b class="uu-left">剩余 ${rem}%</b></span>` +
    `<div class="uu-bar" role="img" tabindex="0" aria-label="${esc(summary)}" data-uutip="${esc(tip)}">` +
      `<div class="uu-gap"${pm ? ` style="${pm.gap}"` : ''}><i class="trk"></i>${rem > 0 ? `<i class="fill" style="width:${rem}%"></i>` : ''}</div>` +
      (pm ? `<span class="uu-tick" data-tl="${tl}" style="${pm.tick}"></span>` : '') + '</div>' +
    `<span class="uu-wr">${pace ? paceIcon(pace) : ''}<span class="uu-reset">${esc(rin || '')}</span></span></div>`;
}
function qcard(p, now) {
  const id = p.id, live0 = Array.isArray(p.windows) ? p.windows : [];
  const lg = p.lastGood && Array.isArray(p.lastGood.windows) ? p.lastGood : null;
  const off = !p.ok;
  const ws = live0.length ? live0 : off && lg ? lg.windows : [];
  const meta = [];
  if (p.checking) meta.push('<span class="uu-chk"><span class="sj-spin"></span>正在刷新…</span>');
  else if (!off && p.ts) meta.push(`<span class="uu-upd1">更新于 ${hm(p.ts)}</span>`);
  else if (off && ws.length && (live0.length ? p.ts : lg && lg.ts)) meta.push(`<span class="uu-ago">${esc(ago(live0.length ? p.ts : lg.ts, now))}</span>`);
  if (p.stale) meta.push('<span class="uu-stale">可能已过时</span>');
  const plan = p.plan ? String(p.plan) : '';
  let h = `<div class="uu-qc${off ? ' uu-off' : ''}" data-p="${esc(id)}" style="--uu-c:${pcolor(id)}">` +
    `<div class="uu-qh"><i class="uu-dot"></i><b>${esc(plabel(id, p.label))}</b>` +
    (plan ? `<span class="uu-plan" data-uutip="套餐">${esc(plan.charAt(0).toUpperCase() + plan.slice(1))}</span>` : '') +
    `<span class="grow"></span><span class="uu-qm">${meta.join('')}</span></div>`;
  if (p.viaProxy) h += '<div class="uu-proxy">账号额度，和实际用量可能不符</div>';
  if (off) h += `<div class="uu-why">${esc((p.unavailable && p.unavailable.text) || '无法读取限额。')}</div>`;
  else if (!ws.length) h += '<div class="uu-why">没有报告限额。</div>';
  // T3 ComposerUsageLimits：某个窗口用完时，“{provider} 用量已用完 · {time} 重置”
  const out = !off && ws.find(w => remainingPercent(w) <= 0 && (resetMillis(w) || 0) > now);
  if (out) h += `<div class="uu-out">${esc(plabel(id, p.label))} 用量已用完 · ${esc(formatUpcoming(resetMillis(out), now))} 重置</div>`;
  if (ws.length) h += `<div class="uu-ws">${ws.map(w => windowRow(w, now)).join('')}</div>`;
  const ex = [];
  const cr = p.credits;
  if (cr && typeof cr === 'object') {
    if (cr.unlimited) ex.push('<span class="uu-credits">积分不限量</span>');
    else if (cr.balance != null && cr.balance !== '' && (cr.hasCredits || +cr.balance > 0)) ex.push(`<span class="uu-credits">积分余额 ${esc(cr.balance)}</span>`);
  }
  const rc = p.resetCredits;
  if (rc && +rc.availableCount > 0) {  // T3 resetCreditChoices 的文案；只显示，不提供兑换
    const n = +rc.availableCount, exp = +rc.nextExpiresAt ? formatDuration(toMs(rc.nextExpiresAt) - now) : null;
    ex.push(`<span class="uu-rc">${IC_TICKET}${exp ? `已存有 ${n} 个重置额度 · 下一个将在 ${exp} 后过期` : `已存有 ${n} 个重置额度`}</span>`);
  }
  if (ex.length) h += `<div class="uu-qx">${ex.join('')}</div>`;
  return h + '</div>';
}
/* ---------- 11-23 用户要求：Claude 卡恢复第十四轮老额度卡（ux-j.js quotaInner/quotaCard，样式 .sj-qcard/.sj-q*），
   去掉 5 小时，进度条改成显示剩余（宽＝100−已用，右侧“剩余 X%”）；其余照老卡：重置时间、余裕、颜色分档、更新时间 ---------- */
const aboutT = s => { s = Math.max(0, s); const d = Math.floor(s / 86400), h = Math.round(s / 3600), m = Math.max(1, Math.round(s / 60));
  return d >= 2 ? `约 ${Math.round(s / 86400)} 天` : h >= 1 ? `约 ${h} 小时` : `约 ${m} 分钟`; };
function claudeOldInner(qd) {
  if (!qd) return '<div class="sj-dim sj-qnone">读取中…</div>';
  if (qd.err) return `<div class="sj-qnone sj-qerr">${esc(qd.err)}</div>`;
  const ws = (qd.windows || []).filter(w => w && w.key !== '5h');
  if (!ws.length) return '<div class="sj-dim sj-qnone">暂无额度数据</div>';
  const now = Date.now() / 1000;
  return ws.map(w => {
    const u = Math.max(0, Math.min(100, +w.used || 0)), hr = w.headroom, rem = 100 - u;
    const lvl = u >= 95 ? 'red' : (hr != null && hr < 0) || u >= 80 ? 'amber' : 'ok';
    const reset = w.resetAt ? aboutT(w.resetAt - now) + '后重置' : '';
    const room = hr == null ? '' : hr >= 0 ? `余裕 ${hr}` : '<span class="no">无余裕</span>';
    return `<div class="sj-q ${lvl}" data-k="${esc(w.key || '')}"><span class="sj-qn">${esc(w.label || w.key || '')}</span><span class="sj-qbar"><i style="width:${rem}%"></i></span>` +
      `<b class="sj-qu">剩余 ${Math.round(rem)}%</b><span class="sj-qm">${[reset ? esc(reset) : '', room].filter(Boolean).join(' · ')}</span></div>`;
  }).join('');
}
function claudeOldCard() {
  const qd = QS.legacy;
  const upd = qd && qd.ts ? (qd.stale ? `${aboutT(qd.age || 0)}前的数据` : '更新于 ' + hm(qd.ts * 1000)) : '';
  return `<div class="sj-qcard uu-qold" data-p="claude"><div class="sj-qh">Claude<span${qd && qd.stale ? ' class="stale"' : ''}>${esc(upd)}</span></div>${claudeOldInner(qd)}</div>`;
}
/* ---------- 11-23 用户要求：Codex、Antigravity 页也是老样式，额度卡照 Claude 老卡的写法：每个窗口一行（名字、剩余条、“剩余 X%”、
   “约 N 天后重置 · 余裕 N”，颜色分档同老卡），余裕＝窗口已过去的％－已用％；套餐写在标题旁，代理提示、积分、重置额度放卡片底部一行小字；
   读不到时显示原因（有上次读到的就照样列出来、标成过时） ---------- */
function oldRow(w, now) {
  const used = Math.max(0, Math.min(100, +w.usedPercent || 0)), rem = remainingPercent(w), e = elapsedShare(w, now);
  const hr = e === null ? null : Math.round(e * 100 - used);
  const lvl = used >= 95 ? 'red' : (hr !== null && hr < 0) || used >= 80 ? 'amber' : 'ok';
  const r = resetMillis(w), reset = r && r > now ? aboutT((r - now) / 1000) + '后重置' : '';
  const room = hr === null ? '' : hr >= 0 ? `余裕 ${hr}` : '<span class="no">无余裕</span>';
  const label = String(w.label || w.id || '');
  return `<div class="sj-q ${lvl}" data-k="${esc(w.id || label)}"><span class="sj-qn" title="${esc(label)}">${esc(label)}</span><span class="sj-qbar"><i style="width:${rem}%"></i></span>` +
    `<b class="sj-qu">剩余 ${rem}%</b><span class="sj-qm">${[reset ? esc(reset) : '', room].filter(Boolean).join(' · ')}</span></div>`;
}
function oldQcard(p, now) {
  const id = p.id, live0 = Array.isArray(p.windows) ? p.windows : [];
  const lg = p.lastGood && Array.isArray(p.lastGood.windows) ? p.lastGood : null;
  const off = !p.ok, ws = live0.length ? live0 : off && lg ? lg.windows : [];
  const ts = live0.length ? p.ts : lg && lg.ts;
  const upd = p.checking ? '正在刷新…' : !off && p.ts ? '更新于 ' + hm(p.ts) + (p.stale ? ' · 可能已过时' : '') : off && ws.length && ts ? ago(ts, now) + '的数据' : '';
  const plan = p.plan ? String(p.plan) : '';
  const wide = ws.some(w => String(w.label || w.id || '').length > 5);
  let h = `<div class="sj-qcard uu-qold${wide ? ' uu-qwide' : ''}" data-p="${esc(id)}"><div class="sj-qh">${esc(plabel(id, p.label))}` +
    (plan ? `<em class="uu-qplan">${esc(plan.charAt(0).toUpperCase() + plan.slice(1))}</em>` : '') +
    `<span${(off && ws.length) || p.stale ? ' class="stale"' : ''}>${esc(upd)}</span></div>`;
  if (off) h += `<div class="sj-qnone sj-qerr">${esc((p.unavailable && p.unavailable.text) || '无法读取限额。')}</div>`;
  else if (!ws.length) h += '<div class="sj-dim sj-qnone">没有报告限额。</div>';
  h += ws.map(w => oldRow(w, now)).join('');
  const ex = [];
  if (p.viaProxy) ex.push('账号额度，和实际用量可能不符');
  const cr = p.credits;
  if (cr && typeof cr === 'object') {
    if (cr.unlimited) ex.push('积分不限量');
    else if (cr.balance != null && cr.balance !== '' && (cr.hasCredits || +cr.balance > 0)) ex.push(`积分余额 ${cr.balance}`);
  }
  const rc = p.resetCredits;
  if (rc && +rc.availableCount > 0) {  // 只显示，不提供兑换
    const n = +rc.availableCount, exp = +rc.nextExpiresAt ? formatDuration(toMs(rc.nextExpiresAt) - now) : null;
    ex.push(exp ? `已存有 ${n} 个重置额度 · 下一个将在 ${exp} 后过期` : `已存有 ${n} 个重置额度`);
  }
  if (ex.length) h += `<div class="sj-dim sj-qnone uu-qfoot">${esc(ex.join(' · '))}</div>`;
  return h + '</div>';
}
function drawQuota() {
  const box = q('.uu-qbody');
  if (!box) return;
  if (tipEl && box.contains(tipEl)) hideTip();
  const now = Date.now(), ids = st.prov === 'all' ? PIDS : [st.prov], old = box.classList.contains('uu-qbody-old');
  const ps = ids.map(id => (id === 'claude' ? (QS.map.claude || QS.legacy ? {id: 'claude'} : null) : QS.map[id])).filter(Boolean);
  if (!ps.length) {
    box.innerHTML = old ? `<div class="sj-qcard uu-qold" data-p="${esc(st.prov)}"><div class="sj-qh">${esc(plabel(st.prov))}<span></span></div>` +
      `<div class="sj-dim sj-qnone">${QS.loading ? '读取中…' : '暂无额度数据'}</div></div>`
      : `<div class="uu-empty">${QS.loading ? '<span class="sj-spin"></span>读取中…' : '暂无额度数据'}</div>`;
    return;
  }
  box.innerHTML = ps.map(p => (p.id === 'claude' ? claudeOldCard() : old ? oldQcard(p, now) : qcard(p, now))).join('');
}
/* 11-23：页面开着时每 60 秒重读一次 quota（老卡的节奏；Claude 卡要老字段，事件里没有） */
let qTimer = 0;
function quotaTick(g) {
  clearInterval(qTimer);
  qTimer = setInterval(async () => {
    if (!live(g)) { clearInterval(qTimer); return; }
    if (document.hidden) return;
    try { takeQuota(await api('quota', {provider: st.prov})); } catch {}
  }, MINUTE);
}

/* 11-23 用户要求：侧栏额度小条去掉，账号行恢复上线前的样子（只有心情和今天的请求、费用）；
   不再创建 #uuStrip，已经有的移除；不再为它做启动读取、定时兜底和切回前台读取 */
{ const old = document.getElementById('uuStrip'); if (old) old.remove(); }

/* 后台事件：串接已有的 CWB.onEvent，不覆盖 */
const prevEv = C.onEvent;
C.onEvent = ev => {
  try { if (prevEv) prevEv(ev); } finally {
    if (ev && ev.type === 'quota') {
      try { takeQuota(ev); } catch (e) { console.error(e); }
    }
  }
};

/* ================= 图表：移植自 T3 UsageProviderChart.tsx ================= */
const VW = 960, VH = 260, TICK_COUNT = 4, PLOT_TOP = 8;
function monotoneTangents(pts) {
  const n = pts.length;
  if (n < 2) return [0];
  const sl = [];
  for (let i = 0; i < n - 1; i++) { const dx = pts[i + 1].x - pts[i].x, dy = pts[i + 1].y - pts[i].y; sl.push(dx === 0 ? 0 : dy / dx); }
  const t = Array.from({length: n}, () => 0);
  t[0] = sl[0] || 0; t[n - 1] = sl[n - 2] || 0;
  for (let i = 1; i < n - 1; i++) { const a = sl[i - 1] || 0, b = sl[i] || 0; t[i] = a * b <= 0 ? 0 : (a + b) / 2; }
  for (let i = 0; i < n - 1; i++) {
    const s = sl[i] || 0;
    if (s === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / s, b = t[i + 1] / s, mag = a * a + b * b;
    if (mag > 9) { const k = 3 / Math.sqrt(mag); t[i] = k * a * s; t[i + 1] = k * b * s; }
  }
  return t;
}
function curvePath(pts) {
  if (pts.length < 2) return '';
  const t = monotoneTangents(pts), f = v => v.toFixed(2);
  let d = `M${f(pts[0].x)},${f(pts[0].y)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i], b = pts[i + 1], dx = b.x - a.x;
    d += ` C${f(a.x + dx / 3)},${f(a.y + t[i] * dx / 3)} ${f(b.x - dx / 3)},${f(b.y - t[i + 1] * dx / 3)} ${f(b.x)},${f(b.y)}`;
  }
  return d;
}
function niceScale(peak, count) {
  if (peak <= 0) return {max: 0, ticks: [0]};
  const raw = peak / count, mag = 10 ** Math.floor(Math.log10(raw)), nrm = raw / mag;
  const step = (nrm > 5 ? 10 : nrm > 2 ? 5 : nrm > 1 ? 2 : 1) * mag;
  const max = Math.ceil(peak / step) * step, ticks = [];
  for (let v = 0; v <= max + step * 1e-6; v += step) ticks.push(v);
  return {max, ticks};
}
const valueToY = (v, max) => (max === 0 ? VH : VH - v / max * (VH - PLOT_TOP));
/* o = {periods:[{label, tip}], series:[{id,label,color,values:[数|null]}], area, total, fmt, tickFmt}
   T3：各序列都从 0 起分层（不叠加），按合计从大到小画；工作台补充：值为 null 的点断开（速度图、今天还没到的小时） */
function chartHtml(o) {
  const n = o.periods.length, stepX = n < 2 ? 0 : VW / (n - 1);
  const peak = o.series.reduce((m, s) => s.values.reduce((a, v) => (v != null && v > a ? v : a), m), 0);
  const sc = niceScale(peak, TICK_COUNT), toY = v => valueToY(v, sc.max);
  const grid = sc.ticks.map(t => `<line x1="0" x2="${VW}" y1="${toY(t).toFixed(2)}" y2="${toY(t).toFixed(2)}" class="uu-gl" vector-effect="non-scaling-stroke"/>`).join('');
  const order = o.series.map((s, i) => [i, s.values.reduce((a, v) => a + (v || 0), 0)]).sort((a, b) => b[1] - a[1]).map(x => x[0]);
  let areas = '', lines = '';
  for (const i of order) {
    const s = o.series[i];
    let run = [];
    const flush = () => {
      if (run.length === 1) lines += `<path d="M${run[0].x.toFixed(2)},${run[0].y.toFixed(2)} l0.01,0" class="uu-dotp" stroke="${s.color}" vector-effect="non-scaling-stroke"/>`;
      else if (run.length > 1) {
        const d = curvePath(run);
        if (o.area) areas += `<path d="${d} L${run[run.length - 1].x.toFixed(2)},${VH} L${run[0].x.toFixed(2)},${VH} Z" fill="${s.color}" fill-opacity="0.12"/>`;
        lines += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" vector-effect="non-scaling-stroke" data-s="${esc(s.id)}"/>`;
      }
      run = [];
    };
    s.values.forEach((v, k) => { if (v == null || !isFinite(v)) flush(); else run.push({x: k * stepX, y: toY(v)}); });
    flush();
  }
  const tf = o.tickFmt || o.fmt;
  const yax = peak > 0 ? sc.ticks.map(t => `<span style="top:${(toY(t) / VH * 100).toFixed(2)}%">${t === 0 ? '0' : esc(tf(t))}</span>`).join('') : '';
  const mid = n ? o.periods[Math.floor(n / 2)] : null;
  const xax = n ? `<span>${esc(o.periods[0].label)}</span><span>${esc(mid.label)}</span><span>${esc(o.periods[n - 1].label)}</span>` : '';
  const lg = o.series.map(s => `<span data-s="${esc(s.id)}"><i style="background:${s.color}"></i>${esc(s.label)}${s.note ? `<em class="uu-scope">${esc(s.note)}</em>` : ''}</span>`).join('');
  return `<div class="uu-chart${o.cls ? ' ' + o.cls : ''}"><div class="uu-cw"><div class="uu-yax" aria-hidden="true">${yax}</div>` +
    `<div class="uu-plot"><svg viewBox="0 0 ${VW} ${VH}" preserveAspectRatio="none" aria-hidden="true">${grid}${areas}${lines}` +
    `<line class="uu-hl" x1="0" x2="0" y1="${PLOT_TOP}" y2="${VH}" vector-effect="non-scaling-stroke"/></svg><div class="uu-ctip" hidden></div></div></div>` +
    `<div class="uu-xax" aria-hidden="true">${xax}</div>${o.series.length ? `<div class="uu-lg">${lg}</div>` : ''}</div>`;
}
/* T3 UsageChartPlot 的悬停：鼠标横坐标换算成第几个点，画竖线，提示框放在指针右下（放不下就换到左上） */
function wireChart(root, o) {
  const plot = root && root.querySelector('.uu-plot');
  if (!plot) return;
  const n = o.periods.length, stepX = n < 2 ? 0 : VW / (n - 1), hl = plot.querySelector('.uu-hl'), tp = plot.querySelector('.uu-ctip');
  const move = e => {
    const b = plot.getBoundingClientRect();
    if (!n || !b.width) return;
    const lx = Math.min(b.width, Math.max(0, e.clientX - b.left)), ly = Math.min(b.height, Math.max(0, e.clientY - b.top));
    const i = Math.min(n - 1, Math.max(0, Math.round(lx / b.width * (n - 1))));
    hl.setAttribute('x1', i * stepX); hl.setAttribute('x2', i * stepX); hl.classList.add('on');
    const vals = o.series.map(s => s.values[i]);
    let h = `<div class="t">${esc(o.periods[i].tip || o.periods[i].label)}</div>` + o.series.map((s, k) =>
      `<div class="r"><span><i style="background:${s.color}"></i>${esc(s.label)}</span><b>${vals[k] == null ? '—' : esc(o.fmt(vals[k]))}</b></div>`).join('');
    if (o.total) h += `<div class="r tot"><span>总计</span><b>${esc(o.fmt(vals.reduce((a, v) => a + (v || 0), 0)))}</b></div>`;
    tp.innerHTML = h; tp.hidden = false;
    const tw = tp.offsetWidth, th = tp.offsetHeight, gap = 12;
    const left = lx + gap + tw <= b.width ? lx + gap : lx - gap - tw, top = ly + gap + th <= b.height ? ly + gap : ly - gap - th;
    tp.style.left = Math.min(Math.max(0, left), Math.max(0, b.width - tw)) + 'px';
    tp.style.top = Math.min(Math.max(0, top), Math.max(0, b.height - th)) + 'px';
  };
  plot.addEventListener('pointermove', move);
  plot.addEventListener('pointerdown', move);
  plot.addEventListener('pointerleave', () => { hl.classList.remove('on'); tp.hidden = true; });
}

/* ================= 时段 ================= */
const WD = '日一二三四五六';
function periodsOf(range, now) {
  const out = [], base = new Date(now); base.setHours(0, 0, 0, 0);
  if (range === 'today') {
    for (let h = 0; h < 24; h++) { const t = new Date(base); t.setHours(h); out.push({k: dkey(t) + 'T' + p2(h), t: +t, label: p2(h) + ':00', tip: `今天 ${p2(h)}:00`}); }
  } else {
    for (let i = (range === '7d' ? 7 : 30) - 1; i >= 0; i--) {
      const t = new Date(base); t.setDate(t.getDate() - i);
      out.push({k: dkey(t), t: +t, label: `${t.getMonth() + 1}月${t.getDate()}日`, tip: `${t.getMonth() + 1}月${t.getDate()}日 周${WD[t.getDay()]}`});
    }
  }
  return out;
}
/* 今天：还没到的小时不画（x 轴仍是一整天） */
const cutFuture = (ps, now, range) => (range === 'today' ? ps.findIndex(p => p.t > now) : -1);

/* ================= 用量数据规整 ================= */
/* 契约的 UsageBucket（T3 contracts/usage.ts）：有 provider、totals。字段名和第十四轮的 buckets 撞了，这里都认：
   先找带 provider/totals 的数组；都没有时退回第十四轮的 buckets（只描述 Claude，每个桶带按模型拆开的 tokens、cost） */
function normRows(d, range) {
  const isT3 = a => Array.isArray(a) && a.some(b => b && typeof b === 'object' && 'totals' in b && 'provider' in b);
  const t3 = [d.usageBuckets, d.t3Buckets, d.ubuckets, d.buckets].find(isT3);
  const day = range !== 'today';
  if (t3) {
    return t3.filter(b => b && b.totals).map(b => {
      const t = b.totals, unc = +t.uncachedInputTokens || 0, ca = +t.cachedInputTokens || 0, cw = +t.cacheCreationTokens || 0, out = +t.outputTokens || 0;
      const k = b.hourStart ? String(b.hourStart).slice(0, 13) : String(b.day || '');
      return {p: day ? k.slice(0, 10) : k, provider: b.provider, model: b.model || '', tokens: unc + ca + cw + out,
        cost: b.costSource === 'unpriced' ? null : +b.costUsd || 0};
    });
  }
  const rows = [];
  for (const b of Array.isArray(d.buckets) ? d.buckets : []) {
    if (!b || !b.models || typeof b.models !== 'object') continue;
    const k = String(b.key || '').replace(' ', 'T');
    for (const [m, v] of Object.entries(b.models)) rows.push({p: day ? k.slice(0, 10) : k, provider: 'claude', model: m, tokens: +v.tokens || 0, cost: v.cost == null ? null : +v.cost || 0});
  }
  return rows;
}
/* 没有 providers 字段时（后端还是第十四轮）：用原有的 kpi 拼出 Claude 一家 */
function provsOf(d) {
  if (Array.isArray(d.providers)) return d.providers.filter(p => p && want(p.id));
  if (!d.kpi || !want('claude')) return [];
  const k = d.kpi;
  return [{id: 'claude', label: 'Claude', scope: 'workbench', scanning: !!d.scanning, kpi: {requests: k.requests, sessions: (d.sessions || []).length, uncachedInput: k.input,
    cachedInput: k.cacheRead, cacheWrite: k.cacheWrite, output: k.output, tokens: k.tokens, cost: k.cost, costSource: 'modelPriced', cacheHit: k.cacheHit, saved: k.saved}}];
}
function labelMap(d) {
  const m = {};
  for (const x of d.models || []) if (x && x.id) m['claude:' + x.id] = x.label || x.id;
  for (const p of (d.speed && d.speed.providers) || []) for (const x of p.models || []) if (x && x.model) m[p.id + ':' + x.model] = x.label || x.model;
  return m;
}
function scanInfo(d, ps) {
  const sc = ps.filter(p => p.scanning);
  const on = sc.length > 0 || (!Array.isArray(d.providers) && !!d.scanning);
  let b = 0, tb = 0;
  for (const p of sc) if (p.progress) { b += +p.progress.bytes || 0; tb += +p.progress.totalBytes || 0; }
  return {on, pct: tb > 0 ? Math.min(99, Math.floor(b / tb * 100)) : null};
}

/* ================= 页面 ================= */
const PROV_OPTS = [['all', '全部'], ['claude', 'Claude'], ['codex', 'Codex'], ['agy', 'Antigravity']];
const RANGE_OPTS = [['today', '今天'], ['7d', '7 天'], ['30d', '30 天']];
const US = {cache: {}, timer: 0, seq: 0, last: null};
const ukey = () => `${st.range}|${st.prov}|${st.rollup ? 1 : 0}|${st.cscope}`;
/* 11-23 用户要求：只看某一家（Claude、Codex、Antigravity）时整页恢复第十四轮的“用量”页（ux-j.js 的 rUsage：指标卡、柱状图、
   模型 Top 4、活跃度、用量最多的对话、最近请求），不显示速度块和 T3 样式的用量块。
   Claude：数据是 usage 接口原有字段（只算工作台），额度卡用上面 11-23 版的 Claude 老卡（没有 5 小时、显示剩余）。
   Codex、Antigravity：rUsage({provider}) 取 usage {legacy: 提供方}（usage_p.Hub.legacy，字段同 Claude，含终端），额度卡用 oldQcard。
   顶部多一行提供方切换，位置和“全部”一样；时段和“全部”共用一个选择。“全部”仍是新页面 */
const oldUsage = sj.RENDER.usage;  // 第十四轮 rUsage，要在文件末尾改挂 render 之前取
const OLD_LEAD = {
  codex: '统计本机所有 Codex 对话（含终端和其他客户端）· 订阅计费，只看 Token 不算钱',
  agy: '统计本机所有 Antigravity 对话（含终端和其他客户端）· 费用只算 Antigravity 自己报的',
};
function renderOld() {
  const b = sj.body(), g = sj.gen();
  hideTip();
  if (st.prov === 'claude') oldUsage(); else oldUsage({provider: st.prov});
  const h2 = b.querySelector('.sj-hd h2');
  if (h2) h2.textContent = '额度与速度';  // 和左侧导航同名
  const lead = b.querySelector('.sj-lead');
  if (lead && OLD_LEAD[st.prov]) lead.textContent = OLD_LEAD[st.prov];
  (lead || b.querySelector('.sj-hd')).insertAdjacentHTML('afterend',
    `<div class="uu-top uu-top-old">${sj.seg('uprov', PROV_OPTS, st.prov, '提供方')}${st.prov === 'claude' ? cscopeSeg() : ''}</div>`);
  sj.bind(b.querySelector('.uu-top-old'), {uprov: v => { st.prov = v; sset('prov', v); hideTip(); render(); },
    ucscope: v => { st.cscope = v; sset('cscope', v); hideTip(); render(); }});
  const oc = b.querySelector('.sj-qcard');  // 老页面自带的额度卡（Claude 的，有 5 小时、显示已用）换成这一家的
  if (oc) { const box = document.createElement('div'); box.className = 'uu-qbody uu-qbody-old'; oc.replaceWith(box); }
  const rs = b.querySelector('.sj-ubar .sj-seg[data-k=range]');
  if (rs) {
    const btn = rs.querySelector(`button[data-v="${st.range}"]`);
    if (btn && !btn.classList.contains('on')) btn.click();  // 进来时对齐其他几栏的时段
    rs.addEventListener('click', e => {
      const x = e.target.closest('button[data-v]');
      if (x && RANGE_OPTS.some(([k]) => k === x.dataset.v)) { st.range = x.dataset.v; sset('range', st.range); }
    });
  }
  drawQuota();
  refreshQuota(g);
  quotaTick(g);
}
function render() {
  const b = sj.body(), g = sj.gen();
  clearTimeout(US.timer); US.seq++;
  if (st.prov !== 'all' && oldUsage) return renderOld();
  b.innerHTML = sj.head('额度与速度') + '<div class="uu-page">' +
    `<div class="uu-top">${sj.seg('uprov', PROV_OPTS, st.prov, '提供方')}${sj.seg('urange', RANGE_OPTS, st.range, '时段')}${cscopeSeg()}<span class="grow"></span>` +
    `<span class="uu-upd"></span><button type="button" class="uu-rf" data-a="refresh" aria-label="刷新" data-uutip="刷新">${IC_RF}</button></div>` +
    '<section class="uu-sec" data-b="quota"><h3 class="uu-h">额度</h3><div class="uu-qbody"></div></section>' +
    '<section class="uu-sec" data-b="speed"><h3 class="uu-h">速度</h3><div class="uu-sbody"></div></section>' +
    '<section class="uu-sec" data-b="usage"><h3 class="uu-h">用量<span class="uu-scan" hidden></span></h3><div class="uu-ubody"></div></section></div>';
  sj.bind(b.querySelector('.uu-top'), {
    uprov: v => { st.prov = v; sset('prov', v); hideTip(); if (v !== 'all') { render(); return; } drawQuota(); refreshQuota(g); loadUsage(g); },
    urange: v => { st.range = v; sset('range', v); hideTip(); loadUsage(g); },
    ucscope: v => { st.cscope = v; sset('cscope', v); hideTip(); loadUsage(g); },
  }, {refresh: (e, btn) => {
    if (btn.classList.contains('spin')) return;
    btn.classList.add('spin');
    US.cache = {};
    Promise.allSettled([refreshQuota(g, true), loadUsage(g)]).then(() => { if (btn.isConnected) btn.classList.remove('spin'); });
  }});
  drawQuota();
  refreshQuota(g);
  quotaTick(g);
  loadUsage(g);
}
async function loadUsage(g, quiet) {
  clearTimeout(US.timer);
  const seq = ++US.seq, key = ukey(), cached = US.cache[key];
  if (cached) drawData(cached);
  else if (!quiet) {
    for (const s of ['.uu-sbody', '.uu-ubody']) { const x = q(s); if (x) x.innerHTML = '<div class="uu-empty"><span class="sj-spin"></span>读取中…</div>'; }
  }
  let d;
  try { d = await api('usage', {range: st.range, provider: st.prov, rollup: !!st.rollup, claudeScope: st.cscope}); }
  catch (e) {
    if (!live(g) || seq !== US.seq) return;
    if (!cached) {
      for (const s of ['.uu-sbody', '.uu-ubody']) {
        const x = q(s); if (!x) continue;
        x.innerHTML = sj.errBox('暂时读不到：' + String((e && e.message) || '请稍后再试').replace(/（\d+）$/, ''), true);  // 不上状态码
        sj.bind(x, {}, {retry: () => loadUsage(g)});
      }
    }
    return;
  }
  if (!live(g) || seq !== US.seq) return;
  d = d && typeof d === 'object' ? d : {};
  const same = cached && JSON.stringify(cached) === JSON.stringify(d);
  US.cache[key] = d;
  if (!same) drawData(d);
  if (scanInfo(d, provsOf(d)).on) US.timer = setTimeout(() => { if (live(g) && seq === US.seq) loadUsage(g, true); }, 2000);
}
function drawData(d) {
  US.last = d;
  SCOPE = {};
  if (Array.isArray(d.providers)) { for (const p of d.providers) if (p && p.id && p.scope) SCOPE[p.id] = p.scope; }
  else if (d.kpi) SCOPE.claude = 'workbench';  // 还是第十四轮的后端：只描述工作台里的 Claude
  const upd = q('.uu-upd');
  if (upd) upd.textContent = d.updated ? '更新于 ' + hm(d.updated) : '';
  if (tipEl && sj.body() && sj.body().contains(tipEl) && !tipEl.closest('.uu-qbody')) hideTip();
  try { drawSpeed(d); } catch (e) { console.error(e); const x = q('.uu-sbody'); if (x) x.innerHTML = `<div class="uu-empty">${esc(e.message)}</div>`; }
  try { drawUsage(d); } catch (e) { console.error(e); const x = q('.uu-ubody'); if (x) x.innerHTML = `<div class="uu-empty">${esc(e.message)}</div>`; }
}

/* ---------- 速度（T3 没有，按 PLAN U6 自建；表格、折线和 T3 的样式统一） ---------- */
const SMET = {tps: ['输出速度', fmtTps, v => String(Math.round(v))], ttft: ['首字等待', fmtMs, fmtMs], turn: ['每轮用时', fmtMs, fmtMs]};
/* 一个格子：null 显示“—”，悬停显示 why 里的原因；估算值前加“约” */
function sv(o, k, fmt, fbWhy) {
  const v = o ? o[k] : null, ak = k === 'turnP90' ? 'turn' : k;
  if (v == null || !isFinite(v)) {
    const why = (o && o.why && o.why[ak]) || fbWhy || '暂无数据';
    return `<span class="uu-na" tabindex="0" aria-label="${esc(why)}" data-uutip="${esc(why)}">—</span>`;
  }
  return (o.approx && o.approx[ak] ? '<span class="uu-approx">约 </span>' : '') + esc(fmt(v));
}
function drawSpeed(d) {
  const box = q('.uu-sbody');
  if (!box) return;
  const sp = d.speed && typeof d.speed === 'object' ? d.speed : {};
  const ps = (sp.providers || []).filter(p => p && want(p.id));
  const recent = (sp.recent || []).filter(r => r && want(r.provider)).slice(0, 20);
  const pts = (sp.series && sp.series.points) || [];
  if (!ps.length && !recent.length) { box.innerHTML = '<div class="uu-empty">此时间范围内没有活动。</div>'; return; }
  const turns = p => (p.models || []).reduce((a, m) => a + (+m.turns || 0), 0);
  let h = '<table class="uu-tbl uu-spd"><thead><tr><th class="l">模型</th><th class="o1">轮次</th><th>输出速度</th><th>首字等待</th><th>每轮用时</th><th class="o2">P90</th></tr></thead><tbody>' +
    ps.map(p => `<tr class="pv" data-p="${esc(p.id)}"><td class="l"><span class="uu-mn"><i class="uu-dot" style="background:${pcolor(p.id)}"></i>${esc(plabel(p.id, p.label))}${scopeTag(p.id)}</span></td>` +
      `<td class="o1">${fmtInt(turns(p))}</td><td>${sv(p, 'tps', fmtTps)}</td><td>${sv(p, 'ttft', fmtMs)}</td><td>${sv(p, 'turn', fmtMs)}</td><td class="o2">${sv(p, 'turnP90', fmtMs)}</td></tr>` +
      (p.models || []).map(m => `<tr class="mr" data-p="${esc(p.id)}" data-m="${esc(m.model || '')}"><td class="l"><span class="uu-mn" title="${esc(m.model || '')}">${esc(m.label || m.model || '未知模型')}</span></td>` +
        `<td class="o1">${fmtInt(m.turns)}</td><td>${sv(m, 'tps', fmtTps)}</td><td>${sv(m, 'ttft', fmtMs)}</td><td>${sv(m, 'turn', fmtMs)}</td><td class="o2">${sv(m, 'turnP90', fmtMs)}</td></tr>`).join('')).join('') +
    '</tbody></table>';
  // 折线：按小时（30 天按天），三家各一条
  const unit = (sp.series && sp.series.unit) || (st.range === '30d' ? 'day' : 'hour');
  const [mName, mFmt, mTick] = SMET[st.smetric];
  const ids = PIDS.filter(id => want(id) && pts.some(pt => pt && pt[id] && pt[id][st.smetric] != null && isFinite(pt[id][st.smetric])));
  const periods = pts.map(pt => ({label: shortLabel(pt, unit), tip: pt.label || shortLabel(pt, unit)}));
  const series = ids.map(id => ({id, label: plabel(id), note: isWb(id) ? WB : '', color: pcolor(id), values: pts.map(pt => { const v = pt[id] && pt[id][st.smetric]; return v == null || !isFinite(v) ? null : +v; })}));
  const sopt = {periods, series, fmt: mFmt, tickFmt: mTick, area: false, total: false, cls: 'uu-sch'};
  h += `<div class="uu-card"><div class="uu-ch"><h4>${mName}（按${unit === 'day' ? '天' : '小时'}）</h4><span class="grow"></span>` +
    `${sj.seg('smetric', [['tps', '输出速度'], ['ttft', '首字等待'], ['turn', '每轮用时']], st.smetric, '折线指标')}</div>` +
    (series.length && series.some(s => s.values.some(v => v != null)) ? chartHtml(sopt)
      : `<div class="uu-empty sm uu-nochart">${esc((ps.length === 1 && ps[0].why && ps[0].why[st.smetric]) || '这段时间没有可画的点')}</div>`) + '</div>';
  // 最近 20 轮
  if (recent.length) {
    const why = {};
    for (const p of ps) why[p.id] = p.why || {};
    const day = st.range !== 'today';
    h += '<h4 class="uu-h4">最近 20 轮</h4><table class="uu-tbl uu-rec"><thead><tr><th class="l c-t">时间</th><th class="l c-m">模型</th><th class="l c-c o1">对话</th>' +
      '<th class="c-n">首字等待</th><th class="c-n">用时</th><th class="c-n o2">输出</th><th class="c-n">速度</th></tr></thead><tbody>' +
      recent.map(r => {
        const t = toMs(r.ts), dt = new Date(t), fb = k => why[r.provider] && why[r.provider][k];
        const title = r.title || '未命名对话';
        return `<tr data-p="${esc(r.provider)}"><td class="l c-t" title="${esc(`${dt.getMonth() + 1}/${dt.getDate()} ${hm(t)}`)}">${day ? `<span class="dd">${p2(dt.getMonth() + 1)}-${p2(dt.getDate())} </span>` : ''}${hm(t)}</td>` +
          `<td class="l c-m"><span class="uu-mn" title="${esc(r.model || '')}"><i class="uu-dot" style="background:${pcolor(r.provider)}"></i>${esc(r.label || r.model || '—')}</span></td>` +
          `<td class="l c-c o1">${r.sid ? `<button type="button" class="sj-link" data-sid="${esc(r.sid)}" title="${esc(title)}">${esc(title)}</button>` : `<span class="uu-tt" title="${esc(title)}">${esc(title)}</span>`}</td>` +
          `<td>${sv(r, 'ttft', fmtMs, fb('ttft'))}</td><td>${sv(r, 'turn', fmtMs, fb('turn'))}</td><td class="o2">${r.output == null ? '—' : esc(formatTokens(r.output))}</td><td>${sv(r, 'tps', fmtTps, fb('tps'))}</td></tr>`;
      }).join('') + '</tbody></table>';
  } else if (ps.length) {
    h += `<h4 class="uu-h4">最近 20 轮</h4><div class="uu-empty sm uu-norec">${ps.every(p => isWb(p.id)) ? '最近轮次只列 Codex 和 Antigravity' : '这段时间没有回复记录'}</div>`;
  }
  box.innerHTML = h;
  sj.bind(box, {smetric: v => { st.smetric = v; sset('smetric', v); drawSpeed(US.last || d); }});
  wireChart(box.querySelector('.uu-sch'), sopt);
  wireLinks(box);
}
function shortLabel(pt, unit) {
  const k = String(pt.key || '');
  let m;
  if (unit === 'day' && (m = /^(\d{4})-(\d{2})-(\d{2})/.exec(k))) return `${+m[2]}月${+m[3]}日`;
  if ((m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2})/.exec(k))) return st.range === 'today' ? `${m[4]}:00` : `${+m[2]}/${+m[3]} ${m[4]}:00`;
  return String(pt.label || k);
}
function wireLinks(box) {
  box.querySelectorAll('.sj-link[data-sid]').forEach(b => b.onclick = () => {
    const sid = b.dataset.sid;
    if (!sid) return;
    hideTip();
    if (C.closeSettings) C.closeSettings();
    C.openSession(sid);
  });
}

/* ---------- 用量（T3 UsageOverview ＋ 第十四轮的指标卡和对话排行） ---------- */
const MC = ['var(--sj-c1)', 'var(--sj-c2)', 'var(--sj-c3)', 'var(--sj-c4)'];
/* T3 usageBreakdown.ts：按 token 类型分色（前景色按比例混进背景色） */
const ink = p => `color-mix(in oklab, var(--fg) ${p}%, var(--bg))`;
function shareBar(label, segs, fmt) {
  const vis = segs.filter(s => s.value > 0), tot = vis.reduce((a, s) => a + s.value, 0);
  if (tot <= 0) return '';
  return `<div class="uu-share"><h5>${esc(label)}</h5><div class="uu-sbarw" aria-hidden="true">` +
    vis.map(s => `<i style="flex:${s.value} 1 0;background:${s.color}" data-uutip="${esc(`${s.label}${s.note ? `（${s.note}）` : ''} · ${fmt(s.value)} · ${formatPercent(s.value / tot)}`)}"></i>`).join('') +
    `</div><ul class="uu-slg" aria-label="${esc(label)}">` +
    vis.map(s => `<li><i style="background:${s.color}"></i><span class="l">${esc(s.label)}</span>${s.note ? `<span class="uu-scope">${esc(s.note)}</span>` : ''}<span class="v">${esc(fmt(s.value))}</span></li>`).join('') + '</ul></div>';
}
const kpiTile = (k, v, dd, tipv) => `<div class="uu-k"><div class="k">${k}</div><div class="v"${tipv ? ` title="${esc(tipv)}"` : ''}>${v}</div><div class="d">${dd || ''}</div></div>`;
function pkCard(p) {
  const k = p.kpi || {}, id = p.id;
  const has = (+k.requests || 0) > 0 || (+k.tokens || 0) > 0;
  let h = `<div class="uu-pk" data-p="${esc(id)}" style="--uu-c:${pcolor(id)}"><div class="uu-pkh"><i class="uu-dot"></i><b>${esc(plabel(id, p.label))}</b>${scopeTag(id)}` +
    (k.sessions != null ? `<span class="s">${fmtInt(k.sessions)} 个会话</span>` : '') + '</div>';
  if (p.scanning && !has) {
    const pr = p.progress, pct = pr && +pr.totalBytes > 0 ? Math.min(99, Math.floor((+pr.bytes || 0) / pr.totalBytes * 100)) : null;
    return h + `<div class="uu-pkscan"><span class="sj-spin"></span>正在统计…${pct != null ? ` ${pct}%` : ''}</div></div>`;
  }
  const tiles = [kpiTile('请求', fmtInt(k.requests), k.turns != null ? `${fmtInt(k.turns)} 轮` : ''),
    kpiTile('Token', esc(formatTokens(k.tokens)), `输出 ${esc(formatTokens(k.output))}`, fmtInt(k.tokens))];
  if (id === 'codex') {
    if (k.weekQuota != null && isFinite(k.weekQuota)) tiles.push(kpiTile('占周额度', `约 ${(+k.weekQuota < 10 ? (+k.weekQuota).toFixed(1).replace(/\.0$/, '') : Math.round(+k.weekQuota))}%`, ''));
  } else if (k.cost != null && (id !== 'agy' || isFinite(k.cost))) {
    tiles.push(kpiTile(id === 'claude' ? '估算费用' : '费用', esc(usdK(k.cost)), k.costSource === 'providerReported' ? '' : 'API 估算', fmtUsd(k.cost)));
  }
  tiles.push(kpiTile('缓存命中', fmtPct0(k.cacheHit), k.saved != null && +k.saved > 0 ? `省下约 ${esc(usdK(k.saved))}` : ''));
  return h + `<div class="uu-kg">${tiles.join('')}</div></div>`;
}
function drawUsage(d) {
  const box = q('.uu-ubody');
  if (!box) return;
  const now = Date.now(), ps = provsOf(d), sc = scanInfo(d, ps), scan = q('.uu-scan');
  if (scan) { scan.hidden = !sc.on; scan.innerHTML = sc.on ? `<span class="sj-spin"></span>正在统计…${sc.pct != null ? ` ${sc.pct}%` : ''}` : ''; }
  const rows = normRows(d, st.range).filter(r => want(r.provider));
  const lm = labelMap(d), mlab = (pv, m) => lm[pv + ':' + m] || m || '未知模型';
  const sum = f => ps.reduce((a, p) => a + (+((p.kpi || {})[f]) || 0), 0);
  const tok = sum('tokens'), sessions = sum('sessions');
  const anyUse = tok > 0 || sum('requests') > 0 || rows.some(r => r.tokens > 0);
  let h = ps.length ? `<div class="uu-pks">${ps.map(pkCard).join('')}</div>` : '';
  if (!anyUse) {
    box.innerHTML = h + (sc.on && !ps.length ? '<div class="uu-empty"><span class="sj-spin"></span>正在统计…</div>' : '<div class="uu-empty">此时间范围内没有活动。</div>') + rankHtml(d);
    bindRank(box, d);
    return;
  }
  const metric = st.metric, single = st.prov !== 'all';
  const val = r => (metric === 'tokens' ? r.tokens : r.cost);
  // 序列：全部＝按提供方；只看一家＝按模型（前 4 个＋其他）
  const pers = periodsOf(st.range, now), cut = cutFuture(pers, now, st.range), pidx = new Map(pers.map((p, i) => [p.k, i]));
  let series;
  if (!single) {
    const ids = PIDS.filter(id => rows.some(r => r.provider === id && (r.tokens > 0 || (r.cost || 0) > 0)) || ps.some(p => p.id === id && +(p.kpi || {}).tokens > 0));
    series = ids.filter(id => metric === 'tokens' || rows.some(r => r.provider === id && r.cost != null))
      .map(id => ({id, label: plabel(id), note: isWb(id) ? WB : '', color: pcolor(id), key: r => r.provider === id}));
  } else {
    const by = new Map();
    for (const r of rows) by.set(r.model, (by.get(r.model) || 0) + (val(r) || 0));
    const top = [...by.entries()].filter(x => x[1] > 0).sort((a, b) => b[1] - a[1]).map(x => x[0]);
    series = top.slice(0, 4).map((m, i) => ({id: m, label: mlab(st.prov, m), color: MC[i], key: r => r.model === m}));
    if (top.length > 4) { const rest = new Set(top.slice(4)); series.push({id: '_other', label: '其他', color: 'var(--sj-c0)', key: r => rest.has(r.model)}); }
  }
  for (const s of series) {
    s.values = pers.map((p, i) => (cut >= 0 && i >= cut ? null : 0));
    for (const r of rows) {
      if (!s.key(r)) continue;
      const i = pidx.get(r.p);
      if (i == null || s.values[i] == null) continue;
      s.values[i] += val(r) || 0;
    }
  }
  const fmtM = metric === 'tokens' ? formatTokens : fmtUsd;
  const copt = {periods: pers, series, fmt: fmtM, tickFmt: metric === 'tokens' ? formatTokens : usdTick, area: true, total: true, cls: 'uu-uch'};
  // 左栏：总量大数字＋占比条（T3 UsageOverview）
  const cost = ps.reduce((a, p) => a + ((p.kpi || {}).cost != null ? +p.kpi.cost || 0 : 0), 0);
  const shareSegs = single ? series.map(s => ({label: s.label, color: s.color, value: s.values.reduce((a, v) => a + (v || 0), 0)}))
    : PIDS.filter(want).map(id => { const p = ps.find(x => x.id === id), k = (p && p.kpi) || {};
      return {label: plabel(id), note: isWb(id) ? WB : '', color: pcolor(id), value: metric === 'tokens' ? +k.tokens || 0 : +k.cost || 0}; });
  const unitT = st.range === 'today' ? '每小时' : '每天';
  h += '<div class="uu-ov"><div class="uu-ovl">' +
    `<div class="uu-big">${esc(metric === 'cost' ? (ps.some(p => (p.kpi || {}).cost != null) ? usdK(cost) : '—') : formatTokens(tok))}</div>` +
    `<div class="uu-bigs">${fmtInt(sessions)} 个会话${metric === 'cost' ? ' · API 估算' : ''}</div>` +
    shareBar(single ? '按模型' : '按提供方', shareSegs, fmtM) + '</div>' +
    `<div class="uu-ovr"><div class="uu-ch"><h4>${metric === 'tokens' ? `${unitT}处理的 token` : `${unitT}费用`}</h4><span class="grow"></span>` +
    `${sj.seg('umetric', [['tokens', 'Token'], ['cost', '费用']], metric, '图表指标')}</div>${chartHtml(copt)}</div></div>`;
  // 总计（T3 Totals）
  const savedAll = ps.some(p => (p.kpi || {}).saved != null) ? sum('saved') : null;
  h += '<h4 class="uu-h4">总计</h4><dl class="uu-tot">' +
    [['处理的 token', formatTokens(tok)], ['缓存的输入', formatTokens(sum('cachedInput'))], ['未缓存的输入', formatTokens(sum('uncachedInput'))],
      ['输出', formatTokens(sum('output'))], ['缓存节省', savedAll == null ? '—' : fmtUsd(savedAll)]]
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('') + '</dl>';
  h += shareBar('按类型统计的 token', [{label: '输入', value: sum('uncachedInput'), color: ink(60)}, {label: '缓存读取', value: sum('cachedInput'), color: ink(30)},
    {label: '缓存写入', value: sum('cacheWrite'), color: ink(72)}, {label: '输出', value: sum('output'), color: ink(100)}], formatTokens);
  // 明细（T3 Breakdown：模型／按时间）
  h += `<div class="uu-ch uu-brkh"><h4 class="uu-h4">明细</h4><span class="grow"></span>${sj.seg('ubrk', [['model', '模型'], ['time', st.range === 'today' ? '小时' : '天']], st.brk, '明细')}</div>` +
    (st.brk === 'model' ? modelTable(rows, ps, metric, mlab) : timeTable(rows, pers, metric));
  h += rankHtml(d);
  box.innerHTML = h;
  sj.bind(box, {
    umetric: v => { st.metric = v; sset('metric', v); drawUsage(US.last || d); },
    ubrk: v => { st.brk = v; sset('brk', v); drawUsage(US.last || d); },
  });
  wireChart(box.querySelector('.uu-uch'), copt);
  bindRank(box, d);
}
const usdTick = n => (n >= 1000 ? '$' + formatTokens(n) : n >= 10 ? '$' + Math.round(n) : '$' + trimNum(n));
const NOCOST = '按订阅计费，不折算成钱';
function modelTable(rows, ps, metric, mlab) {
  const m = new Map();
  for (const r of rows) {
    const k = r.provider + '\u0000' + r.model;
    const x = m.get(k) || {provider: r.provider, model: r.model, tokens: 0, cost: 0, priced: false};
    x.tokens += r.tokens; if (r.cost != null) { x.cost += r.cost; x.priced = true; }
    m.set(k, x);
  }
  const list = [...m.values()].filter(x => x.tokens > 0 || x.cost > 0);
  const totT = list.reduce((a, x) => a + x.tokens, 0), totC = list.reduce((a, x) => a + (x.priced ? x.cost : 0), 0);
  const v = x => (metric === 'tokens' ? x.tokens : x.priced ? x.cost : 0);
  list.sort((a, b) => v(b) - v(a) || b.tokens - a.tokens);
  const peak = list.reduce((a, x) => Math.max(a, v(x)), 0);
  if (!list.length) return '<div class="uu-empty sm">此时间范围内没有活动。</div>';
  return '<table class="uu-tbl uu-mt"><thead><tr><th class="l c-i">#</th><th class="l">模型</th><th>费用</th><th class="o1">占比</th><th>token</th></tr></thead><tbody>' +
    list.map((x, i) => {
      const sh = metric === 'tokens' ? (totT ? x.tokens / totT : null) : x.priced && totC ? x.cost / totC : null;
      const bar = v(x) > 0 && peak > 0 ? `<div class="uu-mb"><i style="width:max(0.5rem, ${(v(x) / peak * 100).toFixed(2)}%);background:${pcolor(x.provider)}"></i></div>` : '';
      return `<tr data-p="${esc(x.provider)}"><td class="l c-i">${i + 1}</td><td class="l"><span class="uu-mn" title="${esc(x.model)}"><i class="uu-dot" style="background:${pcolor(x.provider)}"${isWb(x.provider) ? ` data-uutip="${esc(plabel(x.provider))} · ${WB}"` : ''}></i>${esc(mlab(x.provider, x.model))}</span>${bar}</td>` +
        `<td>${x.priced ? esc(fmtUsd(x.cost)) : `<span class="uu-na" tabindex="0" data-uutip="${esc(x.provider === 'codex' ? NOCOST : '未定价')}">${x.provider === 'codex' ? '—' : '未定价'}</span>`}</td>` +
        `<td class="o1">${sh == null ? '' : esc(formatPercent(sh))}</td><td>${esc(formatTokens(x.tokens))}</td></tr>`;
    }).join('') + '</tbody></table>';
}
function timeTable(rows, pers, metric) {
  const ids = PIDS.filter(id => want(id) && rows.some(r => r.provider === id));
  const by = new Map();
  for (const r of rows) {
    const x = by.get(r.p) || {tokens: 0, cost: 0, pv: {}};
    x.tokens += r.tokens; x.cost += r.cost || 0;
    const y = x.pv[r.provider] || (x.pv[r.provider] = {tokens: 0, cost: 0, priced: false});
    y.tokens += r.tokens; if (r.cost != null) { y.cost += r.cost; y.priced = true; }
    by.set(r.p, x);
  }
  const list = pers.filter(p => by.has(p.k) && by.get(p.k).tokens > 0).reverse();
  if (!list.length) return '<div class="uu-empty sm">此时间范围内没有活动。</div>';
  const cell = y => (!y ? (metric === 'tokens' ? '0' : '$0.00') : metric === 'tokens' ? formatTokens(y.tokens) : y.priced ? fmtUsd(y.cost) : '—');
  return `<table class="uu-tbl uu-tt"><thead><tr><th class="l">${st.range === 'today' ? '小时' : '天'}</th>${ids.map(id => `<th class="o1"${isWb(id) ? ` data-uutip="${WB}"` : ''}>${esc(plabel(id))}</th>`).join('')}` +
    `<th>总计</th><th class="o2">${metric === 'tokens' ? '费用' : 'token'}</th></tr></thead><tbody>` +
    list.map(p => { const x = by.get(p.k);
      return `<tr><td class="l">${esc(p.tip)}</td>${ids.map(id => `<td class="o1">${esc(cell(x.pv[id]))}</td>`).join('')}` +
        `<td class="strong">${esc(metric === 'tokens' ? formatTokens(x.tokens) : fmtUsd(x.cost))}</td><td class="o2">${esc(metric === 'tokens' ? fmtUsd(x.cost) : formatTokens(x.tokens))}</td></tr>`; }).join('') +
    '</tbody></table>';
}
/* 对话排行（第十四轮“用量最多的对话”的样子）；有委派记录时可勾“子任务算到父对话头上” */
function rankHtml(d) {
  let list = Array.isArray(d.ranking) ? d.ranking : (d.sessions || []).map(s => Object.assign({provider: 'claude'}, s));
  list = list.filter(r => r && want(r.provider || 'claude')).slice(0, 10);
  const roll = d.hasTasks ? `<label class="uu-roll"><input type="checkbox" data-a="rollup"${st.rollup ? ' checked' : ''}>子任务算到父对话头上</label>` : '';
  if (!list.length && !roll) return '';
  const wbs = [...new Set(list.map(r => r.provider || 'claude').filter(isWb))];
  const note = wbs.length ? `<span class="uu-scope">${esc(st.prov === 'all' ? wbs.map(id => plabel(id)).join('、') + ' ' + WB : WB)}</span>` : '';
  return `<div class="uu-ch uu-rkh"><h4 class="uu-h4">对话排行</h4>${note}<span class="grow"></span>${roll}</div><div class="sj-card sj-sess uu-rank">` +
    (list.length ? list.map(r => {
      const title = r.title || '未命名对话', pv = r.provider || 'claude';
      const nm = r.sid ? `<button type="button" class="sj-link" data-sid="${esc(r.sid)}" title="${esc(title)}">${esc(title)}</button>` : `<span class="uu-tt" title="${esc(title)}">${esc(title)}</span>`;
      return `<div class="sr" data-p="${esc(pv)}"><span class="uu-rt"><i class="uu-dot" style="background:${pcolor(pv)}"${isWb(pv) ? ` data-uutip="${esc(plabel(pv))} · ${WB}"` : ''}></i>${nm}${r.children ? `<span class="sj-tag">含 ${fmtInt(r.children)} 个子任务</span>` : ''}</span>` +
        `<span class="n">${fmtInt(r.requests)} 次</span><span class="n">${esc(formatTokens(r.tokens))}</span>` +
        `<span class="n c">${r.cost == null ? `<span class="uu-na" tabindex="0" data-uutip="${esc(pv === 'codex' ? NOCOST : '未定价')}">—</span>` : esc(fmtUsd(r.cost))}</span></div>`;
    }).join('') : '<div class="uu-empty sm">此时间范围内没有活动。</div>') + '</div>';
}
function bindRank(box, d) {
  wireLinks(box);
  const cb = box.querySelector('input[data-a=rollup]');
  if (cb) cb.onchange = () => { st.rollup = cb.checked; sset('rollup', st.rollup); const g = sj.gen(); loadUsage(g); };
}

/* ================= 接入设置面板：“用量”改名“额度与速度”，分区键仍是 usage ================= */
const ent = sj.SECS.find(([k]) => k === 'usage');
if (ent) ent[1] = '额度与速度';
const navBtn = document.querySelector('#sjSet .sj-nav [data-s="usage"]');
if (navBtn) navBtn.textContent = '额度与速度';
sj.RENDER.usage = render;
C.uu = {state: st, quota: QS, last: () => US.last, render, remainingPercent, paceRemainingPercent, paceOf, formatResetsIn, formatTokens};
})();
