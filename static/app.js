(() => {
'use strict';
const T = new URLSearchParams(location.search).get('t') || '';
const $ = s => document.querySelector(s);
const input = $('#input');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const store = {
  get(k, d) { try { const v = localStorage.getItem('cwb.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { if (v == null) localStorage.removeItem('cwb.' + k); else localStorage.setItem('cwb.' + k, JSON.stringify(v)); } catch {} },
};
async function api(p, body) {
  let r;
  try { r = await fetch('/api/' + p, {method: 'POST', headers: {'X-Token': T, 'Content-Type': 'application/json'}, body: JSON.stringify(body || {})}); }
  catch { throw new Error('连不上后台服务，正在自动恢复，请稍后再试'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || ('操作没有成功（' + r.status + '）')), {data: j});  // 第十五轮 F1：带上返回内容（blocked、hint 等）
  return j;
}
/* 系统通知由窗口自己发：点击通知直接回到工作台并打开对应对话 */
const NATIVE = !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.cwb);
function native(msg) { try { window.webkit.messageHandlers.cwb.postMessage(msg); } catch {} }
function notifyLocal(title, text, sid) {
  if (NATIVE) { native({type: 'notify', title: title || 'Claude 工作台', text: text || '', sid: sid || ''}); return; }  // 原生 App：系统通知
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    const n = new Notification(title || 'Claude 工作台', {body: text || '', tag: 'cwb-' + (sid || '') + '-' + (text || '').slice(0, 8), icon: '/static/icon.svg'});
    n.onclick = () => { window.focus(); if (sid && sid !== S.cur) openSession(sid); n.close(); };
  } catch {}
}
function askNotifyPermission() {
  if (!NATIVE && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(() => {});
}
const DEFAULT_PHRASES = ['继续', '按方案执行', '先别改，只给建议', '总结一下刚才做了什么'];

const S = {
  projects: [], home: '', boot: null, models: {default: '', list: []}, commands: {cwd: null, list: []},
  cur: null, loading: null, buffer: [], cwd: '', title: '', model: null,
  byId: new Map(), tl: null, runner: null, perms: new Map(), opts: {}, recent: false, attsBy: {}, userUp: false,
  expanded: new Set(store.get('expanded', [])), showArch: store.get('showArch', false),
  filter: '', hits: [], status: '', modalKey: null, pendings: [], atts: [], busyMap: {},
  hist: -1, pop: null, phrases: store.get('phrases', DEFAULT_PHRASES),
};

/* ================= 通用小部件 ================= */
let toastTimer = null;
function toast(t) {
  const el = $('#toast'); el.textContent = t; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}
async function copy(text, btn) {
  try { await navigator.clipboard.writeText(text); } catch {
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
  }
  if (btn) { const o = btn.textContent; btn.textContent = '已复制'; setTimeout(() => { btn.textContent = o; }, 1200); }
}
function cpBtn(getText) {
  const b = document.createElement('button');
  b.className = 'cp'; b.textContent = '复制'; b.type = 'button';
  b.onclick = e => { e.stopPropagation(); copy(getText(), b); };
  return b;
}
/* 弹出菜单：items = [{label, sub, fn, danger, sep}] */
function menu(anchor, items, opt = {}) {
  closeMenu();
  const m = document.createElement('div');
  m.className = 'menu' + (opt.wide ? ' wide' : '');
  for (const it of items) {
    if (it.sep) { m.appendChild(Object.assign(document.createElement('div'), {className: 'msep'})); continue; }
    if (it.head) { m.appendChild(Object.assign(document.createElement('div'), {className: 'mhead', textContent: it.head})); continue; }
    const b = document.createElement('button');
    b.className = 'mi' + (it.danger ? ' danger' : '');
    b.innerHTML = `<span>${esc(it.label)}</span>` + (it.sub ? `<small>${esc(it.sub)}</small>` : '');
    if (it.title) b.title = it.title;
    b.onclick = e => { e.stopPropagation(); closeMenu(); it.fn && it.fn(e); };
    m.appendChild(b);
  }
  if (!items.length) m.innerHTML = `<div class="mhead">${esc(opt.empty || '暂无')}</div>`;
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : {left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y};
  const w = m.offsetWidth, h = m.offsetHeight;
  let x = opt.alignRight ? r.right - w : r.left, y = r.bottom + 4;
  if (y + h > innerHeight - 8) y = Math.max(8, r.top - h - 4);
  m.style.left = Math.max(8, Math.min(x, innerWidth - w - 8)) + 'px';
  m.style.top = y + 'px';
  S.menu = m;
}
function closeMenu() { if (S.menu) { S.menu.remove(); S.menu = null; } }
document.addEventListener('mousedown', e => { if (S.menu && !S.menu.contains(e.target)) closeMenu(); });
/* 文本输入弹窗 */
function askText(title, value, multi) {
  return new Promise(ok => {
    S.modalKey = 'ask';
    const card = $('#card');
    card.innerHTML = `<h3>${esc(title)}</h3>` + (multi ? `<textarea class="ask" rows="8"></textarea>` : `<input class="ask" type="text">`) +
      '<div class="acts"><button class="btn" data-a="no">取消</button><button class="btn primary" data-a="ok">确定</button></div>';
    const f = card.querySelector('.ask'); f.value = value || '';
    const done = v => { closeModal(); ok(v); showModal(); };
    card.querySelector('[data-a=no]').onclick = () => done(null);
    card.querySelector('[data-a=ok]').onclick = () => done(f.value);
    f.onkeydown = e => { if (e.key === 'Enter' && !multi && !e.isComposing) done(f.value); if (e.key === 'Escape') done(null); };
    $('#modal').hidden = false; f.focus(); f.select();
  });
}
const imgUrl = m => `/img?sid=${encodeURIComponent(S.cur)}&o=${m.o}&p=${m.p.join(',')}&t=${encodeURIComponent(T)}`;
const fileUrl = p => `/file?p=${encodeURIComponent(p)}&cwd=${encodeURIComponent(S.cwd)}&t=${encodeURIComponent(T)}`;
function thumbs(list) {
  if (!Array.isArray(list) || !list.length) return '';
  return `<div class="thumbs">${list.map(m => `<img loading="lazy" src="${esc(typeof m === 'string' ? m : imgUrl(m))}" alt="">`).join('')}</div>`;
}
function lightbox(src) { const lb = $('#lightbox'); lb.querySelector('img').src = src; lb.hidden = false; }
$('#lightbox').onclick = () => { $('#lightbox').hidden = true; };
const fmtSecs = s => s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`;
const fmtTok = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1000 ? (n / 1000).toFixed(n >= 1e5 ? 0 : 1) + 'k' : String(n);

/* ================= Markdown ================= */
if (window.marked) {
  marked.use({gfm: true, renderer: {html(t) { return esc(typeof t === 'string' ? t : (t && (t.text || t.raw)) || ''); }}});
}
/* 11-23：agy（Antigravity）回复里的文件链接写成 file:///Users/…，DOMPurify 默认不放行 file: 协议，会把 href 整个删掉，
   链接看着是蓝的却点不动；图片 src 同理。清洗前先把 file:// 换成普通绝对路径，后面照常走侧栏预览和 /file 取图。 */
const FILE_URL_ATTR = /(\s(?:href|src)=["'])file:\/\/(?:localhost)?(?=\/)/gi;
function purify(h) {
  h = h.replace(FILE_URL_ATTR, '$1');
  return window.DOMPurify ? DOMPurify.sanitize(h) : h;
}
function md(s) {
  return purify(window.marked ? marked.parse(s || '') : '<p>' + esc(s).replace(/\n/g, '<br>') + '</p>');
}
/* 11-23：回复里常只写文件名（如“文件夹里的 `改动说明_20261123.md`”），没有斜杠也要能点。
   只认常见文件扩展名，并排除 `console.log`、`e.key`、`res.json` 这类代码写法，免得把代码当成文件。 */
const FILE_EXT = 'md|markdown|txt|log|csv|tsv|json|jsonl|ya?ml|toml|ini|cfg|conf|xml|html?|css|scss|less|m?js|cjs|tsx?|jsx|vue|py|ipynb|sh|zsh|bash|command|swift|go|rs|java|kt|cpp|hpp|rb|php|sql|db|sqlite|lua|tex|bib|rtf|pdf|docx?|xls[xm]?|pptx?|wps|et|dps|ofd|png|jpe?g|gif|webp|bmp|svg|ico|heic|tiff?|psd|mp4|mov|mp3|wav|m4a|zip|rar|7z|tar|gz|tgz|dmg|pkg|dwg|dxf|kml|kmz|shp|geojson|gpx|plist|patch|diff|bak|epub|xmind|vsdx?|mpp';
const BARE_FILE_RE = new RegExp(`^[\\w\\-.一-鿿（）()【】《》·、，＋+&@# ]*[\\w一-鿿）)】》]\\.(${FILE_EXT})(:\\d+(-\\d+)?)?$`, 'i');
const CODE_OBJ_RE = /^(console|logger|logging|math|np|torch|self|this|res|req|resp|r|e|d|it|el)\./i;
function isBareFile(s) {
  if (!BARE_FILE_RE.test(s) || CODE_OBJ_RE.test(s)) return false;
  return !/\s/.test(s) || /[一-鿿]/.test(s);  // 带空格的只认含中文的文件名，免得把 `python3 x.py` 这类命令当成文件
}
function isPathLike(s) {
  s = (s || '').trim();
  if (!s || s.length > 500 || /^https?:\/\//i.test(s)) return false;
  if (/^(~\/|\/)[^\0<>|*?"]+$/.test(s) && (s.match(/\//g) || []).length >= 2) return true;
  // 11-11：文件名里常有全角括号、书名号、顿号、空格等（如“B批/芮城县黄土地区（风陵渡镇12MW）.docx”），一并认作路径
  if (/^\.{0,2}\/?[\w\-.一-鿿（）()【】《》·、，＋+&@# ]+(\/[\w\-.一-鿿（）()【】《》·、，＋+&@# ]+)+\/?(:\d+(-\d+)?)?$/.test(s) && /\.[A-Za-z0-9]{1,8}(:\d+)?$|\/$/.test(s)) return true;
  return isBareFile(s);
}
const NO_PREVIEW = /\.(docx?|xlsx?|pptx?|key|pages|numbers|zip|dmg|mp4|mov|mp3)$/i;
const pathTip = p => (NO_PREVIEW.test(p.replace(/:\d+(-\d+)?$/, '')) ? '点击在访达中显示' : '点击在侧栏预览 · ⌘ 点击在访达中显示') + ' · 右键更多';
/* 11-23：正文里没加反引号的完整路径（/Users/…、~/… 开头，以常见扩展名结尾）也做成可点的 */
const ABS_IN_TEXT = new RegExp(`(?:~|/(?:Users|Volumes|Applications|Library|private|tmp|opt))/[^\\s<>"'\`，。；：！？、“”‘’]*?\\.(?:${FILE_EXT})(?![\\w.\\-])`, 'gi');
function linkTextPaths(el) {
  const tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {acceptNode: n =>
    (n.nodeValue.includes('/') && !n.parentElement.closest('code, pre, a, .path, button')) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT});
  const nodes = [];
  while (tw.nextNode()) nodes.push(tw.currentNode);
  for (const n of nodes) {
    const s = n.nodeValue; let last = 0, m; const frag = document.createDocumentFragment();
    ABS_IN_TEXT.lastIndex = 0;
    while ((m = ABS_IN_TEXT.exec(s))) {
      if (m.index && /[\w.\-/]/.test(s[m.index - 1])) continue;  // 不从半截路径或网址中间开始
      frag.append(s.slice(last, m.index));
      const sp = document.createElement('span');
      sp.className = 'path'; sp.dataset.path = m[0]; sp.title = pathTip(m[0]); sp.textContent = m[0];
      frag.append(sp); last = m.index + m[0].length;
    }
    if (last) { frag.append(s.slice(last)); n.replaceWith(frag); }
  }
}
function enhance(el) {
  el.querySelectorAll('code').forEach(c => {
    if (c.closest('pre')) return;
    if (isPathLike(c.textContent)) { c.classList.add('path'); c.dataset.path = c.textContent.trim(); c.title = pathTip(c.dataset.path); }
  });
  try { linkTextPaths(el); } catch {}
  el.querySelectorAll('a[href]').forEach(a => { if (!/^https?:/i.test(a.getAttribute('href'))) a.title = pathTip(a.getAttribute('href') || ''); });
  el.querySelectorAll('img').forEach(img => {
    const src = img.getAttribute('src') || '';
    if (src && !/^(https?:|data:|blob:|\/img\?|\/file\?)/i.test(src)) img.src = fileUrl(decodeURIComponent(src));
  });
  if (window.hljs) el.querySelectorAll('pre code').forEach(c => { try { hljs.highlightElement(c); } catch {} });
  el.querySelectorAll('pre').forEach(pre => {
    const code = pre.querySelector('code') || pre;
    if (pre.closest('.md') && !pre.querySelector(':scope > .code-hd')) {
      const m = /(?:^|\s)language-([A-Za-z0-9_+#.-]+)/.exec(code.className || '');
      const lang = m ? m[1].toLowerCase() : '';
      const hd = document.createElement('div');
      hd.className = 'code-hd';
      const sp = document.createElement('span');
      sp.className = 'code-lang';
      sp.textContent = lang || 'text';
      hd.append(sp, cpBtn(() => code.textContent));
      pre.classList.add('has-hd');
      pre.prepend(hd);
    } else {
      pre.appendChild(cpBtn(() => code.textContent));
    }
  });
}
function shortPath(p) {
  if (!p) return '';
  if (S.cwd && p.startsWith(S.cwd + '/')) return p.slice(S.cwd.length + 1);
  if (S.home && p.startsWith(S.home + '/')) return '~' + p.slice(S.home.length);
  return p;
}

/* ================= 时间线 ================= */
const NO_OUT = new Set(['Read', 'Edit', 'Write', 'NotebookEdit', 'TodoWrite', 'ExitPlanMode']);
function row(label, txt, cls) {
  if (!txt) return '';
  return `<div class="row ${cls || ''}"><b>${label}</b><pre>${esc(txt)}</pre></div>`;
}
function lineDiff(a, b) {
  const A = (a || '').split('\n'), B = (b || '').split('\n');
  if (A.length * B.length > 250000) return [...A.map(s => ['-', s]), ...B.map(s => ['+', s])];
  const n = A.length, m = B.length, L = Array.from({length: n + 1}, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { out.push([' ', A[i]]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) out.push(['-', A[i++]]);
    else out.push(['+', B[j++]]);
  }
  while (i < n) out.push(['-', A[i++]]);
  while (j < m) out.push(['+', B[j++]]);
  return out;
}
const LANG = {py: 'python', js: 'javascript', mjs: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript', json: 'json', md: 'markdown', sh: 'bash', css: 'css', html: 'xml', xml: 'xml', yaml: 'yaml', yml: 'yaml', sql: 'sql', go: 'go', rs: 'rust', java: 'java', swift: 'swift', c: 'c', cpp: 'cpp', toml: 'ini'};
function langOf(p) { const m = /\.([A-Za-z0-9]+)$/.exec(p || ''); const l = m && LANG[m[1].toLowerCase()]; return l && window.hljs && hljs.getLanguage(l) ? l : ''; }
function hl(line, lang) { if (!lang) return esc(line); try { return hljs.highlight(line, {language: lang, ignoreIllegals: true}).value; } catch { return esc(line); } }
function diffRow(a, b, lang) {
  const html = lineDiff(a, b).map(([t, s]) => `<span class="dl ${t === '-' ? 'del' : t === '+' ? 'add' : 'same'}"><i>${t === ' ' ? '' : t}</i>${hl(s, lang) || ' '}</span>`).join('');
  return `<div class="row diff"><b>DIFF</b><pre>${html}</pre></div>`;
}
function renderTool(el, it, nested) {
  el.className = 'it tool ' + (it.done ? (it.error ? 'err' : it.noresult ? 'end' : 'ok') : 'run') + (it._fold ? ' fold' : '');
  el.dataset.tool = it.name || '';  // 第十五轮打磨：样式按工具区分（计划被退回不按出错显示）
  let head = `<div class="head">${esc(it.name)}`;
  if (it.path) head += `<a class="d" data-path="${esc(it.path)}" title="⌘ 点击在访达中显示">${esc(shortPath(it.path))}</a>`;
  if (it.desc && !(it.path && it.desc === it.path)) head += `<span class="d">${esc(it.desc)}</span>`;
  head += '</div>';
  if (it.todos) {
    el.innerHTML = head + `<ul class="todos">${it.todos.map(t => `<li class="${esc(t.s)}">${esc(t.c)}</li>`).join('')}</ul>`;
  } else {
    let body = it.diff ? diffRow(it.diff.old, it.diff.new, langOf(it.path)) : row('IN', it.in);
    if (it.out && (it.error || !NO_OUT.has(it.name))) body += row(it.error ? 'ERR' : 'OUT', it.out, it.error ? 'eout' : '');
    el.innerHTML = head + (body ? `<div class="box">${body}</div>` : '') + thumbs(it.imgs) +
      (it.bg ? '<div class="sub">后台运行</div>' : '') +
      (!it.done ? '<div class="sub runlbl"><span class="r1">运行中…</span><span class="r2">未完成</span></div>' : it.noresult ? `<div class="sub">${esc(it.noresult)}</div>` : '');
    const box = el.querySelector('.box');
    if (box) {
      const bt = document.createElement('div'); bt.className = 'bt';
      const cb = cpBtn(() => [it.in, it.out].filter(Boolean).join('\n\n')); cb.classList.add('cpb');
      bt.appendChild(cb); box.appendChild(bt);
      if (!it.diff) foldBox(box, it);
    }
    if (it.agent && !nested) {
      const b = document.createElement('button');
      b.className = 'link subbtn'; b.textContent = '查看子代理过程';
      b.onclick = e => { e.stopPropagation(); toggleSub(el, it, b); };
      el.appendChild(b);
    }
  }
  hook('Tool', el, it);  // 第十二轮 D：工具图标、改文件的增删行数（ux-d.js）
  el.querySelector('.head').onclick = e => {
    if (e.target.closest('[data-path]')) return;
    it._fold = !it._fold; el.classList.toggle('fold', it._fold);
  };
}
/* 脚本框（IN/OUT）默认折成一行，只显示命令首行；点击展开全部，收起回到一行 */
function foldBox(box, it) {
  const ins = it.in || '';
  if (!ins.includes('\n') && ins.length < 120 && box.querySelectorAll('.row').length < 2) return;
  box.classList.add('fx'); box.dataset.m = 1;
  const top = document.createElement('button'); top.type = 'button';
  const bottom = document.createElement('button'); bottom.type = 'button'; bottom.className = 'more'; bottom.textContent = '收起';
  const set = open => {
    it._open = open; box.classList.toggle('one', !open); box.classList.toggle('open', open);
    top.textContent = open ? '收起' : '展开'; bottom.hidden = !open;
    if (!open) { const r = box.getBoundingClientRect(); if (r.top < 0) box.scrollIntoView({block: 'nearest'}); }
  };
  top.onclick = e => { e.stopPropagation(); set(!it._open); };
  bottom.onclick = e => { e.stopPropagation(); set(false); };
  box.onclick = e => { if (!it._open && !e.target.closest('button')) set(true); };
  box.querySelector('.bt').prepend(top); box.appendChild(bottom);
  set(!!it._open);
}
async function toggleSub(el, it, btn) {
  const ex = el.querySelector('.subtl');
  if (ex) { ex.remove(); btn.textContent = '查看子代理过程'; return; }
  btn.textContent = '加载中…';
  try {
    const d = await api('subagent', {sid: S.cur, agent: it.agent});
    const box = document.createElement('div'); box.className = 'subtl tl';
    if (!d.items.length) box.innerHTML = '<div class="note">没有找到子代理记录</div>';
    for (const x of d.items) {
      const c = Object.assign({}, x, {id: 'sub-' + it.agent + '-' + x.id});
      box.appendChild(c.k === 'user' ? Object.assign(renderItem(c, true), {className: 'it note subprompt'}) : renderItem(c, true));
    }
    el.appendChild(box); addMore(box); btn.textContent = '收起子代理过程';
  } catch (e) { toast(e.message); btn.textContent = '查看子代理过程'; }
}
function statText(st) {
  if (!st || !st.secs && !st.out) return '';
  return [fmtSecs(st.secs), st.out ? '输出 ' + fmtTok(st.out) : '', st.ctx ? '上下文 ' + fmtTok(st.ctx) : ''].filter(Boolean).join(' · ');
}
function renderItem(it, nested) {
  const el = document.createElement('div');
  el.dataset.id = it.id;
  switch (it.k) {
    case 'user':
      el.className = 'user';
      el.innerHTML = (nested ? '' : `<span class="ustat">${esc(statText(it.stat))}</span>`) + (it.mid ? '<span class="qtag mid">已插入当前回复</span>' : '') + `<div class="utext">${esc(it.text)}</div>` +
        (Array.isArray(it.imgs) ? thumbs(it.imgs) : (it.imgs ? `<div><span class="chip">图片 ×${it.imgs}</span></div>` : ''));
      if (nested) el.innerHTML = esc(it.text.slice(0, 600));
      break;
    case 'thought': {
      const t = (it.text || '').trim();
      el.className = 'it';
      el.innerHTML = `<div class="thought${t ? ' has' : ''}">思考了 ${it.secs} 秒</div>` + (t ? `<div class="tbody md" hidden>${md(t)}</div>` : '');
      if (t) el.firstChild.onclick = () => { const p = el.querySelector('.tbody'); p.hidden = !p.hidden; el.firstChild.classList.toggle('open', !p.hidden); };
      break;
    }
    case 'text':
      el.className = 'it textblock';
      el.innerHTML = `<div class="md">${md(it.text)}</div>`;
      enhance(el);
      el.appendChild(cpBtn(() => it.text));
      break;
    case 'sys': {
      /* CLI 自动插入的消息（后台任务通知、子代理交回结果）：不是你发的，显示成一行提示，点击看原文 */
      el.className = 'it sysn';
      el.innerHTML = `<div class="sysh${it.body ? ' has' : ''}"><span class="chip">${esc(it.tag || '系统消息')}</span><span class="syst">${esc(it.text)}</span>` +
        `<span class="ustat">${nested ? '' : esc(statText(it.stat))}</span></div>`;
      if (it.body) el.firstChild.onclick = () => {
        let p = el.querySelector('.tbody');
        if (!p) { p = document.createElement('div'); p.className = 'tbody md'; p.hidden = true; p.innerHTML = md(it.body); enhance(p); el.appendChild(p); }
        p.hidden = !p.hidden; el.classList.toggle('open', !p.hidden);
      };
      break;
    }
    case 'error': {
      const t = String(it.text || '').replace(/\s+/g, ' ').trim();
      el.className = 'it err';
      el.innerHTML = (it.etype ? `<span class="chip etype">${esc(it.etype)}</span>` : '') + `<div class="errtext">${esc(t.length > 300 ? t.slice(0, 300) + '…' : t)}</div>`;
      if (t.length > 300) el.title = t.slice(0, 4000);
      break;
    }
    case 'cmd': el.className = 'it'; el.innerHTML = `<span class="chip mono">${esc(it.text)}</span>`; break;
    case 'note': el.className = 'it'; el.innerHTML = `<div class="note">${esc(it.text)}</div>`; break;
    case 'tool': renderTool(el, it, nested); break;
    default: el.className = 'it';
  }
  return el;
}
function addMore(root) {
  const users = root.classList && root.classList.contains('user') ? [root] : [...root.querySelectorAll('.user')];
  users.forEach(u => {
    if (u.dataset.m || u.scrollHeight <= 320) return;
    u.dataset.m = 1; u.classList.add('long');
    const b = document.createElement('button'); b.type = 'button'; b.className = 'more ufold'; b.textContent = '展开全文';
    b.onclick = () => { const o = u.classList.toggle('open'); b.textContent = o ? '收起' : '展开全文'; };
    u.after(b);
  });
  root.querySelectorAll('.box').forEach(box => {
    if (box.dataset.m) return;
    const long = [...box.querySelectorAll('.row pre')].some(p => p.scrollHeight > p.clientHeight + 4);
    if (!long) return;
    box.dataset.m = 1; box.classList.add('long');
    const top = document.createElement('button'); top.type = 'button'; top.textContent = '展开';
    const bottom = document.createElement('button'); bottom.type = 'button'; bottom.className = 'more'; bottom.textContent = '收起'; bottom.hidden = true;
    const set = open => {
      box.classList.toggle('open', open); top.textContent = open ? '收起' : '展开'; bottom.hidden = !open;
      if (!open) { const r = box.getBoundingClientRect(); if (r.top < 0) box.scrollIntoView({block: 'nearest'}); }
    };
    top.onclick = e => { e.stopPropagation(); set(!box.classList.contains('open')); };
    bottom.onclick = e => { e.stopPropagation(); set(false); };
    box.querySelector('.bt').prepend(top);
    box.appendChild(bottom);
  });
}
function nearBottom() { const s = $('#scroll'); return !S.userUp && s.scrollHeight - s.scrollTop - s.clientHeight < 160; }
function toBottom() { const s = $('#scroll'); S.userUp = false; s.scrollTop = s.scrollHeight; lastTop = s.scrollTop; updateJump(); }
/* ---- 每一轮的结束状态：已完成 / 已中断 / 出错 / 未完成，附用时和输出 ---- */
const isTurnStart = it => (it.k === 'user' && !it.mid) || (it.k === 'sys' && it.turn);
function turnInfo(t) {
  const stalled = t.tl && t.tl.querySelector('.it.tool.run');
  const word = t.flag === 'err' ? '出错' : t.flag === 'int' ? '已中断' : stalled ? '未完成' : '已完成';
  const cls = t.flag === 'err' ? 'err' : t.flag === 'int' ? 'int' : stalled ? 'stall' : 'ok';
  return {word, cls, glyph: cls === 'ok' ? '✓' : cls === 'err' ? '!' : cls === 'int' ? '■' : '…', stat: statText(t.it.stat)};
}
function forkBtn(t) {
  /* 已答完的回合（✓）才能从这里分叉：新对话只带到这一轮为止 */
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'link fk'; b.textContent = '从这里分叉'; b.dataset.u = t.it.fork;
  b.onclick = e => { e.stopPropagation(); forkAt(b.dataset.u); };
  return b;
}
function fillTurnEnd(el, t) {
  const x = turnInfo(t);
  el.className = 'tend ' + x.cls;
  el.innerHTML = `<span class="tg">${x.glyph}</span><span class="tw">${x.word}</span>` + (x.stat ? `<span class="ts">${esc(x.stat)}</span>` : '');
  if (x.cls === 'ok' && t.it.fork) el.appendChild(forkBtn(t));
}
async function forkAt(u) {
  if (!S.cur || !u) return;
  try {
    const d = await api('fork-at', {sid: S.cur, uuid: u});
    load(d); store.set('last', d.sid); refreshSide(true); toast('已分叉到新对话');
  } catch (e) { toast(e.message); }
}
/* 第十四轮 FH：结束行放在这一轮时间线（.tl）的后面、和时间线同一层，和最新一轮的“已完成”（S.spinEl）位置一致；
   原来放在 .tl 里面，过去的轮次带竖线并缩进，最新一轮不带，下一轮开始时还会跳一下 */
function endTurn() {
  const t = S.turn; S.turn = null;
  if (!t || (!t.tl && !t.it.stat)) return;
  const el = document.createElement('div'); fillTurnEnd(el, t); t.el = el;
  if (t.tl && t.tl.parentNode) { t.tl.after(el); if (S.tl === t.tl) S.tl = null; }  // 下一轮开头的系统消息另起一段时间线，排在结束行后面
  else $('#feed').appendChild(el);
}
function addItem(it, batch) {
  if (S.byId.has(it.id)) { patchItem(it); return; }
  S.byId.set(it.id, it);
  const feed = $('#feed');
  if (isTurnStart(it)) { endTurn(); S.turn = {it, tl: null, flag: '', el: null}; it._turn = S.turn; }
  if (it.k === 'cmd') { const i = S.pendings.findIndex(p => (p._text || '').startsWith('/')); if (i >= 0) { S.pendings[i].remove(); S.pendings.splice(i, 1); } }
  const el = renderItem(it);
  if (it.k === 'user') {
    const pu = S.pendings.shift(); if (pu) pu.remove();
    S.liveTxt = ''; if (S.liveEl) { S.liveEl.remove(); S.liveEl = null; }
    feed.appendChild(el); S.tl = null;
    for (const p of S.pendings) feed.appendChild(p);
  } else {
    if (!S.tl) { S.tl = document.createElement('div'); S.tl.className = 'tl'; feed.appendChild(S.tl); }
    S.tl.appendChild(el);
    if (S.turn) {
      if (!isTurnStart(it)) S.turn.tl = S.tl;
      if (it.k === 'error') S.turn.flag = 'err';
      else if (it.k === 'note' && it.text === '已中断') S.turn.flag = 'int';
    }
    // 工具调用或系统消息写进来时，前面那段正文一定已经落地，旧预览直接收掉
    if (it.k === 'sys' || it.k === 'tool' || (it.k === 'text' && !(S.runner && S.runner.busy))) { S.liveTxt = ''; if (S.liveEl) { S.liveEl.remove(); S.liveEl = null; } }
    if (S.liveEl && S.liveEl.parentNode === S.tl) S.tl.appendChild(S.liveEl);
    for (const p of S.pendings) feed.appendChild(p);
    if (it.k === 'text' && S.liveEl) renderLive(S.runner);
  }
  if (S.spinEl) feed.appendChild(S.spinEl);
  if (S.exitEl && S.exitEl.parentNode === feed) feed.appendChild(S.exitEl);
  if (!batch) { addMore(el); afterChange(it); }
}
/* ---- 长会话分页：“显示更早”每次往前取一页，插在顶部，滚动位置不跳 ---- */
const PAGE = 300;
function olderNote() {
  const feed = $('#feed');
  let n = feed.querySelector(':scope > .hidden-note');
  if (!S.hidden) { if (n) n.remove(); return; }
  if (!n) {
    n = document.createElement('div'); n.className = 'hidden-note'; n.innerHTML = '<button type="button"></button>';
    n.firstChild.onclick = () => loadOlder();
    feed.prepend(n);
  }
  const k = Math.min(PAGE, S.hidden);
  n.firstChild.textContent = k < S.hidden ? `显示更早的 ${k} 条（共 ${S.hidden} 条未显示）` : `显示更早的 ${k} 条`;
}
let olderTask = null;
function loadOlder() {
  if (olderTask) return olderTask;
  if (!S.cur || !S.hidden) return Promise.resolve(false);
  const sid = S.cur, first = S.byId.keys().next().value;
  const btn = $('#feed').querySelector(':scope > .hidden-note button');
  if (btn) { btn.disabled = true; btn.textContent = '正在加载…'; }
  olderTask = (async () => {
    try {
      const d = await api('page', {sid, before: first, n: PAGE});
      if (S.cur !== sid) return false;
      prependItems(d.items || [], d.hidden || 0);
      return true;
    } catch (e) { if (S.cur === sid) { toast(e.message); olderNote(); } return false; }
    finally { olderTask = null; const b = $('#feed').querySelector(':scope > .hidden-note button'); if (b) b.disabled = false; }
  })();
  return olderTask;
}
/* 一直往前加载，直到 found() 找到目标或没有更早的了 */
async function loadUntil(found) {
  const sid = S.cur;
  let x = found();
  while (!x && S.hidden && S.cur === sid) {
    if (!await loadOlder()) break;
    x = found();
  }
  return x;
}
function markFlag(turn, it) { if (it.k === 'error') turn.flag = 'err'; else if (it.k === 'note' && it.text === '已中断') turn.flag = 'int'; }
function prependItems(items, hidden) {
  const feed = $('#feed'), sc = $('#scroll');
  const note = feed.querySelector(':scope > .hidden-note');
  const first = note ? note.nextElementSibling : feed.firstElementChild;
  /* 以视口里第一条可见的条目为锚：插入后它在屏幕上的位置不变（不能按总高度补，上一轮的结束行可能插在它下面） */
  const vt = sc.getBoundingClientRect().top;
  const anchorEl = [...feed.querySelectorAll(':scope > [data-id], :scope > .tl > [data-id]')].find(x => x.getBoundingClientRect().bottom > vt);
  const a0 = anchorEl ? anchorEl.getBoundingClientRect().top : 0;
  const exist = [...S.byId.values()];
  const fresh = items.filter(it => it && it.id && !S.byId.has(it.id));
  /* 按 addItem 同样的规则把这一页画成“提问 + 回复块”，最后一轮可能延续到原来顶部那段没有提问的回复 */
  const wrap = document.createElement('div');
  let tl = null, turn = null;
  const endT = () => {
    if (turn && (turn.tl || turn.it.stat)) {
      const el = document.createElement('div'); fillTurnEnd(el, turn); turn.el = el;
      if (turn.tl) { turn.tl.after(el); if (tl === turn.tl) tl = null; } else wrap.appendChild(el);  // 同 endTurn：结束行在时间线后面
    }
    turn = null;
  };
  for (const it of fresh) {
    if (isTurnStart(it)) { endT(); turn = {it, tl: null, flag: '', el: null}; it._turn = turn; }
    const el = renderItem(it);
    if (it.k === 'user') { wrap.appendChild(el); tl = null; continue; }
    if (!tl) { tl = document.createElement('div'); tl.className = 'tl'; wrap.appendChild(tl); }
    tl.appendChild(el);
    if (turn) { if (!isTurnStart(it)) turn.tl = tl; markFlag(turn, it); }
  }
  const lead = first && first.classList.contains('tl') ? first : null;  // 原来顶部那段没有提问的回复
  if (lead && tl) { lead.prepend(...tl.childNodes); tl.remove(); if (turn && (turn.tl === tl || !turn.tl)) turn.tl = lead; tl = lead; }
  const nodes = [...wrap.childNodes];
  for (const n of nodes) feed.insertBefore(n, first);
  if (turn) {
    /* 这一页的最后一轮延续到原来加载的、第一个回合开头之前的那些条目 */
    const si = exist.findIndex(isTurnStart);
    const leadItems = si < 0 ? exist : exist.slice(0, si);
    leadItems.forEach(it => markFlag(turn, it));
    const more = leadItems.some(it => it.k !== 'user');
    if (si < 0) {
      if (more) turn.tl = S.tl || turn.tl;
      S.turn = turn;  // 原来加载的全在这一轮里：它就是当前这一轮
    } else {
      const se = feed.querySelector(`[data-id="${CSS.escape(exist[si].id)}"]`);
      let host = null;
      if (se && se.parentNode !== feed) host = se.parentNode;
      else if (se) { const pv = se.previousElementSibling; if (pv && pv.classList.contains('tl')) host = pv; }
      if (host && (turn.tl || more)) turn.tl = host;
      if (se && (turn.tl || turn.it.stat)) {
        const el = document.createElement('div'); fillTurnEnd(el, turn); turn.el = el;
        if (host && se.parentNode === host) {
          /* 下一轮从时间线中间的系统消息开始：从它那里把时间线拆成两段，结束行放在两段之间（同 endTurn） */
          const nt = document.createElement('div'); nt.className = 'tl';
          for (let n = se; n;) { const nx = n.nextSibling; nt.appendChild(n); n = nx; }
          host.after(el); el.after(nt);
          for (const it of exist.slice(si)) if (it._turn && it._turn.tl === host) it._turn.tl = nt;
          if (S.tl === host) S.tl = nt;
        } else feed.insertBefore(el, se);
      }
    }
  }
  S.byId = new Map([...fresh.map(it => [it.id, it]), ...S.byId]);
  S.hidden = hidden;
  olderNote();
  hook('Prepend', nodes);  // 第十二轮 D：新加载的已结束轮次在补偿滚动前就折好（ux-d.js）
  nodes.forEach(n => n.nodeType === 1 && addMore(n)); if (lead) addMore(lead);
  if (anchorEl) sc.scrollTop += anchorEl.getBoundingClientRect().top - a0;
  lastTop = sc.scrollTop;
  renderTopBtns(); renderTodo(); renderSpin();
}
function patchItem(p) {
  const it = S.byId.get(p.id);
  if (!it) return;
  Object.assign(it, p);
  const old = $('#feed').querySelector(`[data-id="${CSS.escape(p.id)}"]`);
  if (!old) return;
  if (it.k === 'user' || it.k === 'sys') {
    const s = old.querySelector('.ustat'); if (s) s.textContent = statText(it.stat);
    if (it._turn && it._turn.el) fillTurnEnd(it._turn.el, it._turn);
    if ('fork' in p && it._turn === S.turn) renderSpin();
    return;
  }
  const el = renderItem(it);
  old.replaceWith(el);
  addMore(el);
  afterChange(it);
}
function afterChange(it) {
  if (it.k === 'tool') { if (it.todos) renderTodo(); if (['Edit', 'Write', 'NotebookEdit'].includes(it.name)) renderTopBtns(); }
  if (it.k === 'user') renderTopBtns();
}

/* ---- 任务清单（固定在顶部）---- */
function latestTodos() {
  let t = null;
  for (const it of S.byId.values()) if (it.k === 'tool' && it.todos) t = it.todos;
  return t;
}
function renderTodo() {
  const bar = $('#todoBar'), todos = latestTodos();
  if (!todos || !todos.length) { bar.hidden = true; return; }
  const done = todos.filter(t => t.s === 'completed').length, cur = todos.find(t => t.s === 'in_progress');
  bar.hidden = false;
  bar.innerHTML = `<div class="tsum"><b>任务 ${done}/${todos.length}</b><span>${esc(cur ? '进行中：' + cur.c : done === todos.length ? '全部完成' : '')}</span><i class="tcaret"></i></div>` +
    `<ul class="todos">${todos.map(t => `<li class="${esc(t.s)}">${esc(t.c)}</li>`).join('')}</ul>`;
  bar.classList.toggle('open', !!store.get('todoOpen', false));
  bar.querySelector('.tsum').onclick = () => { const o = bar.classList.toggle('open'); store.set('todoOpen', o); };
}

/* ---- 回到底部 ---- */
function updateJump() { $('#jump').hidden = !S.cur || nearBottom(); }
let lastTop = 0;
$('#scroll').addEventListener('scroll', () => {
  const s = $('#scroll'), dist = s.scrollHeight - s.scrollTop - s.clientHeight;
  if (dist < 24) S.userUp = false; else if (s.scrollTop < lastTop - 2) S.userUp = true;
  lastTop = s.scrollTop; updateJump();
});
$('#scroll').addEventListener('wheel', e => { if (e.deltaY < 0) S.userUp = true; }, {passive: true});
$('#jump').onclick = () => { $('#scroll').scrollTo({top: $('#scroll').scrollHeight, behavior: 'smooth'}); };
/* 输入区高度变化（换行、右栏开合）时，回到底部按钮跟着上移，不压住输入框 */
if (window.ResizeObserver) new ResizeObserver(() => { $('#jump').style.bottom = ($('#dock').offsetHeight + 14) + 'px'; }).observe($('#dock'));

/* ---- 顶部：目录 / 改动 / 导出 ---- */
function userItems() { return [...S.byId.values()].filter(i => i.k === 'user'); }
function changedFiles() {
  const m = new Map();
  const add = p => m.set(p, (m.get(p) || 0) + 1);
  for (const c of S.oldChg || []) if (!S.byId.has(c.id)) add(c.path);
  for (const it of S.byId.values()) if (it.k === 'tool' && it.path && ['Edit', 'Write', 'NotebookEdit'].includes(it.name) && !it.error) add(it.path);
  return [...m.entries()];
}
/* 目录按完整历史列：还没加载的提问由后台给出，点到时再往前加载到那里 */
function tocItems() { return [...(S.oldToc || []).filter(u => !S.byId.has(u.id)), ...userItems()]; }
/* 11-11：“改动”角标只算没看过的文件——点开改动列表就清零，之后有新文件被改才再出现。
   已看过的文件按对话记在本地；第一次打开某个对话时，已有的改动都算看过 */
const chgKey = () => 'chgSeen.' + S.cur;
function chgSeen(files) {
  if (!S.cur || S.loading) return null;
  let v = store.get(chgKey(), null);
  if (v == null) { v = files.map(f => f[0]).slice(-500); store.set(chgKey(), v); }
  return new Set(v);
}
function renderTopBtns() {
  const files = changedFiles(), seen = chgSeen(files);
  const n = seen ? files.filter(([p]) => !seen.has(p)).length : 0;
  const cb = $('#chgBtn'); cb.dataset.n = n || '';  // 第十四轮 H：图标按钮只更新角标
  if (!cb.dataset.ic) cb.textContent = n ? `改动 ${n}` : '改动';
  $('#tocBtn').disabled = !tocItems().length;
}
$('#tocBtn').onclick = e => {
  const us = tocItems();
  menu(e.currentTarget, us.map((u, i) => ({label: `${i + 1}. ${(u.text || '').replace(/\s+/g, ' ').slice(0, 60) || '（图片）'}`, sub: statText(u.stat),
    fn: async () => { if (!S.byId.has(u.id)) await loadUntil(() => S.byId.has(u.id)); jumpTo(u.id); }})), {wide: true, alignRight: true, empty: '还没有提问'});
};
$('#chgBtn').onclick = e => {
  const files = changedFiles(), seen = chgSeen(files) || new Set(files.map(f => f[0]));
  menu(e.currentTarget, files.map(([p, n]) => ({label: shortPath(p), sub: [seen.has(p) ? '' : '新', n > 1 ? `改了 ${n} 次` : ''].filter(Boolean).join(' · '),
    title: '⌘ 点击在访达中显示', fn: ev => openPath(p, ev.metaKey || ev.ctrlKey)})), {wide: true, alignRight: true, empty: '这个对话还没改过文件'});
  if (S.cur) { store.set(chgKey(), files.map(f => f[0]).slice(-500)); renderTopBtns(); }  // 看过了，角标清零
};
$('#expBtn').onclick = e => {
  menu(e.currentTarget, [{label: '导出为 Markdown', fn: exportMd}, {label: '导出为网页', fn: exportHtml}], {alignRight: true});
};
function jumpTo(id) {
  const el = $('#feed').querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (!el) return;
  el.scrollIntoView({block: 'start', behavior: 'smooth'});
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
}
function download(name, blob) {
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name.replace(/[\\/:*?"<>|]/g, '_');
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
async function loadAll() {
  if (!S.hidden) return;
  toast('正在载入完整记录…');
  await loadUntil(() => false);
}
async function exportMd() {
  await loadAll();
  const out = [`# ${S.title}`, '', `项目：${S.cwd}`, ''];
  for (const it of S.byId.values()) {
    if (it.k === 'user') out.push('---', '', '**你：**', '', it.text + (it.imgs && it.imgs.length ? `\n\n（附图 ${it.imgs.length} 张）` : ''), '');
    else if (it.k === 'text') out.push(it.text, '');
    else if (it.k === 'sys') out.push('> ' + it.text, '');
    else if (it.k === 'tool') {
      out.push(`> **${it.name}** ${it.path || it.desc || ''}`.trim());
      if (it.in && !it.diff) out.push('', '```', it.in.slice(0, 1500), '```');
      out.push('');
    } else if (it.k === 'error') out.push(`> 错误：${it.text}`, '');
  }
  download(`${S.title}.md`, new Blob([out.join('\n')], {type: 'text/markdown'}));
}
async function exportHtml() {
  await loadAll();
  toast('正在导出…');
  const css = (await Promise.all(['/static/app.css', '/static/theme.css', '/static/ux-d.css'].map(u => fetch(u).then(r => r.ok ? r.text() : '').catch(() => '')))).join('\n');  // 第十二轮 D：加 ux-d.css
  const feed = $('#feed').cloneNode(true);
  feed.querySelectorAll('.cp, .code-hd, .bt, .more, .hidden-note, .subbtn, .pending').forEach(x => x.remove());
  hook('Export', feed);  // 第十二轮 D：导出含完整过程（去掉折叠行和操作条）
  feed.querySelectorAll('.box').forEach(b => b.classList.add('open'));
  feed.querySelectorAll('.user.long').forEach(u => u.classList.add('open'));
  for (const img of feed.querySelectorAll('img')) {
    try {
      const b = await fetch(img.src).then(r => r.blob());
      img.src = await new Promise(ok => { const fr = new FileReader(); fr.onload = () => ok(fr.result); fr.readAsDataURL(b); });
      img.removeAttribute('loading');
    } catch { img.remove(); }
  }
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${esc(S.title)}</title><style>${css}\nbody{overflow:auto}#feed{padding-top:24px}</style></head>` +
    `<body><div id="feed"><h2 style="color:var(--strong)">${esc(S.title)}</h2><div class="note">${esc(S.cwd)}</div>${feed.innerHTML}</div></body></html>`;
  download(`${S.title}.html`, new Blob([html], {type: 'text/html'}));
}

/* ================= 左栏 ================= */
function fmtTime(t) {
  const d = new Date(t * 1000), n = new Date();
  const p = x => String(x).padStart(2, '0');
  if (d.toDateString() === n.toDateString()) return `${p(d.getHours())}:${p(d.getMinutes())}`;
  if (d.getFullYear() === n.getFullYear()) return `${d.getMonth() + 1}-${p(d.getDate())}`;
  return `${d.getFullYear()}-${d.getMonth() + 1}-${p(d.getDate())}`;
}
function toggleProject(cwd, open) {
  const on = open ?? !S.expanded.has(cwd);
  if (on) S.expanded.add(cwd); else S.expanded.delete(cwd);
  store.set('expanded', [...S.expanded]);
}
function sessionRow(s, showProject) {
  const a = document.createElement('div');
  a.className = 'srow' + (s.sid === S.cur ? ' active' : '') + (s.archived ? ' arch' : '') + (window.CWB && CWB.isSel && CWB.isSel(s.sid) ? ' sel' : '');  // 第十一轮 B：多选
  a.title = s.title; a.dataset.sid = s.sid;
  const dot = s.attention ? 'att' : s.busy ? 'busy' : s.running ? 'on' : '';
  a.innerHTML = `<span class="sdot ${dot}"></span><span class="stitle">${s.pinned ? '<i class="pin">⬆</i>' : ''}${esc(s.title)}${s.wt ? '<span class="wtag">工作树</span>' : ''}` +
    (showProject ? `<span class="phint">${esc(projName(s.sid))}</span>` : '') + `</span><span class="stime">${fmtTime(s.mtime)}</span>` +
    `<button class="smore" title="更多">⋯</button>`;
  a.onclick = e => { if (e.target.closest('.smore')) { e.stopPropagation(); sessionMenu(e.target, s); return; } if (window.CWB && CWB.sideClick && CWB.sideClick(e, s)) return; openSession(s.sid); };
  a.oncontextmenu = e => { e.preventDefault(); sessionMenu({x: e.clientX, y: e.clientY}, s); };
  return a;
}
function sessionMenu(anchor, s) {
  menu(anchor, [
    {label: '改名', fn: () => renameSession(s)},
    {label: s.pinned ? '取消置顶' : '置顶', fn: () => api('pin', {sid: s.sid, on: !s.pinned}).then(() => refreshSide(true))},
    {label: s.archived ? '取消归档' : '归档', fn: () => archive(s, !s.archived)},
    ...((window.CWB && CWB.sessionMenuExtra) ? CWB.sessionMenuExtra(s) : []),
    {sep: true},
    {label: '复制会话 ID', fn: () => copy(s.sid).then(() => toast('已复制会话 ID'))},
    {label: '复制记录文件路径', fn: () => api('session-path', {sid: s.sid}).then(d => copy(d.path)).then(() => toast('已复制记录文件路径')).catch(e => toast(e.message))},
    {sep: true},
    {label: '删除', danger: true, fn: () => removeSession(s)},
  ]);
}
function projectMenu(anchor, p) {
  menu(anchor, [
    {label: '新对话', fn: () => newSession(p.cwd)},
    ...((window.CWB && CWB.projectMenuExtra) ? CWB.projectMenuExtra(p) : []),
    {label: '重命名…', fn: () => renameProject(p)},  // 11-11：访达里的文件夹一起改名
    {label: '在访达中打开', fn: () => api('reveal', {path: p.cwd})},
    {label: p.hidden ? '取消归档' : '归档项目', fn: () => api('hide-project', {cwd: p.cwd, on: !p.hidden})
      .then(() => { toast(p.hidden ? '已取消归档' : '已归档，在侧栏底部“已归档”里'); refreshSide(true); })},
    ...(p.hidden ? [{sep: true}, {label: '删除项目', danger: true, fn: () => removeProject(p)}] : []),  // 11-23：只在已归档的项目上
  ]);
}
async function removeProject(p) {
  if (window.CWB && CWB.undoDeleteProject) return CWB.undoDeleteProject(p);  // 10 秒内可撤销（ux-f.js）
  if (!confirm(`删除项目「${p.name}」？\n里面的对话记录会移到废纸篓，访达里的文件夹不会动。`)) return;
  try { await api('delete-project', {cwd: p.cwd}); toast('已删除项目，对话记录在废纸篓'); refreshSide(true); } catch (e) { toast(e.message); }
}
async function renameProject(p) {
  const v = await askText('项目名称（访达里的文件夹会一起改名）', p.name);
  const name = (v || '').trim();
  if (!name || name === p.name) return;
  try {
    const d = await api('rename-project', {cwd: p.cwd, name});
    const fix = x => (x && (x === d.old || x.startsWith(d.old + '/'))) ? d.cwd + x.slice(d.old.length) : x;
    if (S.cwd) { S.cwd = fix(S.cwd); renderTop(); }
    for (const k of [...S.expanded]) if (fix(k) !== k) { S.expanded.delete(k); S.expanded.add(fix(k)); }
    store.set('expanded', [...S.expanded]);
    toast(d.linked ? `已关联到访达里的「${name}」，原来的对话都在` : `已改名为「${name}」，访达里的文件夹也一起改了`);
    refreshSide(true);
  } catch (e) { toast(e.message); }
}
async function renameSession(s) {
  const v = await askText('对话名称', s.title);
  if (v == null) return;
  try { await api('rename', {sid: s.sid, name: v}); if (s.sid === S.cur) { S.title = v.trim() || S.title; renderTop(); } refreshSide(true); } catch (e) { toast(e.message); }
}
function projName(sid) { for (const p of S.projects) if (p.sessions.some(s => s.sid === sid)) return p.name; return ''; }
function projectGroup(p, ss, f, nameCount) {
  const key = (p.archKey ? 'arch:' : '') + p.cwd;  // 11-11：“已归档”区里列出的项目，展开状态和主列表分开记
  const open = !!f || S.expanded.has(key);
  const g = document.createElement('div');
  g.className = 'proj' + (open ? ' open' : '');
  const parent = p.cwd.split('/').slice(-2, -1)[0] || '';
  const hint = nameCount[p.name] > 1 && parent ? `<span class="phint">${esc(parent)}</span>` : '';
  g.innerHTML = `<div class="prow" title="${esc(p.cwd)}"><span class="caret"></span><span class="pname">${esc(p.name)}${hint}</span>` +
    `<button class="pbtn" data-a="more" title="更多">⋯</button><button class="pbtn" data-a="new" title="新对话">＋</button></div>`;
  g.querySelector('.prow').onclick = e => {
    const a = e.target.dataset.a;
    if (a === 'new') return newSession(p.cwd);
    if (a === 'more') return projectMenu(e.target, p);
    toggleProject(key); renderSide();
  };
  g.querySelector('.prow').oncontextmenu = e => { e.preventDefault(); projectMenu({x: e.clientX, y: e.clientY}, p); };
  if (open) {
    const list = document.createElement('div'); list.className = 'slist';
    if (!ss.length) list.innerHTML = '<div class="pempty">暂无对话</div>';
    // 第十一轮 B：项目内按“需要处理 / 进行中 / 今天 / 昨天 / 更早”分组（分组规则在 feat.js），空组不显示
    for (const [label, items] of (window.CWB && CWB.groupSessions ? CWB.groupSessions(ss) : [['', ss]])) {
      if (label === '更早' && !S.filter.trim() && !(window.CWB && CWB.sideFilter)) {  // “更早”默认收起，点组标题展开；收起时只留当前打开的那条（第十五轮 F2：按状态筛选时也展开）
        const ok = 'oldOpen.' + key, on = !!store.get(ok);
        const h = Object.assign(document.createElement('div'), {className: 'sgroup fold' + (on ? ' open' : '')});
        h.innerHTML = '<span class="fc"></span>更早<span class="fn">' + items.length + '</span>';
        h.onclick = () => { store.set(ok, on ? null : 1); renderSide(); };
        list.appendChild(h);
        (on ? items : items.filter(s => s.sid === S.cur)).forEach(s => list.appendChild(sessionRow(s)));
        continue;
      }
      if (label) list.appendChild(Object.assign(document.createElement('div'), {className: 'sgroup', textContent: label}));
      items.forEach(s => list.appendChild(sessionRow(s)));
    }
    g.appendChild(list);
  }
  return g;
}
function renderSide() {
  hook('PreSide');  // 第十三轮 F：等着删除的对话先不显示、归档状态以本地为准（ux-f.js）
  const f = S.filter.trim().toLowerCase();
  const box = $('#projects');
  const top = box.scrollTop;
  box.innerHTML = '';
  let archCount = 0;
  const nameCount = {}, pinned = [];
  for (const p of S.projects) nameCount[p.name] = (nameCount[p.name] || 0) + 1;
  /* 11-11：归档的项目和归档的对话都放到底部“已归档”区（原来的“其他”分组和“已归档”开关不再用） */
  const sf = window.CWB && CWB.sideFilter;  // 第十五轮 F2：左栏状态筛选（进行中 / 等我确认 / 未读，ux-l.js），只影响显示
  const match = s => (!f || s.title.toLowerCase().includes(f)) && (!sf || sf(s));
  const visible = s => !s.archived && match(s);
  for (const p of S.projects) for (const s of p.sessions) { if (s.archived && !p.hidden) archCount++; if (s.pinned && !p.hidden && visible(s)) pinned.push(s); }
  if (pinned.length) {
    pinned.sort((a, b) => a.pinned - b.pinned);
    const g = document.createElement('div'); g.className = 'group';
    g.innerHTML = '<div class="ghead">置顶</div>';
    pinned.forEach(s => g.appendChild(sessionRow(s, true)));
    box.appendChild(g);
  }
  const archived = [];
  for (const p of S.projects) {
    if (p.hidden) {  // 归档的项目：整组进“已归档”
      archCount++;
      const ss = p.sessions.filter(match);
      if ((!f && !sf) || ss.length || (!sf && p.name.toLowerCase().includes(f))) archived.push([p, ss]);
      continue;
    }
    const arch = p.sessions.filter(s => s.archived && match(s));
    if (arch.length) archived.push([Object.assign({}, p, {archKey: true}), arch]);  // 没归档的项目里归档的对话
    const ss = p.sessions.filter(s => !s.pinned && visible(s));
    if ((f || sf) && !ss.length && (sf || !p.name.toLowerCase().includes(f))) continue;
    box.appendChild(projectGroup(p, ss, f || (sf ? ' ' : ''), nameCount));
  }
  if (archived.length) {
    const open = !!f || S.expanded.has('__arch__');
    const g = document.createElement('div'); g.className = 'group other arch' + (open ? ' open' : '');
    g.innerHTML = `<div class="ghead click"><span class="caret"></span>已归档（${archCount}）</div>`;
    g.firstChild.onclick = () => { toggleProject('__arch__'); renderSide(); };
    if (open) archived.forEach(([p, ss]) => g.appendChild(projectGroup(p, ss, f, nameCount)));
    box.appendChild(g);
  }
  if (f.length >= 2 && S.hits.length) {
    const g = document.createElement('div'); g.className = 'group';
    g.innerHTML = '<div class="ghead">内容匹配</div>';
    for (const h of S.hits) {
      const a = document.createElement('div'); a.className = 'hit' + (h.sid === S.cur ? ' active' : '');
      const i = h.snip.toLowerCase().indexOf(f);
      const snip = i < 0 ? esc(h.snip) : esc(h.snip.slice(0, i)) + '<mark>' + esc(h.snip.slice(i, i + f.length)) + '</mark>' + esc(h.snip.slice(i + f.length));
      a.innerHTML = `<div class="htitle"><span>${esc(h.title)}</span><span class="stime">${fmtTime(h.mtime)}</span></div><div class="hsnip">${snip}</div>`;
      a.onclick = () => openSession(h.sid, S.filter.trim());
      g.appendChild(a);
    }
    box.appendChild(g);
  }
  box.scrollTop = top;
  const sa = $('#showArch');
  sa.hidden = true;  // 11-11：归档的内容统一在“已归档”区里看
  hook('Side');  // 第十一轮 B：多选操作条
}
$('#showArch').onclick = () => { S.showArch = !S.showArch; store.set('showArch', S.showArch); renderSide(); };
async function archive(s, on) {
  if (window.CWB && CWB.undoArchive) return CWB.undoArchive([s], on);  // 第十三轮 F：立即生效，toast 里可撤销（ux-f.js）
  try { await api('archive', {sid: s.sid, on}); s.archived = on; renderSide(); toast(on ? '已归档' : '已恢复'); } catch (e) { toast(e.message); }
}
async function removeSession(s) {
  if (window.CWB && CWB.undoDelete) return CWB.undoDelete([s]);  // 第十三轮 F：不再确认，10 秒内可撤销（ux-f.js）
  if (!confirm(`删除对话「${s.title}」？\n会话记录会移到废纸篓，需要时可以从废纸篓找回。`)) return;
  try {
    await api('delete', {sid: s.sid});
    for (const p of S.projects) p.sessions = p.sessions.filter(x => x.sid !== s.sid);
    if (S.cur === s.sid) clearMain();
    renderSide(); toast('已移到废纸篓');
  } catch (e) { toast(e.message); }
}
function clearMain() {
  S.cur = null; S.byId = new Map(); $('#feed').innerHTML = ''; document.body.classList.add('nosess'); store.set('last', null); $('#todoBar').hidden = true; updateJump();
}
let sideTimer = null;
function refreshSide(now) {
  clearTimeout(sideTimer);
  sideTimer = setTimeout(async () => {
    try {
      const d = await api('projects');
      if (S.boot && d.boot !== S.boot) { location.reload(); return; }
      S.boot = d.boot; S.projects = d.projects; S.home = d.home;
      if (d.models) { S.models = d.models; buildModelOptions(); }
      if (d.status && !S.status) { S.status = d.status; renderStatus(); }
      renderSide();
      if (S.cur) { const s = findSession(S.cur); if (s && s.title !== S.title) { S.title = s.title; renderTop(); } }
    } catch (e) { /* 后端重启中，稍后重试 */ }
  }, now ? 0 : 500);
}
function findSession(sid) {
  for (const p of S.projects) for (const s of p.sessions) if (s.sid === sid) return s;
  return null;
}
let searchTimer = null;
$('#filter').addEventListener('input', e => {
  S.filter = e.target.value; renderSide();
  clearTimeout(searchTimer);
  const q = S.filter.trim();
  if (q.length < 2) { S.hits = []; return; }
  searchTimer = setTimeout(async () => {
    try { const d = await api('search', {q}); if (S.filter.trim() === q) { S.hits = d.results.filter(h => !S.projects.some(p => p.hidden && p.cwd === h.cwd) || true); renderSide(); } } catch {}
  }, 300);
});
$('#filter').addEventListener('keydown', e => { if (e.key === 'Escape') { e.target.value = ''; S.filter = ''; S.hits = []; renderSide(); input.focus(); } });

/* ================= 打开 / 新建对话 ================= */
function renderTop() {
  $('#title').textContent = S.title || '新对话';
  const full = S.cwd ? (S.home && S.cwd.startsWith(S.home) ? '~' + S.cwd.slice(S.home.length) : S.cwd) : '';
  const parts = full.split('/').filter(Boolean);
  const compact = parts.length > 2 ? parts.slice(-2).join('/') : full;
  const cwdEl = $('#cwd');
  cwdEl.textContent = compact;
  cwdEl.title = full ? '在访达中显示：' + full : '';
  document.title = (S.title || '新对话') + ' · Claude 工作台';
}
$('#title').ondblclick = () => { const s = findSession(S.cur); if (s) renameSession(s); };
$('#title').title = '双击改名';
function saveDraft() { if (S.cur) store.set('draft.' + S.cur, input.value.trim() ? input.value : null); }
function load(d, anchor) {
  hook('PreLoad', d);  // 第十四轮 H：从首页进入对话时先收好首页的草稿和附件（ux-h.js）
  const same = S.cur === d.sid;
  const keep = same ? {scroll: $('#scroll').scrollTop, bottom: nearBottom(), focus: document.activeElement === input} : null;
  if (S.cur && !same) { saveDraft(); S.attsBy[S.cur] = S.atts; S.atts = S.attsBy[d.sid] || []; delete S.attsBy[d.sid]; renderAtts(); }
  S.cur = d.sid; S.cwd = d.cwd; S.title = d.title; S.model = d.model; S.recent = d.recent; S.opts = d.opts || {};
  S.byId = new Map(); S.tl = null; S.turn = null; S.pendings = []; S.hist = -1; S.liveEl = null; S.spinEl = null; S.liveTxt = '';
  S.extOpen = !!d.open; S.extAt = (d.mtime || 0) * 1000;
  S.hidden = d.hidden || 0; S.oldToc = d.toc || []; S.oldChg = d.chg || []; S.exit = d.exit || null; S.exitEl = null;
  const feed = $('#feed');
  feed.innerHTML = '';
  olderNote();
  for (const it of d.items) addItem(it, true);
  for (const ev of S.buffer) applyItems(ev);
  S.buffer = [];
  document.body.classList.remove('nosess');
  if (!same) { feed.classList.remove('cwb-in'); void feed.offsetWidth; feed.classList.add('cwb-in'); }
  if (!S.expanded.has(S.cwd)) toggleProject(S.cwd, true);
  if (!same) { input.value = store.get('draft.' + d.sid, '') || ''; autosize(); }
  renderTop(); renderTopBtns(); renderTodo();
  updateRunner(d.runner);
  for (const p of d.perms || []) S.perms.set(p.rid, p);
  syncControls();
  const findAnchor = () => {
    if (!anchor) return null;
    if (!anchor.startsWith('q:')) return feed.querySelector(`[data-id="${CSS.escape(anchor)}"]`);
    const q = anchor.slice(2).toLowerCase();
    return [...feed.querySelectorAll('.user, .tl > .it')].find(x => x.textContent.toLowerCase().includes(q));
  };
  const goAnchor = a => { a.scrollIntoView({block: anchor.startsWith('q:') ? 'center' : 'start'}); if (anchor.startsWith('q:')) a.classList.add('flash'); };
  requestAnimationFrame(() => {
    addMore(feed);
    const a = findAnchor();
    if (a) goAnchor(a);
    else if (keep && !keep.bottom) $('#scroll').scrollTop = keep.scroll;
    else toBottom();
    updateJump();
    if (!a && anchor && S.hidden) loadUntil(findAnchor).then(x => { if (x && S.cur === d.sid) goAnchor(x); });
  });
  renderSide();
  showModal();
  if (!same || keep.focus) input.focus();
  hook('Load', d);
}
async function openSession(sid, q) {
  if (!sid) return;
  S.loading = sid; S.buffer = [];
  hook('Loading', sid);  // 第十四轮 H：加载慢时显示骨架占位（ux-h.js）
  try {
    const d = await api('open', {sid, q: q || '', n: S.cur === sid ? S.byId.size : 0});
    if (S.loading !== sid) return;
    S.loading = null;
    load(d, q ? 'q:' + q : null);
    store.set('last', sid);
  } catch (e) { if (S.loading === sid) { S.loading = null; hook('Loading', null); } toast(e.message); }
}
async function newSession(cwd) {
  if (!cwd) return;
  try {
    const d = await api('new', {cwd});
    toggleProject(cwd, true);
    S.freshSid = d.sid;  // 11-11：刚新建的空对话显示首页（ux-h.js）
    load(d);
    store.set('last', d.sid);
    refreshSide(true);
  } catch (e) { toast(e.message); }
}
$('#newHere').onclick = () => newSession(S.cwd);

/* ================= 运行状态 ================= */
const GLYPHS = ['·', '✢', '✳', '✶', '✻', '✽'];
let gi = 0;
setInterval(() => { if (S.spinEl && !S.spinEl.classList.contains('done')) S.spinEl.querySelector('.sg').textContent = GLYPHS[gi = (gi + 1) % GLYPHS.length]; }, 140);
/* ---- 实时预览：回复生成中，先显示后台终端屏幕上正在出的字；写进记录后换成正式排版 ---- */
const normTxt = t => (t || '').replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/```[\w+-]*/g, '').replace(/[\s*#>`_\-|•·:：\u2500-\u257f]/g, '');
function liveLanded(txt) {
  const head = normTxt(txt).slice(0, 40);
  if (!head) return true;
  const vals = [...S.byId.values()];
  for (let i = vals.length - 1; i >= 0; i--) {
    const it = vals[i];
    if (it.k === 'user' || it.k === 'sys') break;
    if (it.k === 'text' && normTxt(it.text).includes(head)) return true;
  }
  return false;
}
/* 屏幕上进行中的工具块有时只显示说明文字或路径，和本轮已出现的工具对上就不当正文 */
function toolish(txt) {
  const n = normTxt(txt), vals = [...S.byId.values()];
  for (let i = vals.length - 1, k = 0; i >= 0 && k < 60; i--, k++) {
    const it = vals[i];
    if (it.k === 'user' || it.k === 'sys') break;
    if (it.k !== 'tool') continue;
    const d = normTxt(it.desc), p = normTxt(it.path);
    if (d.length >= 4 && n.startsWith(d.slice(0, 30))) return true;
    if (p.length >= 4 && n.includes(p) && n.length < p.length + 16) return true;
  }
  return false;
}
/* 第十四轮 FH：实时预览也按 Markdown 排成段落、列表、标题，间距和写进记录后的正式正文一致，结束时不再整段往上收；
   原来整段放在一个 pre-wrap 的 <p> 里，段落之间的空行占满一整行（约 48px，正式排版约 31px）。
   屏幕上的单个换行照样换行（breaks），CLI 画的 • 列表符号换成 -，光标挂在最后一块的末尾 */
const LIVE_BLK = /^(P|UL|OL|LI|PRE|BLOCKQUOTE|TABLE|THEAD|TBODY|TR|TD|TH|H[1-6])$/;
function liveMd(box, txt) {
  let h = '';
  try { if (window.marked) h = marked.parse(txt.replace(/^(\s*)[•◦▪]\s+/gm, '$1- '), {breaks: true}); } catch {}
  if (!h) h = '<p>' + esc(txt).replace(/\n/g, '<br>') + '</p>';
  box.innerHTML = purify(h);
  let last = box.lastElementChild;
  while (last && last.lastElementChild && LIVE_BLK.test(last.lastElementChild.tagName)) last = last.lastElementChild;
  if (last && last.tagName === 'PRE') last = last.querySelector('code') || last;
  if (last) last.classList.add('lv');
}
function renderLive(st) {
  /* CLI 停止转圈后要过一两秒才把正文写进记录：这段时间保留最后一次预览，等正式正文到了再换，避免中间空白。
     新的一段要连续出现两次才显示，屏幕重绘时一闪而过的内容不上界面 */
  if (S.liveSid !== S.cur) { S.liveTxt = ''; S.liveCand = null; S.liveSid = S.cur; if (S.liveEl) { S.liveEl.remove(); S.liveEl = null; } }
  let cur = st && st.alive && st.busy && (!st.sid || st.sid === S.cur) ? (st.live || '') : '';
  if (cur && toolish(cur)) cur = '';
  const key = normTxt(cur).slice(0, 24);
  if (cur && normTxt(S.liveTxt).slice(0, 24) !== key) {
    if (!S.liveCand || S.liveCand.key !== key) S.liveCand = {key, t: Date.now()};
    if (Date.now() - S.liveCand.t < 350) cur = '';
  }
  if (cur) { S.liveTxt = cur; S.liveAt = Date.now(); }
  else if (S.liveTxt && Date.now() - (S.liveAt || 0) > 30000) S.liveTxt = '';
  const txt = S.liveTxt || '';
  let el = S.liveEl;
  if (!txt || liveLanded(txt)) { S.liveTxt = ''; if (el) { el.remove(); S.liveEl = null; } return; }
  const stick = nearBottom();
  if (!el) { el = document.createElement('div'); el.className = 'it textblock live'; el.innerHTML = '<div class="md"></div>'; S.liveEl = el; }
  if (!S.tl) { S.tl = document.createElement('div'); S.tl.className = 'tl'; $('#feed').appendChild(S.tl); for (const p of S.pendings) $('#feed').appendChild(p); }
  if (el.parentNode !== S.tl || S.tl.lastElementChild !== el) S.tl.appendChild(el);
  if (el._txt !== txt) { el._txt = txt; liveMd(el.firstElementChild, txt); }
  if (stick) toBottom();
}
/* ---- 时间线末尾的动态提示（同 VSCode）：本窗口运行用 CLI 的状态词；别处运行的按记录判断 ---- */
function zhSpin(t) {
  if (!t) return '';
  const dur = d => d.trim().replace(/(\d+)h/, '$1小时').replace(/(\d+)m/, '$1分').replace(/(\d+)s/, '$1秒').replace(/\s+/g, '');
  const eff = e => ({low: '低', medium: '中', high: '高', xhigh: '超高', max: '最高'}[e] || e);
  /* 11-12：网关慢、CLI 等待或重试时的状态行 */
  t = t.replace(/^Waiting for API response/i, '等待接口响应').replace(/(?:will retry|next try) in ([\dhms ]+?)(?=\s*(?:·|$))/i, (m, d) => dur(d) + '后重试')
    .replace(/check your network/i, '请检查网络').replace(/esc to interrupt/i, '按 Esc 可中断').replace(/attempt (\d+(?:\/\d+)?)/i, '第 $1 次尝试')
    .replace(/thinking some more with (\w+) effort/i, (m, e) => '继续思考（' + eff(e) + '）').replace(/thinking (?:some )?more/i, '继续思考')
    .replace(/still thinking/i, '仍在思考').replace(/deep in thought/i, '深度思考中').replace(/picking the thought back up/i, '接着思考');
  return t.replace(/^Compacting conversation…/i, '正在压缩上下文…').replace(/thinking with (\w+) effort/i, (m, e) => '思考中（' + ({low: '低', medium: '中', high: '高', xhigh: '超高', max: '最高'}[e] || e) + '）')
    .replace(/thought for (\d+)s/i, '思考了 $1 秒').replace(/↓ ([\d.]+k?) tokens/i, '输出 $1');
}
function renderSpin() {
  const st = S.runner, mine = st && st.alive;
  let text = '';
  if (mine && st.busy) text = zhSpin(st.spinner) || '处理中…';
  else if (!mine && S.cur && S.extOpen && Date.now() - S.extAt < 300000) text = '运行中…';
  document.body.classList.toggle('turn-live', !!text);
  let el = S.spinEl;
  const t = S.turn, done = !text && S.cur && t && (t.tl || t.it.stat);
  if (!text && !done) { if (el) { el.remove(); S.spinEl = null; } return; }
  if (!el) { el = document.createElement('div'); el.innerHTML = '<span class="sg">✳</span><span class="sv"></span><span class="sx"></span>'; S.spinEl = el; }
  if (done) {
    const x = turnInfo(t);
    el.className = 'spin done ' + x.cls;
    el.querySelector('.sg').textContent = x.glyph; el.querySelector('.sv').textContent = x.word; el.querySelector('.sx').textContent = x.stat;
    const fu = x.cls === 'ok' && t.it.fork ? t.it.fork : '';
    let fb = el.querySelector('.fk');
    if (fb && fb.dataset.u !== fu) { fb.remove(); fb = null; }
    if (fu && !fb) el.appendChild(forkBtn(t));
  } else {
    el.className = 'spin';
    const fb = el.querySelector('.fk'); if (fb) fb.remove();
    const m = /^(.*?…)\s*(.*)$/.exec(text) || [null, text, ''];
    el.querySelector('.sv').textContent = m[1];
    el.querySelector('.sx').textContent = m[2].replace(/^\(|\)$/g, '');
  }
  const feed = $('#feed');
  let tail = S.exitEl && S.exitEl.parentNode === feed ? S.exitEl : null;  // “CLI 已退出”始终在最后
  if (!tail && el.parentNode === feed) {  // 第十五轮 F1：紧跟在结束行后面的回退记录行（.k-tail）不算错位，结束行不往它们后面挪
    let n = el.nextElementSibling;
    while (n && n.classList.contains('k-tail')) n = n.nextElementSibling;
    if (!n && el.nextElementSibling) tail = el.nextElementSibling;
  }
  if (tail ? el.nextElementSibling !== tail : feed.lastElementChild !== el) { const stick = nearBottom(); feed.insertBefore(el, tail); if (stick) toBottom(); }
}
setInterval(() => { renderSpin(); if ((S.runner && S.runner.busy) || S.liveTxt) renderLive(S.runner); }, 500);
/* 开着的对话定期续期，在终端或 VSCode 里运行的对话也一直刷新 */
setInterval(() => { if (S.cur && !document.hidden) api('watch', {sid: S.cur}).catch(() => {}); }, 240000);
/* ---- CLI 意外退出：退出码 + 退出前屏幕最后几行（默认收起） ---- */
function renderExit() {
  const ex = S.exit;
  let el = S.exitEl;
  if (!ex || !S.cur) { if (el) { el.remove(); S.exitEl = null; } return; }
  if (!el || el._t !== ex.t) {
    if (el) el.remove();
    el = document.createElement('div'); el.className = 'it err cliexit'; el._t = ex.t;
    el.innerHTML = `<div class="errtext">Claude 已停止运行（退出码 ${esc(ex.code == null ? '未知' : ex.code)}）</div>` +
      (ex.tail ? '<button type="button" class="link xt">查看停止前的输出</button><pre class="xtail" hidden></pre>' : '');
    const b = el.querySelector('.xt');
    if (b) { el.querySelector('.xtail').textContent = ex.tail; b.onclick = () => { const p = el.querySelector('.xtail'); p.hidden = !p.hidden; b.textContent = p.hidden ? '查看停止前的输出' : '收起'; }; }
    S.exitEl = el;
  }
  const feed = $('#feed');
  if (feed.lastElementChild !== el) { const stick = nearBottom(); feed.appendChild(el); if (stick) toBottom(); }
}
/* ---- CLI 在等人处理、屏幕又没认出弹窗（Notification 钩子）：输入框上方的提示条和几个按键 ---- */
const noticeEl = document.createElement('div');
noticeEl.id = 'notice'; noticeEl.hidden = true;
noticeEl.innerHTML = '<div class="nt"><b>Claude 在等你操作</b><span class="nm"></span></div><div class="nk">' +
  [['1', '1'], ['2', '2'], ['3', '3'], ['up', '↑'], ['down', '↓'], ['enter', '回车'], ['esc', 'Esc']].map(([k, l]) => `<button type="button" data-k="${k}">${l}</button>`).join('') + '</div>';
$('#dock').insertBefore(noticeEl, $('#composer'));
noticeEl.querySelectorAll('[data-k]').forEach(b => b.onclick = () => { if (S.cur) api('press', {sid: S.cur, key: b.dataset.k}).catch(e => toast(e.message)); });
function renderNotice() {
  const st = S.runner;
  const msg = st && st.alive && st.notice && !st.dialog && ![...S.perms.values()].some(p => p.sid === S.cur) ? st.notice : '';
  if (noticeEl.hidden !== !msg) noticeEl.hidden = !msg;
  if (msg) noticeEl.querySelector('.nm').textContent = msg;
}
function updateRunner(st) {
  S.runner = st;
  if (st) S.exit = st.alive ? null : (st.exit || null);
  renderLive(st);
  renderSpin();
  renderExit();
  renderNotice();
  const busy = !!(st && st.alive && st.busy);
  $('#spinner').hidden = true;
  $('#spinText').textContent = busy ? (st.spinner || '处理中…') : '';
  const b = $('#send');
  /* 第十四轮 H：状态记在 data-state（send / stop / queue），不再靠按钮文字判断 */
  const stop = busy && !input.value.trim() && !S.atts.length;
  b.dataset.state = stop ? 'stop' : 'send';
  b.textContent = stop ? '停止' : '发送';
  b.classList.toggle('stop', stop);
  const tip = stop ? '停止回复（Esc）' : store.get('pref.sendKey', 'enter') === 'cmd' ? '发送（⌘Enter）' : '发送（Enter）';
  if (b.title !== tip) { b.title = tip; b.setAttribute('aria-label', stop ? '停止回复' : '发送'); }
  hook('SendBtn', busy);  // 第十二轮 D：回复中且有内容时改成“排队”，旁边出“立即发送”（ux-d.js）
  renderStatus();
  syncControls();
  showModal();
}
function renderStatus() { $('#status').textContent = ''; }  // 11-11：用户要求去掉输入框下面的状态栏（额度改在设置 · 用量里看）
function buildModelOptions() {
  const sel = $('#model');
  sel.innerHTML = `<option value="">默认（${esc(S.models.default || '—')}）</option>` +
    S.models.list.map(m => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
  syncControls();
}
function modelValue(v) {
  if (!v) return '';
  const list = S.models.list || [];
  if (list.some(m => m.id === v)) return v;
  const hit = list.find(m => m.id.toLowerCase().includes(String(v).toLowerCase()));
  return hit ? hit.id : '';
}
function syncControls() {
  const po = S.opts || {};
  const st = S.runner && S.runner.alive ? S.runner : null;
  $('#model').value = modelValue(po.model);
  $('#effort').value = po.effort || (st && st.effort) || '';
  const m = curMode();
  $('#mode').value = [...$('#mode').options].some(o => o.value === m) ? m : 'default';
  hook('Controls');  // 第十四轮 H：模型、思考强度、权限的药丸按钮跟着刷新（ux-h.js）
}
/* 第十四轮 FH：回复进行中 CLI 底栏常常不显示权限模式，后台解析成 default；每个对话记住最后一次解析到的模式，
   忙碌时解析到 default 就沿用它（忙碌时解析到别的模式，比如这时切换了模式，照常采用） */
S.modeBy = {};
function curMode() {
  const po = S.opts || {}, st = S.runner && S.runner.alive ? S.runner : null;
  if (st && S.cur && st.mode) {
    if (!st.busy || st.mode !== 'default') S.modeBy[S.cur] = st.mode;
    return S.modeBy[S.cur] || po.mode || st.mode;
  }
  return po.mode || 'default';
}
async function control(o) {
  if (!S.cur) return;
  try { const d = await api('control', Object.assign({sid: S.cur}, o)); S.opts = d.opts || {}; } catch (e) { toast(e.message); }
  syncControls();
}
$('#model').onchange = e => control({model: e.target.value});
$('#effort').onchange = e => control({effort: e.target.value});
$('#mode').onchange = e => control({mode: e.target.value});

/* ================= 输入：补全、历史、短语、附件 ================= */
let draftTimer = null;
/* 第十四轮 FH：高度至少一行（按当前行高取整，字号大时 scrollHeight 可能比行高还矮一点，会出竖向滚动条） */
function fitHeight() {
  input.style.height = 'auto';
  const lh = Math.ceil(parseFloat(getComputedStyle(input).lineHeight) || 0);
  input.style.height = Math.min(Math.max(input.scrollHeight, lh), innerHeight * 0.4) + 'px';
}
function autosize() {
  fitHeight();
  $('#jump').style.bottom = ($('#dock').offsetHeight + 14) + 'px';
  updateRunner(S.runner);
}
/* 设置里改字号（<html data-font>）后输入框按新行高重新量高，不等下一次输入 */
new MutationObserver(() => autosize()).observe(document.documentElement, {attributes: true, attributeFilter: ['data-font']});
input.addEventListener('input', () => {
  autosize(); S.hist = -1;
  clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 400);
  checkPop();
});
input.addEventListener('click', checkPop);
input.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== input) hidePop(); }, 150));

/* --- @ 文件 与 / 命令 补全 --- */
let popTimer = null, popSeq = 0;
function checkPop() {
  const v = input.value, c = input.selectionStart;
  const before = v.slice(0, c);
  const slash = /^\/([^\s]*)$/.exec(before);
  const at = /(^|\s)@([^\s@"]*)$/.exec(before);
  if (slash) return showCommands(slash[1]);
  if (at) { clearTimeout(popTimer); const q = at[2], seq = ++popSeq; popTimer = setTimeout(() => showFiles(q, seq), 120); return; }
  hidePop();
}
async function showCommands(q) {
  const seq = ++popSeq;
  if (S.commands.cwd !== S.cwd) {
    try { S.commands = {cwd: S.cwd, list: (await api('commands', {cwd: S.cwd})).commands}; } catch { S.commands = {cwd: S.cwd, list: []}; }
  }
  if (seq !== popSeq) return;
  const ql = q.toLowerCase();
  let list = S.commands.list.filter(c => c.name.toLowerCase().includes(ql)).sort((a, b) => (a.name.toLowerCase().startsWith(ql) ? 0 : 1) - (b.name.toLowerCase().startsWith(ql) ? 0 : 1)).slice(0, 30);
  /* 第十五轮 F1：后台给了 group（chat 对话 / review 审查）时按组显示：对话、审查、技能（其余内置命令归“命令”） */
  const grouped = S.commands.list.some(c => c.group);
  const grpOf = c => c.group === 'chat' ? '对话' : c.group === 'review' ? '审查' : c.src === '内置' ? '命令' : '技能';
  if (grouped) { const order = ['对话', '审查', '命令', '技能']; list = list.map((c, i) => [c, i]).sort((a, b) => order.indexOf(grpOf(a[0])) - order.indexOf(grpOf(b[0])) || a[1] - b[1]).map(x => x[0]); }
  showPop('cmd', list.map(c => ({label: '/' + c.name, sub: c.desc, tag: grouped && c.src === '内置' ? '' : c.src, value: c.name, grp: grouped ? grpOf(c) : ''})));
}
async function showFiles(q, seq) {
  let files = [];
  try { files = (await api('files', {cwd: S.cwd, q})).files; } catch {}
  const at = /(^|\s)@([^\s@"]*)$/.exec(input.value.slice(0, input.selectionStart));
  if (seq !== popSeq || !at || at[2] !== q) return;  // 光标已经离开这个 @ 词
  if (!q) {
    const chg = changedFiles().map(([p]) => shortPath(p)).filter(p => !p.startsWith('/') && !p.startsWith('~'));
    files = [...new Set([...chg, ...files])];
  }
  showPop('file', files.slice(0, 40).map(p => ({label: p, value: p, tag: p.endsWith('/') ? '文件夹' : ''})));
}
function showPop(kind, items) {
  const pop = $('#pop');
  if (!items.length) { hidePop(); return; }
  S.pop = {kind, items, i: 0};
  pop.innerHTML = items.map((it, i) => (it.grp && (!i || items[i - 1].grp !== it.grp) ? `<div class="pgrp">${esc(it.grp)}</div>` : '') +  // 第十五轮 F1：分组标题（不是 .pi，不影响上下键）
    `<div class="pi${i === 0 ? ' on' : ''}" data-i="${i}"><span class="pl">${esc(it.label)}</span>` +
    (it.sub ? `<span class="ps">${esc(it.sub)}</span>` : '') + (it.tag ? `<span class="pt">${esc(it.tag)}</span>` : '') + '</div>').join('');
  pop.hidden = false;
  pop.querySelectorAll('.pi').forEach(el => { el.onmousedown = e => { e.preventDefault(); S.pop.i = +el.dataset.i; pickPop(); }; });
}
function hidePop() { clearTimeout(popTimer); popSeq++; $('#pop').hidden = true; S.pop = null; }
function movePop(d) {
  const p = S.pop; p.i = (p.i + d + p.items.length) % p.items.length;
  $('#pop').querySelectorAll('.pi').forEach((el, i) => el.classList.toggle('on', i === p.i));
  $('#pop').querySelector('.pi.on').scrollIntoView({block: 'nearest'});
}
function pickPop() {
  const p = S.pop; if (!p) return;
  const it = p.items[p.i], v = input.value, c = input.selectionStart;
  if (p.kind === 'cmd') {
    const rest = v.slice(c).replace(/^\S*/, '');
    input.value = '/' + it.value + ' ' + rest.replace(/^\s*/, '');
    input.selectionStart = input.selectionEnd = it.value.length + 2;
  } else {
    const before = v.slice(0, c).replace(/@[^\s@"]*$/, '');
    const ins = '@' + (/\s/.test(it.value) ? `"${it.value}"` : it.value) + (it.value.endsWith('/') ? '' : ' ');
    input.value = before + ins + v.slice(c);
    input.selectionStart = input.selectionEnd = (before + ins).length;
  }
  hidePop(); autosize(); input.focus();
  if (p.kind === 'file' && it.value.endsWith('/')) checkPop();
}

/* --- 历史消息 ↑ ↓ --- */
function histStep(d) {
  const us = userItems().map(u => u.text).filter(Boolean).reverse();
  if (!us.length) return false;
  if (S.hist === -1) S.histDraft = input.value;
  const n = S.hist + d;
  if (n < -1 || n >= us.length) return true;
  S.hist = n;
  input.value = n === -1 ? (S.histDraft || '') : us[n];
  autosize();
  input.selectionStart = input.selectionEnd = d > 0 ? 0 : input.value.length;
  return true;
}
input.addEventListener('keydown', e => {
  if (S.pop) {
    if (e.key === 'ArrowDown') { e.preventDefault(); return movePop(1); }
    if (e.key === 'ArrowUp') { e.preventDefault(); return movePop(-1); }
    if ((e.key === 'Enter' || e.key === 'Tab') && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); return pickPop(); }
    if (e.key === 'Escape') { e.preventDefault(); return hidePop(); }
  }
  if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) {
    /* 第十四轮 H：发送键偏好。enter：回车发送、⌘回车立即发送；cmd：回车换行、⌘回车发送、⌘⇧回车立即发送 */
    if (store.get('pref.sendKey', 'enter') === 'cmd') { if (e.metaKey || e.ctrlKey) { e.preventDefault(); send(e.shiftKey); return; } }
    else if (!e.shiftKey) { e.preventDefault(); send(e.metaKey || e.ctrlKey); return; }
  }
  if (e.key === 'ArrowUp' && !e.shiftKey && input.selectionStart === 0 && input.selectionEnd === 0 && (S.hist >= 0 || !input.value.includes('\n'))) {
    if (histStep(1)) e.preventDefault(); return;
  }
  if (e.key === 'ArrowDown' && S.hist >= 0 && input.selectionStart === input.value.length) { e.preventDefault(); histStep(-1); return; }
  if (e.key === 'Escape' && S.runner && S.runner.busy && !input.value) api('interrupt', {sid: S.cur});
});

/* --- 常用短语 --- */
$('#phraseBtn').onclick = e => {
  menu(e.currentTarget, [...S.phrases.map(p => ({label: p, title: '点击直接发送，⌥ 点击只插入', fn: ev => {
    if (ev.altKey) insertText(p); else send(false, p);
  }})), {sep: true}, {label: '编辑常用语…', fn: async () => {
    const v = await askText('常用语（一行一条）', S.phrases.join('\n'), true);
    if (v == null) return;
    S.phrases = v.split('\n').map(x => x.trim()).filter(Boolean);
    if (!S.phrases.length) S.phrases = DEFAULT_PHRASES.slice();
    store.set('phrases', S.phrases);
  }}]);
};

/* --- 附件 --- */
function insertText(t) {
  const s = input.selectionStart ?? input.value.length, e = input.selectionEnd ?? s;
  const pre = input.value.slice(0, s), sep = pre && !/\s$/.test(pre) ? ' ' : '';
  input.value = pre + sep + t + input.value.slice(e);
  autosize(); input.focus(); saveDraft();
}
const mention = p => '@' + (/\s/.test(p) ? `"${p}"` : p) + ' ';
const IMG_RE = /\.(png|jpe?g|gif|webp|bmp)$/i;
function renderAtts() {
  const box = $('#atts'); box.innerHTML = '';
  S.atts.forEach((a, i) => {
    const el = document.createElement('div');
    el.className = 'att' + (a.path ? '' : ' loading');
    el.innerHTML = `<img src="${esc(a.url)}" alt=""><span>${esc(a.name)}</span><button title="移除">×</button>`;
    el.querySelector('img').onclick = () => lightbox(a.url);
    el.querySelector('button').onclick = () => { S.atts.splice(i, 1); renderAtts(); };
    box.appendChild(el);
  });
  updateRunner(S.runner);
}
function readB64(file) {
  return new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(',')[1] || ''); r.onerror = bad; r.readAsDataURL(file); });
}
async function upload(file) { return (await api('upload', {name: file.name || 'image.png', type: file.type, data: await readB64(file)})).path; }
async function addFiles(files, fromDrop) {
  files = [...files];
  if (!files.length) return;
  const sid = S.cur;
  let located = [];
  if (fromDrop) {
    try { located = (await api('locate', {items: files.map(f => ({name: f.name, size: f.size, mtime: f.lastModified}))})).paths || []; } catch {}
  }
  let copies = 0;
  for (let i = 0; i < files.length; i++) {
    const f = files[i], isImg = (f.type || '').startsWith('image/') || IMG_RE.test(f.name);
    if (isImg) {
      const a = {name: f.name || '截图', url: URL.createObjectURL(f), path: located[i] || null};
      S.atts.push(a); renderAtts();
      if (!a.path) { try { a.path = await upload(f); } catch (e) { toast(e.message); S.atts = S.atts.filter(x => x !== a); } renderAtts(); }
    } else if (located[i]) { if (S.cur === sid) insertText(mention(located[i])); }
    else { try { const up = await upload(f); if (S.cur === sid) { insertText(mention(up)); copies++; } } catch (e) { toast(e.message); } }
    if (S.cur !== sid) return;
  }
  if (copies) toast('没找到原文件，已改用一份副本');
}
/* 第十一轮 B：原生 App 里粘贴访达复制的文件——向外壳要剪贴板里的原路径；图片作附件（显示缩略图），其他文件插入 @路径。
   外壳（native/main.swift）注入 CWB_NATIVE_PASTE 表示支持；回调 CWB_pasteFiles(id, 路径数组)。不在原生 App 里保持原来的行为。 */
function addPaths(paths) {
  for (const p of paths || []) {
    if (IMG_RE.test(p)) { S.atts.push({name: p.split('/').pop(), url: fileUrl(p), path: p}); renderAtts(); }
    else insertText(mention(p));
  }
}
const pasteCbs = {};
let pasteSeq = 0;
window.CWB_pasteFiles = (id, paths) => { const f = pasteCbs[id]; delete pasteCbs[id]; if (f) f(Array.isArray(paths) ? paths : []); };
function nativePastePaths() {
  return new Promise(ok => {
    const id = ++pasteSeq; pasteCbs[id] = ok;
    try { window.webkit.messageHandlers.cwb.postMessage({type: 'pasteFiles', id}); } catch { delete pasteCbs[id]; ok([]); return; }
    setTimeout(() => { if (pasteCbs[id]) { delete pasteCbs[id]; ok([]); } }, 1500);
  });
}
input.addEventListener('paste', async e => {
  const cd = e.clipboardData, files = [...(cd && cd.files || [])], text = cd ? cd.getData('text/plain') || '' : '';
  const types = [...(cd && cd.types || [])];
  if (window.CWB_NATIVE_PASTE && window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.cwb &&
      (files.length || types.includes('Files') || (text && text.length < 1024 && !/\n/.test(text)))) {  // 访达复制的文件在网页里可能只露出文件名文字，单行短文字也问一下外壳
    e.preventDefault();
    const sid = S.cur, paths = await nativePastePaths();
    if (S.cur !== sid) return;
    if (paths.length) { addPaths(paths); return; }
    if (files.length && !text.trim()) addFiles(files, false);
    else if (text) { input.focus(); document.execCommand('insertText', false, text); }
    return;
  }
  if (files.length && !text.trim()) { e.preventDefault(); addFiles(files, false); }
});
let dragDepth = 0;
window.addEventListener('dragenter', e => { if ([...e.dataTransfer.types].includes('Files')) { dragDepth++; document.body.classList.add('dragging'); } });
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove('dragging'); } });
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => {
  e.preventDefault(); dragDepth = 0; document.body.classList.remove('dragging');
  if (!S.cur && !document.body.classList.contains('h-home')) { toast('先选择或新建一个对话'); return; }  // 第十四轮 H：首页也能拖入
  addFiles(e.dataTransfer.files, true);
});
$('#attach').onclick = async () => {
  const d = await api('pick-files').catch(() => ({paths: []}));
  addPaths(d.paths);
};

/* --- 发送（回复进行中发送的显示“排队中”：CLI 会在下一步工具结束时插进当前回复；可立即发送或撤回）--- */
function queuedPendings() { return S.pendings.filter(p => p.classList.contains('queued')); }
async function sendNow() {
  try { await api('send-now', {sid: S.cur}); queuedPendings().forEach(p => { const b = p.querySelector('.qact'); if (b) b.textContent = '正在发送…'; }); }
  catch (e) { toast(e.message); }
}
async function unqueue() {
  const qs = queuedPendings(); if (!qs.length) return;
  try {
    const d = await api('unqueue', {sid: S.cur, texts: qs.map(p => p._text || '')});
    if (!d.ok) { toast(d.error || '撤回失败'); return; }
    const texts = qs.map(p => p._text).filter(Boolean), atts = qs.flatMap(p => p._atts || []);
    qs.forEach(p => p.remove()); S.pendings = S.pendings.filter(p => !qs.includes(p));
    input.value = [...texts, input.value].filter(x => x && x.trim()).join('\n\n');
    S.atts = [...atts, ...S.atts]; renderAtts(); autosize(); input.focus();
  } catch (e) { toast(e.message); }
}
async function send(now, direct) {
  askNotifyPermission();
  if (!S.cur) { if (window.CWB && CWB.homeSend) CWB.homeSend(now, direct); return; }  // 第十四轮 H：首页发送先新建对话（ux-h.js）
  const fromInput = direct == null;
  if (fromInput) hidePop();
  const busy = S.runner && S.runner.alive && S.runner.busy;
  const text = fromInput ? input.value : direct;
  if (fromInput && S.atts.some(a => !a.path)) { toast('图片还在上传，稍等一下再发'); return; }
  const atts = fromInput ? S.atts.slice() : [];
  if (!text.trim() && !atts.length) { if (busy) api('interrupt', {sid: S.cur}); return; }
  /* 第十五轮联调：“/btw 问题”走右栏“旁问”（ux-l.js 的 CWB.btwAsk），不进时间线、不排队；后端 send 对 /btw 只返回 {id, btw}，没有记录条目，待发气泡消不掉 */
  const bm = /^\s*\/btw(?:\s+([\s\S]*))?$/i.exec(text);
  if (bm && !atts.length && window.CWB && CWB.btwAsk) {
    if (fromInput) { input.value = ''; autosize(); store.set('draft.' + S.cur, null); S.hist = -1; }
    const q = (bm[1] || '').trim();
    if (q) CWB.btwAsk(q); else if (CWB.openBtw) CWB.openBtw();
    return;
  }
  if (S.recent && !(S.runner && S.runner.alive)) {
    if (!confirm('这个对话刚在别的窗口里更新过，可能正在 VSCode 或终端里运行。确定在这里继续？')) return;
    S.recent = false;
  }
  if (fromInput) { input.value = ''; S.atts = []; renderAtts(); autosize(); store.set('draft.' + S.cur, null); S.hist = -1; }
  S.liveTxt = ''; if (S.liveEl) { S.liveEl.remove(); S.liveEl = null; }
  const pu = document.createElement('div');
  const queued = (busy || S.pendings.length) && !now;
  pu.className = 'user pending' + (queued ? ' queued' : '');
  pu.innerHTML = (queued ? '<div class="qbar"><span class="qtag">排队中</span><span class="grow"></span><button class="link qact" data-a="now">立即发送</button><button class="link qact2" data-a="back">撤回</button></div>' : '') +
    `<div class="utext">${esc(text.trim())}</div>` + thumbs(atts.map(a => a.url));
  pu._text = text.trim(); pu._atts = atts;
  pu.querySelector('[data-a=now]')?.addEventListener('click', sendNow);
  pu.querySelector('[data-a=back]')?.addEventListener('click', unqueue);
  S.pendings.push(pu); $('#feed').appendChild(pu); toBottom();
  try {
    const r = await api('send', {sid: S.cur, text, images: atts.map(a => a.path), now: !!(now && busy)});
    if (r && r.error) throw Object.assign(new Error(r.error), {data: r});  // 第十五轮 F1：被拦截的命令返回 {error, blocked, hint}
  } catch (e) {
    if (e.data && e.data.blocked) hook('Blocked', e.data, text); else toast(e.message);  // 第十五轮 F1：拦截提示显示在输入框上方（ux-k.js）
    pu.remove(); S.pendings = S.pendings.filter(x => x !== pu);
    if (fromInput) { input.value = text; S.atts = atts; renderAtts(); autosize(); }
  }
}
$('#send').onclick = () => {
  if ($('#send').dataset.state === 'stop') api('interrupt', {sid: S.cur});
  else send();
};
$('#cwd').onclick = () => S.cwd && api('reveal', {path: S.cwd});
$('#addFolder').onclick = async () => {
  const d = await api('add-folder').catch(() => ({}));
  if (d.cwd) { toggleProject(d.cwd, true); refreshSide(true); }
};
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#filter').focus(); $('#filter').select(); }
  else if (e.key === 'Escape') { if (!$('#lightbox').hidden) $('#lightbox').hidden = true; closeMenu(); }
});

/* ================= 文件、链接、图片 ================= */
async function openPath(path, inFinder, direct) {
  if (!inFinder && !direct && NO_PREVIEW.test(path.replace(/:\d+(-\d+)?$/, ''))) inFinder = true;  // 11-11：Office 等预览不了的文件，点击直接在访达里选中
  if (!inFinder && !direct && window.CWB && CWB.preview) return CWB.preview(path);
  const r = await api(inFinder ? 'reveal' : 'open-file', {path, cwd: S.cwd, sid: S.cur}).catch(() => ({}));  // 11-23：带上对话 ID，只写文件名时后台按这条对话里出现过的完整路径找回
  if (!r.ok) toast('找不到这个文件：' + path);
}
document.addEventListener('click', e => {
  const img = e.target.closest('.thumbs img, .md img');
  if (img) { e.preventDefault(); lightbox(img.src); return; }
  const a = e.target.closest('a[href]');
  if (a && a.closest('#feed, #modal, #rpane')) {
    e.preventDefault();
    const href = a.getAttribute('href') || '';
    if (/^https?:\/\//i.test(href)) api('open-url', {url: href});
    else if (!/^(mailto|javascript|data):/i.test(href) && !href.startsWith('#')) {
      let p = href; try { p = decodeURIComponent(href); } catch {}
      openPath(p, e.metaKey || e.ctrlKey, e.altKey);
    }
    return;
  }
  const p = e.target.closest('[data-path]');
  if (p && p.closest('#feed, #modal, #todoBar, #rpane')) { e.preventDefault(); e.stopPropagation(); openPath(p.dataset.path, e.metaKey || e.ctrlKey, e.altKey); }
}, true);
/* 11-23：回复里的文件右键：侧栏预览、在访达中显示、用默认程序打开、复制完整路径。选中了文字时不拦，照常用系统菜单复制。 */
document.addEventListener('contextmenu', e => {
  const t = e.target.closest('[data-path], a[href]');
  if (!t || !t.closest('#feed, #modal, #todoBar, #rpane')) return;
  let path = t.dataset.path;
  if (!path) {
    const href = t.getAttribute('href') || '';
    if (/^(https?|mailto|javascript|data):/i.test(href) || href.startsWith('#')) return;
    try { path = decodeURIComponent(href); } catch { path = href; }
  }
  if (String(window.getSelection() || '').trim()) return;
  e.preventDefault(); e.stopPropagation();
  const where = {path, cwd: S.cwd, sid: S.cur};
  const fail = r => { if (!r || !r.ok) toast('找不到这个文件：' + path); };
  menu({x: e.clientX, y: e.clientY}, [
    ...(NO_PREVIEW.test(path.replace(/:\d+(-\d+)?$/, '')) ? [] : [{label: '在侧栏预览', fn: () => openPath(path)}]),
    {label: '在访达中显示', fn: () => api('reveal', where).then(fail, () => fail())},
    {label: '用默认程序打开', fn: () => api('open-file', where).then(fail, () => fail())},
    {sep: true},
    {label: '复制完整路径', fn: () => api('preview', where).then(d => copy(d.path)).then(() => toast('已复制路径')).catch(() => fail())},
  ]);
}, true);

/* ================= 弹窗 ================= */
function closeModal() { $('#modal').hidden = true; $('#card').innerHTML = ''; S.modalKey = null; }
/* 11-25 选择题连点：弹窗刚画出来（或刚换成下一个）的一小段时间里不认鼠标点击。上一个弹窗的第二下点击、
   双击会落到同一位置新出现的按钮上，把还没看到的下一题选掉。键盘（⌘回车等，detail 为 0）不受影响。 */
const MODAL_GUARD = 400;
function armModal(id) { if (id !== S.armedId) { S.armedId = id; S.modalAt = Date.now(); } }  // 同一个弹窗因上方文字变化重画时不重新计时
$('#card').addEventListener('click', e => {
  if (e.detail > 0 && Date.now() - (S.modalAt || 0) < MODAL_GUARD && e.target.closest('button')) { e.preventDefault(); e.stopPropagation(); }
}, true);
function showModal() {
  if (NATIVE) native({type: 'badge', n: 0});  // 11-25 用户要求：程序坞图标不显示数字（原来是等你确认数＋未读数）；发 0 顺带清掉旧角标
  renderNotice();
  if (S.modalKey === 'ask') return;
  const inline = p => { try { return !!(window.CWB && CWB.inlinePerm && CWB.inlinePerm(p)); } catch { return false; } };  // 第十五轮 F1：带计划全文的计划确认画在时间线里（ux-k.js）
  const perm = [...S.perms.values()].filter(p => !inline(p)).sort((a, b) => (a.sid === S.cur ? -1 : 0) - (b.sid === S.cur ? -1 : 0))[0];
  if (perm) { if (S.modalKey !== 'p:' + perm.rid) permModal(perm); return; }
  const dlg = S.runner && S.runner.alive && S.runner.dialog;
  const picked = S.dlgPicked;  // 11-25：刚点过的终端弹窗，CLI 翻页前不再弹出来（后台也会压住，这里防推送在路上）
  if (dlg && !(picked && picked.id && picked.id === dlg.id && Date.now() - picked.t < 1500)) {
    const key = 'd:' + S.cur + ':' + dlg.text + dlg.options.map(o => o.label).join('|');
    if (S.modalKey !== key) dialogModal(dlg, key);
    return;
  }
  if (S.modalKey) closeModal();
}
function sessTitle(sid) { const s = findSession(sid); return s ? s.title : '新对话'; }
function permModal(p) {
  S.modalKey = 'p:' + p.rid;
  const card = $('#card'), inp = p.input || {};
  const from = p.sid !== S.cur ? `<div class="from">来自：${esc(sessTitle(p.sid))}</div>` : '';
  const done = (decision, extra) => { api('perm', Object.assign({rid: p.rid, decision}, extra || {})).catch(e => toast(e.message)); S.perms.delete(p.rid); closeModal(); showModal(); };
  if (p.tool === 'AskUserQuestion') return askModal(p, from, done);
  let body = '', title = p.tool;
  if (p.tool === 'Bash') { body = (inp.description ? `<div class="desc">${esc(inp.description)}</div>` : '') + `<pre class="blk">${esc(inp.command)}</pre>`; title = '运行命令'; }
  else if (p.tool === 'Edit') { title = '修改文件'; body = `<div class="desc"><a data-path="${esc(inp.file_path)}" class="path">${esc(shortPath(inp.file_path))}</a></div><div class="box">${diffRow(inp.old_string, inp.new_string, langOf(inp.file_path))}</div>`; }
  else if (p.tool === 'Write') { title = '写入文件'; body = `<div class="desc">${esc(shortPath(inp.file_path))}</div><pre class="blk">${esc((inp.content || '').slice(0, 4000))}</pre>`; }
  else if (p.tool === 'ExitPlanMode') { title = '执行这个计划？'; body = `<div class="md">${md(inp.plan || '')}</div>`; }
  else if (p.tool === 'WebFetch') { title = '访问网页'; body = `<pre class="blk">${esc(inp.url)}</pre>`; }
  else body = `<pre class="blk">${esc(JSON.stringify(inp, null, 2))}</pre>`;
  const always = p.suggestions && p.suggestions.length && p.tool !== 'ExitPlanMode';
  const allowLabel = p.tool === 'ExitPlanMode' ? '批准' : '允许';
  const denyLabel = p.tool === 'ExitPlanMode' ? '继续规划' : '拒绝';
  card.innerHTML = `<h3>${esc(title)}</h3>${from}${body}<div class="acts"><button class="btn" data-d="deny">${denyLabel}</button>` +
    (always ? '<button class="btn" data-d="always">总是允许</button>' : '') + `<button class="btn primary" data-d="allow">${allowLabel}</button></div>`;
  card.querySelectorAll('[data-d]').forEach(b => b.onclick = () => done(b.dataset.d, p.tool === 'ExitPlanMode' && b.dataset.d === 'deny' ? {message: '用户希望继续完善计划。'} : null));
  $('#modal').hidden = false; armModal('p:' + p.rid);
  card.tabIndex = -1; card.focus({preventScroll: true});
  // 11-25：长按 ⌘回车的自动重复不算，免得一张批完、下一张授权卡刚出来就被连着批掉
  card.onkeydown = e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); if (!e.repeat) card.querySelector('.primary').click(); } };
}
function askModal(p, from, done) {
  const card = $('#card'), qs = (p.input && p.input.questions) || [];
  const sel = qs.map(() => new Set());
  card.innerHTML = `<h3>需要你选择</h3>${from}` + qs.map((q, i) => `<div class="q" data-i="${i}"><div>${q.header ? `<span class="qh">${esc(q.header)}</span>` : ''}${esc(q.question)}</div>` +
    `<div class="opts">${(q.options || []).map((o, j) => `<button class="opt" data-j="${j}">${esc(o.label)}${o.description ? `<small>${esc(o.description)}</small>` : ''}</button>`).join('')}</div>` +
    `<input type="text" placeholder="其他" data-other="${i}"></div>`).join('') +
    '<div class="acts"><button class="btn" data-d="deny">跳过</button><button class="btn primary" data-d="allow">提交</button></div>';
  card.querySelectorAll('.q').forEach(qe => {
    const i = +qe.dataset.i, multi = !!qs[i].multiSelect;
    qe.querySelectorAll('.opt').forEach(b => b.onclick = () => {
      const j = +b.dataset.j;
      if (multi) { sel[i].has(j) ? sel[i].delete(j) : sel[i].add(j); }
      else { sel[i].clear(); sel[i].add(j); }
      qe.querySelectorAll('.opt').forEach(x => x.classList.toggle('sel', sel[i].has(+x.dataset.j)));
    });
  });
  card.querySelector('[data-d=deny]').onclick = () => done('deny', {message: '用户跳过了这个问题。'});
  card.querySelector('[data-d=allow]').onclick = () => {
    const answers = {};
    qs.forEach((q, i) => {
      const other = card.querySelector(`[data-other="${i}"]`).value.trim();
      const labels = [...sel[i]].sort().map(j => q.options[j].label);
      if (other) labels.push(other);
      if (labels.length) answers[q.question] = labels.join(', ');
    });
    done('allow', {updatedInput: Object.assign({}, p.input, {answers})});
  };
  $('#modal').hidden = false; armModal('p:' + p.rid);
}
function dialogModal(d, key) {
  S.modalKey = key;
  const card = $('#card'), sid = S.cur;
  // 11-25：带上弹窗身份，后台核对屏幕上还是这个弹窗才按键；点过的记下来，翻页前不再弹
  // 后台说弹窗已经变了（stale）：这次没按，马上按现在的状态重新弹
  const sent = r => { if (r && r.stale) { S.dlgPicked = null; showModal(); } };
  const pick = i => { S.dlgPicked = {id: d.id, t: Date.now()}; api('dialog', {sid, option: i, id: d.id}).then(sent, () => {}); closeModal(); };
  if (d.kind === 'trust') {
    const yes = d.options.findIndex(o => /trust|yes/i.test(o.label));
    const no = d.options.findIndex(o => /no|exit/i.test(o.label));
    card.innerHTML = `<h3>信任这个文件夹？</h3><div class="desc">${esc(S.cwd)}</div><div class="from">信任后 Claude 可以读取、修改和执行这个文件夹里的文件。</div>` +
      '<div class="acts"><button class="btn" data-a="no">取消</button><button class="btn primary" data-a="yes">信任</button></div>';
    card.querySelector('[data-a=yes]').onclick = () => pick(yes < 0 ? 1 : yes);
    card.querySelector('[data-a=no]').onclick = () => pick(no < 0 ? 0 : no);
  } else {
    card.innerHTML = `<h3>需要你确认</h3>${d.text ? `<pre class="blk">${esc(d.text)}</pre>` : ''}<div class="opts">` +
      d.options.map((o, i) => `<button class="opt${i === d.cur ? ' sel' : ''}" data-i="${i}">${esc(o.label)}${o.desc ? `<small>${esc(o.desc)}</small>` : ''}</button>`).join('') +
      '</div><div class="acts"><button class="btn" data-a="esc">取消</button></div>';
    card.querySelectorAll('.opt').forEach(b => b.onclick = () => pick(+b.dataset.i));
    card.querySelector('[data-a=esc]').onclick = () => { S.dlgPicked = {id: d.id, t: Date.now()}; api('dialog', {sid, key: 'esc', id: d.id}).then(sent, () => {}); closeModal(); };
  }
  $('#modal').hidden = false; armModal('d:' + d.id);
  const pre = card.querySelector('pre.blk');
  if (pre) pre.scrollTop = pre.scrollHeight;  // 题目在选项正上方：滚到底，不用手动往下翻才看到问的是什么
}

/* ================= 推送 ================= */
function applyItems(ev) {
  const stick = nearBottom();
  if ('open' in ev) { S.extOpen = !!ev.open; S.extAt = Date.now(); }
  for (const it of ev.new || []) addItem(it);
  for (const p of ev.patch || []) patchItem(p);
  renderSpin();
  if (stick) toBottom(); else updateJump();
}
function setSideFlags(sid, st) {
  const busy = !!(st.alive && st.busy);
  if (S.busyMap[sid] && !busy && !document.hasFocus()) notifyLocal(sessTitle(sid), '回复完成', sid);
  S.busyMap[sid] = busy;
  const s = findSession(sid);
  if (!s) { refreshSide(); return; }
  const f = {running: !!st.alive, busy, attention: !!(st.alive && st.attention)};
  if (f.running !== s.running || f.busy !== s.busy || f.attention !== s.attention) { Object.assign(s, f); renderSide(); }
}
function hook(n, ...a) { try { if (window.CWB && CWB['on' + n]) CWB['on' + n](...a); } catch (e) { console.error(e); } }
function handle(ev) {
  hook('Event', ev);
  switch (ev.type) {
    case 'notify': notifyLocal(ev.title, ev.text, ev.sid); break;
    case 'items':
      if (ev.sid === S.loading) S.buffer.push(ev);
      else if (ev.sid === S.cur) applyItems(ev);
      break;
    case 'reset': if (ev.sid === S.cur) openSession(S.cur); break;
    case 'runner':
      setSideFlags(ev.sid, ev.state);
      if (ev.sid === S.cur) updateRunner(ev.state);
      break;
    case 'perm':
      S.perms.set(ev.req.rid, ev.req); showModal();
      if (!document.hasFocus()) notifyLocal(sessTitle(ev.req.sid), '需要你确认：' + ev.req.tool, ev.req.sid);
      break;
    case 'perm-done': S.perms.delete(ev.rid); if (S.modalKey === 'p:' + ev.rid) closeModal(); showModal(); break;
    case 'sessions': refreshSide(); break;
    case 'notice': if (!document.hasFocus()) notifyLocal(ev.title || sessTitle(ev.sid), 'Claude 在等你操作：' + (ev.text || ''), ev.sid); break;
    case 'rekey':
      /* /clear 换了会话 ID：后台已迁到新 ID，界面跟着切过去（旧对话留在列表里） */
      if (S.busyMap[ev.old] != null) { S.busyMap[ev.new] = S.busyMap[ev.old]; delete S.busyMap[ev.old]; }
      if (S.cur === ev.old || S.loading === ev.old) {
        store.set('draft.' + ev.new, input.value.trim() ? input.value : null); S.attsBy[ev.new] = S.atts;
        openSession(ev.new).then(() => { if (S.cur === ev.new) { store.set('draft.' + ev.old, null); delete S.attsBy[ev.old]; } });
        store.set('last', ev.new);
      }
      refreshSide(true);
      break;
    case 'status': S.status = ev.text; renderStatus(); break;
    case 'toast': toast(ev.text); break;
  }
}
function connect() {
  const es = new EventSource('/events?t=' + encodeURIComponent(T));
  es.onmessage = e => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  es.onopen = () => { refreshSide(true); if (S.cur) openSession(S.cur); };
}

window.CWB = {S, api, $, esc, md, enhance, toast, menu, closeMenu, askText, copy, lightbox, openSession, newSession, load, insertText, forkAt, loadOlder, loadUntil,
  send: () => send(), shortPath, fileUrl, refreshSide, renderSide, statText, fmtTok, fmtSecs, findSession, sessTitle, toggleProject,
  changedFiles, userItems, langOf, hl, openPath, control, renderItem, addMore, sendText: t => send(false, t)};
Object.assign(window.CWB, {clearMain, addPaths, showModal});  // 第十一轮 B：批量删除、粘贴文件、弹窗关闭后补显授权
Object.assign(window.CWB, {turnInfo, nearBottom, toBottom, sendImmediate: () => send(true)});  // 第十二轮 D：过程折叠、立即发送
Object.assign(window.CWB, {store, renderAtts, renderTop, refreshRunner: () => updateRunner(S.runner)});  // 第十四轮 H：首页、输入栏（ux-h.js）
Object.assign(window.CWB, {curMode, fitHeight});  // 第十四轮 FH：权限药丸和下拉框用同一个模式；首页切换时输入框量高
document.body.classList.add('nosess');
buildModelOptions();
connect();
setInterval(() => refreshSide(), 20000);
(async () => {
  try {
    const d = await api('projects');
    S.boot = d.boot; S.projects = d.projects; S.home = d.home; S.status = d.status || '';
    if (d.models) { S.models = d.models; buildModelOptions(); }
    renderSide(); renderStatus();
    const last = store.get('last', null);
    const go = store.get('pref.startup', 'last') !== 'new' && last && findSession(last);  // 第十四轮 H：启动偏好 new＝直接进首页
    if (go) {
      // 11-11：服务刚重启时偶尔打不开，重试 3 次，别落到首页
      await openSession(last);
      for (let i = 0; i < 3 && !S.cur && !S.loading; i++) { await new Promise(r => setTimeout(r, 1500)); if (!S.cur && !S.loading) await openSession(last); }
    }
    S.booted = true; hook('Boot', !!go);
  } catch (e) { toast(e.message); S.booted = true; hook('Boot', false); }
})();
})();
