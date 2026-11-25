(() => {
'use strict';
/* 第十三轮 E：运行机制相关的界面。
   1. 上次回复被中断：服务崩溃、关机、终端守护进程崩溃或 CLI 意外退出，进行中的一轮没有正常结束时，
      后台记下这个对话；打开时时间线末尾显示“上次回复被中断”和“继续”（发送“继续”）。正常结束、用户主动中断不记。
   2. 左栏：被中断的对话在时间左边标“可继续”。
   只串接 onLoad / onEvent / onSide 钩子（先调原函数），不改 app.js。 */
const C = window.CWB;
if (!C) return;
const {S, $} = C;
const chain = (name, fn) => { const prev = C[name]; C[name] = (...a) => { if (prev) prev(...a); try { fn(...a); } catch (e) { console.error(e); } }; };
let intr = null, el = null;
const working = () => !!(S.runner && S.runner.alive && (S.runner.busy || S.runner.turn));
function render() {
  if (!(intr && S.cur && !working())) { if (el) { el.remove(); el = null; } return; }
  const feed = $('#feed');
  if (!el) {
    el = document.createElement('div'); el.className = 'it ux-intr';
    el.innerHTML = '<span class="ux-intr-t">上次回复被中断</span><button type="button" class="ux-intr-go">继续</button>';
    const b = el.querySelector('button');
    b.onclick = () => { b.disabled = true; Promise.resolve(C.sendText('继续')).finally(() => { b.disabled = false; }); };
  }
  const tail = S.exitEl && S.exitEl.parentNode === feed ? S.exitEl : null;  // “CLI 已退出”留在最后
  if (tail ? el.nextElementSibling !== tail : feed.lastElementChild !== el) {
    const stick = C.nearBottom ? C.nearBottom() : false;
    feed.insertBefore(el, tail);
    if (stick && C.toBottom) C.toBottom();
  }
}
function tags() {
  for (const row of document.querySelectorAll('#projects .srow[data-sid]')) {
    const s = C.findSession(row.dataset.sid), want = !!(s && s.resumable && !s.busy);
    let t = row.querySelector(':scope > .ux-resume');
    if (want && !t) {
      t = document.createElement('span'); t.className = 'ux-resume'; t.textContent = '可继续';
      row.insertBefore(t, row.querySelector(':scope > .ux-unread') || row.querySelector(':scope > .stime'));
    } else if (!want && t) t.remove();
  }
}
chain('onLoad', d => {
  intr = (d && d.interrupted) || null;
  if (el) { el.remove(); el = null; }
  render();
});
chain('onEvent', ev => {
  if (ev.type === 'marks' && ev.sid && 'interrupted' in ev) {
    const s = C.findSession(ev.sid);
    if (s) s.resumable = !!ev.interrupted;
    if (ev.sid === S.cur) intr = ev.interrupted || null;
    Promise.resolve().then(() => { render(); tags(); });
  } else if ((ev.type === 'runner' || ev.type === 'items') && ev.sid === S.cur) {
    Promise.resolve().then(render);  // 等原有处理更新完 S.runner、时间线再摆位置
  }
});
chain('onSide', tags);
})();
