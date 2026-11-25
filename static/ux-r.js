(() => {
'use strict';
/* 第十六轮 R：远程访问（Tailscale）。说明见 README“第十六轮 R：远程控制”，后台在 remote.py。
   - 在 <head> 里先于 app.js 加载：远程页面要先接管 fetch、Notification、EventSource，ux-j.js 才会包到这里的 Notification。
   - 本机（127.0.0.1 / localhost）打开时：只在设置里加“远程访问”页，其他一概不动。
   - 远程（ts.net）打开时：html 加 cwb-remote；会在电脑上打开程序、弹选择框的接口改成右栏预览、下载、手机自己打开或填路径；
     系统通知改成页面内提示；宽度 ≤ 640px 时左栏变抽屉、右栏全屏、输入框贴底（样式在 ux-r.css，全部限定在 html.cwb-remote 下）。 */
const REMOTE = !/^(127\.0\.0\.1|localhost)$/i.test(location.hostname);
const R = window.CWB_R = {remote: REMOTE};
const de = document.documentElement;
const realFetch = window.fetch.bind(window);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const C = () => window.CWB;
const toast = t => { const c = C(); if (c && c.toast) c.toast(t); };

async function realApi(name, body) {
  const r = await realFetch('/api/' + name, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || ('操作没有成功（' + r.status + '）')), {status: r.status});
  return j;
}

/* ================= 远程页面：加载最早的一步 ================= */
function early() {
  de.classList.add('cwb-remote');
  const vp = document.querySelector('meta[name=viewport]');
  if (vp) vp.content = 'width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content';
  const add = (tag, attrs) => { const e = document.createElement(tag); for (const k in attrs) e.setAttribute(k, attrs[k]); document.head.appendChild(e); };
  add('link', {rel: 'manifest', href: '/manifest.webmanifest', crossorigin: 'use-credentials'});
  add('link', {rel: 'apple-touch-icon', href: '/static/apple-touch-icon.png'});
  add('meta', {name: 'theme-color', content: '#1a1a19', media: '(prefers-color-scheme: dark)'});
  add('meta', {name: 'theme-color', content: '#ffffff', media: '(prefers-color-scheme: light)'});
  add('meta', {name: 'apple-mobile-web-app-capable', content: 'yes'});
  add('meta', {name: 'mobile-web-app-capable', content: 'yes'});
  add('meta', {name: 'apple-mobile-web-app-title', content: '工作台'});
  try {  // 没配对时页面用 ?_r=1 重试过一次（SameSite=Strict），进来后去掉
    const u = new URL(location.href);
    if (u.searchParams.has('_r')) { u.searchParams.delete('_r'); history.replaceState(history.state, '', u.pathname + u.search + u.hash); }
  } catch {}
  wrapFetch();
  shimNotification();
  wrapEventSource();
}

/* ---------- 接口：电脑上的动作换成远程能做的 ---------- */
const MAP = {
  'reveal': b => preview(b.path, b.cwd),
  'open-editor': b => preview(b.path, b.cwd),
  'open-with': b => preview(b.cwd),
  'open-file': b => openFile(b.path, b.cwd),
  'open-url': b => { if (/^https?:\/\//i.test(b.url || '')) window.open(b.url, '_blank', 'noopener'); return {ok: true}; },
  'pick-files': () => pickFiles(),
  'add-folder': () => askPath('添加文件夹：填电脑上的完整路径', '~/').then(p => (p ? realApi('remote-add-folder', {path: p}) : {cwd: null})),
  'skill-pick': b => askPath(b.kind === 'zip' ? '技能 zip 文件在电脑上的完整路径' : '技能文件夹在电脑上的完整路径', '~/').then(p => ({path: p || ''})),
  'add-dir': b => (b.path ? null : askPath('要添加的文件夹在电脑上的完整路径', '~/').then(p => (p ? realApi('add-dir', Object.assign({}, b, {path: p})) : {ok: false, cancel: true}))),
  'restart-service': () => ({__status: 403, error: '远程页面不能重启后台服务，需要时请在电脑上操作'}),
  'notify': () => ({}),
  'open-apps': () => ({apps: []}),
};
function wrapFetch() {
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const m = /^\/api\/([\w-]+)$/.exec(url);
    if (m && MAP[m[1]]) {
      let b = {}; try { b = JSON.parse((init && init.body) || '{}'); } catch {}
      let r; try { r = MAP[m[1]](b); } catch (e) { r = {__status: 400, error: e.message}; }  // 同步调用：window.open、选文件要在点击的同一个调用栈里
      if (r !== null) {
        return Promise.resolve(r).then(v => {
          if (v === null) return realFetch(input, init);
          const st = (v && v.__status) || 200; if (v) delete v.__status;
          return new Response(JSON.stringify(v || {}), {status: st, headers: {'Content-Type': 'application/json'}});
        }, e => new Response(JSON.stringify({error: (e && e.message) || '操作没有成功'}), {status: (e && e.status) || 400, headers: {'Content-Type': 'application/json'}}));
      }
    }
    return realFetch(input, init).then(res => { if (m && (res.status === 401 || res.status === 403)) checkMe(); return res; });
  };
}
function fullPath(path, cwd) {
  const c = C(), base = cwd || (c && c.S && c.S.cwd) || '';
  return path && base && !/^[/~]/.test(path) ? base.replace(/\/+$/, '') + '/' + path : path;
}
async function preview(path, cwd) {
  const c = C();
  if (!path || !c || !c.preview) return {ok: false};
  closeDrawer();
  await c.preview(fullPath(path, cwd));
  return {ok: true};
}
async function openFile(path, cwd) {
  let d;
  try { d = await realApi('preview', {path, cwd: cwd || (C() && C().S.cwd) || ''}); } catch { return {ok: false}; }
  if (d.kind === 'dir') return preview(d.path);
  download(d.path, d.name);
  return {ok: true};
}
function download(path, name) {
  const a = document.createElement('a');
  a.href = '/r/dl?p=' + encodeURIComponent(path); a.download = name || ''; a.hidden = true;
  document.body.appendChild(a); a.click(); a.remove();
  toast('正在下载：' + (name || path.split('/').pop()));
}
function pickFiles() {
  return new Promise(ok => {
    const inp = document.createElement('input');
    inp.type = 'file'; inp.multiple = true; inp.hidden = true;
    let done = false;
    const fin = v => { if (!done) { done = true; inp.remove(); ok(v); } };
    inp.onchange = async () => {
      const paths = [];
      for (const f of inp.files) {
        try {
          if (f.size > 40 * 1024 * 1024) throw new Error(f.name + ' 超过 40MB');
          const data = await new Promise((y, n) => { const r = new FileReader(); r.onload = () => y(String(r.result).split(',')[1] || ''); r.onerror = n; r.readAsDataURL(f); });
          const d = await realApi('upload', {name: f.name, type: f.type, data});
          if (d.path) paths.push(d.path);
        } catch (e) { toast(e.message || '上传失败'); }
      }
      fin({paths});
    };
    inp.addEventListener('cancel', () => fin({paths: []}));
    document.body.appendChild(inp); inp.click();
  });
}
async function askPath(title, val) {
  const c = C();
  if (!c || !c.askText) return '';
  const v = await c.askText(title, val || '');
  return (v || '').trim().replace(/^~\/?$/, '');
}

/* ---------- 授权失效、远程被关时整页提示 ---------- */
let meAt = 0, meP = null;
function checkMe() {
  if (meP && Date.now() - meAt < 3000) return meP;
  meAt = Date.now();
  meP = realFetch('/api/remote-me', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})
    .then(async r => {
      if (r.status === 401) { lockout('这台设备的授权已失效', '可能在电脑上撤销了授权。请在电脑上打开“设置 → 远程访问”重新配对。'); return false; }
      if (r.status === 403) { const j = await r.json().catch(() => ({})); lockout('远程访问不可用', j.error || '电脑上关掉了远程访问。'); return false; }
      return true;
    }, () => true);  // 连不上（服务正在重启等）：不算失效，稍后再试
  return meP;
}
function lockout(title, msg) {
  if (!document.body || document.getElementById('rrLock')) return;
  const el = document.createElement('div');
  el.id = 'rrLock'; el.setAttribute('role', 'alertdialog');
  el.innerHTML = `<div class="rr-lock-in"><b>${esc(title)}</b><p>${esc(msg)}</p><button type="button" class="rr-btn rr-pri">重新载入</button></div>`;
  el.querySelector('button').onclick = () => location.reload();
  document.body.appendChild(el);
  const app = document.getElementById('app'); if (app) app.inert = true;
}

/* ---------- 系统通知 → 页面内提示 ---------- */
function shimNotification() {
  function RN(title, opt) { this.title = title; this.body = (opt && opt.body) || ''; this.onclick = null; this._cl = []; note(this); }
  RN.prototype.close = function () { if (this._el) this._el.remove(); };
  RN.prototype.addEventListener = function (t, f) { if (t === 'click') this._cl.push(f); };
  Object.defineProperty(RN, 'permission', {get: () => 'granted'});
  RN.requestPermission = () => Promise.resolve('granted');
  try { window.Notification = RN; } catch {}
}
function note(n) {
  if (!document.body) return;
  let box = document.getElementById('rrNotes');
  if (!box) { box = document.createElement('div'); box.id = 'rrNotes'; document.body.appendChild(box); }
  const el = document.createElement('div');
  el.className = 'rr-note'; el.setAttribute('role', 'status');
  el.innerHTML = `<div class="rr-note-t"><b>${esc(n.title || 'Claude 工作台')}</b><span>${esc(n.body)}</span></div><button type="button" class="rr-note-x" aria-label="关闭">×</button>`;
  el.onclick = e => {
    el.remove();
    if (e.target.closest('.rr-note-x')) return;
    try { if (n.onclick) n.onclick({}); n._cl.forEach(f => f({})); } catch {}
  };
  n._el = el; box.prepend(el);
  while (box.children.length > 3) box.lastChild.remove();
  const arm = () => setTimeout(() => el.remove(), 8000);
  if (document.hidden) document.addEventListener('visibilitychange', function f() { if (!document.hidden) { document.removeEventListener('visibilitychange', f); arm(); } });
  else arm();
}

/* ---------- 事件流：经 Tailscale 转发时，服务重启期间拿到 502，浏览器不会自己重连，这里补上 ---------- */
function wrapEventSource() {
  const RealES = window.EventSource;
  if (!RealES) return;
  function ES(url, cfg) {
    const self = this;
    let es = null, delay = 1000, closed = false, timer = 0;
    self.onmessage = self.onopen = self.onerror = null;
    const open = () => {
      es = new RealES(url, cfg);
      es.onmessage = e => self.onmessage && self.onmessage(e);
      es.onopen = e => { delay = 1000; if (self.onopen) self.onopen(e); };
      es.onerror = e => {
        if (self.onerror) self.onerror(e);
        if (es.readyState !== 2 || closed) return;
        clearTimeout(timer);
        timer = setTimeout(() => checkMe().then(ok => { if (ok && !closed) open(); }), delay);
        delay = Math.min(delay * 2, 15000);
      };
    };
    Object.defineProperty(self, 'readyState', {get: () => (es ? es.readyState : 0)});
    Object.defineProperty(self, 'url', {get: () => (es ? es.url : url)});
    self.close = () => { closed = true; clearTimeout(timer); if (es) es.close(); };
    open();
  }
  ES.CONNECTING = 0; ES.OPEN = 1; ES.CLOSED = 2;
  window.EventSource = ES;
}

/* ================= 页面脚本都加载完以后 ================= */
function late() {
  const c = C();
  if (!c) return;
  try { settings(c); } catch (e) { console.error(e); }
  if (!REMOTE) return;
  try { mobile(c); } catch (e) { console.error(e); }
  try { relabel(); } catch (e) { console.error(e); }
}

/* ---------- 远程页面：菜单和右栏按钮里的“在电脑上打开”改名或藏起来 ---------- */
const LABEL = {'在访达中显示': '在右栏预览', '在访达中打开': '在右栏预览', '用编辑器打开': '在右栏预览', '用默认程序打开': '下载到这台设备'};
function relabel() {
  const fix = root => {
    root.querySelectorAll('.menu').forEach(m => {
      const seen = new Set();
      m.querySelectorAll('.mi > span:first-child').forEach(s => {
        const t = LABEL[s.textContent] || s.textContent;
        if (t !== s.textContent) s.textContent = t;
        const b = s.parentElement;
        if (LABEL[t] === undefined && Object.values(LABEL).includes(t)) { if (seen.has(t)) b.hidden = true; seen.add(t); }
      });
    });
    root.querySelectorAll('.racts [data-a="open"]').forEach(b => { if (b.textContent !== '下载') b.textContent = '下载'; });
  };
  new MutationObserver(ms => {
    for (const m of ms) for (const n of m.addedNodes) if (n.nodeType === 1) fix(n.matches && (n.matches('.menu') || n.matches('.racts')) ? n.parentNode || n : n);
  }).observe(document.body, {childList: true, subtree: true});
  fix(document);
}

/* ---------- 远程页面：手机布局（宽度 ≤ 640px）：左栏抽屉、右栏全屏、跟着软键盘调整高度 ---------- */
let closeDrawer = () => {};
function mobile(c) {
  const body = document.body, mq = matchMedia('(max-width: 640px)');
  const on = () => mq.matches;
  const apply = () => { body.classList.toggle('rr-m', on()); if (!on()) body.classList.remove('rr-drawer'); };
  if (mq.addEventListener) mq.addEventListener('change', apply); else mq.addListener(apply);
  apply();
  const scrim = document.createElement('div');
  scrim.id = 'rrScrim';
  (document.getElementById('app') || body).appendChild(scrim);
  closeDrawer = () => body.classList.remove('rr-drawer');
  scrim.addEventListener('click', closeDrawer);
  document.addEventListener('click', e => {  // 捕获阶段：手机上侧栏按钮开关抽屉，不走 ux-c 的收起/展开
    if (!on() || !e.target.closest('#uxSideBtn')) return;
    e.preventDefault(); e.stopImmediatePropagation();
    body.classList.toggle('rr-drawer');
  }, true);
  const side = document.getElementById('side');
  if (side) side.addEventListener('click', e => {
    if (!on()) return;
    if ((e.target.closest('.srow') && !e.target.closest('.smore')) || e.target.closest('.pbtn[data-a="new"]')) setTimeout(closeDrawer, 0);
  });
  const prev = c.onLoad;
  c.onLoad = (...a) => { try { if (prev) prev(...a); } finally { if (on()) closeDrawer(); } };
  addEventListener('keydown', e => { if (e.key === 'Escape' && body.classList.contains('rr-drawer')) closeDrawer(); });
  const os = c.openSettings;  // 设置在手机上是横排分区：打开后把当前分区滚到看得见的地方
  if (os) c.openSettings = (...a) => { closeDrawer(); const r = os(...a); if (on()) { const b = document.querySelector('#sjSet .sj-nav .on'); if (b) b.scrollIntoView({inline: 'center', block: 'nearest'}); } return r; };
  const vv = window.visualViewport;
  if (vv) {
    const fit = () => {
      de.style.setProperty('--rr-vh', Math.round(vv.height) + 'px');
      de.style.setProperty('--rr-vt', Math.round(vv.offsetTop) + 'px');
      body.classList.toggle('rr-kb', innerHeight - vv.height > 120);
    };
    vv.addEventListener('resize', fit); vv.addEventListener('scroll', fit); fit();
  }
}

/* ================= 设置 →“远程访问” ================= */
function settings(c) {
  const sj = c.sj;
  if (!sj) return;
  if (!sj.SECS.some(([k]) => k === 'remote')) {
    const i = sj.SECS.findIndex(([k]) => k === 'about');
    sj.SECS.splice(i < 0 ? sj.SECS.length : i, 0, ['remote', '远程访问']);
  }
  const nav = document.querySelector('#sjSet .sj-nav');
  if (nav && !nav.querySelector('[data-s="remote"]')) {
    const nb = document.createElement('button'); nb.type = 'button'; nb.dataset.s = 'remote'; nb.textContent = '远程访问';
    const ab = nav.querySelector('[data-s="about"]'); if (ab) ab.before(nb); else nav.appendChild(nb);
  }
  const st = {pair: null, timer: 0, busy: false};
  const live = g => g === sj.gen() && sj.body() && sj.body().dataset.sec === 'remote';
  const box = () => sj.body().querySelector('.rr-box');
  const p2 = n => String(n).padStart(2, '0');
  const when = t => { if (!t) return '—'; const d = new Date(t * 1000); return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`; };
  const ago = t => { if (!t) return '—'; const s = Date.now() / 1000 - t; return s < 90 ? '刚刚' : s < 3600 ? Math.round(s / 60) + ' 分钟前' : s < 86400 ? Math.round(s / 3600) + ' 小时前' : when(t); };
  async function call(n, b) { try { return await c.api(n, b || {}); } catch (e) { return {error: (e && e.message) || '操作没有成功', data: e && e.data}; } }
  const steps = list => '<ol class="rr-steps">' + list.map(s => `<li>${s}</li>`).join('') + '</ol>';
  const STEP = {
    install: steps(['在电脑上从 <b>tailscale.com/download</b> 下载 macOS 版并安装。', '打开 Tailscale，用你的账号登录。',
      '手机也装 Tailscale（App Store 或应用商店），登录<b>同一个账号</b>。', '回到这里，打开“通过 Tailscale 开放”。']),
    run: steps(['在“应用程序”里打开 Tailscale，菜单栏会出现它的图标。', '回到这里，点“刷新”。']),
    login: steps(['点菜单栏的 Tailscale 图标，选“Log in”，在浏览器里登录。', '回到这里，点“刷新”。']),
    https: steps(['打开 Tailscale 管理后台的 DNS 页面（<b>login.tailscale.com/admin/dns</b>）。', '在“HTTPS Certificates”里点“Enable HTTPS”。不需要开 Funnel。', '回到这里，再打开一次开关。']),
  };

  function render() {
    const b = sj.body(), g = sj.gen();
    clearInterval(st.timer);
    b.innerHTML = sj.head('远程访问', REMOTE ? '你正在通过 Tailscale 远程使用这台电脑上的工作台' : '用手机等设备，通过 Tailscale 控制这台电脑上的工作台') +
      '<div class="rr-box"><div class="sj-empty">读取中…</div></div>';
    if (REMOTE) drawRemote(g); else draw(g);
  }

  async function drawRemote(g) {
    const d = await call('remote-me');
    if (!live(g)) return;
    box().innerHTML = '<div class="rr-gh">这台设备</div><div class="sj-grp">' +
      sj.row('名称', `<span class="rr-v">${esc(d.name || '—')}</span>`) + sj.row('配对时间', `<span class="rr-v">${when(d.created)}</span>`) +
      (d.login ? sj.row('Tailscale 账号', `<span class="rr-v">${esc(d.login)}</span>`) : '') +
      sj.row('取消这台设备的授权', '<button type="button" class="sj-btn rr-danger" data-a="forget">取消授权</button>', '取消后这台设备要重新配对才能再用') + '</div>' +
      '<p class="rr-note-p">开关远程访问、配对新设备、管理已授权设备，只能在电脑上操作。远程页面上，在电脑上打开文件和程序的功能改成在右栏预览或下载到这台设备。</p>';
    sj.bind(box(), {}, {forget: async (e, btn) => {
      if (!confirm('取消这台设备的授权？之后要在电脑上重新配对。')) return;
      btn.disabled = true;
      const r = await call('remote-forget');
      if (r.error) { btn.disabled = false; toast(r.error); return; }
      location.reload();
    }});
  }

  async function draw(g, data) {
    const d = data || await call('remote-status');
    if (!live(g)) return;
    const el = box();
    if (d.error) { el.innerHTML = sj.errBox('读取失败：' + d.error, true); sj.bind(el, {}, {retry: () => draw(g)}); return; }
    const ts = d.ts || {}, rm = d.remote || {}, ready = ts.installed && ts.running && ts.loggedIn && ts.dns;
    let h = '<div class="rr-gh">Tailscale<button type="button" class="sj-btn rr-mini" data-a="refresh">刷新</button></div><div class="sj-grp">';
    if (!ts.installed) h += sj.row('状态', '<span class="rr-bad">没有安装</span>') + `<div class="rr-tip">${STEP.install}</div>`;
    else if (!ts.running) h += sj.row('状态', '<span class="rr-bad">没有在运行</span>', esc(ts.error || '')) + `<div class="rr-tip">${STEP.run}</div>`;
    else if (!ts.loggedIn) h += sj.row('状态', '<span class="rr-bad">还没登录</span>') + `<div class="rr-tip">${STEP.login}</div>`;
    else {
      h += sj.row('状态', '<span class="rr-ok">已登录</span>') + sj.row('这台电脑', `<span class="rr-v">${esc(ts.dns)}</span>`) +
        sj.row('Tailscale 账号', `<span class="rr-v">${esc(ts.login || '—')}</span>`) +
        sj.row('HTTPS 证书', ts.https ? '<span class="rr-ok">已开启</span>' : '<span class="rr-bad">没开启</span>', ts.https ? '' : '远程访问要用 tailnet 内的 HTTPS') +
        (ts.https ? '' : `<div class="rr-tip">${STEP.https}</div>`) +
        (ts.magicdns ? '' : sj.row('MagicDNS', '<span class="rr-bad">没开启</span>', '手机要靠它找到这台电脑，请在管理后台 DNS 页面打开'));
    }
    h += '</div><div class="rr-gh">远程开放</div><div class="sj-grp">' +
      sj.row('通过 Tailscale 开放', sj.sw('on', rm.enabled, !ready && !rm.enabled, '通过 Tailscale 开放'), '只有你 tailnet 里的设备能访问，不会开放到公网');
    if (st.need) h += `<div class="rr-tip rr-warn">需要先开启 HTTPS 证书${st.need.url ? `：<a href="${esc(st.need.url)}" class="rr-link" data-u="${esc(st.need.url)}">${esc(st.need.url)}</a>` : ''}${STEP.https}</div>`;
    if (rm.enabled) {
      h += sj.row('地址', `<span class="rr-v rr-url">${esc(rm.url)}</span><button type="button" class="sj-btn" data-a="copyUrl">复制</button>`, '', 'sj-wrap') +
        sj.row('只允许同一个 Tailscale 账号的设备', sj.sw('same', rm.sameAccount, false, '只允许同一个 Tailscale 账号的设备'),
          rm.sameAccount ? '账号：' + esc(rm.selfLogin || ts.login || '—') : '同一个 tailnet 里的其他账号、带标签的设备也能配对') +
        sj.row('远程开放期间防止电脑空闲休眠', sj.sw('awake', rm.awake, false, '远程开放期间防止电脑空闲休眠'), '只防空闲休眠；合上盖子仍会睡眠，这时手机连不上');
      if (rm.active === false) h += '<div class="rr-tip rr-warn">Tailscale 里工作台加的规则不见了（可能运行过 tailscale serve reset）。关掉再打开开关可以恢复。</div>';
      if (rm.funnel) h += '<div class="rr-tip rr-warn">这个端口被设成了公网访问（Funnel）。工作台会拒绝公网来的请求；请在终端运行 <code>tailscale funnel --https=' + esc(rm.port) + ' off</code>。</div>';
      if (rm.error) h += `<div class="rr-tip rr-warn">${esc(rm.error)}</div>`;
    }
    h += '</div>';
    if (rm.enabled) h += '<div class="rr-gh">配对新设备</div><div class="sj-grp rr-pairbox"></div>';
    h += '<div class="rr-gh">已授权设备</div><div class="sj-grp rr-devs"></div>';
    el.innerHTML = h;
    drawPair(g); drawDevs(g, d.devices || []);
    sj.bind(el, {
      on: async (v, sw) => {
        sw.disabled = true; st.need = null;
        const r = await call(v ? 'remote-enable' : 'remote-disable');
        if (!live(g)) return;
        if (r.error) { toast(r.error); draw(g); return; }
        if (r.need === 'https') st.need = {url: r.url || ''};
        if (r.note) toast(r.note);
        if (v && r.ok) toast('已通过 Tailscale 开放');
        if (!v) { st.pair = null; toast('已关闭远程访问'); }
        draw(g, r);
      },
      same: async v => { const r = await call('remote-set', {sameAccount: v}); if (r.error) toast(r.error); if (live(g)) draw(g, r.error ? null : r); },
      awake: async v => { const r = await call('remote-set', {awake: v}); if (r.error) toast(r.error); if (live(g)) draw(g, r.error ? null : r); },
    }, {
      refresh: () => draw(g),
      copyUrl: (e, b) => c.copy(rm.url, b),
    });
    el.querySelectorAll('.rr-link').forEach(a => a.onclick = e => { e.preventDefault(); realApi('open-url', {url: a.dataset.u}).catch(() => {}); });
  }

  function drawPair(g) {
    const el = sj.body().querySelector('.rr-pairbox');
    if (!el) return;
    clearInterval(st.timer);
    const p = st.pair;
    if (!p || p.exp * 1000 <= Date.now()) {
      st.pair = null;
      el.innerHTML = sj.row('用手机扫码配对', '<button type="button" class="sj-btn sj-pri" data-a="pair">生成配对二维码</button>', '二维码 10 分钟内有效，只能用一次');
      sj.bind(el, {}, {pair: async (e, b) => {
        b.disabled = true;
        const r = await call('remote-pair');
        if (!live(g)) return;
        if (r.error) { b.disabled = false; toast(r.error); return; }
        st.pair = r; drawPair(g);
      }});
      return;
    }
    el.innerHTML = `<div class="rr-pair"><div class="rr-qrwrap"><div class="rr-qr">生成中…</div></div><div class="rr-pair-r">` +
      '<p>用手机的相机扫这个二维码，在打开的页面里点“确认配对”。手机要先登录 Tailscale。</p>' +
      `<div class="rr-plink"><input class="sj-in" readonly value="${esc(p.url)}" aria-label="配对链接"><button type="button" class="sj-btn" data-a="copy">复制链接</button></div>` +
      '<div class="rr-pfoot"><span class="rr-left"></span><button type="button" class="sj-btn" data-a="cancel">作废</button></div></div></div>';
    loadQr().then(() => { const q = el.querySelector('.rr-qr'); if (q && st.pair === p) q.outerHTML = qrSvg(p.url); })
      .catch(() => { const q = el.querySelector('.rr-qr'); if (q) q.textContent = '二维码没画出来，请复制链接发到手机上打开'; });
    const left = el.querySelector('.rr-left');
    const tick = () => {
      const s = Math.max(0, Math.round(p.exp - Date.now() / 1000));
      if (!s) { st.pair = null; drawPair(g); return; }
      left.textContent = `${Math.floor(s / 60)}:${p2(s % 60)} 后失效`;
    };
    tick(); st.timer = setInterval(() => { if (!live(g)) { clearInterval(st.timer); return; } tick(); }, 1000);
    sj.bind(el, {}, {
      copy: (e, b) => c.copy(p.url, b),
      cancel: async () => { await call('remote-pair-cancel'); st.pair = null; if (live(g)) drawPair(g); },
    });
  }

  function drawDevs(g, list) {
    const el = sj.body().querySelector('.rr-devs');
    if (!el) return;
    if (!list.length) { el.innerHTML = '<div class="sj-empty">还没有配对的设备</div>'; return; }
    el.innerHTML = list.map(d => `<div class="sj-row rr-dev" data-id="${esc(d.id)}"><div class="sj-lb"><span class="rr-dname">${esc(d.name)}</span>` +
      (d.online ? '<span class="rr-on">在线</span>' : '') +
      `<div class="sj-hint">配对于 ${when(d.created)} · 最近访问 ${ago(d.last)}${d.login ? ' · ' + esc(d.login) : ''}</div></div>` +
      '<div class="sj-val"><button type="button" class="sj-btn" data-a="ren">改名</button><button type="button" class="sj-btn rr-danger" data-a="rev">撤销</button></div></div>').join('');
    el.querySelectorAll('.rr-dev').forEach(row => {
      const d = list.find(x => x.id === row.dataset.id);
      sj.bind(row, {}, {
        ren: async () => {
          const v = await c.askText('设备名称', d.name);
          if (!v || !v.trim() || v.trim() === d.name) return;
          const r = await call('remote-rename', {id: d.id, name: v.trim()});
          if (r.error) { toast(r.error); return; }
          if (live(g)) drawDevs(g, r.devices || []);
        },
        rev: async (e, b) => {
          if (!confirm(`撤销“${d.name}”的授权？它会立即断开，要重新配对才能再用。`)) return;
          b.disabled = true;
          const r = await call('remote-revoke', {id: d.id});
          if (r.error) { b.disabled = false; toast(r.error); return; }
          toast('已撤销：' + d.name);
          if (live(g)) drawDevs(g, r.devices || []);
        },
      });
    });
  }

  let qrP = null;
  function loadQr() {
    if (window.qrcode) return Promise.resolve();
    if (!qrP) qrP = new Promise((ok, no) => {
      const s = document.createElement('script');
      s.src = '/static/vendor/qrcode-generator-1.4.4.js'; s.onload = ok; s.onerror = () => { qrP = null; no(); };
      document.head.appendChild(s);
    });
    return qrP;
  }
  function qrSvg(text) {
    const q = window.qrcode(0, 'M');
    q.addData(text); q.make();
    const n = q.getModuleCount(), m = 3;
    let d = '';
    for (let r = 0; r < n; r++) for (let k = 0; k < n; k++) if (q.isDark(r, k)) d += `M${k + m} ${r + m}h1v1h-1z`;
    return `<svg class="rr-qr" viewBox="0 0 ${n + 2 * m} ${n + 2 * m}" shape-rendering="crispEdges" role="img" aria-label="配对二维码"><rect width="100%" height="100%" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
  }

  sj.RENDER.remote = render;
  /* 有设备配对、被撤销时，正开着这一页就刷新 */
  const prev = c.onEvent;
  c.onEvent = ev => {
    try { if (prev) prev(ev); } finally {
      if (ev && ev.type === 'remote') {
        if (ev.what === 'paired' && !REMOTE) { toast('新设备已配对：' + (ev.name || '')); st.pair = null; }
        const b = sj.body && sj.body();
        if (!REMOTE && b && b.dataset.sec === 'remote' && sj.isOpen()) draw(sj.gen());
      }
    }
  };
  R.qrSvg = t => (window.qrcode ? qrSvg(t) : '');
  R.loadQr = loadQr;
}

/* 启动：远程页面先接管 fetch、通知、事件流（这时 app.js 还没加载）；页面脚本都加载完后再接设置页和手机布局 */
if (REMOTE) early();
document.addEventListener('DOMContentLoaded', late);
})();
