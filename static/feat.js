(() => {
'use strict';
/* 第十一轮 B：全文搜索、网页预览、MCP 管理、技能管理与技能市场、对话列表分组与多选。
   右栏框架在 app2.js（C.pane），左栏渲染在 app.js（groupSessions / isSel / sideClick / onSide 几个钩子）。 */
const C = window.CWB;
if (!C || !C.pane) return;
const {S, api, $, esc, md, toast, menu, askText} = C;
const P = C.pane, body = P.body;
const ls = {get(k, d) { try { const v = localStorage.getItem('cwb3.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('cwb3.' + k, JSON.stringify(v)); } catch {} }};
const fmtSize = n => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const fmtNum = n => n >= 10000 ? (n / 10000).toFixed(n >= 100000 ? 0 : 1) + ' 万' : String(n);
const F = {
  q: '', glob: '', case: false, word: false, regex: false, res: null, resCwd: null, fold: {}, seq: 0, timer: 0, focus: false,
  pvMode: 'file', line: 0, ports: [], url: '', webCwd: null, portsBusy: false,
  extTab: ls.get('extTab', 'skills'), mcpState: {}, mk: {src: ls.get('mkSrc', 'anthropic'), q: '', items: null, key: '', err: '', seq: 0, offset: 0, total: 0},
  sel: new Set(), anchor: null,
};

/* ---------------- 通用：弹窗 ---------------- */
function openCard(html) { S.modalKey = 'ask'; const c = $('#card'); c.innerHTML = html; $('#modal').hidden = false; return c; }
function closeCard() { $('#modal').hidden = true; $('#card').innerHTML = ''; S.modalKey = null; if (C.showModal) C.showModal(); }
function onAct(root, map) { root.querySelectorAll('[data-a]').forEach(b => { const f = map[b.dataset.a]; if (f) b.onclick = e => { e.stopPropagation(); f(e, b); }; }); }
const stripFront = t => (t || '').replace(/^﻿?---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(\r?\n|$)/, '');
const tag = t => t ? `<span class="wtag">${esc(t)}</span>` : '';
function filesHtml(files, n) {
  if (!files || !files.length) return '';
  return `<details class="fx-files"><summary>文件（${n || files.length}）</summary><div class="fx-flist">` +
    files.map(f => `<div><span>${esc(f.p)}</span><span class="fx-dim">${fmtSize(f.s || 0)}</span></div>`).join('') + '</div></details>';
}

/* ================= 右栏：新增“搜索”标签，接管“预览”“扩展” ================= */
C.paneTabs = Object.assign(C.paneTabs || {}, {search: renderSearch, preview: renderPreviewX, ext: renderExt});
{
  const pv = $('#rpane .rtabs [data-tab="preview"]');
  const b = document.createElement('button');
  b.dataset.tab = 'search'; b.textContent = '搜索';
  b.onclick = () => { F.focus = true; P.open('search'); };
  pv.after(b);
}
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === 'f') { e.preventDefault(); F.focus = true; P.open('search'); }
});

/* ================= 全文搜索 ================= */
const tog = (o, label, title) => `<button class="fx-tog${F[o] ? ' on' : ''}" data-o="${o}" title="${title}">${label}</button>`;
function renderSearch() {
  if (!S.cwd) { body.innerHTML = '<div class="rempty">先打开一个对话</div>'; return; }
  const moved = F.resCwd !== S.cwd;
  if (moved) { F.res = null; F.resCwd = S.cwd; F.fold = {}; }
  body.innerHTML = '<div class="sr-form"><div class="sr-row"><input id="srQ" class="fx-in" type="text" placeholder="搜索" spellcheck="false" autocomplete="off">' +
    tog('case', 'Aa', '区分大小写') + tog('word', 'ab', '整词') + tog('regex', '.*', '正则') + '</div>' +
    '<input id="srG" class="fx-in" type="text" placeholder="文件过滤 *.py" spellcheck="false" autocomplete="off"></div><div id="srOut"></div>';
  const q = $('#srQ'), g = $('#srG');
  q.value = F.q; g.value = F.glob;
  q.oninput = () => { F.q = q.value; schedule(); };
  g.oninput = () => { F.glob = g.value; schedule(); };
  const key = e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); F.q = q.value; F.glob = g.value; clearTimeout(F.timer); doSearch(); } };
  q.onkeydown = key; g.onkeydown = key;
  body.querySelectorAll('.fx-tog').forEach(b => b.onclick = () => { F[b.dataset.o] = !F[b.dataset.o]; b.classList.toggle('on', F[b.dataset.o]); doSearch(); });
  drawResults();
  if (F.focus) { F.focus = false; q.focus(); q.select(); }
  if (moved && F.q.trim()) doSearch();
}
function schedule() { clearTimeout(F.timer); F.timer = setTimeout(doSearch, 350); }
async function doSearch() {
  const q = F.q, cwd = S.cwd, seq = ++F.seq;
  if (!q.trim() || !cwd) { F.res = null; drawResults(); return; }
  const out = $('#srOut');
  if (out && !F.res) out.innerHTML = '<div class="rempty">搜索中…</div>';
  let r;
  try { r = await api('grep', {cwd, q, case: F.case, word: F.word, regex: F.regex, glob: F.glob}); } catch (e) { r = {error: e.message}; }
  if (seq !== F.seq || cwd !== S.cwd) return;
  F.res = r; F.resCwd = cwd;
  if (P.R.tab === 'search') drawResults();
}
function marks(t, rr) {
  let o = '', k = 0;
  for (const [a, b] of rr || []) { if (a < k || b <= a) continue; o += esc(t.slice(k, a)) + '<mark>' + esc(t.slice(a, b)) + '</mark>'; k = b; }
  return o + esc(t.slice(k));
}
function drawResults() {
  const out = $('#srOut'), r = F.res;
  if (!out) return;
  if (!r) { out.innerHTML = ''; return; }
  if (r.error) { out.innerHTML = `<div class="rnote fx-err">${esc(r.error)}</div>`; return; }
  if (!r.total) { out.innerHTML = '<div class="rempty">没有找到</div>'; return; }
  out.innerHTML = `<div class="rnote sr-sum">${r.total} 个结果 · ${r.files.length} 个文件${r.full ? ' · 只显示前 500 个' : ''}${r.timeout ? ' · 超时，结果不完整' : ''}</div>` +
    r.files.map((f, i) => {
      const k = f.rel.lastIndexOf('/');
      return `<div class="sr-file${F.fold[f.rel] ? ' fold' : ''}" data-i="${i}"><div class="sr-fh" title="${esc(f.rel)}"><span class="caret"></span><b>${esc(f.rel.slice(k + 1))}</b>` +
        `<span class="sr-dir">${esc(k > 0 ? f.rel.slice(0, k) : '')}</span><span class="sr-n">${f.hits.length}</span></div>` +
        f.hits.map((h, j) => `<div class="sr-ln" data-j="${j}"><span class="sr-no">${h.n}</span><span class="sr-t">${marks(h.t, h.r)}</span></div>`).join('') + '</div>';
    }).join('');
  out.querySelectorAll('.sr-file').forEach(el => {
    const f = r.files[+el.dataset.i];
    el.querySelector('.sr-fh').onclick = () => { F.fold[f.rel] = !F.fold[f.rel]; el.classList.toggle('fold', F.fold[f.rel]); };
    el.querySelectorAll('.sr-ln').forEach(ln => ln.onclick = () => C.preview(f.path, {line: f.hits[+ln.dataset.j].n}));
  });
}

/* ================= 预览：文件 / 网页 ================= */
const basePreview = C.preview;
C.preview = async (path, opt) => {
  let line = (opt && opt.line) || 0;
  const m = /(?::(\d+)(?:-\d+)?(?::\d+)?|#L(\d+)(?:-L?\d+)?)$/.exec(path || '');
  if (!line && m) line = +(m[1] || m[2]) || 0;
  F.pvMode = 'file'; F.line = line;
  return basePreview(path);
};
const modeSeg = () => `<div class="fx-seg pv-seg"><button data-m="file" class="${F.pvMode === 'file' ? 'on' : ''}">文件</button><button data-m="web" class="${F.pvMode === 'web' ? 'on' : ''}">网页</button></div>`;
function bindSeg() {
  body.querySelectorAll('.pv-seg button').forEach(b => b.onclick = () => {
    if (F.pvMode === b.dataset.m) return;
    F.pvMode = b.dataset.m; renderPreviewX();
  });
}
function renderPreviewX() {
  if (F.pvMode === 'web') { renderWeb(); return; }
  const d = P.R.preview, line = F.line;
  const swap = line && d && (d.kind === 'md' || d.kind === 'csv') ? d.kind : '';  // 跳到某一行时按源码显示
  if (swap) d.kind = 'text';
  try { P.renderPreview(); } finally { if (swap) d.kind = swap; }
  body.insertAdjacentHTML('afterbegin', modeSeg());
  bindSeg();
  if (line && d) {
    const tr = body.querySelectorAll('.pv-code tr')[line - 1];
    if (tr) { tr.classList.add('pv-hl'); tr.scrollIntoView({block: 'center'}); }
  }
}
function portOf(u) { try { const x = new URL(u); return +(x.port || (x.protocol === 'https:' ? 443 : 80)); } catch { return 0; } }
function isLocal(u) { try { return ['localhost', '127.0.0.1', '[::1]', '0.0.0.0'].includes(new URL(u).hostname); } catch { return false; } }
function normUrl(v) {
  v = (v || '').trim();
  if (!v) return '';
  if (/^:?\d{2,5}(\/.*)?$/.test(v)) v = 'http://localhost' + (v[0] === ':' ? '' : ':') + v;
  else if (!/^[a-z][\w+.-]*:\/\//i.test(v)) v = 'http://' + v;
  try { const u = new URL(v); return /^https?:$/.test(u.protocol) ? u.href : ''; } catch { return ''; }
}
const curPort = () => isLocal(F.url) ? F.ports.find(p => p.port === portOf(F.url)) : null;
function renderWeb() {
  if (F.webCwd !== S.cwd) { F.webCwd = S.cwd; F.ports = []; F.url = ''; loadPorts(); }
  body.innerHTML = modeSeg() + '<div class="rsec wp-h"><span>端口</span><button class="icon" data-a="ports" title="刷新列表">↻</button></div><div class="wp-ports"></div>' +
    '<div class="wp-bar"><input id="wpUrl" class="fx-in" type="text" spellcheck="false" autocomplete="off" placeholder="http://localhost:3000">' +
    '<button class="btn small" data-a="reload">刷新</button><button class="btn small" data-a="ext">用浏览器打开</button><button class="btn small fx-danger" data-a="stop">停止</button></div>';
  bindSeg();
  const u = $('#wpUrl');
  u.value = F.url;
  u.onkeydown = e => {
    if (e.key !== 'Enter' || e.isComposing) return;
    const v = normUrl(u.value);
    if (!v) { toast('地址无效'); return; }
    setUrl(v);
  };
  onAct(body, {
    ports: () => loadPorts(),
    reload: () => { const v = normUrl(u.value); if (v) setUrl(v, true); },
    ext: () => { const v = normUrl(u.value) || F.url; if (v) api('open-url', {url: v}); },
    stop: stopPort,
  });
  drawPorts(); mountFrame();
  if (!F.ports.length && !F.portsBusy) loadPorts();
}
function drawPorts() {
  const box = body.querySelector('.wp-ports');
  if (!box) return;
  const on = portOf(F.url);
  box.innerHTML = F.ports.length ? F.ports.map((p, i) => `<div class="wp-port${isLocal(F.url) && on === p.port ? ' on' : ''}" data-i="${i}" title="${esc(p.cmd || p.name)}">` +
    `<b>${p.port}</b><span class="wp-name">${esc(p.name)}</span><span class="fx-dim">PID ${p.pid}</span></div>`).join('')
    : `<div class="rnote">${F.portsBusy ? '读取中…' : '没有监听中的端口'}</div>`;
  box.querySelectorAll('.wp-port').forEach(el => el.onclick = () => setUrl(F.ports[+el.dataset.i].url));
  const st = body.querySelector('[data-a=stop]');
  if (st) st.disabled = !curPort();
}
function mountFrame() {
  const old = body.querySelector('.wp-frame');
  if (old) old.remove();
  if (!F.url) return;
  const fr = document.createElement('iframe');
  fr.className = 'wp-frame';
  fr.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads');
  fr.src = F.url;
  body.appendChild(fr);
}
function setUrl(u, force) {
  const same = u === F.url;
  F.url = u;
  if (P.R.tab !== 'preview' || F.pvMode !== 'web') return;
  const inp = $('#wpUrl'); if (inp) inp.value = u;
  if (!same || force) mountFrame();
  drawPorts();
}
C.openWeb = u => { F.pvMode = 'web'; if (F.webCwd !== S.cwd) { F.webCwd = S.cwd; F.ports = []; } F.url = u; P.open('preview'); };  // 第十三轮 G：cwb.py web 打开网页预览（ux-g.js）
async function loadPorts() {
  const cwd = S.cwd;
  if (!cwd) return;
  F.portsBusy = true; drawPorts();
  try { const d = await api('ports', {cwd}); if (cwd !== S.cwd) return; F.ports = d.ports || []; } catch (e) { toast(e.message); }
  F.portsBusy = false;
  drawPorts();  // 不自动打开：列表里可能有不想看的服务，由用户点选
}
async function stopPort() {
  const p = curPort();
  if (!p) return;
  if (!confirm(`停止进程 ${p.name}（PID ${p.pid}，端口 ${p.port}）？`)) return;
  try { await api('port-kill', {cwd: S.cwd, pid: p.pid}); toast('已停止'); } catch (e) { toast(e.message); }
  setTimeout(loadPorts, 900);
}

/* ================= 扩展：技能 / MCP / 市场 ================= */
const SCOPE = {user: '用户级', project: '项目级', plugin: '插件'};
const EXT_TABS = [['skills', '技能'], ['mcp', 'MCP'], ['market', '市场'], ['plugins', '插件'], ['rules', '钩子与权限']];  // 第十五轮 E：插件、钩子与权限
/* 第十四轮 J：扩展页也能渲染进设置面板（CWB.renderExtInto(容器)）；不传容器就是右栏。之后的重画默认画到正在显示的那一处 */
let exRoot = null;
const exHost = () => (exRoot && exRoot.isConnected ? exRoot : body);
const extShown = () => !!(exRoot && exRoot.isConnected) || P.R.tab === 'ext';
function renderExt(h) {
  const t = F.extTab, root = h instanceof Element ? h : body;
  if (root !== body) exRoot = root;
  root.innerHTML = '<div class="fx-seg ext-seg">' + EXT_TABS.map(([k, l]) => `<button data-t="${k}" class="${k === t ? 'on' : ''}">${l}</button>`).join('') +
    '</div><div id="exBody"></div>';
  root.querySelectorAll('.ext-seg button').forEach(b => b.onclick = () => { F.extTab = b.dataset.t; ls.set('extTab', F.extTab); renderExt(h); });
  (({skills: drawSkills, mcp: drawMcp, market: drawMarket, plugins: drawPlugins, rules: drawRules})[t] || drawSkills)(root);
}
C.renderExtInto = el => renderExt(el);
const exBox = r => (r || exHost()).querySelector('#exBody');

/* ---- 本地技能 ---- */
async function drawSkills(r) {
  const box = exBox(r);
  box.innerHTML = '<div class="rempty">读取中…</div>';
  let d;
  try { d = await api('skills', {cwd: S.cwd}); } catch (e) { box.innerHTML = `<div class="rempty">${esc(e.message)}</div>`; return; }
  if (!box.isConnected || F.extTab !== 'skills') return;
  const all = [...d.user, ...d.project, ...d.plugin];
  const grp = (label, list) => list.length ? `<div class="rsec">${label}（${list.length}）</div>` + list.map(s => {
    const i = all.indexOf(s);
    return `<div class="ex fx-item" data-i="${i}"><div class="fx-main"><div class="tk-title">${esc(s.name)}${s.plugin ? tag(s.plugin) : ''}</div><div class="tk-sub">${esc(s.desc)}</div></div>` +
      `<div class="fx-acts"><button class="link" data-a="finder">访达</button>${s.readonly ? '' : '<button class="link fx-danger" data-a="del">删除</button>'}</div></div>`;
  }).join('') : '';
  box.innerHTML = '<div class="fx-bar"><span class="grow"></span><button class="btn small" data-a="install">安装…</button></div>' +
    (grp('用户级', d.user) + grp('项目级', d.project) + grp('插件', d.plugin) || '<div class="rempty">没有技能</div>');
  box.querySelector('[data-a=install]').onclick = e => menu(e.currentTarget, [
    {label: '本地文件夹或 zip…', fn: installLocal},
    {label: 'GitHub 链接…', fn: installGh},
    {label: '技能市场', fn: () => { F.extTab = 'market'; ls.set('extTab', 'market'); renderExt(r && r !== body ? r : undefined); }},
  ], {alignRight: true});
  box.querySelectorAll('.fx-item').forEach(el => {
    const s = all[+el.dataset.i];
    el.onclick = () => viewSkill(s);
    onAct(el, {finder: () => api('reveal', {path: s.dir}), del: () => delSkill(s)});
  });
}
async function viewSkill(s) {
  const c = openCard('<div class="rempty">读取中…</div>');
  let d;
  try { d = await api('skill-read', {dir: s.dir, cwd: S.cwd}); } catch (e) { closeCard(); toast(e.message); return; }
  c.innerHTML = `<h3>${esc(d.name)}</h3><div class="fx-meta">${tag(SCOPE[s.scope])}${tag(s.plugin)}${tag(d.license)}</div>` +
    (d.desc ? `<div class="fx-desc">${esc(d.desc)}</div>` : '') + filesHtml(d.files) + `<div class="md fx-md">${md(stripFront(d.text))}</div>` +
    `<div class="acts"><button class="btn" data-a="finder">在访达中显示</button>${s.readonly ? '' : '<button class="btn fx-danger" data-a="del">删除</button>'}` +
    '<span class="grow"></span><button class="btn" data-a="use">使用</button><button class="btn primary" data-a="close">关闭</button></div>';
  onAct(c, {
    finder: () => api('reveal', {path: s.dir}),
    del: () => delSkill(s, true),
    use: () => { closeCard(); C.insertText('/' + (s.plugin ? s.plugin + ':' : '') + s.name + ' '); },
    close: closeCard,
  });
}
async function delSkill(s, fromCard) {
  if (!confirm(`删除技能「${s.name}」？\n会移到废纸篓。`)) return;
  try { await api('skill-delete', {dir: s.dir, cwd: S.cwd}); toast('已移到废纸篓'); } catch (e) { toast(e.message); return; }
  if (fromCard) closeCard();
  if (extShown() && F.extTab === 'skills') drawSkills();
}
function installLocal() {
  const c = openCard('<h3>从本地安装技能</h3><div class="form"><label>文件夹或 zip<input id="skPath" type="text" spellcheck="false" autocomplete="off"></label></div>' +
    '<div class="fx-err" hidden></div><div class="acts"><button class="btn" data-a="folder">选择文件夹…</button><button class="btn" data-a="zip">选择 zip…</button>' +
    '<span class="grow"></span><button class="btn" data-a="no">取消</button><button class="btn primary" data-a="ok">下一步</button></div>');
  const inp = c.querySelector('#skPath');
  const pick = async kind => { const d = await api('skill-pick', {kind}).catch(() => ({})); if (d.path) inp.value = d.path; inp.focus(); };
  onAct(c, {folder: () => pick('folder'), zip: () => pick('zip'), no: closeCard, ok: () => scanSource(inp.value, c)});
  inp.onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) scanSource(inp.value, c); if (e.key === 'Escape') closeCard(); };
  inp.focus();
}
async function installGh() {
  const v = await askText('GitHub 链接', '');
  if (v && v.trim()) scanSource(v);
}
async function scanSource(src, form) {
  src = (src || '').trim();
  if (!src) return;
  const err = form && form.querySelector('.fx-err');
  if (err) { err.hidden = false; err.textContent = '读取中…'; err.classList.add('dim'); }
  else openCard('<div class="rempty">读取中…</div>');
  let d;
  try { d = await api('skill-scan', {src, cwd: S.cwd}); }
  catch (e) { if (err) { err.textContent = e.message; err.classList.remove('dim'); } else { closeCard(); toast(e.message); } return; }
  if (d.items.length === 1) { previewInstall(d.items[0]); return; }
  const c = openCard(`<h3>选择技能（${d.items.length}）</h3><div class="fx-pick">` + d.items.map((it, i) =>
    `<div class="ex fx-item" data-i="${i}"><div class="fx-main"><div class="tk-title">${esc(it.name)}</div><div class="tk-sub">${esc(it.desc)}</div></div></div>`).join('') +
    '</div><div class="acts"><button class="btn" data-a="no">取消</button></div>');
  onAct(c, {no: closeCard});
  c.querySelectorAll('.fx-item').forEach(el => el.onclick = () => previewInstall(d.items[+el.dataset.i]));
}
async function previewInstall(it) {
  const c = openCard('<div class="rempty">读取中…</div>');
  let d;
  try { d = await api('skill-preview', {cand: it.cand, cwd: S.cwd}); } catch (e) { closeCard(); toast(e.message); return; }
  c.innerHTML = `<h3>${esc(d.name)}</h3><div class="fx-meta">${tag(d.license || '许可证未声明')}<span class="fx-dim">${esc(it.src || d.src || '')}</span></div>` +
    (d.desc ? `<div class="fx-desc">${esc(d.desc)}</div>` : '') + filesHtml(d.files, d.nfiles) + `<div class="md fx-md">${md(stripFront(d.text))}</div>` +
    `<div class="acts"><button class="btn" data-a="no">取消</button>${S.cwd ? '<button class="btn" data-a="project">装到当前项目</button>' : ''}<button class="btn primary" data-a="user">装到用户级</button></div>`;
  onAct(c, {no: closeCard, project: () => doInstall(it, 'project'), user: () => doInstall(it, 'user')});
}
async function doInstall(it, target, mode, newName) {
  const where = target === 'user' ? '用户级' : '当前项目';
  const c = openCard('<div class="rempty">正在安装…</div>');
  let r;
  try { r = await api('skill-install', {cand: it.cand, folder: it.folder || '', target, cwd: S.cwd, mode: mode || '', newName: newName || ''}); }
  catch (e) { closeCard(); toast(e.message); return; }
  if (r.conflict) {
    c.innerHTML = `<h3>${where}已有同名技能「${esc(r.name)}」</h3><div class="acts"><button class="btn" data-a="no">取消</button>` +
      '<button class="btn" data-a="ren">改名</button><button class="btn primary" data-a="ow">覆盖</button></div>';
    onAct(c, {
      no: closeCard,
      ow: () => doInstall(it, target, 'overwrite'),
      ren: async () => { const v = await askText('新名称', r.name + '-2'); if (v && v.trim()) doInstall(it, target, 'rename', v.trim()); },
    });
    return;
  }
  closeCard();
  toast(`已安装「${r.name}」到${where}`);
  if (extShown() && F.extTab === 'skills') drawSkills();
}

/* ---- 技能市场 ---- */
function drawMarket(r) {
  const M = F.mk, box = exBox(r);
  M.box = box;
  box.innerHTML = '<div class="fx-bar mk-bar"><select id="mkSrc" class="fx-sel"><option value="anthropic">Anthropic 官方</option><option value="community">社区目录</option></select>' +
    '<input id="mkQ" class="fx-in" type="text" placeholder="搜索技能" spellcheck="false" autocomplete="off"><button class="icon" data-a="refresh" title="刷新">↻</button></div><div id="mkList"></div>';
  const sel = box.querySelector('#mkSrc'), q = box.querySelector('#mkQ');
  sel.value = M.src; q.value = M.q;
  sel.onchange = () => { M.src = sel.value; ls.set('mkSrc', M.src); M.items = null; loadMarket(); };
  let t = 0;
  q.oninput = () => { M.q = q.value; clearTimeout(t); t = setTimeout(() => loadMarket(), 400); };
  q.onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) { clearTimeout(t); loadMarket(); } };
  onAct(box, {refresh: () => loadMarket(true)});
  if (M.items && M.key === M.src + '|' + M.q) drawMarketList(); else loadMarket();
}
async function loadMarket(refresh, more) {
  const M = F.mk, seq = ++M.seq, key = M.src + '|' + M.q;
  const offset = more ? M.offset + 40 : 0;
  if (!more) { M.err = ''; if (refresh || M.key !== key) M.items = null; drawMarketList(); }
  let d;
  try { d = await api('market', {source: M.src, q: M.q, refresh: !!refresh, offset}); }
  catch (e) { if (seq === M.seq) { M.err = e.message; if (!more) M.items = null; drawMarketList(); } return; }
  if (seq !== M.seq) return;
  M.items = more ? [...(M.items || []), ...d.items] : d.items;
  M.offset = offset; M.total = d.total || 0; M.stale = d.stale; M.key = key; M.err = '';
  drawMarketList();
}
function drawMarketList() {
  const M = F.mk, host = M.box && M.box.isConnected ? M.box : exBox(), list = host && host.querySelector('#mkList');
  if (!list) return;
  if (M.err && !M.items) { list.innerHTML = `<div class="rempty fx-err">${esc(M.err)}</div>`; return; }
  if (!M.items) { list.innerHTML = '<div class="rempty">读取中…</div>'; return; }
  if (!M.items.length) { list.innerHTML = '<div class="rempty">没有找到</div>'; return; }
  list.innerHTML = (M.stale ? '<div class="rnote">网络不可用，显示的是缓存</div>' : '') + (M.err ? `<div class="rnote fx-err">${esc(M.err)}</div>` : '') +
    M.items.map((x, i) => `<div class="ex fx-item mk-item" data-i="${i}"><div class="fx-main"><div class="tk-title">${esc(x.name)}${tag(x.license)}${x.stars ? tag('★ ' + fmtNum(x.stars)) : ''}</div>` +
      `<div class="tk-sub">${esc(x.desc)}</div><div class="mk-src">${esc(x.src)}</div></div></div>`).join('') +
    (M.src === 'community' && M.total > M.items.length ? '<div class="fx-bar"><button class="btn small" data-a="more">更多</button></div>' : '');
  list.querySelectorAll('.mk-item').forEach(el => el.onclick = () => previewInstall(M.items[+el.dataset.i]));
  onAct(list, {more: () => loadMarket(false, true)});
}

/* ---- MCP ---- */
const MSCOPE = {user: '用户', project: '项目', local: '本机'};
async function drawMcp(r) {
  const box = exBox(r);
  if (!box.children.length) box.innerHTML = '<div class="rempty">读取中…</div>';
  let d;
  try { d = await api('mcp-list', {cwd: S.cwd}); } catch (e) { box.innerHTML = `<div class="rempty">${esc(e.message)}</div>`; return; }
  if (!box.isConnected || F.extTab !== 'mcp') return;
  const list = d.servers || [];
  box.innerHTML = '<div class="fx-bar"><span class="grow"></span><button class="btn small" data-a="add">添加</button></div>' + (list.length ? list.map((m, i) => {
    const st = F.mcpState[m.scope + ':' + m.name];
    const target = m.url || [m.command, ...(m.args || [])].map(a => /[\s"']/.test(a) ? JSON.stringify(a) : a).join(' ');
    const extra = [m.env.length ? '环境变量 ' + m.env.join(', ') : '', m.headers.length ? '请求头 ' + m.headers.join(', ') : ''].filter(Boolean).join(' · ');
    return `<div class="ex fx-item mcp-item" data-i="${i}"><div class="fx-main"><div class="tk-title">${esc(m.name)}${tag(MSCOPE[m.scope])}${tag(m.type)}` +
      (st ? `<span class="mcp-st ${st.state}">${esc(st.label)}</span>` : '') + `</div><div class="tk-sub" title="${esc(target)}">${esc(target)}</div>` +
      (extra ? `<div class="tk-sub">${esc(extra)}</div>` : '') + (st && st.issue ? `<div class="mcp-issue">${esc(st.issue)}</div>` : '') + '</div>' +
      `<div class="fx-acts on"><button class="link" data-a="test"${st && st.state === 'testing' ? ' disabled' : ''}>测试</button><button class="link fx-danger" data-a="del">删除</button></div></div>`;
  }).join('') : '<div class="rempty">没有配置 MCP 服务</div>');
  box.querySelector('[data-a=add]').onclick = addMcp;
  box.querySelectorAll('.mcp-item').forEach(el => {
    const m = list[+el.dataset.i];
    onAct(el, {test: () => testMcp(m), del: () => delMcp(m)});
  });
}
async function testMcp(m) {
  const k = m.scope + ':' + m.name;
  F.mcpState[k] = {state: 'testing', label: '测试中…'};
  drawMcp();
  try { F.mcpState[k] = await api('mcp-test', {cwd: S.cwd, name: m.name}); } catch (e) { F.mcpState[k] = {state: 'fail', label: '测试失败', issue: e.message}; }
  if (extShown() && F.extTab === 'mcp') drawMcp();
}
async function delMcp(m) {
  if (!confirm(`删除 MCP 服务「${m.name}」（${MSCOPE[m.scope]}）？`)) return;
  try { await api('mcp-remove', {cwd: S.cwd, name: m.name, scope: m.scope}); toast('已删除'); delete F.mcpState[m.scope + ':' + m.name]; } catch (e) { toast(e.message); }
  drawMcp();
}
function addMcp() {
  const proj = !!S.cwd;
  const c = openCard('<h3>添加 MCP 服务</h3><div class="form">' +
    '<label>名称<input id="mN" type="text" spellcheck="false" autocomplete="off"></label>' +
    `<div class="fx-2"><label>范围<select id="mS"><option value="user">用户</option>${proj ? '<option value="project">项目</option><option value="local" selected>本机</option>' : ''}</select></label>` +
    '<label>类型<select id="mT"><option value="stdio">stdio</option><option value="http">http</option><option value="sse">sse</option></select></label></div>' +
    '<div class="fx-f" data-k="stdio"><label>命令<input id="mC" type="text" spellcheck="false" autocomplete="off" placeholder="npx"></label>' +
    '<label>参数<input id="mA" type="text" spellcheck="false" autocomplete="off"></label>' +
    '<label>环境变量<textarea id="mE" rows="3" spellcheck="false" placeholder="KEY=VALUE"></textarea></label></div>' +
    '<div class="fx-f" data-k="net" hidden><label>URL<input id="mU" type="text" spellcheck="false" autocomplete="off" placeholder="https://"></label>' +
    '<label>请求头<textarea id="mH" rows="3" spellcheck="false" placeholder="Authorization: Bearer …"></textarea></label></div>' +
    '</div><div class="fx-err" hidden></div><div class="acts"><button class="btn" data-a="no">取消</button><button class="btn primary" data-a="ok">添加</button></div>');
  const v = id => c.querySelector('#' + id).value;
  const typ = c.querySelector('#mT');
  typ.onchange = () => { c.querySelector('[data-k=stdio]').hidden = typ.value !== 'stdio'; c.querySelector('[data-k=net]').hidden = typ.value === 'stdio'; };
  const err = c.querySelector('.fx-err');
  onAct(c, {
    no: closeCard,
    ok: async (e, b) => {
      b.disabled = true; err.hidden = true;
      try {
        await api('mcp-add', {cwd: S.cwd, name: v('mN').trim(), scope: v('mS'), type: v('mT'), command: v('mC'), args: v('mA'), env: v('mE'), url: v('mU'), headers: v('mH')});
        closeCard(); toast('已添加'); drawMcp();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; b.disabled = false; }
    },
  });
  c.querySelector('#mN').focus();
}

/* ================= 第十五轮 E：插件（claude plugin 子命令）、钩子与权限规则（只读） ================= */
const PSCOPE = {user: '用户', project: '项目', local: '本机', session: '随会话加载'};
const PCNT = [['skills', '技能'], ['commands', '命令'], ['agents', '代理'], ['hooks', '钩子'], ['mcp', 'MCP'], ['lsp', 'LSP']];
const ALL_SESS = '对所有 Claude Code 会话生效';
F.pl = {view: 'installed', data: null, seq: 0, q: '', market: '', items: null, total: 0, avErr: '', stale: false, markets: [], aseq: 0, busy: false};
const skel = n => '<div class="fx-skel" aria-busy="true">' + '<div><i></i><b></b></div>'.repeat(n) + '</div>';
const stateBox = (title, detail, btns) => `<div class="fx-state"><div class="fx-state-t">${esc(title)}</div>${detail ? `<div class="fx-state-d">${esc(detail)}</div>` : ''}${btns || ''}</div>`;
const plShown = () => extShown() && F.extTab === 'plugins';
const fmtCount = n => n >= 10000 ? (n / 10000).toFixed(n >= 100000 ? 0 : 1) + ' 万' : String(n);
function plCounts(p) {
  if (!p.counts) return '';
  return PCNT.filter(([k]) => p.counts[k]).map(([k, l]) => `${l} ${p.counts[k]}`).join(' · ');
}

/* 先画上次读到的，再在后台重读（插件可能在终端里被改过） */
async function drawPlugins(r) {
  const box = exBox(r), D = F.pl;
  if (!box) return;
  if (D.view === 'browse') { drawPlBrowse(box); return; }
  if (D.data) paintPlugins(box); else box.innerHTML = skel(3);
  if (await loadInstalled() && plShown() && D.view === 'installed') paintPlugins(exBox());
}
async function loadInstalled() {
  const D = F.pl, seq = ++D.seq;
  let d;
  try { d = await api('plugin-list', {cwd: S.cwd || ''}); } catch (e) { d = {plugins: null, error: e.message, markets: null, marketsError: e.message}; }
  if (seq !== D.seq) return false;
  D.data = d;
  return true;
}
function paintPlugins(box) {
  const D = F.pl, d = D.data;
  if (!box || !d) return;
  const ps = d.plugins, mks = d.markets;
  let h = '<div class="fx-bar"><span class="grow"></span><button class="btn small" data-a="addmk">添加市场</button><button class="btn small primary" data-a="browse">浏览插件</button></div>';
  if (!ps) h += stateBox('暂时拿不到插件列表', d.error, '<button class="btn small" data-a="retry">重试</button>');
  else if (!ps.length) h += stateBox('还没有安装插件', '', '<button class="btn small primary" data-a="browse">浏览插件</button>');
  else h += `<div class="rsec">已安装（${ps.length}）</div>` + ps.map((p, i) => {
    const cnt = plCounts(p);
    const meta = [p.market && p.market !== 'inline' ? p.market : '', PSCOPE[p.scope] || p.scope, cnt].filter(Boolean).join(' · ');
    return `<div class="ex fx-item pl-item${p.enabled ? '' : ' off'}" data-i="${i}"><span class="pl-ic" aria-hidden="true">${esc((p.name || '?').slice(0, 1).toUpperCase())}</span>` +
      `<div class="fx-main"><div class="tk-title">${esc(p.name)}${p.version ? tag('v' + p.version) : ''}</div>` +
      (p.desc ? `<div class="tk-sub">${esc(p.desc)}</div>` : '') + `<div class="pl-meta">${esc(meta)}</div></div>` +
      `<div class="pl-side"><label class="sw" title="${p.readonly ? '随会话加载' : p.enabled ? '停用' : '启用'}"><input type="checkbox"${p.enabled ? ' checked' : ''}${p.readonly ? ' disabled' : ''}><span></span></label>` +
      '<button class="smore pl-more" title="更多">⋯</button></div></div>';
  }).join('');
  if (mks && mks.length) h += `<div class="rsec">市场（${mks.length}）</div>` + mks.map(m =>
    `<div class="ex fx-item pl-mk"><div class="fx-main"><div class="tk-title">${esc(m.name)}</div><div class="tk-sub">${esc(m.source)}</div></div>` +
    (m.path ? `<div class="fx-acts"><button class="link" data-a="mkfinder" data-p="${esc(m.path)}">访达</button></div>` : '') + '</div>').join('');
  else if (!mks && ps) h += `<div class="rsec">市场</div><div class="fx-state sm"><div class="fx-state-d">${esc(d.marketsError || '暂时拿不到')}</div></div>`;
  box.innerHTML = h;
  onAct(box, {
    browse: () => { D.view = 'browse'; drawPlugins(); },
    addmk: addMarket,
    retry: () => { D.data = null; drawPlugins(); },
    mkfinder: (e, b) => api('reveal', {path: b.dataset.p}),
  });
  box.querySelectorAll('.pl-item').forEach(el => {
    const p = ps[+el.dataset.i];
    const sw = el.querySelector('.sw input');
    el.querySelector('.sw').onclick = e => e.stopPropagation();
    sw.onchange = () => { sw.checked = p.enabled; if (!p.readonly) togglePlugin(p); };
    el.querySelector('.pl-more').onclick = e => {
      e.stopPropagation();
      const items = [{label: '详情', fn: () => viewPlugin(p)}];
      if (p.path) items.push({label: '在访达中显示', fn: () => api('reveal', {path: p.path})});
      if (!p.readonly) items.push({sep: true}, {label: p.enabled ? '停用' : '启用', fn: () => togglePlugin(p)}, {label: '卸载', danger: true, fn: () => uninstallPlugin(p)});
      menu(e.currentTarget, items, {alignRight: true});
    };
    el.onclick = () => viewPlugin(p);
  });
}
function viewPlugin(p) {
  const rows = p.counts ? PCNT.filter(([k]) => p.counts[k]).map(([k, l]) => {
    const ns = (p.names && p.names[k]) || [];
    return `<div class="pl-comp-r"><span class="pl-comp-k">${l}<b>${p.counts[k]}</b></span><span class="pl-comp-v">${esc(ns.join('、'))}</span></div>`;
  }).join('') : '';
  const c = openCard(`<h3>${esc(p.name)}</h3><div class="fx-meta">${p.version ? tag('v' + p.version) : ''}${p.market && p.market !== 'inline' ? tag(p.market) : ''}${tag(PSCOPE[p.scope] || p.scope)}${tag(p.enabled ? '已启用' : '已停用')}</div>` +
    (p.desc ? `<div class="fx-desc">${esc(p.desc)}</div>` : '') +
    (rows ? `<div class="pl-comp">${rows}</div>` : '') +
    (p.tokens != null ? `<div class="pl-cost">常驻上下文约 ${fmtCount(p.tokens)} tokens</div>` : '') +
    `<div class="acts">${p.path ? '<button class="btn" data-a="finder">在访达中显示</button>' : ''}${p.readonly ? '' : '<button class="btn fx-danger" data-a="del">卸载</button>'}` +
    `<span class="grow"></span>${p.readonly ? '' : `<button class="btn" data-a="tog">${p.enabled ? '停用' : '启用'}</button>`}<button class="btn primary" data-a="close">关闭</button></div>`);
  onAct(c, {finder: () => api('reveal', {path: p.path}), del: () => uninstallPlugin(p), tog: () => togglePlugin(p), close: closeCard});
}
/* 会影响所有 Claude Code 会话的操作：先弹确认框，点了确定才调接口（接口也只认 confirmed: true） */
function plConfirm(o) {
  const scopes = o.scopeSel ? `<div class="form pl-cf-form"><label>范围<select id="plScope"><option value="user">用户</option>${S.cwd ? '<option value="project">项目</option><option value="local">本机</option>' : ''}</select></label></div>` : '';
  const c = openCard(`<h3>${esc(o.title)}</h3>${o.sub ? `<div class="pl-cf-sub">${esc(o.sub)}</div>` : ''}${o.html || ''}${scopes}` +
    `<div class="pl-warn" role="note">${ALL_SESS}</div><div class="fx-err" hidden></div>` +
    `<div class="acts"><button class="btn" data-a="no">取消</button><button class="btn ${o.danger ? 'pl-danger' : 'primary'}" data-a="ok">${esc(o.ok)}</button></div>`);
  c.classList.add('pl-cf');
  const err = c.querySelector('.fx-err'), okb = c.querySelector('[data-a=ok]'), nob = c.querySelector('[data-a=no]');
  onAct(c, {
    no: () => { if (!F.pl.busy) closeCard(); },
    ok: async () => {
      if (F.pl.busy) return;
      F.pl.busy = true; okb.disabled = nob.disabled = true; err.hidden = true;
      const label = okb.textContent; okb.textContent = o.doing;
      try {
        const sel = c.querySelector('#plScope');
        const r = await o.run(sel ? sel.value : undefined);
        F.pl.busy = false;
        if (r && r.needAccept) { o.onAccept && o.onAccept(r.needAccept, sel ? sel.value : undefined); return; }
        closeCard();
        toast(r && r.dry ? '空跑：' + (r.cmd || []).join(' ') : o.done);
        loadInstalled().then(ok => { if (ok && plShown()) { if (F.pl.view === 'installed') paintPlugins(exBox()); else paintAvail(); } });
      } catch (e) {
        F.pl.busy = false; okb.disabled = nob.disabled = false; okb.textContent = label;
        err.textContent = e.message; err.hidden = false;
      }
    },
  });
  okb.focus();
  return c;
}
const pcall = (name, body) => api(name, Object.assign({cwd: S.cwd || '', confirmed: true}, body));
function togglePlugin(p) {
  const on = !p.enabled;
  plConfirm({title: `${on ? '启用' : '停用'}插件「${p.name}」？`, sub: p.id + (p.scope && p.scope !== 'user' ? ' · ' + (PSCOPE[p.scope] || p.scope) : ''),
    ok: on ? '启用' : '停用', doing: on ? '正在启用…' : '正在停用…', done: on ? '已启用' : '已停用',
    run: () => pcall('plugin-enable', {id: p.id, scope: p.scope, on})});
}
function uninstallPlugin(p) {
  plConfirm({title: `卸载插件「${p.name}」？`, sub: p.id + (p.scope && p.scope !== 'user' ? ' · ' + (PSCOPE[p.scope] || p.scope) : ''), danger: true,
    ok: '卸载', doing: '正在卸载…', done: '已卸载', run: () => pcall('plugin-uninstall', {id: p.id, scope: p.scope})});
}
function installPlugin(x) {
  const go = (accept, scope) => pcall('plugin-install', {id: x.id, scope: scope || 'user', accept: accept || ''});
  plConfirm({title: `安装插件「${x.name}」？`, sub: x.id, scopeSel: true, ok: '安装', doing: '正在安装…', done: '已安装',
    run: scope => go('', scope),
    onAccept: (na, scope) => plConfirm({title: `安装「${x.name}」需要运行这条命令`, sub: x.id, html: `<pre class="blk pl-cmd">${esc(na.command)}</pre>`,
      ok: '仍然安装', doing: '正在安装…', done: '已安装', run: () => go(na.sha256, scope)})});
}
function addMarket() {
  const c = plConfirm({title: '添加插件市场', ok: '添加', doing: '正在添加…', done: '已添加市场',
    html: '<div class="form pl-cf-form"><label>地址<input id="plSrc" type="text" spellcheck="false" autocomplete="off" placeholder="owner/repo"></label></div>',
    run: () => {
      const v = c.querySelector('#plSrc').value.trim();
      if (!v) { c.querySelector('#plSrc').focus(); return Promise.reject(new Error('请填写市场地址')); }
      return pcall('plugin-market-add', {source: v});
    }});
  const inp = c.querySelector('#plSrc');
  inp.onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) c.querySelector('[data-a=ok]').click(); if (e.key === 'Escape' && !F.pl.busy) closeCard(); };
  inp.focus();
}

/* ---- 浏览市场里的插件 ---- */
function drawPlBrowse(box) {
  const D = F.pl;
  box.innerHTML = '<div class="fx-bar mk-bar pl-bar"><button class="link pl-back" data-a="back">‹ 已安装</button>' +
    '<select id="plMk" class="fx-sel"><option value="">全部市场</option></select>' +
    '<input id="plQ" class="fx-in" type="text" placeholder="搜索插件" spellcheck="false" autocomplete="off"><button class="icon" data-a="refresh" title="刷新">↻</button></div><div id="plList"></div>';
  const sel = box.querySelector('#plMk'), q = box.querySelector('#plQ');
  fillMarkets(sel);
  sel.value = D.market; q.value = D.q;
  sel.onchange = () => { D.market = sel.value; loadAvail(); };
  let t = 0;
  q.oninput = () => { D.q = q.value; clearTimeout(t); t = setTimeout(() => loadAvail(), 300); };
  q.onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) { clearTimeout(t); loadAvail(); } };
  onAct(box, {back: () => { D.view = 'installed'; drawPlugins(); }, refresh: () => loadAvail(true)});
  if (D.items && D.key === D.market + '|' + D.q) paintAvail(); else loadAvail();
  if (!D.data) loadInstalled().then(ok => { if (ok && plShown() && D.view === 'browse') paintAvail(); });  // 标出已安装的
}
function fillMarkets(sel) {
  if (!sel) return;
  const v = F.pl.market;
  sel.innerHTML = '<option value="">全部市场</option>' + F.pl.markets.map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join('');
  if (v && !F.pl.markets.some(m => m.name === v)) sel.insertAdjacentHTML('beforeend', `<option value="${esc(v)}">${esc(v)}</option>`);
  sel.value = v;
}
async function loadAvail(refresh, more) {
  const D = F.pl, seq = ++D.aseq, key = D.market + '|' + D.q;
  const offset = more ? (D.items || []).length : 0;
  if (!more) { D.avErr = ''; D.items = null; paintAvail(); }
  let d;
  try { d = await api('plugin-available', {q: D.q, market: D.market, offset, refresh: !!refresh}); }
  catch (e) { if (seq === D.aseq) { D.avErr = e.message; if (!more) D.items = null; paintAvail(); } return; }
  if (seq !== D.aseq) return;
  D.items = more ? [...(D.items || []), ...d.items] : d.items;
  D.total = d.total || 0; D.stale = !!d.stale; D.key = key; D.avErr = '';
  if (d.markets) { D.markets = d.markets; fillMarkets(exHost().querySelector('#plMk')); }
  paintAvail();
}
function paintAvail() {
  const D = F.pl, list = exHost().querySelector('#plList');
  if (!list) return;
  if (D.avErr && !D.items) { list.innerHTML = stateBox('暂时拿不到可安装的插件', D.avErr, '<button class="btn small" data-a="retry">重试</button>'); onAct(list, {retry: () => loadAvail(true)}); return; }
  if (!D.items) { list.innerHTML = skel(4); return; }
  if (!D.items.length) { list.innerHTML = stateBox(D.q ? '没有找到' : '这个市场里没有插件', ''); return; }
  const inst = new Set(((D.data && D.data.plugins) || []).map(p => p.id));
  list.innerHTML = (D.stale ? '<div class="rnote">网络不可用，显示的是缓存</div>' : '') + (D.avErr ? `<div class="rnote fx-err">${esc(D.avErr)}</div>` : '') +
    `<div class="rsec">${D.total} 个插件</div>` +
    D.items.map((x, i) => `<div class="ex fx-item pl-av" data-i="${i}"><span class="pl-ic" aria-hidden="true">${esc((x.name || '?').slice(0, 1).toUpperCase())}</span>` +
      `<div class="fx-main"><div class="tk-title">${esc(x.name)}${x.version ? tag('v' + x.version) : ''}</div>` +
      (x.desc ? `<div class="tk-sub">${esc(x.desc)}</div>` : '') + `<div class="pl-meta">${esc([x.market, x.n ? '安装 ' + fmtCount(x.n) : ''].filter(Boolean).join(' · '))}</div></div>` +
      `<div class="pl-side">${inst.has(x.id) ? '<span class="pl-done">已安装</span>' : '<button class="btn small pl-inst" data-a="inst">安装</button>'}</div></div>`).join('') +
    (D.total > D.items.length ? '<div class="fx-bar pl-more-bar"><button class="btn small" data-a="more">更多</button></div>' : '');
  list.querySelectorAll('.pl-av').forEach(el => onAct(el, {inst: () => installPlugin(D.items[+el.dataset.i])}));
  onAct(list, {more: () => loadAvail(false, true)});
}

/* ---- 钩子与权限规则（只读） ---- */
const RSRC = {user: '用户', project: '项目', local: '本机'};
const RKIND = {allow: '允许', ask: '询问', deny: '拒绝'};
F.rl = {seq: 0};
async function drawRules(r) {
  const box = exBox(r);
  if (!box) return;
  box.innerHTML = skel(3);
  const seq = ++F.rl.seq;
  let d;
  try { d = await api('settings-rules', {cwd: S.cwd || ''}); } catch (e) { d = {error: e.message}; }
  if (seq !== F.rl.seq || !extShown() || F.extTab !== 'rules') return;
  const b = exBox();
  if (!b) return;
  if (d.error) { b.innerHTML = stateBox('暂时拿不到设置文件', d.error, '<button class="btn small" data-a="retry">重试</button>'); onAct(b, {retry: () => drawRules()}); return; }
  const files = d.files || [], hooks = d.hooks || [], perms = d.perms || [];
  const fin = p => `<div class="fx-acts"><button class="link" data-a="finder" data-p="${esc(p)}">访达</button></div>`;
  const warns = files.filter(f => f.error || f.disableAllHooks).map(f => `<div class="ex fx-item rl-warn"><div class="fx-main"><div class="tk-title">${tag(RSRC[f.key])}<span>${esc(f.error ? f.error : '停用了全部钩子')}</span></div><div class="tk-sub rl-path">${esc(C.shortPath ? C.shortPath(f.path) : f.path)}</div></div>${fin(f.path)}</div>`).join('');
  const hk = hooks.map(x => `<div class="ex fx-item rl-item"><div class="fx-main"><div class="tk-title"><span class="rl-ev">${esc(x.event)}</span>` +
    `${x.matcher && x.matcher !== '*' ? `<span class="rl-match" title="匹配">${esc(x.matcher)}</span>` : ''}${tag(RSRC[x.src])}${x.type !== 'command' ? tag(x.type) : ''}${x.async ? tag('异步') : ''}</div>` +
    `<div class="tk-sub rl-cmd" title="${esc(x.text)}">${esc(x.text)}</div>${x.if ? `<div class="tk-sub rl-if">${esc(x.if)}</div>` : ''}</div>${fin(x.path)}</div>`).join('');
  const order = {allow: 0, ask: 1, deny: 2};
  const pm = perms.slice().sort((a, b2) => order[a.kind] - order[b2.kind]).map(x => `<div class="ex fx-item rl-item"><div class="fx-main"><div class="tk-title rl-prow">` +
    `<span class="rl-kind rl-${x.kind}">${RKIND[x.kind]}</span><span class="rl-rule" title="${esc(x.rule)}">${esc(x.rule)}</span>${tag(RSRC[x.src])}</div></div>${fin(x.path)}</div>`).join('');
  b.innerHTML = warns + `<div class="rsec">钩子（${hooks.length}）</div>` + (hk || '<div class="fx-state sm"><div class="fx-state-d">没有钩子</div></div>') +
    `<div class="rsec">权限规则（${perms.length}）</div>` + (pm || '<div class="fx-state sm"><div class="fx-state-d">没有权限规则</div></div>');
  b.querySelectorAll('[data-a=finder]').forEach(x => x.onclick = e => { e.stopPropagation(); api('reveal', {path: x.dataset.p}); });
}

/* 定时任务有运行开始、结束、错过时，后台推 {type: 'sched'}：右栏“定时”开着就重画（列表在 app2.js） */
{
  const prev = C.onEvent;
  C.onEvent = ev => {
    try { if (prev) prev(ev); } finally {
      if (ev && ev.type === 'sched' && !$('#rpane').hidden && P.R.tab === 'sched') { clearTimeout(F.schedT); F.schedT = setTimeout(() => P.render(), 250); }
    }
  };
}

/* ================= 左栏：项目内分组、多选 ================= */
C.groupSessions = ss => {
  const now = new Date(), t0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const y0 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime() / 1000;
  const g = {att: [], busy: [], today: [], yest: [], old: []};
  for (const s of ss) (s.attention ? g.att : s.busy ? g.busy : s.mtime >= t0 ? g.today : s.mtime >= y0 ? g.yest : g.old).push(s);
  return [['需要处理', g.att], ['进行中', g.busy], ['今天', g.today], ['昨天', g.yest], ['更早', g.old]].filter(x => x[1].length);
};
C.isSel = sid => F.sel.has(sid);
const sideOrder = () => [...document.querySelectorAll('#projects .srow[data-sid]:not(.w-hide)')].map(x => x.dataset.sid);  // 11-24：收起的子对话不进 Shift 连选
C.sideClick = (e, s) => {
  if (e.metaKey || e.ctrlKey) {
    if (F.sel.has(s.sid)) F.sel.delete(s.sid); else F.sel.add(s.sid);
    F.anchor = s.sid; C.renderSide(); return true;
  }
  if (e.shiftKey) {
    const order = sideOrder(), b = order.indexOf(s.sid);
    let a = order.indexOf(F.anchor);
    if (a < 0) a = order.indexOf(S.cur);
    if (a < 0) F.sel.add(s.sid);
    else order.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(x => F.sel.add(x));
    if (a < 0) F.anchor = s.sid;
    getSelection().removeAllRanges();
    C.renderSide(); return true;
  }
  F.anchor = s.sid;
  if (F.sel.size) { F.sel.clear(); C.renderSide(); }
  return false;
};
function allSessions() { const m = new Map(); for (const p of S.projects) for (const s of p.sessions) m.set(s.sid, s); return m; }
C.onSide = () => {
  const all = allSessions();
  for (const sid of [...F.sel]) if (!all.has(sid)) F.sel.delete(sid);
  let bar = $('#selBar');
  if (!F.sel.size) { if (bar) bar.hidden = true; return; }
  if (!bar) {
    bar = document.createElement('div'); bar.id = 'selBar';
    $('#side').insertBefore(bar, $('#side .side-foot'));
  }
  const list = [...F.sel].map(x => all.get(x)), arch = list.every(s => s.archived);
  bar.innerHTML = `<span class="sb-n">已选 ${F.sel.size} 个</span><i>·</i><button class="link" data-a="arch">${arch ? '恢复' : '归档'}</button><i>·</i>` +
    '<button class="link fx-danger" data-a="del">删除</button><i>·</i><button class="link" data-a="no">取消</button>';
  bar.hidden = false;
  onAct(bar, {arch: () => batchArchive(!arch), del: batchDelete, no: clearSel});
};
function clearSel() { F.sel.clear(); F.anchor = null; C.renderSide(); }
async function batchArchive(on) {
  const sids = [...F.sel];
  if (C.undoArchive) { const all = allSessions(); F.sel.clear(); F.anchor = null; C.undoArchive(sids.map(x => all.get(x)), on); return; }  // 第十三轮 F：可撤销
  let ok = 0, err = '';
  for (const sid of sids) { try { await api('archive', {sid, on}); ok++; } catch (e) { err = e.message; } }
  const all = allSessions();
  for (const sid of sids) { const s = all.get(sid); if (s) s.archived = on; }
  F.sel.clear(); C.renderSide(); C.refreshSide(true);
  toast(err ? `${ok} 个成功，${sids.length - ok} 个失败：${err}` : `已${on ? '归档' : '恢复'} ${ok} 个对话`);
}
async function batchDelete() {
  const sids = [...F.sel];
  if (C.undoDelete) { const all = allSessions(); F.sel.clear(); F.anchor = null; C.undoDelete(sids.map(x => all.get(x))); return; }  // 第十三轮 F：不再确认，10 秒内可撤销
  if (!confirm(`删除选中的 ${sids.length} 个对话？\n会话记录会移到废纸篓，需要时可以从废纸篓找回。`)) return;
  let ok = 0, err = '';
  for (const sid of sids) {
    try {
      await api('delete', {sid}); ok++;
      for (const p of S.projects) p.sessions = p.sessions.filter(x => x.sid !== sid);
      if (S.cur === sid && C.clearMain) C.clearMain();
    } catch (e) { err = e.message; }
  }
  F.sel.clear(); C.renderSide(); C.refreshSide(true);
  toast(err ? `已删除 ${ok} 个，${sids.length - ok} 个失败：${err}` : `已移到废纸篓：${ok} 个对话`);
}
addEventListener('keydown', e => {
  if (e.key === 'Escape' && F.sel.size && $('#modal').hidden && $('#palette').hidden) { e.stopPropagation(); e.preventDefault(); clearSel(); }
}, true);
C.renderSide();
})();

/* 输入区下拉框宽度贴合当前文字（不支持 field-sizing 的 WebKit 用测量兜底） */
(function () {
  if (window.CSS && CSS.supports && CSS.supports('field-sizing', 'content')) return;
  const cv = document.createElement('canvas').getContext('2d');
  const fit = el => {
    const cs = getComputedStyle(el), o = el.options[el.selectedIndex];
    cv.font = cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
    el.style.width = Math.ceil(cv.measureText(o ? o.text : '').width + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + 2) + 'px';
  };
  let last = '';
  const check = () => {
    const els = [...document.querySelectorAll('.bar select')];
    const k = els.map(s => s.value + ':' + s.options.length).join('|');
    if (k !== last) { last = k; els.forEach(fit); }
  };
  document.addEventListener('change', e => { if (e.target.matches && e.target.matches('.bar select')) fit(e.target); });
  setInterval(check, 400); check();
})();
