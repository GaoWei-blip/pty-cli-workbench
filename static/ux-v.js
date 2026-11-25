(() => {
'use strict';
/* 第十六轮 P：Codex、Antigravity 对话的界面。
   1. 新对话首页：选 Claude / Codex / Antigravity（默认 Claude），没装、没登录时给一句怎么处理；首页发送时按选的新建对话。
   2. 对话里：模型、思考强度、权限模式换成各家自己的列表（输入栏的三个药丸）；Claude 专属的功能藏起来。
   3. 左栏：非 Claude 对话带提供方小标识；删除时确认框写清楚只删工作台这边的记录。
   4. 授权卡：标出是哪家要确认，“总是允许”换成各家的说法，多个文件的修改逐个列出差异。
   5. 用户消息里的图片显示缩略图。
   复核修复：实时预览按增量接起来（7）；出错时没发出去的排队消息放回输入框（6）；
   选了 Codex / Antigravity 时不预热 Claude、“重启后台服务”说明要等这两家这一轮结束（8）。
   钩子都串在原有处理之后（chain），不改已有文件的逻辑；app.js、ux-h.js 等只读它们导出的东西。 */
const C = window.CWB;
if (!C) return;
const {S, $, esc, api, toast} = C;
const store = C.store || {get: (k, d) => d, set() {}};
const body = document.body, input = $('#input'), composer = $('#composer'), bar = composer.querySelector('.bar');
const chain = (name, fn) => { const prev = C[name]; C[name] = (...a) => { try { if (prev) prev(...a); } finally { try { fn(...a); } catch (e) { console.error(e); } } }; };
const mk = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const LABEL = {claude: 'Claude', codex: 'Codex', agy: 'Antigravity'};
const CHEV = '<svg class="h-i h-chev" viewBox="0 0 10 10" aria-hidden="true"><path d="M2.6 3.9 5 6.3l2.4-2.4"/></svg>';
const CHECK = '<svg class="h-i h-sm" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.4 8.4 6.6 11.4 12.6 4.8"/></svg>';
const SHIELD = '<svg class="h-i" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.9 3.1 3.7v3.9c0 3.1 2.1 5.4 4.9 6.5 2.8-1.1 4.9-3.4 4.9-6.5V3.7z"/></svg>';
const V = {prov: 'claude', status: null, stT: 0, models: {}, mT: {}, homeOpts: {codex: {}, agy: {}}, cur: '', curD: null, pop: null};

/* ================= 当前是哪家 ================= */
function provOf(sid) {
  if (!sid) return '';
  if (sid === S.cur && V.cur) return V.cur;
  const s = C.findSession(sid);
  return (s && s.prov) || '';
}
/* 当前界面对应的提供方：有对话按对话；首页按首页的选择 */
function curProv() { return S.cur ? (V.cur || provOf(S.cur) || 'claude') : (isHome() ? V.prov : 'claude'); }
const isHome = () => !!(C.home && C.home.isHome && C.home.isHome());
const isPv = () => curProv() !== 'claude';
function syncBody() {
  const p = curProv(), on = p !== 'claude';
  body.classList.toggle('pv-on', on);
  if (on) body.dataset.pv = p; else delete body.dataset.pv;
  renderBadge(); renderPills(); renderSeg();
  /* “/”补全：这两家只列它自己报上来的命令（Antigravity 的 available_commands_update），不列 Claude 的命令和技能 */
  if (on) S.commands = {cwd: S.cwd, list: S.cur ? ((V.curD && V.curD.pvCmds) || []).map(c => ({name: c.name, desc: c.desc, src: LABEL[p]})) : [], _pv: true};
  else if (S.commands && S.commands._pv) S.commands = {cwd: null, list: []};
}

/* ================= 状态（装没装、登没登录） ================= */
async function provStatus(force) {
  if (V.status && !force && Date.now() - V.stT < 60000) return V.status;
  try { const d = await api('prov-status', force ? {refresh: true} : {}); V.status = {}; for (const p of d.provs || []) V.status[p.id] = p; V.stT = Date.now(); }
  catch { V.status = V.status || {}; }
  return V.status;
}
async function provModels(prov, force) {
  if (!force && V.models[prov] && Date.now() - (V.mT[prov] || 0) < 300000 && !V.models[prov].error) return V.models[prov];
  try { V.models[prov] = await api('prov-models', {prov, sid: S.cur && provOf(S.cur) === prov ? S.cur : ''}); V.mT[prov] = Date.now(); }
  catch (e) { V.models[prov] = V.models[prov] || {models: [], modes: [], error: e.message}; }
  return V.models[prov];
}

/* ================= 1. 首页：选提供方 ================= */
const seg = mk('div', 'pv-seg');
seg.id = 'pvSeg'; seg.setAttribute('role', 'radiogroup'); seg.setAttribute('aria-label', '用哪个模型服务');
const hint = mk('div', 'pv-hint');
hint.id = 'pvHint';
const heroIcon = $('#hProviderIcon');
const HOME_ICONS = {
  claude: heroIcon.innerHTML,
  codex: '<svg class="pv-logo pv-logo-codex" viewBox="0 0 24 24" fill="currentColor" fill-rule="evenodd" aria-hidden="true"><path clip-rule="evenodd" d="M8.086.457a6.105 6.105 0 013.046-.415c1.333.153 2.521.72 3.564 1.7a.117.117 0 00.107.029c1.408-.346 2.762-.224 4.061.366l.063.03.154.076c1.357.703 2.33 1.77 2.918 3.198.278.679.418 1.388.421 2.126a5.655 5.655 0 01-.18 1.631.167.167 0 00.04.155 5.982 5.982 0 011.578 2.891c.385 1.901-.01 3.615-1.183 5.14l-.182.22a6.063 6.063 0 01-2.934 1.851.162.162 0 00-.108.102c-.255.736-.511 1.364-.987 1.992-1.199 1.582-2.962 2.462-4.948 2.451-1.583-.008-2.986-.587-4.21-1.736a.145.145 0 00-.14-.032c-.518.167-1.04.191-1.604.185a5.924 5.924 0 01-2.595-.622 6.058 6.058 0 01-2.146-1.781c-.203-.269-.404-.522-.551-.821a7.74 7.74 0 01-.495-1.283 6.11 6.11 0 01-.017-3.064.166.166 0 00.008-.074.115.115 0 00-.037-.064 5.958 5.958 0 01-1.38-2.202 5.196 5.196 0 01-.333-1.589 6.915 6.915 0 01.188-2.132c.45-1.484 1.309-2.648 2.577-3.493.282-.188.55-.334.802-.438.286-.12.573-.22.861-.304a.129.129 0 00.087-.087A6.016 6.016 0 015.635 2.31C6.315 1.464 7.132.846 8.086.457zm-.804 7.85a.848.848 0 00-1.473.842l1.694 2.965-1.688 2.848a.849.849 0 001.46.864l1.94-3.272a.849.849 0 00.007-.854l-1.94-3.393zm5.446 6.24a.849.849 0 000 1.695h4.848a.849.849 0 000-1.696h-4.848z"></path></svg>',
  agy: '<img class="pv-logo" src="/static/provider-icons/antigravity.svg" alt="">',
};
{
  const hero = $('#hHero');
  if (hero) hero.append(seg, hint);
}
const SEG_ICONS = {
  claude: '<svg class="pv-si" viewBox="0 0 16 16" aria-hidden="true"><g transform="translate(8 8)" stroke="var(--run)" stroke-width="1.65" stroke-linecap="round"><line x1="0" y1="-5.2" x2="0" y2="5.2"/><line x1="-5.2" y1="0" x2="5.2" y2="0"/><line x1="-3.7" y1="-3.7" x2="3.7" y2="3.7"/><line x1="3.7" y1="-3.7" x2="-3.7" y2="3.7"/></g></svg>',
  codex: '<svg class="pv-si" viewBox="0 0 24 24" fill="currentColor" fill-rule="evenodd" aria-hidden="true"><path clip-rule="evenodd" d="M8.086.457a6.105 6.105 0 013.046-.415c1.333.153 2.521.72 3.564 1.7a.117.117 0 00.107.029c1.408-.346 2.762-.224 4.061.366l.063.03.154.076c1.357.703 2.33 1.77 2.918 3.198.278.679.418 1.388.421 2.126a5.655 5.655 0 01-.18 1.631.167.167 0 00.04.155 5.982 5.982 0 011.578 2.891c.385 1.901-.01 3.615-1.183 5.14l-.182.22a6.063 6.063 0 01-2.934 1.851.162.162 0 00-.108.102c-.255.736-.511 1.364-.987 1.992-1.199 1.582-2.962 2.462-4.948 2.451-1.583-.008-2.986-.587-4.21-1.736a.145.145 0 00-.14-.032c-.518.167-1.04.191-1.604.185a5.924 5.924 0 01-2.595-.622 6.058 6.058 0 01-2.146-1.781c-.203-.269-.404-.522-.551-.821a7.74 7.74 0 01-.495-1.283 6.11 6.11 0 01-.017-3.064.166.166 0 00.008-.074.115.115 0 00-.037-.064 5.958 5.958 0 01-1.38-2.202 5.196 5.196 0 01-.333-1.589 6.915 6.915 0 01.188-2.132c.45-1.484 1.309-2.648 2.577-3.493.282-.188.55-.334.802-.438.286-.12.573-.22.861-.304a.129.129 0 00.087-.087A6.016 6.016 0 015.635 2.31C6.315 1.464 7.132.846 8.086.457zm-.804 7.85a.848.848 0 00-1.473.842l1.694 2.965-1.688 2.848a.849.849 0 001.46.864l1.94-3.272a.849.849 0 00.007-.854l-1.94-3.393zm5.446 6.24a.849.849 0 000 1.695h4.848a.849.849 0 000-1.696h-4.848z"/></svg>',
  agy: '<img class="pv-si" src="/static/provider-icons/antigravity.svg" alt="" aria-hidden="true">',
};
function renderSeg() {
  const p = S.cur ? curProv() : V.prov;
  body.dataset.homePv = p;
  if (seg._p !== p) {
    seg._p = p;
    heroIcon.innerHTML = HOME_ICONS[p];
    heroIcon.dataset.p = p;
    heroIcon.setAttribute('aria-label', LABEL[p]);
    seg.innerHTML = ['claude', 'codex', 'agy'].map(k => `<button type="button" role="radio" data-p="${k}" aria-checked="${k === p}" class="${k === p ? 'on' : ''}">${SEG_ICONS[k]}${LABEL[k]}</button>`).join('');
    seg.querySelectorAll('button').forEach(b => b.onclick = () => pickProv(b.dataset.p));
  }
  renderHint();
}
function renderHint() {
  const p = S.cur ? curProv() : V.prov;
  if (p === 'claude') { hint.hidden = true; hint.textContent = ''; return; }
  const st = V.status && V.status[p];
  const warn = !!st && !st.ok;
  hint.hidden = !warn;
  hint.classList.toggle('warn', warn);
  hint.textContent = warn ? (st.hint || LABEL[p] + ' 现在用不了') : '';
}
const isEmptySess = () => !!S.cur && !S.loading && !S.byId.size && !S.pendings.length;
async function pickProv(p) {
  if (!LABEL[p]) return;
  closePop();
  if (!S.cur) {
    V.prov = p;
    syncBody();
    if (p !== 'claude') { await provStatus(); renderHint(); provModels(p).then(renderPills); }
    return;
  }
  /* 刚新建、还没发过消息的空对话（显示的是首页）：换成另一家新建一个，原来的空对话结束掉（不留空对话），和换文件夹的做法一样 */
  if (!isEmptySess() || p === curProv() || (C.home.state && C.home.state.creating)) return;
  const old = S.cur, cwd = S.cwd, text = input.value, H = C.home.state;
  if (p !== 'claude') { const st = (await provStatus())[p]; if (st && !st.installed) { V.prov = p; renderHint(); toast(st.hint); return; } }
  H.creating = true; body.classList.add('h-creating');
  try {
    const d = p === 'claude' ? await api('new', {cwd}) : await api('prov-new', {cwd, prov: p, opts: V.homeOpts[p] || {}});
    store.set('draft.' + d.sid, text.trim() ? text : null);
    S.attsBy[d.sid] = S.atts;
    S.freshSid = d.sid;
    C.load(d);
    store.set('last', d.sid); store.set('draft.' + old, null); delete S.attsBy[old];
    api('stop', {sid: old}).catch(() => {});
    V.prov = p;
    C.refreshSide(true);
  } catch (e) { toast(e.message); }
  finally { H.creating = false; body.classList.remove('h-creating'); }
}
/* 首页发送：选的是 Codex / Antigravity 时按它新建（和 ux-h.js 的 homeSend 同一套草稿、附件处理） */
const origHomeSend = C.homeSend;
C.homeSend = async (now, direct) => {
  if (V.prov === 'claude' || !origHomeSend) return origHomeSend && origHomeSend(now, direct);
  const H = C.home.state;
  if (H.creating) return;
  const fromInput = direct == null, text = fromInput ? input.value : String(direct || '');
  if (fromInput && S.atts.some(a => !a.path)) { toast('图片还在上传，稍等一下再发'); return; }
  if (!text.trim() && !(fromInput && S.atts.length)) { input.focus(); return; }
  const cwd = C.home.cwd();
  if (!cwd) { toast('先选一个项目文件夹'); const fc = $('#hProj'); if (fc) fc.click(); return; }
  const st = (await provStatus())[V.prov];
  if (st && !st.ok) { renderHint(); toast(st.hint || LABEL[V.prov] + ' 现在用不了'); return; }
  H.creating = true; body.classList.add('h-creating');
  try {
    const d = await api('prov-new', {cwd, prov: V.prov, opts: V.homeOpts[V.prov] || {}});
    if (fromInput) store.set('draft.' + d.sid, input.value || null);
    store.set('draft.home', null);
    C.toggleProject(cwd, true);
    S.freshSid = d.sid;
    C.load(d); store.set('last', d.sid); C.refreshSide(true);
    H.opts = {}; H.cwd = '';
    if (fromInput) C.send(); else C.sendText(text);
  } catch (e) { toast(e.message); }
  finally { H.creating = false; body.classList.remove('h-creating'); }
};

/* ================= 2. 输入栏药丸：模型、思考强度、权限 ================= */
const mP = mk('button', 'h-pill pv-pill'), eP = mk('button', 'h-pill pv-pill'), oP = mk('button', 'h-pill pv-pill');
mP.id = 'pvModel'; eP.id = 'pvEffort'; oP.id = 'pvMode';
for (const [b, t] of [[mP, '选择模型'], [eP, '思考强度'], [oP, '权限模式']]) { b.type = 'button'; b.title = t; b.setAttribute('aria-haspopup', 'menu'); }
{
  const after = $('#modePill');
  if (after) after.after(mP, eP, oP); else bar.prepend(mP, eP, oP);
}
function curOpts() { return S.cur ? (S.opts || {}) : (V.homeOpts[curProv()] || {}); }
function modelList(md) { return (md && md.models) || []; }
function effortsFor(md, mid) {
  if (!md) return [];
  if (Array.isArray(md.efforts) && md.efforts.length) return md.efforts;  // ACP：思考强度是单独的一项
  const m = modelList(md).find(x => x.id === mid) || modelList(md).find(x => x.isDefault) || modelList(md)[0];
  const names = md.effortNames || {};
  return ((m && m.efforts) || []).map(e => ({id: e, label: names[e] || e}));
}
/* 正在用的：Codex 没选就是“默认”（按 config.toml）；Antigravity 的选项是会话自己的状态，没选就显示它当前的值 */
function live() { return (S.cur && S.runner && S.runner.pvcur) || {}; }
function shown(md) {
  const p = curProv(), o = curOpts(), lv = live();
  if (p === 'codex') return {model: o.model || '', effort: o.effort || '', mode: o.mode || 'default', runModel: lv.model || (md && md.cfgModel) || ''};
  return {model: o.model || lv.model || (md && md.defaultModel) || '', effort: o.effort || lv.effort || (md && md.defaultEffort) || '',
    mode: o.mode || lv.mode || (md && md.defaultMode) || 'default', runModel: ''};
}
function renderPills() {
  if (!isPv()) return;
  const p = curProv(), md = V.models[p], sh = shown(md), ml = modelList(md);
  const mid = sh.model || sh.runModel;
  const m = ml.find(x => x.id === mid);
  const mlabel = m ? m.label : (mid || (md && md.defaultLabel) || '默认模型');
  mP.innerHTML = `<span class="h-pl">${esc(mlabel)}</span>` + CHEV;
  mP.title = p === 'codex' && !sh.model ? '模型（按 Codex 设置）' : '选择模型';
  const effs = effortsFor(md, mid);
  const el = sh.effort ? ((effs.find(x => x.id === sh.effort) || {}).label || ((md && md.effortNames) || {})[sh.effort] || sh.effort) : '默认';
  eP.innerHTML = `<span class="h-pl">思考 ${esc(el)}</span>` + CHEV;
  eP.hidden = !effs.length && !sh.effort;
  const modes = (md && md.modes) || [];
  const mo = modes.find(x => x.id === sh.mode) || {label: p === 'codex' ? '按 Codex 设置' : '默认权限'};
  oP.innerHTML = SHIELD + `<span class="h-pl">${esc(mo.label)}</span>` + CHEV;
  oP.classList.toggle('warn', sh.mode === 'full');
  if (V.pop) V.pop.render();
}
function closePop() { if (V.pop) { V.pop.el.remove(); V.pop.anchor.classList.remove('open'); V.pop = null; } }
function openPop(anchor, fill, cls) {
  if (V.pop && V.pop.anchor === anchor) { closePop(); return; }
  closePop(); C.closeMenu();
  const el = mk('div', 'h-pop pv-pop ' + (cls || ''));
  el.setAttribute('role', 'menu');
  document.body.appendChild(el);
  const render = () => { el.innerHTML = ''; fill(el); place(); };
  const place = () => {
    const r = anchor.getBoundingClientRect(), m = 8;
    el.style.maxHeight = Math.max(160, Math.min(440, r.top - 60)) + 'px';
    const w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = Math.round(Math.max(m, Math.min(r.left, innerWidth - w - m))) + 'px';
    el.style.top = Math.round(Math.max(m, r.top - 6 - h)) + 'px';
  };
  V.pop = {el, anchor, render};
  anchor.classList.add('open');
  render();
  const f = el.querySelector('[aria-checked=true]') || el.querySelector('.h-mi');
  if (f) f.focus({preventScroll: true});
  el.addEventListener('keydown', e => {
    const items = [...el.querySelectorAll('.h-mi')], i = items.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); closePop(); anchor.focus(); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); const n = items[(i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]; if (n) n.focus(); }
  });
}
function mi(label, desc, check, fn, cls) {
  const b = mk('button', 'h-mi' + (cls ? ' ' + cls : ''));
  b.type = 'button'; b.setAttribute('role', 'menuitemradio'); b.setAttribute('aria-checked', check ? 'true' : 'false');
  b.innerHTML = `<span class="h-mt"><span class="h-mn">${esc(label)}</span>${desc ? `<small>${esc(desc)}</small>` : ''}</span><span class="h-ck">${check ? CHECK : ''}</span>`;
  b.onclick = fn;
  return b;
}
const note = t => mk('div', 'h-note', esc(t));
document.addEventListener('mousedown', e => { if (V.pop && !V.pop.el.contains(e.target) && !V.pop.anchor.contains(e.target)) closePop(); }, true);
addEventListener('resize', closePop);
async function setOpt(o) {
  const p = curProv();
  if (!S.cur) { const h = V.homeOpts[p] = V.homeOpts[p] || {}; Object.assign(h, o); for (const k in o) if (!o[k]) delete h[k]; renderPills(); return; }
  const sid = S.cur, busy = !!(S.runner && S.runner.busy);
  try {
    const d = await api('control', Object.assign({sid}, o));
    if (S.cur === sid) S.opts = d.opts || {};
    renderPills();
    const md = V.models[p];
    if ('model' in o) { const m = modelList(md).find(x => x.id === o.model); toast((busy ? '下一轮起换成 ' : '已换成 ') + (m ? m.label : (o.model || '默认模型'))); }
    else if ('mode' in o) { const m = ((md && md.modes) || []).find(x => x.id === (o.mode || 'default')); toast((busy ? '下一轮起用“' : '已换成“') + ((m && m.label) || '默认') + '”'); }
    else if ('effort' in o) toast(busy ? '下一轮起生效' : '已切换思考强度');
  } catch (e) { toast(e.message); }
}
mP.onclick = async () => {
  const p = curProv();
  openPop(mP, el => {
    const md = V.models[p], sh = shown(md);
    if (!md) { el.appendChild(note('正在读取模型列表…')); return; }
    if (md.error) el.appendChild(note(md.error));
    if (p === 'codex') {
      const dl = md.cfgModel || md.defaultLabel || '';
      el.appendChild(mi('默认' + (dl ? '（' + dl + '）' : ''), '按 Codex 自己的设置', !sh.model, () => { closePop(); setOpt({model: ''}); }));
    }
    for (const m of modelList(md)) el.appendChild(mi(m.label, m.desc, m.id === sh.model, () => { closePop(); if (m.id !== sh.model) setOpt({model: m.id}); }, 'h-model-it'));
  }, 'pv-mpop');
  await provModels(p);
  if (V.pop && V.pop.anchor === mP) V.pop.render();
  renderPills();
};
eP.onclick = () => {
  const p = curProv();
  openPop(eP, el => {
    const md = V.models[p], sh = shown(md), effs = effortsFor(md, sh.model || sh.runModel);
    if (!effs.length) { el.appendChild(note('这个模型没有可选的思考强度')); return; }
    if (p === 'codex') {
      const de = md.cfgEffort || '';
      el.appendChild(mi('默认' + (de ? '（' + ((md.effortNames || {})[de] || de) + '）' : ''), '按 Codex 自己的设置', !sh.effort, () => { closePop(); setOpt({effort: ''}); }));
    }
    for (const e of effs) el.appendChild(mi(e.label, '', e.id === sh.effort, () => { closePop(); if (e.id !== sh.effort) setOpt({effort: e.id}); }));
  }, 'pv-epop');
};
oP.onclick = async () => {
  const p = curProv();
  openPop(oP, el => {
    const md = V.models[p], sh = shown(md), modes = (md && md.modes) || [];
    if (!modes.length) { el.appendChild(note('正在读取权限档位…')); return; }
    for (const m of modes) el.appendChild(mi(m.label, m.desc, m.id === sh.mode, () => { closePop(); if (m.id !== sh.mode) setOpt({mode: m.id}); }, m.id === 'full' ? 'warn' : ''));
  }, 'h-mode pv-opop');
  await provModels(p);
  if (V.pop && V.pop.anchor === oP) V.pop.render();
};

/* ================= 顶栏：提供方小标识 ================= */
const badge = mk('span', 'pv-badge');
badge.id = 'pvBadge'; badge.hidden = true;
{ const t = $('#title'); if (t) t.after(badge); }
function renderBadge() {
  const p = S.cur ? curProv() : '';
  badge.hidden = !p || p === 'claude';
  if (!badge.hidden) { badge.textContent = LABEL[p]; badge.dataset.p = p; }
}

/* ================= 3. 左栏标识、删除确认 ================= */
function decoSide() {
  for (const row of document.querySelectorAll('#projects .srow[data-sid]')) {
    const s = C.findSession(row.dataset.sid), p = s && s.prov;
    const ex = row.querySelector('.pv-tag');
    if (!p) { if (ex) ex.remove(); continue; }
    if (ex && ex.dataset.p === p) continue;
    if (ex) ex.remove();
    const t = mk('span', 'pv-tag', esc(LABEL[p] || p)); t.dataset.p = p; t.title = '这是 ' + (LABEL[p] || p) + ' 对话';
    const st = row.querySelector('.stitle');  // 放在标题后面、和标题并列（放进标题里会被省略号截掉）
    if (st) st.after(t);
  }
}
chain('onSide', () => { decoSide(); if (S.cur && !V.cur) { const p = provOf(S.cur); if (p) { V.cur = p; syncBody(); } } });
if (C.undoDelete) {
  const orig = C.undoDelete;
  C.undoDelete = (list, ...rest) => {
    const pv = (list || []).filter(s => s && (s.prov || provOf(s.sid)));
    if (pv.length) {
      const names = [...new Set(pv.map(s => LABEL[s.prov || provOf(s.sid)]))].join('、');
      const msg = (pv.length === 1 ? `删除对话「${pv[0].title || '新对话'}」？` : `删除这 ${list.length} 个对话？`) +
        `\n\n只删除工作台里的记录（移到废纸篓）。${names} 自己保存的对话不会删除，之后还能在 ${names} 里找到。`;
      if (!confirm(msg)) return;
    }
    return orig(list, ...rest);
  };
}

/* ================= 4. 授权卡 ================= */
function decoPerm() {
  const card = $('#card'), key = S.modalKey || '';
  if (!key.startsWith('p:')) return;
  const p = S.perms.get(key.slice(2));
  if (!p || !p.prov || card.dataset.pv === p.rid) return;
  card.dataset.pv = p.rid;
  const v = p.pvView || {}, lab = p.provLabel || LABEL[p.prov] || '';
  const h = card.querySelector(':scope > h3');
  if (h) h.innerHTML = `<span class="pv-tag" data-p="${esc(p.prov)}">${esc(lab)}</span>${esc(v.title || h.textContent)}`;
  if (Array.isArray(v.files) && v.files.length > 1) {  // 多个文件：逐个列出（app.js 只画第一个）
    const box = card.querySelector(':scope > .box'), d = card.querySelector(':scope > .desc');
    const list = mk('div', 'pv-files');
    for (const f of v.files) {
      const it = {k: 'tool', id: 'pvf' + Math.random(), name: f.kind || 'Edit', path: f.path, done: true, error: false, out: '', in: f.text || '',
        diff: f.old != null && f.new != null ? {old: f.old, new: f.new} : null};
      list.appendChild(C.renderItem(it, true));
    }
    if (box) box.replaceWith(list); else if (d) d.after(list);
    if (d) d.remove();
  }
  if (v.reason) { const r = mk('div', 'desc pv-reason', esc(v.reason)); (h || card.firstChild).after(r); }
  if (v.text && p.tool !== 'Bash' && p.tool !== 'Edit') {
    const pre = card.querySelector(':scope > pre.blk');
    if (pre) pre.textContent = v.text;
  }
  const al = card.querySelector('[data-d=always]');
  if (al && p.pvAlways) al.textContent = p.pvAlways;
  fixDenyPh(card, lab);
}
function fixDenyPh(card, lab) {
  const inp = card.querySelector('.k-dn input');
  if (inp && lab && !inp.dataset.pv) { inp.dataset.pv = 1; inp.placeholder = `告诉 ${lab} 应该怎么做`; }
}
new MutationObserver(() => { try { const k = S.modalKey || ''; if (!k.startsWith('p:')) { delete $('#card').dataset.pv; return; } decoPerm(); const p = S.perms.get(k.slice(2)); if (p && p.prov) fixDenyPh($('#card'), p.provLabel); } catch (e) { console.error(e); } })
  .observe($('#card'), {childList: true, subtree: true});

/* ================= 5. 用户消息里的图片 ================= */
function decoImgs() {
  if (!S.cur || !isPv()) return;
  for (const el of document.querySelectorAll('#feed .user[data-id]')) {
    const it = S.byId.get(el.dataset.id);
    if (!it || !Array.isArray(it.pimgs) || !it.pimgs.length || el.querySelector('.thumbs')) continue;
    const chip = el.querySelector('.chip');
    const box = mk('div', 'thumbs', it.pimgs.map(p => `<img loading="lazy" src="${esc(C.fileUrl(p))}" alt="">`).join(''));
    if (chip && chip.parentNode && chip.parentNode !== el) chip.parentNode.replaceWith(box); else el.appendChild(box);
  }
}

/* ================= Claude 专属功能：在这两家的对话里藏起来 ================= */
/* “+”菜单的“添加文件夹…”看 C.addDir 有没有；“技能”在浮层出来后去掉 */
if (C.addDir) {
  const addDir = C.addDir;
  try { Object.defineProperty(C, 'addDir', {configurable: true, get: () => (isPv() ? undefined : addDir), set: v => { /* 保持原函数 */ }}); } catch {}
}
new MutationObserver(ms => {
  if (!isPv()) return;
  for (const m of ms) for (const n of m.addedNodes) {
    if (n.nodeType !== 1 || !n.classList.contains('h-plusm')) continue;
    for (const b of n.querySelectorAll('.h-mi')) { const t = (b.querySelector('.h-mn') || {}).textContent; if (t === '技能') b.remove(); }
  }
}).observe(document.body, {childList: true});
document.addEventListener('keydown', e => {  // ⌘; 旁问
  if (isPv() && (e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && (e.key === ';' || e.code === 'Semicolon')) { e.preventDefault(); e.stopImmediatePropagation(); toast('这个对话不支持旁问'); }
}, true);
function leaveClaudeTabs() {  // 右栏开着“旁问”时换到“预览”（11-23：“任务”页两家也能用了，不再跳走）
  const on = document.querySelector('#rpane .rtabs [data-tab].on');
  if (isPv() && on && on.dataset.tab === 'btw' && C.pane && C.pane.open) { try { C.pane.open('preview'); } catch {} }
}

/* ================= 复核修复 7：实时预览按增量接 =================
   后台平时只推新增的正文（liveAdd，从第 liveAt 个字接上，到 liveEnd 为止），每 2 秒推一次整段（只含末尾一屏，起点 liveFrom）。
   这里把它接回整段放进 state.live，app.js 照常渲染。没接上（中途打开的窗口）就先用上次的，等下一次整段。 */
V.lv = {};
function liveIn(sid, st) {
  if (!sid || !st || !st.prov) return;
  if (!('liveAdd' in st)) { V.lv[sid] = {from: st.liveFrom || 0, end: st.liveEnd || 0, text: st.live || ''}; return; }
  const b = V.lv[sid];
  if (b && b.from === st.liveFrom && b.end >= st.liveAt && b.end <= st.liveEnd) {
    const skip = b.end - st.liveAt;  // 位置按字（码点）算，和后台一致
    b.text += skip ? Array.from(st.liveAdd).slice(skip).join('') : st.liveAdd;
    b.end = st.liveEnd;
  }
  st.live = b && b.from === st.liveFrom ? b.text : '';
}

/* ================= 复核修复 6：出错结束时没发出去的排队消息，放回输入框 ================= */
function unsentIn(ev) {
  const txt = (ev.texts || []).join('\n\n');
  if (!txt) return;
  if (ev.sid === S.cur && !input.value.trim()) {
    input.value = txt; input.dispatchEvent(new Event('input', {bubbles: true}));
    toast('排队的消息没发出去，已放回输入框');
  } else if (ev.sid !== S.cur && !store.get('draft.' + ev.sid, null)) store.set('draft.' + ev.sid, txt);
}

/* ================= 复核修复 8：选了 Codex / Antigravity 时不预热 Claude =================
   首页（ux-h.js）和打开对话（app2.js）都会预热一个 Claude；它们用的是 app.js 里的 api()，这里在 fetch 这一层拦下这一个接口 */
{
  const of = window.fetch.bind(window);
  window.fetch = (u, o) => {
    if (u === '/api/prewarm' && (S.cur ? isPv() : V.prov !== 'claude')) {
      V.pwSkip = (V.pwSkip || 0) + 1;
      return Promise.resolve(new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}}));
    }
    return of(u, o);
  };
}

/* ================= 复核修复 8：“重启后台服务”遇到这两家正在回复，说清楚要等 =================
   设置页（ux-j.js）的按钮：先问后台这两家忙不忙；不忙交回原来的处理，忙就换成说明要等的确认框和提示 */
document.addEventListener('click', async e => {
  const b = e.target && e.target.closest && e.target.closest('.sj-btn[data-a=restart]');
  if (!b || b.disabled) return;
  if (b.dataset.pvPass) { delete b.dataset.pvPass; return; }
  e.preventDefault(); e.stopImmediatePropagation();
  let w = '';
  try { w = (await api('prov-busy')).wait || ''; } catch {}
  if (!w) { b.dataset.pvPass = '1'; b.click(); return; }
  if (!confirm(`重启后台服务？\n${w}：它们的回复在后台服务里，重启会断开，所以会等这一轮回复完再重启。\nClaude 的对话不受影响。`)) return;
  try { const d = await api('restart-service', {pvAware: true}); toast(d.wait || w); }
  catch (err) { toast('重启失败：' + err.message); }
}, true);

/* ================= 接入 ================= */
chain('onPreLoad', d => { V.cur = (d && d.prov) || ''; V.curD = d && d.prov ? d : null; if (d && d.prov) liveIn(d.sid, d.runner); });
chain('onLoad', d => {
  V.cur = (d && d.prov) || ''; V.curD = d && d.prov ? d : null;
  if (V.cur) provModels(V.cur).then(renderPills);
  syncBody(); decoImgs(); leaveClaudeTabs();
});
chain('onEvent', ev => {
  if (!ev) return;
  if (ev.type === 'runner' && ev.state && ev.state.prov) liveIn(ev.sid, ev.state);  // 在 app.js 渲染之前（钩子先于 switch）
  if (ev.type === 'pv-unsent') unsentIn(ev);
  if (ev.type === 'items' && ev.sid === S.cur && isPv()) queueMicrotask(decoImgs);
  else if (ev.type === 'runner' && ev.sid === S.cur && isPv()) renderPills();
});
chain('onControls', () => { if (isPv()) renderPills(); });
/* 首页进出、清空主区 */
if (C.clearMain) { const cm = C.clearMain; C.clearMain = (...a) => { const r = cm(...a); V.cur = ''; V.curD = null; syncBody(); return r; }; }
new MutationObserver(() => { const h = body.classList.contains('h-home') && !S.cur; if (h !== V.wasHome) { V.wasHome = h; if (h) V.prov = 'claude'; syncBody(); } })
  .observe(body, {attributes: true, attributeFilter: ['class']});
syncBody();
provStatus().then(renderHint);
C.pv = {state: V, provOf, curProv, pick: pickProv, models: provModels, status: provStatus, sync: syncBody};
})();
