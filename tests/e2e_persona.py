"""11-18 个性化端到端：测试服务 + 假网关（存请求体）+ 真实 claude，只在 /tmp/cwb-ps-e2e 新建对话；
界面用 agent-browser 检查设置页。不保存全局指令、不写全局命令和技能（只用“本项目”范围）。"""
import glob, json, os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
os.environ.update(CWB_E2E_PORT='47962', CWB_E2E_GW='18812', CWB_E2E_PROJ='/tmp/cwb-ps-e2e',
                  CWB_E2E_STATE='/tmp/cwb-ps-exp/state.json', CWB_E2E_GWLOG='/tmp/cwb-ps-gw/requests.jsonl')
import e2e_lib as E
import e2e_r15_g as G
G.SESSION = 'ps'
ab, ev, wait = G.ab, G.ev, G.wait
os.makedirs('/tmp/cwb-ps-exp', exist_ok=True); os.makedirs(E.PROJ, exist_ok=True)
PY = os.path.join(E.ROOT, '.venv/bin/python')
pids, sid = {}, None
try:
    lg = open('/tmp/cwb-ps-exp/gw.log', 'ab')
    pids['gw'] = subprocess.Popen([PY, '-I', os.path.join(E.ROOT, 'tests/fakegw.py'), str(E.GW), '/tmp/cwb-ps-gw'], cwd=E.ROOT, env=dict(os.environ, FAKEGW_BODIES='1'),
                                  stdin=subprocess.DEVNULL, stdout=lg, stderr=lg, start_new_session=True).pid
    pids['server'] = E.start_server(CWB_TITLE_DELAY='99999')
    r = E.api('persona', {'set': {'style': 'Concise', 'tone': 'candid', 'structure': True, 'extra': 'PSMARK 先说结论'}})
    E.check(r.get('persona', {}).get('tone') == 'candid', '接口保存风格和语气')
    n0 = len(E.gw_rows())
    sid = E.new_session()
    E.api('send', {'sid': sid, 'text': '你好 PSRUN'})
    E.wait_until(lambda: any('PSRUN' in (x.get('last') or '') for x in E.gw_rows()[n0:]), 40)
    time.sleep(1)
    bodies = []
    for f in sorted(glob.glob('/tmp/cwb-ps-gw/bodies/*.json')):
        b = json.load(open(f))
        if b.get('tools') and 'PSRUN' in json.dumps(b.get('messages'), ensure_ascii=False): bodies.append((json.dumps(b.get('system'), ensure_ascii=False), json.dumps(b.get('messages'), ensure_ascii=False)))
    E.check(bodies and all('PSMARK 先说结论' in s and '直接指出' in s and '小标题和列表' in s for s, _ in bodies), '附加提示词进了系统提示')
    E.check(bodies and all('Output Style: Concise' in m for _, m in bodies), '输出风格生效（CLI 放在消息里）')
    E.ua_check(n0)
    # 界面
    E.api('persona', {'set': {'style': None, 'tone': None, 'structure': False, 'extra': ''}})
    ab('set', 'viewport', '1280', '860')
    ab('open', 'http://127.0.0.1:%d/?t=%s' % (E.PORT, E.token()))
    wait('window.CWB && CWB.S && CWB.openSettings', 20)
    ev("CWB.openSession(%s); return 1;" % json.dumps(sid)); wait("CWB.S.cur===%s" % json.dumps(sid), 15); time.sleep(1)
    ev("CWB.openSettings('persona'); return 1;")
    E.check(wait("document.querySelector('#sjSet .sj-nav [data-s=persona].on') && document.querySelector('.ps-tone select[data-k=tone]') && document.querySelector('.ps-mdta') && document.querySelector('.ps-cmd .ps-bar')", 15), '“个性化”页渲染出四块')
    nav = ev("return [...document.querySelectorAll('#sjSet .sj-nav [data-s]')].map(b=>b.textContent);")
    E.check(nav[2:4] == ['对话默认', '个性化'] if isinstance(nav, list) else False, f'导航位置：{nav}')
    md = ev("return {len: document.querySelector('.ps-mdta').value.length, dis: document.querySelector('.ps-md [data-a=save]').disabled};")
    E.check(md.get('len', 0) > 0 and md.get('dis') is True, f'全局指令读出、未修改时保存按钮不可点（{md}）')
    ev("const s=document.querySelector('.ps-tone select[data-k=tone]'); s.value='roast'; s.dispatchEvent(new Event('change')); return 1;")
    E.check(E.wait_until(lambda: E.api('persona', {}).get('persona', {}).get('tone') == 'roast', 8), '界面改语气已保存')
    ev("document.querySelector('.ps-tone .sj-sw[data-k=emoji]').click(); return 1;")
    E.check(E.wait_until(lambda: E.api('persona', {}).get('persona', {}).get('emoji') is True, 8), '界面开关已保存')
    # 项目命令：新建
    ev("document.querySelector('.ps-cmd .sj-seg button[data-v=project]').click(); return 1;"); time.sleep(0.6)
    ev("document.querySelector('.ps-cmd [data-a=new]').click(); return 1;"); time.sleep(0.4)
    ev("const b=document.querySelector('.ps-cmd'); b.querySelector('.ps-name').value='pstest'; b.querySelector('.ps-desc').value='测试命令'; b.querySelector('.ps-body').value='说 $ARGUMENTS'; b.querySelector('[data-a=save]').click(); return 1;")
    E.check(wait("[...document.querySelectorAll('.ps-cmd .ps-nm')].some(x=>x.textContent.includes('/pstest'))", 8), '新建项目命令后出现在列表')
    cmds = E.api('commands', {'cwd': E.PROJ}).get('commands', [])
    E.check(any(c['name'] == 'pstest' and c['desc'] == '测试命令' for c in cmds), '“/”菜单里有新命令')
    # 项目技能：新建、编辑
    ev("document.querySelector('.ps-sk .sj-seg button[data-v=project]').click(); return 1;"); time.sleep(0.6)
    ev("document.querySelector('.ps-sk [data-a=new]').click(); return 1;"); time.sleep(0.4)
    ev("const b=document.querySelector('.ps-sk'); b.querySelector('.ps-name').value='ps-skill'; b.querySelector('.ps-desc').value='测试技能'; b.querySelector('.ps-body').value='步骤'; b.querySelector('[data-a=save]').click(); return 1;")
    E.check(wait("[...document.querySelectorAll('.ps-sk .ps-nm')].some(x=>x.textContent==='ps-skill')", 8), '新建项目技能后出现在列表')
    ev("[...document.querySelectorAll('.ps-sk .ps-item')].find(x=>x.textContent.includes('ps-skill')).querySelector('[data-a=edit]').click(); return 1;")
    E.check(wait("document.querySelector('.ps-sk .ps-desc') && document.querySelector('.ps-sk .ps-desc').value==='测试技能'", 8), '编辑技能读回内容')
    ev("const b=document.querySelector('.ps-sk'); b.querySelector('.ps-desc').value='改过的说明'; b.querySelector('[data-a=save]').click(); return 1;")
    E.check(wait("[...document.querySelectorAll('.ps-sk .ps-ds')].some(x=>x.textContent==='改过的说明')", 8), '技能修改已保存')
    lay = ev("const b=document.querySelector('#sjSet .sj-body'); return {sw: b.scrollWidth - b.clientWidth};")
    E.check(lay.get('sw', 1) <= 0, '设置页没有横向滚动')
    E.check(os.path.isfile(os.path.join(E.PROJ, '.claude/commands/pstest.md')) and os.path.isfile(os.path.join(E.PROJ, '.claude/skills/ps-skill/SKILL.md')), '文件落在项目文件夹里')
finally:
    ab('close')
    try: E.api('persona', {'set': {'style': None, 'tone': None, 'structure': False, 'emoji': False, 'brief': False, 'extra': ''}})
    except Exception: pass
    if sid:
        try: E.api('delete', {'sids': [sid]})
        except Exception: pass
    E.stop_server()
    if pids.get('gw'): G.kill_pid(pids['gw'], 'fakegw.py')
print('\n全部通过' if not E.FAIL else f'\n失败 {len(E.FAIL)} 项：' + '；'.join(E.FAIL))
