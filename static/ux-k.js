(() => {
'use strict';
/* 第十五轮 F1：时间线与输入框
   1. 每轮结束行：改动胶囊（turn-changes，点开审阅“这一轮”）+ 带下拉的“从这里分叉”（分叉 / 分叉并回退代码，最后一轮只有分叉）
   2. 用户消息悬停操作条：复制、回退代码 → 回退预览 → 时间线里的回退行（10 秒内可撤销）；重新打开时按 rewinds 还原
   3. 计划卡：带计划全文和选项的 ExitPlanMode 授权画在时间线里（app.js 的 showModal 用 CWB.inlinePerm 跳过它）
   4. 授权弹窗“拒绝并说明”
   5. 输入框：上下文环与小卡（缓存倒计时、费用、压缩带说明、查看明细）、命令拦截提示、添加文件夹、出错横幅
   6. 时间线“简洁 / 详细”两档：CWB.applyDensity()（读 pref.density）
   7. 压缩：compact 事件 → “正在压缩上下文…” / “已压缩上下文”（可展开看摘要）
   接口一律容错：不存在或返回 {error} 时对应功能不显示，不影响发送、时间线、授权。
   接入：钩子都串在原有处理之后（onLoad / onPrepend / onEvent / onExport / onBlocked）；
   app.js 改了 api 出错带 data、send 处理 blocked、renderSpin 让过 .k-tail、showModal 跳过 inlinePerm、命令补全分组；
   ux-h.js 改了权限模式菜单（说明与分隔线）、“+”菜单加“添加文件夹…”。 */
const C = window.CWB;
if (!C) return;
const {S, $, esc} = C;
const store = C.store || {get: (k, d) => d, set() {}};
const feed = $('#feed'), sc = $('#scroll'), input = $('#input'), dock = $('#dock'), composer = $('#composer'), body = document.body;
const chain = (name, fn) => { const prev = C[name]; C[name] = (...a) => { try { if (prev) prev(...a); } finally { try { fn(...a); } catch (e) { console.error(e); } } }; };
const safe = f => (...a) => { try { return f(...a); } catch (e) { console.error(e); } };
const soon = fn => { let q = false; return () => { if (q) return; q = true; queueMicrotask(() => { q = false; safe(fn)(); }); }; };
/* 接口：出错（不存在、断线、返回 {error}）一律变成 {error}，调用方据此安静地不显示 */
async function call(n, b) {
  try {
    const d = await C.api(n, b);
    return d && typeof d === 'object' ? d : {error: '暂时拿不到'};
  } catch (e) { return {error: (e && e.message) || '暂时拿不到', data: e && e.data}; }
}
const busyNow = () => !!(S.runner && S.runner.alive && S.runner.busy);
const cnt = x => (Array.isArray(x) ? x.length : (+x || 0));
const clip = (s, n) => { s = String(s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n) + '…' : s; };
const num = v => (v == null || v === '' || !isFinite(+v) ? null : +v);
const tilde = p => (S.home && p && p.startsWith(S.home + '/') ? '~' + p.slice(S.home.length) : p || '');
function mk(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
function btn(cls, html, title) { const b = mk('button', cls, html); b.type = 'button'; if (title) { b.title = title; b.setAttribute('aria-label', title); } return b; }
const I = d => `<svg class="k-i" viewBox="0 0 16 16" aria-hidden="true">${d}</svg>`;
const IC = {
  fork: I('<circle cx="4.5" cy="3.8" r="1.5"/><circle cx="4.5" cy="12.2" r="1.5"/><circle cx="11.5" cy="5.5" r="1.5"/><path d="M4.5 5.3v5.4M11.5 7c0 2.6-7 1.4-7 3.7"/>'),
  undo: I('<path d="M5.5 3.5 2.5 6.5l3 3"/><path d="M2.5 6.5h7a3.5 3.5 0 0 1 0 7H7"/>'),
  copy: I('<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M3 10.5v-7a1 1 0 0 1 1-1h6.5"/>'),
  side: I('<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M9.5 3v10"/>'),
  plan: I('<path d="M3.5 4h1M6.5 4h6M3.5 8h1M6.5 8h6M3.5 12h1M6.5 12h4"/>'),
  info: I('<circle cx="8" cy="8" r="5.8"/><path d="M8 7.3v3.5M8 5.2v.2"/>'),
  warn: I('<path d="M8 2.8 14 13H2z"/><path d="M8 6.8v3M8 11.3v.2"/>'),
  file: I('<path d="M4 2.5h5.5L12 5v8.5H4z"/><path d="M9.5 2.5V5H12"/>'),
  trash: I('<path d="M3 4.5h10M6.5 4.5v-2h3v2M4.5 4.5l.5 9h6l.5-9"/>'),
  x: I('<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>'),
  up: I('<path d="M8 12.5v-9M4.5 7 8 3.5 11.5 7"/>'),
  folder: I('<path d="M2.2 4.4c0-.7.5-1.2 1.2-1.2h2.8l1.4 1.6h5c.7 0 1.2.5 1.2 1.2v5.8c0 .7-.5 1.2-1.2 1.2H3.4c-.7 0-1.2-.5-1.2-1.2z"/>'),
  cmp: I('<path d="M5 2.5v3h-3M11 2.5v3h3M5 13.5v-3h-3M11 13.5v-3h3"/>'),
  chev: '<svg class="k-i k-chev" viewBox="0 0 10 10" aria-hidden="true"><path d="M2.6 3.9 5 6.3l2.4-2.4"/></svg>',
};
body.classList.add('k-on');

const K = {
  tc: null, tcSid: null, tcSeq: 0, tcT: [],   // turn-changes
  plans: new Map(), planTool: null,           // 计划卡 rid → 元素；被收起的 ExitPlanMode 工具行
  ctxBy: {}, ctxOpen: false, ctxPin: false,   // 上下文
  blocked: [], block: null, blockOff: '',     // 拦截命令
  errOff: '', cmp: null, dirsBy: {}, dlg: null, planFail: new Set(), cmpSaved: {}, pwEl: null, inh: null,
};

/* 视口不跳：停在底部就继续贴底，否则以视口里第一个可见元素为锚补偿滚动 */
function keepView(fn) {
  const stick = C.nearBottom();
  let anc = null, y0 = 0;
  if (!stick) {
    const v = sc.getBoundingClientRect();
    for (const el of feed.querySelectorAll(':scope > *, :scope > .tl > *')) {
      if (el.classList.contains('tl')) continue;
      const r = el.getBoundingClientRect();
      if (r.top >= v.bottom) break;
      if (r.height > 0 && r.bottom > v.top) { anc = el; y0 = r.top; break; }
    }
  }
  fn();
  if (stick) C.toBottom();
  else if (anc && anc.isConnected) sc.scrollTop += anc.getBoundingClientRect().top - y0;
}
function keepBottom(fn) { const stick = S.cur && C.nearBottom(); fn(); if (stick) C.toBottom(); }

/* ================= 1. 每轮改动（turn-changes） ================= */
/* turn-changes 的一轮可能由用户消息、系统通知或斜杠命令 / 技能开头：任何带 uuid 的这类条目都拿来对（按记录顺序） */
const isTurnStart = it => (it.k === 'user' && !it.mid) || (it.k === 'sys' && it.turn);
const isAnchor = it => !!it.uuid && (isTurnStart(it) || it.k === 'cmd');
const starts = () => { const o = []; for (const it of S.byId.values()) if (isAnchor(it)) o.push(it); return o; };
function tcMap() { const m = new Map(); if (K.tc) for (const t of K.tc.turns || []) if (t && t.uuid) m.set(t.uuid, t); return m; }
const nFiles = t => (t ? (t.n != null ? +t.n || 0 : (t.files || []).length) : 0);
/* 第 i 个轮开头（含 / 不含它自己）往后改过文件的轮 */
function laterTurns(us, i, strict, m) {
  const out = [];
  if (i == null || i < 0) return out;
  for (let j = strict ? i + 1 : i; j < us.length; j++) { const t = m.get(us[j].uuid); if (t && nFiles(t)) out.push(t); }
  return out;
}
/* 快照不可用的原因（修复线：按 turn-changes 给的原因，不再看标题）：fork 这一轮继承自原对话（分叉前各轮、含分叉点那一轮）；gone 快照已清理 */
const expiredText = why => (why === 'fork' ? '这一轮的快照在原对话里' : '快照已过期，无法回退');
const inh = uuid => !!(uuid && K.inh && K.tcSid === S.cur && K.inh.has(uuid));
const whyOf = (uuid, later) => { if (inh(uuid)) return 'fork'; const t = later.find(x => x.expired); return t ? t.why || 'gone' : ''; };
/* 界面上每一轮（结束行所属的那一轮）里带 uuid 的轮开头条目：开头那条 + 中间的斜杠命令 */
function turnAnchors() {
  const out = new Map();
  let cur = null;
  for (const it of S.byId.values()) {
    if (isTurnStart(it)) cur = it._turn || null;
    if (cur && isAnchor(it)) { if (!out.has(cur)) out.set(cur, []); out.get(cur).push(it); }
  }
  return out;
}
const cmdName = it => { const w = String(it.text || '').trim().split(/\s+/)[0] || ''; return clip(w.startsWith('/') ? w : '/' + w, 24); };
async function loadTC(sid) {
  sid = sid || S.cur;
  if (!sid) return;
  const seq = ++K.tcSeq;
  const d = await call('turn-changes', {sid});
  if (seq !== K.tcSeq || S.cur !== sid) return;
  if (!d.error && Array.isArray(d.turns)) { K.tc = d; K.tcSid = sid; K.inh = new Set(Array.isArray(d.inherited) ? d.inherited : []); }
  else if (K.tcSid !== sid) { K.tc = null; K.tcSid = sid; K.inh = null; }  // 拿不到：没有旧数据就不显示；同一对话保留上次的，不闪
  decoNow(); restoreRewinds();
}
function scheduleTC(delays) {
  K.tcT.forEach(clearTimeout);
  const sid = S.cur;
  K.tcT = delays.map(ms => setTimeout(() => { if (S.cur === sid) loadTC(sid); }, ms));
}

/* ---- 结束行：改动胶囊 + 从这里分叉 ---- */
function decoEnds() {
  if (!S.cur) return;
  const us = starts(), pos = new Map(us.map((u, i) => [u, i])), m = tcMap(), ta = turnAnchors(), ends = [];
  for (const it of S.byId.values()) { const t = it._turn; if (t && t.it === it && t !== S.turn && t.el && t.el.isConnected) ends.push([t, t.el, false]); }
  const sp = S.spinEl;
  if (sp && sp.isConnected) {
    if (S.turn && sp.classList.contains('done')) ends.push([S.turn, sp, true]);
    else { const g = sp.querySelector(':scope > .k-end'); if (g) g.remove(); }
  }
  for (const [t, el, last] of ends) endGroup(t, el, last, us, pos, m, ta.get(t) || []);
}
function endGroup(t, el, last, us, pos, m, list) {
  const u = t.it;
  /* 这一轮里改过文件的（一般只有开头那条；中间跑了斜杠命令 / 技能的，那一段单独一个胶囊，前面带命令名） */
  const chs = list.map(a => [a, m.get(a.uuid)]).filter(([, ch]) => nFiles(ch));
  const fork = C.turnInfo(t).cls === 'ok' && u.fork ? u.fork : '';
  const tail = list[list.length - 1] || null;  // “分叉并回退代码”回到这一轮结束时：以这一轮里最后一个轮开头为准
  const rw = !!(fork && !last && tail && K.tc && !inh(tail.uuid) && laterTurns(us, pos.get(tail), true, m).length);  // 修复线：继承自原对话的轮不给“分叉并回退代码”
  let g = el.querySelector(':scope > .k-end');
  if (!chs.length && !fork) { if (g) g.remove(); return; }
  const sig = [chs.map(([a, ch]) => `${a.uuid}:${nFiles(ch)}|${ch.add || 0}|${ch.del || 0}`).join(','), fork, rw ? 1 : 0].join('/');
  if (!g) g = mk('span', 'k-end');
  g._t = t; g._rw = rw; g._tail = tail;
  if (g._sig !== sig) {
    g._sig = sig;
    g.innerHTML = chs.map(([a, ch]) => `<button type="button" class="k-chg" data-u="${esc(a.uuid)}">${a.k === 'cmd' ? `<span class="k-cn">${esc(cmdName(a))}</span>` : ''}改动 ${nFiles(ch)} 个文件` +
        `<span class="k-pm">${ch.add ? `<b class="a">+${ch.add}</b>` : ''}${ch.del ? `<b class="d">−${ch.del}</b>` : ''}</span></button>`).join('') +
      (fork ? `<button type="button" class="k-fork"${rw ? ' aria-haspopup="menu"' : ''}>${IC.fork}<span>从这里分叉</span>${rw ? IC.chev : ''}</button>` : '');
  }
  const fk = el.querySelector(':scope > .fk');
  if (g.parentNode !== el || (fk ? g.nextElementSibling !== fk : el.lastElementChild !== g)) el.insertBefore(g, fk);
}
function openReview(uuid) {
  if (typeof C.openReview === 'function') C.openReview({scope: 'turn', uuid});
  else if (C.pane && C.pane.open) C.pane.open('review');
}
function forkMenu(b) {
  const g = b.closest('.k-end'), t = g && g._t;
  if (!t || !t.it.fork) return;
  if (!g._rw) { C.forkAt(t.it.fork); return; }
  if (b.classList.contains('open') || Date.now() - (b._shut || 0) < 300) { C.closeMenu(); return; }  // 开着时再点一下是收起（按下时菜单已被关掉）
  C.menu(b, [
    {label: '从这里分叉', sub: '新对话带到这一轮结束', fn: () => C.forkAt(t.it.fork)},
    {label: '分叉并回退代码', sub: '文件也回到这一轮结束时', fn: () => openRewind({mode: 'fork', it: t.it, at: g._tail})},
  ]);
  b.classList.add('open');
  const mo = new MutationObserver(() => { if (!S.menu || !S.menu.isConnected) { b.classList.remove('open'); b._shut = Date.now(); mo.disconnect(); } });
  mo.observe(body, {childList: true});
}

/* ---- 用户消息悬停操作条：复制、回退代码 ---- */
function decoUsers() {
  if (!S.cur) return;
  const us = starts(), pos = new Map(us.map((u, i) => [u, i])), m = tcMap(), busy = busyNow(), els = new Map();
  feed.querySelectorAll(':scope > .user[data-id]').forEach(el => els.set(el.dataset.id, el));
  for (const u of S.byId.values()) {
    if (u.k !== 'user' || u.mid) continue;
    const el = els.get(u.id);
    if (!el) continue;
    let a = el.nextElementSibling;
    if (a && a.classList.contains('ufold')) a = a.nextElementSibling;
    if (!a || !a.classList.contains('k-uact') || a.dataset.for !== u.id) {
      a = mk('div', 'k-uact', '<div class="k-ubar"></div>'); a.dataset.for = u.id;
      const nx = el.nextElementSibling;
      (nx && nx.classList.contains('ufold') ? nx : el).after(a);
    }
    a._it = u;
    const later = u.uuid && K.tc ? laterTurns(us, pos.get(u), false, m) : [];
    const rw = later.length > 0, why = rw ? whyOf(u.uuid, later) : '';
    const off = busy ? '正在回复，结束后再回退' : why ? expiredText(why) : '';
    const sig = (rw ? 1 : 0) + '|' + off;
    a._off = off;
    if (a._sig === sig) continue;
    a._sig = sig;
    a.firstChild.innerHTML = `<button type="button" data-a="copy">${IC.copy}<span>复制</span></button>` +
      (rw ? `<button type="button" data-a="rw"${off ? ` aria-disabled="true" title="${esc(off)}"` : ''}>${IC.undo}<span>回退代码</span></button>` : '');
  }
}
function userAct(b) {
  const a = b.closest('.k-uact'), it = a && a._it;
  if (!it) return;
  if (b.dataset.a === 'copy') {
    C.copy(it.text || '');
    const s = b.querySelector('span'); s.textContent = '已复制';
    clearTimeout(b._t); b._t = setTimeout(() => { s.textContent = '复制'; }, 1200);
  } else if (b.dataset.a === 'rw') {
    if (busyNow()) { C.toast('正在回复，结束后再回退'); return; }
    if (a._off) { C.toast(a._off); return; }
    openRewind({mode: 'rw', it, at: it});
  }
}

feed.addEventListener('click', e => {
  const t = e.target;
  const chg = t.closest('.k-end .k-chg');
  if (chg) { e.stopPropagation(); openReview(chg.dataset.u); return; }
  const fk = t.closest('.k-end .k-fork');
  if (fk) { e.stopPropagation(); safe(forkMenu)(fk); return; }
  const ub = t.closest('.k-ubar button[data-a]');
  if (ub) { e.stopPropagation(); safe(userAct)(ub); return; }
  const un = t.closest('.k-rwrow .k-undo');
  if (un) { e.stopPropagation(); safe(undoRw)(un.closest('.k-rwrow')); return; }
  const cp = t.closest('.k-cmp.done .k-cmph');
  if (cp) { const b = cp.parentNode.querySelector('.k-cmpb'); if (b) { b.hidden = !b.hidden; cp.setAttribute('aria-expanded', b.hidden ? 'false' : 'true'); cp.parentNode.classList.toggle('open', !b.hidden); } }
});

/* ================= 2. 回退预览、回退行、撤销 ================= */
function closeCard() {
  const card = $('#card');
  $('#modal').hidden = true; card.innerHTML = ''; card.classList.remove('k-narrow', 'k-wide');
  S.modalKey = null; K.dlg = null;
  if (C.showModal) C.showModal();
}
function openCard(cls, html) {
  const card = $('#card');
  S.modalKey = 'ask';  // 和其他对话框一样：开着时授权弹窗等它关掉再出
  card.classList.remove('k-narrow', 'k-wide'); card.classList.add(cls);
  card.innerHTML = html;
  $('#modal').hidden = false;
  const tok = {}, root = card.firstElementChild; K.dlg = tok;
  return {card, tok, alive: () => K.dlg === tok && root.isConnected && !$('#modal').hidden};
}
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !K.dlg || $('#modal').hidden) return;
  if (K.dlg.busy) return;
  e.preventDefault(); closeCard();
});
const sec = (ic, t) => `<div class="k-rwsec">${ic}<span>${t}</span></div>`;
const flist = rows => `<div class="k-rwlist">${rows.map(([p, r]) => `<div>${IC.file}<span class="k-fp" title="${esc(p)}">${esc(p)}</span>${r ? `<em>${r}</em>` : ''}</div>`).join('')}</div>`;
const names = fs => { const n = fs.map(f => `<code>${esc(f.rel || f.path || '')}</code>`); return n.length > 3 ? n.slice(0, 3).join('、') + ` 等 ${n.length} 个文件` : n.join('、'); };
const warnRow = (cls, ic, h) => `<div class="k-rwwarn ${cls}">${ic}<span>${h}</span></div>`;
async function openRewind(o) {
  const sid = S.cur, it = o.it, fork = o.mode === 'fork', at = o.at || it;  // at：回退到它之前（或它那一轮结束时）的轮开头条目
  if (!sid || !it || !at || !at.uuid || (fork && !it.fork)) return;
  if (busyNow()) { C.toast('正在回复，结束后再回退'); return; }
  C.closeMenu();
  const dl = openCard('k-narrow', `<div class="k-rw"><h3>${fork ? '分叉并回退代码' : '回退代码到这条消息之前'}</h3>` +
    `<div class="k-rwq">${fork ? '回到这一轮结束时 · ' : ''}“${esc(clip(it.text, 80) || '（图片）')}”</div>` +
    '<div class="k-rwb"><div class="k-load"><i class="k-spin"></i><span>正在读取要回退的文件…</span></div></div>' +
    '<div class="k-rwerr" hidden></div>' +
    `<div class="acts"><button class="btn" data-a="no">取消</button><button class="btn primary" data-a="ok" disabled>${fork ? '分叉并回退' : '回退代码'}</button></div></div>`);
  const root = dl.card.querySelector('.k-rw'), ok = root.querySelector('[data-a=ok]'), no = root.querySelector('[data-a=no]'), err = root.querySelector('.k-rwerr');
  no.onclick = () => { if (!K.dlg || !K.dlg.busy) closeCard(); };
  const box = root.querySelector('.k-rwb');
  if (inh(at.uuid)) { box.innerHTML = warnRow('amb', IC.warn, expiredText('fork')); return; }  // 修复线：继承自原对话的轮不回退（后端也拒绝）
  const d = await call('rewind-preview', fork ? {sid, uuid: at.uuid, after: true} : {sid, uuid: at.uuid});
  if (!dl.alive() || S.cur !== sid) return;
  if (d.error) {
    box.innerHTML = warnRow('amb', IC.warn, '暂时拿不到回退信息' + (d.data ? '' : '：' + esc(d.error)));
    return;
  }
  const w = d.write || [], r = d.remove || [], cf = d.conflicts || [], ms = d.missing || [];
  const us = starts(), i = us.indexOf(at), m = tcMap();
  const why = i >= 0 ? whyOf(at.uuid, laterTurns(us, i, fork, m)) : '', expired = !!why;
  let h = '';
  if (w.length) h += sec(IC.undo, `写回 ${w.length} 个文件`) + flist(w.map(f => [f.rel || f.path, (f.add ? `<b class="a">+${f.add}</b>` : '') + (f.del ? `<b class="d">−${f.del}</b>` : '')]));
  if (r.length) h += sec(IC.trash, `删除 ${r.length} 个文件`) + flist(r.map(f => [f.rel || f.path, '这之后新建的']));
  if (!w.length && !r.length) h += `<div class="k-rwempty">没有需要回退的文件</div>`;
  let ws = '';
  if (expired) ws += warnRow('amb', IC.warn, expiredText(why));
  if (ms.length) ws += warnRow('amb', IC.warn, `${names(ms)} 的快照已不在，无法回退`);
  if (d.busy) ws += warnRow('amb', IC.warn, '正在回复，结束后再回退');
  if (cf.length) ws += warnRow('amb', IC.warn, `${names(cf)} 在这之后被别的程序改过，回退会盖掉那些修改`);
  if (!K.tc || K.tc.bashNote !== false) ws += warnRow('', IC.info, '只被命令行改过的文件不会恢复');
  box.innerHTML = h + (ws ? `<div class="k-rwws">${ws}</div>` : '');
  ok.disabled = !(w.length || r.length) || !!ms.length || expired || !!d.busy;
  if (!ok.disabled) ok.focus({preventScroll: true});
  ok.onclick = async () => {
    if (ok.disabled) return;
    dl.tok.busy = true; ok.disabled = true; no.disabled = true; err.hidden = true;
    ok.textContent = fork ? '正在分叉…' : '正在回退…';
    const fail = t => { dl.tok.busy = false; no.disabled = false; ok.disabled = false; ok.textContent = fork ? '分叉并回退' : '回退代码'; err.textContent = t; err.hidden = false; };
    if (!fork) {
      const x = await call('rewind-apply', {sid, uuid: at.uuid});
      if (!dl.alive()) return;
      if (x.error || !x.ok) return fail(x.error || '没能回退，请重试');
      dl.tok.busy = false; closeCard();
      if (x.id == null) C.toast(NOOP);  // 没有文件要改：后台不留回退记录，时间线也不插行
      else if (S.cur === sid) addRwRow({id: x.id, uuid: at.uuid, written: x.written, removed: x.removed}, {live: true, after: false, text: it.text});
      scheduleTC([300, 2000]);
      return;
    }
    const f = await call('fork-at', {sid, uuid: it.fork});
    if (!dl.alive()) return;
    if (f.error || !f.sid) return fail(f.error || '没能分叉，请重试');
    const x = await call('rewind-apply', {sid, uuid: at.uuid, after: true});
    dl.tok.busy = false; closeCard();
    C.load(f); store.set('last', f.sid); C.refreshSide(true);
    if (x.error || !x.ok) C.toast('已分叉到新对话，代码没有回退：' + (x.error || '请重试'));
    else if (x.id == null) C.toast('已分叉到新对话；' + NOOP);
    else { addRwRow({id: x.id, uuid: at.uuid, written: x.written, removed: x.removed}, {live: true, after: true, text: it.text}); C.toast('已分叉到新对话'); }
  };
}
const NOOP = '文件已经是那时的样子，没有需要回退的';
function rwText(r, text, after) {
  const parts = [], w = cnt(r.written), x = cnt(r.removed);
  if (w) parts.push(`写回 ${w} 个文件`);
  if (x) parts.push(`删除 ${x} 个文件`);
  const q = text != null ? clip(text, 24) : '';
  const head = text != null ? `已回退代码到“${q || '图片'}”${after ? '这一轮结束时' : '之前'}` : '已回退代码';
  return head + (parts.length ? '，' + parts.join('，') : '');
}
/* 回退行插在哪一轮的结束行后面（同一轮已有回退行就排在它们后面） */
function turnByKey(key) {
  if (!key) return null;
  for (const it of S.byId.values()) if (it._turn && it._turn.it === it && (it.uuid === key || it.fork === key || it.id === key)) return it._turn;
  return null;
}
const turnKey = t => (t ? t.it.uuid || t.it.fork || '' : '');
function anchorOf(t) {
  let a = null;
  if (t) {
    if (t === S.turn) a = S.spinEl && S.spinEl.parentNode === feed ? S.spinEl : null;
    else a = t.el && t.el.parentNode === feed ? t.el : null;
    if (!a && t.tl && t.tl.parentNode === feed) a = t.tl;
  }
  if (!a) return null;
  while (a.nextElementSibling && a.nextElementSibling.classList.contains('k-tail')) a = a.nextElementSibling;
  return a;
}
function placeRow(row, t) {
  const a = anchorOf(t);
  if (a) { a.after(row); return true; }
  if (t) return false;  // 那一轮还没载入（在更早的记录里）
  const p = S.pendings.find(x => x.parentNode === feed);
  feed.insertBefore(row, p || (S.exitEl && S.exitEl.parentNode === feed ? S.exitEl : null));
  return true;
}
function addRwRow(r, o) {
  if (!r || !S.cur) return;
  const id = r.id ? String(r.id) : '';
  if (id && feed.querySelector(`.k-rwrow[data-rw="${CSS.escape(id)}"]`)) return;
  const row = mk('div', 'k-row k-rwrow k-tail' + (r.undone ? ' undone' : ''));
  if (id) row.dataset.rw = id;
  row.innerHTML = `${IC.undo}<span class="k-rt"></span>` + (o.live && id ? '<button type="button" class="k-undo">撤销</button>' : '');
  row.querySelector('.k-rt').textContent = rwText(r, o.text, o.after) + (r.undone ? '（已撤销）' : '');
  const t = o.live ? S.turn : o.turn;
  if (o.live && id) {
    const at = store.get('k.rwAt', {}) || {};
    at[id] = {a: turnKey(S.turn), f: o.after ? 1 : 0, q: clip(o.text, 40)};
    const ks = Object.keys(at); if (ks.length > 200) ks.slice(0, ks.length - 200).forEach(k => delete at[k]);
    store.set('k.rwAt', at);
    row._t = setTimeout(() => { const b = row.querySelector('.k-undo'); if (b && !b.disabled) b.remove(); }, 10000);
  }
  keepView(() => { if (!placeRow(row, t)) row.remove(); });
}
async function undoRw(row) {
  const b = row.querySelector('.k-undo'), id = row.dataset.rw;
  if (!b || b.disabled || !id) return;
  b.disabled = true; b.textContent = '正在撤销…';
  const d = await call('rewind-undo', {id});
  if (d.error || !d.ok) { C.toast(d.error || '没能撤销'); b.disabled = false; b.textContent = '撤销'; return; }
  clearTimeout(row._t); b.remove(); row.classList.add('undone');
  const tx = row.querySelector('.k-rt'); tx.textContent += '（已撤销）';
  scheduleTC([300, 2000]);
}
/* 重新打开：按 turn-changes.rewinds 还原回退行（不带撤销）；位置用本机记下的那一轮，没有就放最后 */
function restoreRewinds() {
  if (!K.tc || K.tcSid !== S.cur || !Array.isArray(K.tc.rewinds) || !K.tc.rewinds.length) return;
  const at = store.get('k.rwAt', {}) || {};
  const list = K.tc.rewinds.filter(r => r && r.id != null).slice().sort((a, b) => (num(a.t) || 0) - (num(b.t) || 0));
  for (const r of list) {
    const id = String(r.id), ex = feed.querySelector(`.k-rwrow[data-rw="${CSS.escape(id)}"]`);
    if (ex) {
      if (r.undone && !ex.classList.contains('undone')) { ex.classList.add('undone'); const b = ex.querySelector('.k-undo'); if (b) b.remove(); ex.querySelector('.k-rt').textContent += '（已撤销）'; }
      continue;
    }
    const a = at[id] || null, tu = r.uuid ? starts().find(u => u.uuid === r.uuid) : null;
    const t = a && a.a ? turnByKey(a.a) : null;
    if (a && a.a && !t) continue;  // 那一轮还没载入
    addRwRow(r, {live: false, turn: t, after: !!(a && a.f), text: tu ? tu.text : (a && a.q != null ? a.q : null)});
  }
}

/* ================= 3. 计划卡 ================= */
/* 画不出卡片的（出错）退回原来的弹窗，授权不会“看不见” */
C.inlinePerm = p => !!(p && !K.planFail.has(p.rid) && p.tool === 'ExitPlanMode' && typeof p.plan === 'string' && p.plan.trim() && Array.isArray(p.options) && p.options.length);
function syncPlans() {
  const want = new Map();
  if (S.cur) for (const p of S.perms.values()) if (p.sid === S.cur && C.inlinePerm(p)) want.set(p.rid, p);
  for (const [rid, el] of [...K.plans]) {
    if (want.has(rid) && el.isConnected && el._sid === S.cur) {
      /* 第十五轮联调：授权事件常比这一轮的记录先到，卡片先插在了还没落记录的用户消息前面；记录到了以后挪回这一轮过程的后面 */
      const ref = planRef();
      if (ref !== el && el.nextElementSibling !== ref) keepView(() => feed.insertBefore(el, ref));
      continue;
    }
    K.plans.delete(rid);
    if (el.isConnected) keepView(() => el.remove());
  }
  for (const [rid, p] of want) {
    if (K.plans.has(rid)) continue;
    try { addPlan(p); } catch (e) {
      console.error(e); K.planFail.add(rid); K.plans.delete(rid);
      const el = feed.querySelector(`.k-plan[data-rid="${CSS.escape(rid)}"]`); if (el) el.remove();
      if (C.showModal) C.showModal();
    }
  }
  planTool(K.plans.size > 0);
  planWait();
}
/* 计划卡显示时，同一份计划的 ExitPlanMode 工具行先收起来，不显示两遍；修复线：简洁档里所有 ExitPlanMode 工具行都收起（计划卡保留） */
function planTool(on) {
  const ids = new Set(), v = [...S.byId.values()];
  if (on) for (let i = v.length - 1; i >= 0; i--) { const it = v[i]; if (it.k === 'tool' && it.name === 'ExitPlanMode') { ids.add(it.id); break; } if (it.k === 'user') break; }
  if (body.classList.contains('k-simple')) for (const it of v) if (it.k === 'tool' && it.name === 'ExitPlanMode') ids.add(it.id);
  feed.querySelectorAll('.k-planhide').forEach(el => { if (!ids.has(el.dataset.id)) el.classList.remove('k-planhide'); });
  for (const id of ids) { const el = feed.querySelector(`[data-id="${CSS.escape(id)}"]`); if (el) el.classList.add('k-planhide'); }
}
/* 修复线：计划卡在等确认时 CLI 停在授权上，这一轮的结束行会显示“… 未完成 0 秒”，先藏起来（renderSpin 每次重写 className，所以用行内样式） */
function planWait() {
  const el = S.spinEl && S.spinEl.parentNode === feed ? S.spinEl : null;
  const on = !!el && [...K.plans.values()].some(c => c.isConnected && c._sid === S.cur);
  if (K.pwEl && (K.pwEl !== el || !on)) { K.pwEl.style.removeProperty('display'); K.pwEl = null; }
  if (on && K.pwEl !== el) { el.style.setProperty('display', 'none', 'important'); K.pwEl = el; }
}
/* 计划卡放在这一轮过程的后面：转圈 / 结束行之前；这一轮还没画出来时先放在待发消息、“已退出”之前 */
function planRef() {
  if (S.turn && S.spinEl && S.spinEl.parentNode === feed) return S.spinEl;
  const cand = [S.spinEl, ...S.pendings, S.exitEl].filter(x => x && x.parentNode === feed && !x.classList.contains('k-plan'));
  return cand.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1))[0] || null;
}
function addPlan(p) {
  const card = mk('div', 'k-plan');
  card.dataset.rid = p.rid; card._sid = S.cur; card._p = p;
  card.innerHTML = `<div class="k-plh">${IC.plan}<b>计划</b><span class="k-grow"></span>` +
    (p.planPath ? `<button type="button" class="k-ghost" data-a="side">${IC.side}<span>在右栏打开</span></button>` : '') + '</div>' +
    '<div class="md k-plmd"></div><div class="k-placts"></div>' +
    `<div class="k-plfb" hidden><textarea rows="1" placeholder="哪里要调整？回车发出" spellcheck="false"></textarea><button type="button" class="k-sendb" data-a="send" title="发出" aria-label="发出" disabled>${IC.up}</button></div>`;
  const mdEl = card.querySelector('.k-plmd');
  mdEl.innerHTML = C.md(p.plan); try { C.enhance(mdEl); } catch {}
  const acts = card.querySelector('.k-placts');
  p.options.forEach((o, i) => {
    const b = btn(i ? 'btn' : 'btn primary', esc(o.label || o.key));
    b.dataset.k = o.key; acts.appendChild(b);
  });
  const fb = btn('btn k-fbb', '提修改意见'); fb.dataset.a = 'fb'; fb.setAttribute('aria-expanded', 'false'); acts.appendChild(fb);
  const row = card.querySelector('.k-plfb'), ta = row.querySelector('textarea'), sb = row.querySelector('.k-sendb');
  const fit = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 200) + 'px'; sb.disabled = !ta.value.trim() || !!card._busy; };
  ta.addEventListener('input', fit);
  ta.addEventListener('keydown', e => {
    e.stopPropagation();
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); if (ta.value.trim()) pickPlan(card, 'feedback', ta.value.trim()); }
    else if (e.key === 'Escape') { e.preventDefault(); toggleFb(card, false); }
  });
  card.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    if (b.dataset.a === 'side') { if (C.preview) C.preview(p.planPath); else C.openPath && C.openPath(p.planPath); }
    else if (b.dataset.a === 'fb') toggleFb(card, row.hidden);
    else if (b.dataset.a === 'send') { if (ta.value.trim()) pickPlan(card, 'feedback', ta.value.trim()); }
    else if (b.dataset.k) pickPlan(card, b.dataset.k);
  });
  card._fit = fit;
  K.plans.set(p.rid, card);
  const ref = planRef();
  const stick = C.nearBottom();
  feed.insertBefore(card, ref);
  if (stick) {
    C.toBottom();
    const v = sc.getBoundingClientRect(), r = card.getBoundingClientRect();
    if (r.top < v.top + 12) sc.scrollTop -= v.top + 12 - r.top;  // 长计划：从开头看起
  }
}
function toggleFb(card, open) {
  const row = card.querySelector('.k-plfb'), fb = card.querySelector('.k-fbb'), ta = row.querySelector('textarea');
  keepView(() => { row.hidden = !open; });
  fb.classList.toggle('open', open); fb.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) { card._fit(); ta.focus({preventScroll: true}); row.scrollIntoView({block: 'nearest'}); }
}
async function pickPlan(card, key, message) {
  if (card._busy) return;
  const p = card._p;
  card._busy = true;
  card.classList.add('busy');
  const bs = [...card.querySelectorAll('.k-placts button, .k-plfb button')];
  bs.forEach(b => { b.disabled = true; });
  const hit = key === 'feedback' ? card.querySelector('.k-sendb') : card.querySelector(`.k-placts [data-k="${CSS.escape(key)}"]`);
  if (hit) hit.classList.add('on');
  const r = await call('perm', key === 'feedback' ? {rid: p.rid, plan: 'feedback', message} : {rid: p.rid, plan: key});
  if (r.error) {
    card._busy = false; card.classList.remove('busy');
    bs.forEach(b => { b.disabled = false; }); if (hit) hit.classList.remove('on'); card._fit();
    C.toast(r.error); return;
  }
  S.perms.delete(p.rid);
  syncPlans();
  if (C.showModal) C.showModal();
}

/* ================= 4. 授权弹窗“拒绝并说明” ================= */
function decoPermCard() {
  const card = $('#card');
  if (!card.querySelector('.k-rw, .k-sheet')) card.classList.remove('k-narrow', 'k-wide');
  const key = S.modalKey || '';
  if (!key.startsWith('p:')) return;
  const p = S.perms.get(key.slice(2));
  if (!p || p.tool === 'AskUserQuestion' || p.tool === 'ExitPlanMode') return;
  const acts = card.querySelector(':scope > .acts'), deny = acts && acts.querySelector('[data-d=deny]');
  if (!deny || acts.querySelector('.k-dnb')) return;
  const b = btn('btn k-dnb', '拒绝并说明');
  acts.prepend(b);
  b.onclick = () => openDeny(card, p, acts, b);
}
function openDeny(card, p, acts, b) {
  let row = card.querySelector(':scope > .k-dn');
  if (row) { row.querySelector('input').focus(); return; }
  row = mk('div', 'k-dn', '<input type="text" placeholder="告诉 Claude 应该怎么做" spellcheck="false" autocomplete="off"><button type="button" class="btn k-dngo" disabled>拒绝并发送</button>');
  acts.before(row);
  b.hidden = true;
  const inp = row.querySelector('input'), go = row.querySelector('.k-dngo');
  const send = () => {
    const msg = inp.value.trim();
    if (!msg || go.dataset.sent) return;
    go.dataset.sent = '1';
    C.api('perm', {rid: p.rid, decision: 'deny', allow: false, message: msg}).catch(e => C.toast(e.message));
    S.perms.delete(p.rid);
    if (S.modalKey === 'p:' + p.rid) closeCard(); else if (C.showModal) C.showModal();
  };
  inp.oninput = () => { go.disabled = !inp.value.trim(); };
  inp.onkeydown = e => {
    e.stopPropagation();  // 不让弹窗的 ⌘回车（= 允许）接到
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); send(); }
    else if (e.key === 'Escape') { e.preventDefault(); row.remove(); b.hidden = false; b.focus(); }
  };
  go.onclick = send;
  inp.focus();
}
new MutationObserver(safe(decoPermCard)).observe($('#card'), {childList: true});

/* ================= 5. 上下文环与小卡 ================= */
const ctxChip = $('#ctx');
const ring = btn('k-ring');
ring.id = 'kRing'; ring.hidden = true; ring.setAttribute('aria-haspopup', 'dialog'); ring.setAttribute('aria-expanded', 'false');
ring.innerHTML = '<svg class="k-rg" viewBox="0 0 18 18" aria-hidden="true"><circle class="tr" cx="9" cy="9" r="6.6"/><circle class="pg" cx="9" cy="9" r="6.6" transform="rotate(-90 9 9)" stroke-dasharray="0 42"/></svg><span class="k-rp"></span>';
if (ctxChip) ctxChip.after(ring); else composer.querySelector('.bar').appendChild(ring);
const RC = 2 * Math.PI * 6.6;
function ctxRaw() { if (!S.cur) return null; return S.cur in K.ctxBy ? K.ctxBy[S.cur] : (S.runner && S.runner.ctx) || null; }
function ctxInfo(c) {
  if (!c || typeof c !== 'object') return null;
  const used = num(c.used), win = num(c.window);
  let pct = used != null && win ? used / win * 100 : num(c.pct);
  if (pct == null) return null;
  pct = Math.max(0, Math.min(100, pct));
  let exp = num(c.cacheExpiresAt); if (exp != null && exp < 1e12) exp *= 1000;
  /* 修复线：缓存三种状态 warm / expired / unknown（后端 cache 字段；没有时按 cacheWarm 推） */
  const cache = ['warm', 'expired', 'unknown'].includes(c.cache) ? c.cache : c.cacheWarm === true ? 'warm' : c.cacheWarm === false ? 'expired' : 'unknown';
  return {pct, used, win, warm: c.cacheWarm, cache, exp, cost: num(c.cost)};
}
const fmtK = n => n >= 1e6 ? +(n / 1e6).toFixed(n % 1e6 ? 1 : 0) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k' : n >= 1000 ? +(n / 1e3).toFixed(1) + 'k' : String(Math.round(n));
const fmtPct = p => (p > 0 && p < 1 ? '1%' : Math.round(p) + '%');
const fmtUsd = n => (n > 0 && n < 0.01 ? '<$0.01' : '$' + n.toFixed(2));
function renderRing() {
  const x = !body.classList.contains('h-home') ? ctxInfo(ctxRaw()) : null;
  if (ctxChip) ctxChip.classList.toggle('k-off', !!x);
  if (ring.hidden !== !x) ring.hidden = !x;
  if (!x) { closeCtx(true); return; }
  ring.querySelector('.pg').setAttribute('stroke-dasharray', `${(RC * x.pct / 100).toFixed(2)} 42`);
  ring.classList.toggle('warn', x.pct >= 80 && x.pct < 95);
  ring.classList.toggle('full', x.pct >= 95);
  const t = fmtPct(x.pct), lb = ring.querySelector('.k-rp');
  if (lb.textContent !== t) lb.textContent = t;
  ring.setAttribute('aria-label', '上下文 ' + t);
  if (K.ctxOpen) fillCtx();
}
const cx = mk('div', 'k-ctx');
cx.id = 'kCtx'; cx.hidden = true; cx.setAttribute('role', 'dialog'); cx.setAttribute('aria-label', '上下文');
cx.innerHTML = '<div class="k-cxk">上下文</div><div class="k-cxbig"><span class="v"></span><span class="p"></span></div><div class="k-cxbar"><i></i></div>' +
  '<div class="k-cxr k-cache"><span class="l">提示缓存</span><span class="v"></span></div>' +
  '<div class="k-cxr k-cost"><span class="l">本对话费用（估算）</span><span class="v"></span></div>' +
  '<div class="k-cxa"><button type="button" class="k-sel" data-a="cmp">压缩…</button><button type="button" class="k-sel" data-a="detail">查看明细</button></div>' +
  '<div class="k-cxc" hidden><input type="text" placeholder="重点保留什么（可不填）" spellcheck="false" autocomplete="off"><button type="button" class="k-sel k-go" data-a="go">压缩</button></div>' +
  '<div class="k-cxerr" hidden></div>';
body.appendChild(cx);
function fillCtx() {
  const x = ctxInfo(ctxRaw());
  if (!x) { closeCtx(true); return; }
  const v = x.used != null ? fmtK(x.used) + (x.win ? ' / ' + fmtK(x.win) : '') : fmtPct(x.pct);
  const set = (sel, t) => { const e = cx.querySelector(sel); if (e.textContent !== t) e.textContent = t; };
  set('.k-cxbig .v', v); set('.k-cxbig .p', x.used != null ? `（${fmtPct(x.pct)}）` : '');
  cx.querySelector('.k-cxbar i').style.width = x.pct.toFixed(1) + '%';
  cx.querySelector('.k-cxbar').classList.toggle('warn', x.pct >= 80);
  const cache = cx.querySelector('.k-cache'), now = Date.now();
  const expired = x.cache === 'expired' || (x.cache === 'warm' && x.exp != null && x.exp <= now);
  if (x.cache === 'unknown') cache.hidden = true;  // 修复线：从没观察到缓存时不说“已过期”，不显示这一行
  else if (expired) { cache.hidden = false; cache.classList.add('amb'); set('.k-cache .l', '缓存已过期，下一条会重新计费'); set('.k-cache .v', ''); }
  else if (x.exp != null) {
    const s = Math.ceil((x.exp - now) / 1000);
    cache.hidden = false; cache.classList.remove('amb'); set('.k-cache .l', '提示缓存');
    set('.k-cache .v', s >= 60 ? `还有 ${Math.ceil(s / 60)} 分钟过期` : `还有 ${s} 秒过期`);
  } else cache.hidden = true;
  const cost = cx.querySelector('.k-cost');
  cost.hidden = x.cost == null;
  if (x.cost != null) set('.k-cost .v', fmtUsd(x.cost));
}
function placeCtx() {
  const r = ring.getBoundingClientRect(), w = cx.offsetWidth, h = cx.offsetHeight;
  cx.style.left = Math.round(Math.max(8, Math.min(r.right - w, innerWidth - w - 8))) + 'px';
  cx.style.top = Math.round(Math.max(8, r.top - h - 8)) + 'px';
}
let cxT = 0;
function openCtx(pin) {
  clearTimeout(cxT);
  if (pin) K.ctxPin = true;
  if (K.ctxOpen) { placeCtx(); return; }
  if (ring.hidden) return;
  K.ctxOpen = true;
  cx.querySelector('.k-cxc').hidden = true; cx.querySelector('.k-cxerr').hidden = true;
  const db = cx.querySelector('[data-a=detail]'); db.disabled = false; db.textContent = '查看明细';
  fillCtx(); cx.hidden = false; placeCtx();
  ring.classList.add('open'); ring.setAttribute('aria-expanded', 'true');
}
function closeCtx(force) {
  clearTimeout(cxT);
  if (!K.ctxOpen) return;
  if (!force && (K.ctxPin || cx.contains(document.activeElement) && document.activeElement.tagName === 'INPUT')) return;
  K.ctxOpen = false; K.ctxPin = false;
  cx.hidden = true; ring.classList.remove('open'); ring.setAttribute('aria-expanded', 'false');
}
const lazyClose = () => { clearTimeout(cxT); cxT = setTimeout(() => closeCtx(false), 220); };
ring.addEventListener('mouseenter', () => { clearTimeout(cxT); if (!K.ctxOpen) cxT = setTimeout(() => openCtx(false), 120); });
ring.addEventListener('mouseleave', lazyClose);
cx.addEventListener('mouseenter', () => clearTimeout(cxT));
cx.addEventListener('mouseleave', lazyClose);
ring.addEventListener('click', () => { if (K.ctxOpen && K.ctxPin) closeCtx(true); else openCtx(true); });
document.addEventListener('mousedown', e => { if (K.ctxOpen && !cx.contains(e.target) && !ring.contains(e.target)) closeCtx(true); }, true);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && K.ctxOpen) { closeCtx(true); ring.focus({preventScroll: true}); } });
addEventListener('resize', () => { if (K.ctxOpen) placeCtx(); });
function doCompact(note) {
  if (!S.cur) return;
  if (/Compacting/i.test((S.runner && S.runner.spinner) || '')) { C.toast('正在压缩上下文，请稍等'); return; }
  note = String(note || '').replace(/\s+/g, ' ').trim();
  C.sendText('/compact' + (note ? ' ' + note : ''));
  C.toast(busyNow() ? '这一轮回复结束后开始压缩上下文' : '开始压缩上下文');
  closeCtx(true);
}
async function ctxDetail(b) {
  const sid = S.cur, er = cx.querySelector('.k-cxerr');
  if (!sid) return;
  er.hidden = true;
  if (busyNow()) { er.textContent = '正在回复，结束后再查看'; er.hidden = false; placeCtx(); return; }
  b.disabled = true; b.textContent = '正在读取…';
  const d = await call('context-detail', {sid});
  b.disabled = false; b.textContent = '查看明细';
  if (S.cur !== sid) return;
  if (d.error || typeof d.md !== 'string' || !d.md.trim()) {
    er.textContent = d.error ? '暂时拿不到明细：' + d.error : '暂时拿不到明细';
    er.hidden = false; if (K.ctxOpen) placeCtx(); return;
  }
  closeCtx(true);
  openSheet('上下文明细', d.md);
}
cx.addEventListener('click', e => {
  const b = e.target.closest('button[data-a]');
  if (!b || b.disabled) return;
  K.ctxPin = true;
  if (b.dataset.a === 'cmp') {
    const row = cx.querySelector('.k-cxc'); row.hidden = !row.hidden; b.classList.toggle('on', !row.hidden);
    placeCtx(); if (!row.hidden) row.querySelector('input').focus();
  } else if (b.dataset.a === 'go') doCompact(cx.querySelector('.k-cxc input').value);
  else if (b.dataset.a === 'detail') ctxDetail(b);
});
cx.querySelector('.k-cxc input').addEventListener('keydown', e => {
  e.stopPropagation();
  if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); doCompact(e.target.value); }
  else if (e.key === 'Escape') { e.preventDefault(); closeCtx(true); ring.focus({preventScroll: true}); }
});
function openSheet(title, mdText) {
  const dl = openCard('k-wide', `<div class="k-sheet"><div class="k-shh"><h3>${esc(title)}</h3><button type="button" class="k-ib" data-a="x" title="关闭" aria-label="关闭">${IC.x}</button></div><div class="md k-shb"></div></div>`);
  const b = dl.card.querySelector('.k-shb');
  b.innerHTML = C.md(mdText); try { C.enhance(b); } catch {}
  dl.card.querySelector('[data-a=x]').onclick = closeCard;
}

/* ================= 6. 输入框上方的横幅：命令拦截、接口出错 ================= */
const ban = mk('div');
ban.id = 'kBan'; ban.hidden = true; ban.setAttribute('role', 'status');
dock.insertBefore(ban, composer);
const normCmd = c => String(c || '').trim().replace(/^\//, '').toLowerCase();
function blockedOf(text) {
  const m = /^\s*\/([^\s]+)/.exec(text || '');
  if (!m || !K.blocked.length) return null;
  const n = m[1].toLowerCase();
  return K.blocked.find(b => normCmd(b.cmd) === n) || null;
}
async function loadBlocked(tries) {
  const d = await call('projects', {});
  if (Array.isArray(d.blockedCmds)) { K.blocked = d.blockedCmds.filter(b => b && b.cmd); checkTyped(); }
  else if (tries > 0 && d.error) setTimeout(() => loadBlocked(tries - 1), 5000);
}
function lastErr() {
  if (!S.cur || busyNow() || S.pendings.length) return null;
  const v = [...S.byId.values()];
  for (let i = v.length - 1; i >= 0; i--) {
    const it = v[i];
    if (it.k === 'error') return it;
    if (it.k === 'user' || it.k === 'text' || it.k === 'tool' || (it.k === 'sys' && it.turn)) return null;
  }
  return null;
}
let banKey = '';
function renderBanner() {
  let key = '', html = '';
  const b = K.block;
  const er = !b && !body.classList.contains('h-home') ? lastErr() : null;
  if (b && normCmd(b.cmd) !== K.blockOff) {
    const c = '/' + normCmd(b.cmd);
    key = 'b:' + c + '|' + (b.hint || '');
    html = `${IC.info}<span class="k-bt"><code>${esc(c)}</code> 不会发出。${esc(b.hint || '这个命令会改全局设置')}</span>` +
      `<button type="button" class="k-ib" data-a="x" title="关闭" aria-label="关闭">${IC.x}</button>`;
  } else if (er && S.cur + '|' + er.id !== K.errOff) {
    const t = String(er.text || '').replace(/\s+/g, ' ').trim();
    key = 'e:' + er.id;
    html = `${IC.info}<span class="k-bt" title="${esc(t.slice(0, 600))}">${esc((er.etype ? er.etype + '：' : '') + (t || '请求出错'))}</span>` +
      '<button type="button" class="k-ghost" data-a="retry">重试</button><button type="button" class="k-ghost" data-a="copy">复制</button>' +
      `<button type="button" class="k-ib" data-a="x" title="关闭" aria-label="关闭">${IC.x}</button>`;
  }
  if (key === banKey) return;
  banKey = key;
  keepBottom(() => {
    ban.className = key.startsWith('e:') ? 'err' : '';
    ban.innerHTML = html; ban.hidden = !key; ban._it = er;
  });
}
ban.addEventListener('click', e => {
  const b = e.target.closest('button[data-a]');
  if (!b) return;
  const a = b.dataset.a;
  if (a === 'x') { if (banKey.startsWith('b:')) { K.blockOff = normCmd(K.block && K.block.cmd); } else if (ban._it) K.errOff = S.cur + '|' + ban._it.id; renderBanner(); input.focus(); }
  else if (a === 'copy' && ban._it) { C.copy(String(ban._it.text || '')); b.textContent = '已复制'; setTimeout(() => { b.textContent = '复制'; }, 1200); }
  else if (a === 'retry' && ban._it) { K.errOff = S.cur + '|' + ban._it.id; renderBanner(); C.sendText('继续'); }
});
function checkTyped() {
  const b = blockedOf(input.value);
  if (!b) K.blockOff = '';
  K.block = b;
  renderBanner();
}
input.addEventListener('input', () => safe(checkTyped)());
const sendsOnEnter = e => (store.get('pref.sendKey', 'enter') === 'cmd' ? (e.metaKey || e.ctrlKey) : !e.shiftKey);
document.addEventListener('keydown', e => {
  if (e.target !== input || e.key !== 'Enter' || e.isComposing || e.keyCode === 229 || S.pop || !sendsOnEnter(e)) return;
  const b = blockedOf(input.value);
  if (!b) return;
  e.preventDefault(); e.stopPropagation();
  K.block = b; K.blockOff = ''; renderBanner();
}, true);
document.addEventListener('click', e => {
  const s = e.target.closest && e.target.closest('#send, #sendNow');
  if (!s || (s.id === 'send' && s.dataset.state === 'stop')) return;
  const b = blockedOf(input.value);
  if (!b) return;
  e.preventDefault(); e.stopPropagation();
  K.block = b; K.blockOff = ''; renderBanner();
}, true);
chain('onBlocked', (d, text) => {
  const m = /^\s*\/([^\s]+)/.exec(text || '');
  K.block = {cmd: (d && d.cmd) || (m ? m[1] : ''), hint: (d && d.hint) || ''};
  K.blockOff = '';
  renderBanner();
});

/* ================= 7. 添加文件夹 ================= */
const dirsEl = mk('div');
dirsEl.id = 'kDirs'; dirsEl.hidden = true;
composer.insertBefore(dirsEl, $('#atts'));
C.addDir = async () => {
  const sid = S.cur;
  if (!sid || K.addingDir) return;
  K.addingDir = true;
  const d = await call('add-dir', {sid, path: ''});
  K.addingDir = false;
  if (d.error) { if (!/取消|没有选/.test(d.error)) C.toast(d.error); return; }
  if (!d.ok || !d.dir) return;
  (K.dirsBy[sid] = K.dirsBy[sid] || []).push({dir: d.dir, at: Date.now()});
  if (S.cur === sid) renderDirs();
  C.toast('已添加文件夹 ' + tilde(d.dir));
};
let dirsKey = '';
function renderDirs() {
  let list = [];
  if (S.cur && !body.classList.contains('h-home')) {
    const rd = S.runner && Array.isArray(S.runner.dirs) ? S.runner.dirs : null, mine = K.dirsBy[S.cur] || [];
    list = rd ? [...rd, ...mine.filter(x => Date.now() - x.at < 15000).map(x => x.dir)] : mine.map(x => x.dir);
    list = [...new Set(list.filter(x => typeof x === 'string' && x))];
  }
  const key = list.join('\n');
  if (key === dirsKey) return;
  dirsKey = key;
  keepBottom(() => {
    dirsEl.innerHTML = list.map(p => `<span class="k-dir" title="${esc(p)}">${IC.folder}<span>${esc(tilde(p))}</span></span>`).join('');
    dirsEl.hidden = !list.length;
  });
}

/* ================= 8. 时间线“简洁 / 详细” ================= */
C.applyDensity = () => {
  const simple = store.get('pref.density', 'detail') === 'simple';
  body.classList.toggle('k-simple', simple);
  if (S.cur) safe(planTool)(K.plans.size > 0);  // 修复线：简洁档收起 ExitPlanMode 工具行
  const dz = C.dz;
  if (!dz) return;
  /* 简洁档：已结束的轮次过程收起；回到详细档时恢复之前“展开全部过程”的选择 */
  if (simple && dz.allOpen) { store.set('k.densityAll', true); dz.setAll(false); }
  else if (!simple && store.get('k.densityAll', false)) { store.set('k.densityAll', null); dz.setAll(true); }
};
addEventListener('cwb:pref', e => { if (e.detail && e.detail.key === 'density') safe(C.applyDensity)(); });
addEventListener('storage', e => { if (e.key === 'cwb.pref.density') safe(C.applyDensity)(); });

/* ================= 9. 压缩进度与摘要 ================= */
function compactEv(ev) {
  let el = K.cmp && K.cmp.isConnected && K.cmp._sid === S.cur ? K.cmp : null;
  if (ev.phase === 'start' && el && el._done) el = null;
  if (!el) {
    el = mk('div', 'it k-cmp'); el._sid = S.cur;
    if (!S.tl) { S.tl = mk('div', 'tl'); feed.appendChild(S.tl); for (const p of S.pendings) feed.appendChild(p); }
    keepBottom(() => S.tl.appendChild(el));
    K.cmp = el;
  }
  if (ev.phase === 'start') {
    el.className = 'it run k-cmp';
    el.innerHTML = '<div class="k-cmph"><i class="k-spin"></i><span>正在压缩上下文…</span></div>';
  } else {
    el._done = true;
    keepView(() => fillCmp(el, ev.summary));
  }
}
function fillCmp(el, summary) {
  const s = String(summary || '').trim();
  el.className = 'it ok k-cmp done';
  el.innerHTML = s ? `<button type="button" class="k-cmph" aria-expanded="false">${IC.chev}<span>已压缩上下文</span></button><div class="k-cmpb md" hidden></div>`
    : '<div class="k-cmph"><span>已压缩上下文</span></div>';
  if (s) { const b = el.querySelector('.k-cmpb'); b.innerHTML = C.md(s); try { C.enhance(b); } catch {} }
}
/* 修复线：重开对话后，CLI 原文“Compacted (…)”那一行换成可展开的“已压缩上下文”。
   摘要来自 open 返回的 compacts（后端落盘的 PostCompact 摘要），按压缩完成时间（条目的 at）对上，两分钟内取最近的一条 */
function restoreCmp() {
  const list = K.cmpSaved[S.cur];
  if (!Array.isArray(list) || !list.length) return;
  for (const it of S.byId.values()) {
    if (it.k !== 'note' || !it.at || !/^Compacted \(/.test(it.text || '')) continue;
    const el = feed.querySelector(`[data-id="${CSS.escape(it.id)}"]`);
    if (!el || el.classList.contains('k-cmphide')) continue;
    let best = null, bd = 120000;
    for (const c of list) { const d = Math.abs((num(c.t) || 0) - it.at); if (d < bd && String(c.summary || '').trim()) { best = c; bd = d; } }
    if (!best) continue;
    const row = mk('div'); row._sid = S.cur; row._done = true;
    fillCmp(row, best.summary);
    keepView(() => { el.classList.add('k-cmphide'); el.after(row); });
  }
}

/* 第十五轮联调：压缩事件比记录先到，“已压缩上下文”会排在 /compact 命令行前面，后面还跟一行 CLI 原文“Compacted (ctrl+o …)”。
   记录到了以后把这一行挪到原文那行的位置，原文那行收起 */
function tidyCmp() {
  const el = K.cmp;
  if (!el || !el.isConnected || !el._done || el._sid !== S.cur) return;
  let hit = null;
  for (let n = el.nextElementSibling; n; n = n.nextElementSibling) if (n.classList.contains('it') && /^Compacted \(/.test((n.textContent || '').trim())) hit = n;
  if (!hit) return;
  keepView(() => { hit.classList.add('k-cmphide'); hit.after(el); });
}

/* ================= 接入 ================= */
function decoNow() { if (!S.cur) return; for (const f of [syncPlans, decoEnds, decoUsers, renderBanner, renderDirs, tidyCmp, restoreCmp]) safe(f)(); }  // 各管各的，一步出错不影响别的
const deco = soon(decoNow);
chain('onLoad', d => {
  if (K.tcSid !== d.sid) { K.tc = null; K.tcSid = null; K.tcSeq++; }
  K.cmp = null; banKey = '\u0000';  // 11-18：换对话后必须重画横幅。原来清成空串，新对话没错误时算出的也是空串，横幅不收起，点 × 也关不掉
  if (Array.isArray(d.compacts)) K.cmpSaved[d.sid] = d.compacts;  // 修复线：落盘的压缩摘要
  if (d.runner && typeof d.runner === 'object' && 'ctx' in d.runner) K.ctxBy[d.sid] = d.runner.ctx;
  decoNow(); renderRing(); checkTyped();
  loadTC(d.sid);
});
chain('onPrepend', () => { decoNow(); restoreRewinds(); });
chain('onEvent', ev => {
  if (!ev) return;
  if (ev.type === 'runner' && ev.sid) {
    const st = ev.state || {}, was = !!S.busyMap[ev.sid], now = !!(st.alive && st.busy);
    if ('ctx' in st) K.ctxBy[ev.sid] = st.ctx;
    if (ev.sid === S.cur) { if (was && !now) scheduleTC([1500, 6000]); queueMicrotask(safe(() => { renderRing(); renderDirs(); })); deco(); }
  } else if (ev.type === 'ctx' && ev.sid) {
    K.ctxBy[ev.sid] = ev.ctx || null;
    if (ev.sid === S.cur) queueMicrotask(safe(renderRing));
  } else if (ev.type === 'items' && ev.sid === S.cur) {
    if ((ev.patch || []).some(p => 'fork' in p)) scheduleTC([800, 5000]);
    deco();
  } else if (ev.type === 'perm' || ev.type === 'perm-done') deco();
  else if (ev.type === 'compact' && ev.sid === S.cur) queueMicrotask(safe(() => compactEv(ev)));
});
chain('onExport', f => {
  f.querySelectorAll('.k-uact, .k-end, .k-plan, .k-undo').forEach(x => x.remove());
  f.querySelectorAll('.k-planhide').forEach(x => x.classList.remove('k-planhide'));
  f.querySelectorAll('.k-cmphide').forEach(x => x.classList.remove('k-cmphide'));
  /* 第十五轮联调：导出的网页不带 ux-k.css，留下的回退行、压缩行里的图标没有尺寸会撑得很大；去掉图标，给一点行内样式，摘要直接展开 */
  f.querySelectorAll('.k-rwrow svg, .k-cmp svg, .k-cmp .k-spin').forEach(x => x.remove());
  f.querySelectorAll('.k-rwrow').forEach(x => { x.style.cssText = 'margin:6px 0;color:var(--muted);font-size:13px'; });
  f.querySelectorAll('.k-cmp .k-cmpb').forEach(x => { x.hidden = false; });
});
/* 慢速巡检：结束行在没有推送时也可能变成“已完成”（别处运行的对话超时）；小卡开着时走缓存倒计时 */
setInterval(safe(() => {
  if (S.cur && !document.hidden) { decoEnds(); decoUsers(); planWait(); }
  if (K.ctxOpen) fillCtx();
}), 1000);
safe(C.applyDensity)();
setTimeout(() => loadBlocked(2), 1200);
C.k = {loadTC, loadBlocked, renderRing, renderBanner, renderDirs, compactEv, syncPlans, openRewind, decorate: decoNow, state: K};
})();
