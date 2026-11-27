(() => {
'use strict';
/* 第十四轮 J：
   1. 设置面板 CWB.openSettings(section)：通用、通知、对话默认、用量、技能与 MCP、网关与账号、关于。⌘, 打开，Esc 或点遮罩关闭，记住上次的分区。
   2. 用量页：今天 / 7 天 / 30 天；指标卡、柱状图、模型前 4、53 周热力图、用量最多的对话、最近请求（接口 usage，见 PLAN 约定第 4 条）。
   3. 偏好生效：外观（强制浅色 / 深色时改写各样式表里 prefers-color-scheme 的媒体条件）、字号、系统通知、过程默认折叠。
      sendKey、startup 只在这里读写，生效在 H（ux-h.js）。偏好改动后派发 window 事件 cwb:pref {key, value}。
   4. 原生 App：[data-drag] 区域按下拖动窗口、双击按系统设置缩放或最小化；外观和通知偏好告诉外壳。
   偏好与 app.js 的 store 同一套存储（localStorage 'cwb.' + 键，JSON）。不改 app.js。 */
const root = document.documentElement;
const sget = (k, d) => { try { const v = localStorage.getItem('cwb.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } };
const sset = (k, v) => { try { if (v == null) localStorage.removeItem('cwb.' + k); else localStorage.setItem('cwb.' + k, JSON.stringify(v)); } catch {} };
const pref = (k, d) => sget('pref.' + k, d);
const NATIVE = !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.cwb);
const native = m => { try { window.webkit.messageHandlers.cwb.postMessage(m); } catch {} };

/* ================= 外观、字号：尽早生效 ================= */
const CS = /\(\s*prefers-color-scheme\s*:\s*(light|dark)\s*\)/gi;
const origMedia = new WeakMap();
function eachMediaRule(fn) {
  const walk = rules => { for (const r of rules) { if (r instanceof CSSMediaRule) fn(r); if (r.cssRules && r.cssRules.length) walk(r.cssRules); } };
  for (const sh of document.styleSheets) { let rs = null; try { rs = sh.cssRules; } catch {} if (rs) walk(rs); }
}
/* 强制浅色 / 深色：把各样式表里的 (prefers-color-scheme: x) 换成恒真 / 恒假条件；跟随系统时还原 */
function applyTheme(t) {
  if (t !== 'light' && t !== 'dark') t = 'system';
  if (t === 'system') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', t);
  const swap = (o, yes, no) => o.replace(CS, (_, s) => s.toLowerCase() === t ? yes : no);
  eachMediaRule(r => {
    let o = origMedia.get(r);
    if (o == null) { o = r.media.mediaText; if (!/prefers-color-scheme/i.test(o)) return; origMedia.set(r, o); }
    const want = t === 'system' ? o : swap(o, '(min-width: 0px)', '(max-width: 0px)');
    if (r.media.mediaText !== want) r.media.mediaText = want;
  });
  for (const l of document.querySelectorAll('link[rel=stylesheet][media]')) {   // 代码高亮的两份样式表
    if (l.dataset.sjMedia == null) { if (!/prefers-color-scheme/i.test(l.media)) continue; l.dataset.sjMedia = l.media; }
    const o = l.dataset.sjMedia, want = t === 'system' ? o : swap(o, 'all', 'not all');
    if (l.media !== want) l.media = want;
  }
}
function applyFont(f) { if (f === 's' || f === 'l') root.setAttribute('data-font', f); else root.removeAttribute('data-font'); }
applyTheme(pref('theme', 'system'));
applyFont(pref('font', 'm'));
if (NATIVE) native({type: 'prefs', theme: pref('theme', 'system'), notify: pref('notify', 'bg')});

/* ================= 系统通知：关闭 / 总是 ================= */
/* 浏览器：关闭时 new Notification 变成空操作（app.js 的 notifyLocal 照常调用）。原生 App：外壳按 prefs.notify 过滤 */
const RealN = window.Notification;
if (RealN && !NATIVE) {
  const SjN = function (title, opt) {
    if (pref('notify', 'bg') === 'off') return {close() {}, addEventListener() {}};
    return new RealN(title, opt);
  };
  Object.defineProperty(SjN, 'permission', {get: () => RealN.permission});
  SjN.requestPermission = (...a) => RealN.requestPermission(...a);
  SjN.prototype = RealN.prototype;
  try { window.Notification = SjN; } catch {}
}

const C = window.CWB;
if (!C) return;
const {S, $, esc} = C;
const api = (n, b) => C.api(n, b);  // 每次现取，测试时可替换 CWB.api
const toast = t => C.toast(t);
const chain = (name, fn) => {
  const prev = C[name];
  C[name] = (...a) => { try { if (prev) prev(...a); } finally { try { fn(...a); } catch (e) { console.error(e); } } };
};
/* 偏好写入：app.js 导出了 store（H）就用它，否则用同格式的 sset（localStorage 'cwb.' + 键，JSON） */
const put = (k, v) => (C.store && C.store.set ? C.store.set(k, v) : sset(k, v));
const emit = (key, value) => { try { window.dispatchEvent(new CustomEvent('cwb:pref', {detail: {key, value}})); } catch {} };

function sysNotify(title, text, sid) {
  if (NATIVE) { native({type: 'notify', title: title || 'Claude 工作台', text: text || '', sid: sid || ''}); return; }
  if (!RealN || RealN.permission !== 'granted') return;
  try {
    const n = new RealN(title || 'Claude 工作台', {body: text || '', tag: 'cwb-' + (sid || '') + '-' + (text || '').slice(0, 8), icon: '/static/icon.svg'});
    n.onclick = () => { window.focus(); if (sid && sid !== S.cur) C.openSession(sid); n.close(); };
  } catch {}
}
/* “总是”：窗口在前台时 app.js 不发，这里补发（钩子在 setSideFlags 之前跑，S.busyMap 里还是上一次的状态） */
chain('onEvent', ev => {
  if (pref('notify', 'bg') !== 'always' || !document.hasFocus()) return;
  const title = sid => (C.sessTitle ? C.sessTitle(sid) : '') || 'Claude 工作台';
  if (ev.type === 'runner' && ev.sid) {
    const st = ev.state || {};
    if (S.busyMap[ev.sid] && !(st.alive && st.busy)) sysNotify(title(ev.sid), '回复完成', ev.sid);
  } else if (ev.type === 'perm' && ev.req) sysNotify(title(ev.req.sid), '需要你确认：' + ev.req.tool, ev.req.sid);
  else if (ev.type === 'notice') sysNotify(ev.title || title(ev.sid), 'CLI 在等你处理：' + (ev.text || ''), ev.sid);
});

/* 过程默认折叠：以 ux-d.js 的全局开关为准，pref.foldProcess 跟着它 */
if (C.dz) put('pref.foldProcess', !C.dz.allOpen);

function setPref(k, v) {
  put('pref.' + k, v);
  if (k === 'theme') applyTheme(v);
  else if (k === 'font') applyFont(v);
  if (NATIVE && (k === 'theme' || k === 'notify')) native({type: 'prefs', [k]: v});
  emit(k, v);
}
addEventListener('storage', e => {
  if (e.key === 'cwb.pref.theme') applyTheme(pref('theme', 'system'));
  else if (e.key === 'cwb.pref.font') applyFont(pref('font', 'm'));
});

/* ================= 格式 ================= */
const trim0 = s => (s.includes('.') ? s.replace(/\.?0+$/, '') : s);
function fmtTok(n) {
  n = +n || 0;
  if (n < 1000) return String(Math.round(n));
  if (n < 1e6) return trim0((n / 1e3).toFixed(n < 1e5 ? 1 : 0)) + 'K';
  if (n < 1e9) return trim0((n / 1e6).toFixed(1)) + 'M';
  return trim0((n / 1e9).toFixed(2)) + 'B';
}
const fmtInt = n => Math.round(+n || 0).toLocaleString('en-US');
const fmtUsd = n => { n = +n || 0; return n > 0 && n < 0.005 ? '<$0.01' : '$' + n.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); };
const fmtSpeed = v => v == null || !isFinite(v) || v <= 0 ? '—' : Math.round(v) + ' tok/s';
const fmtPct = v => v == null || !isFinite(v) ? '—' : (v * 100).toFixed(1).replace(/\.0$/, '') + '%';
const fmtMs = ms => { ms = +ms || 0; if (!ms) return '—'; if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's'; return Math.floor(ms / 60000) + 'm' + Math.round(ms % 60000 / 1000) + 's'; };
const p2 = n => String(n).padStart(2, '0');
const toMs = t => { t = +t || 0; return t && t < 1e12 ? t * 1000 : t; };
const hm = t => { const d = new Date(toMs(t)); return p2(d.getHours()) + ':' + p2(d.getMinutes()); };
const mdhm = t => { const d = new Date(toMs(t)); return p2(d.getMonth() + 1) + '-' + p2(d.getDate()) + ' ' + hm(t); };
const dkey = d => d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate());
const WD = '日一二三四五六';
const homeShort = p => (p && S.home && (p === S.home || p.startsWith(S.home + '/')) ? '~' + p.slice(S.home.length) : p || '');
/* 只显示打码后的令牌；万一拿到的是完整令牌，这里再打一次码 */
const maskTok = t => { t = String(t || ''); return !t ? '' : /[•*…]/.test(t) || t.length <= 8 ? t : t.slice(0, 4) + '••••••••' + t.slice(-2); };

/* ================= 设置面板 ================= */
const SECS = [['general', '通用'], ['notify', '通知'], ['chat', '对话默认'], ['usage', '用量'], ['ext', '扩展'], ['gateway', '网关与账号'], ['about', '关于']];
const ALIAS = {gw: 'gateway', account: 'gateway', skills: 'ext', mcp: 'ext', defaults: 'chat'};
const IC_X = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
const NAV_IC = {
  general: '<path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11"/><circle cx="6" cy="4.5" r="1.4" fill="var(--bg)"/><circle cx="10.5" cy="8" r="1.4" fill="var(--bg)"/><circle cx="5.5" cy="11.5" r="1.4" fill="var(--bg)"/>',
  notify: '<path d="M8 2.2a3.6 3.6 0 0 0-3.6 3.6v2.5L3 10.6h10l-1.4-2.3V5.8A3.6 3.6 0 0 0 8 2.2z"/><path d="M6.6 12.6a1.5 1.5 0 0 0 2.8 0"/>',
  chat: '<path d="M2.6 4.4c0-.9.7-1.6 1.6-1.6h7.6c.9 0 1.6.7 1.6 1.6v5c0 .9-.7 1.6-1.6 1.6H7.2l-2.7 2.2V11h-.3c-.9 0-1.6-.7-1.6-1.6z"/>',
  persona: '<path d="M8 2.2l1.3 3.4 3.5.3-2.7 2.2.8 3.5L8 9.7l-2.9 1.9.8-3.5-2.7-2.2 3.5-.3z"/>',
  collab: '<circle cx="5.4" cy="5.6" r="2.1"/><circle cx="11" cy="6.2" r="1.7"/><path d="M2.2 12.8c.4-2 1.7-3 3.2-3s2.8 1 3.2 3"/><path d="M9.6 12.8c.3-1.4 1.1-2.2 2.2-2.2 1 0 1.8.8 2 2.2"/>',
  usage: '<path d="M3 12.8V8.4M6.4 12.8V5.2M9.8 12.8V7.2M13.2 12.8V3.2"/>',
  ext: '<rect x="2.6" y="2.6" width="4.4" height="4.4" rx="1"/><rect x="9" y="2.6" width="4.4" height="4.4" rx="1"/><rect x="2.6" y="9" width="4.4" height="4.4" rx="1"/><path d="M11.2 9.2v4M9.2 11.2h4"/>',
  memory: '<path d="M4 2.5h5.8L12.5 5.2v8.3H4z"/><path d="M9.5 2.5v2.8h3"/><path d="M6 8h4M6 10.6h2.8"/>',
  gateway: '<circle cx="8" cy="8" r="5.4"/><path d="M2.8 8h10.4M8 2.6c1.6 1.6 2.3 3.4 2.3 5.4S9.6 11.8 8 13.4C6.4 11.8 5.7 10 5.7 8S6.4 4.2 8 2.6z"/>',
  remote: '<rect x="5" y="2.2" width="6" height="11.6" rx="1.4"/><path d="M7.3 11.5h1.4"/>',
  about: '<circle cx="8" cy="8" r="5.5"/><path d="M8 7.3v3.5M8 5.1v.2"/>',
};
const NAV_GRP = {general: '偏好与对话', usage: '能力与数据', gateway: '连接与系统'};
function navHtml() {
  let lastG = '', out = '<h3>设置</h3>';
  for (const [k, l] of SECS) {
    const g = NAV_GRP[k];
    if (g && g !== lastG) {
      out += `<div class="sj-ng">${g}</div>`;
      lastG = g;
    }
    const ic = NAV_IC[k] ? `<svg class="sj-ni" viewBox="0 0 16 16" aria-hidden="true">${NAV_IC[k]}</svg>` : '';
    out += `<button type="button" data-s="${k}">${ic}${l}</button>`;
  }
  return out;
}
/* Fast：与模型浮层（ux-h.js 的 IC.bolt）同一个闪电 */
const IC_BOLT = '<svg class="sj-bolt" viewBox="0 0 16 16" aria-hidden="true"><path d="M9.2 1.6 3.4 9.1h4.1l-.8 5.3 5.9-7.6H8.4z"/></svg>';
let el = null, body = null, tip = null, cur = null, gen = 0, lastFocus = null, downOnScrim = false;

function build() {
  el = document.createElement('div');
  el.id = 'sjSet'; el.className = 'sj-scrim'; el.hidden = true;
  el.innerHTML = '<div class="sj-panel" role="dialog" aria-modal="true" aria-label="设置"><nav class="sj-nav">' +
    navHtml() +
    `</nav><section class="sj-body" tabindex="-1"></section><button type="button" class="sj-x" title="关闭（Esc）" aria-label="关闭">${IC_X}</button></div>` +
    '<div class="sj-tip" hidden></div>';
  document.body.appendChild(el);
  body = el.querySelector('.sj-body');
  tip = el.querySelector('.sj-tip');
  el.addEventListener('mousedown', e => { downOnScrim = e.target === el; });
  el.addEventListener('click', e => { if (e.target === el && downOnScrim) close(); });
  el.querySelector('.sj-nav').addEventListener('click', e => { const b = e.target.closest('[data-s]'); if (b && b.dataset.s !== cur) show(b.dataset.s); });
  el.querySelector('.sj-nav').addEventListener('keydown', e => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const i = SECS.findIndex(([k]) => k === cur), j = (i + (e.key === 'ArrowDown' ? 1 : SECS.length - 1)) % SECS.length;
    show(SECS[j][0]); el.querySelector(`.sj-nav [data-s="${SECS[j][0]}"]`).focus();
  });
  el.querySelector('.sj-x').onclick = close;
  body.addEventListener('scroll', () => { hideTip(); body.classList.toggle('sj-scrolled', body.scrollTop > 2); }, {passive: true});
}
const isOpen = () => !!(el && !el.hidden);
function open(sec) {
  sec = ALIAS[sec] || sec;
  if (!SECS.some(([k]) => k === sec)) { sec = sget('sj.sec', 'general'); if (!SECS.some(([k]) => k === sec)) sec = 'general'; }
  if (!el) build();
  const was = isOpen();
  if (was && cur === sec) return;
  if (!was) {
    lastFocus = document.activeElement;
    if (C.closeMenu) C.closeMenu();
    el.hidden = false;
    el.classList.remove('sj-show'); void el.offsetWidth; el.classList.add('sj-show');
    const app = $('#app'); if (app) app.inert = true;
    document.body.classList.add('sj-open');
  }
  show(sec);
  if (!was) body.focus({preventScroll: true});  // 焦点放进面板（不显示焦点环），Tab 可到导航和控件
}
function close() {
  if (!isOpen()) return;
  el.hidden = true; el.classList.remove('sj-show');
  const app = $('#app'); if (app) app.inert = false;
  document.body.classList.remove('sj-open');
  stopUsage(); hideTip(); gen++;
  body.innerHTML = ''; cur = null;  // 清空：扩展页的容器随之失效，feat.js 的重画回到右栏
  if (lastFocus && lastFocus.isConnected && lastFocus.focus) try { lastFocus.focus({preventScroll: true}); } catch {}
  lastFocus = null;
}
function show(sec) {
  cur = sec; sset('sj.sec', sec); gen++;
  el.querySelectorAll('.sj-nav [data-s]').forEach(b => { const on = b.dataset.s === sec; b.classList.toggle('on', on); if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); });
  stopUsage(); hideTip();
  body.dataset.sec = sec; body.scrollTop = 0; body.classList.remove('sj-scrolled');
  try { RENDER[sec](); } catch (e) { console.error(e); body.innerHTML = `<div class="sj-empty">${esc(e.message)}</div>`; }
}
C.openSettings = open;
C.closeSettings = close;
C.settingsOpen = isOpen;
/* 设置里技能详情点“使用”会往输入框插入命令：先关掉设置，让输入框露出来 */
const insText = C.insertText;
if (insText) C.insertText = (...a) => { if (isOpen()) close(); return insText(...a); };

/* ⌘, 打开；打开时 Esc 关闭，其他 ⌘ 快捷键不再落到背后的界面 */
addEventListener('keydown', e => {
  if (e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && (e.key === ',' || e.code === 'Comma')) { e.preventDefault(); e.stopImmediatePropagation(); open(); return; }
  if (!isOpen()) return;
  const overlay = !$('#modal').hidden || !$('#palette').hidden || !!S.menu;
  if (e.key === 'Escape' && !e.isComposing) {
    if (overlay) return;  // 先关上面那层（技能详情、菜单等）
    e.preventDefault(); e.stopImmediatePropagation(); close();
  } else if ((e.metaKey || e.ctrlKey) && !overlay && !/^[acvxz]$/i.test(e.key) && !['Meta', 'Control', 'Shift', 'Alt'].includes(e.key)) {
    e.stopImmediatePropagation();
  }
}, true);

/* ---------- 小部件 ---------- */
const head = (t, lead) => `<div class="sj-hd"><h2>${t}</h2></div>${lead ? `<div class="sj-lead">${lead}</div>` : ''}`;
const row = (label, ctrl, hint, cls) => `<div class="sj-row${cls ? ' ' + cls : ''}"><div class="sj-lb">${label}${hint ? `<div class="sj-hint">${hint}</div>` : ''}</div><div class="sj-val">${ctrl}</div></div>`;
const opts = (list, v) => list.map(([val, l]) => `<option value="${esc(val)}"${val === v ? ' selected' : ''}>${esc(l)}</option>`).join('');
const sel = (k, list, v, label) => `<select class="sj-sel" data-k="${k}" aria-label="${esc(label || '')}">${opts(list, v)}</select>`;
const seg = (k, list, v, label) => `<div class="sj-seg" role="radiogroup" data-k="${k}" aria-label="${esc(label || '')}">` +
  list.map(([val, l]) => `<button type="button" role="radio" data-v="${esc(val)}" aria-checked="${val === v}"${val === v ? ' class="on"' : ''}>${esc(l)}</button>`).join('') + '</div>';
const sw = (k, on, dis, label) => `<button type="button" role="switch" class="sj-sw${on ? ' on' : ''}" data-k="${k}" aria-checked="${!!on}" aria-label="${esc(label || '')}"${dis ? ' disabled' : ''}></button>`;
const setSw = (b, v) => { b.classList.toggle('on', !!v); b.setAttribute('aria-checked', !!v); };
function bind(scope, map, acts) {
  scope.querySelectorAll('select[data-k]').forEach(s => s.onchange = () => map[s.dataset.k] && map[s.dataset.k](s.value, s));
  scope.querySelectorAll('.sj-seg[data-k]').forEach(g => g.onclick = e => {
    const b = e.target.closest('button[data-v]');
    if (!b || b.classList.contains('on')) return;
    g.querySelectorAll('button[data-v]').forEach(x => { x.classList.toggle('on', x === b); x.setAttribute('aria-checked', x === b); });
    if (map[g.dataset.k]) map[g.dataset.k](b.dataset.v, b);
  });
  scope.querySelectorAll('.sj-sw[data-k]').forEach(b => b.onclick = () => {
    if (b.disabled) return;
    const v = !b.classList.contains('on'); setSw(b, v);
    if (map[b.dataset.k]) map[b.dataset.k](v, b);
  });
  if (acts) scope.querySelectorAll('[data-a]').forEach(b => { const f = acts[b.dataset.a]; if (f) b.onclick = e => { e.stopPropagation(); f(e, b); }; });
}
const errBox = (msg, retry) => `<div class="sj-empty sj-err">${esc(msg)}${retry ? '<div><button type="button" class="sj-btn" data-a="retry">重试</button></div>' : ''}</div>`;

/* ---------- 通用 ---------- */
function rGeneral() {
  body.innerHTML = head('通用', '外观和输入') + '<div class="sj-grp">' +
    row('外观', sel('theme', [['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']], pref('theme', 'system'), '外观')) +
    row('字号', seg('font', [['s', '小'], ['m', '标准'], ['l', '大']], pref('font', 'm'), '字号')) +
    row('发送消息', seg('sendKey', [['enter', 'Enter'], ['cmd', '⌘ Enter']], pref('sendKey', 'enter'), '发送消息')) +
    row('启动时打开', sel('startup', [['last', '上次的对话'], ['new', '新对话']], pref('startup', 'last'), '启动时打开')) + '</div>';
  bind(body, {theme: v => setPref('theme', v), font: v => setPref('font', v), sendKey: v => setPref('sendKey', v), startup: v => setPref('startup', v)});
}

/* ---------- 通知 ---------- */
function rNotify() {
  const fx = C.fx, dz = C.dz;
  const perm = !NATIVE && RealN ? RealN.permission : 'granted';
  const hint = !NATIVE && !RealN ? '这个浏览器不支持系统通知' : perm === 'denied' ? '浏览器已禁止这个页面发通知，需要在浏览器设置里允许' : '';
  body.innerHTML = head('通知', '回复完成、需要确认时的提醒') + '<div class="sj-grp">' +
    row('回复完成提示音', sw('chime', fx ? fx.chimeOn : false, !fx, '回复完成提示音')) +
    row('系统通知', sel('notify', [['bg', '窗口不在前台时'], ['always', '总是'], ['off', '关闭']], pref('notify', 'bg'), '系统通知'), hint) +
    row('过程默认折叠', sw('fold', dz ? !dz.allOpen : pref('foldProcess', true), !dz, '过程默认折叠')) + '</div>';
  bind(body, {
    chime: v => { if (fx && fx.setChime) fx.setChime(v); },
    notify: v => {
      setPref('notify', v);
      if (v !== 'off' && !NATIVE && RealN && RealN.permission === 'default') RealN.requestPermission().then(() => { if (cur === 'notify') rNotify(); }).catch(() => {});
    },
    fold: v => { if (dz) dz.setAll(!v); put('pref.foldProcess', v); emit('foldProcess', v); },
  });
}

/* ---------- 对话默认 ---------- */
let P = null, CLI = {};  // 后端 prefs 缓存；CLI：prefs 返回的 cli（全局默认权限 mode，未设置为 null）
const EFFORTS = [['', '默认'], ['low', '低'], ['medium', '中'], ['high', '高'], ['xhigh', '超高'], ['max', '最高']];
const MODES = [['', '默认'], ['auto mode', '自动'], ['manual mode', '手动确认'], ['accept edits', '自动接受编辑'], ['plan mode', '规划模式'], ['bypass permissions', '跳过确认']];
/* 权限“未设置”跟随 CLI 全局默认（~/.claude/settings.json 的 permissions.defaultMode，后端换成 #mode 的取值）；CLI 也没设置就是默认模式 */
const cliModeName = () => { const m = CLI && CLI.mode; return (m && m !== 'default' && (MODES.find(x => x[0] === m) || [])[1]) || '默认'; };
const mlist = () => (S.models && S.models.list) || [];
function fastOk(id) {
  const x = id || (S.models && S.models.defaultId) || '', m = mlist().find(v => v.id === x);
  if (m && 'fast' in m) return !!m.fast;
  return /opus-5|opus-4-8/.test(x);  // 与 CLI 的判断一致
}
async function rChat() {
  const g = gen;
  body.innerHTML = head('对话默认', '新建对话时使用') + '<div class="sj-grp"><div class="sj-empty">读取中…</div></div>';
  try { const r = await api('prefs'); P = (r && r.prefs) || {}; CLI = (r && r.cli) || {}; }
  catch (e) {
    if (g !== gen) return;
    body.querySelector('.sj-grp').innerHTML = errBox('读取失败：' + e.message, true);
    bind(body, {}, {retry: rChat});
    return;
  }
  if (g === gen) drawChat();
}
function drawChat() {
  const d = P.defaults || {}, list = mlist(), extra = P.extraModels || [];
  const rec = list.filter(m => m.group !== 'more'), more = list.filter(m => m.group === 'more');
  const mopt = m => `<option value="${esc(m.id)}"${m.id === (d.model || '') ? ' selected' : ''}>${esc(m.label || m.id)}</option>`;
  let mo = `<option value=""${d.model ? '' : ' selected'}>跟随 CLI 默认（${esc(S.models.default || '—')}）</option>`;
  if (more.length) mo += `<optgroup label="推荐">${rec.map(mopt).join('')}</optgroup><optgroup label="更多">${more.map(mopt).join('')}</optgroup>`;
  else mo += rec.map(mopt).join('');
  if (d.model && !list.some(m => m.id === d.model)) mo += `<option value="${esc(d.model)}" selected>${esc(d.model)}</option>`;
  const sm = list.find(m => m.id === d.model);
  const cwd = P.defaultCwd || '';
  body.innerHTML = head('对话默认', '新建对话时使用') + '<div class="sj-grp">' +
    row('模型', `<select class="sj-sel" data-k="model" aria-label="模型">${mo}</select>`, sm && sm.desc ? esc(sm.desc) : '') +
    row('思考强度', sel('effort', EFFORTS, d.effort || '', '思考强度')) +
    row('权限', sel('mode', [['', `跟随 CLI（${cliModeName()}）`], ...MODES.slice(1)], d.mode || '', '权限')) +
    row('默认项目文件夹', (cwd ? `<span class="sj-path" title="${esc(cwd)}">${esc(homeShort(cwd))}</span>` : '<span class="sj-dim">未指定</span>') +
      '<button type="button" class="sj-btn" data-a="cwd">更改</button>') + '</div>' +
    '<div class="sj-grp">' + row('自定义模型 ID', '', '', 'sj-col') +
    `<div class="sj-xm">${extra.map((id, i) => `<span class="sj-chip"><span>${esc(id)}</span><button type="button" data-a="rm" data-i="${i}" title="移除" aria-label="移除 ${esc(id)}">${IC_X}</button></span>`).join('') || '<span class="sj-dim">还没有添加</span>'}</div>` +
    '<div class="sj-add"><input class="sj-in" type="text" placeholder="模型 ID" spellcheck="false" autocomplete="off" aria-label="模型 ID"><button type="button" class="sj-btn" data-a="add">添加</button></div></div>';
  const inp = body.querySelector('.sj-add input');
  const setD = async part => {
    const ok = await savePrefs({defaults: Object.assign({}, P.defaults || {}, part)});
    if (!ok || 'model' in part) redrawChat();  // 失败时还原显示；换模型后 Fast 是否可用、说明要跟着变
  };
  const addModel = async () => {
    const v = inp.value.trim();
    if (!v) return inp.focus();
    if (!/^[\w.:\-\[\]\/@]{2,100}$/.test(v)) { toast('模型 ID 只能包含字母、数字和 . - _ : [ ] / @'); return inp.focus(); }
    if (extra.includes(v) || mlist().some(m => m.id === v)) { toast('已在列表里'); return; }
    if (await savePrefs({extraModels: [...extra, v]})) { await refreshModels(); redrawChat(); }
  };
  inp.onkeydown = e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); addModel(); } };
  bind(body, {
    model: v => setD({model: v || null}),
    effort: v => setD({effort: v || null}),
    mode: v => setD({mode: v || null}),
    fast: v => setD({fast: v}),
  }, {
    cwd: (e, b) => pickCwd(b),
    add: addModel,
    rm: async (e, b) => {
      const list2 = extra.filter((_, i) => i !== +b.dataset.i);
      if (await savePrefs({extraModels: list2})) { await refreshModels(); redrawChat(); }
    },
  });
}
async function savePrefs(part) {
  try {
    const r = await api('prefs', {set: part});
    P = (r && r.prefs) || Object.assign({}, P, part);
    if (r && r.cli) CLI = r.cli;
    emit('prefs', P);
    return true;
  } catch (e) { toast('保存失败：' + e.message); return false; }
}
const redrawChat = () => { if (cur === 'chat' && P && body.querySelector('.sj-sel[data-k=model]')) drawChat(); };
async function refreshModels() {
  try { const d = await api('projects'); if (d && d.models) { S.models = d.models; window.dispatchEvent(new CustomEvent('cwb:models', {detail: d.models})); } } catch {}
}
function pickCwd(btn) {
  const setCwd = async v => { await savePrefs({defaultCwd: v}); redrawChat(); };
  const ps = (S.projects || []).filter(p => !p.hidden && p.cwd).sort((a, b) => (b.mtime || 0) - (a.mtime || 0)).slice(0, 8);
  const items = [];
  if (ps.length) items.push({head: '最近的项目'}, ...ps.map(p => ({label: p.name || p.cwd, sub: homeShort(p.cwd), fn: () => setCwd(p.cwd)})), {sep: true});
  items.push({label: '选择其他文件夹…', fn: async () => {
    const d = await api('add-folder').catch(e => { toast(e.message); return {}; });
    if (d && d.cwd) { setCwd(d.cwd); if (C.refreshSide) C.refreshSide(true); }
  }});
  if (P && P.defaultCwd) items.push({label: '不指定', fn: () => setCwd(null)});
  C.menu(btn, items, {alignRight: true, wide: true});
}

/* ---------- 技能与 MCP：复用 feat.js 的扩展页 ---------- */
function rExt() {
  body.innerHTML = head('扩展', '技能、MCP 服务、插件，以及钩子和权限规则') + '<div class="sj-ext"></div>';
  const box = body.querySelector('.sj-ext');
  if (C.renderExtInto) C.renderExtInto(box);
  else box.innerHTML = '<div class="sj-empty">扩展管理没有加载</div>';
}

/* ---------- 网关与账号、关于：数据来自 about ---------- */
let ABOUT = null;
async function loadAbout(g, sec, draw) {
  if (ABOUT) draw();
  else body.querySelector('.sj-grp').innerHTML = '<div class="sj-empty">读取中…</div>';
  try { const a = await api('about'); if (g !== gen) return; ABOUT = a || {}; draw(); }
  catch (e) {
    if (g !== gen || ABOUT) return;
    body.querySelector('.sj-grp').innerHTML = errBox('读取失败：' + e.message, true);
    bind(body, {}, {retry: RENDER[sec]});
  }
}
/* ================= 第十四轮补（11-11）：额度 =================
   数据来自后端 api('quota')：只读用户状态栏脚本写的 ~/.claude/.crs-quota.json（5 小时、7 天、Fable 等窗口的已用百分比和重置时间）。
   余裕＝窗口已过去的百分比－已用百分比，与状态栏一致；为负时显示“无余裕”。用量页顶部和“网关与账号”各放一张。 */
const aboutT = s => { s = Math.max(0, s); const d = Math.floor(s / 86400), h = Math.round(s / 3600), m = Math.max(1, Math.round(s / 60));
  return d >= 2 ? `约 ${Math.round(s / 86400)} 天` : h >= 1 ? `约 ${h} 小时` : `约 ${m} 分钟`; };
function quotaInner(q) {
  if (!q) return '<div class="sj-dim sj-qnone">读取中…</div>';
  if (q.err) return `<div class="sj-qnone sj-qerr">${esc(q.err)}</div>`;
  const ws = q.windows || [];
  if (!ws.length) return '<div class="sj-dim sj-qnone">暂无额度数据</div>';
  const now = Date.now() / 1000;
  return ws.map(w => {
    const u = Math.max(0, Math.min(100, +w.used || 0)), hr = w.headroom;
    const lvl = u >= 95 ? 'red' : (hr != null && hr < 0) || u >= 80 ? 'amber' : 'ok';
    const reset = w.resetAt ? aboutT(w.resetAt - now) + '后重置' : '';
    const room = hr == null ? '' : hr >= 0 ? `余裕 ${hr}` : '<span class="no">无余裕</span>';
    return `<div class="sj-q ${lvl}"><span class="sj-qn">${esc(w.label || w.key || '')}</span><span class="sj-qbar"><i style="width:${u}%"></i></span>` +
      `<b class="sj-qu">${Math.round(u)}%</b><span class="sj-qm">${[reset ? esc(reset) : '', room].filter(Boolean).join(' · ')}</span></div>`;
  }).join('');
}
function quotaCard(el) {
  if (!el) return;
  const draw = q => {
    if (!el.isConnected) return;
    const upd = q && q.ts ? (q.stale ? `${aboutT(q.age || 0)}前的数据` : '更新于 ' + hm(q.ts * 1000)) : '';
    el.innerHTML = `<div class="sj-qh">额度<span${q && q.stale ? ' class="stale"' : ''}>${esc(upd)}</span></div>${quotaInner(q)}`;
  };
  const load = () => api('quota').then(draw, () => draw({windows: []}));
  draw(null); load();
  const t = setInterval(() => { if (!el.isConnected) clearInterval(t); else load(); }, 60000);
}
let uaBusy = false;
function uaHtml(u) {
  if (uaBusy) return '<span class="sj-ua"><span class="sj-spin"></span>检测中…</span>';
  if (!u) return '<span class="sj-dim">未检测</span>';
  const meta = [u.xapp ? 'x-app ' + u.xapp : '', u.ms ? Math.round(u.ms) + ' ms' : '', u.at ? '检测于 ' + mdhm(u.at) : ''].filter(Boolean).join(' · ');
  return `<span class="sj-ua ${u.ok ? 'ok' : 'bad'}" title="${esc(meta)}"><b>${u.ok ? '✓' : '✗'}</b><code>${esc(u.ua || u.error || (u.ok ? '' : '没有抓到请求头'))}</code></span>`;
}
function rGateway() {
  const g = gen;
  body.innerHTML = head('网关与账号', '请求经由的网关和令牌，请求头自检确认没有被改动') + '<div class="sj-grp"></div>';
  const draw = () => {
    const a = ABOUT || {}, gw = a.gateway || {}, cli = a.cli || {}, u = a.uaCheck;
    body.querySelector('.sj-grp').outerHTML = '<div class="sj-grp">' +
      row('网关', `<span class="sj-path" title="${esc(gw.host || '')}">${esc(gw.host || '官方默认')}</span>`) +
      row('令牌', gw.tokenMasked ? `<code class="sj-code">${esc(maskTok(gw.tokenMasked))}</code>` : '<span class="sj-dim">未设置</span>') +
      row('CLI 版本', esc(cli.version || '—')) +
      row('请求头自检', uaHtml(u) + `<button type="button" class="sj-btn" data-a="ua"${uaBusy ? ' disabled' : ''}>${u ? '重新检测' : '检测'}</button>`,
        u && !uaBusy && !u.ok && u.error && u.ua ? esc(u.error) : '', 'sj-uarow') + '</div>';
    if (!body.querySelector('.sj-qcard')) { const qc = document.createElement('div'); qc.className = 'sj-qcard'; body.appendChild(qc); quotaCard(qc); }  // 第十四轮补：额度
    bind(body, {}, {ua: runUa});
  };
  const runUa = async () => {
    if (uaBusy) return;
    uaBusy = true; draw();
    let r;
    try { r = await api('ua-check'); } catch (e) { r = {ok: false, error: e.message, at: Date.now()}; }
    uaBusy = false;
    ABOUT = Object.assign({}, ABOUT, {uaCheck: r});
    if (cur === 'gateway') draw();
  };
  loadAbout(g, 'gateway', draw);
}
function rAbout() {
  const g = gen;
  body.innerHTML = head('关于', '版本、后台服务、日志和备份') + '<div class="sj-grp"></div>';
  let busy = false;
  const draw = () => {
    const a = ABOUT || {}, app = a.app || {}, cli = a.cli || {}, logs = a.logs || {}, bk = a.backups || [];
    const LOGS = [['server', '服务日志'], ['ptyd', '终端守护日志'], ['restart', '重启日志']].filter(([k]) => logs[k]);
    body.querySelector('.sj-ab') && body.querySelector('.sj-ab').remove();
    const html = '<div class="sj-ab"><div class="sj-grp">' +
      row('工作台版本', esc((app.version || '—') + (app.round ? ` · 第 ${app.round} 轮` : ''))) +
      row('CLI', `<span class="sj-path" title="${esc(cli.path || '')}">${esc([cli.version, cli.path ? homeShort(cli.path) : ''].filter(Boolean).join(' · ') || '—')}</span>`) +
      row('后台服务', (a.port ? `<span class="sj-dim">端口 ${esc(a.port)}</span>` : '') + `<button type="button" class="sj-btn" data-a="restart"${busy ? ' disabled' : ''}>${busy ? '正在重启…' : '重启后台服务'}</button>`, '正在进行的对话不受影响') +
      row('日志', LOGS.length ? LOGS.map(([k, l]) => `<button type="button" class="sj-btn" data-a="log" data-k="${k}">${l}</button>`).join('') : '<span class="sj-dim">—</span>', '', 'sj-wrap') +
      '</div><div class="sj-sub">备份</div><div class="sj-grp sj-bk">' +
      (bk.length ? bk.map((b, i) => row(`<span class="sj-bkn" title="${esc(b.path || '')}">${esc(b.name || b.path || '')}</span>`,
        `<span class="sj-dim">${b.mtime ? esc(mdhm(b.mtime)) : ''}</span><button type="button" class="sj-btn" data-a="bk" data-i="${i}">在访达中显示</button>`)).join('')
        : '<div class="sj-row"><span class="sj-dim">没有备份</span></div>') + '</div></div>';
    const grp = body.querySelector(':scope > .sj-grp');
    if (grp) grp.outerHTML = html; else body.insertAdjacentHTML('beforeend', html);
    bind(body, {}, {
      restart: async () => {
        if (busy || !confirm('重启后台服务？\n正在进行的对话不受影响。')) return;
        busy = true; draw();
        try { await api('restart-service'); toast('后台服务正在重启，正在进行的对话不受影响'); }
        catch (e) { toast('重启失败：' + e.message); }
        setTimeout(() => { busy = false; if (cur === 'about') draw(); }, 4000);
      },
      log: (e, b) => reveal(logs[b.dataset.k]),
      bk: (e, b) => reveal((bk[+b.dataset.i] || {}).path),
    });
  };
  loadAbout(g, 'about', draw);
}
const reveal = p => { if (p) api('reveal', {path: p}).then(r => { if (r && r.ok === false) toast('找不到：' + p); }).catch(e => toast(e.message)); };

/* ================= 用量 ================= */
const U = {range: sget('sj.range', '30d'), metric: sget('sj.metric', 'tokens'), stack: sget('sj.stack', 'model'), cache: {}, timer: 0, seq: 0, more: false, prov: ''};
if (!['today', '7d', '30d'].includes(U.range)) U.range = '30d';
if (!['tokens', 'cost', 'requests'].includes(U.metric)) U.metric = 'tokens';
if (!['model', 'total'].includes(U.stack)) U.stack = 'model';
function stopUsage() { clearTimeout(U.timer); U.timer = 0; U.seq++; }
/* 11-23：同一个老页面也给 Codex、Antigravity 用（ux-u.js 传 {provider}）：数据取 usage {range, legacy: 提供方}，字段和 Claude 的一样；
   不传参数时就是 Claude，和原来一模一样。和 Claude 不同的地方：没有价格时（kpi.cost 为 null）不显示钱，图表没有“费用”；
   Codex 的“估算费用”换成“占周额度”；活跃度只统计 heatSince 之后 */
const PNAME = {codex: 'Codex', agy: 'Antigravity'};
/* 11-26：Claude 统计范围，和 ux-u.js 共用偏好 cwb.uu.cscope：all（默认）含终端、VS Code；wb 只算工作台 */
const cscope = () => { try { return JSON.parse(localStorage.getItem('cwb.uu.cscope')) === 'wb' ? 'wb' : 'all'; } catch { return 'all'; } };
const noCost = d => !!U.prov && (!d.kpi || d.kpi.cost == null);
const metricOf = d => (U.metric === 'cost' && noCost(d) ? 'tokens' : U.metric);
function rUsage(o) {
  U.prov = (o && PNAME[o.provider] && o.provider) || '';
  body.innerHTML = head('用量', (cscope() === 'wb' ? '只统计在工作台里运行的对话' : '统计本机所有 Claude 对话（含终端和 VS Code）') + ' · 费用按官方价估算，实际以网关账单为准') +
    '<div class="sj-qcard"></div>' +  // 第十四轮补：额度
    `<div class="sj-ubar">${seg('range', [['today', '今天'], ['7d', '7 天'], ['30d', '30 天']], U.range, '时间范围')}<span class="grow"></span><span class="sj-upd"></span></div><div class="sj-ubody"></div>`;
  quotaCard(body.querySelector('.sj-qcard'));
  bind(body.querySelector('.sj-ubar'), {range: v => { U.range = v; sset('sj.range', v); U.more = false; loadUsage(); }});
  loadUsage();
}
async function loadUsage() {
  stopUsage();
  const seq = U.seq, range = U.range, prov = U.prov, cs = cscope(), ck = prov ? prov + '|' + range : range + '|' + cs, box = body.querySelector('.sj-ubody');
  if (!box) return;
  const cached = U.cache[ck];
  if (cached) drawUsage(cached); else { box.innerHTML = '<div class="sj-empty">读取中…</div>'; body.querySelector('.sj-upd').textContent = ''; }
  let d;
  try { d = await api('usage', prov ? {range, legacy: prov} : {range, claudeScope: cs}); }
  catch (e) {
    if (seq !== U.seq || cur !== 'usage') return;
    if (!cached) { box.innerHTML = errBox('用量统计暂时不可用：' + e.message, true); bind(box, {}, {retry: loadUsage}); }
    return;
  }
  if (seq !== U.seq || cur !== 'usage') return;
  d = d || {};
  const same = cached && JSON.stringify(cached) === JSON.stringify(d);
  U.cache[ck] = d;
  if (!same) drawUsage(d);
  if (d.scanning) U.timer = setTimeout(() => { if (cur === 'usage' && seq === U.seq) loadUsage(); }, 2000);
}
/* 缓存命中省下的钱：缓存读的 Token 若按普通输入计价要多花 (输入单价 − 缓存读单价)。
   各模型的缓存读量按它的 Token 占比分摊；单价为官方价（美元 / 百万 Token），Fast 加价不计 */
const PRICE = [[/fable-5-2|mythos-5-2/, 8, 0.2], [/fable|mythos/, 10, 1], [/opus-5-5/, 4, 0.2], [/opus/, 5, 0.5],
  [/sonnet-5-5/, 2, 0.1], [/sonnet-5/, 2, 0.2], [/sonnet/, 3, 0.3], [/haiku-5-5/, 0.1, 0.01], [/haiku/, 1, 0.1]];
const priceOf = id => (PRICE.find(([re]) => re.test(String(id || '').toLowerCase())) || [null, 5, 0.5]).slice(1);
function savedUsd(d) {
  const cr = (d.kpi && d.kpi.cacheRead) || 0, ms = d.models || [];
  if (!cr) return 0;
  const tot = ms.reduce((s, m) => s + (m.tokens || 0), 0);
  if (!ms.length || !tot) { const [pi, pc] = priceOf(''); return cr * (pi - pc) / 1e6; }
  return ms.reduce((s, m) => { const sh = m.share != null ? m.share : (m.tokens || 0) / tot; const [pi, pc] = priceOf(m.id); return s + cr * sh * (pi - pc) / 1e6; }, 0);
}
const kpi = (k, v, d, full) => `<div class="sj-kpi"><div class="k">${k}</div><div class="v" title="${esc(full || v)}">${v}</div><div class="d">${d}</div></div>`;
/* 指标卡上的金额：上千美元不显示分（悬停看完整值）；“省下约”只到整数 */
const usdK = n => (+n || 0) >= 1000 ? '$' + Math.round(+n).toLocaleString('en-US') : fmtUsd(n);
const usdAbout = n => (+n || 0) >= 100 ? '$' + Math.round(+n).toLocaleString('en-US') : fmtUsd(n);
/* 第三张指标卡：Claude 是“估算费用”（原样）；11-23 起 Codex 没有价格，换成“占周额度”（写法同 ux-u.js）；agy 有报的费用才显示金额 */
function costKpi(d, n, range) {
  const k = d.kpi || {}, t = d.today || {};
  if (!U.prov) return kpi('估算费用', usdK(k.cost), range === 'today' ? '每次 ' + fmtUsd((k.cost || 0) / n) : '今天 ' + fmtUsd(t.cost), fmtUsd(k.cost));
  if (k.cost == null && U.prov === 'codex') {
    const w = k.weekQuota;
    return kpi('占周额度', w != null && isFinite(w) ? `约 ${+w < 10 ? (+w).toFixed(1).replace(/\.0$/, '') : Math.round(+w)}%` : '—', '订阅计费，不算钱');
  }
  if (k.cost == null) return kpi('费用', '—', `${PNAME[U.prov]} 没有报告费用`);
  const pr = k.pricedRequests != null ? +k.pricedRequests : n;  // 只有部分请求报了费用时不摊“每次”，直接写几次报了费用
  return kpi('费用', usdK(k.cost), range !== 'today' ? '今天 ' + (t.cost == null ? '—' : fmtUsd(t.cost))
    : pr >= (k.requests || 0) ? '每次 ' + fmtUsd(k.cost / n) : `${fmtInt(pr)} 次报了费用`, fmtUsd(k.cost));
}
/* 柱状图按模型堆叠：本范围用量前 4 的模型各一色（c1～c4），其余合并为“其他”（c0）；模型 Top 4 的占比条用同一套颜色 */
const topIds = d => (d.models || []).slice(0, 4).map(m => m.id);
const mLabel = (d, id) => { const m = (d.models || []).find(x => x.id === id); return (m && (m.label || m.id)) || id || '未知模型'; };
function parts(b, top, get) {
  const ms = b && b.models;
  if (!ms || typeof ms !== 'object') return null;
  const out = top.map((id, i) => ({c: i + 1, id, v: ms[id] ? +get(ms[id]) || 0 : 0}));
  const rest = Object.keys(ms).filter(id => !top.includes(id)).reduce((a, id) => a + (+get(ms[id]) || 0), 0);
  out.push({c: 0, id: '', v: rest});
  return out;
}
const METRIC = {tokens: ['Token', b => b.tokens, fmtTok], cost: ['费用', b => b.cost, fmtUsd], requests: ['请求', b => b.requests, fmtInt]};
function drawUsage(d) {
  const box = body.querySelector('.sj-ubody'), upd = body.querySelector('.sj-upd');
  if (!box || !upd) return;
  hideTip();
  const k = d.kpi || {}, t = d.today || {}, range = d.range || U.range, has = (k.requests || 0) > 0;
  upd.innerHTML = d.scanning && has ? '<span class="sj-spin"></span>正在统计…' : d.updated ? '更新于 ' + hm(d.updated) : '';
  if (!has && d.scanning) { box.innerHTML = '<div class="sj-empty sj-scan"><span class="sj-spin"></span>正在统计…</div>'; return; }
  const heat = d.heat || [];
  let html = '';
  if (!has) {
    html += '<div class="sj-none"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>' +
      `<b>${range === 'today' ? '今天' : '这段时间'}还没有用量</b><span>${U.prov ? `用 ${PNAME[U.prov]} 发起对话后，这里会显示请求和 Token` : '在工作台里发起对话后，这里会显示请求、Token 和费用'}</span></div>`;
  } else {
    const n = k.requests || 1, saved = U.prov ? 0 : k.saved != null ? +k.saved : savedUsd(d);
    html += '<div class="sj-kpis">' +
      kpi('请求', fmtInt(k.requests), range === 'today' ? '每次 ' + fmtTok((k.tokens || 0) / n) + ' Token' : '今天 ' + fmtInt(t.requests)) +
      kpi('Token', fmtTok(k.tokens), '输出 ' + fmtTok(k.output)) +
      costKpi(d, n, range) +
      kpi('缓存命中', fmtPct(k.cacheHit), U.prov ? '缓存读 ' + fmtTok(k.cacheRead) : '省下约 ' + usdAbout(saved)) +
      kpi('输出速度', fmtSpeed(k.speed), k.approxSpeed ? '中位数 · 估算' : '中位数') + '</div>';
    const ms = (d.models || []).slice(0, 4), tot = (d.models || []).reduce((s, m) => s + (m.tokens || 0), 0) || 1;
    html += '<div class="sj-cards2"><div class="sj-card sj-chart">' +
      `<h4><span class="t">${d.unit === 'hour' ? '今天每小时' : '每日用量'}</span><span class="grow"></span>` +
      `<select class="sj-sel sj-sel-s" data-k="stack" aria-label="柱子分色">${opts([['total', '合计'], ['model', '按模型']], U.stack)}</select>` +
      `${seg('metric', [['tokens', 'Token'], ...(noCost(d) ? [] : [['cost', '费用']]), ['requests', '请求']], metricOf(d), '图表指标')}</h4>` +
      '<div class="sj-bars"></div><div class="sj-axis"></div><div class="sj-mlg"></div></div>' +
      '<div class="sj-card sj-top4"><h4>模型 Top 4</h4><div class="tr hd"><span>模型</span><span>占比</span><span class="n">Token</span><span class="s">速度</span></div>' +
      (ms.length ? ms.map((m, i) => {
        const sh = m.share != null ? m.share : (m.tokens || 0) / tot;
        return `<div class="tr"><span class="nm" title="${esc(m.id || '')}">${esc(m.label || m.id || '')}</span><span class="b" title="${fmtPct(sh)}"><i class="c${i + 1}" style="width:${Math.max(0, Math.min(100, sh * 100)).toFixed(1)}%"></i></span>` +
          `<span class="n">${fmtTok(m.tokens)}</span><span class="s">${fmtSpeed(m.speed)}</span></div>`;
      }).join('') : '<div class="sj-dim sj-pad">—</div>') + '</div></div>';
  }
  if (heat.some(h => (h.tokens || 0) > 0 || (h.requests || 0) > 0)) {
    html += `<div class="sj-card sj-heatc"><h4>近一年活跃度${d.heatSince ? `<span class="sj-hnote">只统计 ${esc(String(d.heatSince).slice(5))} 以后</span>` : ''}<span class="grow"></span><span class="sj-legend">少` +
      [0, 1, 2, 3, 4].map(i => `<i class="l${i}"></i>`).join('') + '多</span></h4>' + heatHtml(heat, d.heatSince) + '</div>';
  }
  if (has) {
    const ss = (d.sessions || []).slice(0, 10);
    if (ss.length) {
      html += '<div class="sj-card sj-sess"><h4>用量最多的对话</h4>' + ss.map(s =>
        `<div class="sr">${s.sid ? `<button type="button" class="sj-link" data-sid="${esc(s.sid)}" title="${esc(s.title || s.sid)}">${esc(s.title || '未命名对话')}</button>`
          : `<span class="sj-snm" title="${esc((s.title || '未命名对话') + ' · 不是在工作台里开的对话')}">${esc(s.title || '未命名对话')}</span>`}` +  // 11-23：Codex、agy 终端里的对话没有工作台 sid，不能点开
        `<span class="n">${fmtInt(s.requests)} 次</span><span class="n">${fmtTok(s.tokens)}</span><span class="n c">${s.cost == null ? '—' : fmtUsd(s.cost)}</span></div>`).join('') + '</div>';
    }
    html += recentHtml(d.recent || [], range);
  }
  box.innerHTML = html;
  if (has) {
    drawBars(d);
    bind(box.querySelector('.sj-chart h4'), {metric: v => { U.metric = v; sset('sj.metric', v); drawBars(d); }, stack: v => { U.stack = v; sset('sj.stack', v); drawBars(d); }});
  }
  box.querySelectorAll('.sj-link[data-sid]').forEach(b => b.onclick = () => goSession(b.dataset.sid));
  const mb = box.querySelector('[data-a=more]');
  if (mb) mb.onclick = () => { U.more = true; drawUsage(d); };
  wireTips(box, d);
}
function drawBars(d) {
  const card = body.querySelector('.sj-chart');
  if (!card) return;
  hideTip();
  const bs = d.buckets || [], [, get] = METRIC[metricOf(d)], max = Math.max(0, ...bs.map(b => +get(b) || 0));
  const top = topIds(d), stk = U.stack === 'model' && top.length > 0 && bs.some(b => b.models);
  const used = new Set();
  const bars = card.querySelector('.sj-bars');
  bars.innerHTML = bs.map((b, i) => {
    const v = +get(b) || 0, h = max ? Math.max(v ? 2 : 0, v / max * 100) : 0;
    let seg = '';
    const ps = stk && v ? parts(b, top, get) : null, sum = ps ? ps.reduce((a, p) => a + p.v, 0) : 0;
    if (ps && sum > 0) {
      seg = ps.filter(p => p.v > 0).map(p => { used.add(p.c); return `<s class="c${p.c}" style="height:${(p.v / sum * 100).toFixed(3)}%"></s>`; }).join('');
    }
    return `<div class="sj-bar" data-i="${i}"><i style="height:${h.toFixed(2)}%">${seg}</i></div>`;
  }).join('');
  bars.classList.toggle('flat', !max);
  bars.classList.toggle('stk', stk);
  /* 图例：按模型时列出前 4 个模型，范围里有其余模型时加“其他” */
  const lg = card.querySelector('.sj-mlg');
  if (lg) {
    const its = stk ? top.map((id, i) => [i + 1, mLabel(d, id), id]).concat((d.models || []).length > 4 ? [[0, '其他', '']] : []) : [];
    lg.innerHTML = its.map(([c, l, id]) => `<span${id ? ` title="${esc(id)}"` : ''}><i class="c${c}"></i>${esc(l)}</span>`).join('');
    lg.hidden = !its.length;
  }
  const n = bs.length, lab = b => esc(b.label || b.key || '');
  const idx = n <= 1 ? [0] : d.unit === 'hour' ? [0, 6, 12, 18, n - 1].filter(i => i < n) : [...new Set([0, Math.round((n - 1) / 3), Math.round((n - 1) * 2 / 3), n - 1])];
  card.querySelector('.sj-axis').innerHTML = n ? idx.map((i, j) => {
    const pos = j === 0 ? 'left:0' : j === idx.length - 1 ? 'right:0' : `left:${((i + 0.5) / n * 100).toFixed(2)}%;transform:translateX(-50%)`;
    return `<span style="${pos}">${lab(bs[i])}</span>`;
  }).join('') : '';
}
function heatHtml(heat, since) {  // since（11-23，Codex、agy）：这天之前没有统计，格子标 data-na
  const map = new Map(heat.map(h => [h.date, h]));
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const wd = (today.getDay() + 6) % 7;  // 每列一周，周一在上
  const start = new Date(today); start.setDate(start.getDate() - (52 * 7 + wd));
  const vals = heat.map(h => +h.tokens || 0).filter(v => v > 0).sort((a, b) => a - b);
  const q = f => vals.length ? vals[Math.min(vals.length - 1, Math.floor(vals.length * f))] : 0;
  const q1 = q(0.25), q2 = q(0.5), q3 = q(0.75);
  const lv = v => !v ? 0 : v <= q1 ? 1 : v <= q2 ? 2 : v <= q3 ? 3 : 4;
  let cells = '', months = '', lastM = -1;
  const dt = new Date(start);
  for (let c = 0; c < 53; c++) {
    for (let r = 0; r < 7; r++) {
      if (dt > today) { cells += '<i class="x"></i>'; }
      else {
        const key = dkey(dt), h = map.get(key);
        const v = h ? (+h.tokens || 0) : 0, req = h ? (+h.requests || 0) : 0, l = lv(v) || (req ? 1 : 0);
        cells += `<i data-d="${key}" data-t="${v}" data-r="${req}"${l ? ` class="l${l}"` : ''}${since && key < since ? ' data-na="1"' : ''}></i>`;
        if (r === 0 && dt.getMonth() !== lastM) {
          if (lastM !== -1 || dt.getDate() <= 7) months += `<span style="left:${(c / 53 * 100).toFixed(2)}%">${dt.getMonth() + 1}月</span>`;
          lastM = dt.getMonth();
        }
      }
      dt.setDate(dt.getDate() + 1);
    }
  }
  return `<div class="sj-months">${months}</div><div class="sj-heat">${cells}</div>`;
}
/* 时间：窄宽度时只留“时:分”（日期、秒用 CSS 隐藏） */
const tcell = (ts, day) => { const d = new Date(toMs(ts)); return (day ? `<span class="dd">${p2(d.getMonth() + 1)}-${p2(d.getDate())} </span>` : '') + hm(ts) + (day ? '' : `<span class="ss">:${p2(d.getSeconds())}</span>`); };
function recentHtml(list, range) {
  if (!list.length) return '';
  const rows = U.more ? list : list.slice(0, 30), day = range !== 'today';
  return `<div class="sj-card sj-recent"><h4>最近请求</h4><table class="sj-tbl${day ? ' days' : ''}"><thead><tr>` +
    '<th class="c-t">时间</th><th class="c-m">模型</th><th class="r c-n o2">输入</th><th class="r c-n">输出</th>' +
    '<th class="r c-n o1">缓存读</th><th class="r c-ms o1">耗时</th><th class="r c-sp">速度</th></tr></thead><tbody>' +
    rows.map(x => `<tr><td class="c-t" title="${esc(mdhm(x.ts))}">${tcell(x.ts, day)}</td>` +
      `<td class="c-m"><span class="mw"><span class="mn" title="${esc(x.model || '')}">${esc(x.label || x.model || '—')}</span>${x.fast ? `<span class="sj-tag fast">${IC_BOLT}Fast</span>` : ''}</span></td>` +
      `<td class="r o2">${fmtTok(x.input)}</td><td class="r">${fmtTok(x.output)}</td><td class="r o1">${fmtTok(x.cacheRead)}</td>` +
      `<td class="r o1">${fmtMs(x.ms)}</td><td class="r">${fmtSpeed(x.speed)}</td></tr>`).join('') +
    '</tbody></table>' + (!U.more && list.length > rows.length ? `<div class="sj-more"><button type="button" class="sj-btn" data-a="more">显示全部 ${list.length} 条</button></div>` : '') + '</div>';
}
function goSession(sid) {
  if (!sid) return;
  close();
  C.openSession(sid);
}
/* 悬停提示：柱子、热力格 */
function wireTips(box, d) {
  const bars = box.querySelector('.sj-bars');
  if (bars) {
    bars.onmouseover = e => {
      const b = e.target.closest('.sj-bar'); if (!b) return;
      const x = (d.buckets || [])[+b.dataset.i]; if (!x) return;
      bars.querySelectorAll('.sj-bar.on').forEach(y => y.classList.remove('on'));
      b.classList.add('on'); bars.classList.add('hov');
      const [mName, get, fmt] = METRIC[metricOf(d)], ps = (parts(x, topIds(d), get) || []).filter(p => p.v > 0);
      const rows = ps.length ? `<div class="sj-tm"><em>按模型 · ${mName}</em>` + ps.map(p =>
        `<i class="c${p.c}"></i><span class="l">${esc(p.c ? mLabel(d, p.id) : '其他')}</span><span class="n">${fmt(p.v)}</span>`).join('') + '</div>' : '';
      showTip(b, `<b>${esc(x.label || x.key || '')}</b><span>Token ${fmtTok(x.tokens)} · 输出 ${fmtTok(x.output)}</span><span>请求 ${fmtInt(x.requests)}${noCost(d) ? '' : ' · ' + (x.cost == null ? '费用 —' : fmtUsd(x.cost))}</span>` + rows);
    };
    bars.onmouseleave = () => { bars.querySelectorAll('.sj-bar.on').forEach(y => y.classList.remove('on')); bars.classList.remove('hov'); hideTip(); };
  }
  const heat = box.querySelector('.sj-heat');
  if (heat) {
    heat.onmouseover = e => {
      const c = e.target.closest('i[data-d]'); if (!c) { hideTip(); return; }
      const dd = new Date(c.dataset.d + 'T00:00:00'), v = +c.dataset.t, r = +c.dataset.r;
      showTip(c, `<b>${c.dataset.d} 周${WD[dd.getDay()]}</b><span>${c.dataset.na ? '不在统计范围（只统计近 30 天）' : v || r ? `${fmtTok(v)} Token · ${fmtInt(r)} 次请求` : '没有用量'}</span>`);
    };
    heat.onmouseleave = hideTip;
  }
}
function showTip(target, html) {
  if (!tip) return;
  tip.innerHTML = html; tip.hidden = false;
  const r = target.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  let x = r.left + r.width / 2 - w / 2, y = r.top - h - 8;
  if (y < 6) y = r.bottom + 8;
  x = Math.max(6, Math.min(x, innerWidth - w - 6));
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip() { if (tip) tip.hidden = true; }

const RENDER = {general: rGeneral, notify: rNotify, chat: rChat, usage: rUsage, ext: rExt, gateway: rGateway, about: rAbout};
/* 第十五轮 F2：ux-l.js 往设置里加“记忆与指令”分区、往“通用”里加行，复用这里的小部件 */
C.sj = {SECS, RENDER, head, row, seg, sw, setSw, bind, errBox, setPref, pref, close, isOpen, body: () => body, gen: () => gen};

/* ================= 原生 App：拖动窗口、双击标题区 ================= */
/* [data-drag] 区域里按下（目标不在按钮、输入框、链接、[data-nodrag] 内）：交给外壳 performDrag；双击按系统设置缩放或最小化 */
if (NATIVE) {
  const NODRAG = 'button,input,select,textarea,a,label,summary,[contenteditable=""],[contenteditable="true"],[role=button],[data-nodrag]';
  document.addEventListener('mousedown', e => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || !root.classList.contains('native-inset') || root.classList.contains('native-fs')) return;
    const t = e.target instanceof Element ? e.target : null;
    if (!t || !t.closest('[data-drag]') || t.closest(NODRAG)) return;
    if (e.detail > 2) return;
    e.preventDefault();
    native({type: e.detail === 2 ? 'winDbl' : 'winDrag'});
  }, true);
}
})();
