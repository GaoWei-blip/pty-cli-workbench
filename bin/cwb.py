"""第十三轮 G：在 Claude 工作台的对话里操作工作台界面。只用标准库。

  python3 ~/claude-workbench/bin/cwb.py open <路径> [--line N]          右栏预览打开文件，可跳到第 N 行
  python3 ~/claude-workbench/bin/cwb.py web <网址>                      右栏网页预览打开网址
  python3 ~/claude-workbench/bin/cwb.py shot <网址> [--out 路径] [--size 1280x800]
                                                                        给网址截图，输出 PNG 的路径

端口、令牌、对话 ID 从工作台给 CLI 设的环境变量（CWB_PORT、CWB_TOKEN、CWB_SID、CWB_GEN）读取。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

USAGE = '''用法：
  cwb.py open <路径> [--line N]
  cwb.py web <网址>
  cwb.py shot <网址> [--out 路径] [--size 1280x800]'''


def die(msg, code=1):
    sys.stderr.write(msg.rstrip() + '\n')
    sys.exit(code)


def parse(argv):
    if not argv or argv[0] in ('-h', '--help', 'help'):
        die(USAGE, 0 if argv else 2)
    cmd, pos, opt = argv[0], [], {}
    known = {'open': ('--line',), 'web': (), 'shot': ('--out', '--size')}
    if cmd not in known:
        die('未知命令：%s\n%s' % (cmd, USAGE), 2)
    i, a = 0, argv[1:]
    while i < len(a):
        x = a[i]
        if x.startswith('--'):
            k, eq, v = x.partition('=')
            if k not in known[cmd]:
                die('%s 不支持参数 %s\n%s' % (cmd, k, USAGE), 2)
            if not eq:
                i += 1
                if i >= len(a):
                    die('%s 缺少值' % k, 2)
                v = a[i]
            opt[k] = v
        else:
            pos.append(x)
        i += 1
    if len(pos) != 1:
        die(USAGE, 2)
    return cmd, pos[0], opt


def check_url(u):
    if not re.match(r'(?i)^https?://[^/\s]+', u or ''):
        die('只接受 http:// 或 https:// 开头的网址：%s' % u, 2)
    return u


def call(port, token, name, body, timeout):
    req = urllib.request.Request('http://127.0.0.1:%s/api/%s' % (port, name), data=json.dumps(body).encode('utf-8'),
                                 method='POST', headers={'X-Token': token, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        if e.code == 403:
            die('工作台不认这个令牌：工作台后台重启过，需要在工作台里重新打开这个对话')
        if e.code == 404:
            die('工作台版本太旧，不支持这个命令')
        try:
            msg = json.loads(e.read().decode('utf-8') or '{}').get('error')
        except Exception:
            msg = ''
        die(msg or '工作台返回错误 %d' % e.code)
    except (urllib.error.URLError, OSError) as e:
        die('连不上工作台后台服务（端口 %s）：%s' % (port, getattr(e, 'reason', e)))


def main(argv):
    port, token, sid = os.environ.get('CWB_PORT'), os.environ.get('CWB_TOKEN'), os.environ.get('CWB_SID')
    if not (port and token and sid):
        die('这个命令只能在 Claude 工作台启动的对话里使用（缺少 CWB_PORT、CWB_TOKEN、CWB_SID 环境变量）。', 3)
    cmd, arg, opt = parse(argv)
    base = {'sid': sid, 'gen': os.environ.get('CWB_GEN', '')}
    if cmd == 'open':
        path = os.path.abspath(os.path.expanduser(arg))
        if not os.path.exists(path):
            die('文件不存在：%s' % path, 2)
        line = 0
        if '--line' in opt:
            if not re.fullmatch(r'\d+', opt['--line']):
                die('行号要是正整数：%s' % opt['--line'], 2)
            line = int(opt['--line'])
        d = call(port, token, 'ui-open', dict(base, path=path, line=line), 15)
        print('已请求工作台在右栏打开：%s%s' % (d.get('path', path), '（第 %d 行）' % d['line'] if d.get('line') else ''))
    elif cmd == 'web':
        d = call(port, token, 'ui-web', dict(base, url=check_url(arg)), 15)
        print('已请求工作台在网页预览打开：%s' % d.get('url', arg))
    else:
        url = check_url(arg)
        w, h = 1280, 800
        if '--size' in opt:
            m = re.fullmatch(r'(\d{3,4})[x×*](\d{3,4})', opt['--size'].strip().lower())
            if not m:
                die('尺寸格式是 宽x高，例如 1280x800', 2)
            w, h = int(m.group(1)), int(m.group(2))
        out = os.path.abspath(os.path.expanduser(opt['--out'])) if opt.get('--out') else ''
        d = call(port, token, 'ui-shot', dict(base, url=url, w=w, h=h, out=out), 45)
        print(d['path'])


if __name__ == '__main__':
    main(sys.argv[1:])
