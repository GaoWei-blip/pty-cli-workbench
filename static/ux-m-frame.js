/* 第十五轮 G：注入到网页预览页面里的点选脚本（由 webpick.py 的转发口以 /__cwb_pick.js 提供，工作台主页面不加载）。
   只在“父窗口就是顶层工作台”的那一层生效；和工作台只用 postMessage 通信，只认配置里的工作台地址。
   不改页面样式表、不用 innerHTML，页面有严格 CSP / Trusted Types 时也能画高亮框。 */
(function (W, CFG) {
  'use strict';
  var D = W.document;

  /* ---------------- 选择器与元素描述（纯函数，测试经 __cwbPickTest 取用） ---------------- */
  var cssEsc = function (s) {
    if (W.CSS && W.CSS.escape) return W.CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, function (c) { return '\\' + c; });
  };
  var attrEsc = function (v) { return String(v).replace(/["\\]/g, '\\$&'); };
  var STATE = /^(?:is-|has-)|^(?:hover|hovered|active|focus|focused|focus-visible|selected|open|opened|show|shown|visible|hidden|disabled|current|checked|expanded|collapsed)$/i;
  var GEN = /^(?:css|sc|jsx|emotion|svelte|astro|ng|_ngcontent|tw|chakra|mui-\w+)-|__[A-Za-z0-9_-]{4,}$|\d{3,}/i;
  function goodId(id) {
    return !!id && id.length <= 64 && !/[\s:«»]/.test(id) && !/^[\d-]/.test(id) && !/\d{4,}/.test(id) && !/^[a-f0-9-]{12,}$/i.test(id);
  }
  function goodClass(c) {
    if (!c || c.length > 40 || !/^[a-zA-Z_][\w-]*$/.test(c) || STATE.test(c) || GEN.test(c)) return false;
    if (/^[a-zA-Z]{5,8}$/.test(c) && (c.slice(1).match(/[A-Z]/g) || []).length >= 2) return false;  // 形如 bxLvGq 的随机类名
    return true;
  }
  function classes(el) {
    var cl = el.classList ? Array.prototype.slice.call(el.classList) : [];
    var ok = cl.filter(goodClass);
    var score = function (c) { return (/\d/.test(c) ? 0 : 2) + (c.length >= 4 ? 1 : 0); };
    return ok.map(function (c, i) { return [c, i]; }).sort(function (a, b) { return score(b[0]) - score(a[0]) || a[1] - b[1]; })
      .map(function (x) { return x[0]; });
  }
  function count(root, sel) { try { return root.querySelectorAll(sel).length; } catch (e) { return 99; } }
  function unique(sel, el) { try { var r = D.querySelectorAll(sel); return r.length === 1 && r[0] === el; } catch (e) { return false; } }
  function sibCount(el, sel) {
    var p = el.parentElement; if (!p) return 1;
    var n = 0, k = p.children;
    for (var i = 0; i < k.length; i++) { try { if (k[i].matches(sel)) n++; } catch (e) { return 99; } }
    return n;
  }
  var ATTRS = ['data-testid', 'data-test-id', 'data-test', 'data-cy', 'data-qa', 'name', 'aria-label'];
  function tagOf(el) { return cssEsc(el.localName || el.nodeName.toLowerCase()); }
  function candidates(el) {
    var tag = tagOf(el), out = [];
    for (var i = 0; i < ATTRS.length; i++) {
      var v = el.getAttribute(ATTRS[i]);
      if (v && v.length <= 60 && !/[\r\n]/.test(v)) out.push(tag + '[' + ATTRS[i] + '="' + attrEsc(v) + '"]');
    }
    var cl = classes(el).slice(0, 3);
    for (var j = 0; j < cl.length; j++) out.push(tag + '.' + cssEsc(cl[j]));
    for (var a = 0; a < cl.length; a++) for (var b = a + 1; b < cl.length; b++) out.push(tag + '.' + cssEsc(cl[a]) + '.' + cssEsc(cl[b]));
    out.push(tag);
    return out;
  }
  function nthOfType(el) {
    var n = 1, s = el.previousElementSibling;
    for (; s; s = s.previousElementSibling) if (s.localName === el.localName) n++;
    return n;
  }
  /* 在兄弟里唯一的一段：优先测试属性、name、aria-label、类名，最后 tag:nth-of-type(n) */
  function segment(el) {
    var c = candidates(el);
    for (var i = 0; i < c.length; i++) if (sibCount(el, c[i]) === 1) return c[i];
    return tagOf(el) + ':nth-of-type(' + nthOfType(el) + ')';
  }
  function anchor(el) { return goodId(el.id) && unique('#' + cssEsc(el.id), el) ? '#' + cssEsc(el.id) : ''; }
  function selector(el) {
    if (!el || el.nodeType !== 1) return '';
    if (el === D.documentElement) return 'html';
    if (el === D.body) return 'body';
    if (el.getRootNode && el.getRootNode() !== D) return '';
    var a = anchor(el); if (a) return a;
    var c = candidates(el);
    for (var i = 0; i < c.length; i++) if (unique(c[i], el)) return c[i];
    var parts = [segment(el)], cur = el;
    if (unique(parts[0], el)) return parts[0];
    for (var guard = 0; guard < 64; guard++) {
      var p = cur.parentElement;
      if (!p || p === D.documentElement) { parts.unshift('html'); break; }
      if (p === D.body) { parts.unshift('body'); break; }
      var pa = anchor(p);
      if (pa) { parts.unshift(pa); break; }
      parts.unshift(segment(p));
      var s = parts.join(' > ');
      if (unique(s, el)) return s;
      cur = p;
    }
    var full = parts.join(' > ');
    for (var k = 1; k < parts.length - 1; k++) {  // 去掉前面多余的层级，仍唯一就用短的
      var t = parts.slice(k).join(' > ');
      if (unique(t, el)) return t;
    }
    if (unique(full, el)) return full;
    var chain = [], e = el;  // 兜底：完整 nth-child 路径
    for (; e && e !== D.documentElement; e = e.parentElement) chain.unshift(tagOf(e) + ':nth-child(' + (Array.prototype.indexOf.call(e.parentElement.children, e) + 1) + ')');
    return 'html > ' + chain.join(' > ');
  }
  var squash = function (s, n) { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  var KEEP = ['id', 'class', 'name', 'type', 'role', 'aria-label', 'href', 'src', 'alt', 'placeholder', 'title', 'for', 'data-testid'];
  function textOf(el) {
    var tag = el.localName;
    if (tag === 'input' || tag === 'textarea') return el.type === 'password' ? '' : squash(el.placeholder || '', 120);
    if (tag === 'select') { var o = el.options && el.options[el.selectedIndex]; return o ? squash(o.text, 120) : ''; }
    if (tag === 'img') return squash(el.alt || '', 120);
    var t = '';
    try { t = el.innerText; } catch (e) { t = ''; }
    if (t == null) t = el.textContent || '';
    return squash(String(t).slice(0, 2000), 120);
  }
  function describe(el) {
    var attrs = [];
    for (var i = 0; i < KEEP.length; i++) {
      var k = KEEP[i], v = el.getAttribute(k);
      if (v == null) continue;
      if (k === 'class') { v = Array.prototype.slice.call(el.classList).slice(0, 6).join(' ') + (el.classList.length > 6 ? ' …' : ''); if (!v) continue; }
      if ((k === 'src' || k === 'href') && /^data:/i.test(v)) v = v.slice(0, 24) + '…';
      attrs.push([k, squash(v, 80)]);
    }
    var r = el.getBoundingClientRect();
    return {
      href: String(W.location.href), title: squash(D.title, 80), sel: selector(el), tag: el.localName, attrs: attrs, text: textOf(el),
      rect: {x: Math.round(r.left + W.scrollX), y: Math.round(r.top + W.scrollY), w: Math.round(r.width), h: Math.round(r.height)},
      vp: {w: W.innerWidth, h: W.innerHeight}
    };
  }
  var core = {selector: selector, describe: describe, goodId: goodId, goodClass: goodClass};
  if (typeof W.__cwbPickTest === 'function') { try { W.__cwbPickTest(core); } catch (e) {} return; }

  /* ---------------- 与工作台握手 ---------------- */
  var P;
  try { P = W.parent; } catch (e) { return; }
  if (!P || P === W || P !== W.top) return;
  var KEY = '__cwbPick';
  try { if (W[KEY]) return; Object.defineProperty(W, KEY, {value: 1}); } catch (e) { return; }
  var parents = (CFG && CFG.parents) || [];
  function post(m) { for (var i = 0; i < parents.length; i++) { try { P.postMessage(m, parents[i]); } catch (e) {} } }
  if (CFG && CFG.fail) { post({cwbPick: 'fail', reason: CFG.fail}); return; }

  var picking = false, cur = null, host = null, box = null, label = null, lt = null, lc = null, ls = null, raf = 0, prevCursor = null;
  var col = {accent: '#2f6bd8', soft: 'rgba(47,107,216,.12)', bg: '#1f1e1c', fg: '#ffffff'};
  var COLOR = /^[#\w(),.%\s-]{1,48}$/;
  var reduce = false;
  try { reduce = W.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}
  function css(el, o) { for (var k in o) el.style.setProperty(k, o[k], 'important'); }
  function mk(tag, o) { var e = D.createElement(tag); if (o) css(e, o); return e; }
  function overlay() {
    if (host && host.isConnected) return;
    host = mk('cwb-pick-layer', {position: 'fixed', top: '0', left: '0', width: '0', height: '0', margin: '0', padding: '0', border: '0',
      'z-index': '2147483647', 'pointer-events': 'none', display: 'block', overflow: 'visible', background: 'transparent'});
    var root = host;
    try { root = host.attachShadow({mode: 'open'}); } catch (e) {}
    var anim = reduce ? 'none' : 'top .07s ease-out, left .07s ease-out, width .07s ease-out, height .07s ease-out';
    box = mk('div', {position: 'fixed', 'box-sizing': 'border-box', border: '2px solid ' + col.accent, background: col.soft, 'border-radius': '4px',
      'pointer-events': 'none', display: 'none', transition: anim, margin: '0', padding: '0'});
    label = mk('div', {position: 'fixed', display: 'none', 'pointer-events': 'none', background: col.bg, color: col.fg, margin: '0',
      font: '500 11px/16px -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",system-ui,sans-serif', 'letter-spacing': '0',
      padding: '3px 8px', 'border-radius': '6px', 'white-space': 'nowrap', 'box-shadow': '0 2px 8px rgba(0,0,0,.18)',
      'max-width': 'calc(100vw - 12px)', overflow: 'hidden', 'text-overflow': 'ellipsis', 'text-align': 'left', 'text-transform': 'none'});
    lt = mk('span', {font: 'inherit', color: 'inherit', 'font-weight': '600'});
    lc = mk('span', {font: 'inherit', color: 'inherit', opacity: '.72'});
    ls = mk('span', {font: 'inherit', color: 'inherit', opacity: '.72', 'margin-left': '8px', 'font-variant-numeric': 'tabular-nums'});
    label.appendChild(lt); label.appendChild(lc); label.appendChild(ls);
    root.appendChild(box); root.appendChild(label);
    (D.documentElement || D.body).appendChild(host);
  }
  function norm(t) {
    if (!t || t.nodeType !== 1) return null;
    if (t === host) return null;
    if (t instanceof W.SVGElement && t.ownerSVGElement) { while (t.ownerSVGElement) t = t.ownerSVGElement; }  // 图标里的 path 等归到整个 svg
    return t;
  }
  function draw() {
    raf = 0;
    if (!picking || !box) return;
    if (!cur || !cur.isConnected) { box.style.setProperty('display', 'none', 'important'); label.style.setProperty('display', 'none', 'important'); return; }
    var r = cur.getBoundingClientRect(), vw = W.innerWidth, vh = W.innerHeight;
    var first = box.style.getPropertyValue('display') !== 'block', anim = box.style.getPropertyValue('transition');
    if (first) box.style.setProperty('transition', 'none', 'important');  // 第一次出现不从角落滑过来
    css(box, {display: 'block', top: r.top + 'px', left: r.left + 'px', width: Math.max(r.width, 2) + 'px', height: Math.max(r.height, 2) + 'px'});
    if (first) { void box.offsetWidth; box.style.setProperty('transition', anim, 'important'); }
    var cl = classes(cur).slice(0, 2);
    lt.textContent = cur.localName;
    lc.textContent = cur.id && goodId(cur.id) ? '#' + cur.id : cl.length ? '.' + cl.join('.') : '';
    ls.textContent = Math.round(r.width) + ' × ' + Math.round(r.height);
    css(label, {display: 'block', left: '0px', top: '0px'});
    var lw = label.offsetWidth, lh = label.offsetHeight;
    var y = r.top - lh - 6;
    if (y < 4) y = r.bottom + 6;
    if (y + lh > vh - 4) y = Math.max(4, Math.min(r.top + 6, vh - lh - 4));
    var x = Math.max(6, Math.min(r.left, vw - lw - 6));
    css(label, {left: x + 'px', top: y + 'px'});
  }
  function later() { if (!raf) raf = W.requestAnimationFrame(draw); }
  function setCursor(on) {
    var h = D.documentElement; if (!h) return;
    if (on) { if (prevCursor === null) prevCursor = [h.style.getPropertyValue('cursor'), h.style.getPropertyPriority('cursor')]; h.style.setProperty('cursor', 'crosshair', 'important'); }
    else if (prevCursor) { if (prevCursor[0]) h.style.setProperty('cursor', prevCursor[0], prevCursor[1]); else h.style.removeProperty('cursor'); prevCursor = null; }
  }
  function start(d) {
    var c = d && d.colors;
    if (c && typeof c === 'object') for (var k in col) if (typeof c[k] === 'string' && COLOR.test(c[k])) col[k] = c[k].trim();
    if (host && host.isConnected) { host.remove(); host = null; }
    picking = true; cur = null;
    overlay(); setCursor(true);
    post({cwbPick: 'started'});
  }
  function stop() {
    picking = false; cur = null;
    if (raf) { W.cancelAnimationFrame(raf); raf = 0; }
    if (host) { try { host.remove(); } catch (e) {} host = null; }
    setCursor(false);
  }
  function pick(t) {
    var el = norm(t) || cur;
    if (!el) return;
    var info;
    try { info = describe(el); } catch (e) { info = null; }
    cur = el; draw();
    if (box) css(box, {background: col.soft, 'border-width': '2px', transition: 'none'});
    var done = function () { stop(); };
    if (reduce) done(); else W.setTimeout(done, 160);
    picking = false;
    post(info ? {cwbPick: 'picked', info: info} : {cwbPick: 'cancel'});
  }
  /* 监听在脚本最早执行时就挂上（捕获阶段、window 上），保证点选时先于页面自己的处理拿到事件 */
  var BLOCK = ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click', 'dblclick', 'auxclick', 'contextmenu', 'touchstart', 'touchend'];
  function block(e) {
    if (!picking) return;
    e.preventDefault(); e.stopPropagation(); if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    if (e.type === 'click' && e.button === 0) pick(e.target);
  }
  for (var i = 0; i < BLOCK.length; i++) W.addEventListener(BLOCK[i], block, {capture: true, passive: false});
  W.addEventListener('mousemove', function (e) {
    if (!picking) return;
    var t = norm(e.target);
    if (t !== cur) { cur = t; later(); }
  }, true);
  W.addEventListener('scroll', function () { if (picking) later(); }, {capture: true, passive: true});
  W.addEventListener('resize', function () { if (picking) later(); });
  W.addEventListener('keydown', function (e) {
    if (!picking || e.key !== 'Escape') return;
    e.preventDefault(); e.stopPropagation(); if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    stop(); post({cwbPick: 'cancel'});
  }, true);
  W.addEventListener('message', function (e) {
    if (e.source !== P || parents.indexOf(e.origin) < 0) return;
    var d = e.data;
    if (!d || typeof d !== 'object') return;
    if (d.cwbPick === 'start') start(d);
    else if (d.cwbPick === 'stop') stop();
    else if (d.cwbPick === 'ping') post({cwbPick: 'hello', href: String(W.location.href)});
  });
  W.addEventListener('pagehide', function () { stop(); post({cwbPick: 'bye'}); });
  W.addEventListener('pageshow', function (e) { if (e.persisted) post({cwbPick: 'hello', href: String(W.location.href)}); });
  post({cwbPick: 'hello', href: String(W.location.href)});
})(window, /*CWB_PICK_CFG*/{});
