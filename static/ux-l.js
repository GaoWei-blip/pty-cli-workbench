(() => {
'use strict';
/* 第十五轮 F2：右栏、设置、左栏小项。
   1. 审阅范围「这一轮｜整个对话｜未提交」：turn-changes / turn-diff；“未提交”沿用 app2.js 的 git 审阅。CWB.openReview({scope, uuid})
   2. 任务页运行中的后台命令加“停止”：task-stop
   3. 右栏“旁问”标签（⌘;）：btw-list / btw，事件 btw。CWB.btwAsk(q) 打开标签并发出
   4. 设置“记忆与指令”：memory-files；右栏预览里 Markdown、纯文本可编辑保存：file-save
   5. 设置“通用”：时间线详略（pref.density，生效由 F1 的 CWB.applyDensity）、上下文与缓存显示（后端偏好 prefs.ctxStatus）
   6. 左栏搜索框旁筛选（全部 / 进行中 / 等我确认 / 未读，只影响显示）；对话菜单“标记为未读”（mark-unread）；顶部“⋯”加标记为未读、复制会话 ID、复制记录路径
   7. 顶栏“用…打开”：open-apps / open-with，记住上次选的
   接口每次现取 CWB.api（测试可替换）；接口不存在或返回 {error} 时对应功能安静降级，不影响原有功能。 */
const C = window.CWB;
if (!C || !C.pane) return;
const {S, $, esc} = C;
const P = C.pane, R = P.R, body = P.body, pane = $('#rpane');
const ls = {get(k, d) { try { const v = localStorage.getItem('cwbl.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { if (v == null) localStorage.removeItem('cwbl.' + k); else localStorage.setItem('cwbl.' + k, JSON.stringify(v)); } catch {} }};
const chain = (name, fn) => {
  const prev = C[name];
  C[name] = (...a) => { try { if (prev) prev(...a); } finally { try { fn(...a); } catch (e) { console.error(e); } } };
};
/* 接口：出错一律变成 {error}，不抛 */
async function call(n, b) {
  try { const d = await C.api(n, b || {}); return d && typeof d === 'object' ? d : {error: '返回格式不对'}; }
  catch (e) { return {error: (e && e.message) || '操作没有成功'}; }
}
const toast = t => C.toast(t);
const svg = (d, cls, vb) => `<svg class="lx-i${cls ? ' ' + cls : ''}" viewBox="${vb || '0 0 16 16'}" aria-hidden="true">${d}</svg>`;
const IC = {
  chev: svg('<path d="M2.6 3.9 5 6.3l2.4-2.4"/>', 'lx-chev', '0 0 10 10'),
  right: svg('<path d="M6.2 3.6 10.6 8l-4.4 4.4"/>', 'lx-sm'),
  copy: svg('<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M3 10.5v-7a1 1 0 0 1 1-1h6.5"/>', 'lx-sm'),
  quote: svg('<path d="M2.5 4.2c0-.9.7-1.6 1.6-1.6h7.8c.9 0 1.6.7 1.6 1.6v5.2c0 .9-.7 1.6-1.6 1.6H7.2l-2.8 2.4V11h-.3c-.9 0-1.6-.7-1.6-1.6z"/><path d="M5.4 5.6h5.2M5.4 7.9h3.4"/>', 'lx-sm'),
  up: svg('<path d="M8 12.5v-9M4.5 7 8 3.5 11.5 7"/>'),
  filter: svg('<path d="M2.5 4.5h11M4.5 8h7M6.5 11.5h3"/>'),
  folder: svg('<path d="M2.2 4.4c0-.7.5-1.2 1.2-1.2h2.8l1.4 1.6h5c.7 0 1.2.5 1.2 1.2v5.8c0 .7-.5 1.2-1.2 1.2H3.4c-.7 0-1.2-.5-1.2-1.2z"/>'),
  code: svg('<path d="M6 4.5 2.5 8 6 11.5M10 4.5 13.5 8 10 11.5"/>'),
  cursor: svg('<path d="M4 2.8 12.5 8l-4 1-1.3 4z"/>'),
  term: svg('<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M4.8 6.5 6.5 8 4.8 9.5M8 10h3"/>'),
  app: svg('<rect x="2.5" y="2.5" width="11" height="11" rx="2.5"/>'),
  file: svg('<path d="M4 2.5h5.5L12 5v8.5H4z"/><path d="M9.5 2.5V5H12"/>'),
  pen: svg('<path d="M3 13l.6-2.8L10.9 3a1.2 1.2 0 0 1 1.7 0l.4.4a1.2 1.2 0 0 1 0 1.7L5.8 12.4z"/>', 'lx-sm'),
  more: svg('<circle cx="3.8" cy="8" r="1.05"/><circle cx="8" cy="8" r="1.05"/><circle cx="12.2" cy="8" r="1.05"/>', 'lx-fill'),
  check: svg('<path d="M3.4 8.4 6.6 11.4 12.6 4.8"/>', 'lx-sm'),
  warn: svg('<circle cx="8" cy="8" r="5.8"/><path d="M8 5v3.4M8 10.8v.1"/>', 'lx-sm'),
  diff: svg('<path d="M4 2.5h6l2.5 2.5v8.5H4z"/><path d="M6.5 8h3M8 6.5v3"/>'),
  chat: svg('<path d="M2.5 4.2c0-.9.7-1.6 1.6-1.6h7.8c.9 0 1.6.7 1.6 1.6v5.2c0 .9-.7 1.6-1.6 1.6H7.2l-2.8 2.4V11h-.3c-.9 0-1.6-.7-1.6-1.6z"/>'),
};
const p2 = n => String(n).padStart(2, '0');
const toMs = t => { t = +t || 0; return t && t < 1e12 ? t * 1000 : t; };
function fmtWhen(t, short) {
  t = toMs(t); if (!t) return '';
  const d = new Date(t), n = new Date(), hm = p2(d.getHours()) + ':' + p2(d.getMinutes());
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(n) - day(d)) / 86400000);
  if (diff === 0) return short ? hm : '今天 ' + hm;
  if (diff === 1) return '昨天 ' + hm;
  if (d.getFullYear() === n.getFullYear()) return p2(d.getMonth() + 1) + '-' + p2(d.getDate()) + ' ' + hm;
  return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate());
}
const tilde = p => (S.home && p && (p === S.home || p.startsWith(S.home + '/')) ? '~' + p.slice(S.home.length) : p || '');
const baseName = p => (p || '').split('/').filter(Boolean).pop() || p || '';
const paneOn = tab => !pane.hidden && R.tab === tab;
const emptyBox = (ic, title, extra) => `<div class="lx-empty">${ic ? `<span class="lx-eic">${ic}</span>` : ''}<b>${esc(title)}</b>${extra || ''}</div>`;
function mkBtn(cls, html, title) { const b = document.createElement('button'); b.type = 'button'; b.className = cls; b.innerHTML = html; if (title) { b.title = title; b.setAttribute('aria-label', title); } return b; }
function quoteToInput(text) {
  const i = $('#input'); if (!i || !text) return;
  const q = String(text).trim().split('\n').map(l => '> ' + l).join('\n') + '\n\n';
  i.value = (i.value ? i.value.replace(/\s*$/, '\n\n') : '') + q;
  i.dispatchEvent(new Event('input')); i.focus(); i.selectionStart = i.selectionEnd = i.value.length;
}

/* ====================================================================
   1. 审阅范围
   ==================================================================== */
const SC = [['turn', '这一轮'], ['all', '整个对话'], ['git', '未提交']];
const KIND = {new: '新增', mod: '修改', del: '删除'};
const V = {};
function resetV() {
  Object.assign(V, {sid: S.cur, cwd: S.cwd, data: null, sig: '', git: null, loaded: false, scope: null, uuid: null, follow: true, fresh: true,
    open: {}, diffs: {}, unfold: {}, text: {}, seq: 0, timer: 0});
}
resetV(); V.sid = null;
const turns = () => (V.data && V.data.turns) || [];
const latest = () => { const t = turns(); return t.length ? t[t.length - 1] : null; };
const turnOf = uuid => turns().find(t => t.uuid === uuid) || null;
function scopes() { const o = []; if (V.data) o.push('turn', 'all'); if (V.git) o.push('git'); return o; }
const curSet = () => (V.scope === 'all' ? V.data && V.data.all : turnOf(V.uuid));
const dkey = f => (V.scope === 'all' ? '*' : V.uuid) + '|' + f.path;
const fsig = f => [f.st, f.add, f.del].join('/');
function normFiles(x) {
  const fs = x && Array.isArray(x.files) ? x.files : [];
  return fs.filter(f => f && typeof f.path === 'string' && f.path).map(f => ({path: f.path, rel: typeof f.rel === 'string' && f.rel ? f.rel : C.shortPath(f.path),
    st: KIND[f.st] ? f.st : 'mod', add: +f.add || 0, del: +f.del || 0}));
}
function normSet(x) { const files = normFiles(x); return {uuid: x && x.uuid, files, add: x && x.add != null ? +x.add || 0 : files.reduce((a, f) => a + f.add, 0),
  del: x && x.del != null ? +x.del || 0 : files.reduce((a, f) => a + f.del, 0), n: files.length, expired: !!(x && x.expired)}; }

/* app2.js 的 git 审阅据此决定画不画；拿不到“按轮改动”时返回 null，git 审阅照旧 */
C.reviewScope = () => (V.sid === S.cur && V.data ? V.scope : null);
C.reviewTop = el => {
  if (V.sid !== S.cur || !V.data || V.scope !== 'git') return;
  const h = topHtml(); if (!h) return;
  el.insertAdjacentHTML('afterbegin', h);
  bindTop(el);
};
function sumHtml() {
  let n = 0, a = 0, d = 0, exp = false;
  if (V.scope === 'git') { const fs = (R.review && R.review.git && R.review.files) || []; n = fs.length; for (const f of fs) { a += +f.add || 0; d += +f.del || 0; } }
  else { const s = curSet(); if (s) { n = s.files.length; a = s.add; d = s.del; exp = s.expired; } }
  if (!n) return '';
  return `${n} 个文件${a ? ` <b class="a">+${a}</b>` : ''}${d ? ` <b class="d">−${d}</b>` : ''}${exp && V.scope === 'turn' ? '<span class="lx-tag">快照已清理</span>' : ''}`;
}
function topHtml() {
  const av = scopes(); if (av.length < 2) return '';
  return '<div class="lx-rvtop"><div class="lx-seg" role="tablist" aria-label="审阅范围">' +
    SC.filter(([k]) => av.includes(k)).map(([k, l]) => `<button type="button" role="tab" data-sc="${k}" aria-selected="${k === V.scope}"${k === V.scope ? ' class="on"' : ''}>${l}</button>`).join('') +
    `</div><span class="lx-rvsum">${sumHtml()}</span></div>`;
}
function bindTop(el) { const s = el.querySelector('.lx-seg'); if (s) s.onclick = e => { const b = e.target.closest('[data-sc]'); if (b) setScope(b.dataset.sc); }; }
function setScope(k) {
  if (!scopes().includes(k)) return;
  if (k === V.scope && !(k === 'turn' && !V.follow)) return;
  V.scope = k; V.fresh = true;
  if (k === 'turn') { V.follow = true; const l = latest(); V.uuid = l ? l.uuid : null; }
  drawReview();
}
/* 选定范围和轮次：openReview 的请求优先，其次保持原选择，默认“这一轮”（最近一个有改动的轮） */
function pick() {
  const av = scopes();
  if (V.req) {
    const q = V.req; V.req = null; V.fresh = true;
    V.scope = av.includes(q.scope) ? q.scope : av.includes('turn') ? 'turn' : av[0] || null;
    if (V.scope === 'turn' && q.uuid && turnOf(q.uuid)) { V.uuid = q.uuid; V.follow = !!(latest() && latest().uuid === q.uuid); }
    else V.follow = true;
  } else if (!av.includes(V.scope)) { V.scope = av.includes('turn') ? 'turn' : av[0] || null; V.fresh = true; V.follow = true; }
  if (V.scope === 'turn' && (V.follow || !turnOf(V.uuid))) {
    const l = latest(), u = l ? l.uuid : null;
    if (u !== V.uuid) V.fresh = true;
    V.uuid = u; V.follow = true;
  }
}
async function loadChanges(quiet) {
  const sid = S.cur, cwd = S.cwd, seq = ++V.seq;
  if (!sid) return;
  if (!V.loaded && !quiet && paneOn('review')) body.innerHTML = `<div class="lx-rv">${emptyBox('', '读取改动…')}</div>`;
  const needGit = V.git == null || V.cwd !== cwd;
  const [c, g] = await Promise.all([call('turn-changes', {sid}), needGit ? call('git-status', {cwd}) : Promise.resolve(null)]);
  if (seq !== V.seq || sid !== S.cur || V.sid !== sid) return;
  if (g) { V.git = !g.error && !!g.git; V.cwd = cwd; }
  V.loaded = true;
  let changed = false;
  if (c.error || !Array.isArray(c.turns)) { changed = !!V.data || !V.sig; V.data = null; V.sig = 'x'; }
  else {
    const data = {turns: c.turns.filter(t => t && t.uuid).map(normSet), all: normSet(c.all || {})};
    const sig = JSON.stringify(data);
    changed = sig !== V.sig; V.sig = sig;
    if (changed) {  // 增删行数变了的文件，缓存的差异作废
      const now = {};
      for (const t of data.turns) for (const f of t.files) now[t.uuid + '|' + f.path] = fsig(f);
      for (const f of data.all.files) now['*|' + f.path] = fsig(f);
      for (const k of Object.keys(V.diffs)) if (V.diffs[k].sig !== now[k]) { delete V.diffs[k]; delete V.unfold[k]; }
      V.text = {};
    }
    V.data = data;
  }
  const before = V.scope + '|' + V.uuid;
  pick();
  if (paneOn('review') && (changed || V.fresh || before !== V.scope + '|' + V.uuid)) drawReview();
}
function reviewTab() {
  if (!S.cur) { body.innerHTML = `<div class="lx-rv">${emptyBox('', '先打开一个对话')}</div>`; return; }
  if (V.sid !== S.cur) resetV();
  if (V.loaded) { pick(); drawReview(); }
  loadChanges(V.loaded);
}
C.paneTabs = Object.assign(C.paneTabs || {}, {review: reviewTab});
C.openReview = (o = {}) => {
  if (!S.cur) return;
  if (V.sid !== S.cur) resetV();
  V.req = {scope: ['turn', 'all', 'git'].includes(o.scope) ? o.scope : 'turn', uuid: o.uuid || null};
  if (paneOn('review')) { if (V.loaded) { pick(); drawReview(); } loadChanges(true); }
  else P.open('review');
};
function drawReview() {
  if (!paneOn('review') || V.sid !== S.cur) return;
  if (!V.data || V.scope === 'git') {  // 没有按轮数据，或选了“未提交”：交给 app2.js；正显示着审阅、又有 git 数据时先不清空，免得闪
    V.fresh = false;
    const showing = !!body.querySelector(':scope > .lx-rv, :scope > .lx-rvtop');
    P.loadReview(!!(showing && V.data && R.review && R.review.git && V.cwd === S.cwd));
    return;
  }
  const s = curSet(), files = s ? s.files : [];
  if (V.fresh) {
    V.fresh = false;
    if (files.length && !Object.keys(V.open).some(k => V.open[k] && k.startsWith((V.scope === 'all' ? '*' : V.uuid) + '|'))) V.open[dkey(files[0])] = true;
  }
  const keep = body.querySelector(':scope > .lx-rv') && body.dataset.lxv === V.scope + '|' + V.uuid ? body.scrollTop : 0;
  let h = topHtml();
  if (V.scope === 'turn' && s && !V.follow) {
    const u = C.userItems().find(x => x.uuid === s.uuid), t = u && (u.text || '').replace(/\s+/g, ' ').trim();
    if (t) h += `<div class="lx-rvq" title="${esc(t.slice(0, 400))}">“${esc(t.slice(0, 80))}${t.length > 80 ? '…' : ''}”</div>`;
  }
  if (!files.length) h += emptyBox(IC.diff, V.scope === 'all' ? '这个对话还没有改动文件' : '还没有改动文件');
  else h += files.map(fileHtml).join('');
  body.innerHTML = `<div class="lx-rv">${h}</div>`;
  body.dataset.lxv = V.scope + '|' + V.uuid;
  body.scrollTop = keep;
  bindReview(body.querySelector('.lx-rv'));
  for (const f of files) { const k = dkey(f); if (V.open[k] && !V.diffs[k]) fetchDiff(f); }
}
function fileHtml(f) {
  const k = dkey(f), open = !!V.open[k], kind = KIND[f.st];
  return `<div class="lx-rvf${open ? ' open' : ''}" data-k="${esc(k)}"><div class="lx-rvr" role="button" tabindex="0" aria-expanded="${open}">${IC.chev}` +
    `<span class="kind k-${kind}">${kind}</span><span class="lx-nm" title="${esc(f.path)}"><bdi>${esc(f.rel)}</bdi></span>` +
    `<span class="lx-pm">${f.add ? `<b class="a">+${f.add}</b>` : ''}${f.del ? `<b class="d">−${f.del}</b>` : ''}</span>` +
    `<button type="button" class="lx-more" title="更多" aria-label="更多">${IC.more}</button></div>` +
    (open ? `<div class="lx-diff">${diffBody(f)}</div>` : '') + '</div>';
}
const fileByKey = k => { const s = curSet(); return s ? s.files.find(f => dkey(f) === k) : null; };
function redrawFile(k) {
  const el = [...body.querySelectorAll('.lx-rvf')].find(x => x.dataset.k === k), f = fileByKey(k);
  if (!el || !f) return;
  const t = document.createElement('template'); t.innerHTML = fileHtml(f);
  el.replaceWith(t.content.firstChild);
}
function bindReview(root) {
  if (!root) return;
  bindTop(root);
  root.onclick = e => {
    const rf = e.target.closest('.lx-rvf'), k = rf && rf.dataset.k, f = k && fileByKey(k);
    if (!f) return;
    if (e.target.closest('.lx-more')) { e.stopPropagation(); fileMenu(e.target.closest('.lx-more'), f); return; }
    if (e.target.closest('.lx-retry')) { delete V.diffs[k]; fetchDiff(f); return; }
    const fo = e.target.closest('.lx-fold.can');
    if (fo) { unfold(f, k, +fo.dataset.f); return; }
    if (e.target.closest('.lx-rvr')) toggleFile(f, k);
  };
  root.onkeydown = e => {
    const r = e.target.closest && e.target.closest('.lx-rvr');
    if (!r || e.target !== r || (e.key !== 'Enter' && e.key !== ' ')) return;
    e.preventDefault();
    const k = r.parentNode.dataset.k, f = fileByKey(k); if (f) toggleFile(f, k, true);
  };
}
function toggleFile(f, k, focus) {
  V.open[k] = !V.open[k];
  redrawFile(k);
  if (V.open[k] && !V.diffs[k]) fetchDiff(f);
  if (focus) { const el = [...body.querySelectorAll('.lx-rvf')].find(x => x.dataset.k === k); if (el) el.querySelector('.lx-rvr').focus(); }
}
function fileMenu(btn, f) {
  C.menu(btn, [
    ...(f.st !== 'del' ? [{label: '预览文件', fn: () => C.preview(f.path)}, {label: '用编辑器打开', fn: () => call('open-editor', {path: f.path}).then(r => r.error && toast(r.error))}] : []),
    {label: '在访达中显示', fn: () => call('reveal', {path: f.st === 'del' ? f.path.replace(/\/[^/]+$/, '') : f.path}).then(r => r.error && toast(r.error))},
    {label: '复制路径', fn: () => C.copy(f.path).then(() => toast('已复制路径'))},
  ], {alignRight: true});
}
async function fetchDiff(f) {
  const k = dkey(f), sid = S.cur, uuid = V.scope === 'all' ? '*' : V.uuid;
  if (V.diffs[k] && !V.diffs[k].error) return;
  V.diffs[k] = {loading: true, sig: fsig(f)};
  redrawFile(k);
  const d = await call('turn-diff', {sid, uuid, path: f.path, full: true});
  if (sid !== S.cur || V.sid !== sid || !V.diffs[k] || !V.diffs[k].loading) return;
  V.diffs[k] = d.error ? {error: d.error, sig: fsig(f)} : {diff: typeof d.diff === 'string' ? d.diff : '', binary: !!d.binary, sig: fsig(f)};
  redrawFile(k);
}
/* 差异：解析成块，块之间、块内连续 8 行以上没改动的折成“N 行未改动” */
function parseDiff(text) {
  const hunks = []; let h = null, o = 0, n = 0;
  for (const raw of text.split('\n')) {
    const m = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(raw);
    if (m) { h = {o: +m[1], ol: m[2] == null ? 1 : +m[2], n: +m[3], nl: m[4] == null ? 1 : +m[4], lines: []}; hunks.push(h); o = h.o; n = h.n; continue; }
    if (!h || raw.startsWith('\\')) continue;
    const t = raw[0], s = raw.slice(1);
    if (t === '+') h.lines.push({t: 'a', no: n++, s});
    else if (t === '-') h.lines.push({t: 'd', no: o++, s});
    else if (t === ' ') { h.lines.push({t: 'c', no: n, s}); o++; n++; }
  }
  return hunks;
}
function buildRows(hunks) {
  const out = []; let next = 1, id = 0;
  const fold = (part, n) => {  // 相邻的两段（块尾没改动的几行 + 块间空档）合成一行
    const last = out[out.length - 1];
    if (last && last.fold != null) { last.n += n; last.parts.push(part); }
    else out.push({fold: id++, n, parts: [part]});
  };
  hunks.forEach(h => {
    const start = h.nl === 0 ? h.n + 1 : h.n;
    if (start - next > 0) fold({from: next, to: start - 1}, start - next);
    next = h.nl === 0 ? h.n + 1 : h.n + h.nl;
    const L = h.lines;
    for (let i = 0; i < L.length;) {
      if (L[i].t !== 'c') { out.push(L[i]); i++; continue; }
      let j = i; while (j < L.length && L[j].t === 'c') j++;
      const head = i === 0 ? 0 : 3, tail = j === L.length ? 0 : 3, hide = j - i - head - tail;
      if (hide >= 4) {
        for (let x = i; x < i + head; x++) out.push(L[x]);
        fold({lines: L.slice(i + head, j - tail)}, hide);
        for (let x = j - tail; x < j; x++) out.push(L[x]);
      } else for (let x = i; x < j; x++) out.push(L[x]);
      i = j;
    }
  });
  return out;
}
/* 块之间的空档只有“改后”就是磁盘现状时（整个对话、最近一轮）才能从磁盘补出来 */
function canDisk(f) {
  if (f.st === 'del') return false;
  if (V.scope === 'all') return true;
  const l = latest(); return !!(l && l.uuid === V.uuid);
}
const row = (t, no, html) => `<tr class="lx-l ${t}"><td class="lx-no">${no == null ? '' : no}</td><td class="lx-sg">${t === 'a' ? '+' : t === 'd' ? '−' : ''}</td><td class="lx-cd">${html || ' '}</td></tr>`;
/* 行内字符级差异高亮：成对的删/增行标出具体改动片段 */
function inlinePair(oldS, newS) {
  if (oldS === newS || !oldS || !newS || oldS.length + newS.length > 1600) return null;
  let pre = 0, aLen = oldS.length, bLen = newS.length;
  while (pre < aLen && pre < bLen && oldS[pre] === newS[pre]) pre++;
  let suf = 0;
  while (suf < aLen - pre && suf < bLen - pre && oldS[aLen - 1 - suf] === newS[bLen - 1 - suf]) suf++;
  if (pre === 0 && suf === 0) return null;
  const dMid = oldS.slice(pre, aLen - suf), aMid = newS.slice(pre, bLen - suf);
  const dHtml = esc(oldS.slice(0, pre)) + (dMid ? `<mark class="lx-wd">${esc(dMid)}</mark>` : '') + esc(oldS.slice(aLen - suf));
  const aHtml = esc(newS.slice(0, pre)) + (aMid ? `<mark class="lx-wa">${esc(aMid)}</mark>` : '') + esc(newS.slice(bLen - suf));
  return [dHtml, aHtml];
}
function diffBody(f) {
  const k = dkey(f), x = V.diffs[k];
  if (!x || x.loading) return '<div class="lx-dnote"><span class="lx-spin"></span>读取差异…</div>';
  if (x.error) return `<div class="lx-dnote">${esc(x.error)}<button type="button" class="lx-link lx-retry">重试</button></div>`;
  if (x.binary) return '<div class="lx-dnote">二进制文件，无法显示差异</div>';
  if (!x.diff) return '<div class="lx-dnote">没有文本差异</div>';
  if (!x.rows) { x.rows = buildRows(parseDiff(x.diff)); x.hl = x.rows.length < 3000; }
  const lang = C.langOf(f.path), hl = s => (x.hl ? C.hl(s, lang) : esc(s)), open = V.unfold[k] || {}, disk = canDisk(f), txt = V.text[f.path];
  const rows = x.rows, inlineHtml = new Map();
  for (let i = 0; i < rows.length;) {
    if (rows[i].fold != null || rows[i].t !== 'd') { i++; continue; }
    let dEnd = i; while (dEnd < rows.length && rows[dEnd].fold == null && rows[dEnd].t === 'd') dEnd++;
    let aEnd = dEnd; while (aEnd < rows.length && rows[aEnd].fold == null && rows[aEnd].t === 'a') aEnd++;
    const pairs = Math.min(dEnd - i, aEnd - dEnd);
    for (let p = 0; p < pairs; p++) {
      const res = inlinePair(rows[i + p].s, rows[dEnd + p].s);
      if (res) { inlineHtml.set(i + p, res[0]); inlineHtml.set(dEnd + p, res[1]); }
    }
    i = aEnd;
  }
  let h = '<table><colgroup><col class="lx-cn"><col class="lx-cs"><col></colgroup><tbody>';
  for (let idx = 0; idx < rows.length; idx++) {
    const r = rows[idx];
    if (r.fold == null) { h += row(r.t, r.no, inlineHtml.has(idx) ? inlineHtml.get(idx) : hl(r.s)); continue; }
    const gap = r.parts.some(p => !p.lines);
    if (open[r.fold] && (!gap || Array.isArray(txt))) {
      for (const p of r.parts) {
        if (p.lines) for (const l of p.lines) h += row('c', l.no, hl(l.s));
        else for (let n = p.from; n <= p.to && n <= txt.length; n++) h += row('c', n, hl(txt[n - 1]));
      }
      continue;
    }
    const can = !gap || disk;
    h += `<tr class="lx-fold${can ? ' can' : ''}" data-f="${r.fold}"><td colspan="3"><span>${can ? IC.chev : ''}${r.n} 行未改动</span></td></tr>`;
  }
  return h + '</tbody></table>';
}
async function unfold(f, k, id) {
  const x = V.diffs[k]; if (!x || !x.rows) return;
  const r = x.rows.find(y => y.fold === id); if (!r) return;
  if (r.parts.some(p => !p.lines) && !Array.isArray(V.text[f.path])) {
    const d = await call('preview', {path: f.path, cwd: S.cwd});
    if (d.error || typeof d.text !== 'string' || d.truncated) { toast(d.error || '文件太大，没法展开'); return; }
    V.text[f.path] = d.text.split('\n');
  }
  (V.unfold[k] = V.unfold[k] || {})[id] = true;
  redrawFile(k);
}
/* 一轮结束后静默刷新（审阅开着时） */
function rvSoon() {
  clearTimeout(V.timer);
  V.timer = setTimeout(() => { if (paneOn('review') && V.sid === S.cur && V.loaded) loadChanges(true); }, 900);
}

/* ====================================================================
   2. 任务页：停止后台命令
   ==================================================================== */
const STOP = {};
C.onShellCard = (a, el) => {
  const id = String(a.id).replace(/^sh:/, ''), key = S.cur + '|' + id, st = STOP[key];
  let b = el.querySelector('.lx-stop'), w = el.querySelector('.lx-agw');
  if (a.state !== 'run' || (st && st.s === 'done')) {
    if (st && a.state !== 'run') delete STOP[key];
    if (b) b.remove(); if (w) w.remove();
    return;
  }
  if (!b) {
    b = mkBtn('lx-btn lx-stop', '<span>停止</span>', '停止这个后台命令');
    b.onclick = e => { e.stopPropagation(); stopShell(id); };
    el.querySelector('.ag-h').appendChild(b);
  }
  const stopping = !!(st && st.s === 'stopping'), t = stopping ? '正在停止…' : '停止';
  if (b.textContent !== t) b.firstChild.textContent = t;
  b.disabled = stopping;
  if (st && st.s === 'fail') {
    if (!w) { w = document.createElement('div'); w.className = 'lx-agw'; (el.querySelector('.ag-l') || el.querySelector('.ag-m')).after(w); }
    if (w.textContent !== st.msg) w.textContent = st.msg;
  } else if (w) w.remove();
};
function redrawShells() { if (paneOn('tasks')) P.render(); }
async function stopShell(id) {
  const sid = S.cur, key = sid + '|' + id;
  if (STOP[key] && STOP[key].s === 'stopping') return;
  STOP[key] = {s: 'stopping'};
  syncStop(key);
  const r = await call('task-stop', {sid, id});
  if (r.error || !r.ok) { STOP[key] = {s: 'fail', msg: '没能停止，请重试'}; syncStop(key); return; }
  const t0 = Date.now();
  const poll = async () => {
    if (!STOP[key] || STOP[key].s !== 'stopping') return;
    const d = await call('agents', {sid});
    const sh = !d.error && Array.isArray(d.shells) ? d.shells.find(x => String(x.id) === id) : null;
    if (!d.error && (!sh || sh.state !== 'run')) { STOP[key] = {s: 'done'}; redrawShells(); return; }
    if (Date.now() - t0 > 4000) { STOP[key] = {s: 'fail', msg: '没能停止，请重试'}; syncStop(key); return; }
    setTimeout(poll, 800);
  };
  setTimeout(poll, 500);
}
/* 只更新这张卡片上的按钮和提示，不整页重取 */
function syncStop(key) {
  const id = key.slice(key.indexOf('|') + 1), el = body.querySelector(`.ag[data-id="${CSS.escape('sh:' + id)}"]`);
  const a = (R.shells || []).find(x => String(x.id) === id);
  if (el && a && key.startsWith(S.cur + '|')) C.onShellCard(Object.assign({}, a, {id: 'sh:' + id}), el);
}

/* ====================================================================
   3. 旁问
   ==================================================================== */
const B = {sid: null, items: [], state: 'idle', pend: null, draft: '', focus: false, seq: 0, timer: 0};
const BTW_NOTE = '旁问会带着主对话的上下文单独问一次，有缓存时费用很低。';
{
  const ext = pane.querySelector('.rtabs [data-tab="ext"]') || pane.querySelector('.rtabs .grow');
  const b = document.createElement('button');
  b.dataset.tab = 'btw'; b.textContent = '旁问'; b.title = '旁问（⌘;）';
  b.onclick = () => { B.focus = true; P.open('btw'); };
  if (ext && ext.dataset.tab) ext.after(b); else if (ext) ext.before(b);
  /* 多了一个标签：右栏窄时标签收紧一点，少截字 */
  try { new ResizeObserver(() => pane.classList.toggle('lx-rpn', pane.offsetWidth > 0 && pane.offsetWidth < 440)).observe(pane); } catch {}
}
function resetB() { clearTimeout(B.timer); Object.assign(B, {sid: S.cur, items: [], state: 'loading', pend: null, draft: '', seq: B.seq + 1}); }
async function loadBtw() {
  const sid = S.cur, seq = B.seq;
  const r = await call('btw-list', {sid});
  if (B.sid !== sid || B.seq !== seq) return;
  if (r.error || !Array.isArray(r.items)) { B.state = 'off'; }
  else {
    B.state = 'ok';
    const seen = new Map(B.items.map(x => [x.id, x]));  // 加载期间到达的事件不丢
    for (const x of r.items) if (x && x.id != null && !seen.has(x.id)) seen.set(x.id, normQa(x));
    B.items = [...seen.values()].sort((a, b) => toMs(a.t) - toMs(b.t));
  }
  if (paneOn('btw')) { drawBtwList(true); syncBtwInput(); }
}
const normQa = x => ({id: x.id, q: String(x.q || ''), a: String(x.a || ''), t: x.t || 0});
function renderBtw() {
  if (!S.cur) { body.innerHTML = `<div class="lx-btw">${emptyBox(IC.chat, '先打开一个对话')}</div>`; return; }
  if (B.sid !== S.cur) { resetB(); loadBtw(); }
  let root = body.querySelector(':scope > .lx-btw');
  if (!root || root.dataset.sid !== S.cur) {
    body.innerHTML = `<div class="lx-btw" data-sid="${esc(S.cur)}"><div class="lx-qas"></div><div class="lx-bin"><div class="lx-box">` +
      `<textarea rows="1" placeholder="顺便问一句（⌘;）" spellcheck="false" aria-label="旁问"></textarea>` +
      `<button type="button" class="lx-send" title="发送" aria-label="发送">${IC.up}<span class="lx-spin"></span></button></div></div></div>`;
    root = body.querySelector('.lx-btw');
    const ta = root.querySelector('textarea'), sb = root.querySelector('.lx-send');
    ta.value = B.draft;
    ta.oninput = () => { B.draft = ta.value; fitTa(ta); syncBtwInput(); };
    ta.onkeydown = e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); btwSend(ta.value); }
      else if (e.key === 'Escape') e.stopPropagation();
    };
    sb.onclick = () => btwSend(ta.value);
    root.querySelector('.lx-qas').onclick = btwClick;
    fitTa(ta);
    drawBtwList(true);
  }
  syncBtwInput();
  if (B.focus) { B.focus = false; const ta = root.querySelector('textarea'); ta.focus(); ta.selectionStart = ta.selectionEnd = ta.value.length; }
}
function fitTa(ta) { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 132) + 'px'; }
function syncBtwInput() {
  const root = body.querySelector(':scope > .lx-btw'); if (!root) return;
  const ta = root.querySelector('textarea'), sb = root.querySelector('.lx-send'); if (!ta) return;
  const off = B.state === 'off';
  ta.disabled = off;
  ta.placeholder = off ? '旁问暂时用不了' : '顺便问一句（⌘;）';
  sb.classList.toggle('busy', !!(B.pend && !B.pend.err));
  sb.disabled = off || !!(B.pend && !B.pend.err) || !ta.value.trim();
}
function qaHtml(x) {
  return `<div class="lx-qa" data-id="${esc(x.id)}"><div class="lx-q"><span class="lx-qt">${esc(fmtWhen(x.t, true))}</span><div class="lx-qb">${esc(x.q)}</div></div>` +
    `<div class="lx-ans md"></div><div class="lx-ops"><button type="button" class="lx-ghost" data-a="copy">${IC.copy}<span>复制</span></button>` +
    `<button type="button" class="lx-ghost" data-a="quote">${IC.quote}<span>引用到输入框</span></button></div></div>`;
}
function drawBtwList(toEnd) {
  const box = body.querySelector(':scope > .lx-btw .lx-qas'); if (!box) return;
  const stick = toEnd || box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  if (B.state === 'loading' && !B.items.length) box.innerHTML = emptyBox('', '读取中…');
  else if (B.state === 'off') box.innerHTML = emptyBox(IC.chat, '旁问暂时用不了');
  else if (!B.items.length && !B.pend) box.innerHTML = `<div class="lx-bnote">${BTW_NOTE}</div>` + emptyBox(IC.chat, '还没有旁问');
  else {
    box.innerHTML = `<div class="lx-bnote">${BTW_NOTE}</div>` + B.items.map(qaHtml).join('') + (B.pend ? pendHtml(B.pend) : '');
    for (const x of B.items) {
      const el = [...box.querySelectorAll('.lx-qa')].find(n => n.dataset.id === String(x.id));
      const a = el && el.querySelector('.lx-ans');
      if (!a) continue;
      a.innerHTML = C.md(x.a || '');
      try { C.enhance(a); } catch {}
    }
  }
  if (stick) box.scrollTop = box.scrollHeight;
}
function pendHtml(p) {
  return `<div class="lx-qa pend"><div class="lx-q"><span class="lx-qt">${esc(fmtWhen(p.t, true))}</span><div class="lx-qb">${esc(p.q)}</div></div>` +
    (p.err ? `<div class="lx-berr">${IC.warn}<span>${esc(p.err)}</span><button type="button" class="lx-link" data-a="retry">重试</button><button type="button" class="lx-link" data-a="drop">移除</button></div>`
      : '<div class="lx-wait" aria-label="正在回答"><i></i><i></i><i></i></div>') + '</div>';
}
function btwClick(e) {
  const b = e.target.closest('[data-a]'); if (!b) return;
  const a = b.dataset.a;
  if (a === 'retry' && B.pend) { const q = B.pend.q; B.pend = null; btwSend(q, true); return; }
  if (a === 'drop') { B.pend = null; drawBtwList(); syncBtwInput(); return; }
  const id = b.closest('.lx-qa') && b.closest('.lx-qa').dataset.id, x = B.items.find(y => String(y.id) === id);
  if (!x) return;
  if (a === 'copy') {
    C.copy(x.a || '');
    const sp = b.querySelector('span'); sp.textContent = '已复制'; b.classList.add('ok');
    setTimeout(() => { sp.textContent = '复制'; b.classList.remove('ok'); }, 1200);
  } else if (a === 'quote') quoteToInput(x.a || '');
}
async function btwSend(q, keepInput) {
  q = String(q || '').trim();
  if (!q || !S.cur || B.state === 'off' || (B.pend && !B.pend.err)) return;
  if (B.sid !== S.cur) { resetB(); loadBtw(); }
  const sid = S.cur;
  B.pend = {q, t: Date.now(), id: null, err: ''};
  if (!keepInput) {
    B.draft = '';
    const ta = body.querySelector(':scope > .lx-btw textarea'); if (ta) { ta.value = ''; fitTa(ta); }
  }
  drawBtwList(true); syncBtwInput();
  const r = await call('btw', {sid, q});
  if (B.sid !== sid || !B.pend || B.pend.q !== q) return;
  if (r.error || r.id == null) { B.pend.err = r.error || '没能发出'; drawBtwList(); syncBtwInput(); return; }
  B.pend.id = r.id;
  if (B.items.some(x => x.id === r.id)) { B.pend = null; drawBtwList(true); syncBtwInput(); return; }  // 答案比返回值先到
  clearTimeout(B.timer);
  B.timer = setTimeout(() => { if (B.pend && B.pend.id === r.id && !B.pend.err) { B.pend.err = '还没有收到回答'; drawBtwList(); syncBtwInput(); } }, 120000);
}
function onBtwEvent(ev) {
  if (ev.sid !== B.sid || ev.id == null) return;
  if (ev.error && B.pend && (B.pend.id === ev.id || (B.pend.id == null && B.pend.q === ev.q))) { B.pend.err = String(ev.error); }
  else {
    const it = normQa(ev), i = B.items.findIndex(x => x.id === it.id);
    if (i >= 0) B.items[i] = it; else B.items.push(it);
    if (B.pend && (B.pend.id === ev.id || (B.pend.id == null && B.pend.q === it.q))) { B.pend = null; clearTimeout(B.timer); }
    if (B.state === 'loading' || B.state === 'idle') B.state = 'ok';
  }
  if (paneOn('btw')) { drawBtwList(true); syncBtwInput(); }
}
C.paneTabs.btw = renderBtw;
/* 给 F1 的 /btw 用：打开旁问标签并发出 */
C.btwAsk = q => { if (!S.cur) return; B.focus = false; P.open('btw'); btwSend(q, true); };
C.openBtw = () => { B.focus = true; P.open('btw'); };
document.addEventListener('keydown', e => {
  if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || !(e.key === ';' || e.code === 'Semicolon')) return;
  if (C.settingsOpen && C.settingsOpen()) return;
  e.preventDefault();
  if (paneOn('btw')) { const ta = body.querySelector(':scope > .lx-btw textarea'); if (ta) { ta.focus(); return; } }
  B.focus = true; P.open('btw');
});

/* ====================================================================
   4. 记忆与指令；右栏预览编辑
   ==================================================================== */
const sj = C.sj;
const GROUP_LABEL = {user: '全局指令', project: '项目指令', auto: '自动记忆'};
function rMemory() {
  const b = sj.body(), g = sj.gen();
  b.innerHTML = sj.head('记忆与指令') + `<div class="lx-mem">${emptyBox('', '读取中…')}</div>`;
  (async () => {
    const r = await call('memory-files', {cwd: S.cwd || ''});
    if (g !== sj.gen()) return;
    const box = b.querySelector('.lx-mem'); if (!box) return;
    if (r.error || !Array.isArray(r.groups)) {
      box.innerHTML = sj.errBox('暂时拿不到记忆文件', true);
      sj.bind(box, {}, {retry: () => rMemory()});
      return;
    }
    const normP = p => String(p || '').replace(/\/+$/, '').replace(/^\/private(?=\/(tmp|var|etc)\/)/, '');  // /tmp 与 /private/tmp 是同一个
    const cwd = normP(S.cwd);
    const hint = (key, p) => {
      if (key === 'auto') return '';
      const dir = normP(p.replace(/\/[^/]+$/, ''));
      if (cwd && (dir === cwd || dir.startsWith(cwd + '/'))) return baseName(cwd) + dir.slice(cwd.length);
      return tilde(dir);
    };
    box.innerHTML = r.groups.filter(x => x && Array.isArray(x.files)).map(x => {
      const files = x.files.filter(f => f && f.path);
      return `<div class="lx-gh">${esc(x.label || GROUP_LABEL[x.key] || '')}</div><div class="lx-grp">` +
        (files.length ? files.map(f => `<button type="button" class="lx-frow" data-p="${esc(f.path)}" title="${esc(tilde(f.path))}">${IC.file}` +
          `<span class="lx-fn">${esc(f.name || baseName(f.path))}</span><span class="lx-fp">${esc(hint(x.key, f.path))}</span>` +
          `<span class="lx-ft">${esc(fmtWhen(f.mtime))}</span>${IC.right}</button>`).join('')
          : '<div class="lx-frow lx-none">暂无</div>') + '</div>';
    }).join('') || emptyBox('', '没有找到记忆文件');
    box.onclick = e => {
      const f = e.target.closest('.lx-frow[data-p]'); if (!f) return;
      sj.close();
      C.preview(f.dataset.p);
    };
  })();
}
/* “通用”里加两行：时间线详略、上下文与缓存显示 */
function generalRows() {
  const b = sj.body(), grp = b.querySelector('.sj-grp'), g = sj.gen();
  if (!grp) return;
  const wrap = document.createElement('div');
  const ctxOn = ls.get('ctxStatus', true) !== false;
  wrap.innerHTML = sj.row('时间线详略', sj.seg('density', [['simple', '简洁'], ['detail', '详细']], sj.pref('density', 'detail') === 'simple' ? 'simple' : 'detail', '时间线详略')) +
    sj.row('上下文与缓存显示', sj.sw('ctxStatus', ctxOn, true, '上下文与缓存显示'), '下次新开或重启对话后生效', 'lx-ctxrow');
  sj.bind(wrap, {
    density: v => { sj.setPref('density', v); try { if (C.applyDensity) C.applyDensity(); } catch (e) { console.error(e); } },
    ctxStatus: async (v, btn) => {
      btn.disabled = true;
      const r = await call('prefs', {set: {ctxStatus: v}});
      if (!btn.isConnected) return;
      btn.disabled = false;
      const got = r.prefs && r.prefs.ctxStatus;
      if (r.error || !r.prefs || (got !== v && !(v && got == null))) { sj.setSw(btn, !v); toast(r.error || '没能保存，请重试'); return; }
      ls.set('ctxStatus', v);
    },
  });
  const ctxRow = wrap.querySelector('.lx-ctxrow'), sw = ctxRow.querySelector('.sj-sw');
  grp.append(...wrap.children);
  call('prefs', {}).then(r => {
    if (g !== sj.gen() || !sw.isConnected) return;
    if (r.error || !r.prefs) { ctxRow.hidden = true; return; }  // 读不到偏好：这一行不显示
    const on = r.prefs.ctxStatus !== false;
    sj.setSw(sw, on); sw.disabled = false; ls.set('ctxStatus', on);
  });
}
if (sj && Array.isArray(sj.SECS) && sj.RENDER) {
  if (!sj.SECS.some(([k]) => k === 'memory')) {
    const i = sj.SECS.findIndex(([k]) => k === 'ext');
    sj.SECS.splice(i < 0 ? sj.SECS.length : i + 1, 0, ['memory', '记忆与指令']);
  }
  sj.RENDER.memory = rMemory;
  const nav = document.querySelector('#sjSet .sj-nav');  // 设置面板已经建好时补一个导航按钮
  if (nav && !nav.querySelector('[data-s="memory"]')) {
    const nb = document.createElement('button'); nb.type = 'button'; nb.dataset.s = 'memory'; nb.textContent = '记忆与指令';
    const ex = nav.querySelector('[data-s="ext"]'); if (ex) ex.after(nb); else nav.appendChild(nb);
  }
  const g0 = sj.RENDER.general;
  sj.RENDER.general = (...a) => { g0(...a); try { generalRows(); } catch (e) { console.error(e); } };
}

/* 右栏预览：Markdown、纯文本（.txt）加“编辑”，保存走 file-save，有冲突时提示 */
const E = {on: false, path: null, text: '', orig: '', mtime: null, saving: false, conflict: false};
const editKind = d => !!d && typeof d.text === 'string' && (d.kind === 'md' || (d.kind === 'text' && /\.(txt|text)$/i.test(d.path || '')));
const tooBig = d => !!d.truncated || (+d.size || 0) > 1048576;  // 预览只读了前 512KB：保存会丢掉后半部分
const canEdit = d => editKind(d) && !tooBig(d);
const pv0 = C.paneTabs.preview;
C.paneTabs.preview = (...a) => {
  const d = R.preview;
  if (E.on && d && d.path === E.path) { drawEditor(); return; }
  if (E.on) Object.assign(E, {on: false, conflict: false});
  if (pv0) pv0(...a); else P.renderPreview();
  try { addEditBtn(d); } catch (e) { console.error(e); }
};
function addEditBtn(d) {
  const h = body.querySelector(':scope > .pv-head');
  if (!h || !editKind(d) || !R.preview || R.preview.path !== d.path) return;
  const row = document.createElement('div'); row.className = 'lx-pvh';
  h.replaceWith(row); row.append(h);
  const b = mkBtn('lx-btn', IC.pen + '<span>编辑</span>');
  if (tooBig(d)) {  // 不能编辑：按钮置灰，点了说明原因（禁用的按钮不出提示，所以用 aria-disabled）
    b.classList.add('dis'); b.setAttribute('aria-disabled', 'true'); b.title = '文件太大，请用其他应用编辑';
    b.onclick = () => toast('文件太大，请用其他应用编辑');
  } else b.onclick = () => { Object.assign(E, {on: true, path: d.path, text: d.text, orig: d.text, mtime: d.mtime, saving: false, conflict: false}); drawEditor(true); };
  row.append(b);
}
function drawEditor(focus) {
  const d = R.preview;
  body.innerHTML = `<div class="lx-edw"><div class="lx-pvh"><div class="pv-head"><b title="${esc(d.path)}">${esc(d.name || baseName(d.path))}</b><span>${esc(C.shortPath(d.path))}</span></div>` +
    '<div class="lx-pvb"><button type="button" class="lx-btn" data-a="cancel">取消</button><button type="button" class="lx-btn pri" data-a="save">保存</button></div></div>' +
    (E.conflict ? `<div class="lx-conf" role="alert">${IC.warn}<span>文件已在别处被修改</span><button type="button" class="lx-link" data-a="copy">复制我的修改</button><button type="button" class="lx-link" data-a="reload">重新载入</button></div>` : '') +
    '<textarea class="lx-ed" spellcheck="false" aria-label="编辑文件"></textarea></div>';
  const ta = body.querySelector('.lx-ed');
  ta.value = E.text;
  ta.oninput = () => { E.text = ta.value; edBtns(); };
  ta.onkeydown = e => {
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 's') { e.preventDefault(); e.stopPropagation(); edSave(); }
    else if (e.key === 'Escape') e.stopPropagation();
  };
  body.querySelector('.lx-edw').onclick = e => {
    const a = e.target.closest('[data-a]'); if (!a) return;
    if (a.dataset.a === 'save') edSave();
    else if (a.dataset.a === 'cancel') edCancel();
    else if (a.dataset.a === 'reload') { const p = E.path; Object.assign(E, {on: false, conflict: false}); C.preview(p); }
    else if (a.dataset.a === 'copy') C.copy(E.text).then(() => toast('已复制你的修改'));
  };
  edBtns();
  if (focus) { ta.focus(); ta.selectionStart = ta.selectionEnd = 0; ta.scrollTop = 0; }
}
function edBtns() {
  const s = body.querySelector('.lx-edw [data-a="save"]'); if (!s) return;
  s.disabled = E.saving || E.text === E.orig;
  s.textContent = E.saving ? '保存中…' : '保存';
}
function edCancel() {
  if (E.text !== E.orig && !confirm('放弃未保存的修改？')) return;
  Object.assign(E, {on: false, conflict: false});
  if (paneOn('preview')) P.render();
}
async function edSave() {
  if (E.saving || E.text === E.orig) return;
  E.saving = true; edBtns();
  const path = E.path, text = E.text;
  const r = await call('file-save', {path, text, mtime: E.mtime});
  E.saving = false;
  if (!E.on || E.path !== path) return;
  if (r.ok && !r.error) {
    const d = R.preview;
    if (d && d.path === path) { d.text = text; if (r.mtime != null) d.mtime = r.mtime; try { d.size = new Blob([text]).size; } catch {} }
    Object.assign(E, {on: false, conflict: false, orig: text});
    if (paneOn('preview')) P.render();
    toast('已保存');
    return;
  }
  if (r.conflict || /别处|冲突|conflict|modified/i.test(r.error || '')) { E.conflict = true; if (paneOn('preview')) drawEditor(); return; }
  toast(r.error || '没能保存，请重试'); edBtns();
}

/* ====================================================================
   6. 左栏筛选、标记为未读、复制会话 ID / 记录路径
   ==================================================================== */
const FILTERS = [['all', '全部'], ['run', '进行中'], ['ask', '等我确认'], ['unread', '未读']];
const FEMPTY = {run: '没有进行中的对话', ask: '没有等你确认的对话', unread: '没有未读的对话'};
const isUnread = sid => { try { return !!(C.ux && C.ux.isUnread && C.ux.isUnread(sid)); } catch { return false; } };
const waiting = s => !!s.attention || [...(S.perms || new Map()).values()].some(p => p && p.sid === s.sid);
const PRED = {run: s => !!s.busy, ask: waiting, unread: s => isUnread(s.sid)};
let fKey = ls.get('sideFilter', 'all');
if (!PRED[fKey]) fKey = 'all';
const fbtn = mkBtn('lx-fbtn', IC.filter, '筛选');
{
  const fi = $('#filter');
  if (fi) fi.after(fbtn);
  let was = false;
  fbtn.onmousedown = () => { was = fbtn.classList.contains('open'); };  // 菜单开着时再点按钮＝收起（document 的 mousedown 已经关了菜单）
  fbtn.onclick = e => {
    e.stopPropagation();
    if (was) { was = false; C.closeMenu(); return; }
    C.menu(fbtn, FILTERS.map(([k, l]) => ({label: l, fn: () => setFilter(k)})), {alignRight: true});
    const m = S.menu; if (!m) return;
    m.classList.add('lx-menu');
    m.querySelectorAll('.mi').forEach((b, i) => { b.classList.add('lx-mi'); if (FILTERS[i][0] === fKey) b.insertAdjacentHTML('beforeend', `<i class="lx-ck">${IC.check}</i>`); });
    fbtn.classList.add('open');
    const mo = new MutationObserver(() => { if (!m.isConnected) { fbtn.classList.remove('open'); mo.disconnect(); } });
    mo.observe(document.body, {childList: true});
  };
}
function setFilter(k, quiet) {
  fKey = PRED[k] ? k : 'all'; ls.set('sideFilter', fKey === 'all' ? null : fKey);
  C.sideFilter = fKey === 'all' ? null : PRED[fKey];
  fbtn.classList.toggle('act', fKey !== 'all');
  const t = fKey === 'all' ? '筛选' : '筛选：' + FILTERS.find(x => x[0] === fKey)[1];
  fbtn.title = t; fbtn.setAttribute('aria-label', t);
  if (!quiet) C.renderSide();
}
setFilter(fKey, true);
if (S.projects && S.projects.length) C.renderSide();
chain('onSide', () => {
  const box = $('#projects'); if (!box) return;
  box.querySelector(':scope > .lx-sempty')?.remove();
  if (!C.sideFilter || box.querySelector('.srow[data-sid]')) return;
  const d = document.createElement('div'); d.className = 'lx-sempty';
  d.innerHTML = `<span>${esc(FEMPTY[fKey] || '没有符合条件的对话')}</span><button type="button" class="lx-link">显示全部</button>`;
  d.querySelector('button').onclick = () => setFilter('all');
  box.prepend(d);
});
async function markUnread(sid) {
  const r = await call('mark-unread', {sid});
  if (r.error || !r.ok) { toast('没能标记为未读'); return; }
  C.refreshSide(true);
}
async function copyPath(sid) {
  const r = await call('session-path', {sid});
  if (r.error || !r.path) { toast(r.error || '没拿到记录路径'); return; }
  await C.copy(r.path); toast('已复制记录路径');
}
const sme = C.sessionMenuExtra;
C.sessionMenuExtra = s => [...(sme ? sme(s) : []), ...(isUnread(s.sid) || s.cwbParent ? [] : [{label: '标记为未读', fn: () => markUnread(s.sid)}])];  // 11-25：子对话不标未读
{
  const mb = $('#moreBtn'), prev = mb && mb.onclick;
  if (mb) mb.onclick = e => {
    if (prev) prev.call(mb, e);
    const m = S.menu, sid = S.cur;
    if (!m || !sid) return;
    const item = (label, fn) => { const b = document.createElement('button'); b.className = 'mi'; b.innerHTML = `<span>${esc(label)}</span>`; b.onclick = ev => { ev.stopPropagation(); C.closeMenu(); fn(); }; return b; };
    const seps = m.querySelectorAll('.msep'), first = seps[0], last = seps[seps.length - 1];
    if (!isUnread(sid) && !(C.findSession(sid) || {}).cwbParent) { const u = item('标记为未读', () => markUnread(sid)); if (first) m.insertBefore(u, first); else m.appendChild(u); }
    const sp = document.createElement('div'); sp.className = 'msep';
    const a = item('复制会话 ID', () => C.copy(sid).then(() => toast('已复制会话 ID'))), b = item('复制记录路径', () => copyPath(sid));
    if (last) { m.insertBefore(sp, last); m.insertBefore(a, last); m.insertBefore(b, last); } else m.append(sp, a, b);
  };
}

/* ====================================================================
   7. 顶栏“用…打开”
   ==================================================================== */
const O = {apps: null, state: 'idle', last: ls.get('openWith', null)};
const appIcon = x => { const s = ((x && x.key) || '') + ' ' + ((x && x.name) || ''); return /finder|访达/i.test(s) ? IC.folder : /cursor/i.test(s) ? IC.cursor : /code/i.test(s) ? IC.code : /term|终端|iterm|warp|ghostty/i.test(s) ? IC.term : IC.app; };
const owTitle = x => { const n = (x && x.name) || '访达', sp = /^[a-z]/i.test(n) ? ' ' : ''; return '用' + sp + n + sp + '打开'; };
const ow = document.createElement('div'); ow.id = 'lxOw'; ow.className = 'lx-ow';
const owMain = mkBtn('h-ib lx-ow-m', IC.folder), owMore = mkBtn('lx-ow-c', IC.chev, '选择打开方式');
owMain.dataset.tip = '在访达中打开'; owMain.setAttribute('aria-label', '在访达中打开');
ow.append(owMain, owMore);
{
  const toc = $('#tocBtn'); if (toc) toc.before(ow); else { const t = $('#top'); if (t) t.appendChild(ow); }
}
const curApp = () => (O.apps && O.apps.length ? O.apps.find(x => x.key === O.last) || O.apps.find(x => /finder|访达/i.test(x.key + x.name)) || O.apps[0] : null);
function syncOw() {
  const a = curApp(), t = a ? owTitle(a) : '在访达中打开';
  owMain.innerHTML = a ? appIcon(a) : IC.folder;
  owMain.dataset.tip = t; owMain.setAttribute('aria-label', t);
  owMore.hidden = !(O.apps && O.apps.length);
  ow.classList.toggle('solo', owMore.hidden);
  ow.hidden = !S.cwd;
}
async function loadApps() {
  if (O.state === 'busy') return;
  O.state = 'busy';
  const r = await call('open-apps', {});
  O.state = 'done'; O.at = Date.now();
  O.apps = !r.error && Array.isArray(r.apps) ? r.apps.filter(x => x && x.key) : null;
  syncOw();
}
async function openWith(a) {
  if (!S.cwd) return;
  if (!a) { const r = await call('reveal', {path: S.cwd}); if (r.error) toast(r.error); return; }
  const r = await call('open-with', {cwd: S.cwd, app: a.key});
  if (r.error || !r.ok) toast(r.error || '没能打开');
}
owMain.onclick = () => openWith(curApp());
let owWas = false;
owMore.onmousedown = () => { owWas = ow.classList.contains('open'); };
owMore.onclick = e => {
  e.stopPropagation();
  if (!O.apps || !O.apps.length) return;
  if (owWas) { owWas = false; C.closeMenu(); return; }
  const cur = curApp();
  C.menu(ow, O.apps.map(x => ({label: x.name || x.key, fn: () => { O.last = x.key; ls.set('openWith', x.key); syncOw(); openWith(x); }})), {alignRight: true});
  const m = S.menu; if (!m) return;
  m.classList.add('lx-menu');
  m.querySelectorAll('.mi').forEach((b, i) => {
    const x = O.apps[i]; b.classList.add('lx-mi');
    b.insertAdjacentHTML('afterbegin', `<span class="lx-mic">${appIcon(x)}</span>`);
    if (cur && x.key === cur.key) b.insertAdjacentHTML('beforeend', `<i class="lx-ck">${IC.check}</i>`);
  });
  ow.classList.add('open');
  const mo = new MutationObserver(() => { if (!m.isConnected) { ow.classList.remove('open'); mo.disconnect(); } });
  mo.observe(document.body, {childList: true});
};
syncOw();
setTimeout(loadApps, 800);
addEventListener('focus', () => { if (!O.apps && O.state === 'done' && Date.now() - (O.at || 0) > 60000) { O.at = Date.now(); loadApps(); } });  // 没拿到清单时，回到窗口再试（至多每分钟一次）
C.lx = {loadApps};  // 联调、测试用

/* ====================================================================
   接入：载入对话、推送事件
   ==================================================================== */
chain('onLoad', () => {
  syncOw();
  if (V.sid && V.sid !== S.cur) { resetV(); V.sid = null; }
  if (B.sid && B.sid !== S.cur) { clearTimeout(B.timer); Object.assign(B, {sid: null, items: [], state: 'idle', pend: null, draft: ''}); }
  if (E.on && (!R.preview || R.preview.path !== E.path)) Object.assign(E, {on: false, conflict: false});
});
chain('onEvent', ev => {
  if (!ev) return;
  if (ev.type === 'btw') { onBtwEvent(ev); return; }
  if (ev.sid !== S.cur || !S.cur) return;
  if (ev.type === 'items' && (ev.patch || []).some(x => x && x.done)) rvSoon();
  else if (ev.type === 'runner') {
    const st = ev.state || {};
    if (S.busyMap[ev.sid] && !(st.alive && st.busy)) rvSoon();  // 这里还是上一次的忙闲状态：忙 → 闲＝一轮结束
  }
});
const clr = C.clearMain;
if (clr) C.clearMain = (...a) => { const r = clr(...a); syncOw(); return r; };
})();
