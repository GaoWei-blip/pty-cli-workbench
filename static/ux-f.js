(() => {
'use strict';
/* 第十三轮 F：
   F3 出错后的恢复按钮：最后一轮的带类型错误下面一排“继续 / 重发上一条 / 开新对话”。
   F5 对话内查找：⌘F 查找条，回车下一个、Shift+回车上一个、Esc 关闭；命中在折叠过程里先展开；可往前加载再搜。
   F6 表格：回复里的表格悬停出现“复制为 Markdown / 复制为 CSV”；输入框粘贴网页、Excel 表格转成 Markdown 表格。
   F7 归档、删除可撤销：界面立即生效，toast 里 10 秒可撤销；删除延迟 10 秒才调接口（待删记在 localStorage，关页面时立即发出，下次打开补做）。
   F13 完成提示音：窗口不在前台或不是当前对话时，一轮结束响一声（WebAudio 合成）；出错结束换低音。“⋯”菜单里开关。
   接入：钩子都串在原有处理之后（onLoad / onEvent / onPrepend / onSide / onExport），新增 onPreSide；
   app.js 的 archive / removeSession、feat.js 的批量归档 / 删除在开头转给这里（CWB.undoArchive / CWB.undoDelete）。 */
const C = window.CWB;
if (!C) return;
const {S, $, esc, api, toast} = C;
const feed = $('#feed'), sc = $('#scroll'), input = $('#input');
const T = new URLSearchParams(location.search).get('t') || '';
const chain = (name, fn) => {
  const prev = C[name];
  C[name] = (...a) => { try { if (prev) prev(...a); } finally { try { fn(...a); } catch (e) { console.error(e); } } };
};
const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } };
const lsSet = (k, v) => { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch {} };
const isStart = it => (it.k === 'user' && !it.mid) || (it.k === 'sys' && it.turn);
const busyNow = () => !!(S.runner && S.runner.alive && S.runner.busy);
const away = () => document.hidden || !document.hasFocus();
const onCur = fn => { let q = false; return () => { if (q) return; q = true; queueMicrotask(() => { q = false; try { fn(); } catch (e) { console.error(e); } }); }; };

/* ================= F3 出错后的恢复按钮 ================= */
const RETRY = ['继续', '重发上一条'];
const ACTS = {'接口限流': RETRY, '网关错误': RETRY, '服务端错误': RETRY, '接口出错': RETRY,
  '鉴权失败': ['开新对话'], '请求被拒绝': ['开新对话', '重发上一条'], '输出超长': ['继续']};
const clicked = new Set();  // 点过的错误条目：按钮保持禁用
/* 最后一轮里的错误：从后往前找，碰到这一轮的开头还没找到就没有 */
function lastErr() {
  const v = [...S.byId.values()];
  for (let i = v.length - 1; i >= 0; i--) {
    const it = v[i];
    if (it.k === 'error') return it.etype ? it : null;
    if (isStart(it) || it.k === 'text' || it.k === 'tool') return null;
  }
  return null;
}
function lastUserText() {
  const v = [...S.byId.values()];
  for (let i = v.length - 1; i >= 0; i--) if (v[i].k === 'user' && (v[i].text || '').trim()) return v[i].text;
  return '';
}
function f3() {
  const it = S.cur && !busyNow() ? lastErr() : null;
  const acts = it ? (ACTS[it.etype] || []).filter(a => a !== '重发上一条' || lastUserText()) : [];
  const el = acts.length ? feed.querySelector(`[data-id="${CSS.escape(it.id)}"]`) : null;
  feed.querySelectorAll('.f3-acts').forEach(x => { if (!el || x.parentNode !== el) x.remove(); });
  if (!el || el.querySelector(':scope > .f3-acts')) return;
  const row = document.createElement('div');
  row.className = 'f3-acts';
  const off = clicked.has(it.id) ? ' disabled' : '';
  row.innerHTML = acts.map(a => `<button type="button" class="btn small" data-f3="${a}"${off}>${a}</button>`).join('') +
    (it.etype === '鉴权失败' ? '<span class="f3-hint">请先检查登录状态</span>' : '');
  row.addEventListener('click', e => {
    const b = e.target.closest('button[data-f3]');
    if (!b || b.disabled) return;
    e.stopPropagation();
    clicked.add(it.id);
    row.querySelectorAll('button').forEach(x => { x.disabled = true; });
    const a = b.dataset.f3;
    if (a === '继续') C.sendText('继续');
    else if (a === '重发上一条') C.sendText(lastUserText());
    else if (a === '开新对话') C.newSession(S.cwd);
  });
  el.appendChild(row);
}
const f3Soon = onCur(f3);

/* ================= F5 对话内查找 ================= */
const HL = !!(window.Highlight && window.CSS && CSS.highlights);
const bar = document.createElement('div');
bar.id = 'fdBar'; bar.hidden = true;
bar.innerHTML = '<input id="fdQ" type="text" placeholder="在对话中查找" spellcheck="false" autocomplete="off">' +
  '<span class="fd-n"></span>' +
  '<button type="button" class="fd-older" data-fd="older" hidden>搜更早的内容</button>' +
  '<button type="button" class="fd-ic" data-fd="prev" title="上一个（Shift+回车）"><svg viewBox="0 0 16 16"><path d="M4 10l4-4 4 4"/></svg></button>' +
  '<button type="button" class="fd-ic" data-fd="next" title="下一个（回车）"><svg viewBox="0 0 16 16"><path d="M4 6l4 4 4-4"/></svg></button>' +
  '<button type="button" class="fd-ic" data-fd="close" title="关闭（Esc）"><svg viewBox="0 0 16 16"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg></button>';
$('#main').appendChild(bar);
const fq = bar.querySelector('#fdQ'), fn = bar.querySelector('.fd-n'), fOlder = bar.querySelector('.fd-older');
const FD = {q: '', list: [], i: -1, timer: 0, busy: false};
const fold = s => { let o = ''; for (const ch of s) { const l = ch.toLowerCase(); o += l.length === ch.length ? l : ch; } return o; };
const BLOCK = 'p,li,td,th,pre,h1,h2,h3,h4,h5,h6,blockquote,div';
/* 只搜正文（回复的文字块，不含实时预览）和用户消息 */
const hosts = () => feed.querySelectorAll(':scope > .user > .utext, :scope > .tl > .textblock:not(.live) > .md');
function scan(host, q) {
  const nodes = [];
  let s = '', lastB = null;
  const tw = document.createTreeWalker(host, NodeFilter.SHOW_TEXT,
    {acceptNode: n => n.parentElement && n.parentElement.closest('.f6-tb') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
  for (let n = tw.nextNode(); n; n = tw.nextNode()) {
    const b = n.parentElement.closest(BLOCK);
    if (lastB && b !== lastB) s += '\u0000';  // 段落之间不连成一句
    lastB = b;
    nodes.push([n, s.length]); s += n.data;
  }
  const out = [], f = fold(s);
  if (!f.includes(q)) return out;
  const at = off => { let lo = 0, hi = nodes.length - 1; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (nodes[m][1] <= off) lo = m; else hi = m - 1; } return nodes[lo]; };
  for (let i = f.indexOf(q); i >= 0; i = f.indexOf(q, i + q.length)) {
    const [a, ao] = at(i), [b, bo] = at(i + q.length - 1);
    const r = document.createRange();
    r.setStart(a, i - ao); r.setEnd(b, i + q.length - bo);
    out.push({host, start: i, range: r});
  }
  return out;
}
function paint() {
  if (!HL) return;
  const cur = FD.list[FD.i];
  CSS.highlights.set('cwbf-find', new Highlight(...FD.list.filter(m => m !== cur).map(m => m.range)));
  if (cur) CSS.highlights.set('cwbf-find-cur', new Highlight(cur.range)); else CSS.highlights.delete('cwbf-find-cur');
}
function clearPaint() { if (HL) { CSS.highlights.delete('cwbf-find'); CSS.highlights.delete('cwbf-find-cur'); } }
function count() {
  const q = FD.q;
  fn.textContent = !q ? '' : FD.list.length ? `${FD.i + 1}/${FD.list.length}` : '无结果';
  fn.classList.toggle('none', !!q && !FD.list.length);
  const older = !!(q && S.cur && S.hidden);
  if (fOlder.hidden === older) fOlder.hidden = !older;
}
/* 重新搜一遍。keep：尽量停在原来那一处（内容变化后）；否则从视口顶部往下找第一个 */
function search(keep) {
  const q = fold(fq.value.trim());
  const old = FD.list[FD.i];
  FD.q = q; FD.list = [];
  if (q && S.cur) for (const h of hosts()) FD.list.push(...scan(h, q));
  if (!FD.list.length) FD.i = -1;
  else if (keep && old) {
    const j = FD.list.findIndex(m => m.host === old.host && m.start === old.start);
    FD.i = j >= 0 ? j : Math.min(FD.i, FD.list.length - 1);
  } else {
    const top = sc.getBoundingClientRect().top;
    const y = m => { const h = m.host.closest('.dz-h'); return (h ? h.closest('.tl') || m.host : m.range).getBoundingClientRect().bottom; };
    const j = FD.list.findIndex(m => y(m) > top + 4);
    FD.i = j >= 0 ? j : FD.list.length - 1;
  }
  paint(); count();
}
function show(i) {
  const m = FD.list[i];
  if (!m) return;
  FD.i = i;
  if (m.host.closest('.dz-h')) m.host.scrollIntoView({block: 'center'});  // ux-d 包过 scrollIntoView：先展开这一轮
  const u = m.host.closest('.user.long');
  if (u && !u.classList.contains('open') && m.range.getBoundingClientRect().bottom > u.getBoundingClientRect().bottom - 8) {
    const b = u.nextElementSibling; if (b && b.classList.contains('ufold')) b.click();
  }
  const r = m.range.getBoundingClientRect(), v = sc.getBoundingClientRect();
  if (r.top < v.top + 48 || r.bottom > v.bottom - 24) sc.scrollTop += r.top - (v.top + v.height / 2);
  if (!HL) { const s = getSelection(); s.removeAllRanges(); s.addRange(m.range); }
  paint(); count();
}
function step(d) {
  if (FD.timer) { clearTimeout(FD.timer); FD.timer = 0; search(false); show(FD.i); if (d > 0) return; }
  if (!FD.list.length) return;
  show((FD.i + d + FD.list.length) % FD.list.length);
}
function place() {
  const m = $('#main').getBoundingClientRect(), v = sc.getBoundingClientRect();
  bar.style.top = Math.round(v.top - m.top + 8) + 'px';
}
/* 第十四轮 FH：查找条浮在消息区顶部，打开时消息区顶部让出它的高度（第一条消息不被盖住）；
   不在最顶上时同时补上滚动距离，看到的内容不跳。关闭时收回 */
let roomPx = 0;
function room(on) {
  const st = sc.scrollTop, old = roomPx;
  feed.style.paddingTop = '';
  roomPx = 0;
  const first = on && feed.firstElementChild;
  if (first) {
    /* 第一块内容（含它自己的上边距）离消息区顶部多远；条在顶部下 8px，条下面再留 8px */
    const top = first.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop;
    roomPx = Math.max(0, Math.ceil(8 + bar.offsetHeight + 8 - top));
    if (roomPx) feed.style.paddingTop = ((parseFloat(getComputedStyle(feed).paddingTop) || 0) + roomPx) + 'px';
  }
  if (st > 0 && roomPx !== old) sc.scrollTop = Math.max(0, st + roomPx - old);
}
function openFind() {
  if (!S.cur) return;
  const sel = getSelection(), t = sel && sel.rangeCount && feed.contains(sel.anchorNode) ? sel.toString().trim() : '';
  if (t && t.length <= 120 && !/\n/.test(t)) fq.value = t;
  place();
  if (bar.hidden) { bar.hidden = false; room(true); search(false); if (FD.list.length) show(FD.i); }
  fq.focus(); fq.select();
}
function closeFind(focusInput) {
  if (bar.hidden) return;
  bar.hidden = true; clearTimeout(FD.timer); FD.timer = 0;
  FD.list = []; FD.i = -1; clearPaint();
  room(false);
  if (focusInput) input.focus();
}
fq.addEventListener('input', () => {
  clearTimeout(FD.timer);
  FD.timer = setTimeout(() => { FD.timer = 0; search(false); if (FD.list.length) show(FD.i); }, 150);
});
fq.addEventListener('keydown', e => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter') { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeFind(true); }
});
bar.addEventListener('mousedown', e => { if (e.target.closest('button')) e.preventDefault(); });  // 点按钮不抢输入框焦点
bar.addEventListener('click', e => {
  const b = e.target.closest('button[data-fd]');
  if (!b) return;
  const a = b.dataset.fd;
  if (a === 'prev') step(-1);
  else if (a === 'next') step(1);
  else if (a === 'close') closeFind(true);
  else if (a === 'older') searchOlder();
});
/* 还有没加载的更早内容：往前一页页加载，直到出现新的命中或者到头 */
async function searchOlder() {
  if (FD.busy || !FD.q) return;
  FD.busy = true; fOlder.disabled = true; fOlder.textContent = '正在加载…';
  const sid = S.cur, before = FD.list.length;
  try {
    await C.loadUntil(() => { search(true); return FD.list.length > before; });
    if (S.cur !== sid || bar.hidden) return;
    search(true);
    const added = FD.list.length - before;
    if (added > 0) show(added - 1);  // 新露出来的最后一处（紧挨着原来内容的上面）
    else { count(); toast('更早的内容里没有找到'); }
  } finally { FD.busy = false; fOlder.disabled = false; fOlder.textContent = '搜更早的内容'; count(); }
}
document.addEventListener('keydown', e => {
  if (e.defaultPrevented || !e.metaKey || e.ctrlKey || e.altKey) return;
  const k = (e.key || '').toLowerCase(), code = e.code;
  if ((k === 'f' || code === 'KeyF') && !e.shiftKey) {
    if (!$('#modal').hidden || !$('#palette').hidden || !S.cur) return;
    e.preventDefault(); openFind();
  } else if ((k === 'g' || code === 'KeyG') && !bar.hidden) { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
}, true);
/* 内容变了（新回复、加载更早、重画）：节流重搜，停在原来那一处，不滚动 */
const reSearch = () => {
  if (bar.hidden || !FD.q || FD.reT) return;
  FD.reT = setTimeout(() => { FD.reT = 0; if (!bar.hidden && !FD.timer) search(true); }, 250);
};
addEventListener('resize', () => { if (!bar.hidden) place(); });
if (window.ResizeObserver) new ResizeObserver(() => { if (!bar.hidden) place(); }).observe(sc);

/* ================= F6 表格：复制为 Markdown / CSV，粘贴表格转 Markdown ================= */
const cellText = c => { const x = c.cloneNode(true); x.querySelectorAll('br').forEach(b => b.replaceWith('\n')); return x.textContent.replace(/[ \t ]+/g, ' ').replace(/ ?\n ?/g, '\n').trim(); };
const mdCell = v => String(v).replace(/\\/g, '\\\\').replace(/\|/g, '\\|').replace(/\n+/g, '<br>') || ' ';
function gridOf(table) {
  const g = [];
  [...table.rows].forEach((tr, r) => {
    g[r] = g[r] || [];
    let c = 0;
    for (const td of tr.cells) {
      while (g[r][c] != null) c++;
      const v = cellText(td), cs = Math.min(50, Math.max(1, td.colSpan || 1)), rs = Math.min(500, Math.max(1, td.rowSpan || 1));
      for (let i = 0; i < rs; i++) for (let j = 0; j < cs; j++) (g[r + i] = g[r + i] || [])[c + j] = i || j ? '' : v;
      c += cs;
    }
  });
  let rows = g.filter(r => r && r.some(v => v));
  const w = Math.max(0, ...rows.map(r => r.length));
  rows = rows.map(r => Array.from({length: w}, (_, i) => r[i] || ''));
  let n = w;
  while (n > 1 && rows.every(r => !r[n - 1])) n--;  // 去掉末尾的空列
  return rows.map(r => r.slice(0, n));
}
function gridMd(rows, aligns) {
  const w = rows[0].length, line = r => '| ' + r.map(mdCell).join(' | ') + ' |';
  const sep = '| ' + Array.from({length: w}, (_, i) => ({left: ':---', center: ':---:', right: '---:'}[(aligns || [])[i]] || '---')).join(' | ') + ' |';
  return [line(rows[0]), sep, ...rows.slice(1).map(line)].join('\n');
}
const csvCell = v => /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
function tableCsv(t) { return gridOf(t).map(r => r.map(csvCell).join(',')).join('\r\n') + '\r\n'; }
/* Markdown 原文：按 marked 的词法结果找到同一张表（含列表、引用里嵌的表），数量对不上时按界面上的表格重建 */
function tableMd(t) {
  const blk = t.closest('.textblock'), it = blk && S.byId.get(blk.dataset.id);
  const all = [...blk.querySelectorAll('.md table')], i = all.indexOf(t);
  if (it && it.text && window.marked && marked.lexer) {
    try {
      const toks = [];
      const walk = a => { for (const k of a || []) { if (k.type === 'table') toks.push(k); walk(k.tokens); walk(k.items); } };
      walk(marked.lexer(it.text));
      if (toks.length === all.length && toks[i]) return toks[i].raw.replace(/^\n+|\n+$/g, '').split('\n').map(l => l.trim()).join('\n');
    } catch (e) { console.error(e); }
  }
  const head = t.rows[0] ? [...t.rows[0].cells] : [];
  return gridMd(gridOf(t), head.map(c => c.style.textAlign || c.getAttribute('align') || ''));
}
function decorateTables() {
  for (const t of feed.querySelectorAll('.textblock:not(.live) > .md table:not([data-f6])')) {
    t.dataset.f6 = '1';
    if (t.parentElement.classList.contains('f6-tw')) continue;
    const w = document.createElement('div');
    w.className = 'f6-tw';
    t.before(w); w.appendChild(t);
    w.insertAdjacentHTML('afterbegin', '<div class="f6-tb"><button type="button" data-f6="md">复制为 Markdown</button><button type="button" data-f6="csv">复制为 CSV</button></div>');
  }
}
feed.addEventListener('click', e => {
  const b = e.target.closest('.f6-tb button[data-f6]');
  if (!b) return;
  e.stopPropagation();
  const t = b.closest('.f6-tw').querySelector(':scope > table');
  if (t) C.copy(b.dataset.f6 === 'csv' ? tableCsv(t) : tableMd(t), b);
});
/* 粘贴：剪贴板里是表格（网页、Excel 的 text/html，或 Excel 纯文本的制表符多行）就转成 Markdown 表格。
   在 #composer 的捕获阶段先看一眼：是表格才接管并拦下，其余（图片、访达文件、普通文字）照旧交给 app.js。 */
function htmlTables(html) {
  if (!/<table[\s>]/i.test(html)) return null;
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const tops = [...doc.querySelectorAll('table')].filter(t => !t.parentElement.closest('table'));
  const grids = tops.map(gridOf).filter(g => g.length >= 2 && g[0].length >= 2);
  if (!grids.length) return null;
  const all = (doc.body.textContent || '').replace(/\s+/g, '').length;
  const inT = tops.reduce((n, t) => n + (t.textContent || '').replace(/\s+/g, '').length, 0);
  if (all && inT < all * 0.8) return null;  // 正文里夹着排版用的表格：不当表格
  return grids.map(g => gridMd(g)).join('\n\n');
}
function parseTsv(text) {
  const t = text.replace(/\r\n?/g, '\n').replace(/\n+$/, '');
  if (!t.includes('\t') || !t.includes('\n')) return null;
  const rows = []; let row = [], cell = '', q = false;
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (q) {
      if (ch === '"') { if (t[i + 1] === '"') { cell += '"'; i++; } else q = false; }
      else cell += ch;
      continue;
    }
    if (ch === '"' && cell === '') q = true;
    else if (ch === '\t') { row.push(cell); cell = ''; }
    else if (ch === '\n') { row.push(cell); rows.push(row); row = []; cell = ''; }
    else cell += ch;
  }
  if (q) return null;
  row.push(cell); rows.push(row);
  const w = rows[0].length;
  if (rows.length < 2 || w < 2 || rows.some(r => r.length !== w)) return null;
  if (rows.every(r => !r[0].trim())) return null;  // 每行都以制表符开头：是缩进的代码，不是表格
  return gridMd(rows.map(r => r.map(v => v.trim())));
}
function insertBlock(md) {
  const s = input.selectionStart ?? input.value.length, e = input.selectionEnd ?? s;
  const pre = input.value.slice(0, s), post = input.value.slice(e);
  const txt = (pre && !/\n$/.test(pre) ? '\n' : '') + md + (post && !/^\n/.test(post) ? '\n' : '');
  input.focus();
  if (!document.execCommand('insertText', false, txt)) C.insertText(txt);
}
$('#composer').addEventListener('paste', e => {
  if (e.target !== input) return;
  const cd = e.clipboardData;
  if (!cd) return;
  let md = null;
  try {
    const html = cd.getData('text/html') || '';
    md = html ? htmlTables(html) : null;
    if (!md) md = parseTsv(cd.getData('text/plain') || '');
  } catch (err) { console.error(err); md = null; }
  if (!md) return;
  e.preventDefault(); e.stopPropagation();
  insertBlock(md);
}, true);

/* ================= F7 归档、删除可撤销 ================= */
const PK = 'cwbf.pendDel';  // {sid: 到期时间毫秒}：等着真正删除的对话
const readP = () => { try { return JSON.parse(localStorage.getItem(PK) || '{}') || {}; } catch { return {}; } };
const writeP = o => lsSet(PK, Object.keys(o).length ? JSON.stringify(o) : null);
let delSet = new Set(Object.keys(readP()));
const syncDel = () => { delSet = new Set(Object.keys(readP())); };
const ovArch = new Map();  // 接口回来前后以本地的归档状态为准，免得列表刷新时闪回
const memDel = new Map();  // 本页发起、还没执行的删除：sid → 计时器
const ut = document.createElement('div');
ut.id = 'fUndo'; ut.hidden = true;
$('#toast').after(ut);
let utTimer = 0, utFn = null;
function hideUndo() { clearTimeout(utTimer); ut.hidden = true; utFn = null; }
function undoToast(text, fn) {
  $('#toast').hidden = true;
  ut.innerHTML = `<span>${esc(text)}</span><i>·</i><button type="button">撤销</button>`;
  ut.hidden = false; utFn = fn;
  ut.querySelector('button').onclick = () => { const f = utFn; hideUndo(); if (f) f(); };
  clearTimeout(utTimer); utTimer = setTimeout(hideUndo, 10000);
}
C.onPreSide = (prev => () => {
  if (prev) prev();
  for (const p of S.projects || []) {
    if (delSet.size) p.sessions = p.sessions.filter(s => !delSet.has(s.sid));
    if (ovArch.size) for (const s of p.sessions) if (ovArch.has(s.sid)) s.archived = ovArch.get(s.sid);
  }
  if (delSet.size && S.hits && S.hits.length) S.hits = S.hits.filter(h => !delSet.has(h.sid));
  if (delProj.size && S.projects) S.projects = S.projects.filter(p => !delProj.has(p.cwd));  // 11-23：等着删除的项目先不显示
})(C.onPreSide);
/* 11-23 删除项目：已归档项目菜单里的“删除项目”。先从左栏藏起来，10 秒内可撤销；到时让后台把对话记录移到废纸篓，访达里的文件夹不动 */
const delProj = new Map();  // cwd → 计时器
async function commitProj(cwd) {
  if (!delProj.has(cwd)) return;  // 已撤销
  try { await api('delete-project', {cwd}); } catch (e) { toast('删除项目失败：' + e.message); }
  delProj.delete(cwd);
  C.refreshSide(true);
}
C.undoDeleteProject = p => {
  if (!p || delProj.has(p.cwd)) return;
  const n = p.sessions.length, wasCur = p.sessions.some(s => s.sid === S.cur) ? S.cur : null;
  if (wasCur && C.clearMain) C.clearMain();
  delProj.set(p.cwd, setTimeout(() => commitProj(p.cwd), 10000));
  C.renderSide();
  undoToast(`已删除项目「${p.name}」` + (n ? `，${n} 个对话移到废纸篓` : ''), () => {
    clearTimeout(delProj.get(p.cwd)); delProj.delete(p.cwd);
    C.renderSide(); C.refreshSide(true);
    if (wasCur && !S.cur) C.openSession(wasCur);
  });
};
addEventListener('pagehide', () => {  // 关页面时没到时间的删除立即发出
  for (const cwd of delProj.keys()) {
    try { fetch('/api/delete-project', {method: 'POST', keepalive: true, headers: {'X-Token': T, 'Content-Type': 'application/json'}, body: JSON.stringify({cwd})}).catch(() => {}); } catch {}
  }
});
async function setArch(list, on) {
  for (const s of list) { s.archived = on; ovArch.set(s.sid, on); }
  C.renderSide();
  const res = await Promise.allSettled(list.map(s => api('archive', {sid: s.sid, on})));
  const bad = res.map((r, i) => r.status === 'rejected' ? [list[i], r.reason] : null).filter(Boolean);
  for (const [s] of bad) if (ovArch.get(s.sid) === on) ovArch.delete(s.sid);
  if (bad.length) { hideUndo(); toast(`${bad.length} 个对话${on ? '归档' : '恢复'}失败：${bad[0][1] && bad[0][1].message}`); C.refreshSide(true); }
  setTimeout(() => { for (const s of list) if (ovArch.get(s.sid) === on) ovArch.delete(s.sid); C.refreshSide(true); }, 1500);
  return !bad.length;
}
C.undoArchive = (list, on) => {
  list = (list || []).filter(Boolean);
  if (!list.length) return;
  const word = on ? '归档' : '恢复';
  undoToast(`已${word} ${list.length} 个对话`, () => setArch(list, !on));
  setArch(list, on);
};
async function commitDel(sids, quiet) {
  let err = '';
  for (const sid of sids) {
    if (!(sid in readP())) continue;  // 已撤销
    memDel.delete(sid);
    try { await api('delete', {sid}); } catch (e) { if (!/找不到/.test(e.message)) err = e.message; }
    const P = readP(); delete P[sid]; writeP(P);
  }
  syncDel();
  if (err && !quiet) toast('删除失败：' + err);
  C.refreshSide(true);
}
C.undoDelete = list => {
  list = (list || []).filter(Boolean);
  if (!list.length) return;
  const due = Date.now() + 10000, P = readP(), saved = [];
  for (const s of list) {
    P[s.sid] = due;
    for (const p of S.projects) { const i = p.sessions.indexOf(s); if (i >= 0) saved.push({p, s, i}); }
  }
  writeP(P); syncDel();
  const sids = list.map(s => s.sid), wasCur = sids.includes(S.cur) ? S.cur : null;
  if (wasCur && C.clearMain) C.clearMain();
  C.renderSide();
  const timer = setTimeout(() => commitDel(sids), 10000);
  sids.forEach(sid => memDel.set(sid, timer));
  undoToast(`已删除 ${list.length} 个对话`, () => {
    clearTimeout(timer);
    const P2 = readP(); sids.forEach(sid => { delete P2[sid]; memDel.delete(sid); }); writeP(P2); syncDel();
    saved.sort((a, b) => a.i - b.i).forEach(({p, s, i}) => { if (!p.sessions.some(x => x.sid === s.sid)) p.sessions.splice(Math.min(i, p.sessions.length), 0, s); });
    C.renderSide(); C.refreshSide(true);
    if (wasCur && !S.cur) C.openSession(wasCur);
  });
};
/* 关页面（含刷新、退出 App）时：本页还没执行的删除立即发出（keepalive）；记录先不清，下次打开时再补一次，找不到就算已删 */
addEventListener('pagehide', () => {
  const P = readP();
  for (const sid of memDel.keys()) {
    if (!(sid in P)) continue;
    try { fetch('/api/delete', {method: 'POST', keepalive: true, headers: {'X-Token': T, 'Content-Type': 'application/json'}, body: JSON.stringify({sid})}).catch(() => {}); } catch {}
  }
});
/* 打开页面时：上次留下的待删（关页面没发出去、或别的窗口发起的）到期就执行 */
{
  const P = readP(), now = Date.now();
  for (const [sid, due] of Object.entries(P)) setTimeout(() => commitDel([sid], true), Math.max(1500, due - now));
}
addEventListener('storage', e => { if (e.key === PK) { syncDel(); C.renderSide(); } });

/* ================= F13 完成提示音 ================= */
const SND = 'cwbf.chime';
let chimeOn = lsGet(SND, '1') !== '0';
let ac = null;
function ensureAc() {
  if (!chimeOn) return;
  if (!ac) { const A = window.AudioContext || window.webkitAudioContext; if (!A) return; try { ac = new A(); } catch { return; } }
  if (ac.state === 'suspended') ac.resume().catch(() => {});
}
/* WKWebView 的自动播放限制：等用户第一次点、按键之后再建 AudioContext；之后每次交互顺手恢复被挂起的 */
for (const t of ['pointerdown', 'keydown']) addEventListener(t, ensureAc, {capture: true, passive: true});
function tone(err) {
  const t0 = ac.currentTime + 0.02;
  /* 正常：G5→C6 两声上行正弦；出错：G4→E♭4 两声下行三角波，低一些 */
  const notes = err ? [[392, 0], [311.13, 0.13]] : [[783.99, 0], [1046.5, 0.1]];
  for (const [f, dt] of notes) {
    const o = ac.createOscillator(), g = ac.createGain(), s = t0 + dt, dur = err ? 0.3 : 0.34;
    o.type = err ? 'triangle' : 'sine'; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, s);
    g.gain.exponentialRampToValueAtTime(err ? 0.06 : 0.05, s + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, s + dur);
    o.connect(g); g.connect(ac.destination);
    o.start(s); o.stop(s + dur + 0.02);
  }
}
function chime(err) {
  if (!chimeOn || !ac) return;
  if (ac.state === 'running') { tone(err); return; }
  /* 被挂起（还没有算数的交互）：试着恢复，恢复得慢就不响了，免得之后一次交互时把攒下的一起放出来 */
  const t = Date.now();
  ac.resume().then(() => { if (ac.state === 'running' && Date.now() - t < 300) tone(err); }).catch(() => {});
}
const errAt = {}, busyAt = {}, rang = {};
function onRunner(ev) {
  const st = ev.state || {}, sid = ev.sid, busy = !!(st.alive && st.busy);
  /* 这个钩子在 setSideFlags 之前跑：S.busyMap 里还是上一次的状态 */
  const s = C.findSession(sid), prev = S.busyMap[sid] != null ? S.busyMap[sid] : !!(s && s.busy);
  if (busy && !prev) busyAt[sid] = Date.now();
  if (!prev || busy) return;
  if (!(sid !== S.cur || away())) return;
  const crashed = !st.alive, t0 = busyAt[sid] || 0;
  /* 出错的那条错误要等一会儿才写进记录（钩子补记最多 2 秒），稍等再定音色 */
  setTimeout(() => {
    if (Date.now() - (rang[sid] || 0) < 3000) return;
    rang[sid] = Date.now();
    const e = errAt[sid] || 0;
    chime(crashed || !!(e && (t0 ? e >= t0 - 500 : Date.now() - e < 10000)));
  }, 900);
}
function setChime(v) {
  chimeOn = v; lsSet(SND, v ? '1' : '0');
  if (v) { ensureAc(); chime(false); }
}
const moreBtn = $('#moreBtn'), prevMore = moreBtn.onclick;
moreBtn.onclick = e => {
  if (prevMore) prevMore.call(moreBtn, e);
  const m = S.menu;
  if (!m) return;
  const b = document.createElement('button');
  b.className = 'mi f13-mi';
  b.innerHTML = `<span>完成提示音</span>${chimeOn ? '<i class="f13-ck">✓</i>' : ''}`;
  b.onclick = ev => { ev.stopPropagation(); C.closeMenu(); setChime(!chimeOn); };
  const seps = m.querySelectorAll('.msep'), last = seps[seps.length - 1];
  if (last) m.insertBefore(b, last); else m.appendChild(b);
};

/* ================= 接入 ================= */
/* 只看元素的增删（转圈字符、实时预览只换文字节点，不用管） */
const mo = new MutationObserver(recs => {
  if (!recs.some(r => [...r.addedNodes, ...r.removedNodes].some(n => n.nodeType === 1))) return;
  decorateTables(); reSearch();
});
mo.observe(feed, {childList: true, subtree: true});
chain('onLoad', () => { decorateTables(); f3(); if (!bar.hidden) { search(false); place(); } });
chain('onPrepend', () => decorateTables());
chain('onEvent', ev => {
  if (ev.type === 'items' && (ev.new || []).some(it => it && it.k === 'error')) errAt[ev.sid] = Date.now();
  if (ev.type === 'runner' && ev.sid) onRunner(ev);
  if (ev.sid === S.cur && (ev.type === 'items' || ev.type === 'runner')) f3Soon();
});
chain('onExport', f => { f.querySelectorAll('.f6-tb, .f3-acts').forEach(x => x.remove()); });
const clr = C.clearMain;
if (clr) C.clearMain = (...a) => { closeFind(false); return clr(...a); };
decorateTables();
C.fx = {openFind, closeFind, search, tableMd, tableCsv, htmlTables, parseTsv, f3, chime, setChime, get chimeOn() { return chimeOn; }};  // setChime：第十四轮 J 设置面板复用
})();
