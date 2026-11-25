/* 移植自 T3 Code apps/web/src/components/chat/ChildThreadApprovals.tsx 等（MIT，Copyright (c) 2026 T3 Tools Inc.；文件清单见下方注释） */
(() => {
'use strict';
/* 第十七轮 M：多智能体协作（委派子任务）的界面。后台接口见 collab.py。
   界面布局和文案移植自 T3 Code（MIT，Copyright (c) 2026 T3 Tools Inc.）：apps/web/src/components/chat/ChildThreadApprovals.tsx、
   TimelineWorkEntryRow.tsx（“打开子智能体”）、ThreadRelationshipsControl.tsx（“打开父会话”），以及 apps/web/src/i18n/translations/zh.json
   里的“委派子任务 / 获取委派任务状态 / 取消委派任务”“{model} 子智能体：{status}”“来自 {title} 的审批”“停止子智能体”等。
   1. 设置 →“协作”：总开关、能派给谁、子任务权限、同时进行的上限、最长时间、完成后自动交回。
   2. 父对话：工具位置的委派卡（手动复核挂在那一轮结束行后面）；子任务等你确认时，输入框上方列出它们的审批（用户点）。
   3. 结束行“让 Codex 复核这轮改动”（不受协作开关控制）。
   4. 左栏：子对话缩进挂在父对话下（↳＋提供方），父对话有子任务在跑时显示个数。子对话顶部“由《父对话》委派”。
   5. 右栏“任务”加“委派”分组；授权卡上写“Claude 想让 Codex 做：…”；删除、归档父对话前提示会一起停掉子任务。
   钩子都串在原有处理之后（chain），不改已有文件的逻辑。 */
const C = window.CWB;
if (!C) return;
const {S, $, esc, api, toast} = C;
const chain = (name, fn) => { const prev = C[name]; C[name] = (...a) => { try { if (prev) prev(...a); } finally { try { fn(...a); } catch (e) { console.error(e); } } }; };
const mk = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const LABEL = {claude: 'Claude', codex: 'Codex', agy: 'Antigravity'};
const TERM = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
const W = {tasks: new Map(), child: null, parentTitle: '', provs: null, provT: 0, perms: new Map(), sid: null};
async function call(n, b) { try { return await api(n, b || {}); } catch (e) { return {error: (e && e.message) || '操作没有成功'}; } }
const fmtDur = ms => { const s = Math.max(0, Math.round(ms / 1000)); return s < 60 ? s + ' 秒' : s < 3600 ? Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒' : Math.floor(s / 3600) + ' 小时 ' + Math.floor(s % 3600 / 60) + ' 分'; };
const fmtTok = n => (C.fmtTok ? C.fmtTok(n) : String(n));
const provOfCur = () => { const s = S.cur && C.findSession(S.cur); return (s && s.prov) || (document.body.dataset.pv) || 'claude'; };

/* ================= 数据 ================= */
function take(d) {
  if (!d || !d.cwb || d.sid !== S.cur) return;
  W.sid = d.sid;
  W.tasks = new Map((d.cwb.tasks || []).map(t => [t.id, t]));
  W.child = d.cwb.task || null; W.parentTitle = d.cwb.parentTitle || '';
  W.perms = new Map((d.cwb.childPerms || []).map(p => [p.rid, p]));
}
async function refresh() {
  const sid = S.cur;
  if (!sid) return;
  const d = await call('collab-tasks', {sid});
  if (d.error || S.cur !== sid) return;
  take({sid, cwb: d});
  decoAll();
}
async function provs(force) {
  if (!force && W.provs && Date.now() - W.provT < 60000) return W.provs;
  const d = await call('collab-prefs');
  if (!d.error) { W.provs = d.provs || []; W.provT = Date.now(); W.prefs = d.prefs; }
  return W.provs || [];
}

/* ================= 委派卡 ================= */
const IC = {open: '<svg class="h-i" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5H3.5v9h9V10M9 3h4v4M13 3 7.5 8.5"/></svg>',
  stop: '<svg class="h-i" viewBox="0 0 16 16" aria-hidden="true"><rect x="4.5" y="4.5" width="7" height="7" rx="1.2"/></svg>'};
function meta(t) {
  const u = t.usage || {}, parts = [t.modeLabel];
  const tin = (u.input || 0) + (u.cachedInput || 0) * 0 + (u.cacheRead || 0) + (u.cacheWrite || 0);
  if (tin || u.output) parts.push(`输入 ${fmtTok(tin)} · 输出 ${fmtTok(u.output || 0)}`);
  if (typeof u.cost === 'number' && u.cost > 0) parts.push('$' + u.cost.toFixed(u.cost < 0.1 ? 4 : 2));
  if (t.quotaUnknown) parts.push('额度未知');
  return parts.filter(Boolean).join(' · ');
}
function stCls(t) { return t.status === 'completed' ? 'ok' : t.status === 'failed' ? 'err' : TERM.has(t.status) ? 'end' : t.status === 'waiting' ? 'att' : 'run'; }
function cardHtml(t) {
  const who = t.childLabel + (t.model ? ' · ' + t.model : '');
  const pend = t.delivery === 'pending' && TERM.has(t.status) && !(S.runner && S.runner.busy);
  return `<div class="w-ch"><span class="w-pv w-pv-${esc(t.childProv)}">${esc(t.childLabel.slice(0, 1))}</span>` +
    `<b class="w-who">交给 ${esc(who)}</b><span class="w-st w-${stCls(t)}">${esc(t.state)}</span><span class="w-time"></span></div>` +
    `<div class="w-ct">${esc(t.title)}</div><div class="w-cm">${esc(meta(t))}</div>` +
    '<div class="w-cb">' +
    `<button type="button" class="w-btn" data-a="open">${IC.open}<span>打开子智能体</span></button>` +
    (TERM.has(t.status) ? '' : `<button type="button" class="w-btn" data-a="stop">${IC.stop}<span>停止子智能体</span></button>`) +
    (TERM.has(t.status) && t.summary ? '<button type="button" class="w-btn" data-a="res">查看结果</button>' : '') +
    (pend ? `<button type="button" class="w-btn w-pri" data-a="deliver">把结果交给 ${esc(t.parentLabel)}</button>` : '') +
    (TERM.has(t.status) && t.delivery === 'acknowledged' ? '<span class="w-done">结果已交回</span>' : '') +
    '</div><div class="w-res md" hidden></div>';
}
function fillCard(card, t) {
  const sig = [t.status, t.state, t.delivery, t.model, (t.summary || '').length, JSON.stringify(t.usage || {}), !!(S.runner && S.runner.busy)].join('|');
  card.dataset.task = t.id;
  if (card._sig === sig) return tickCard(card, t);
  const open = card.querySelector('.w-res') && !card.querySelector('.w-res').hidden;
  card._sig = sig;
  card.className = 'w-card w-' + stCls(t) + (t.origin === 'manual' ? ' w-manual' : '');
  card.innerHTML = cardHtml(t);
  card.querySelectorAll('[data-a]').forEach(b => b.onclick = e => { e.stopPropagation(); act(b.dataset.a, t.id, card); });
  if (open) showRes(card, t);
  tickCard(card, t);
}
function tickCard(card, t) {
  const el = card.querySelector('.w-time');
  if (!el) return;
  const a = t.startedAt || t.createdAt, b = t.endedAt || Date.now();
  const v = a ? fmtDur(b - a) : '';
  if (el.textContent !== v) el.textContent = v;
}
function showRes(card, t) {
  const r = card.querySelector('.w-res');
  r.innerHTML = C.md ? C.md((t.summary || '') + (t.more ? '\n\n…（只显示前 2 万字，全文在子对话里）' : '')) : esc(t.summary || '');
  if (C.enhance) C.enhance(r);
  r.hidden = false;
  const b = card.querySelector('[data-a="res"] span') || card.querySelector('[data-a="res"]');
  if (b) b.textContent = '收起结果';
}
async function act(a, id, card) {
  const t = W.tasks.get(id) || (W.child && W.child.id === id ? W.child : null);
  if (!t) return;
  if (a === 'open') return C.openSession(t.child);
  if (a === 'res') {
    const r = card.querySelector('.w-res');
    if (!r.hidden) { r.hidden = true; card.querySelector('[data-a="res"]').textContent = '查看结果'; return; }
    return showRes(card, t);
  }
  if (a === 'stop') {
    const d = await call('collab-stop', {id});
    if (d.error) toast('无法停止子智能体：' + d.error); else upd(d.task);
    return;
  }
  if (a === 'deliver') {
    const d = await call('collab-deliver', {id});
    if (d.error) toast(d.error); else { upd(d.task); toast('已把结果交给 ' + t.parentLabel); }
  }
}
function upd(t) {
  if (!t) return;
  if (t.parent === S.cur) W.tasks.set(t.id, t);
  if (W.child && W.child.id === t.id) W.child = t;
  decoAll();
}
/* 工具行：委派子任务 / 获取委派任务状态 / 取消委派任务（T3 的中文说法），委派的那一行下面挂委派卡 */
const TOOL_ZH = {
  delegate_task: {run: '正在委派子任务', ok: '已委派子任务', err: '委派子任务失败'},
  task_status: {run: '正在获取委派任务状态', ok: '已获取委派任务状态', err: '获取委派任务状态失败'},
  task_cancel: {run: '正在取消委派任务', ok: '已请求取消委派任务', err: '取消委派任务失败'},
  orchestrator_capabilities: {run: '正在查看能委派给谁', ok: '已查看能委派给谁', err: '查看能委派给谁失败'},
};
function taskIdOf(it) {
  const m = /"taskId"\s*:\s*"(task_[0-9a-f]+)"/.exec(String(it.out || ''));
  return m ? m[1] : '';
}
chain('onTool', (el, it) => {
  const zh = TOOL_ZH[it.name];
  if (!zh || !el) return;
  const isCwb = it.name !== 'delegate_task' || /cwb|taskId|"task"/.test(String(it.desc || '') + String(it.in || '') + String(it.out || ''));
  if (!isCwb) return;
  el.classList.add('w-tool');
  const h = el.querySelector('.head');
  const tn = h && [...h.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());  // 前面可能有图标
  if (tn) tn.textContent = it.done ? (it.error ? zh.err : zh.ok) : zh.run;
  if (it.name !== 'delegate_task') return;
  const id = taskIdOf(it);
  el.dataset.cwbTask = id || '';
  const t = id && W.tasks.get(id);
  let card = el.querySelector(':scope > .w-card');
  if (!t) { if (card) card.remove(); return; }
  if (!card) { card = mk('div', 'w-card'); el.appendChild(card); }
  fillCard(card, t);
});
/* 手动复核的卡挂在那一轮结束行后面；工具行重画时补回卡片 */
function endRows() {
  const out = [];
  for (const it of S.byId ? S.byId.values() : []) { const t = it._turn; if (t && t.it === it && t.el && t.el.isConnected) out.push([t, t.el]); }
  const sp = S.spinEl;
  if (S.turn && sp && sp.isConnected && sp.classList.contains('done')) out.push([S.turn, sp]);
  return out;
}
function decoCards() {
  const feed = $('#feed');
  if (!feed) return;
  feed.querySelectorAll('.it.tool.w-tool[data-cwb-task]').forEach(el => {
    const t = W.tasks.get(el.dataset.cwbTask);
    let card = el.querySelector(':scope > .w-card');
    if (t) { if (!card) { card = mk('div', 'w-card'); el.appendChild(card); } fillCard(card, t); }
  });
  const ends = endRows();
  const seen = new Set();
  for (const t of W.tasks.values()) {
    if (t.origin !== 'manual' || !t.anchor) continue;
    const row = ends.find(([tt]) => tt.it && (tt.it.uuid === t.anchor || tt.it.id === t.anchor || (tt.it.fork && tt.it.fork === t.anchor)))
      || ends.find(([, el]) => el.querySelector(`.k-chg[data-u="${CSS.escape(t.anchor)}"]`));
    if (!row) continue;
    let card = feed.querySelector(`.w-card.w-manual[data-task="${CSS.escape(t.id)}"]`);
    if (!card) { card = mk('div', 'w-card w-manual'); card.dataset.task = t.id; }
    let host = row[1].nextElementSibling;
    let last = row[1];
    while (host && host.classList.contains('w-card')) { last = host; host = host.nextElementSibling; }
    if (card.parentNode !== feed || (card !== last && card.previousElementSibling !== row[1] && !card.previousElementSibling?.classList.contains('w-card'))) last.after(card);
    fillCard(card, t);
    seen.add(t.id);
  }
  feed.querySelectorAll('.w-card.w-manual').forEach(c => { if (!seen.has(c.dataset.task)) c.remove(); });
}
setInterval(() => {
  document.querySelectorAll('.w-card[data-task]').forEach(c => { const t = W.tasks.get(c.dataset.task) || (W.child && W.child.id === c.dataset.task ? W.child : null); if (t && !TERM.has(t.status)) tickCard(c, t); });
}, 1000);

/* ================= 结束行：让 Codex 复核这轮改动 ================= */
const REVIEW_DEF = {claude: 'codex', codex: 'claude', agy: 'claude'};
const CHANGE_TOOLS = new Set(['Edit', 'Write', '删除文件', 'MultiEdit', 'NotebookEdit']);
function pvTurnsWithChanges() {
  const out = new Set();
  let cur = null;
  for (const it of S.byId ? S.byId.values() : []) {
    if ((it.k === 'user' && !it.mid) || (it.k === 'sys' && it.turn)) cur = it;
    else if (cur && it.k === 'tool' && CHANGE_TOOLS.has(it.name) && it.path && !it.error) out.add(cur.id);
  }
  return out;
}
async function decoReview() {
  if (!S.cur || W.child) { document.querySelectorAll('.w-rev').forEach(b => b.remove()); return; }
  const me = provOfCur();
  const ps = (await provs()).filter(p => p.installed && p.ok && p.id !== me);
  const ends = endRows();
  const pvCh = me === 'claude' ? null : pvTurnsWithChanges();
  for (const [t, el] of ends) {
    let anchor = null, key = null;
    if (me === 'claude') { const ch = el.querySelector('.k-chg[data-u]'); if (ch) { anchor = ch.dataset.u; key = 'uuid'; } }
    else if (t.it && pvCh.has(t.it.id)) { anchor = t.it.id; key = 'uid'; }
    let g = el.querySelector(':scope > .k-end > .w-rev, :scope > .w-rev');
    if (!anchor || !ps.length) { if (g) g.remove(); continue; }
    const def = ps.find(p => p.id === REVIEW_DEF[me]) || ps[0];
    const sig = anchor + '|' + ps.map(p => p.id).join(',');
    if (g && g._sig === sig) continue;
    if (!g) g = mk('span', 'w-rev');
    g._sig = sig;
    g.innerHTML = `<button type="button" class="w-rbtn" data-p="${esc(def.id)}">让 ${esc(def.label)} 复核这轮改动</button>` +
      (ps.length > 1 ? '<button type="button" class="w-rdd" aria-haspopup="menu" title="换一家">▾</button>' : '');
    const go = async prov => {
      const b = g.querySelector('.w-rbtn'); b.disabled = true;
      const d = await call('collab-review', {sid: S.cur, prov, [key]: anchor});
      b.disabled = false;
      if (d.error) { toast(d.error); return; }
      upd(d.task);
      toast(`已交给 ${LABEL[prov]} 复核，完成后在这里看结果`);
    };
    g.querySelector('.w-rbtn').onclick = e => { e.stopPropagation(); go(def.id); };
    const dd = g.querySelector('.w-rdd');
    if (dd) dd.onclick = e => { e.stopPropagation(); C.menu(dd, ps.map(p => ({label: `让 ${p.label} 复核`, fn: () => go(p.id)}))); };
    const kend = el.querySelector(':scope > .k-end');
    if (kend) kend.appendChild(g); else el.appendChild(g);  // 放进结束行那组按钮的最后（ux-k 要求 .k-end 在结束行最后）
  }
}
/* “改动 N 个文件”是 ux-k 读完 turn-changes 后才补上的（没有事件），盯着结束行的变化补按钮 */
try {
  let mt = 0;
  new MutationObserver(recs => {
    const hit = recs.some(r => r.target.nodeType === 1 && (r.target.classList.contains('k-end') ||
      [...r.addedNodes].some(n => n.nodeType === 1 && n.classList.contains('k-end'))));
    if (hit) { clearTimeout(mt); mt = setTimeout(decoReview, 120); }
  }).observe($('#feed'), {childList: true, subtree: true});
} catch {}

/* ================= 父对话里子任务的审批（T3 ChildThreadApprovals） ================= */
function renderPerms() {
  const dock = $('#dock'), comp = $('#composer');
  let box = $('#wPerms');
  const list = [...W.perms.values()];
  if (!S.cur || !list.length || W.child) { if (box) box.remove(); return; }
  if (!box) { box = mk('div', 'w-perms'); box.id = 'wPerms'; dock.insertBefore(box, comp); }
  const sig = list.map(p => p.rid).join(',');
  if (box._sig === sig) return;
  box._sig = sig;
  box.innerHTML = list.map(p => {
    const t = [...W.tasks.values()].find(x => x.child === p.sid);
    const title = t ? t.title : '子智能体';
    const inp = p.input || {};
    const what = p.tool === 'Bash' ? (inp.command || '') : p.tool === 'Edit' || p.tool === 'Write' ? (inp.file_path || '') : (inp.message || JSON.stringify(inp).slice(0, 300));
    return `<section class="w-ap" data-rid="${esc(p.rid)}" aria-label="来自 ${esc(title)} 的审批"><p class="w-aph">来自《${esc(title)}》的审批 · ${esc(p.tool)}</p>` +
      `<pre class="w-apd">${esc(String(what).slice(0, 1200))}</pre><div class="w-apb"><button type="button" class="btn small" data-d="deny">拒绝</button>` +
      `<button type="button" class="btn small primary" data-d="allow">允许</button><button type="button" class="btn small" data-d="open">打开子智能体</button></div></section>`;
  }).join('');
  box.querySelectorAll('.w-ap').forEach(sec => sec.querySelectorAll('[data-d]').forEach(b => b.onclick = async () => {
    const rid = sec.dataset.rid, p = W.perms.get(rid);
    if (b.dataset.d === 'open') { if (p) C.openSession(p.sid); return; }
    const d = await call('perm', {rid, decision: b.dataset.d});
    if (d.error) toast(d.error);
    W.perms.delete(rid); renderPerms();
  }));
}

/* ================= 子对话顶部：由《父对话》委派 ================= */
function renderBanner() {
  let b = $('#wBanner');
  const t = W.child;
  if (!S.cur || !t) { if (b) b.remove(); return; }
  if (!b) { b = mk('div', 'w-banner'); b.id = 'wBanner'; $('#scroll').before(b); }
  const sig = t.id + '|' + t.state + '|' + W.parentTitle;
  if (b._sig === sig) return;
  b._sig = sig;
  b.innerHTML = `<span class="w-bt">↳ 由《<button type="button" class="link w-bp" title="打开父会话：${esc(W.parentTitle)}">${esc(W.parentTitle || '父对话')}</button>》委派</span>` +
    `<span class="w-st w-${stCls(t)}">${esc(t.state)}</span>` + (TERM.has(t.status) ? '<span class="w-bn">之后在这里接着聊，不再回报父对话</span>' : '');
  b.querySelector('.w-bp').onclick = () => C.openSession(t.parent);
}

/* ================= 左栏：子对话挂在父对话下面 ================= */
function decoSide() {
  const box = $('#projects');
  if (!box) return;
  box.querySelectorAll('.srow[data-sid]').forEach(row => {
    const s = C.findSession(row.dataset.sid);
    if (!s) return;
    const st = row.querySelector('.stitle');
    if (s.cwbParent) {
      row.classList.add('w-kid');
      if (st && !st.querySelector('.w-arrow')) st.insertAdjacentHTML('afterbegin', '<span class="w-arrow">↳</span>');
      const prov = s.prov || 'claude';
      const own = row.querySelector('.pv-tag, .v-ptag');  // ux-v 的标识和 .stitle 平级（复核 M-4）：有了就不再加，免得显示两遍
      if (own) row.querySelectorAll('.w-kpv').forEach(x => x.remove());
      else if (st && !row.querySelector('.w-kpv')) st.insertAdjacentHTML('beforeend', `<span class="w-kpv">${esc(LABEL[prov] || prov)}</span>`);
      const grp = row.parentNode, parent = grp && grp.querySelector(`.srow[data-sid="${CSS.escape(s.cwbParent)}"]`);
      if (parent && parent !== row) {
        let at = parent;
        while (at.nextElementSibling && at.nextElementSibling.classList.contains('w-kid') && at.nextElementSibling !== row) at = at.nextElementSibling;
        if (at.nextElementSibling !== row) at.after(row);
      }
    }
    let cnt = row.querySelector('.w-cnt');
    if (s.cwbKids) {
      if (!cnt) { cnt = mk('span', 'w-cnt'); const tm = row.querySelector('.stime'); if (tm) tm.before(cnt); else row.appendChild(cnt); }
      cnt.textContent = s.cwbKids; cnt.title = `${s.cwbKids} 个子任务在跑`;
    } else if (cnt) cnt.remove();
  });
  foldSide(box);
}
chain('onSide', decoSide);

/* 11-24：子对话默认收起（同左栏“更早”分组的规矩）。父对话行上的“› N”点开/收起，展开过的记在本机；
   正在看的那条子对话不收；左栏搜索或按状态筛选时全部展开。收起后子任务在跑、等你处理，按钮跟着变色。
   挂在父对话下的子对话才收（父对话在别的分组时子对话照旧单独显示）；有了按钮就不再单独显示“在跑个数”。 */
const FOLD_KEY = 'cwb-kids-open';
const opened = (() => { try { return new Set(JSON.parse(localStorage.getItem(FOLD_KEY) || '[]')); } catch { return new Set(); } })();
function saveOpened() { try { localStorage.setItem(FOLD_KEY, JSON.stringify([...opened].slice(-200))); } catch {} }
function foldSide(box) {
  const searching = !!(S.filter || '').trim() || !!C.sideFilter;
  box.querySelectorAll('.srow[data-sid]:not(.w-kid)').forEach(row => {
    const sid = row.dataset.sid, fam = new Set([sid]), kids = [];
    for (let n = row.nextElementSibling; n && n.classList.contains('w-kid'); n = n.nextElementSibling) {
      const k = C.findSession(n.dataset.sid);
      if (!k || !fam.has(k.cwbParent)) break;
      fam.add(n.dataset.sid); kids.push([n, k]);
    }
    let btn = row.querySelector('.w-fold');
    if (!kids.length) { if (btn) btn.remove(); return; }
    const s = C.findSession(sid) || {};
    const open = searching || opened.has(sid);
    kids.forEach(([n]) => n.classList.toggle('w-hide', !open && n.dataset.sid !== S.cur));
    const run = (s.cwbKids || 0) > 0 || kids.some(([, k]) => k.busy);
    const att = kids.some(([, k]) => k.attention);
    const cnt = row.querySelector('.w-cnt');
    if (cnt) cnt.remove();
    if (!btn) {
      btn = mk('button', 'w-fold'); btn.type = 'button';
      const tm = row.querySelector('.stime'); if (tm) tm.before(btn); else row.appendChild(btn);
      btn.onclick = e => {
        e.stopPropagation();
        if (opened.has(sid)) opened.delete(sid); else opened.add(sid);
        saveOpened(); foldSide(box);
      };
    }
    btn.className = 'w-fold' + (open ? ' w-open' : '') + (att ? ' w-att' : run ? ' w-run' : '');
    btn.setAttribute('aria-expanded', String(open));
    btn.innerHTML = '<svg class="w-chev" viewBox="0 0 12 12" aria-hidden="true"><path d="M4.5 2.75 7.75 6 4.5 9.25"/></svg>' + kids.length;
    btn.title = `${kids.length} 个子对话` + (s.cwbKids ? `，${s.cwbKids} 个在跑` : '') + (att ? '，有等你处理的' : '') +
      (searching ? '' : ' · 点击' + (open ? '收起' : '展开'));
  });
}

/* ================= 右栏“任务”：委派分组 ================= */
function renderPaneGroup() {
  const rb = $('#rbody');
  if (!rb) return;
  const list = rb.querySelector('.aglist');
  let g = rb.querySelector('.w-dlg');
  const ts = [...W.tasks.values()].sort((a, b) => (TERM.has(a.status) - TERM.has(b.status)) || b.createdAt - a.createdAt);
  if (!list || !S.cur || !ts.length) { if (g) g.remove(); return; }
  if (!g) { g = mk('div', 'w-dlg'); list.after(g); }
  const sig = ts.map(t => [t.id, t.status, t.delivery].join(':')).join(',');
  if (g._sig === sig) return;
  g._sig = sig;
  g.innerHTML = '<div class="w-gh">委派</div>' + ts.map(t => `<div class="ag w-ag" data-task="${esc(t.id)}"><div class="ag-h"><span class="sdot ${TERM.has(t.status) ? (t.status === 'completed' ? 'on' : 'att') : 'busy'}"></span>` +
    `<b class="ag-t">${esc(t.title)}</b><span class="ag-s">${esc(t.state)}</span></div><div class="ag-m">${esc(t.childLabel + (t.model ? ' · ' + t.model : '') + ' · ' + meta(t))}</div>` +
    '<div class="ag-b"><button class="link" data-a="open">打开子智能体</button>' + (TERM.has(t.status) ? '' : '<button class="link" data-a="stop">停止子智能体</button>') + '</div></div>').join('');
  g.querySelectorAll('.w-ag').forEach(el => el.querySelectorAll('[data-a]').forEach(b => b.onclick = e => { e.stopPropagation(); act(b.dataset.a, el.dataset.task, el); }));
}
try {
  const rb = $('#rbody');
  if (rb) new MutationObserver(() => { const g = rb.querySelector('.w-dlg'), l = rb.querySelector('.aglist'); if (l && (!g || g.previousElementSibling !== l)) { if (g) g.remove(); renderPaneGroup(); } }).observe(rb, {childList: true});
} catch {}

/* ================= 授权卡：Claude 想让 Codex 做：… ================= */
function decoPermCard() {
  const card = $('#card');
  if (!card || $('#modal').hidden || !String(S.modalKey || '').startsWith('p:')) return;
  const p = S.perms && S.perms.get(S.modalKey.slice(2));
  if (!p || !/(^|__)delegate_task$/.test(p.tool || '') || !p.input || !p.input._cwb_preview || card.dataset.wDone === p.rid) return;
  const v = p.input._cwb_preview;
  card.dataset.wDone = p.rid;
  const h = card.querySelector('h3');
  if (h) h.textContent = `${v.who} 想让 ${v.to} 做：`;
  const pre = card.querySelector('pre.blk');
  const html = `<div class="w-pc">${v.title ? `<div class="w-pct">${esc(v.title)}</div>` : ''}<div class="w-pcx">${esc(v.task)}${v.task.length >= 200 ? '…' : ''}</div>` +
    `<div class="w-pcm">模型 ${esc(v.model || '默认')} · 权限 ${esc(v.mode)}</div></div>`;
  if (pre) pre.outerHTML = html;
  else if (h) h.insertAdjacentHTML('afterend', html);
}
try { new MutationObserver(decoPermCard).observe($('#card'), {childList: true}); } catch {}

/* ================= 删除、归档父对话前提示 ================= */
function kidsOf(list) { return (list || []).filter(Boolean).reduce((n, s) => n + (s.cwbKids || 0), 0); }
for (const name of ['undoArchive', 'undoDelete']) {
  const prev = C[name];
  if (!prev) continue;
  C[name] = (list, ...rest) => {
    const on = name === 'undoDelete' || rest[0] !== false;
    const n = on ? kidsOf(list) : 0;
    if (n && !confirm(`还有 ${n} 个子任务在跑，会一起停掉。`)) return;  // 真正归档、删除成功后由后台连带停掉（复核 M-9）
    return prev(list, ...rest);
  };
}

/* ================= 设置 →“协作” ================= */
const sj = C.sj;
if (sj) {
  if (!sj.SECS.some(([k]) => k === 'collab')) {
    const i = sj.SECS.findIndex(([k]) => k === 'persona' || k === 'chat');
    sj.SECS.splice(i < 0 ? sj.SECS.length : i + 1, 0, ['collab', '协作']);
  }
  const live = g => g === sj.gen() && sj.body() && sj.body().dataset.sec === 'collab';
  const PERM = [['same', '和父对话一样'], ['auto-accept-edits', '自动改文件'], ['approval-required', '每步确认']];
  async function render() {
    const b = sj.body(), g = sj.gen();
    b.innerHTML = sj.head('协作', '让对话里的智能体把子任务交给 Claude、Codex 或 Antigravity') + '<div class="w-set"><div class="sj-empty">读取中…</div></div>';
    draw(g);
  }
  async function draw(g, data) {
    const d = data || await call('collab-prefs', {refresh: true});
    if (!live(g)) return;
    const box = sj.body().querySelector('.w-set');
    if (d.error) { box.innerHTML = sj.errBox('读取失败：' + d.error, true); sj.bind(box, {}, {retry: () => draw(g)}); return; }
    const p = d.prefs, ps = d.provs || [];
    W.provs = ps; W.provT = Date.now(); W.prefs = p;
    const num = (k, v, lo, hi, unit) => `<span class="w-num"><input type="number" class="sj-in w-in" data-n="${k}" min="${lo}" max="${hi}" value="${v}">${unit ? `<span>${unit}</span>` : ''}</span>`;
    box.innerHTML = '<div class="sj-grp">' + sj.row('协作', sj.sw('on', p.on, false, '协作'), '打开后，新开或重启的对话可以委派子任务；关着时和平常完全一样') + '</div>' +
      '<div class="w-gh2">能派给谁</div><div class="sj-grp">' + ps.map(x => sj.row(esc(x.label), sj.sw('pv_' + x.id, p.provs[x.id] && x.installed && x.ok, !(x.installed && x.ok), x.label),
        x.installed && x.ok ? '' : esc(x.hint || '不能用'))).join('') + '</div>' +
      '<div class="w-gh2">子任务</div><div class="sj-grp">' +
      sj.row('权限', sj.seg('perm', PERM, p.perm, '子任务权限'), '永远不高于父对话') +
      sj.row('同时进行', `<span class="w-nums">每个对话 ${num('maxPer', p.maxPer, 1, 10, '个')}全部 ${num('maxAll', p.maxAll, 1, 20, '个')}</span>`) +
      sj.row('最长时间', num('maxMins', p.maxMins, 1, 240, '分钟'), '到时自动停') +
      sj.row('完成后自动把结果交回父对话', sj.sw('autoReturn', p.autoReturn, false, '完成后自动交回'), '关掉后在委派卡上点“把结果交给…”') + '</div>';
    const save = async set => {
      const x = await call('collab-prefs', {set});
      if (!live(g)) return;
      if (x.error) { toast(x.error); draw(g); return; }
      draw(g, x);
    };
    const map = {on: v => save({on: v}), autoReturn: v => save({autoReturn: v}), perm: v => save({perm: v})};
    ps.forEach(x => { map['pv_' + x.id] = v => save({provs: {[x.id]: v}}); });
    sj.bind(box, map);
    box.querySelectorAll('input[data-n]').forEach(inp => inp.onchange = () => save({[inp.dataset.n]: inp.value}));
  }
  sj.RENDER.collab = render;
  const nav = document.querySelector('#sjSet .sj-nav');
  if (nav && !nav.querySelector('[data-s="collab"]')) {
    const nb = document.createElement('button'); nb.type = 'button'; nb.dataset.s = 'collab'; nb.textContent = '协作';
    const ref = nav.querySelector('[data-s="persona"]') || nav.querySelector('[data-s="chat"]'); if (ref) ref.after(nb); else nav.appendChild(nb);
  }
}

/* ================= 串接 ================= */
function decoAll() { decoCards(); renderBanner(); renderPerms(); renderPaneGroup(); decoReview(); }
chain('onLoad', d => { take(d); if (!d || !d.cwb) { W.tasks = new Map(); W.child = null; W.perms = new Map(); } decoAll(); });
let rt = 0;
const later = () => { clearTimeout(rt); rt = setTimeout(decoAll, 150); };
chain('onEvent', ev => {
  if (!ev) return;
  if (ev.type === 'cwb-task') {
    const t = ev.task;
    if (t.parent === S.cur) { W.tasks.set(t.id, t); later(); }
    else if (t.child === S.cur) { W.child = t; later(); }
    return;
  }
  if (ev.type === 'perm' && ev.req && [...W.tasks.values()].some(t => t.child === ev.req.sid)) { W.perms.set(ev.req.rid, ev.req); renderPerms(); return; }
  if (ev.type === 'perm-done' && W.perms.has(ev.rid)) { W.perms.delete(ev.rid); renderPerms(); return; }
  if (ev.type === 'items' && ev.sid === S.cur) later();
  if (ev.type === 'runner' && ev.sid === S.cur) later();
  if (ev.type === 'collab-prefs') W.provT = 0;
});
chain('onPrepend', () => later());
})();
