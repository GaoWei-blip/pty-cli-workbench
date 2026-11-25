// 第十五轮 G 测试辅助：在 agent-browser 浏览器里对网页预览的 iframe 执行脚本（agent-browser 的 eval 进不了跨源 iframe）。
// 用法：node tests/cdp_frame_eval.mjs <CDP 浏览器地址> <iframe 地址包含的片段> <表达式>
// 跨站 iframe 是独立进程（Target 类型 iframe）就直接连；同站的在页面进程里，按 frameId 找它的主世界上下文。输出 JSON。
const [ws, match, expr] = process.argv.slice(2);
const s = new WebSocket(ws);
let id = 0;
const pend = new Map(), ctxs = [];
const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pend.set(i, r); s.send(JSON.stringify({id: i, method, params, sessionId})); });
const out = v => { console.log(JSON.stringify(v)); process.exit(0); };
setTimeout(() => out({error: 'timeout'}), 10000);
s.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  else if (m.method === 'Runtime.executionContextCreated') ctxs.push([m.sessionId, m.params.context]);
};
s.onopen = async () => {
  try {
    const infos = (await send('Target.getTargets')).result.targetInfos;
    let sess = null, ctx;
    const ifr = infos.find(x => x.type === 'iframe' && x.url.includes(match));
    if (ifr) sess = (await send('Target.attachToTarget', {targetId: ifr.targetId, flatten: true})).result.sessionId;
    else {
      for (const p of infos.filter(x => x.type === 'page')) {
        const sid = (await send('Target.attachToTarget', {targetId: p.targetId, flatten: true})).result.sessionId;
        const tree = (await send('Page.getFrameTree', {}, sid)).result.frameTree;
        const find = n => (n.frame.parentId && n.frame.url.includes(match)) ? n.frame : (n.childFrames || []).map(find).find(Boolean);
        const fr = find(tree);
        if (!fr) continue;
        await send('Runtime.enable', {}, sid);
        const c = ctxs.find(([ss, c]) => ss === sid && c.auxData && c.auxData.frameId === fr.id && c.auxData.isDefault);
        if (!c) continue;
        sess = sid; ctx = c[1].id;
        break;
      }
    }
    if (!sess) out({error: 'no frame'});
    const r = await send('Runtime.evaluate', {expression: expr, returnByValue: true, awaitPromise: true, contextId: ctx}, sess);
    if (r.result && r.result.exceptionDetails) out({error: r.result.exceptionDetails.text});
    out(r.result ? r.result.result.value : r);
  } catch (e) { out({error: String(e)}); }
};
