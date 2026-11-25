(() => {
'use strict';
/* 第四轮：右侧面板（预览 / 审阅 / 任务 / 定时 / 扩展）、命令面板、引用、上下文、预热、分叉、工作树、项目默认设置 */
const C = window.CWB;
const {S, api, $, esc, md, toast, menu, askText} = C;
const ls = {get(k, d) { try { const v = localStorage.getItem('cwb2.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('cwb2.' + k, JSON.stringify(v)); } catch {} }};
const R = {tab: ls.get('tab', 'review'), preview: null, review: null, open: {}, comments: [], diffs: {}, checked: {}, tasks: [], scheds: [], ext: null};
const pane = $('#rpane'), body = $('#rbody');
const base = p => (p || '').split('/').filter(Boolean).pop() || p;
const fmtSize = n => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const MODE_LABEL = {'default': '默认', 'manual mode': '手动确认', 'plan mode': '规划模式', 'auto mode': '自动', 'accept edits': '自动接受编辑'};

/* ================= 右侧面板框架 ================= */
function syncTopPane() {
  const open = !pane.hidden;
  const rb = $('#reviewBtn'), tb = $('#taskBtn');
  if (rb) rb.classList.toggle('open', open && R.tab === 'review');
  if (tb) tb.classList.toggle('open', open && R.tab === 'tasks');
}
function openPane(tab) {
  R.tab = tab || R.tab; ls.set('tab', R.tab);
  pane.hidden = false; document.body.classList.add('rp');
  pane.querySelectorAll('.rtabs [data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === R.tab));
  syncTopPane();
  render();
}
function closePane() { pane.hidden = true; document.body.classList.remove('rp'); syncTopPane(); }
pane.querySelectorAll('.rtabs [data-tab]').forEach(b => b.onclick = () => openPane(b.dataset.tab));
$('#rclose').onclick = closePane;
const maxPane = () => Math.max(300, innerWidth - ($('#side').offsetWidth || 0) - 420);  // 第十二轮 C：主区至少留约 420（原来 600）
const fitPane = w => Math.max(300, Math.min(maxPane(), w));
const setPane = () => { pane.style.width = fitPane(ls.get('width', 520)) + 'px'; };  // 第十二轮 C：抽成函数，导出给 ux-c.js
setPane();
addEventListener('resize', setPane);
C.fitPane = setPane;  // 第十二轮 C：左栏收起、拖宽、双击复位后重算右栏宽度
$('#rgrip').onmousedown = e => {
  e.preventDefault();
  const x0 = e.clientX, w0 = pane.offsetWidth;
  const mv = ev => { if (!ev.buttons) return up(); pane.style.width = fitPane(w0 + x0 - ev.clientX) + 'px'; };
  const up = () => { removeEventListener('mousemove', mv); removeEventListener('mouseup', up); ls.set('width', pane.offsetWidth); };
  addEventListener('mousemove', mv); addEventListener('mouseup', up);
};
function render() {
  if (pane.hidden) return;
  // 第十一轮 B：feat.js 可以新增或接管标签（搜索、预览里的网页、扩展管理），见 C.paneTabs
  const fn = (C.paneTabs || {})[R.tab] || ({preview: renderPreview, review: loadReview, tasks: loadTasks, sched: loadScheds, ext: loadExt})[R.tab];
  if (fn) fn();
}
C.pane = {R, body, open: openPane, close: closePane, render, renderPreview, loadReview};  // 第十一轮 B：给 feat.js 用；第十五轮 F2：loadReview 给 ux-l.js（审阅范围切换）
$('#reviewBtn').onclick = () => (pane.hidden || R.tab !== 'review') ? openPane('review') : closePane();
$('#taskBtn').onclick = () => (pane.hidden || R.tab !== 'tasks') ? openPane('tasks') : closePane();

/* ================= 预览 ================= */
C.preview = async path => {
  try {
    const d = await api('preview', {path, cwd: S.cwd, sid: S.cur});  // 11-23：带对话 ID，只写文件名时按对话里出现过的完整路径找回
    if (d.kind === 'other') { await api('open-file', {path: d.path}); return; }
    R.preview = d; openPane('preview');
  } catch (e) { toast(e.message); }
};
function csvRows(text, sep) {
  const rows = []; let row = [], cur = '', q = false;
  for (let i = 0; i < text.length && rows.length < 300; i++) {
    const ch = text[i];
    if (q) { if (ch === '"' && text[i + 1] === '"') { cur += '"'; i++; } else if (ch === '"') q = false; else cur += ch; }
    else if (ch === '"') q = true;
    else if (ch === sep) { row.push(cur); cur = ''; }
    else if (ch === '\n') { row.push(cur.replace(/\r$/, '')); rows.push(row); row = []; cur = ''; }
    else cur += ch;
  }
  if (cur || row.length) { row.push(cur); rows.push(row); }
  return rows;
}
function renderPreview() {
  const d = R.preview;
  if (!d) { body.innerHTML = '<div class="rempty">点击对话里的文件路径，在这里预览</div>'; return; }
  const acts = `<div class="racts"><button class="btn small" data-a="open">用默认程序打开</button><button class="btn small" data-a="editor">用编辑器打开</button>` +
    `<button class="btn small" data-a="finder">在访达中显示</button><button class="btn small" data-a="copy">复制路径</button></div>`;
  let main = '';
  if (d.kind === 'image') main = `<div class="pv-img"><img src="${esc(C.fileUrl(d.path))}" alt=""></div>`;
  else if (d.kind === 'pdf') main = `<iframe class="pv-pdf" src="${esc(C.fileUrl(d.path))}"></iframe>`;
  else if (d.kind === 'md') main = `<div class="md pv-md">${md(d.text)}</div>`;
  else if (d.kind === 'csv') {
    const rows = csvRows(d.text, d.sep);
    main = `<div class="pv-csv"><table>${rows.map((r, i) => `<tr>${r.map(c => i ? `<td>${esc(c)}</td>` : `<th>${esc(c)}</th>`).join('')}</tr>`).join('')}</table></div>`;
  } else if (d.kind === 'dir') {
    const up = d.path.replace(/\/[^/]+\/?$/, '') || '/';
    main = `<div class="pv-dir"><div class="pv-ent up" data-p="${esc(up)}">⬑ 上一级</div>` +
      d.entries.map(e => `<div class="pv-ent${e.dir ? ' dir' : ''}" data-p="${esc(d.path + '/' + e.name)}">${e.dir ? '▸ ' : ''}${esc(e.name)}</div>`).join('') + '</div>';
  } else {
    const lang = C.langOf(d.path), lines = d.text.split('\n');
    const big = d.text.length > 200000;
    main = `<div class="pv-code"><table>${lines.map((l, i) => `<tr><td class="ln">${i + 1}</td><td>${big ? esc(l) : C.hl(l, lang)}</td></tr>`).join('')}</table></div>`;
  }
  body.innerHTML = `<div class="pv-head"><b title="${esc(d.path)}">${esc(d.name)}</b><span>${esc(C.shortPath(d.path))}${d.kind === 'dir' ? '' : ' · ' + fmtSize(d.size)}</span></div>` + acts +
    (d.truncated ? '<div class="rnote">文件较大，只显示前 500 KB</div>' : '') + main;
  body.querySelector('.racts').onclick = e => {
    const a = e.target.dataset.a; if (!a) return;
    if (a === 'open') api('open-file', {path: d.path}); else if (a === 'editor') api('open-editor', {path: d.path});
    else if (a === 'finder') api('reveal', {path: d.path}); else if (a === 'copy') C.copy(d.path, e.target);
  };
  body.querySelectorAll('.pv-ent').forEach(el => el.onclick = () => C.preview(el.dataset.p));
  if (d.kind === 'md') C.enhance(body.querySelector('.pv-md'));
}

/* ================= 审阅（git 改动） ================= */
let reviewTimer = null, rvTimer = null;
/* 第十五轮 F2：审阅顶部的范围切换（ux-l.js）。范围不是“未提交”时这里不画；每次重画后补上顶部 */
const rvScope = () => { try { return C.reviewScope ? C.reviewScope() : null; } catch { return null; } };
const rvTop = () => { try { if (C.reviewTop) C.reviewTop(body); } catch (e) { console.error(e); } };
async function loadReview(quiet) {
  if (rvScope() && rvScope() !== 'git') return;
  if (!S.cur) { body.innerHTML = '<div class="rempty">先打开一个对话</div>'; return; }
  if (!quiet) { body.innerHTML = '<div class="rempty">读取改动…</div>'; rvTop(); }
  const cwd = S.cwd, sid = S.cur;
  let d;
  try { d = await api('git-status', {cwd}); } catch (e) { if (S.cwd === cwd && R.tab === 'review' && (!rvScope() || rvScope() === 'git')) { body.innerHTML = `<div class="rempty">${esc(e.message)}</div>`; rvTop(); } return; }
  if (S.cwd !== cwd || S.cur !== sid) return;
  // 增删行数变了的文件，缓存的差异作废，展开着的重新取
  const old = R.review && R.review.files ? Object.fromEntries(R.review.files.map(f => [f.path, f.add + '/' + f.del])) : {};
  R.review = d;
  for (const f of d.files || []) if (R.diffs[f.path] && old[f.path] !== f.add + '/' + f.del) { delete R.diffs[f.path]; if (R.open[f.path]) fetchDiff(f); }
  if (R.tab === 'review' && !pane.hidden && (!rvScope() || rvScope() === 'git')) renderReview();
}
function lineRows(path, diff, lang) {
  const out = []; let o = 0, n = 0;
  for (const raw of diff.split('\n')) {
    if (/^(diff --git|index |--- |\+\+\+ |new file|deleted file|similarity|rename |old mode|new mode)/.test(raw)) continue;
    const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(raw);
    if (m) { o = +m[1]; n = +m[2]; out.push(`<tr class="hunk"><td></td><td></td><td>${esc(raw)}</td></tr>`); continue; }
    if (raw.startsWith('\\')) continue;
    const t = raw[0], s = raw.slice(1);
    let on = '', nn = '', cls = 'same';
    if (t === '+') { nn = n++; cls = 'add'; } else if (t === '-') { on = o++; cls = 'del'; } else if (t === ' ') { on = o++; nn = n++; } else continue;
    const line = nn || on, side = nn ? 'new' : 'old';
    const cm = R.comments.filter(c => c.path === path && c.line === line && c.side === side);
    out.push(`<tr class="dl2 ${cls}" data-line="${line}" data-side="${side}"><td class="ln">${on}</td><td class="ln">${nn}</td><td><i>${t === ' ' ? '' : t}</i>${C.hl(s, lang) || ' '}</td></tr>` +
      cm.map(c => `<tr class="cmt"><td></td><td></td><td><span>💬 ${esc(c.text)}</span><button data-del="${R.comments.indexOf(c)}">删除</button></td></tr>`).join(''));
  }
  return out.join('');
}
function renderReview() {
  if (rvScope() && rvScope() !== 'git') return;
  const d = R.review;
  if (!d.git) {
    const ch = C.changedFiles();
    body.innerHTML = '<div class="rempty">这个文件夹不是 git 仓库，没有可审阅的差异。</div>' +
      (ch.length ? '<div class="rsec">这个对话改过的文件</div>' + ch.map(([p]) => `<div class="rfile"><a data-path="${esc(p)}">${esc(C.shortPath(p))}</a></div>`).join('') : '');
    rvTop();
    return;
  }
  const files = d.files;
  for (const f of files) if (!(f.path in R.checked)) R.checked[f.path] = !f.untracked;
  const sync = d.upstream ? `${d.ahead ? '↑' + d.ahead : ''}${d.behind ? ' ↓' + d.behind : ''}` : '未关联远端';
  const isGh = /github\.com/.test(d.remote || '');
  body.innerHTML = `<div class="rv-head"><span class="branch">⎇ ${esc(d.branch)}</span><span class="sync">${esc(sync.trim() || '已同步')}</span><span class="grow"></span>` +
    `<button class="btn small" data-a="refresh">刷新</button></div>` +
    `<div class="racts"><button class="btn small primary" data-a="commit"${files.length ? '' : ' disabled'}>提交…</button><button class="btn small" data-a="claude"${files.length ? '' : ' disabled'}>让 Claude 提交</button>` +
    `<button class="btn small" data-a="push">推送</button>${isGh ? '<button class="btn small" data-a="pr">发起合并请求</button>' : ''}</div>` +
    (R.comments.length ? `<div class="rv-cm">审阅意见 ${R.comments.length} 条<span class="grow"></span><button class="btn small primary" data-a="sendcm">发给 Claude</button><button class="btn small" data-a="clearcm">清空</button></div>` : '') +
    (files.length ? '' : '<div class="rempty">没有未提交的改动</div>') +
    files.map(f => `<div class="rv-file${R.open[f.path] ? ' open' : ''}" data-path-key="${esc(f.path)}">` +
      `<div class="rv-row"><input type="checkbox"${R.checked[f.path] ? ' checked' : ''} title="提交时包含"><span class="kind k-${esc(f.kind)}">${esc(f.kind)}</span>` +
      `<span class="rv-name" title="${esc(f.path)}"><bdi>${esc(f.path)}</bdi></span><span class="nums">${f.add ? `<b class="a">+${esc(f.add)}</b>` : ''}${f.del ? `<b class="d">−${esc(f.del)}</b>` : ''}</span>` +
      `<button class="smore" title="更多">⋯</button></div>` +
      (R.open[f.path] ? `<div class="rv-diff">${diffHtml(f)}</div>` : '') + '</div>').join('') +
    (d.more ? `<div class="rnote">还有 ${d.more} 个文件未列出</div>` : '');
  rvTop();
  body.querySelector('.rv-head').onclick = e => { if (e.target.dataset.a === 'refresh') { R.diffs = {}; loadReview(); } };
  body.querySelector('.racts').onclick = e => reviewAct(e.target.dataset.a, d);
  const cmb = body.querySelector('.rv-cm'); if (cmb) cmb.onclick = e => reviewAct(e.target.dataset.a, d);
  body.querySelectorAll('.rv-file').forEach(el => {
    const f = files.find(x => x.path === el.dataset.pathKey);
    el.querySelector('input').onchange = e => { R.checked[f.path] = e.target.checked; };
    el.querySelector('.rv-name').onclick = () => { R.open[f.path] = !R.open[f.path]; if (R.open[f.path] && !R.diffs[f.path]) fetchDiff(f); else renderReview(); };
    el.querySelector('.smore').onclick = e => menu(e.target, [
      {label: '预览文件', fn: () => C.preview(d.top + '/' + f.path)},
      {label: '用编辑器打开', fn: () => api('open-editor', {path: d.top + '/' + f.path})},
      {label: '在访达中显示', fn: () => api('reveal', {path: d.top + '/' + f.path})},
      {sep: true},
      {label: f.untracked || f.kind === '新增' ? '撤销（移到废纸篓）' : '撤销改动', danger: true, fn: () => revert(f)},
    ], {alignRight: true});
    const tb = el.querySelector('.rv-diff table');
    if (tb) tb.onclick = e => {
      const del = e.target.dataset.del;
      if (del != null) { R.comments.splice(+del, 1); renderReview(); return; }
      const tr = e.target.closest('tr.dl2'); if (!tr) return;
      addComment(f.path, +tr.dataset.line, tr.dataset.side, tr.querySelector('td:last-child').textContent.replace(/^[+-]/, ''));
    };
  });
}
function diffHtml(f) {
  const x = R.diffs[f.path];
  if (!x) return '<div class="rnote">读取差异…</div>';
  if (f.dir) return '<div class="rnote">未跟踪的文件夹，展开请在访达中查看</div>';
  if (x.binary) return '<div class="rnote">二进制文件，无法显示差异</div>';
  if (!x.diff) return '<div class="rnote">没有文本差异</div>';
  return `<table>${lineRows(f.path, x.diff, C.langOf(f.path))}</table>`;
}
async function fetchDiff(f) {
  renderReview();
  const cwd = S.cwd;
  let x;
  try { x = await api('git-diff', {cwd, path: f.path}); } catch (e) { x = {diff: '', binary: false}; if (S.cwd === cwd) toast(e.message); }
  if (S.cwd !== cwd) return;
  R.diffs[f.path] = x;
  renderReview();
}
async function addComment(path, line, side, code) {
  const v = await askText(`${path} 第 ${line} 行的意见`, '');
  if (!v || !v.trim()) return;
  R.comments.push({path, line, side, code: code.trim().slice(0, 80), text: v.trim()});
  renderReview();
}
async function revert(f) {
  const msg = f.untracked || f.kind === '新增' ? `把新文件 ${f.path} 移到废纸篓？` : `撤销 ${f.path} 的所有改动，恢复成最近一次提交的内容？`;
  if (!confirm(msg)) return;
  try { await api('git-revert', {cwd: S.cwd, path: f.path}); delete R.diffs[f.path]; toast('已撤销'); loadReview(true); } catch (e) { toast(e.message); }
}
async function reviewAct(a, d) {
  if (!a) return;
  if (a === 'sendcm') {
    const txt = '请根据下面的审阅意见修改：\n\n' + R.comments.map((c, i) => `${i + 1}. \`${c.path}\` 第 ${c.line} 行${c.code ? `（\`${c.code}\`）` : ''}：${c.text}`).join('\n');
    const inp = $('#input'); inp.value = (inp.value.trim() ? inp.value.replace(/\s+$/, '') + '\n\n' : '') + txt; inp.dispatchEvent(new Event('input')); inp.focus();
    R.comments = []; renderReview(); toast('已填入输入框，确认后发送');
  } else if (a === 'clearcm') { R.comments = []; renderReview(); }
  else if (a === 'claude') C.sendText('请检查当前未提交的改动，写好提交说明后提交。不要推送。');
  else if (a === 'commit') {
    const paths = d.files.filter(f => R.checked[f.path]).map(f => f.path);
    if (!paths.length) { toast('先勾选要提交的文件'); return; }
    const m = await askText(`提交 ${paths.length} 个文件，填写提交说明`, '', true);
    if (!m || !m.trim()) return;
    try { await api('git-commit', {cwd: S.cwd, message: m.trim(), paths}); toast('已提交'); R.diffs = {}; R.checked = {}; loadReview(true); } catch (e) { toast(e.message); }
  } else if (a === 'push') {
    if (!confirm(`把分支 ${d.branch} 推送到远端？\n${d.remote || ''}`)) return;
    toast('正在推送…');
    try { const r = await api('git-push', {cwd: S.cwd}); toast('推送完成'); loadReview(true); console.log(r.out); } catch (e) { toast(e.message); }
  } else if (a === 'pr') {
    if (!confirm(`为分支 ${d.branch} 发起合并请求？`)) return;
    try { const r = await api('git-pr', {cwd: S.cwd}); toast('已创建：' + r.url); if (r.url) api('open-url', {url: r.url}); } catch (e) { toast(e.message); }
  }
}

/* ================= 任务：当前对话的子代理和后台命令（11-13） ================= */
R.agents = []; R.shells = []; R.agOpen = {};
const fmtDur = s => { s = Math.max(0, Math.round(s)); return s < 60 ? s + ' 秒' : s < 3600 ? Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒' : Math.floor(s / 3600) + ' 小时 ' + Math.floor(s % 3600 / 60) + ' 分'; };
const AG_STATE = {run: '运行中', done: '已完成', stale: '未完成', err: '出错', killed: '已停止'};  // 11-23：Codex / Antigravity 子代理被停掉
const SH_STATE = {run: '运行中', done: '已完成', stale: '未完成', err: '失败', killed: '已停止'};
async function loadTasks() {
  const sid = S.cur;
  let list = [], shells = [];
  if (sid) { try { const d = await api('agents', {sid}); list = d.agents || []; shells = d.shells || []; } catch { list = R.agents; shells = R.shells; } }
  if (sid !== S.cur) return;
  R.agents = list; R.shells = shells; R.agSid = sid;
  updateTaskBtn();
  if (!pane.hidden && R.tab === 'tasks') renderAgents();
}
function agMeta(a) {
  const now = Date.now() / 1000, dur = a.start ? fmtDur((a.end || now) - a.start) : '';
  return [a.type, a.bg ? '后台' : '', a.tools ? `工具 ${a.tools} 次` : '', a.out ? '输出 ' + C.fmtTok(a.out) : '', dur].filter(Boolean).join(' · ');
}
function shMeta(a) {
  const now = Date.now() / 1000, dur = a.start ? '用时 ' + fmtDur((a.end || now) - a.start) : '';
  return ['后台命令', dur, a.code ? '退出码 ' + a.code : '', a.size ? '输出 ' + fmtSize(a.size) : ''].filter(Boolean).join(' · ');
}
/* 后台命令卡片：展开看输出文件最后几十行，运行中跟着刷新 */
function renderShell(a, el) {
  const set = (sel, v) => { const n = el.querySelector(sel); if (n.textContent !== v) n.textContent = v; };
  el.querySelector('.sdot').className = 'sdot ' + (a.state === 'run' ? 'busy' : a.state === 'done' ? 'on' : a.state === 'err' ? 'att' : '');
  el.classList.toggle('err', a.state === 'err');
  el.classList.toggle('run', a.state === 'run');
  set('.ag-t', a.desc || a.cmd.split('\n')[0].slice(0, 60) || '后台命令'); set('.ag-s', SH_STATE[a.state] || ''); set('.ag-m', shMeta(a));
  set('.ag-l', a.last ? (a.state === 'run' ? '最新输出：' : '最后输出：') + a.last : '');
  if (R.agOpen[a.id]) refreshOut(a, el);
  try { if (C.onShellCard) C.onShellCard(a, el); } catch (e) { console.error(e); }  // 第十五轮 F2：运行中的后台命令加“停止”（ux-l.js）
}
function toggleOut(id) {
  R.agOpen[id] = !R.agOpen[id];
  const el = body.querySelector(`.ag[data-id="${CSS.escape(id)}"]`); if (!el) return;
  const p = el.querySelector('.ag-p'), b = el.querySelector('[data-a=proc]');
  p.hidden = !R.agOpen[id]; b.textContent = R.agOpen[id] ? '收起输出' : '查看输出';
  if (R.agOpen[id]) { p.dataset.sig = ''; const a = R.shells.find(x => 'sh:' + x.id === id); if (a) refreshOut(Object.assign({}, a, {id}), el, true); }
}
async function refreshOut(a, el, force) {
  const p = el.querySelector('.ag-p'), sig = a.size + ':' + a.state;
  if ((!force && p.dataset.sig === sig) || p.dataset.busy) return;
  p.dataset.busy = 1;
  try {
    const d = await api('shell-output', {sid: S.cur, id: a.id.replace(/^sh:/, '')});
    p.dataset.sig = sig;
    if (d.error) { p.innerHTML = `<div class="note">${esc(d.error)}</div>`; return; }
    if (!d.text) { p.innerHTML = `<div class="note">${a.state === 'run' ? '还没有输出' : '没有输出'}</div>`; return; }
    let pre = p.querySelector('pre.sh-out');
    if (!pre) { p.innerHTML = '<pre class="sh-out"></pre>'; pre = p.querySelector('pre.sh-out'); }
    const stick = p.scrollHeight - p.scrollTop - p.clientHeight < 40;
    const t = (d.more ? '…（只显示最后几十行）\n' : '') + d.text;
    if (pre.textContent !== t) pre.textContent = t;
    if (stick || force) p.scrollTop = p.scrollHeight;
  } catch (e) { toast(e.message); }
  finally { delete p.dataset.busy; }
}
/* 按 id 原地更新卡片，展开的过程只追加新内容，不整块重画（避免闪烁） */
function renderAgents() {
  if (R.agSid !== S.cur) { body.innerHTML = ''; }
  let box = body.querySelector('.aglist');
  if (!box) { body.innerHTML = '<div class="aglist"></div>'; box = body.querySelector('.aglist'); }
  const all = [...R.agents.map(a => Object.assign({kind: 'ag'}, a)), ...(R.shells || []).map(a => Object.assign({}, a, {kind: 'sh', id: 'sh:' + a.id}))]
    .sort((x, y) => (x.state !== 'run') - (y.state !== 'run') || (y.start || 0) - (x.start || 0));
  if (!S.cur || !all.length) { box.innerHTML = `<div class="rempty">${S.cur ? '这个对话还没有子代理或后台命令' : '先打开一个对话'}</div>`; return; }
  box.querySelector('.rempty')?.remove();
  const seen = new Set();
  all.forEach((a, i) => {
    seen.add(a.id);
    let el = box.querySelector(`.ag[data-id="${CSS.escape(a.id)}"]`);
    if (a.kind === 'sh') {
      if (!el) {
        el = document.createElement('div'); el.className = 'ag sh'; el.dataset.id = a.id;
        el.innerHTML = '<div class="ag-h"><span class="sdot"></span><b class="ag-t"></b><span class="ag-s"></span></div><div class="ag-m"></div><div class="ag-l"></div>' +
          '<div class="ag-b"><button class="link" data-a="proc">查看输出</button><button class="link" data-a="loc">在对话中定位</button></div><div class="ag-p" hidden></div>';
        el.querySelector('[data-a=proc]').onclick = () => toggleOut(a.id);
        el.querySelector('[data-a=loc]').onclick = () => locateAgent(a.id);
        el.querySelector('.ag-h').onclick = () => toggleOut(a.id);
        if (R.agOpen[a.id]) { el.querySelector('.ag-p').hidden = false; el.querySelector('[data-a=proc]').textContent = '收起输出'; }
      }
      if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
      renderShell(a, el);
      return;
    }
    if (!el) {
      el = document.createElement('div'); el.className = 'ag'; el.dataset.id = a.id;
      el.innerHTML = '<div class="ag-h"><span class="sdot"></span><b class="ag-t"></b><span class="ag-s"></span></div><div class="ag-m"></div><div class="ag-l"></div>' +
        '<div class="ag-b"><button class="link" data-a="proc">查看过程</button><button class="link" data-a="loc">在对话中定位</button></div><div class="ag-p tl" hidden></div>';
      el.querySelector('[data-a=proc]').onclick = () => toggleProc(a.id);
      el.querySelector('[data-a=loc]').onclick = () => locateAgent(a.id);
      el.querySelector('.ag-h').onclick = () => toggleProc(a.id);
      if (R.agOpen[a.id]) { el.querySelector('.ag-p').hidden = false; el.querySelector('[data-a=proc]').textContent = '收起过程'; }
    }
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
    const set = (sel, v) => { const n = el.querySelector(sel); if (n.textContent !== v) n.textContent = v; };
    el.querySelector('.sdot').className = 'sdot ' + (a.state === 'run' ? 'busy' : a.state === 'done' ? 'on' : a.state === 'err' ? 'att' : '');
    el.classList.toggle('err', a.state === 'err');
    el.classList.toggle('run', a.state === 'run');
    set('.ag-t', a.desc || a.type || '子代理'); set('.ag-s', AG_STATE[a.state] || ''); set('.ag-m', agMeta(a));
    set('.ag-l', a.last ? (a.state === 'run' ? '正在：' : '最后：') + a.last : '');
    if (R.agOpen[a.id]) refreshProc(a, el);
  });
  box.querySelectorAll('.ag').forEach(el => { if (!seen.has(el.dataset.id)) el.remove(); });
}
function toggleProc(id) {
  R.agOpen[id] = !R.agOpen[id];
  const el = body.querySelector(`.ag[data-id="${CSS.escape(id)}"]`); if (!el) return;
  const p = el.querySelector('.ag-p'), b = el.querySelector('[data-a=proc]');
  p.hidden = !R.agOpen[id]; b.textContent = R.agOpen[id] ? '收起过程' : '查看过程';
  if (R.agOpen[id]) { p.dataset.sig = ''; const a = R.agents.find(x => x.id === id); if (a) refreshProc(a, el, true); }
}
async function refreshProc(a, el, force) {
  const p = el.querySelector('.ag-p'), sig = a.tools + ':' + a.out + ':' + a.state;
  if (!force && p.dataset.sig === sig) return;
  if (p.dataset.busy) return;
  p.dataset.busy = 1;
  try {
    const d = await api('subagent', {sid: S.cur, agent: a.id});
    p.dataset.sig = sig;
    if (!d.items.length) { p.innerHTML = '<div class="note">还没有过程记录</div>'; return; }
    p.querySelector(':scope > .note')?.remove();
    const stick = p.scrollHeight - p.scrollTop - p.clientHeight < 40;
    const keep = new Set();
    for (const x of d.items) {
      const c = Object.assign({}, x, {id: 'ag-' + a.id + '-' + x.id}), js = JSON.stringify(x);
      keep.add(c.id);
      const old = p.querySelector(`[data-id="${CSS.escape(c.id)}"]`);
      if (old && old.dataset.js === js) continue;
      const n = c.k === 'user' ? Object.assign(C.renderItem(c, true), {className: 'it note subprompt'}) : C.renderItem(c, true);
      n.dataset.js = js;
      if (old) old.replaceWith(n); else p.appendChild(n);
      C.addMore(n);
    }
    p.querySelectorAll(':scope > [data-id]').forEach(n => { if (!keep.has(n.dataset.id)) n.remove(); });
    if (stick) p.scrollTop = p.scrollHeight;
  } catch (e) { toast(e.message); }
  finally { delete p.dataset.busy; }
}
function locateAgent(id) {
  const a = R.agents.find(x => x.id === id) || (R.shells || []).find(x => 'sh:' + x.id === id);
  const el = a && a.tool_id && $('#feed').querySelector(`[data-id="${CSS.escape(a.tool_id)}"]`);
  if (!el) { toast(/^sh:/.test(id) ? '这条命令不在当前显示的记录里' : '这个子代理的调用不在当前显示的记录里'); return; }
  el.scrollIntoView({block: 'center', behavior: 'smooth'});
  el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1600);
}
function updateTaskBtn() {
  const n = R.agSid === S.cur ? R.agents.filter(a => a.state === 'run').length + (R.shells || []).filter(a => a.state === 'run').length : 0;
  const t = n ? `任务 ${n}` : '任务', tb = $('#taskBtn');
  tb.dataset.n = n || '';  // 第十四轮 H：图标按钮只更新角标
  if (!tb.dataset.ic && tb.textContent !== t) tb.textContent = t;
  if (!tb.dataset.ic) tb.title = '当前对话的子代理和后台命令';
}
/* 有子代理在跑、或者任务页开着且对话在回复时，定时刷新 */
setInterval(() => {
  if (!S.cur || document.hidden) return;
  const showing = !pane.hidden && R.tab === 'tasks';
  if (R.agents.some(a => a.state === 'run') || (R.shells || []).some(a => a.state === 'run') || (showing && S.runner && S.runner.busy)) loadTasks();
  else if (showing) renderAgents();
}, 3000);

/* ================= 定时任务 ================= */
/* 第十五轮 E：频率（每小时 / 每天 / 工作日 / 每周 / 自定义间隔）、下次运行、运行历史（点开对话）、错过后补跑、运行期间保持唤醒。样式在 feat.css */
const WD = ['一', '二', '三', '四', '五', '六', '日'];
const SC_RES = {running: '运行中', done: '完成', error: '出错', failed: '启动失败', untrusted: '未执行', missed: '已错过', busy: '已跳过', unknown: '结果未知'};
const SC_KIND = {catchup: '补跑', manual: '手动'};
const p2 = n => String(n).padStart(2, '0');
function scFreq(t) {
  const f = t.freq || (t.days === 'weekdays' ? 'weekdays' : 'daily');
  if (f === 'hourly') return `每小时 :${p2(t.minute || 0)}`;
  if (f === 'weekdays') return `工作日 ${t.time}`;
  if (f === 'weekly') return `每周${(t.weekdays || []).map(d => WD[d]).join('、')} ${t.time}`;
  if (f === 'interval') return `每 ${t.every} 小时 · ${t.time} 起`;
  return `每天 ${t.time}`;
}
function scWhen(sec) {
  const d = new Date(sec * 1000), now = new Date();
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(d) - day(now)) / 86400000), hm = p2(d.getHours()) + ':' + p2(d.getMinutes());
  return (diff === 0 ? '今天 ' : diff === 1 ? '明天 ' : diff === -1 ? '昨天 ' : `${d.getMonth() + 1}月${d.getDate()}日 `) + hm;
}
R.scOpen = R.scOpen || {};
async function loadScheds() {
  let err = '';
  try { R.scheds = (await api('schedules', {action: 'list'})).schedules || []; } catch (e) { R.scheds = []; err = e.message; }
  if (R.tab !== 'sched' || pane.hidden) return;
  body.innerHTML = '<div class="racts"><button class="btn small primary" data-a="new">新建定时任务</button></div>' +
    '<div class="rnote">只在工作台开着时运行；需要确认的操作会被自动拒绝。</div>' +
    (err ? `<div class="rempty">暂时拿不到定时任务<br>${esc(err)}</div>` : R.scheds.length ? '' : '<div class="rempty">还没有定时任务</div>') +
    R.scheds.map(t => {
      const last = (t.runs || [])[0], open = !!R.scOpen[t.id];
      const st = t.running ? '<span class="sc-st running"><i></i>运行中</span>' :
        last && last.result !== 'running' ? `<span class="sc-st ${esc(last.result)}" title="${esc(SC_RES[last.result] || '')}${last.note ? '：' + esc(last.note) : ''}"><i></i></span>` : '';
      const sub = [esc(scFreq(t)), t.enabled !== false && t.next_at ? '下次 <b>' + esc(scWhen(t.next_at)) + '</b>' : t.enabled === false ? '已暂停' : '', esc(base(t.cwd))].filter(Boolean).map(x => `<span class="sc-seg">${x}</span>`).join(' · ');
      return `<div class="sc-card${open ? ' open' : ''}" data-id="${esc(t.id)}"><div class="sc"><label class="sw" title="${t.enabled !== false ? '暂停' : '启用'}"><input type="checkbox"${t.enabled !== false ? ' checked' : ''}><span></span></label>` +
        `<div class="tk-main"><div class="tk-title">${esc(t.name || t.prompt.slice(0, 30))}</div><div class="tk-sub">${sub}</div></div>${st}` +
        `<button class="smore" title="更多">⋯</button></div><div class="sc-hist"${open ? '' : ' hidden'}>${scHist(t)}</div></div>`;
    }).join('');
  body.querySelector('[data-a=new]').onclick = () => editSched({});
  body.querySelectorAll('.sc-card').forEach(el => {
    const t = R.scheds.find(x => x.id === el.dataset.id);
    const sw = el.querySelector('.sw input');
    el.querySelector('.sw').onclick = e => e.stopPropagation();
    sw.onchange = () => api('schedules', {action: 'save', task: Object.assign({}, t, {enabled: sw.checked})}).then(loadScheds, e => { sw.checked = !sw.checked; toast(e.message); });
    el.querySelector('.sc').onclick = e => {
      if (e.target.closest('.smore,.sw')) return;
      R.scOpen[t.id] = !R.scOpen[t.id];
      el.classList.toggle('open', R.scOpen[t.id]); el.querySelector('.sc-hist').hidden = !R.scOpen[t.id];
    };
    el.querySelectorAll('.sc-run[data-sid]').forEach(r => r.onclick = () => C.openSession(r.dataset.sid));
    el.querySelector('.smore').onclick = e => { e.stopPropagation(); menu(e.currentTarget, [
      {label: t.running ? '正在运行' : '立即运行', fn: () => { if (t.running) return; api('schedules', {action: 'run', id: t.id}).then(() => { toast('已开始运行'); R.scOpen[t.id] = true; setTimeout(loadScheds, 600); setTimeout(() => C.refreshSide(true), 1500); }, er => toast(er.message)); }},
      {label: '编辑', fn: () => editSched(t)},
      {sep: true}, {label: '删除', danger: true, fn: () => confirm('删除这个定时任务？') && api('schedules', {action: 'delete', id: t.id}).then(loadScheds, er => toast(er.message))},
    ], {alignRight: true}); };
  });
}
function scHist(t) {
  const runs = t.runs || [];
  if (!runs.length) return '<div class="sc-none">还没有运行记录</div>';
  return runs.map(r => `<div class="sc-run${r.sid ? ' link-row' : ''}"${r.sid ? ` data-sid="${esc(r.sid)}" title="打开这次运行的对话"` : ''}>` +
    `<span class="sc-dot ${esc(r.result)}"></span><span class="sc-rt">${esc(scWhen(r.t))}</span><span class="sc-rr">${esc(SC_RES[r.result] || r.result)}</span>` +
    (SC_KIND[r.kind] ? `<span class="sc-rk">${SC_KIND[r.kind]}</span>` : '') + `<span class="sc-rn">${esc(r.note || '')}</span>` +
    (r.sid ? '<span class="sc-go">›</span>' : '') + '</div>').join('');
}
function editSched(t) {
  const card = $('#card');
  const projs = S.projects.map(p => `<option value="${esc(p.cwd)}"${p.cwd === (t.cwd || S.cwd) ? ' selected' : ''}>${esc(p.name)} — ${esc(C.shortPath(p.cwd))}</option>`).join('');
  const models = `<option value="">默认（${esc(S.models.default)}）</option>` + S.models.list.map(m => `<option value="${esc(m.id)}"${m.id === t.model ? ' selected' : ''}>${esc(m.label)}</option>`).join('');
  const modes = Object.entries(MODE_LABEL).map(([v, l]) => `<option value="${v}"${v === (t.mode || 'default') ? ' selected' : ''}>${l}</option>`).join('');
  const freq = t.freq || (t.days === 'weekdays' ? 'weekdays' : 'daily');
  const mins = [...new Set([...Array(12).keys()].map(i => i * 5).concat(t.minute != null ? [t.minute] : []))].sort((a, b) => a - b);
  const days = new Set(t.weekdays && t.weekdays.length ? t.weekdays : [new Date().getDay() === 0 ? 6 : new Date().getDay() - 1]);
  S.modalKey = 'ask';
  card.innerHTML = `<h3>${t.id ? '编辑' : '新建'}定时任务</h3><div class="form sc-form">` +
    `<label>名称<input id="f-name" type="text" value="${esc(t.name || '')}"></label>` +
    `<label>项目<select id="f-cwd">${projs}</select></label>` +
    `<div class="frow"><label>频率<select id="f-freq">${[['hourly', '每小时'], ['daily', '每天'], ['weekdays', '工作日'], ['weekly', '每周'], ['interval', '自定义间隔']].map(([v, l]) => `<option value="${v}"${v === freq ? ' selected' : ''}>${l}</option>`).join('')}</select></label>` +
    `<label data-f="time">时间<input id="f-time" type="time" value="${esc(t.time || '09:00')}"></label>` +
    `<label data-f="minute">分钟<select id="f-min">${mins.map(m => `<option value="${m}"${m === (t.minute || 0) ? ' selected' : ''}>:${p2(m)}</option>`).join('')}</select></label></div>` +
    `<div class="sc-lbl" data-f="days"><span>星期</span><div class="sc-days">${WD.map((w, i) => `<button type="button" data-d="${i}" class="${days.has(i) ? 'on' : ''}" aria-pressed="${days.has(i)}">${w}</button>`).join('')}</div></div>` +
    `<label data-f="every">间隔<span class="sc-every">每<input id="f-every" type="number" min="1" max="168" step="1" value="${esc(String(t.every || 4))}">小时</span></label>` +
    `<div class="frow"><label>模型<select id="f-model">${models}</select></label><label>权限<select id="f-mode">${modes}</select></label></div>` +
    `<label>提示词<textarea id="f-prompt" rows="5">${esc(t.prompt || '')}</textarea></label>` +
    `<label class="sc-tg">错过后补跑<span class="sw"><input id="f-catchup" type="checkbox"${t.catchup !== false ? ' checked' : ''}><span></span></span></label>` +
    `<label class="sc-tg">运行期间保持电脑唤醒<span class="sw"><input id="f-awake" type="checkbox"${t.awake ? ' checked' : ''}><span></span></span></label>` +
    '<div class="fx-err" hidden></div></div>' +
    '<div class="acts"><button class="btn" data-a="no">取消</button><button class="btn primary" data-a="ok">保存</button></div>';
  const q = id => card.querySelector(id), v = id => q(id).value;
  const showF = () => {
    const f = v('#f-freq');
    card.querySelector('[data-f=time]').hidden = f === 'hourly';
    card.querySelector('[data-f=minute]').hidden = f !== 'hourly';
    card.querySelector('[data-f=days]').hidden = f !== 'weekly';
    card.querySelector('[data-f=every]').hidden = f !== 'interval';
    card.querySelector('[data-f=time]').firstChild.textContent = f === 'interval' ? '开始时间' : '时间';
  };
  q('#f-freq').onchange = showF; showF();
  card.querySelectorAll('.sc-days button').forEach(b => b.onclick = e => {
    e.preventDefault();
    const on = !b.classList.contains('on'); b.classList.toggle('on', on); b.setAttribute('aria-pressed', on);
  });
  const err = q('.fx-err');
  const close = () => { $('#modal').hidden = true; card.innerHTML = ''; S.modalKey = null; if (C.showModal) C.showModal(); };
  card.querySelector('[data-a=no]').onclick = close;
  card.querySelector('[data-a=ok]').onclick = async e => {
    const btn = e.currentTarget, f = v('#f-freq');
    const wd = [...card.querySelectorAll('.sc-days button.on')].map(b => +b.dataset.d);
    err.hidden = true;
    if (f === 'weekly' && !wd.length) { err.textContent = '请选择星期几'; err.hidden = false; return; }
    const every = Math.round(+v('#f-every'));
    if (f === 'interval' && !(every >= 1 && every <= 168)) { err.textContent = '间隔要在 1 到 168 小时之间'; err.hidden = false; return; }
    btn.disabled = true;
    try {
      const task = Object.assign({}, t, {name: v('#f-name').trim(), cwd: v('#f-cwd'), freq: f, time: v('#f-time'), days: f === 'weekdays' ? 'weekdays' : 'daily',
        minute: +v('#f-min'), model: v('#f-model'), mode: v('#f-mode'), prompt: v('#f-prompt').trim(),
        catchup: q('#f-catchup').checked, awake: q('#f-awake').checked});
      if (f === 'weekly') task.weekdays = wd;
      if (f === 'interval') task.every = every;
      await api('schedules', {action: 'save', task});
      close(); openPane('sched');
    } catch (ex) { err.textContent = ex.message; err.hidden = false; btn.disabled = false; }
  };
  $('#modal').hidden = false;
  q('#f-name').focus();
}

/* ================= 扩展（技能与 MCP，只读） ================= */
async function loadExt() {
  body.innerHTML = '<div class="rempty">读取中…</div>';
  try { R.ext = await api('extensions', {cwd: S.cwd}); } catch (e) { body.innerHTML = `<div class="rempty">${esc(e.message)}</div>`; return; }
  const {skills, mcp} = R.ext;
  body.innerHTML = `<div class="rsec">技能与命令（${skills.length}）</div>` + skills.map(s => `<div class="ex" data-cmd="${esc(s.name)}"><div class="tk-title">/${esc(s.name)}<span class="wtag">${esc(s.src)}</span></div><div class="tk-sub">${esc(s.desc || '')}</div></div>`).join('') +
    `<div class="rsec">MCP 服务（${mcp.length}）</div>` + (mcp.length ? mcp.map(m => `<div class="ex"><div class="tk-title">${esc(m.name)}<span class="wtag">${esc(m.scope)}</span><span class="wtag">${esc(m.type)}</span></div><div class="tk-sub">${esc(m.target)}</div></div>`).join('') : '<div class="rnote">没有配置</div>');
  body.querySelectorAll('[data-cmd]').forEach(el => el.onclick = () => { $('#input').value = '/' + el.dataset.cmd + ' '; $('#input').dispatchEvent(new Event('input')); $('#input').focus(); });
}

/* ================= 分叉、工作树、项目默认设置 ================= */
async function fork(sid) {
  try { const d = await api('fork', {sid}); C.load(d); C.refreshSide(true); toast('已分叉，可以在新对话里尝试别的方案'); } catch (e) { toast(e.message); }
}
async function newWorktree(cwd) {
  try { const d = await api('new', {cwd, worktree: true}); C.toggleProject(cwd, true); C.load(d); C.refreshSide(true); } catch (e) { toast(e.message); }
}
async function projectDefaults(p) {
  const cur = (await api('project-opts', {cwd: p.cwd})).opts || {};
  const card = $('#card');
  S.modalKey = 'ask';
  const models = `<option value="">默认（${esc(S.models.default)}）</option>` + S.models.list.map(m => `<option value="${esc(m.id)}"${m.id === cur.model ? ' selected' : ''}>${esc(m.label)}</option>`).join('');
  const efforts = [['', '默认'], ['low', '低'], ['medium', '中'], ['high', '高'], ['xhigh', '超高'], ['max', '最高']].map(([v, l]) => `<option value="${v}"${v === (cur.effort || '') ? ' selected' : ''}>${l}</option>`).join('');
  const modes = Object.entries(MODE_LABEL).map(([v, l]) => `<option value="${v}"${v === (cur.mode || 'default') ? ' selected' : ''}>${l}</option>`).join('');
  card.innerHTML = `<h3>${esc(p.name)} · 新对话默认设置</h3><div class="form"><label>模型<select id="d-model">${models}</select></label>` +
    `<label>思考强度<select id="d-effort">${efforts}</select></label><label>权限模式<select id="d-mode">${modes}</select></label></div>` +
    '<div class="acts"><button class="btn" data-a="no">取消</button><button class="btn primary" data-a="ok">保存</button></div>';
  const close = () => { $('#modal').hidden = true; card.innerHTML = ''; S.modalKey = null; };
  card.querySelector('[data-a=no]').onclick = close;
  card.querySelector('[data-a=ok]').onclick = async () => {
    await api('project-opts', {cwd: p.cwd, opts: {model: card.querySelector('#d-model').value, effort: card.querySelector('#d-effort').value, mode: card.querySelector('#d-mode').value}});
    close(); toast('已保存，之后新建的对话会用这些设置');
  };
  $('#modal').hidden = false;
}
C.sessionMenuExtra = s => [{label: '分叉', fn: () => fork(s.sid)}];
C.projectMenuExtra = p => [{label: '新对话（独立工作树）', fn: () => newWorktree(p.cwd)}, {label: '新对话默认设置…', fn: () => projectDefaults(p)}];

/* ================= 顶部“⋯”菜单与上下文 ================= */
function lastCtx() { const us = C.userItems(); for (let i = us.length - 1; i >= 0; i--) if (us[i].stat && us[i].stat.ctx) return us[i].stat.ctx; return 0; }
function compact() {
  if (!S.cur) return;
  if (/Compacting/i.test((S.runner && S.runner.spinner) || '')) { toast('正在压缩上下文，请稍等'); return; }
  C.sendText('/compact');
  toast(S.runner && S.runner.busy ? '这一轮回复结束后开始压缩上下文' : '开始压缩上下文，内容多时要几分钟');
}
function updateCtx() {
  const n = lastCtx(), el = $('#ctx');
  el.hidden = !S.cur || !n;
  el.textContent = '上下文 ' + C.fmtTok(n);
  el.classList.toggle('warn', n > 800000);
}
$('#ctx').onclick = e => menu(e.currentTarget, [{label: '压缩上下文', sub: '把前面的对话浓缩成要点，腾出空间', fn: compact}]);
$('#moreBtn').onclick = e => {
  const s = C.findSession(S.cur), p = S.projects.find(x => x.cwd === S.cwd);
  menu(e.currentTarget, [
    {label: '分叉此对话', fn: () => S.cur && fork(S.cur)},
    {label: '压缩上下文', fn: compact},
    {sep: true},
    {label: '定时任务', fn: () => openPane('sched')},
    {label: '扩展', fn: () => openPane('ext')},
    ...(p ? [{label: '本项目新对话默认设置…', fn: () => projectDefaults(p)}] : []),
    {sep: true},
    {label: '命令面板', sub: '⌘⇧P', fn: palette},
  ].filter(x => s || x.sep || !/分叉/.test(x.label || '')), {alignRight: true});
};

/* ================= 引用回复 ================= */
const qb = $('#quoteBtn');
document.addEventListener('mouseup', () => setTimeout(() => {
  const sel = getSelection(), txt = sel && sel.toString().trim();
  if (!txt || !sel.rangeCount || !$('#feed').contains(sel.anchorNode)) { qb.hidden = true; return; }
  const r = sel.getRangeAt(0).getBoundingClientRect();
  qb.style.left = Math.min(innerWidth - 80, r.right - 30) + 'px'; qb.style.top = Math.max(8, r.top - 34) + 'px';
  qb.hidden = false; qb.dataset.t = txt;
}, 10));
qb.onmousedown = e => e.preventDefault();
qb.onclick = () => {
  const q = qb.dataset.t.split('\n').map(l => '> ' + l).join('\n') + '\n\n';
  const i = $('#input'); i.value = (i.value ? i.value.replace(/\s*$/, '\n\n') : '') + q; i.dispatchEvent(new Event('input')); i.focus();
  i.selectionStart = i.selectionEnd = i.value.length; qb.hidden = true; getSelection().removeAllRanges();
};

/* ================= 命令面板 ================= */
function paletteItems(q) {
  const p = S.projects.find(x => x.cwd === S.cwd);
  const acts = [
    ...(S.cwd ? [{label: '新对话', sub: base(S.cwd), fn: () => C.newSession(S.cwd)}, {label: '新对话（独立工作树）', sub: base(S.cwd), fn: () => newWorktree(S.cwd)}] : []),
    ...(S.cur ? [{label: '分叉此对话', fn: () => fork(S.cur)}, {label: '压缩上下文', fn: compact}, {label: '导出为 Markdown', fn: () => $('#expBtn').click()}] : []),
    {label: '打开审阅面板', fn: () => openPane('review')}, {label: '查看子代理任务', fn: () => openPane('tasks')},
    {label: '定时任务', fn: () => openPane('sched')}, {label: '扩展', fn: () => openPane('ext')},
    ...(p ? [{label: '本项目新对话默认设置…', fn: () => projectDefaults(p)}] : []),
    ...S.models.list.map(m => ({label: '切换模型：' + m.label, fn: () => C.control({model: m.id})})),
    ...[['low', '低'], ['medium', '中'], ['high', '高'], ['xhigh', '超高'], ['max', '最高']].map(([v, l]) => ({label: '思考强度：' + l, fn: () => C.control({effort: v})})),
    ...Object.entries(MODE_LABEL).filter(([v]) => v !== 'default').map(([v, l]) => ({label: '权限模式：' + l, fn: () => C.control({mode: v})})),
  ];
  const sess = [];
  for (const pr of S.projects) for (const s of pr.sessions) if (!s.archived) sess.push({label: s.title, sub: pr.name, kind: '对话', fn: () => C.openSession(s.sid)});
  const ql = q.trim().toLowerCase();
  const m = x => !ql || (x.label + ' ' + (x.sub || '')).toLowerCase().includes(ql);
  return [...acts.filter(m).map(x => Object.assign({kind: '操作'}, x)), ...sess.filter(m).slice(0, ql ? 40 : 12)];
}
function palette() {
  const pl = $('#palette'), inp = pl.querySelector('input'), list = pl.querySelector('.pl-list');
  let items = [], idx = 0;
  const draw = () => {
    items = paletteItems(inp.value); idx = Math.min(idx, Math.max(0, items.length - 1));
    list.innerHTML = items.map((x, i) => `<div class="pi${i === idx ? ' on' : ''}" data-i="${i}"><span class="pt">${esc(x.kind)}</span><span class="pl">${esc(x.label)}</span>${x.sub ? `<span class="ps">${esc(x.sub)}</span>` : ''}</div>`).join('') || '<div class="rnote">没有匹配</div>';
    list.querySelectorAll('.pi').forEach(el => el.onmousedown = e => { e.preventDefault(); idx = +el.dataset.i; run(); });
  };
  const close = () => { pl.hidden = true; };
  const run = () => { const x = items[idx]; close(); if (x) x.fn(); };
  inp.value = ''; idx = 0; draw(); pl.hidden = false; inp.focus();
  inp.oninput = () => { idx = 0; draw(); };
  inp.onkeydown = e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); idx = (idx + 1) % Math.max(1, items.length); draw(); list.querySelector('.on') && list.querySelector('.on').scrollIntoView({block: 'nearest'}); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); idx = (idx - 1 + items.length) % Math.max(1, items.length); draw(); list.querySelector('.on') && list.querySelector('.on').scrollIntoView({block: 'nearest'}); }
    else if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); run(); }
    else if (e.key === 'Escape') close();
  };
  pl.onmousedown = e => { if (e.target === pl) close(); };
}
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === 'p') { e.preventDefault(); palette(); }
});

/* ================= 事件接入 ================= */
let lastCwd = null, evTimer = null;
C.onLoad = d => {
  updateCtx();
  if (R.agSid !== S.cur) { R.agents = []; R.shells = []; R.agOpen = {}; updateTaskBtn(); loadTasks(); }
  if (d.cwd !== lastCwd) { lastCwd = d.cwd; R.review = null; R.diffs = {}; R.checked = {}; R.open = {}; R.comments = []; if (!document.hidden) api('prewarm', {cwd: d.cwd}).catch(() => {}); }
  if (!pane.hidden && R.tab !== 'preview') render();
};
C.onEvent = ev => {
  if (ev.type === 'items' && ev.sid === S.cur) {
    setTimeout(updateCtx, 0);
    if ([...(ev.new || []), ...(ev.patch || [])].some(x => x.k === 'tool' && /^(Agent|Task)$|invoke_subagent/.test(x.name || '') || x.agent)) setTimeout(loadTasks, 600);  // 11-23：Antigravity 的是 Run invoke_subagent
  }
  if (ev.type === 'runner' || ev.type === 'sessions') {
    clearTimeout(evTimer);
    evTimer = setTimeout(loadTasks, 700);
  }
  if (ev.type === 'sessions' || (ev.type === 'items' && ev.sid === S.cur && (ev.patch || []).some(x => x.done))) {
    clearTimeout(rvTimer);
    rvTimer = setTimeout(() => { if (!pane.hidden && R.tab === 'review') loadReview(true); }, 900);
  }
};
loadTasks();
})();
