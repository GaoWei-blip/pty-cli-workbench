(() => {
'use strict';
/* 第十二轮 D：时间线阅读与输入体验
   1. 过程折叠：已结束的轮次把中间过程收成一行“执行了 N 步 · 用时 X”，点击展开 / 收起；“⋯”里有全局开关。
   2. 最终回复的悬停操作条：复制、引用、从这里分叉。
   3. 发送按钮：回复中且输入框有内容时变成“排队”，左边出“立即发送”（⌘Enter）。
   4. 工具标题前的类型图标；Edit / MultiEdit / Write 显示增删行数。
   时间线元素不加动画和过渡。折叠只在一轮结束（一次）或用户点击时发生，并按视口锚点补偿滚动，不跳。
   对 app.js 的接入点：renderTool / prependItems / updateRunner / exportHtml 里各一行 hook，末尾导出几个函数。 */
const C = window.CWB;
if (!C) return;
const {S, $} = C;
const feed = $('#feed'), sc = $('#scroll'), input = $('#input');

/* ================= 1. 过程折叠 ================= */
const SETTLE = 1200;  // 一轮停下后再等这么久才折：最后一段正文要过一两秒才写进记录
const ALL_KEY = 'cwb.dz.allOpen';
let allOpen = false;
try { allOpen = localStorage.getItem(ALL_KEY) === '1'; } catch {}
const isStart = it => (it.k === 'user' && !it.mid) || (it.k === 'sys' && it.turn);
/* 一直留在外面的条目：你在回复中插入的话、错误、命令及其输出、选择题、计划确认 */
const PIN_TOOLS = new Set(['AskUserQuestion', 'ExitPlanMode']);
const pinned = it => it.k === 'user' || it.k === 'sys' || it.k === 'error' || it.k === 'cmd' || it.k === 'note' || (it.k === 'tool' && PIN_TOOLS.has(it.name));
const sealed = new Set();  // `${sid}|${组}`：已结束的轮次（只增不减）
const over = new Map();    // `${sid}|${组}` → 单轮手动展开(true) / 收起(false)，优先于全局开关
const A = new Map();       // 组 → 已套用的状态 {sig, hide, finals, row, bar, fork, tend}
const dirty = new Set();   // 被重画过的条目（patch 后是新元素，要重新套用）
let liveAt = 0, hov = null;
const sk = key => S.cur + '|' + key;
const isOpen = key => over.has(sk(key)) ? over.get(sk(key)) : allOpen;

/* 按记录顺序分轮：提问（或开启新一轮的系统消息）开头；最前面没有提问的一段算 lead */
function groups() {
  const out = []; let g = null;
  for (const it of S.byId.values()) {
    if (isStart(it)) { g = {key: it.id, start: it, items: []}; out.push(g); continue; }
    if (!g) { g = {key: 'lead', start: null, items: []}; out.push(g); }
    g.items.push(it);
  }
  return out;
}
/* 最终回复 = 最后一个工具调用之后的正文；它之前的思考、工具、叙述、待办都算过程。
   没有工具调用：不折。没有结论正文（最后是工具，常见于中断、出错）：最后一步留在外面。 */
function plan(g) {
  const body = g.items;
  let last = -1, n = 0;
  body.forEach((it, i) => { if (it.k === 'tool') { last = i; n++; } });
  const hasFinal = body.slice(last + 1).some(it => it.k === 'text');
  const hide = [], finals = [];
  body.forEach((it, i) => {
    if (i > last && it.k === 'text') { finals.push(it.id); return; }
    if (!n || pinned(it) || (i === last && !hasFinal)) return;
    hide.push(it.id);
  });
  return {n, hide, finals};
}
function elMap() {
  const m = new Map();
  feed.querySelectorAll(':scope > [data-id], :scope > .tl > [data-id]').forEach(el => m.set(el.dataset.id, el));
  return m;
}
function job(g, lastG) {
  const p = plan(g), t = g.start && g.start._turn;
  const tend = g === lastG ? null : (t && t.el) || null;  // 最后一轮的结束行是 S.spinEl，另外处理
  const fork = !!(g.start && g.start.fork && t && p.finals.length && C.turnInfo(t).cls === 'ok');
  const lastIt = g.items[g.items.length - 1];
  const sig = [isOpen(g.key) ? 1 : 0, g.items.length, lastIt ? lastIt.id : '', g.start && g.start.stat ? g.start.stat.secs : '', fork ? 1 : 0].join('|');
  return {g, p, sig, fork, tend};
}
function undo(st) {
  st.hide.forEach(el => el.classList.remove('dz-h'));
  st.finals.forEach(el => el.classList.remove('dz-final'));
  if (st.row) st.row.remove();
  if (st.bar) st.bar.remove();
  if (st.tend) st.tend.removeAttribute('data-dzbar');
}
function mkRow() {
  const r = document.createElement('div');
  r.className = 'dz-row'; r.tabIndex = 0; r.setAttribute('role', 'button');
  r.innerHTML = '<i class="dz-chev"></i><span class="dz-n"></span><span class="dz-t"></span>';
  return r;
}
function mkBar(key, fork) {
  const b = document.createElement('div');
  b.className = 'dz-bar' + (hov === key ? ' on' : ''); b.dataset.dzg = key;
  b.innerHTML = '<button type="button" data-a="copy">复制</button><button type="button" data-a="quote">引用</button>' +
    (fork ? '<button type="button" data-a="fork">从这里分叉</button>' : '');
  return b;
}
function applyJob(j, els, shown) {
  const {g, p} = j, st = A.get(g.key);
  const open = isOpen(g.key);
  const hide = p.hide.map(id => els.get(id)).filter(Boolean);
  const finals = p.finals.map(id => els.get(id)).filter(Boolean);
  const hs = new Set(hide), fs = new Set(finals);
  if (st) {
    st.hide.forEach(el => { if (!hs.has(el) && el.classList.contains('dz-h')) { el.classList.remove('dz-h'); if (el.isConnected) shown.push(el); } });
    st.finals.forEach(el => { if (!fs.has(el)) el.classList.remove('dz-final'); });
    if (st.bar) st.bar.remove();
    if (st.tend && st.tend !== j.tend) st.tend.removeAttribute('data-dzbar');
  }
  let row = st && st.row;
  if (hide.length) {
    if (!row) row = mkRow();
    row.dataset.dzg = g.key;
    if (row.nextElementSibling !== hide[0]) hide[0].before(row);
    const secs = g.start && g.start.stat && g.start.stat.secs;
    const n = `执行了 ${p.n} 步`, tt = secs ? '· 用时 ' + C.fmtSecs(secs) : '';
    const nEl = row.querySelector('.dz-n'), tEl = row.querySelector('.dz-t');
    if (nEl.textContent !== n) nEl.textContent = n;
    if (tEl.textContent !== tt) tEl.textContent = tt;
    row.classList.toggle('open', open); row.setAttribute('aria-expanded', open ? 'true' : 'false');
    for (const el of hide) {
      el._dzg = g.key;
      if (open && el.classList.contains('dz-h')) { el.classList.remove('dz-h'); shown.push(el); }
      else if (!open) el.classList.add('dz-h');
    }
  } else if (row) { row.remove(); row = null; }
  let bar = null;
  for (const el of finals) { el.classList.add('dz-final'); el.dataset.dzg = g.key; }
  if (finals.length) { bar = mkBar(g.key, j.fork); finals[0].appendChild(bar); }
  if (j.tend) j.tend.toggleAttribute('data-dzbar', !!(bar && j.fork));
  A.set(g.key, {sig: j.sig, hide, finals, row, bar, fork: j.fork, tend: j.tend});
}
const vbox = () => sc.getBoundingClientRect();
function inView(el, v) { const r = el.getBoundingClientRect(); return r.height > 0 && r.bottom > v.top && r.top < v.bottom; }
/* 锚点：视口里第一个之后仍然看得见的元素（不选要收起的） */
function pickAnchor(skip) {
  const v = vbox();
  for (const el of feed.querySelectorAll(':scope > *, :scope > .tl > *')) {
    if (el.classList.contains('tl') || skip.has(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.top >= v.bottom) return null;
    if (r.height > 0 && r.bottom > v.top) return el;
  }
  return null;
}
/* 套用一批：view = 'none'（载入 / 显示更早，由调用方定位）| 'keep'（停在底部就留在底部，否则视口不动）| 元素（以它为锚） */
function run(jobs, els, view) {
  if (!jobs.length) return;
  const keep = view !== 'none';
  const stick = view === 'keep' && C.nearBottom();
  let anc = view instanceof Element ? view : null, y0 = 0;
  if (keep && !stick && !anc) {
    const skip = new Set();
    for (const j of jobs) if (!isOpen(j.g.key)) j.p.hide.forEach(id => { const e = els.get(id); if (e) skip.add(e); });
    anc = pickAnchor(skip);
    if (!anc) {  // 视口里只剩要收起的过程：改以这一轮的折叠行为锚，收起后它留在视口顶部
      const v = vbox(), e = [...skip].find(x => inView(x, v));
      const st = e && A.get(e._dzg);
      if (st && st.row) { anc = st.row; y0 = v.top + 8; }
    }
  }
  if (anc && !y0) y0 = anc.getBoundingClientRect().top;
  const shown = [];
  for (const j of jobs) applyJob(j, els, shown);
  for (const el of shown) C.addMore(el);  // 收着时量不到高度，展开后补上长内容的“展开”按钮
  if (stick) C.toBottom();
  else if (anc && anc.isConnected) sc.scrollTop += anc.getBoundingClientRect().top - y0;
}
function reconcile(mode) {
  if (!S.cur || !feed.isConnected) return;
  const gs = groups(), lastG = gs[gs.length - 1];
  const live = document.body.classList.contains('turn-live'), now = Date.now();
  if (live) liveAt = now;
  const keys = new Set(gs.map(g => g.key));
  let stale = false;
  for (const [k, st] of A) if (!keys.has(k)) { undo(st); A.delete(k); stale = true; }  // “显示更早”把开头那段并进了上一轮
  const jobs = [];
  for (const g of gs) {
    if (!sealed.has(sk(g.key))) {
      const done = g !== lastG || (!live && !S.liveEl && (mode === 'load' || now - liveAt > SETTLE));
      if (!done) continue;
      sealed.add(sk(g.key));
    }
    const j = job(g, lastG), st = A.get(g.key);
    const touched = dirty.size && (g.items.some(it => dirty.has(it.id)) || (g.start && dirty.has(g.start.id)));
    if (st && st.sig === j.sig && st.tend === j.tend && !touched) continue;
    jobs.push(j);
  }
  dirty.clear();
  if (jobs.length || stale) {
    const els = elMap(), view = mode === 'load' || mode === 'prepend' ? 'none' : 'keep';
    /* 刚结束的轮次：你正往上看它的过程（不在底部）时不收，免得眼前的内容消失（之后可以手动收起）；
       折叠行也先不插，等这段过程离开视口再插，插在视口外由锚点补偿，眼前的内容不下移 */
    let todo = jobs;
    if (view === 'keep' && !C.nearBottom()) {
      const v = vbox();
      todo = jobs.filter(j => {
        if (A.has(j.g.key) || !j.p.hide.some(id => { const e = els.get(id); return e && inView(e, v); })) return true;
        if (!allOpen && !over.has(sk(j.g.key))) over.set(sk(j.g.key), true);
        return false;
      });
      todo.forEach(j => { j.sig = job(j.g, lastG).sig; });
    }
    run(todo, els, view);
  }
  /* 最后一轮的结束行（S.spinEl）：有操作条时藏起开发线 A 的“从这里分叉”，统一到操作条 */
  const sp = S.spinEl, lst = lastG && A.get(lastG.key), has = !!(lst && lst.bar && lst.fork);
  if (sp && sp.hasAttribute('data-dzbar') !== has) sp.toggleAttribute('data-dzbar', has);
}
const safe = f => (...a) => { try { return f(...a); } catch (e) { console.error(e); } };
const rec = safe(reconcile);
/* 单轮 / 全局切换后重新套用（已结束的轮次） */
function rerun(filter, view) {
  const gs = groups(), lastG = gs[gs.length - 1], els = elMap();
  const jobs = gs.filter(g => sealed.has(sk(g.key)) && (!filter || filter.has(g.key))).map(g => job(g, lastG));
  run(jobs, els, view);
}
function toggleRow(row) {
  const key = row.dataset.dzg;
  over.set(sk(key), !isOpen(key));
  rerun(new Set([key]), row);
}
function setAll(v) {
  allOpen = v;
  try { localStorage.setItem(ALL_KEY, v ? '1' : '0'); } catch {}
  for (const k of [...over.keys()]) over.delete(k);
  rerun(null, 'keep');
}
/* 跳转目标在折叠里：先展开这一轮再定位（目录、搜索结果、子代理定位等都走 scrollIntoView） */
function reveal(h) {
  const key = h._dzg;
  if (!key || isOpen(key)) return;
  over.set(sk(key), true);
  rerun(new Set([key]), 'keep');
}
const sIV = Element.prototype.scrollIntoView;
Element.prototype.scrollIntoView = function (...a) {
  try { const h = this.closest && feed.contains(this) && this.closest('.dz-h'); if (h) reveal(h); } catch (e) { console.error(e); }
  return sIV.apply(this, a);
};

feed.addEventListener('click', e => {
  const row = e.target.closest('.dz-row');
  if (row) { e.preventDefault(); safe(toggleRow)(row); return; }
  const btn = e.target.closest('.dz-bar button[data-a]');
  if (btn) { e.stopPropagation(); safe(barAct)(btn); }
});
feed.addEventListener('keydown', e => {
  const row = e.target.closest && e.target.closest('.dz-row');
  if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); safe(toggleRow)(row); }
});

/* ================= 2. 最终回复的悬停操作条 ================= */
function setHover(k) {
  if (k === hov) return;
  const a = hov && A.get(hov); if (a && a.bar) a.bar.classList.remove('on');
  hov = k;
  const b = k && A.get(k); if (b && b.bar) b.bar.classList.add('on');
}
feed.addEventListener('mouseover', e => {
  const f = e.target.closest('.dz-final');
  if (f) setHover(f.dataset.dzg);
  else if (!e.target.classList.contains('tl')) setHover(null);  // 两段结论之间的空隙不算离开
});
feed.addEventListener('mouseleave', () => setHover(null));
function finalText(key) {
  const g = groups().find(x => x.key === key);
  if (!g) return {g: null, text: ''};
  return {g, text: plan(g).finals.map(id => S.byId.get(id)).filter(Boolean).map(it => it.text || '').join('\n\n')};
}
function barAct(btn) {
  const {g, text} = finalText(btn.closest('.dz-bar').dataset.dzg);
  if (!g) return;
  const a = btn.dataset.a;
  if (a === 'copy') C.copy(text, btn);
  else if (a === 'quote') { const qb = $('#quoteBtn'); qb.dataset.t = text; qb.click(); }  // 复用选中文字的引用逻辑
  else if (a === 'fork' && g.start && g.start.fork) C.forkAt(g.start.fork);
}

/* “⋯”菜单：展开全部过程 / 收起全部过程 */
const moreBtn = $('#moreBtn'), origMore = moreBtn.onclick;
moreBtn.onclick = e => {
  if (origMore) origMore.call(moreBtn, e);
  const m = S.menu;
  if (!m || !S.cur) return;
  const b = document.createElement('button');
  b.className = 'mi'; b.innerHTML = `<span>${allOpen ? '收起全部过程' : '展开全部过程'}</span>`;
  b.onclick = ev => { ev.stopPropagation(); C.closeMenu(); safe(setAll)(!allOpen); };
  const sep = m.querySelector('.msep');
  if (sep) m.insertBefore(b, sep); else m.appendChild(b);
};

C.dz = {setAll: v => safe(setAll)(!!v), get allOpen() { return allOpen; }};  // 第十四轮 J：设置里的“过程默认折叠”复用全局开关

/* 导出：完整过程，不带折叠行和操作条 */
C.onExport = f => {
  f.querySelectorAll('.dz-row, .dz-bar').forEach(x => x.remove());
  f.querySelectorAll('.dz-h').forEach(x => x.classList.remove('dz-h'));
};

/* 接入：载入、显示更早、推送事件，再加一个慢速巡检（判断一轮是否已经停稳） */
const prevLoad = C.onLoad;
C.onLoad = d => {
  try { if (prevLoad) prevLoad(d); }
  finally { try { A.clear(); dirty.clear(); hov = null; liveAt = 0; reconcile('load'); } catch (e) { console.error(e); } }  // 同步执行，仍在第一次绘制之前
};
C.onPrepend = () => rec('prepend');
const prevEv = C.onEvent;
let queued = false;
C.onEvent = ev => {
  try { if (prevEv) prevEv(ev); } finally { onEv(ev); }
};
function onEv(ev) {
  if (ev.sid !== S.cur || (ev.type !== 'items' && ev.type !== 'runner')) return;
  if (ev.type === 'items') for (const p of ev.patch || []) dirty.add(p.id);
  if (!queued) { queued = true; queueMicrotask(() => { queued = false; rec('live'); }); }  // 在这次推送画完之后、浏览器绘制之前
}
setInterval(() => rec('tick'), 500);

/* ================= 3. 发送按钮：排队 / 立即发送 ================= */
const nowBtn = document.createElement('button');
nowBtn.type = 'button'; nowBtn.id = 'sendNow'; nowBtn.textContent = '立即发送'; nowBtn.title = '立即发送：不等这一步做完（⌘Enter）'; nowBtn.hidden = true;
$('#send').before(nowBtn);
nowBtn.onclick = () => { if (C.sendImmediate) C.sendImmediate(); };
C.onSendBtn = busy => {
  const b = $('#send'), q = !!busy && !!(input.value.trim() || S.atts.length);
  if (q) { b.dataset.state = 'queue'; b.textContent = '排队'; b.title = '排队：这一步做完后插进回复'; b.setAttribute('aria-label', '排队发送'); }  // 第十四轮 H：data-state
  b.classList.toggle('queue', q);
  if (nowBtn.hidden === q) nowBtn.hidden = !q;
};

/* ================= 4. 工具图标与增删行数 ================= */
const P = {
  read: '<path d="M4 1.8h5.3l2.9 2.9v9.5H4z"/><path d="M9.2 1.8v3h3M6.2 8h4M6.2 10.6h3"/>',
  edit: '<path d="M10.6 2.6l2.8 2.8-7.7 7.7-3.4.7.7-3.4z"/><path d="M9.2 4l2.8 2.8"/>',
  cmd: '<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="2"/><path d="M4.6 6.3l2 1.7-2 1.7M8.3 10.2h3"/>',
  search: '<circle cx="7" cy="7" r="4.4"/><path d="M10.3 10.3l3.4 3.4"/>',
  web: '<circle cx="8" cy="8" r="5.9"/><path d="M2.3 8h11.4M8 2.1c1.6 1.6 2.4 3.6 2.4 5.9S9.6 12.3 8 13.9C6.4 12.3 5.6 10.3 5.6 8S6.4 3.7 8 2.1z"/>',
  agent: '<rect x="2.6" y="5" width="10.8" height="8.4" rx="2.4"/><path d="M8 2.2V5M6 8.8v1M10 8.8v1"/>',
  other: '<path d="M9.9 2.4a3.3 3.3 0 0 0-3.6 4.4L2.4 10.7a1.4 1.4 0 0 0 2 2l3.9-3.9a3.3 3.3 0 0 0 4.4-3.6l-2 2-1.8-.4-.4-1.8z"/>',
};
const KIND = {Read: 'read', NotebookRead: 'read', Edit: 'edit', MultiEdit: 'edit', Write: 'edit', NotebookEdit: 'edit',
  Bash: 'cmd', BashOutput: 'cmd', KillShell: 'cmd', KillBash: 'cmd', Grep: 'search', Glob: 'search',
  WebFetch: 'web', WebSearch: 'web', Task: 'agent', Agent: 'agent'};
/* 后台按完整输入算好（it.ln）；旧数据没有时按界面拿到的文本估算 */
function countLines(a, b) {
  let A1 = (a || '').split('\n'), B1 = (b || '').split('\n');
  let i = 0; while (i < A1.length && i < B1.length && A1[i] === B1[i]) i++;
  let j = 0; while (j < A1.length - i && j < B1.length - i && A1[A1.length - 1 - j] === B1[B1.length - 1 - j]) j++;
  A1 = A1.slice(i, A1.length - j); B1 = B1.slice(i, B1.length - j);
  const n = A1.length, m = B1.length;
  if (!n || !m || n * m > 250000) return [m, n];
  let prev = new Uint16Array(m + 1), cur = new Uint16Array(m + 1);
  for (let x = 1; x <= n; x++) {
    for (let y = 1; y <= m; y++) cur[y] = A1[x - 1] === B1[y - 1] ? prev[y - 1] + 1 : Math.max(prev[y], cur[y - 1]);
    [prev, cur] = [cur, prev];
  }
  const same = prev[m];
  return [m - same, n - same];
}
function lineStat(it) {
  if (Array.isArray(it.ln)) return it.ln;
  if (it.name === 'Write') return [(it.in || '').replace(/\n$/, '').split('\n').length, 0];
  if (it.diff) return countLines(it.diff.old, it.diff.new);
  return null;
}
C.onTool = (el, it) => {
  const h = el.querySelector(':scope > .head');
  if (!h || h.querySelector('.dz-ti')) return;
  h.insertAdjacentHTML('afterbegin', `<svg class="dz-ti" viewBox="0 0 16 16" aria-hidden="true">${P[KIND[it.name] || 'other']}</svg>`);
  if (!/^(Edit|MultiEdit|Write)$/.test(it.name) || it.error) return;
  const ln = lineStat(it);
  if (!ln || (!ln[0] && !ln[1])) return;
  h.insertAdjacentHTML('beforeend', `<span class="dz-ln">${ln[0] ? `<b class="a">+${ln[0]}</b>` : ''}${ln[1] ? `<b class="d">−${ln[1]}</b>` : ''}</span>`);
};
})();
