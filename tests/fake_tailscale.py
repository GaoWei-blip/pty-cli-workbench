"""第十六轮 R 测试用的假 tailscale 命令（不连任何网络、不碰真实 Tailscale）。

测试在临时目录里放一个叫 tailscale 的小脚本（exec 本文件），再把那个目录放到 PATH 最前面。状态目录由环境变量 FAKE_TS_DIR 指定：
  status.json  `tailscale status --json` 的输出；没有这个文件就模拟“没在运行”（退出码 1）
  serve.json   当前 serve 配置（ipn.ServeConfig 的 JSON），没有就是空配置 {}
  need_https   有这个文件时，`serve` 打印开启 HTTPS 的网址后直接退出 0、不改配置
               （模拟官方 enableFeatureInteractive 里 info.ShouldWait 为假时的 os.Exit(0)）
  calls.jsonl  每次调用的参数，一行一个 JSON 数组（测试据此检查命令拼装、从没调用过 funnel）
真命令的行为依据（见 NOTES-R.md）：serve status --json 原样输出 ServeConfig；Web 的键是“DNS名:端口”；
--set-path 时只撤这一个挂载点，否则撤这个端口上全部挂载点；端口上没有挂载点了就连 TCP 监听一起撤。
"""
import json
import os
import sys

D = os.environ.get('FAKE_TS_DIR') or '/tmp/fake-ts'
args = sys.argv[1:]
os.makedirs(D, exist_ok=True)
with open(os.path.join(D, 'calls.jsonl'), 'a', encoding='utf-8') as f:
    f.write(json.dumps(args, ensure_ascii=False) + '\n')


def read(name, dflt=None):
    try:
        with open(os.path.join(D, name), encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return dflt


def write(name, obj):
    with open(os.path.join(D, name), 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2)


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


if not args:
    die(2, 'usage')
if args[0] == 'version':
    print('1.90.2-fake')
    sys.exit(0)
if args[0] == 'funnel':
    die(5, 'fake: 测试里禁止调用 funnel')
st = read('status.json')
if args[0] == 'status':
    if st is None:
        die(1, 'failed to connect to local Tailscale daemon (fake)')
    print(json.dumps(st, indent=2))
    sys.exit(0)
if args[0] != 'serve':
    die(2, 'fake: 不支持的命令 ' + ' '.join(args))
if st is None:
    die(1, 'failed to connect to local Tailscale daemon (fake)')
rest = args[1:]
if rest[:1] == ['status']:
    print(json.dumps(read('serve.json', {}) or {}, indent=2))
    sys.exit(0)
bg, port, path, pos = False, 443, None, []
for a in rest:
    if a == '--bg' or a == '--bg=true':
        bg = True
    elif a.startswith('--https='):
        port = int(a.split('=', 1)[1])
    elif a.startswith('--set-path='):
        path = a.split('=', 1)[1] or '/'
    elif a.startswith('-'):
        die(2, 'fake: 不认识的参数 ' + a)
    else:
        pos.append(a)
host = (st.get('Self') or {}).get('DNSName', '').rstrip('.')
hp = '%s:%d' % (host, port)
sc = read('serve.json', {}) or {}
web = sc.setdefault('Web', {})
if pos[-1:] == ['off']:
    hs = (web.get(hp) or {}).get('Handlers') or {}
    mounts = [path] if path else list(hs)
    if not mounts or any(m not in hs for m in mounts):
        die(1, 'error: handler does not exist')
    for m in mounts:
        hs.pop(m)
    if not hs:
        web.pop(hp, None)
        (sc.get('TCP') or {}).pop(str(port), None)
        (sc.get('AllowFunnel') or {}).pop(hp, None)
    for k in ('Web', 'TCP', 'AllowFunnel'):
        if k in sc and not sc[k]:
            sc.pop(k)
    write('serve.json', sc)
    sys.exit(0)
if len(pos) != 1:
    die(2, 'fake: 需要一个目标')
if os.path.exists(os.path.join(D, 'need_https')):
    print('\nServe is not enabled on your tailnet.\nTo enable, visit:')
    print('\n         https://login.tailscale.com/f/serve?node=nFAKE123\n')
    sys.exit(0)
if not bg:
    die(3, 'fake: 测试里只允许 --bg（前台模式会一直挂着）')
m = path or '/'
if m in ((web.get(hp) or {}).get('Handlers') or {}):
    die(4, 'fake: 这个挂载点已经有规则（真命令会覆盖它，工作台不应该这样调用）')
if str(port) in (sc.get('TCP') or {}) and (sc['TCP'][str(port)].get('TCPForward')):
    die(1, 'cannot serve web; already serving TCP')
sc.setdefault('TCP', {})[str(port)] = {'HTTPS': True}
web.setdefault(hp, {}).setdefault('Handlers', {})[m] = {'Proxy': pos[0]}
write('serve.json', sc)
print('Available within your tailnet:\n\nhttps://%s%s/\n|-- proxy %s\n\nServe started and running in the background.' %
      (host, '' if port == 443 else ':%d' % port, pos[0]))
print('To disable the proxy, run: tailscale serve --https=%d off' % port)
