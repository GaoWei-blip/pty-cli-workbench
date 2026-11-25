(() => {
'use strict';
/* 第十二轮 C：布局与导航。
   1. 右栏：分隔线悬停高亮、双击复位 520、放大/还原（状态记在 localStorage）
   2. 左栏：收起/展开（顶栏最左按钮、⌘B）、右边缘拖宽 220～420、双击复位 272
   3. 在提问之间跳转：⌥↑ / ⌥↓，主区滚动条旁的提问刻度轨
   4. 左栏“有新回复”圆点、进行中对话显示已用时间（后台 public_state.since）
      第十三轮 E：未读改由服务端记录 done_at，这里只存 seen_at；Dock 角标把未读也算上
   只用已有钩子（onSide / onLoad / onEvent，串在原有处理之后）和 app2.js 导出的 C.fitPane，不改 app.js。 */
const C = window.CWB;
if (!C) return;
const {S, $, api} = C;
const ls = {get(k, d) { try { const v = localStorage.getItem('cwbc.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { if (v == null) localStorage.removeItem('cwbc.' + k); else localStorage.setItem('cwbc.' + k, JSON.stringify(v)); } catch {} }};
const body = document.body, side = $('#side'), main = $('#main'), pane = $('#rpane'), scroll = $('#scroll'), feed = $('#feed');
const svg = d => `<svg viewBox="0 0 16 16" aria-hidden="true">${d}</svg>`;
const chain = (name, fn) => { const prev = C[name]; C[name] = (...a) => { if (prev) prev(...a); try { fn(...a); } catch (e) { console.error(e); } }; };
const fitPane = () => { if (C.fitPane) C.fitPane(); };
const MAIN_MIN = 420, PANE_MIN = 300, PANE_DEF = 520, SIDE_DEF = 272, SIDE_MIN = 220, SIDE_MAX = 420;
const mainHidden = () => body.classList.contains('rp') && body.classList.contains('ux-rpmax');
const modalOpen = () => !$('#modal').hidden || !$('#palette').hidden;
body.classList.add('ux-notr');  // 首次套用保存的布局时不播过渡
requestAnimationFrame(() => requestAnimationFrame(() => body.classList.remove('ux-notr')));

/* ================= 1. 右栏 ================= */
const rgrip = $('#rgrip');
rgrip.addEventListener('mousedown', e => {
  if (e.button !== 0) return;
  body.classList.add('ux-rdrag');
  addEventListener('mouseup', () => body.classList.remove('ux-rdrag'), {once: true});
});
rgrip.addEventListener('dblclick', () => { try { localStorage.setItem('cwb2.width', String(PANE_DEF)); } catch {} fitPane(); });

const IC_MAX = svg('<path d="M9.5 2.5h4v4M13.5 2.5 9.2 6.8M6.5 13.5h-4v-4M2.5 13.5l4.3-4.3"/>');
const IC_RES = svg('<path d="M13.5 6.5h-4v-4M9.5 6.5l4-4M2.5 9.5h4v4M6.5 9.5l-4 4"/>');
const maxBtn = document.createElement('button');
maxBtn.id = 'uxRmax'; maxBtn.className = 'icon'; maxBtn.type = 'button';
$('#rclose').before(maxBtn);
function setMax(on, save) {
  body.classList.toggle('ux-rpmax', on);
  maxBtn.innerHTML = on ? IC_RES : IC_MAX;
  maxBtn.title = on ? '还原' : '放大';
  if (save) ls.set('rpMax', on ? 1 : null);
}
maxBtn.onclick = () => setMax(!body.classList.contains('ux-rpmax'), true);
setMax(!!ls.get('rpMax', null), false);
/* 11-18：右栏放大时会话区是隐藏的。在侧栏换对话、展开侧栏、点“在对话中定位”都是要看会话区，先还原，免得像是会话区被挤掉了 */
const unMax = () => { if (body.classList.contains('ux-rpmax')) setMax(false, true); };
let maxSid = S.cur;
chain('onLoad', d => { if (d && d.sid !== maxSid) { maxSid = d.sid; unMax(); } });
document.addEventListener('click', e => { if (e.target.closest && e.target.closest('#rbody [data-a="loc"]')) unMax(); }, true);

/* 主区被放大的右栏盖住时是 display:none，滚动位置会丢：平时记下，回来时恢复（原来在底部就回到底部） */
let lastScroll = {sid: null, top: 0, bottom: true};
scroll.addEventListener('scroll', () => {
  if (!scroll.clientHeight) return;
  lastScroll = {sid: S.cur, top: scroll.scrollTop, bottom: scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 24};
}, {passive: true});
let wasHidden = mainHidden(), wasRp = body.classList.contains('rp');
new MutationObserver(() => {
  const h = mainHidden(), rp = body.classList.contains('rp');
  if (rp !== wasRp) { wasRp = rp; applySide(); fitPane(); }
  if (h === wasHidden) return;
  wasHidden = h;
  if (!h) {
    scroll.scrollTop = lastScroll.sid === S.cur && !lastScroll.bottom ? lastScroll.top : scroll.scrollHeight;
  } else hideTip();
  sched();
}).observe(body, {attributes: true, attributeFilter: ['class']});

/* ================= 2. 左栏 ================= */
const sideBtn = document.createElement('button');
sideBtn.id = 'uxSideBtn'; sideBtn.className = 'icon'; sideBtn.type = 'button';
sideBtn.innerHTML = svg('<rect x="2" y="2.75" width="12" height="10.5" rx="2.2"/><path d="M6.25 2.75v10.5"/>');
$('#top').prepend(sideBtn);
const lgrip = document.createElement('div');
lgrip.id = 'uxLgrip';
side.appendChild(lgrip);
const collapsed = () => body.classList.contains('ux-sidec');
const roomFor = () => innerWidth - MAIN_MIN - (pane.hidden ? 0 : PANE_MIN);
const clampSide = w => Math.max(SIDE_MIN, Math.min(SIDE_MAX, roomFor(), w));
const defSide = () => matchMedia('(max-width:760px)').matches ? SIDE_MIN : SIDE_DEF;
/* 实际宽度：没拖过就用样式表默认值；拖过的按窗口宽度收一收，保证主区有地方 */
const sideTarget = () => { const w = ls.get('sideW', 0); return w ? clampSide(w) : 0; };
function applySide() {
  const w = sideTarget(), px = w ? w + 'px' : '';
  if (side.style.width !== px) side.style.width = px;
  if (!collapsed()) side.style.setProperty('--ux-sw', (w || defSide()) + 'px');
}
/* 右栏宽度按侧栏的目标宽度先算好，过渡时两边同步变化（和 app2.js 的 fitPane 同一规则） */
function paneFor(sw) {
  if (pane.hidden || mainHidden()) return;
  let w = PANE_DEF; try { const v = JSON.parse(localStorage.getItem('cwb2.width')); if (v) w = v; } catch {}
  pane.style.width = Math.max(PANE_MIN, Math.min(Math.max(PANE_MIN, innerWidth - sw - MAIN_MIN), w)) + 'px';
}
let sideTimer = 0;
function setSide(c, save) {
  if (c === collapsed()) return;
  if (!c) unMax();
  const target = c ? 0 : (sideTarget() || defSide());
  side.style.setProperty('--ux-sw', (c ? side.offsetWidth || defSide() : target) + 'px');
  body.classList.add('ux-sidet');
  body.classList.toggle('ux-sidec', c);
  paneFor(target);
  sideBtn.title = c ? '展开侧栏' : '收起侧栏';
  if (save) ls.set('sideC', c ? 1 : null);
  clearTimeout(sideTimer);
  sideTimer = setTimeout(() => { body.classList.remove('ux-sidet'); fitPane(); sched(); }, 200);
}
sideBtn.onclick = () => setSide(!collapsed(), true);
applySide();
if (ls.get('sideC', null)) { body.classList.add('ux-sidec'); fitPane(); }
sideBtn.title = collapsed() ? '展开侧栏' : '收起侧栏';
addEventListener('resize', () => { applySide(); fitPane(); });

lgrip.addEventListener('mousedown', e => {
  if (e.button !== 0) return;
  e.preventDefault();
  const x0 = e.clientX, w0 = side.offsetWidth;
  let moved = false;
  body.classList.add('ux-ldrag');
  const mv = ev => {
    if (!ev.buttons) return up();
    const w = clampSide(w0 + ev.clientX - x0);
    if (w === side.offsetWidth) return;
    moved = true;
    side.style.width = w + 'px'; side.style.setProperty('--ux-sw', w + 'px');
    fitPane();
  };
  const up = () => {
    removeEventListener('mousemove', mv); removeEventListener('mouseup', up);
    body.classList.remove('ux-ldrag');
    if (moved) ls.set('sideW', side.offsetWidth);
    fitPane(); sched();
  };
  addEventListener('mousemove', mv); addEventListener('mouseup', up);
});
lgrip.addEventListener('dblclick', () => { ls.set('sideW', null); applySide(); fitPane(); sched(); });

/* ================= 3. 在提问之间跳转 ================= */
/* 用户气泡：时间线顶层的 .user（子代理过程里的提问不算） */
const bubbles = () => [...feed.querySelectorAll('.user')].filter(el => !el.closest('.tl'));
const bubbleText = el => { const t = el.querySelector('.utext'); return ((t ? t.textContent : el.textContent) || '').replace(/\s+/g, ' ').trim(); };
function measure() {
  const top0 = scroll.getBoundingClientRect().top - scroll.scrollTop;
  return bubbles().map(el => ({el, y: el.getBoundingClientRect().top - top0}));
}
const GAP = 12;  // 跳过去后气泡离顶边留一点空
const goY = y => { scroll.scrollTop = Math.max(0, y - GAP); };
let jumping = false;
async function jumpQ(dir) {
  const st = scroll.scrollTop, ms = measure();
  if (dir > 0) {
    const t = ms.find(m => m.y - GAP > st + 4);
    if (t) goY(t.y); else scroll.scrollTop = scroll.scrollHeight;
    return;
  }
  let t = null;
  for (const m of ms) if (m.y - GAP < st - 4) t = m;
  if (t) return goY(t.y);
  /* 上面没有了但还有更早的没加载：一页页往前加载，跳到新露出来的最后一条提问（紧挨在原来内容上方） */
  if (!S.hidden || !C.loadOlder || jumping) { if (!S.hidden) scroll.scrollTop = 0; return; }
  jumping = true;
  try {
    const old = new Set(ms.map(m => m.el));
    for (let i = 0; i < 30 && S.hidden; i++) {
      if (!await C.loadOlder()) break;
      const fresh = measure().filter(m => !old.has(m.el));
      if (fresh.length) return goY(fresh[fresh.length - 1].y);
    }
  } finally { jumping = false; }
}
function canJump(dir) {
  if (!S.cur || mainHidden() || modalOpen() || body.classList.contains('nosess')) return false;
  const a = document.activeElement;
  if (a && a.id === 'input') {
    if (S.pop) return false;  // @、/ 补全打开时上下键归补全
    /* 多行文字：⌥↑ 只在光标位于首行时接管，⌥↓ 只在末行时接管，其余照常按段落移动光标 */
    return dir < 0 ? !a.value.slice(0, a.selectionStart).includes('\n') : !a.value.slice(a.selectionEnd).includes('\n');
  }
  if (a && (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName))) return false;
  return true;
}

/* 刻度轨：主区滚动条左边，每个提问一个刻度，按它在整段内容里的相对位置摆放 */
const rail = document.createElement('div');
rail.id = 'uxRail'; rail.hidden = true;
main.appendChild(rail);
const tip = document.createElement('div');
tip.id = 'uxTip'; tip.hidden = true;
body.appendChild(tip);
let ticks = [], marks = [], railTimer = 0, railBox = '';
function hideTip() { tip.hidden = true; }
function updateRail() {
  if (!S.cur || mainHidden() || !scroll.clientHeight || body.classList.contains('nosess')) { rail.hidden = true; hideTip(); return; }
  const H = scroll.scrollHeight, h = scroll.clientHeight;
  marks = H > h + 1 ? measure() : [];
  if (!marks.length) { rail.hidden = true; hideTip(); return; }
  const box = `${scroll.offsetTop}|${h}|${scroll.offsetWidth - scroll.clientWidth}`;
  if (box !== railBox) {
    railBox = box;
    rail.style.top = scroll.offsetTop + 'px';
    rail.style.height = h + 'px';
    rail.style.right = (scroll.offsetWidth - scroll.clientWidth) + 'px';
  }
  while (ticks.length < marks.length) { const t = document.createElement('i'); t.dataset.i = ticks.length; rail.appendChild(t); ticks.push(t); }
  while (ticks.length > marks.length) ticks.pop().remove();
  marks.forEach((m, i) => {
    const top = Math.max(3, Math.min(h - 3, Math.round(m.y / H * h)));
    if (ticks[i]._top !== top) { ticks[i].style.top = top + 'px'; ticks[i]._top = top; }
  });
  rail.hidden = false;
}
/* 节流：内容一直在变（实时输出）时最多约 0.2 秒更新一次；只改变化了的刻度位置，不整条重画 */
function sched() {
  if (railTimer) return;
  railTimer = setTimeout(() => requestAnimationFrame(() => { railTimer = 0; updateRail(); }), 180);
}
new MutationObserver(sched).observe(feed, {childList: true});
if (window.ResizeObserver) { const ro = new ResizeObserver(sched); ro.observe(feed); ro.observe(scroll); }
rail.addEventListener('mouseover', e => {
  const t = e.target.closest('#uxRail i'), m = t && marks[+t.dataset.i];
  if (!m) return;
  tip.textContent = bubbleText(m.el).slice(0, 120) || '（图片）';
  tip.hidden = false;
  const r = t.getBoundingClientRect();
  tip.style.left = Math.max(8, r.left - 8 - tip.offsetWidth) + 'px';
  tip.style.top = Math.max(8, Math.min(innerHeight - tip.offsetHeight - 8, r.top + r.height / 2 - tip.offsetHeight / 2)) + 'px';
});
rail.addEventListener('mouseout', e => { if (!e.relatedTarget || !e.relatedTarget.closest || !e.relatedTarget.closest('#uxRail i')) hideTip(); });
rail.addEventListener('mousedown', e => e.preventDefault());  // 点刻度不抢走输入框的焦点
rail.addEventListener('click', e => {
  const t = e.target.closest('#uxRail i'), m = t && marks[+t.dataset.i];
  if (!m || !m.el.isConnected) return;
  goY(measure().find(x => x.el === m.el).y);
});

/* ================= 4. 有新回复、已用时间 ================= */
/* 第十三轮 E：服务端在一轮结束（Stop、出错）时记 done_at[sid]（秒，左栏数据、runner 状态和 marks 事件里都带着），
   本地只存看过的时间 seen_at（localStorage cwbc.seenAt），done_at > seen_at 即未读。
   第一次用新规则时记基准时间 '*'，之前结束的都算看过；第十二轮 C 的本地未读 cwbc.unread 不再使用。 */
const doneAt = {};
const seenAt = ls.get('seenAt', null) || {'*': Date.now() / 1000};
ls.set('seenAt', seenAt); ls.set('unread', null);
const saveSeen = () => ls.set('seenAt', seenAt);
const since = {}, seen = {};  // since：后台给的本轮开始时间；seen：没拿到 since 时，亲眼看到变忙的时间
const away = () => document.hidden || !document.hasFocus();
const doneOf = sid => { const s = C.findSession(sid); return Math.max(doneAt[sid] || 0, (s && s.done_at) || 0); };
// 11-25 用户要求：委派出来的子对话（左栏数据带 cwbParent）跑完不标未读，结果由父对话收；普通对话照旧有圆点
const isKid = sid => { const s = C.findSession(sid); return !!(s && s.cwbParent); };
const isUnread = sid => { if (isKid(sid)) return false; const d = doneOf(sid); return d > 0 && d > (seenAt[sid] != null ? seenAt[sid] : seenAt['*']); };
const unreadShown = sid => isUnread(sid) && !(sid === S.cur && !away());
function unreadCount() {
  let n = 0;
  for (const p of S.projects || []) for (const s of p.sessions || []) if (!s.archived && unreadShown(s.sid)) n++;
  return n;
}
let lastBadge = -1;
function badge() { const n = unreadCount(); if (n !== lastBadge) { lastBadge = n; if (C.showModal) C.showModal(); } }  // 角标由 app.js 的 showModal 发出（授权数 + 未读数）
function markSeen(sid) { if (sid && isUnread(sid)) { seenAt[sid] = Math.max(Date.now() / 1000, doneOf(sid)); saveSeen(); decorate(); } }
const pad = n => String(n).padStart(2, '0');
const fmtEl = sec => { const s = Math.max(0, Math.floor(sec)), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`; };
const startOf = sid => since[sid] || seen[sid] || 0;
let seedAt = 0;
async function seed() {
  seedAt = Date.now();
  try {
    const d = await api('tasks');
    for (const t of d.tasks || []) if (t.state && 'since' in t.state) since[t.sid] = t.state.since || 0;
    decorate();
  } catch {}
}
function decorate() {
  const now = Date.now() / 1000;
  let need = false;
  for (const row of document.querySelectorAll('#projects .srow[data-sid]')) {
    const sid = row.dataset.sid, s = C.findSession(sid);
    const want = unreadShown(sid);  // 第十三轮 E
    let dot = row.querySelector(':scope > .ux-unread');
    if (want && !dot) {
      dot = document.createElement('span'); dot.className = 'ux-unread';
      row.insertBefore(dot, row.querySelector(':scope > .stime'));
    } else if (!want && dot) dot.remove();
    const tm = row.querySelector(':scope > .stime');
    if (!tm) continue;
    const busy = !!(s && s.busy);
    if (busy && !(sid in since)) need = true;
    const t0 = busy ? startOf(sid) : 0;
    if (t0) {
      const txt = fmtEl(now - t0);
      if (tm.dataset.ux == null) tm.dataset.ux = tm.textContent;
      if (tm.textContent !== txt) tm.textContent = txt;
    } else if (tm.dataset.ux != null) { tm.textContent = tm.dataset.ux; delete tm.dataset.ux; }
  }
  if (need && Date.now() - seedAt > 10000) seed();
  badge();
}
const noteDone = (sid, t) => { if (t > (doneAt[sid] || 0)) { doneAt[sid] = t; if (sid === S.cur && !away()) markSeen(sid); } };
chain('onEvent', ev => {
  if (ev.type === 'rekey') {
    for (const m of [since, seen]) if (ev.old in m) { m[ev.new] = m[ev.old]; delete m[ev.old]; }
    return;
  }
  if (ev.type === 'marks' && ev.sid && 'done_at' in ev) { noteDone(ev.sid, ev.done_at || 0); Promise.resolve().then(decorate); return; }  // 第十三轮 E
  if (ev.type !== 'runner' || !ev.sid) return;
  const st = ev.state || {}, sid = ev.sid, busy = !!(st.alive && st.busy);
  /* 这个钩子在 setSideFlags 之前跑：S.busyMap 里还是上一次的状态 */
  const s = C.findSession(sid), prev = S.busyMap[sid] != null ? S.busyMap[sid] : !!(s && s.busy);
  if ('since' in st) since[sid] = st.since || 0;
  if (busy && !prev) seen[sid] = Date.now() / 1000;
  if (!busy) delete seen[sid];
  if (st.done_at) noteDone(sid, st.done_at);  // 第十三轮 E
  Promise.resolve().then(decorate);  // 等 setSideFlags 更新完再画
});
chain('onLoad', d => {
  if (d && d.runner && 'since' in d.runner) since[d.sid] = d.runner.since || 0;
  if (d && d.runner && d.runner.done_at) noteDone(d.sid, d.runner.done_at);
  if (d && !away()) markSeen(d.sid);
  sched();
});
chain('onSide', () => {
  if (S.projects && S.projects.length) {
    let ch = false;
    for (const sid of Object.keys(seenAt)) if (sid !== '*' && !C.findSession(sid)) { delete seenAt[sid]; ch = true; }
    if (ch) saveSeen();
  }
  decorate();
});
const back = () => { if (!away()) markSeen(S.cur); };
addEventListener('focus', back);
document.addEventListener('visibilitychange', back);
setInterval(decorate, 1000);
seed();

/* ================= 快捷键 ================= */
document.addEventListener('keydown', e => {
  if (e.defaultPrevented) return;
  const k = (e.key || '').toLowerCase();
  if (e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && (e.code === 'KeyB' || k === 'b')) {  // ⌘B（Ctrl+B 是输入框里的光标左移，不接管）
    e.preventDefault(); setSide(!collapsed(), true); return;
  }
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && k === 'k' && collapsed()) setSide(false, true);  // ⌘K 搜索对话：先展开侧栏
  if (e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown') && !e.isComposing) {
    const dir = e.key === 'ArrowUp' ? -1 : 1;
    if (!canJump(dir)) return;
    e.preventDefault(); e.stopPropagation();
    jumpQ(dir);
  }
}, true);

C.ux = {setSide, setMax, jumpQ, updateRail, decorate, isUnread, markSeen};
C.unreadCount = unreadCount;  // 第十三轮 E：app.js 的 showModal 把它加进 Dock 角标
sched();
})();
