(() => {
'use strict';
/* 第十五轮 G：网页预览“点选元素”。
   网页预览（feat.js 的 renderWeb / mountFrame）不改：这里观察右栏，出现 .wp-bar 就在地址框后面加按钮，出现新的 .wp-frame 就重置状态。
   点按钮时才走转发：后台 wp-pick 为这个本机端口起一个转发口（webpick.py），iframe 换成转发地址，页面里注入的
   ux-m-frame.js 发来 hello 后开始点选。点中后把元素整理成一段引用追加到输入框，不发送。
   任何一步失败：iframe 换回原地址照常预览，按钮变成不可用并提示原因；不影响预览和主流程。 */
const C = window.CWB;
if (!C || !C.pane || !C.api) return;
const {api, toast, $} = C;
const body = C.pane.body;
const LOOP = ['localhost', '127.0.0.1', '[::1]', '0.0.0.0'];
const ICON = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M6 2.5H4A1.5 1.5 0 0 0 2.5 4v2M2.5 9v1.5M9 2.5h1.5M13.5 6v-2A1.5 1.5 0 0 0 12 2.5M6 13.5H4A1.5 1.5 0 0 1 2.5 12"/>' +
  '<path d="M8.2 8.2l5.6 2-2.4.9-.9 2.4z" fill="currentColor"/></svg>';
const M = {frame: null, orig: '', target: '', pOrigin: '', proxied: false, ready: false, picking: false, acked: false, busy: false,
  fail: '', want: false, retry: 0, seq: 0, timer: 0, ackT: 0, onload: null};

const isLoop = u => { try { const x = new URL(u); return x.protocol === 'http:' && LOOP.includes(x.hostname); } catch { return false; } };

/* ---------------- 按钮与状态 ---------------- */
function button() {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'btn small wpk-btn';
  b.innerHTML = `<span class="wpk-ic">${ICON}</span><span>点选元素</span>`;
  b.addEventListener('click', e => { e.stopPropagation(); onBtn(); });
  return b;
}
function paint() {
  const b = body.querySelector('.wpk-btn');
  const fr = M.frame;
  if (fr) fr.classList.toggle('wpk-picking', M.picking);
  if (!b) return;
  const why = !fr ? '' : !isLoop(M.orig) ? '只支持本机 http 页面' : M.fail;
  b.classList.toggle('on', M.picking);
  b.classList.toggle('busy', M.busy);
  b.setAttribute('aria-pressed', M.picking ? 'true' : 'false');
  b.setAttribute('aria-disabled', !fr || why ? 'true' : 'false');
  b.title = why || (M.picking ? '退出点选（Esc）' : '点选元素');
}
function reset(fr) {
  clearTimeout(M.timer); clearTimeout(M.ackT);
  if (M.frame && M.onload) M.frame.removeEventListener('load', M.onload);
  if (M.picking) unbind();
  Object.assign(M, {frame: fr || null, orig: fr ? fr.getAttribute('src') || '' : '', target: '', pOrigin: '', proxied: false, ready: false,
    picking: false, acked: false, busy: false, fail: '', want: false, retry: 0, onload: null});
  M.seq++;
}
function sync() {
  const fr = body.querySelector('iframe.wp-frame');
  if (fr !== M.frame) reset(fr);
  const bar = body.querySelector('.wp-bar');
  if (bar && !bar.querySelector('.wpk-btn')) {
    const inp = bar.querySelector('#wpUrl');
    if (inp) inp.after(button()); else bar.prepend(button());
  }
  paint();
}
try { new MutationObserver(sync).observe(body, {childList: true, subtree: true}); } catch (e) { console.error(e); }

/* ---------------- 流程 ---------------- */
function post(m) { try { M.frame.contentWindow.postMessage(m, M.pOrigin); } catch {} }
function palette() {
  const cs = getComputedStyle(document.documentElement), v = n => cs.getPropertyValue(n).trim();
  return {accent: v('--accent'), soft: v('--accent-soft'), bg: v('--btn'), fg: v('--btn-fg')};
}
function onBtn() {
  if (!M.frame) return;
  if (!isLoop(M.orig)) { toast('只支持本机 http 页面'); return; }
  if (M.fail) { toast(M.fail); return; }
  if (M.busy) return;
  if (M.picking) { endPick(); return; }
  M.want = true; M.retry = 0;
  if (M.ready) begin(); else prepare();
}
async function prepare() {
  const seq = M.seq, fr = M.frame;
  M.busy = true; paint();
  let d;
  try { d = await api('wp-pick', {url: M.orig, host: location.hostname}); }
  catch (e) { if (seq === M.seq) fail(e.message || '暂时无法点选', false); return; }
  if (seq !== M.seq || !d || !d.src || !d.origin) { if (seq === M.seq) fail('暂时无法点选', false); return; }
  M.pOrigin = d.origin; M.target = d.target || ''; M.proxied = true; M.ready = false;
  if (M.onload) fr.removeEventListener('load', M.onload);
  // 只在等着开始点选时按加载完成判失败；平时在页面里跳到别处不打扰
  M.onload = () => { if (seq === M.seq && M.proxied && M.want && !M.ready && !M.fail) { clearTimeout(M.timer); M.timer = setTimeout(() => { if (seq === M.seq && !M.ready) fail('这个页面不支持点选', true); }, 1500); } };
  fr.addEventListener('load', M.onload);
  clearTimeout(M.timer);
  M.timer = setTimeout(() => { if (seq === M.seq && !M.ready) fail('这个页面不支持点选', true); }, 30000);
  fr.src = d.src;
}
function fail(msg, revert) {
  clearTimeout(M.timer); clearTimeout(M.ackT);
  if (M.frame && M.onload) { M.frame.removeEventListener('load', M.onload); M.onload = null; }
  if (M.picking) unbind();
  Object.assign(M, {busy: false, want: false, picking: false, ready: false, fail: msg});
  if (revert && M.frame && M.proxied) { M.proxied = false; M.frame.src = M.orig; }  // 换回原地址，照常预览
  toast(msg); paint();
}
function begin() {
  const seq = M.seq;
  Object.assign(M, {want: false, picking: true, acked: false, busy: false});
  bind(); paint();
  post({cwbPick: 'start', colors: palette()});
  clearTimeout(M.ackT);
  M.ackT = setTimeout(() => {
    if (seq !== M.seq || !M.picking || M.acked) return;
    // 当前文档不是经转发来的（比如在页面里跳到了别的站点）：重新经转发打开一次，还不行就不可用
    M.picking = false; M.ready = false; unbind();
    if (M.retry++ < 1) { M.want = true; prepare(); } else fail('这个页面不支持点选', true);
  }, 1500);
}
function endPick() {
  if (!M.picking) return;
  post({cwbPick: 'stop'});
  M.picking = false; M.want = false;
  unbind(); paint();
}

/* 点选期间：工作台里按 Esc 退出（捕获阶段先拿到，不让它落到输入框去中断回复）；点工作台别处也退出 */
function onKey(e) {
  if (e.key !== 'Escape' || e.isComposing) return;
  e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
  endPick();
}
function onDown(e) { if (!(e.target.closest && e.target.closest('.wpk-btn'))) endPick(); }
function bind() { addEventListener('keydown', onKey, true); document.addEventListener('pointerdown', onDown, true); }
function unbind() { removeEventListener('keydown', onKey, true); document.removeEventListener('pointerdown', onDown, true); }

addEventListener('message', e => {
  const fr = M.frame;
  if (!fr || !M.pOrigin || e.origin !== M.pOrigin || e.source !== fr.contentWindow) return;
  const d = e.data;
  if (!d || typeof d !== 'object' || typeof d.cwbPick !== 'string') return;
  try {
    switch (d.cwbPick) {
      case 'hello':
        M.ready = true; M.busy = false; clearTimeout(M.timer);
        if (M.want) begin(); else paint();
        break;
      case 'bye':  // 页面跳转或刷新：点选中的话等新页面的 hello 接着点
        M.ready = false;
        if (M.picking) { clearTimeout(M.ackT); M.picking = false; M.want = true; unbind(); }
        paint();
        break;
      case 'started': M.acked = true; break;
      case 'picked':
        clearTimeout(M.ackT); M.picking = false; M.want = false; unbind(); paint();
        quote(d.info);
        break;
      case 'cancel': clearTimeout(M.ackT); M.picking = false; M.want = false; unbind(); paint(); break;
      case 'fail': fail(d.reason === 'up' ? '连不上这个地址' : '暂时无法点选', true); break;
    }
  } catch (err) { console.error(err); }
});

/* ---------------- 引用 ---------------- */
const one = (v, n) => { const s = String(v == null ? '' : v).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const num = v => Number.isFinite(+v) ? Math.round(+v) : 0;
function realUrl(h) {
  if (M.pOrigin && M.target && (h === M.pOrigin || h.startsWith(M.pOrigin + '/'))) return M.target + h.slice(M.pOrigin.length);
  return h;
}
function quoteText(x, href) {
  const tag = one(x.tag, 40).replace(/[^\w-]/g, '') || 'element';
  const attrs = (Array.isArray(x.attrs) ? x.attrs : []).slice(0, 14)
    .filter(a => Array.isArray(a) && /^[\w-]{1,40}$/.test(String(a[0])))
    .map(a => ` ${a[0]}="${one(a[1], 100).replace(/"/g, '&quot;')}"`).join('');
  const r = x.rect || {}, vp = x.vp || {}, text = one(x.text, 160);
  const lines = [`页面：${one(href, 2000)}`, `选择器：${one(x.sel, 600)}`, `元素：<${tag}${attrs}>`];
  if (text) lines.push(`文字：${text}`);
  lines.push(`位置：x ${num(r.x)}，y ${num(r.y)}（相对页面左上角），宽 ${num(r.w)}，高 ${num(r.h)}；视口 ${num(vp.w)}×${num(vp.h)}`);
  const b = lines.join('\n');
  let fence = '```';
  while (b.includes(fence)) fence += '`';
  return `网页预览中选中的元素：\n${fence}text\n${b}\n${fence}\n`;
}
function quote(x) {
  if (!x || typeof x !== 'object') return;
  const inp = $('#input');
  if (!inp) return;
  const t = quoteText(x, realUrl(String(x.href || M.orig)));
  const v = inp.value, sep = !v ? '' : v.endsWith('\n\n') ? '' : v.endsWith('\n') ? '\n' : '\n\n';
  inp.focus();
  inp.setSelectionRange(v.length, v.length);
  let ok = false;
  try { ok = document.execCommand('insertText', false, sep + t); } catch {}
  if (!ok || inp.value !== v + sep + t) { inp.value = v + sep + t; inp.dispatchEvent(new Event('input', {bubbles: true})); }
  inp.setSelectionRange(inp.value.length, inp.value.length);
  inp.scrollTop = inp.scrollHeight;
  toast('已引用到输入框');
}
C.wpPick = {quoteText, isLoop};  // 测试用
sync();
})();
