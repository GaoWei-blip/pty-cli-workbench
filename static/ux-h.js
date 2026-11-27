(() => {
'use strict';
/* 第十四轮 H：新对话首页、模型与权限浮层、“+”菜单、发送键与启动偏好、顶栏图标与悬停提示、侧栏“新对话”与账号行、
   加载骨架、⌘N。钩子都串在原有处理之后；app.js 只加了 PreLoad / Loading / Controls / Boot 几个钩子和几项导出。
   设置入口调 CWB.openSettings（开发线 J 定义），没有时回退到右栏“扩展”或提示。 */
const C = window.CWB;
if (!C) return;
const {S, $, esc, api, toast} = C;
const store = C.store || {get: (k, d) => d, set() {}};
const body = document.body, main = $('#main'), dock = $('#dock'), composer = $('#composer'), input = $('#input'),
  feed = $('#feed'), scroll = $('#scroll'), bar = composer.querySelector('.bar'), side = $('#side');
const chain = (name, fn) => { const prev = C[name]; C[name] = (...a) => { try { if (prev) prev(...a); } finally { try { fn(...a); } catch (e) { console.error(e); } } }; };
const RM = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const base = p => (p || '').split('/').filter(Boolean).pop() || p || '';
const normP = p => String(p || '').replace(/\/+$/, '').replace(/^\/private(?=\/(tmp|var|etc)\/)/, '');  // /tmp 与 /private/tmp 是同一个
const samePath = (a, b) => !!a && !!b && normP(a) === normP(b);
const tilde = p => (S.home && p && p.startsWith(S.home + '/') ? '~' + p.slice(S.home.length) : p || '');
const DEFAULT_PHRASES = ['继续', '按方案执行', '先别改，只给建议', '总结一下刚才做了什么'];
function mk(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
function btn(cls, html, label) { const b = mk('button', cls, html); b.type = 'button'; if (label) { b.title = label; b.setAttribute('aria-label', label); } return b; }
const svg = (d, cls, vb) => `<svg class="h-i${cls ? ' ' + cls : ''}" viewBox="${vb || '0 0 16 16'}" aria-hidden="true">${d}</svg>`;
const IC = {
  plus: svg('<path d="M8 3.5v9M3.5 8h9"/>'),
  chev: svg('<path d="M2.6 3.9 5 6.3l2.4-2.4"/>', 'h-chev', '0 0 10 10'),
  right: svg('<path d="M6.2 3.6 10.6 8l-4.4 4.4"/>', 'h-sm'),
  check: svg('<path d="M3.4 8.4 6.6 11.4 12.6 4.8"/>', 'h-sm'),
  bolt: svg('<path d="M9.2 1.6 3.4 9.1h4.1l-.8 5.3 5.9-7.6H8.4z"/>', 'h-fill h-bolt'),
  shield: svg('<path d="M8 1.9 3.1 3.7v3.9c0 3.1 2.1 5.4 4.9 6.5 2.8-1.1 4.9-3.4 4.9-6.5V3.7z"/>'),
  folder: svg('<path d="M2.2 4.4c0-.7.5-1.2 1.2-1.2h2.8l1.4 1.6h5c.7 0 1.2.5 1.2 1.2v5.8c0 .7-.5 1.2-1.2 1.2H3.4c-.7 0-1.2-.5-1.2-1.2z"/>'),
  folderPlus: svg('<path d="M2.2 4.4c0-.7.5-1.2 1.2-1.2h2.8l1.4 1.6h5c.7 0 1.2.5 1.2 1.2v5.8c0 .7-.5 1.2-1.2 1.2H3.4c-.7 0-1.2-.5-1.2-1.2z"/><path d="M8 7.2v3.6M6.2 9h3.6"/>'),
  gear: svg('<circle cx="8" cy="8" r="2.2"/><path d="M8 1.8v1.6M8 12.6v1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M1.8 8h1.6M12.6 8h1.6M3.6 12.4l1.1-1.1M11.3 4.7l1.1-1.1"/>'),
  toc: svg('<path d="M3 4h10M3 8h10M3 12h6"/>'),
  chg: svg('<path d="M4 2.5h6l2.5 2.5v8.5H4z"/><path d="M6.5 8h3M8 6.5v3"/>'),
  review: svg('<path d="M2 8s2.2-4 6-4 6 4 6 4-2.2 4-6 4-6-4-6-4z"/><circle cx="8" cy="8" r="1.8"/>'),
  task: svg('<rect x="2.6" y="5" width="10.8" height="8.4" rx="2.4"/><path d="M8 2.2V5M6 8.8v1M10 8.8v1"/>'),
  exp: svg('<path d="M8 10V2.6M5.2 5.3 8 2.5l2.8 2.8"/><path d="M3 9.6v2.9c0 .6.4 1 1 1h8c.6 0 1-.4 1-1V9.6"/>'),
  more: svg('<circle cx="3.8" cy="8" r="1.05"/><circle cx="8" cy="8" r="1.05"/><circle cx="12.2" cy="8" r="1.05"/>', 'h-fill'),
  compose: svg('<path d="M13.4 8.7v3.6c0 .7-.6 1.2-1.2 1.2H3.8c-.7 0-1.2-.5-1.2-1.2V3.8c0-.7.5-1.2 1.2-1.2h3.6"/><path d="M11.5 2.2 13.8 4.5 8.3 10l-2.7.4.4-2.7z"/>'),
  clip: svg('<path d="M13.2 7.6 8.1 12.7a3.3 3.3 0 0 1-4.7-4.7l5.4-5.4a2.2 2.2 0 0 1 3.1 3.1L6.6 11a1.1 1.1 0 0 1-1.6-1.6l4.9-4.9"/>'),
  chat: svg('<path d="M2.5 4.2c0-.9.7-1.6 1.6-1.6h7.8c.9 0 1.6.7 1.6 1.6v5.2c0 .9-.7 1.6-1.6 1.6H7.2l-2.8 2.4V11h-.3c-.9 0-1.6-.7-1.6-1.6z"/><path d="M5.4 5.6h5.2M5.4 7.9h3.4"/>'),
  skill: svg('<path d="M8 1.9 13.3 5v6L8 14.1 2.7 11V5z"/><path d="M2.7 5 8 8l5.3-3M8 8v6.1"/>'),
  manage: svg('<path d="M2.5 4.5h6M11.5 4.5h2M2.5 11.5h2M7.5 11.5h6"/><circle cx="10" cy="4.5" r="1.5"/><circle cx="6" cy="11.5" r="1.5"/>'),
};
// 侧栏账号行的 Claude 像素机器人（按今日花费微调眼神）；首页使用 Claude 官方星芒标志。
const PIXBOT = (cls, moodIdx = 0) => {
  const eyes = moodIdx <= 0
    ? '<path class="pb-e" d="M4.2 3.9l2.3 1.5-2.3 1.5M17.8 3.9l-2.3 1.5 2.3 1.5"/>'
    : moodIdx === 1
    ? '<rect class="pb-f" x="4.8" y="3.9" width="2.2" height="3.2"/><rect class="pb-f" x="15" y="3.9" width="2.2" height="3.2"/>'
    : moodIdx === 2
    ? '<rect class="pb-f" x="4.4" y="5.1" width="3" height="1.4"/><rect class="pb-f" x="14.6" y="5.1" width="3" height="1.4"/>'
    : moodIdx <= 4
    ? '<path class="pb-e" d="M4.2 3.9l2.3 1.5-2.3 1.5M17.8 3.9l-2.3 1.5 2.3 1.5"/>'
    : '<path class="pb-e" d="M4.4 4.1l2.8 2.8M7.2 4.1l-2.8 2.8M14.8 4.1l2.8 2.8M17.6 4.1l-2.8 2.8"/>';
  return `<svg class="${cls}" viewBox="0 0 22 18" aria-hidden="true">` +
    '<path class="pb-b" d="M2 0h18v4h2v5h-2v9h-2v-5h-2v5h-2v-5H8v5H6v-5H4v5H2V9H0V4h2z"/>' +
    eyes + '</svg>';
};
// Claude 官方品牌资源：来源见 static/provider-icons/README.md
const CLAUDE_LOGO = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" class="h-provider-logo" aria-hidden="true"> <path fill="#D97757" d="M11.376 24L10.776 23.544L10.44 22.8L10.776 21.312L11.16 19.392L11.472 17.856L11.76 15.96 L11.928 15.336L11.904 15.288L11.784 15.312L10.344 17.28L8.16 20.232L6.432 22.056 L6.024 22.224L5.304 21.864L5.376 21.192L5.784 20.616L8.16 17.568L9.6 15.672L10.536 14.592 L10.512 14.448H10.464L4.128 18.576L3 18.72L2.496 18.264L2.568 17.52L2.808 17.28L4.704 15.96 L9.432 13.32L9.504 13.08L9.432 12.96H9.192L8.4 12.912L5.712 12.84L3.384 12.744L1.104 12.624 L0.528 12.504L0 11.784L0.048 11.424L0.528 11.112L1.224 11.16L2.736 11.28L5.016 11.424 L6.672 11.52L9.12 11.784H9.504L9.552 11.616L9.432 11.52L9.336 11.424L6.96 9.84L4.416 8.16 L3.072 7.176L2.352 6.672L1.992 6.216L1.848 5.208L2.496 4.488L3.384 4.56L3.6 4.608 L4.488 5.304L6.384 6.768L8.88 8.616L9.24 8.904L9.408 8.808V8.736L9.24 8.472L7.896 6.024 L6.456 3.528L5.808 2.496L5.64 1.872C5.576 1.656 5.544 1.416 5.544 1.152L6.288 0.144001 L6.696 0L7.704 0.144001L8.112 0.504001L8.736 1.92L9.72 4.152L11.28 7.176L11.736 8.088 L11.976 8.904L12.072 9.168H12.24V9.024L12.36 7.296L12.6 5.208L12.84 2.52L12.912 1.752 L13.296 0.840001L14.04 0.360001L14.616 0.624001L15.096 1.32L15.024 1.752L14.76 3.6 L14.184 6.504L13.824 8.472H14.04L14.28 8.208L15.264 6.912L16.92 4.848L17.64 4.032 L18.504 3.12L19.056 2.688H20.088L20.832 3.816L20.496 4.992L19.44 6.336L18.552 7.464 L17.28 9.168L16.512 10.536L16.584 10.632H16.752L19.608 10.008L21.168 9.744L22.992 9.432 L23.832 9.816L23.928 10.2L23.592 11.016L21.624 11.496L19.32 11.952L15.888 12.768 L15.84 12.792L15.888 12.864L17.424 13.008L18.096 13.056H19.728L22.752 13.272L23.544 13.8 L24 14.424L23.928 14.928L22.704 15.528L21.072 15.144L17.232 14.232L15.936 13.92H15.744 V14.016L16.848 15.096L18.84 16.896L21.36 19.224L21.48 19.8L21.168 20.28L20.832 20.232 L18.624 18.552L17.76 17.808L15.84 16.2H15.72V16.368L16.152 17.016L18.504 20.544 L18.624 21.624L18.456 21.96L17.832 22.176L17.184 22.056L15.792 20.136L14.376 17.952 L13.224 16.008L13.104 16.104L12.408 23.352L12.096 23.712L11.376 24Z"/> </svg>';
const HOME_PH = '描述你想做的事，输入 / 使用命令，@ 引用文件';
const PH = input.getAttribute('placeholder') || '输入消息';

/* 首页状态：opts 只记这次在首页明确选过的项（没选的由后台按“默认设置 < 项目设置”合并） */
const H = {opts: {}, cwd: '', prefs: null, cli: null, home: false, booting: !S.booted || !!S.loading, loadingShown: false, creating: false, moreOpen: null};

/* ================= 1. 选项：模型、思考强度、权限、Fast ================= */
const EFF = [['', '默认'], ['low', '低'], ['medium', '中'], ['high', '高'], ['xhigh', '超高'], ['max', '最高']];
const MODES = [['default', '默认', '按 CLI 全局设置'], ['auto mode', '自动', '安全的操作直接做，有风险的再问'], ['manual mode', '手动确认', '每一步都问你'],
  ['accept edits', '自动接受编辑', '改文件不问，其他操作照常确认'], ['plan mode', '规划模式', '只读，先出方案再动手'], ['bypass permissions', '跳过确认', '所有操作都不问，风险最高']];
const REC_DESC = {'claude-opus-5-5': '日常主力，复杂任务首选', 'claude-fable-5-1': '最强推理，适合难题，费用较高',
  'claude-sonnet-5-5': '更快更省，适合大多数改动', 'claude-haiku-5-5': '最快，适合简单任务'};
const fastById = id => /opus-5|opus-4-8/.test(String(id || ''));
/* 后台没给 group / desc / fast 时退化：前 4 个当推荐，fast 按 id 判断 */
function models() {
  const list = (S.models && S.models.list) || [], grouped = list.some(m => m.group);
  return list.map((m, i) => ({id: m.id, label: m.label || m.id, desc: m.desc || REC_DESC[m.id] || '',
    group: grouped ? (m.group === 'rec' ? 'rec' : 'more') : (i < 4 ? 'rec' : 'more'), fast: 'fast' in m ? !!m.fast : fastById(m.id)}));
}
function defaultId() {
  const M = S.models || {};
  if (M.defaultId) return M.defaultId;
  const hit = (M.list || []).find(m => m.label === M.default || m.id === M.default);
  return hit ? hit.id : '';
}
function modelValue(v) {
  if (!v) return '';
  const list = (S.models && S.models.list) || [];
  if (list.some(m => m.id === v)) return v;
  const hit = list.find(m => m.id.toLowerCase().includes(String(v).toLowerCase()));
  return hit ? hit.id : v;
}
const labelOf = id => { const m = models().find(x => x.id === id); return m ? m.label : (id || ''); };
const fastOk = () => false;  // 第十四轮补（11-11）：Fast 功能去掉（公司网关对 Fast 请求返回 402）
/* 当前生效的选项：有对话按对话（与 syncControls 同一规则），首页按首页选择 + 默认设置 */
function cur() {
  if (S.cur) {
    /* 第十四轮 FH：模式用 app.js 的 curMode（回复中沿用最后一次解析到的模式，不再跳成“默认权限”） */
    const po = S.opts || {}, st = S.runner && S.runner.alive ? S.runner : null, m = C.curMode ? C.curMode() : ((st && st.mode) || po.mode || 'default');
    return {model: modelValue(po.model), effort: po.effort || (st && st.effort) || '', mode: MODES.some(x => x[0] === m) ? m : 'default', fast: !!po.fast};
  }
  const d = (H.prefs && H.prefs.defaults) || {}, pick = k => (k in H.opts ? H.opts[k] : d[k]);
  const m = pick('mode') || 'default';
  return {model: modelValue(pick('model') || ''), effort: pick('effort') || '', mode: MODES.some(x => x[0] === m) ? m : 'default', fast: !!pick('fast')};
}
const effModel = c => c.model || defaultId();
function explicitOpts() { const o = {}; for (const k of ['model', 'effort', 'mode', 'fast']) if (k in H.opts) o[k] = H.opts[k]; return o; }
async function setOpt(o) {
  if (!S.cur) { Object.assign(H.opts, o); renderPills(true); return; }
  const sid = S.cur, prev = S.opts || {}, busy = !!(S.runner && S.runner.alive && S.runner.busy);
  S.opts = Object.assign({}, prev, o);  // 先按选择显示，接口返回后以后台为准
  renderPills(true);
  let ok = false, pending = busy;
  try { const d = await api('control', Object.assign({sid}, o)); if (S.cur === sid) S.opts = d.opts || {}; ok = true; if ('pending' in d) pending = !!d.pending; }
  catch (e) { if (S.cur === sid) S.opts = prev; toast(e.message); }
  if (S.cur !== sid) return;
  C.refreshRunner();
  renderPills(true);
  if (!ok) return;
  const when = pending ? '这一轮和后台任务结束后' : '已';  // 11-14：以后台返回为准（只有后台子代理在跑时也要等）
  if ('model' in o) toast(when + '换成 ' + (labelOf(o.model) || '默认模型'));
  else if ('fast' in o) toast(!!S.opts.fast !== !!o.fast ? '这个对话暂时不能切换 Fast 模式' : o.fast ? when + '开启 Fast 模式' : '已关闭 Fast 模式');
}

/* ================= 2. 浮层（向上弹出、限高滚动、键盘上下移动、Esc 关闭、子菜单） ================= */
let P = null, SUB = null, subT = 0;
function navItems(root) {
  const out = [];
  for (const x of root.querySelectorAll('[data-nav]')) {
    if (x.disabled || x.offsetParent === null) continue;
    const g = x.closest('.h-seg');
    if (g) {  // 分段按钮整组算一站：上下键停在当前那一个，左右键在组内移动
      if (out.some(y => y.closest('.h-seg') === g)) continue;
      out.push(g.contains(document.activeElement) ? document.activeElement : (g.querySelector('.on') || x));
    } else out.push(x);
  }
  return out;
}
function moveFocus(root, d) {
  const items = navItems(root);
  if (!items.length) return;
  const a = document.activeElement;
  let i = items.findIndex(x => x === a || (x.closest('.h-seg') && x.closest('.h-seg').contains(a)));
  i = i < 0 ? (d > 0 ? 0 : items.length - 1) : (i + d + items.length) % items.length;
  items[i].focus(); items[i].scrollIntoView({block: 'nearest'});
}
function popKey(e, root, isSub) {
  const k = e.key, a = document.activeElement;
  if (k === 'ArrowDown' || k === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); moveFocus(root, k === 'ArrowDown' ? 1 : -1); return; }
  if (k === 'Home' || k === 'End') { e.preventDefault(); const it = navItems(root), t = k === 'Home' ? it[0] : it[it.length - 1]; if (t) t.focus(); return; }
  if (k === 'Escape') { e.preventDefault(); e.stopPropagation(); if (isSub && SUB) { const o = SUB.owner; closeSub(); o.focus(); } else closePop(true); return; }
  if (k === 'Tab') { e.preventDefault(); closePop(true); return; }
  if (k === 'ArrowLeft' || k === 'ArrowRight') {
    const seg = a && a.closest && a.closest('.h-seg');
    if (seg) { e.preventDefault(); const bs = [...seg.querySelectorAll('button')], i = bs.indexOf(a); bs[(i + (k === 'ArrowRight' ? 1 : -1) + bs.length) % bs.length].focus(); return; }
    if (k === 'ArrowRight' && !isSub && a && a.dataset && a.dataset.sub) { e.preventDefault(); openSub(a, true); return; }
    if (k === 'ArrowLeft' && isSub && SUB) { e.preventDefault(); const o = SUB.owner; closeSub(); o.focus(); }
  }
}
/* 第十四轮 FH：向上弹出时不盖住顶栏：上沿最多到顶栏下面 8px，最大高度按按钮上方到这里的空间算 */
function topLimit(m) {
  const t = document.getElementById('top');
  if (!t || t.offsetParent === null) return m;
  const b = t.getBoundingClientRect().bottom;
  return b > 0 ? Math.round(b) + m : m;
}
function placePop(el, anchor, opt) {
  const r = anchor.getBoundingClientRect(), gap = 6, m = 8, maxH = opt.maxH || 440, top = topLimit(m);
  el.style.maxHeight = 'none'; el.style.left = '0px'; el.style.top = '0px';
  const above = r.top - gap - top, below = innerHeight - r.bottom - gap - m, want = Math.min(el.scrollHeight + 2, maxH);
  const up = opt.dir === 'down' ? (below < want && above > below) : !(above < want && below > above);
  el.style.maxHeight = Math.max(120, Math.min(maxH, up ? above : below)) + 'px';
  const w = el.offsetWidth, h = el.offsetHeight;
  const x = Math.max(m, Math.min(opt.alignRight ? r.right - w : r.left, innerWidth - w - m));
  const y = up ? Math.max(m, r.top - gap - h) : Math.min(innerHeight - h - m, r.bottom + gap);  // 高度已按 above 限住，上沿不会越过顶栏
  el.style.left = Math.round(x) + 'px'; el.style.top = Math.round(y) + 'px';
  el.dataset.dir = up ? 'up' : 'down';
}
function openPop(kind, anchor, render, opt = {}) {
  closePop(false); C.closeMenu(); hideTip();
  const el = mk('div', 'h-pop ' + (opt.cls || ''));
  el.setAttribute('role', opt.role || 'menu');
  el.addEventListener('keydown', e => popKey(e, el, false));
  document.body.appendChild(el);
  P = {kind, el, anchor, opt, render};
  render(el);
  anchor.classList.add('open'); anchor.setAttribute('aria-expanded', 'true');
  placePop(el, anchor, opt);
  const f = (opt.focusSel && el.querySelector(opt.focusSel)) || el.querySelector('[data-nav][aria-checked=true]') || navItems(el)[0];
  if (f) f.focus({preventScroll: true});
  const ml = el.querySelector('.h-mlist');
  if (f && ml && ml.contains(f)) f.scrollIntoView({block: 'nearest'});  // 选中的是“更多模型”里的：把它滚进模型列表的可见处
  return el;
}
function rerender() {  // 内容变了：原地重画，焦点和滚动位置留在原处
  if (!P) return;
  const scr = () => P.el.querySelector('.h-mlist') || P.el;  // 模型浮层里滚动的是模型列表那一块
  const k = document.activeElement && P.el.contains(document.activeElement) ? document.activeElement.dataset.key : null, st = scr().scrollTop;
  P.el.innerHTML = ''; P.render(P.el);
  placePop(P.el, P.anchor, P.opt);
  scr().scrollTop = st;
  const f = k && P.el.querySelector(`[data-key="${CSS.escape(k)}"]`);
  if (f) f.focus({preventScroll: true});
}
function closePop(refocus) {
  closeSub();
  if (!P) return;
  const p = P; P = null;
  p.el.remove();
  for (const a of [p.anchor, p.opt.also]) if (a) { a.classList.remove('open'); a.setAttribute('aria-expanded', 'false'); }
  if (refocus && p.anchor.isConnected) p.anchor.focus({preventScroll: true});
}
function placeSub(el, owner) {
  const r = owner.getBoundingClientRect(), pr = P ? P.el.getBoundingClientRect() : r, m = 8, top = Math.min(topLimit(m), r.top - 7);
  el.style.maxHeight = Math.min(420, innerHeight - top - m) + 'px';
  const w = el.offsetWidth, h = el.offsetHeight;
  let x = pr.right + 4;
  if (x + w > innerWidth - m) x = Math.max(m, pr.left - w - 4);
  el.style.left = Math.round(x) + 'px';
  el.style.top = Math.round(Math.max(top, Math.min(r.top - 7, innerHeight - h - m))) + 'px';
}
/* 第十四轮 FH：子菜单像 macOS 菜单那样悬停约 150ms 展开；路过（不到 150ms 就移开）不展开；
   移到别的菜单项 150ms 后收起，期间进了子菜单就不收。点击、→ 键照旧立即展开 */
const HOVER_MS = 150;
let subFor = null;  // 正在等着展开子菜单的那一项
function hoverSub(b) {
  if (SUB && SUB.owner === b) { clearTimeout(subT); subFor = null; return; }  // 已经开着（也取消正在等的收起）
  if (subFor === b) return;
  clearTimeout(subT); subFor = b;
  subT = setTimeout(() => { subFor = null; if (P && b.isConnected) openSub(b, false); }, HOVER_MS);
}
function hoverOther() {
  if (subFor) { clearTimeout(subT); subFor = null; }
  if (SUB) { clearTimeout(subT); subT = setTimeout(closeSub, HOVER_MS); }
}
function openSub(owner, focus) {
  clearTimeout(subT); subFor = null;
  if (!(SUB && SUB.owner === owner)) {
    closeSub();
    const el = mk('div', 'h-pop h-subpop');
    el.setAttribute('role', 'menu');
    el.addEventListener('keydown', e => popKey(e, el, true));
    el.addEventListener('mouseenter', () => { clearTimeout(subT); subFor = null; });
    document.body.appendChild(el);
    SUB = {owner, el};
    owner.classList.add('open'); owner.setAttribute('aria-expanded', 'true');
    owner._sub(el);
    placeSub(el, owner);
  }
  SUB.want = !!focus;
  if (focus) { const f = navItems(SUB.el)[0]; if (f) f.focus(); }
}
function closeSub() {
  clearTimeout(subT); subFor = null;
  if (!SUB) return;
  SUB.el.remove(); SUB.owner.classList.remove('open'); SUB.owner.setAttribute('aria-expanded', 'false'); SUB = null;
}
/* 菜单项：{label, desc, icon, tag, check, sub, fn, title, cls, key} */
function mi(o) {
  const b = btn('h-mi' + (o.cls ? ' ' + o.cls : ''));
  b.dataset.nav = ''; if (o.key) b.dataset.key = o.key;
  b.setAttribute('role', o.check != null ? 'menuitemradio' : 'menuitem');
  if (o.title) b.title = o.title;
  b.innerHTML = (o.icon ? `<span class="h-mic">${o.icon}</span>` : '') +
    `<span class="h-mt"><span class="h-mn">${esc(o.label)}</span>${o.desc ? `<small>${esc(o.desc)}</small>` : ''}</span>` +
    (o.tag ? `<span class="h-tag">${esc(o.tag)}</span>` : '') + (o.sub ? `<span class="h-arr">${IC.right}</span>` : '') +
    (o.check != null ? `<span class="h-ck">${o.check ? IC.check : ''}</span>` : '');
  if (o.check != null) b.setAttribute('aria-checked', o.check ? 'true' : 'false');
  const inSub = () => !!b.closest('.h-subpop');
  if (o.sub) {
    b.dataset.sub = '1'; b._sub = o.sub; b.setAttribute('aria-haspopup', 'menu'); b.setAttribute('aria-expanded', 'false');
    b.onclick = () => openSub(b, true);
    b.onmouseenter = b.onmousemove = () => hoverSub(b);  // mousemove：菜单弹出时指针正好停在这一项上，一动就开始计时
    b.onmouseleave = () => { if (subFor === b) { clearTimeout(subT); subFor = null; } };
  } else {
    b.onclick = e => o.fn(e);
    b.onmouseenter = () => { if (!inSub()) hoverOther(); };
  }
  return b;
}
const sepEl = () => mk('div', 'h-sep');
const noteEl = t => mk('div', 'h-note', esc(t));
document.addEventListener('mousedown', e => {
  if (!P) return;
  const t = e.target;
  if (P.el.contains(t) || (SUB && SUB.el.contains(t))) return;
  if (P.anchor.contains(t) || (P.opt.also && P.opt.also.contains(t))) return;  // 交给按钮自己的点击来开关
  closePop(false);
}, true);
addEventListener('resize', () => closePop(false));
addEventListener('blur', () => closePop(false));

/* ================= 3. 输入栏：“+”、模型药丸、思考强度、权限 ================= */
const plusBtn = btn('h-plus', IC.plus, '添加');
plusBtn.id = 'plusBtn'; plusBtn.setAttribute('aria-haspopup', 'menu'); plusBtn.dataset.tip = '添加文件、常用语、技能';
const mPill = btn('h-pill'), ePill = btn('h-pill'), oPill = btn('h-pill');
mPill.id = 'modelPill'; ePill.id = 'effortPill'; oPill.id = 'modePill';
mPill.title = '选择模型'; ePill.title = '思考强度'; oPill.title = '权限模式';
for (const b of [mPill, ePill, oPill]) b.setAttribute('aria-haspopup', 'dialog');
bar.prepend(plusBtn, mPill, ePill, oPill);
let pillKey = '';
/* 11-14：回复进行中换的模型、思考强度要等空闲才生效。后台运行状态的 pending 给出正在用的和已选的；没有待生效的返回 null */
const effLabel = v => (EFF.find(x => x[0] === (v || '')) || EFF[0])[1];
function pend() {
  const st = S.cur && S.runner && S.runner.alive ? S.runner : null, p = st && st.pending;
  if (!p || typeof p !== 'object') return null;
  const m = p.model !== p.run_model, e = (p.effort || '') !== (p.run_effort || '');
  return m || e ? Object.assign({m, e}, p) : null;
}
function pendText(p) {
  const a = [];
  if (p.m) a.push(labelOf(p.model));
  if (p.e) a.push('思考 ' + effLabel(p.effort));
  return a.join(' · ');
}
async function applyNow() {
  const sid = S.cur, st = S.runner || {}, what = [];
  const p = pend();
  closePop(true);
  if (!p) return;
  if (st.busy) what.push('正在进行的回复');
  if (st.bg) what.push(st.bg + ' 个后台任务');
  const msg = '立即换成 ' + pendText(p) + '？\n' + (what.length ? '会停掉' + what.join('和') + '，没做完的部分会丢失，' : '') +
    '已经写进对话的内容都保留，切换后可以发“继续”接着做。';
  if (!confirm(msg)) return;
  try { await api('apply-opts', {sid}); toast('正在换成 ' + pendText(p)); }
  catch (e) { toast(e.message); }
  if (S.cur === sid) C.refreshRunner();
}
function renderPills(force) {
  const c = cur(), id = effModel(c), fast = c.fast && fastOk(id), p = pend();
  let ml = c.model ? labelOf(c.model) : ((S.models && S.models.default) || labelOf(id) || '默认模型');
  let el = (EFF.find(x => x[0] === c.effort) || EFF[0])[1];
  if (p && p.m) ml = labelOf(p.run_model);  // 按钮上显示正在用的，箭头后面是已选的
  if (p && p.e) el = effLabel(p.run_effort);
  const mo = MODES.find(x => x[0] === c.mode) || MODES[0];
  /* 第十四轮 FH：首页选的是“默认”（没选或选了默认）时，显示 CLI 全局默认权限模式的名称（prefs 返回的 cli.mode），
     免得建好对话后从“默认权限”变成“自动”，像是被改了；没有这个字段时照旧显示“默认权限” */
  const cm = mo[0] === 'default' && !S.cur ? cliMode() : null, shown = cm || mo;
  const key = [ml, fast, el, mo[0], shown[0], p ? JSON.stringify(p) : ''].join('|');
  if (key === pillKey && !force) return;
  pillKey = key;
  const later = '这一轮和后台任务结束后生效';
  mPill.innerHTML = (fast ? IC.bolt : '') + `<span class="h-pl">${esc(ml)}</span>` +
    (p && p.m ? `<span class="h-pend">→ ${esc(labelOf(p.model))}</span>` : '') + IC.chev;
  mPill.classList.toggle('fast', fast);
  mPill.setAttribute('aria-label', '模型：' + ml + (p && p.m ? '，已选 ' + labelOf(p.model) + '，' + later : '') + (fast ? '，Fast 模式已开' : ''));
  mPill.title = p && p.m ? '已选 ' + labelOf(p.model) + '，' + later : '';
  ePill.innerHTML = `<span class="h-pl">思考 ${esc(el)}</span>` + (p && p.e ? `<span class="h-pend">→ ${esc(effLabel(p.effort))}</span>` : '') + IC.chev;
  ePill.setAttribute('aria-label', '思考强度：' + el + (p && p.e ? '，已选 ' + effLabel(p.effort) + '，' + later : ''));
  ePill.title = p && p.e ? '已选 ' + effLabel(p.effort) + '，' + later : '';
  oPill.innerHTML = IC.shield + `<span class="h-pl">${esc(shown[0] === 'default' ? '默认权限' : shown[1])}</span>` + IC.chev;
  oPill.classList.toggle('warn', shown[0] === 'bypass permissions');
  oPill.setAttribute('aria-label', '权限模式：' + (cm ? '默认（' + cm[1] + '）' : mo[1]));
  if (P && (P.kind === 'model' || P.kind === 'mode')) rerender();
}
function renderModelPop(el) {
  const c = cur(), id = effModel(c), list = models(), rec = list.filter(m => m.group === 'rec'), more = list.filter(m => m.group === 'more');
  el.setAttribute('aria-label', '模型与思考强度');
  /* 第十四轮 FH：模型列表（推荐 + 更多）单独滚动，思考强度和 Fast 固定在浮层底部，展开“更多模型”也始终看得见 */
  const mlist = mk('div', 'h-mlist'), foot = mk('div', 'h-mfoot'), p = pend();
  if (p) {  // 11-14：已选未生效：说明现在用的是什么，可以立即切换
    const now = [p.m ? labelOf(p.run_model) : '', p.e ? '思考 ' + effLabel(p.run_effort) : ''].filter(Boolean).join(' · ');
    const bar = mk('div', 'h-pendbar', `<span>这一轮和后台任务结束后换成 ${esc(pendText(p))}<br><em>现在仍是 ${esc(now)}</em></span>`);
    const go = btn('h-pendgo', '立即切换');
    go.dataset.nav = ''; go.dataset.key = 'apply-now'; go.onclick = applyNow;
    bar.appendChild(go);
    el.append(bar);
  }
  el.append(mlist, foot);
  for (const m of rec) {
    mlist.appendChild(mi({label: m.label, desc: m.desc, check: m.id === id, cls: 'h-model-it', key: 'm:' + m.id, fn: () => pickModel(m.id, c)}));
  }
  if (more.length) {
    const open = H.moreOpen == null ? more.some(m => m.id === id) : H.moreOpen;
    const t = btn('h-more', `<span>更多模型</span><span class="h-mc">${IC.right}</span>`);
    t.dataset.nav = ''; t.dataset.key = 'more'; t.setAttribute('aria-expanded', open ? 'true' : 'false');
    t.onclick = () => {
      H.moreOpen = !open; rerender();
      /* 展开后把更多模型滚进来，最多滚到“更多模型”这一行贴着列表上沿 */
      const l = P && P.el.querySelector('.h-mlist'), g = l && l.querySelector('.h-grid:not([hidden])'), tb = l && l.querySelector('.h-more');
      if (g && tb) {
        const lr = l.getBoundingClientRect(), need = g.getBoundingClientRect().bottom + 4 - lr.bottom, room = tb.getBoundingClientRect().top - lr.top - 4;
        if (need > 0) l.scrollTop += Math.max(0, Math.min(need, room));
      }
    };
    const g = mk('div', 'h-grid'); g.hidden = !open;
    for (const m of more) {
      const b = btn('h-gi' + (m.id === id ? ' on' : ''), `<span>${esc(m.label)}</span>${m.id === id ? IC.check : ''}`);
      b.dataset.nav = ''; b.dataset.key = 'm:' + m.id; b.title = m.desc || m.id;
      b.setAttribute('role', 'menuitemradio'); b.setAttribute('aria-checked', m.id === id ? 'true' : 'false');
      b.onclick = () => pickModel(m.id, c);
      g.appendChild(b);
    }
    mlist.append(t, g);
  }
  foot.appendChild(sepEl());
  const er = mk('div', 'h-row', '<span class="h-lb" id="hEffLb">思考强度</span>'), seg = mk('div', 'h-seg');
  seg.setAttribute('role', 'radiogroup'); seg.setAttribute('aria-labelledby', 'hEffLb');
  for (const [v, l] of EFF) {
    const b = btn(v === c.effort ? 'on' : '', esc(l));
    b.dataset.nav = ''; b.dataset.key = 'e:' + v; b.setAttribute('role', 'radio'); b.setAttribute('aria-checked', v === c.effort ? 'true' : 'false');
    b.onclick = () => { if (v !== c.effort) setOpt({effort: v}); };
    seg.appendChild(b);
  }
  er.appendChild(seg); foot.appendChild(er);
  /* 第十四轮补（11-11）：Fast 开关去掉 */
}
function pickModel(id, c) {
  closePop(true);
  if (id === (c.model || defaultId())) return;
  setOpt({model: id});
}
function openModelPop(from, focusSel) {
  if (P && P.kind === 'model') {  // 已经开着：点另一个药丸只移动焦点，点同一个就关上
    if (P.anchor === from || P.opt.also === from) { closePop(true); return; }
    const f = P.el.querySelector(focusSel || '[data-nav][aria-checked=true]'); if (f) f.focus(); return;
  }
  H.moreOpen = null;
  openPop('model', mPill, renderModelPop, {cls: 'h-model', role: 'dialog', also: from === ePill ? ePill : null, focusSel, maxH: 560});
  if (from === ePill) { ePill.classList.add('open'); ePill.setAttribute('aria-expanded', 'true'); }
}
mPill.onclick = () => openModelPop(mPill);
ePill.onclick = () => openModelPop(ePill, '.h-seg .on');
/* CLI 全局默认权限模式（后台 prefs 的 cli.mode，已换成 #mode 的取值）；是“默认”或没有时返回 null */
function cliMode() {
  const v = H.cli && H.cli.mode;
  return v && v !== 'default' ? MODES.find(x => x[0] === v) || null : null;
}
/* 第十五轮 F1：每项一句白话说明；规划模式单独放最上面，和其余几档之间一条分隔线 */
const MODE_TIP = {'plan mode': '先出计划，不改文件', 'manual mode': '每次改文件前问我', 'accept edits': '改文件不问，执行命令仍问',
  'auto mode': '有风险时才问', 'bypass permissions': '全部不问'};
function renderModePop(el) {
  const c = cur(), cm = cliMode();
  const rank = ['plan mode', null, 'default', 'manual mode', 'accept edits', 'auto mode', 'bypass permissions'];  // 其余几档从严到松
  const order = rank.map(v => (v == null ? null : MODES.find(x => x[0] === v))).filter(x => x !== undefined);
  for (const m of order) {
    if (!m) { el.appendChild(sepEl()); continue; }
    const [v, l, d0] = m, d1 = MODE_TIP[v] || d0;
    const d = v === 'default' && cm ? d1 + '（' + cm[1] + '）' : d1;
    el.appendChild(mi({label: l, desc: d, check: v === c.mode, key: 'o:' + v, cls: v === 'bypass permissions' ? 'warn' : '',
      fn: () => { closePop(true); if (v !== c.mode) setOpt({mode: v}); }}));
  }
}
oPill.onclick = () => (P && P.kind === 'mode') ? closePop(true) : openPop('mode', oPill, renderModePop, {cls: 'h-mode'});

/* “+”菜单：添加文件或图片、常用语、技能、管理技能与 MCP */
function renderPlus(el) {
  el.append(
    mi({label: '添加文件或图片', icon: IC.clip, fn: () => { closePop(false); input.focus(); $('#attach').click(); }}),
    /* 第十五轮 F1：会话内追加文件夹（只对这个对话有效，ux-k.js 的 CWB.addDir 调 add-dir） */
    ...(S.cur && C.addDir ? [mi({label: '添加文件夹…', desc: '只对这个对话有效', icon: IC.folderPlus, fn: () => { closePop(false); C.addDir(); }})] : []),
    mi({label: '常用语', icon: IC.chat, sub: renderPhrases}),
    mi({label: '技能', icon: IC.skill, sub: renderSkills}),
    sepEl(),
    mi({label: '管理扩展…', icon: IC.manage, fn: () => openSettings('ext')}),
  );
}
plusBtn.onclick = () => (P && P.kind === 'plus') ? closePop(true) : openPop('plus', plusBtn, renderPlus, {cls: 'h-plusm'});
function renderPhrases(el) {
  for (const p of S.phrases) el.appendChild(mi({label: p, title: '点击直接发送，⌥ 点击只插入', cls: 'h-one',
    fn: e => { closePop(false); if (e.altKey) C.insertText(p); else C.sendText(p); }}));
  el.append(sepEl(), mi({label: '编辑常用语…', fn: editPhrases}));
}
async function editPhrases() {
  closePop(false);
  const v = await C.askText('常用语（一行一条）', S.phrases.join('\n'), true);
  if (v == null) return;
  S.phrases = v.split('\n').map(x => x.trim()).filter(Boolean);
  if (!S.phrases.length) S.phrases = DEFAULT_PHRASES.slice();
  store.set('phrases', S.phrases);
  renderHome();
}
let skills = null;  // {cwd, at, list}
const SCOPE = {project: '项目', user: '个人', plugin: '插件'};
async function renderSkills(el) {
  const cwd = homeCwd();
  const draw = () => {
    el.innerHTML = '';
    if (!skills.list.length) { el.append(noteEl('还没有本地技能'), mi({label: '管理扩展…', fn: () => openSettings('ext')})); return; }
    for (const s of skills.list) el.appendChild(mi({label: '/' + s.cmd, desc: s.desc, tag: SCOPE[s.scope] || '', cls: 'h-skill', title: s.desc,
      fn: () => { closePop(false); insertSkill(s.cmd); }}));
  };
  if (skills && skills.cwd === cwd && Date.now() - skills.at < 60000) { draw(); return; }
  el.appendChild(noteEl('正在读取技能…'));
  let d;
  try { d = await api('skills', {cwd}); }
  catch (e) { if (SUB && SUB.el === el) { el.innerHTML = ''; el.appendChild(noteEl('读取技能失败：' + e.message)); placeSub(el, SUB.owner); } return; }
  const seen = new Set(), list = [];
  for (const scope of ['project', 'user', 'plugin']) for (const s of d[scope] || []) {
    const nm = /^[\w.-]+$/.test(s.name || '') ? s.name : (s.folder || s.name);  // 名字带空格、斜杠的用文件夹名当命令
    const cmd = (s.plugin ? s.plugin + ':' : '') + nm;
    if (!cmd || seen.has(cmd)) continue;
    seen.add(cmd); list.push({cmd, desc: String(s.desc || '').replace(/\s+/g, ' ').trim(), scope});
  }
  skills = {cwd, at: Date.now(), list};
  if (SUB && SUB.el === el) {
    draw(); placeSub(el, SUB.owner);
    if (SUB.want && document.activeElement === SUB.owner) { const f = navItems(el)[0]; if (f) f.focus(); }  // 用键盘打开的：读完后焦点进子菜单
  }
}
/* 技能命令只在消息开头才生效：放到最前面，原有开头的命令换掉 */
function insertSkill(cmd) {
  const t = '/' + cmd + ' ', rest = input.value.replace(/^\/\S*\s*/, '').replace(/^\s+/, '');
  input.value = t + rest;
  input.focus(); input.selectionStart = input.selectionEnd = t.length;
  input.dispatchEvent(new Event('input'));
}
function openSettings(sec) {
  closePop(false);
  if (window.CWB && typeof CWB.openSettings === 'function') return CWB.openSettings(sec);
  if (sec === 'ext' && C.pane) return C.pane.open('ext');
  toast('设置面板暂时打不开');
}

/* ================= 4. 新对话首页 ================= */
const hero = mk('div', 'h-hero', '<div id="hProviderIcon" class="h-bot" role="img" aria-label="Claude" data-p="claude">' + CLAUDE_LOGO + '</div><h1 class="h-hello"></h1>');
hero.id = 'hHero';
main.insertBefore(hero, dock);
const below = mk('div', 'h-below');
below.id = 'hBelow';
dock.after(below);
function greet() {
  const h = new Date().getHours();
  return (h >= 5 && h < 11 ? '早上好' : h >= 11 && h < 13 ? '中午好' : h >= 13 && h < 18 ? '下午好' : h >= 18 && h < 23 ? '晚上好' : '夜深了') + '，今天想构建什么？';
}
const projList = () => [...S.projects.filter(p => !p.hidden), ...S.projects.filter(p => p.hidden)];
/* 文件夹：空的新对话就是它自己的；首页先用这次选过的，其次当前项目、默认文件夹、第一个项目 */
function homeCwd() {
  if (S.cur) return S.cwd;
  if (H.cwd) return H.cwd;
  if (S.cwd) return S.cwd;
  const d = H.prefs && H.prefs.defaultCwd;
  if (d) return d;
  const p = projList()[0];
  return p ? p.cwd : '';
}
function renderHome() {
  hero.querySelector('.h-hello').textContent = greet();
  const cwd = homeCwd();
  homeKey = (S.cur || '') + '|' + cwd;
  below.innerHTML = '';
  const fc = btn('h-chip h-proj', IC.folder + `<span>${esc(cwd ? base(cwd) : '选择文件夹')}</span>` + IC.chev);
  fc.id = 'hProj'; fc.title = cwd ? '项目文件夹：' + tilde(cwd) : '选择项目文件夹'; fc.setAttribute('aria-haspopup', 'menu');
  fc.onclick = () => (P && P.kind === 'proj') ? closePop(true) : openPop('proj', fc, renderProjPop, {cls: 'h-projm', dir: 'down', maxH: 360});
  below.appendChild(fc);
  if (S.phrases.length) below.appendChild(mk('span', 'h-bsep'));
  for (const p of S.phrases.slice(0, 4)) {
    const b = btn('h-chip', `<span>${esc(p)}</span>`);
    b.title = '填入输入框';
    b.onclick = () => fillInput(p);
    below.appendChild(b);
  }
}
function fillInput(t) {
  const v = input.value;
  input.value = v.trim() ? v.replace(/\s*$/, ' ') + t : t;
  input.focus(); input.selectionStart = input.selectionEnd = input.value.length;
  input.dispatchEvent(new Event('input'));
}
function renderProjPop(el) {
  const cw = homeCwd(), list = projList();
  if (!list.length) el.appendChild(noteEl('还没有项目文件夹'));
  for (const p of list) el.appendChild(mi({label: p.name, desc: tilde(p.cwd), check: samePath(p.cwd, cw), key: 'p:' + p.cwd, cls: 'h-one',
    fn: () => { closePop(true); pickFolder(p.cwd); }}));
  el.append(sepEl(), mi({label: '添加文件夹…', icon: IC.folderPlus, fn: addFolder}));
}
async function addFolder() {
  closePop(false);
  const d = await api('add-folder').catch(() => ({}));
  if (!d.cwd) return;
  C.toggleProject(d.cwd, true); C.refreshSide(true);
  pickFolder(d.cwd);
}
const isEmptySess = () => !!S.cur && !S.loading && !S.byId.size && !S.pendings.length;
async function pickFolder(cwd) {
  if (!cwd || samePath(cwd, homeCwd()) || H.creating) return;
  if (!S.cur) { H.cwd = cwd; skills = null; renderHome(); prewarmSoon(); return; }
  if (!isEmptySess()) return;
  /* 空的新对话换文件夹：先在新文件夹里建好，再结束原来那个（还没发过消息，没有记录，不留空对话） */
  const old = S.cur, text = input.value, carry = {};
  for (const k of ['model', 'effort', 'mode', 'fast']) if (S.opts && S.opts[k] != null && S.opts[k] !== '') carry[k] = S.opts[k];
  H.creating = true; body.classList.add('h-creating');
  try {
    const d = await api('new', {cwd, opts: carry});
    store.set('draft.' + d.sid, text.trim() ? text : null);
    S.attsBy[d.sid] = S.atts;  // load() 换对话时把附件带过去
    C.toggleProject(cwd, true);
    C.load(d);
    store.set('last', d.sid); store.set('draft.' + old, null); delete S.attsBy[old];
    api('stop', {sid: old}).catch(() => {});
    skills = null;
    C.refreshSide(true);
  } catch (e) { toast(e.message); }
  finally { H.creating = false; body.classList.remove('h-creating'); }
}
let pwT = 0;
function prewarmSoon() {
  clearTimeout(pwT);
  pwT = setTimeout(() => { const cwd = homeCwd(); if (!S.cur && H.home && cwd && !document.hidden) api('prewarm', {cwd}).catch(() => {}); }, 800);
}
async function loadPrefs() {
  try { const d = await api('prefs', {}); H.prefs = (d && d.prefs) || {}; H.cli = (d && d.cli) || null; } catch { H.prefs = H.prefs || {}; }
  if (H.home && !S.cur) renderHome();
  renderPills();
}
/* 没有对话时发送：用选中的文件夹和首页选项新建对话，建好再发 */
C.homeSend = async (now, direct) => {
  if (H.creating) return;
  const fromInput = direct == null, text = fromInput ? input.value : String(direct || '');
  if (fromInput && S.atts.some(a => !a.path)) { toast('图片还在上传，稍等一下再发'); return; }
  if (!text.trim() && !(fromInput && S.atts.length)) { input.focus(); return; }
  const cwd = homeCwd();
  if (!cwd) { toast('先选一个项目文件夹'); const fc = $('#hProj'); if (fc) fc.click(); return; }
  H.creating = true; body.classList.add('h-creating');
  try {
    const d = await api('new', {cwd, opts: explicitOpts()});
    if (fromInput) store.set('draft.' + d.sid, input.value || null);  // load() 会按草稿填回输入框，send() 再从输入框发出
    store.set('draft.home', null);
    C.toggleProject(cwd, true);
    S.freshSid = d.sid;  // 11-11
    C.load(d); store.set('last', d.sid); C.refreshSide(true);
    H.opts = {}; H.cwd = '';
    if (fromInput) C.send(); else C.sendText(text);
  } catch (e) { toast(e.message); }
  finally { H.creating = false; body.classList.remove('h-creating'); }
};
/* 进入首页：侧栏“新对话”、⌘N、顶栏新对话按钮 */
function goHome() {
  closePop(false); C.closeMenu();
  if (!$('#palette').hidden) $('#palette').hidden = true;
  if (S.loading) S.loading = null;  // 正在打开的对话不要了
  clearTimeout(H.skelT); setLoading(false);
  if (S.cur && isEmptySess()) S.freshSid = S.cur;  // 11-11：当前就是个空对话，主动回首页时照旧显示首页
  if (S.cur && !isEmptySess()) {
    const sid = S.cur;
    store.set('draft.' + sid, input.value.trim() ? input.value : null);
    S.attsBy[sid] = S.atts; S.atts = S.attsBy.__home || []; delete S.attsBy.__home;
    H.cwd = ''; H.opts = {};
    C.clearMain();
    const cx = $('#ctx'); if (cx) cx.hidden = true;  // 第十四轮 FH：上一个对话的“上下文 N”不留在首页
    Object.assign(S, {runner: null, opts: {}, title: '', exit: null, exitEl: null, spinEl: null, liveEl: null, liveTxt: '', turn: null, tl: null, pendings: []});
    input.value = store.get('draft.home', '') || '';
    C.renderAtts(); C.renderTop(); C.renderSide();
    input.dispatchEvent(new Event('input'));
  }
  syncHome();
  input.focus();
}
$('#newHere').onclick = goHome;
/* 从首页进入别的对话：首页草稿和附件先收起来，回首页时再拿出来 */
chain('onPreLoad', d => {
  if (S.cur || H.creating) return;
  store.set('draft.home', input.value.trim() ? input.value : null);
  S.attsBy.__home = S.atts; S.atts = S.attsBy[d.sid] || []; delete S.attsBy[d.sid];
  C.renderAtts();
});
let hdT = 0;
input.addEventListener('input', () => {
  if (S.cur) return;
  clearTimeout(hdT);
  hdT = setTimeout(() => { if (!S.cur && !H.creating) store.set('draft.home', input.value.trim() ? input.value : null); }, 400);
});

/* 首页的显示与离开：离开时输入框平滑回到底部（FLIP），机器人和问候、下方 chip 淡出 */
function wantHome() {
  if (H.booting || H.loadingShown) return false;
  if (!S.cur) return true;
  // 11-11：只有刚新建、还没发过消息的对话才显示首页。已有对话的消息区暂时空白（重新载入、/clear 换了会话、载入中）都留在对话页
  if (S.cur !== S.freshSid) return false;
  if (S.byId.size || S.pendings.length) return false;
  return ![...feed.children].some(x => !x.classList.contains('spin'));
}
let syncQ = false;
function syncHome() {
  if (syncQ) return;
  syncQ = true;
  queueMicrotask(() => { syncQ = false; applyHome(wantHome()); });
}
let homeKey = '';
function applyHome(on) {
  body.classList.toggle('h-nosess', on && !S.cur);
  if (on === H.home) { if (on && !P && homeKey !== (S.cur || '') + '|' + homeCwd()) renderHome(); return; }  // 仍在首页但换了对话或文件夹
  H.home = on;
  const anim = !RM() && !document.hidden && main.offsetWidth > 0;
  if (on) {
    renderHome(); renderPills(true);
    flip(() => body.classList.add('h-home'), anim);
    input.placeholder = HOME_PH;
    if (!S.cur) { C.renderTop(); loadPrefs(); prewarmSoon(); }
  } else {
    closePop(false);
    if (anim) { ghost(hero); ghost(below); }
    flip(() => body.classList.remove('h-home'), anim);
    input.placeholder = PH;
    if (anim) { feed.classList.remove('h-in'); void feed.offsetWidth; feed.classList.add('h-in'); }
  }
  placeToasts();
}
let flipT = 0;
function fitInput() { if (C.fitHeight) { C.fitHeight(); return; } input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, innerHeight * 0.4) + 'px'; }
function flip(fn0, anim) {
  const fn = () => { fn0(); fitInput(); };  // 首页输入框最小高度不同，切换后按内容重新量高
  if (!anim) { fn(); return; }
  const a = composer.getBoundingClientRect();
  dock.style.transition = 'none';
  fn();
  const b = composer.getBoundingClientRect(), dy = a.top - b.top, w0 = a.width, w1 = b.width;
  if (Math.abs(dy) < 2 && Math.abs(w0 - w1) < 2) { dock.style.transition = ''; return; }
  /* 宽度用 max-width 过渡，位置用 transform 从原处回到新位置；都是 200ms，只动 #dock 这一层 */
  const mw = getComputedStyle(dock).maxWidth;
  dock.style.maxWidth = (w0 + (dock.offsetWidth - composer.offsetWidth)) + 'px';
  dock.style.transform = `translateY(${dy}px)`;
  void dock.offsetWidth;
  dock.style.transition = 'transform .2s cubic-bezier(.2,.8,.2,1), max-width .2s cubic-bezier(.2,.8,.2,1)';
  dock.style.transform = '';
  dock.style.maxWidth = mw;
  clearTimeout(flipT);
  flipT = setTimeout(() => { dock.style.transition = ''; dock.style.maxWidth = ''; }, 240);
}
function ghost(el) {
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const g = el.cloneNode(true);
  g.removeAttribute('id'); g.querySelectorAll('[id]').forEach(x => x.removeAttribute('id'));
  g.classList.add('h-ghost'); g.setAttribute('aria-hidden', 'true');
  Object.assign(g.style, {left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px'});
  document.body.appendChild(g);
  setTimeout(() => g.remove(), 230);
}
new MutationObserver(syncHome).observe(feed, {childList: true});

/* ================= 5. 加载骨架：打开对话超过 120ms 还没好，就先画几条占位 ================= */
const skel = mk('div', '', '<div class="h-sk h-sk-u"></div><div class="h-sk-b"><i style="width:94%"></i><i style="width:86%"></i><i style="width:58%"></i></div>' +
  '<div class="h-sk h-sk-u s2"></div><div class="h-sk-b"><i style="width:90%"></i><i style="width:72%"></i></div>');
skel.id = 'hSkel'; skel.setAttribute('aria-hidden', 'true');
scroll.insertBefore(skel, feed);
function setLoading(on) {
  if (H.loadingShown === on) return;
  H.loadingShown = on;
  body.classList.toggle('h-loading', on);
  scroll.setAttribute('aria-busy', on ? 'true' : 'false');
  syncHome();
}
chain('onLoading', sid => {
  clearTimeout(H.skelT);
  if (!sid) { setLoading(false); if (S.booted) endBoot(); return; }  // 11-11：启动时打开失败会重试，重试完之前不落到首页
  if (sid === S.cur) return;  // 同一个对话重新载入（重连、/clear）不遮内容
  H.skelT = setTimeout(() => { if (S.loading === sid) setLoading(true); }, 120);
});
function endBoot() {
  if (!H.booting) return;
  H.booting = false;
  if (!S.cur && !input.value) { const v = store.get('draft.home', ''); if (v) { input.value = v; input.dispatchEvent(new Event('input')); } }
  syncHome();
}
chain('onBoot', opened => { if (!opened) endBoot(); });  // 打开了上次的对话就等它载入完
chain('onLoad', () => { clearTimeout(H.skelT); setLoading(false); endBoot(); syncHome(); renderPills(true); });
chain('onControls', () => renderPills());
if (!H.booting) syncHome();
setTimeout(endBoot, 6000);  // 兜底：后台一直没响应也别一直空着

/* ================= 6. 发送键偏好：立即发送的提示跟着变 ================= */
chain('onSendBtn', () => {
  const nb = $('#sendNow');
  if (!nb) return;
  const t = '立即发送：不等这一步做完（' + (store.get('pref.sendKey', 'enter') === 'cmd' ? '⌘⇧Enter' : '⌘Enter') + '）';
  if (nb.title !== t) nb.title = t;
});

/* ================= 7. 顶栏：16px 线性图标、悬停 300ms 出小提示 ================= */
$('#top').setAttribute('data-drag', '');
for (const [id, name, ic, key] of [['tocBtn', '目录', IC.toc], ['chgBtn', '改动', IC.chg], ['reviewBtn', '审阅', IC.review], ['taskBtn', '子代理和后台命令', IC.task],
  ['expBtn', '导出', IC.exp], ['moreBtn', '更多', IC.more], ['newHere', '新对话', IC.compose, '⌘N']]) {
  const b = $('#' + id);
  if (!b) continue;
  b.dataset.ic = '1'; b.classList.add('h-ib'); b.innerHTML = ic;
  b.title = name; b.setAttribute('aria-label', name); b.dataset.tip = name;
  if (key) b.dataset.key = key;
}
{
  const exp = $('#expBtn');
  if (exp && !$('#top .h-tsep')) {
    const sep = mk('span', 'h-tsep');
    sep.setAttribute('aria-hidden', 'true');
    exp.before(sep);
  }
}
const sideBtn = $('#uxSideBtn');
if (sideBtn) sideBtn.dataset.key = '⌘B';
const tip = mk('div');
tip.id = 'hTip'; tip.setAttribute('role', 'tooltip'); tip.hidden = true;
document.body.appendChild(tip);
const TIPS = '#top .h-ib, #uxSideBtn, #setBtn, #setBtn2, #addFolder, #plusBtn, .h-pill';
let tipEl = null, tipT = 0;
function hideTip() {
  clearTimeout(tipT); tip.hidden = true;
  if (tipEl) {
    if (!tipEl.hasAttribute('title') && tipEl.dataset.htitle) tipEl.title = tipEl.dataset.htitle;  // title 留给读屏，悬停时先收起，免得和系统提示重复
    delete tipEl.dataset.htitle; tipEl = null;
  }
}
function showTip(el) {
  if (!el.isConnected || P || el.classList.contains('open')) return;
  const name = el.dataset.tip || el.dataset.htitle || el.getAttribute('aria-label') || '';
  if (!name) return;
  tip.innerHTML = `<span>${esc(name + (el.dataset.n ? ' ' + el.dataset.n : ''))}</span>` + (el.dataset.key ? `<kbd>${esc(el.dataset.key)}</kbd>` : '');
  tip.hidden = false;
  const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  const up = !!el.closest('#dock, #hMe') || el.id === 'setBtn2';
  let y = up ? r.top - h - 6 : r.bottom + 6;
  if (y < 4) y = r.bottom + 6; else if (y + h > innerHeight - 4) y = r.top - h - 6;
  tip.style.left = Math.round(Math.max(6, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 6))) + 'px';
  tip.style.top = Math.round(y) + 'px';
}
document.addEventListener('mouseover', e => {
  const el = e.target.closest ? e.target.closest(TIPS) : null;
  if (el === tipEl) return;
  hideTip();
  if (!el) return;
  tipEl = el;
  if (el.title) { el.dataset.htitle = el.title; el.removeAttribute('title'); }
  tipT = setTimeout(() => showTip(el), 300);
});
document.addEventListener('mouseout', e => { if (!e.relatedTarget) hideTip(); });
document.addEventListener('mousedown', hideTip, true);
document.addEventListener('keydown', hideTip, true);
addEventListener('blur', hideTip);
addEventListener('scroll', hideTip, true);

/* ================= 8. 侧栏：新对话按钮、账号行（今日用量、设置）、主区右下角设置 ================= */
const sideTop = side.querySelector('.side-top');
sideTop.setAttribute('data-drag', '');
const sideNew = btn('', IC.plus + '<span>新对话</span><kbd>⌘N</kbd>');  // 第十四轮 FH：同设计稿，“+”图标、无边框
sideNew.id = 'sideNew'; sideNew.setAttribute('aria-label', '新对话（⌘N）');
sideNew.onclick = goHome;
sideTop.prepend(sideNew);
const addF = $('#addFolder');
addF.innerHTML = IC.folderPlus; addF.setAttribute('aria-label', '添加文件夹'); addF.dataset.tip = '添加文件夹';
const me = mk('div', '', '<span class="h-av" id="hBotAv" aria-hidden="true">' + PIXBOT('h-mebot', 0) + '</span><div class="h-me-t"><b id="hMood">轻松的工作</b><small id="hUse"></small></div>');
me.id = 'hMe'; me.title = '查看用量';
const setBtn = btn('h-ib', IC.gear, '设置');
setBtn.id = 'setBtn'; setBtn.dataset.tip = '设置'; setBtn.dataset.key = '⌘,';
setBtn.onclick = e => { e.stopPropagation(); openSettings(); };
me.appendChild(setBtn);
me.onclick = () => openSettings('usage');
side.appendChild(me);
const setBtn2 = btn('h-ib', IC.gear, '设置');
setBtn2.id = 'setBtn2'; setBtn2.dataset.tip = '设置'; setBtn2.dataset.key = '⌘,';
setBtn2.onclick = () => openSettings();
main.appendChild(setBtn2);
function renderMe() {}  // 11-18：头像换成小机器人，不再显示用户名首字母
// 11-18：账号行标题按今天的花费换心情
const MOODS = [[10, '轻松的工作'], [50, '渐入佳境'], [100, '有点累了'], [200, '忙得团团转'], [300, '快要冒烟了'], [500, '累瘫了，求放过'], [Infinity, '电量耗尽…']];
const moodIdxOf = c => MOODS.findIndex(([lim]) => c < lim);
let lastMoodIdx = 0;
async function loadUsage() {
  if (document.hidden) return;
  let t = '', mood = null, mi = -1;
  try {
    let cs = 'all';
    try { cs = JSON.parse(localStorage.getItem('cwb.uu.cscope')) === 'wb' ? 'wb' : 'all'; } catch {}  // 11-26：和用量页同一个 Claude 统计范围
    const d = await api('usage-summary', {claudeScope: cs}), x = d && d.today;
    if (x && x.requests != null) {
      const c = Number(x.cost) || 0;
      t = `今天 ${Number(x.requests) || 0} 次请求 · $${c.toFixed(2)}`;
      mi = moodIdxOf(c);
      mood = MOODS[mi][1];
    }
  } catch {}
  const el = $('#hUse');
  if (el.textContent !== t) el.textContent = t;
  const m = $('#hMood');
  if (mood && m.textContent !== mood) m.textContent = mood;
  if (mi >= 0 && mi !== lastMoodIdx) {
    lastMoodIdx = mi;
    const av = $('#hBotAv');
    if (av) av.innerHTML = PIXBOT('h-mebot', mi);
  }
}
loadUsage();
setInterval(loadUsage, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadUsage(); });
chain('onSide', () => { renderMe(); if (H.home && !S.cur && !P) renderHome(); });

/* ================= 9. 快捷键：⌘N 进入首页（⌘, 归设置面板） ================= */
document.addEventListener('keydown', e => {
  if (e.defaultPrevented || e.isComposing) return;
  const k = (e.key || '').toLowerCase();
  if (e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && (k === 'n' || e.code === 'KeyN')) {
    if (!$('#modal').hidden) return;
    e.preventDefault(); goHome();
  }
}, true);

/* ================= 10. 提示条位置（第十四轮 FH） =================
   #toast 和撤销条 #fUndo 原来固定在窗口底部 24px，会压在输入框底边和药丸上：对话里放到输入区上方 10px；
   首页输入框居中，放在页面底部。用 CSS 变量 --toast-b，两者的叠放关系（撤销条在 #toast 上面）不变。
   按布局位置算（offsetTop），首页切换时 #dock 的 transform 动画不影响 */
var toastB = -1;
function placeToasts() {
  const b = H.home || !dock.offsetHeight ? 24 : Math.max(24, Math.round(innerHeight - (main.getBoundingClientRect().top + dock.offsetTop) + 10));
  if (b === toastB) return;
  toastB = b;
  document.documentElement.style.setProperty('--toast-b', b + 'px');
}
if (window.ResizeObserver) new ResizeObserver(() => placeToasts()).observe(dock);
addEventListener('resize', () => placeToasts());
placeToasts();

C.home = {go: goHome, sync: syncHome, isHome: () => H.home, cwd: homeCwd, opts: () => cur(), pick: pickFolder, state: H};
loadPrefs();
renderPills(true);
if (C.refreshRunner) C.refreshRunner();  // 发送按钮一开始就带上 data-state 和提示
})();
