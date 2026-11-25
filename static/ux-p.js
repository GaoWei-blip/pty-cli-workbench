(() => {
'use strict';
/* 11-18 设置 →“个性化”：风格和语气、全局指令、自定义命令、技能（新建与编辑）。
   接口见 persona.py：persona、claude-md、cmd-list/get/save/delete、skill-get/save；技能列表和删除沿用 skills、skill-delete。 */
const C = window.CWB;
if (!C || !C.sj) return;
const {S, esc} = C;
const sj = C.sj;
async function call(n, b) {
  try { const d = await C.api(n, b || {}); return d && typeof d === 'object' ? d : {error: '返回格式不对'}; }
  catch (e) { return {error: (e && e.message) || '操作没有成功'}; }
}
const toast = t => C.toast(t);
const cwd = () => S.cwd || '';
const tilde = p => (p && S.home && (p === S.home || p.startsWith(S.home + '/')) ? '~' + p.slice(S.home.length) : p || '');
const base = p => String(p || '').replace(/\/+$/, '').split('/').pop();
const opts = (list, v) => list.map(x => `<option value="${esc(x.id)}"${x.id === v ? ' selected' : ''}>${esc(x.label)}</option>`).join('');

/* 页面状态：当前在编辑的命令或技能（null 表示显示列表） */
const st = {cmdScope: 'user', skScope: 'user', cmdEdit: null, skEdit: null, md: null};
const live = g => g === sj.gen() && sj.body() && sj.body().dataset.sec === 'persona';

function render() {
  const b = sj.body(), g = sj.gen();
  st.cmdEdit = st.skEdit = null;
  b.innerHTML = sj.head('个性化') +
    '<div class="ps-gh">风格和语气<span>对新开或重启的对话生效</span></div><div class="sj-grp ps-tone"><div class="sj-empty">读取中…</div></div>' +
    '<div class="ps-gh">全局指令<span class="ps-path"></span></div><div class="sj-grp ps-md"><div class="sj-empty">读取中…</div></div>' +
    '<div class="ps-gh">自定义命令</div><div class="sj-grp ps-cmd"></div>' +
    '<div class="ps-gh">技能</div><div class="sj-grp ps-sk"></div>';
  drawTone(g); drawMd(g); drawCmds(g); drawSkills(g);
}

/* ---------------- 风格和语气 ---------------- */
async function drawTone(g, data) {
  const box = sj.body().querySelector('.ps-tone');
  const r = data || await call('persona', {cwd: cwd()});
  if (!live(g)) return;
  if (r.error || !r.persona) { box.innerHTML = sj.errBox('读取失败：' + (r.error || ''), true); sj.bind(box, {}, {retry: () => drawTone(g)}); return; }
  const p = r.persona, styles = r.styles || [], cur = styles.find(x => x.id === (p.style || 'default'));
  let so = opts(styles.filter(x => !x.custom), p.style || 'default');
  const custom = styles.filter(x => x.custom);
  if (custom.length) so += `<optgroup label="自定义">${opts(custom, p.style)}</optgroup>`;
  if (p.style && !cur) so += `<option value="${esc(p.style)}" selected>${esc(p.style)}</option>`;
  box.innerHTML =
    sj.row('输出风格', `<select class="sj-sel" data-k="style" aria-label="输出风格">${so}</select>`, cur && cur.desc ? esc(cur.desc) : '') +
    sj.row('语气', `<select class="sj-sel" data-k="tone" aria-label="语气">${opts(r.tones || [], p.tone || 'default')}</select>`) +
    (r.switches || []).map(s => sj.row(esc(s.label), sj.sw(s.id, !!p[s.id], false, s.label))).join('') +
    sj.row('补充说明', '', '', 'sj-col') +
    `<div class="ps-ta-wrap"><textarea class="ps-ta ps-extra" rows="3" maxlength="4000" spellcheck="false" placeholder="比如：回答用中文；先说结论再说理由">${esc(p.extra || '')}</textarea>` +
    '<div class="ps-acts"><span class="ps-msg"></span><button type="button" class="sj-btn" data-a="extra" disabled>保存</button></div></div>';
  const ta = box.querySelector('.ps-extra'), sv = box.querySelector('[data-a="extra"]');
  const save = async (part, ctl) => {
    if (ctl) ctl.disabled = true;
    const x = await call('persona', {cwd: cwd(), set: part});
    if (!live(g)) return;
    if (x.error) { toast(x.error); drawTone(g); return; }
    drawTone(g, x);
    if ('extra' in part) { const m = sj.body().querySelector('.ps-tone .ps-msg'); if (m) { m.textContent = '已保存'; setTimeout(() => { m.textContent = ''; }, 1500); } }
  };
  const map = {style: v => save({style: v === 'default' ? null : v}), tone: v => save({tone: v === 'default' ? null : v})};
  for (const s of r.switches || []) map[s.id] = (v, btn) => save({[s.id]: v}, btn);
  sj.bind(box, map, {extra: () => save({extra: ta.value}, sv)});
  ta.oninput = () => { sv.disabled = ta.value.trim() === (p.extra || '').trim(); };
}

/* ---------------- 全局指令 ---------------- */
async function drawMd(g) {
  const box = sj.body().querySelector('.ps-md');
  const r = await call('claude-md', {});
  if (!live(g)) return;
  if (r.error) { box.innerHTML = sj.errBox('读取失败：' + r.error, true); sj.bind(box, {}, {retry: () => drawMd(g)}); return; }
  st.md = r;
  const path = sj.body().querySelector('.ps-path'); if (path) path.textContent = tilde(r.path);
  box.innerHTML = `<div class="ps-ta-wrap"><textarea class="ps-ta ps-mdta" rows="10" spellcheck="false" placeholder="写给 Claude 的长期指令，所有项目的对话都会读到">${esc(r.text || '')}</textarea>` +
    '<div class="ps-acts"><span class="ps-msg"></span><button type="button" class="sj-btn" data-a="reload">重新读取</button><button type="button" class="sj-btn sj-pri" data-a="save" disabled>保存</button></div></div>';
  const ta = box.querySelector('textarea'), sv = box.querySelector('[data-a="save"]'), msg = box.querySelector('.ps-msg');
  ta.oninput = () => { sv.disabled = ta.value === (st.md.text || ''); msg.textContent = ''; };
  sj.bind(box, {}, {
    reload: () => { if (ta.value !== (st.md.text || '') && !confirm('放弃没保存的修改，重新读取？')) return; drawMd(g); },
    save: async () => {
      sv.disabled = true;
      const x = await call('claude-md', {text: ta.value, mtime: st.md.mtime});
      if (!live(g)) return;
      if (x.error) { toast(x.error); sv.disabled = false; return; }
      st.md = x; msg.textContent = '已保存'; setTimeout(() => { msg.textContent = ''; }, 1500);
    },
  });
}

/* ---------------- 列表 + 编辑器（命令、技能共用） ---------------- */
const scopeSeg = (k, v) => {
  const has = !!cwd();
  return `<div class="ps-bar">${sj.seg(k, [['user', '全局'], ['project', has ? '本项目（' + base(cwd()) + '）' : '本项目']], v, '范围')}<span class="grow"></span>` +
    `<button type="button" class="sj-btn" data-a="new"${v === 'project' && !has ? ' disabled' : ''}>新建</button></div>`;
};
const field = (label, cls, val, ph, extra) => `<label class="ps-f"><span>${label}</span><input class="sj-in ${cls}" type="text" value="${esc(val || '')}" placeholder="${esc(ph || '')}" spellcheck="false" autocomplete="off"${extra || ''}></label>`;

async function drawCmds(g) {
  const box = sj.body().querySelector('.ps-cmd');
  if (st.cmdScope === 'project' && !cwd()) st.cmdScope = 'user';
  if (st.cmdEdit) return cmdEditor(g, box);
  box.innerHTML = scopeSeg('cscope', st.cmdScope) + '<div class="ps-list"><div class="sj-empty">读取中…</div></div>';
  sj.bind(box, {cscope: v => { st.cmdScope = v; drawCmds(g); }}, {new: () => { st.cmdEdit = {scope: st.cmdScope, isNew: true}; drawCmds(g); }});
  const r = await call('cmd-list', {cwd: cwd()});
  if (!live(g)) return;
  const list = box.querySelector('.ps-list');
  if (r.error) { list.innerHTML = sj.errBox('读取失败：' + r.error, true); sj.bind(list, {}, {retry: () => drawCmds(g)}); return; }
  const xs = r[st.cmdScope] || [];
  list.innerHTML = xs.length ? xs.map((c, i) => `<div class="ps-item" data-i="${i}"><div class="ps-main"><div class="ps-nm">/${esc(c.name)}${c.hint ? ` <span class="ps-hint">${esc(c.hint)}</span>` : ''}</div>` +
    `${c.desc ? `<div class="ps-ds">${esc(c.desc)}</div>` : ''}</div><button type="button" class="sj-btn" data-a="edit" data-i="${i}">编辑</button>` +
    `<button type="button" class="sj-btn ps-del" data-a="del" data-i="${i}">删除</button></div>`).join('')
    : '<div class="sj-empty">还没有命令</div>';
  sj.bind(list, {}, {
    edit: async (e, b) => {
      const c = xs[+b.dataset.i], d = await call('cmd-get', {path: c.path, cwd: cwd()});
      if (!live(g)) return;
      if (d.error) { toast(d.error); return drawCmds(g); }
      st.cmdEdit = d; drawCmds(g);
    },
    del: async (e, b) => {
      const c = xs[+b.dataset.i];
      if (!confirm(`删除命令 /${c.name}？\n文件会移到废纸篓。`)) return;
      const d = await call('cmd-delete', {path: c.path, cwd: cwd()});
      if (d.error) toast(d.error);
      if (live(g)) drawCmds(g);
    },
  });
}
function cmdEditor(g, box) {
  const c = st.cmdEdit;
  box.innerHTML = `<div class="ps-ed"><div class="ps-edh">${c.isNew ? '新建命令' : '编辑命令'}<span>${c.scope === 'project' ? '本项目' : '全局'}</span></div>` +
    field('命令名', 'ps-name', c.name, '比如 review，输入框里打 /review 调用', ' maxlength="64"') +
    field('说明', 'ps-desc', c.desc, '一句话，显示在 / 菜单里') +
    field('参数提示', 'ps-hint', c.hint, '可不填，比如 [文件路径]') +
    `<label class="ps-f ps-fcol"><span>内容</span><textarea class="ps-ta ps-body" rows="9" spellcheck="false" placeholder="发给 Claude 的提示词；$ARGUMENTS 会换成 / 命令后面跟的文字">${esc(c.body || '')}</textarea></label>` +
    '<div class="ps-acts"><span class="grow"></span><button type="button" class="sj-btn" data-a="cancel">取消</button><button type="button" class="sj-btn sj-pri" data-a="save">保存</button></div></div>';
  const q = s => box.querySelector(s);
  sj.bind(box, {}, {
    cancel: () => { st.cmdEdit = null; drawCmds(g); },
    save: async (e, b) => {
      b.disabled = true;
      const d = await call('cmd-save', {cwd: cwd(), scope: c.scope, path: c.isNew ? '' : c.path, mtime: c.mtime, name: q('.ps-name').value,
        desc: q('.ps-desc').value, hint: q('.ps-hint').value, body: q('.ps-body').value});
      if (!live(g)) return;
      b.disabled = false;
      if (d.error) { toast(d.error); return; }
      toast(`已保存 /${d.name}`);
      st.cmdEdit = null; st.cmdScope = c.scope; drawCmds(g);
    },
  });
  q('.ps-name').focus();
}

async function drawSkills(g) {
  const box = sj.body().querySelector('.ps-sk');
  if (st.skScope === 'project' && !cwd()) st.skScope = 'user';
  if (st.skEdit) return skEditor(g, box);
  box.innerHTML = scopeSeg('kscope', st.skScope) + '<div class="ps-list"><div class="sj-empty">读取中…</div></div>';
  sj.bind(box, {kscope: v => { st.skScope = v; drawSkills(g); }}, {new: () => { st.skEdit = {scope: st.skScope, isNew: true}; drawSkills(g); }});
  let r;
  try { r = await C.api('skills', {cwd: cwd()}); } catch (e) { r = {error: e.message}; }
  if (!live(g)) return;
  const list = box.querySelector('.ps-list');
  if (r.error) { list.innerHTML = sj.errBox('读取失败：' + r.error, true); sj.bind(list, {}, {retry: () => drawSkills(g)}); return; }
  const xs = r[st.skScope] || [];
  list.innerHTML = xs.length ? xs.map((s, i) => `<div class="ps-item"><div class="ps-main"><div class="ps-nm">${esc(s.name)}</div>${s.desc ? `<div class="ps-ds">${esc(s.desc)}</div>` : ''}</div>` +
    `<button type="button" class="sj-btn" data-a="edit" data-i="${i}">编辑</button><button type="button" class="sj-btn ps-del" data-a="del" data-i="${i}">删除</button></div>`).join('')
    : '<div class="sj-empty">还没有技能</div>';
  sj.bind(list, {}, {
    edit: async (e, b) => {
      const s = xs[+b.dataset.i], d = await call('skill-get', {dir: s.dir, cwd: cwd()});
      if (!live(g)) return;
      if (d.error) { toast(d.error); return drawSkills(g); }
      st.skEdit = d; drawSkills(g);
    },
    del: async (e, b) => {
      const s = xs[+b.dataset.i];
      if (!confirm(`删除技能「${s.name}」？\n整个技能文件夹会移到废纸篓。`)) return;
      const d = await call('skill-delete', {dir: s.dir, cwd: cwd()});
      if (d.error) toast(d.error);
      if (live(g)) drawSkills(g);
    },
  });
}
function skEditor(g, box) {
  const s = st.skEdit;
  box.innerHTML = `<div class="ps-ed"><div class="ps-edh">${s.isNew ? '新建技能' : '编辑技能'}<span>${s.scope === 'project' ? '本项目' : '全局'}</span></div>` +
    field('技能名', 'ps-name', s.name, '小写英文、数字、短横线，比如 weekly-report', ' maxlength="64"') +
    field('说明', 'ps-desc', s.desc, 'Claude 靠这句话判断什么时候用它，写清楚用途和触发场景', ' maxlength="1024"') +
    `<label class="ps-f ps-fcol"><span>内容</span><textarea class="ps-ta ps-body" rows="12" spellcheck="false" placeholder="用到这个技能时 Claude 要遵循的步骤和要求">${esc(s.body || '')}</textarea></label>` +
    '<div class="ps-acts"><span class="grow"></span><button type="button" class="sj-btn" data-a="cancel">取消</button><button type="button" class="sj-btn sj-pri" data-a="save">保存</button></div></div>';
  const q = x => box.querySelector(x);
  sj.bind(box, {}, {
    cancel: () => { st.skEdit = null; drawSkills(g); },
    save: async (e, b) => {
      b.disabled = true;
      const d = await call('skill-save', {cwd: cwd(), scope: s.scope, dir: s.isNew ? '' : s.dir, mtime: s.mtime, name: q('.ps-name').value.trim(),
        desc: q('.ps-desc').value, body: q('.ps-body').value});
      if (!live(g)) return;
      b.disabled = false;
      if (d.error) { toast(d.error); return; }
      toast(`已保存技能 ${d.name}`);
      st.skEdit = null; st.skScope = s.scope; drawSkills(g);
    },
  });
  q('.ps-name').focus();
}

/* 接入设置面板：放在“对话默认”后面 */
if (!sj.SECS.some(([k]) => k === 'persona')) {
  const i = sj.SECS.findIndex(([k]) => k === 'chat');
  sj.SECS.splice(i < 0 ? sj.SECS.length : i + 1, 0, ['persona', '个性化']);
}
sj.RENDER.persona = render;
const nav = document.querySelector('#sjSet .sj-nav');
if (nav && !nav.querySelector('[data-s="persona"]')) {
  const nb = document.createElement('button'); nb.type = 'button'; nb.dataset.s = 'persona'; nb.textContent = '个性化';
  const ch = nav.querySelector('[data-s="chat"]'); if (ch) ch.after(nb); else nav.appendChild(nb);
}
})();
