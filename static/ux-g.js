(() => {
'use strict';
/* 第十三轮 G：Claude 操作工作台（bin/cwb.py → features.py 的 ui-* 接口 → SSE → 这里）。
   ui-req：open 在右栏预览打开文件（可跳行），web 在网页预览打开网址。发起请求的是当前对话就直接打开；
           不是就弹提示“〈标题〉请求打开 xxx · 打开”，点“打开”才执行，不切换对话。
   ui-shot：只有原生 App（外壳注入 CWB_NATIVE_SHOT）认领，转给外壳截图；外壳回调 CWB_shotDone，再交回后台保存。
   onEvent 串在原有处理之后，不覆盖。 */
const C = window.CWB;
if (!C) return;
const {S, api, esc} = C;
const chain = (name, fn) => { const prev = C[name]; C[name] = (...a) => { if (prev) prev(...a); try { fn(...a); } catch (e) { console.error(e); } }; };
const base = p => (p || '').split('/').filter(Boolean).pop() || p;
const shortUrl = u => { try { const x = new URL(u); return x.host + (x.pathname === '/' && !x.search ? '' : x.pathname) + x.search; } catch { return u; } };

function run(ev) {
  if (ev.act === 'open') C.preview(ev.path, ev.line ? {line: ev.line} : undefined);
  else if (ev.act === 'web' && C.openWeb) C.openWeb(ev.url);
}

/* ---- 非当前对话的请求：带“打开”的提示，悬停时不消失 ---- */
let box = null, timer = 0;
function hide() { clearTimeout(timer); if (box) box.hidden = true; }
function arm() { clearTimeout(timer); timer = setTimeout(hide, 10000); }
function ask(ev) {
  if (!box) {
    box = document.createElement('div');
    box.id = 'gToast'; box.hidden = true;
    box.onmouseenter = () => clearTimeout(timer);
    box.onmouseleave = arm;
    document.body.appendChild(box);
  }
  const what = ev.act === 'open' ? base(ev.path) + (ev.line ? ':' + ev.line : '') : shortUrl(ev.url);
  box.innerHTML = `<span class="gt-txt"><b>${esc(C.sessTitle(ev.sid) || '对话')}</b> 请求打开 <span class="gt-what">${esc(what)}</span></span>` +
    '<span class="gt-dot">·</span><button type="button" class="gt-go">打开</button>';
  box.title = ev.act === 'open' ? ev.path : ev.url;
  box.querySelector('.gt-go').onclick = e => { e.stopPropagation(); hide(); run(ev); };
  box.hidden = false;
  box.style.animation = 'none'; void box.offsetWidth; box.style.animation = '';
  arm();
}

/* ---- 截图：外壳在不显示的网页视图里加载、截图，结果交回后台 ---- */
const shell = () => window.CWB_NATIVE_SHOT && window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.cwb;
async function shot(ev) {
  if (!shell()) return;  // 浏览器里打开的页面不认领，后台几秒后报“需要 App”
  let d;
  try { d = await api('ui-shot-ack', {id: ev.id}); } catch { return; }
  if (!d.go) return;
  try { window.webkit.messageHandlers.cwb.postMessage({type: 'shot', id: ev.id, url: ev.url, w: ev.w, h: ev.h}); }
  catch (e) { api('ui-shot-done', {id: ev.id, error: '外壳不支持截图'}).catch(() => {}); }
}
window.CWB_shotDone = r => {
  if (!r || !r.id) return;
  api('ui-shot-done', {id: r.id, png: r.png || '', error: r.error || ''}).catch(() => {});
};

chain('onEvent', ev => {
  if (ev.type === 'ui-req') { if (ev.sid && ev.sid === S.cur) run(ev); else ask(ev); }
  else if (ev.type === 'ui-shot') shot(ev);
});
})();
