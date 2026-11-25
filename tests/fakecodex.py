"""第十六轮 P：测试用假 Responses 后端（Codex 的 wire_api = "responses"），只在本机回环地址监听，不转发任何请求。

用法：.venv/bin/python -I tests/fakecodex.py <端口> <日志目录>
- 每个请求记一行到 <日志目录>/requests.jsonl：路径、模型、reasoning、工具名、最后一条输入的类型和开头文字
- 环境变量 FAKECODEX_BODIES=1 时每个请求体另存 <日志目录>/bodies/NNNN.json
- POST /v1/responses 按最后一条用户消息里的关键字决定回复（流式 SSE）：
    CMD[[命令]]END     第一步调用执行命令的工具（按请求里给的工具名：exec_command / shell / local_shell），拿到结果后回“命令完成”
    PATCH[[文件名]]END 第一步用 apply_patch 新建这个文件（内容 hello），拿到结果后回“改好了”
    MCP[[工具名|JSON 参数]]END  第十七轮 M：第一步调用名字以这个结尾的 MCP 工具（如 delegate_task），拿到结果后回“命令完成”
    SLOW[:秒数]        慢速流式输出（默认 20 秒，每 0.25 秒一段），用来测中断
    THINK              先给一段推理摘要再回复
    FAIL               返回 500
    其他               立即回复“收到：<前 20 字>”
  回复的 usage 里带 input/cached/output/reasoning token 数（固定的假数字），首段输出前等 0.3 秒（测首字时间）
- GET /v1/models 返回两个假模型
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



def app_server():
    """第十七轮 U：假的 `codex app-server`（stdio JSON-RPC），只回额度相关的几个请求，测 quota_hub 的临时进程。
    用法：fakecodex.py app-server [任意参数…]；环境变量：
      FAKECX_RL=ok（默认）| unsupported（-32600）| fail（-32603）| slow（不回）
      FAKECX_PROVIDER=custom 时 config/read 返回这个 model_provider（测“走代理”）
      FAKECX_LOG=文件  每次启动和每个请求记一行（argv、method）"""
    logf = os.environ.get('FAKECX_LOG')

    def note(obj):
        if logf:
            with open(logf, 'a', encoding='utf-8') as f:
                f.write(json.dumps(obj, ensure_ascii=False) + '\n')
    note({'start': sys.argv[1:], 'pid': os.getpid()})
    mode = os.environ.get('FAKECX_RL') or 'ok'
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except Exception:
            continue
        if 'id' not in m:
            continue
        meth = m.get('method')
        note({'method': meth})
        res, err = None, None
        if meth == 'initialize':
            res = {'userAgent': 'fake'}
        elif meth == 'config/read':
            res = {'config': {'model_provider': os.environ.get('FAKECX_PROVIDER') or None}}
        elif meth == 'account/rateLimits/read':
            if mode == 'unsupported':
                err = {'code': -32600, 'message': 'codex account authentication required to read rate limits'}
            elif mode == 'fail':
                err = {'code': -32603, 'message': 'internal error'}
            elif mode == 'slow':
                continue
            else:
                res = {'rateLimits': {'limitId': 'codex', 'planType': 'pro',
                                      'primary': {'usedPercent': 42, 'windowDurationMins': 300, 'resetsAt': int(time.time()) + 3600},
                                      'secondary': {'usedPercent': 18, 'windowDurationMins': 10080, 'resetsAt': int(time.time()) + 5 * 86400},
                                      'credits': {'hasCredits': False, 'unlimited': False, 'balance': '0'}},
                       'rateLimitsByLimitId': {'gpt-x': {'limitId': 'gpt-x', 'primary': {'usedPercent': 99, 'windowDurationMins': 300}}},
                       'rateLimitResetCredits': {'availableCount': 1, 'credits': [{'status': 'available', 'expiresAt': int(time.time()) + 9 * 86400}]}}
        else:
            err = {'code': -32601, 'message': 'not supported'}
        msg = {'jsonrpc': '2.0', 'id': m['id']}
        msg.update({'error': err} if err else {'result': res})
        sys.stdout.write(json.dumps(msg) + '\n')
        sys.stdout.flush()


if len(sys.argv) > 1 and sys.argv[1] == 'app-server':  # 第十七轮 U
    app_server()
    sys.exit(0)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18832
LOGDIR = sys.argv[2] if len(sys.argv) > 2 else '/tmp/cwb-p-codex'
os.makedirs(LOGDIR, exist_ok=True)
LOG = os.path.join(LOGDIR, 'requests.jsonl')
LK = threading.Lock()
N = [0]


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if isinstance(c, dict) and c.get('type') in ('input_text', 'output_text', 'text'):
            out.append(c.get('text') or '')
    return '\n'.join(out)


def last_user_text(inp):
    for it in reversed(inp or []):
        if isinstance(it, dict) and it.get('role') == 'user' and it.get('type', 'message') == 'message':
            t = text_of(it.get('content'))
            # Codex 会在用户消息前插入环境说明（<environment_context> 等），跳过纯标签消息
            if t.strip() and not t.strip().startswith('<'):
                return t, any(isinstance(c, dict) and c.get('type') == 'input_image' for c in it.get('content') or [])
    return '', False


def tool_names(body):
    out = []
    for t in body.get('tools') or []:
        if isinstance(t, dict):
            out.append(t.get('name') or (t.get('function') or {}).get('name') or t.get('type'))
            if t.get('type') == 'namespace':  # 第十七轮 M：Codex 把 MCP 工具放在命名空间里（mcp__cwb），记成“命名空间.工具名”
                out += [t['name'] + '.' + x.get('name') for x in t.get('tools') or [] if isinstance(x, dict) and x.get('name')]
    return out


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip('/').endswith('/models'):
            return self._json(200, {'object': 'list', 'data': [{'id': 'fake-model', 'object': 'model'}, {'id': 'fake-mini', 'object': 'model'}]})
        return self._json(404, {})

    def ev(self, typ, **kw):
        d = dict(type=typ, **kw)
        self.wfile.write(('event: %s\ndata: %s\n\n' % (typ, json.dumps(d, ensure_ascii=False))).encode())
        self.wfile.flush()

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            body = json.loads(raw or b'{}')
        except Exception:
            body = {}
        with LK:
            N[0] += 1
            k = N[0]
        inp = body.get('input') or []
        last = inp[-1] if inp else {}
        txt, has_img = last_user_text(inp)
        rec = {'n': k, 't': time.time(), 'path': self.path, 'model': body.get('model'), 'reasoning': body.get('reasoning'),
               'stream': body.get('stream'), 'tools': tool_names(body), 'last_type': last.get('type'), 'user': txt[:200],
               'img': has_img, 'ua': self.headers.get('User-Agent'), 'auth': bool(self.headers.get('Authorization'))}
        with LK:
            with open(LOG, 'a') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            if os.environ.get('FAKECODEX_BODIES') == '1':
                os.makedirs(os.path.join(LOGDIR, 'bodies'), exist_ok=True)
                with open(os.path.join(LOGDIR, 'bodies', '%04d.json' % k), 'wb') as f:
                    f.write(raw)
        if not self.path.rstrip('/').endswith('/responses'):
            return self._json(404, {})
        if 'FAIL' in txt and last.get('type') in (None, 'message'):
            return self._json(500, {'error': {'message': 'fake failure', 'type': 'server_error'}})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        rid = 'resp_' + uuid.uuid4().hex[:12]
        self.ev('response.created', response={'id': rid, 'object': 'response', 'status': 'in_progress', 'output': []})
        time.sleep(0.3)
        out_items = []
        tools = tool_names(body)
        after_tool = last.get('type') in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output')
        m_cmd = re.search(r'CMD\[\[(.*?)\]\]END', txt, re.S)
        m_patch = re.search(r'PATCH\[\[(.*?)\]\]END', txt, re.S)
        m_mcp = re.search(r'MCP\[\[(\w+)\|(.*?)\]\]END', txt, re.S)  # 第十七轮 M：调 MCP 工具（按名字结尾在请求的工具里找）
        try:
            mcp_name = next((x for x in tools if m_mcp and x and (x == m_mcp.group(1) or x.endswith('__' + m_mcp.group(1)) or x.endswith('.' + m_mcp.group(1)))), None)
            if not after_tool and m_mcp and mcp_name:
                ns, _, nm = mcp_name.rpartition('.')
                item = {'type': 'function_call', 'id': 'fc_' + uuid.uuid4().hex[:8], 'call_id': 'call_' + uuid.uuid4().hex[:8],
                        'name': nm, 'arguments': m_mcp.group(2), 'status': 'completed'}
                if ns:
                    item['namespace'] = ns
                self.ev('response.output_item.added', output_index=0, item=dict(item, arguments=''))
                self.ev('response.output_item.done', output_index=0, item=item)
                out_items.append(item)
            elif not after_tool and m_cmd:
                cmd = m_cmd.group(1)
                name = next((x for x in ('exec_command', 'shell_command', 'shell') if x in tools), 'shell')
                if name == 'exec_command':
                    args = {'cmd': cmd}
                elif name == 'shell_command':
                    args = {'command': cmd}
                else:
                    args = {'command': ['bash', '-lc', cmd]}
                item = {'type': 'function_call', 'id': 'fc_' + uuid.uuid4().hex[:8], 'call_id': 'call_' + uuid.uuid4().hex[:8],
                        'name': name, 'arguments': json.dumps(args), 'status': 'completed'}
                self.ev('response.output_item.added', output_index=0, item=dict(item, arguments=''))
                self.ev('response.output_item.done', output_index=0, item=item)
                out_items.append(item)
            elif not after_tool and m_patch:
                fn = m_patch.group(1).strip()
                patch = '*** Begin Patch\n*** Add File: %s\n+hello\n*** End Patch\n' % fn
                if 'apply_patch' in tools and any((t.get('type') == 'custom') for t in body.get('tools') or [] if isinstance(t, dict) and t.get('name') == 'apply_patch'):
                    item = {'type': 'custom_tool_call', 'id': 'ct_' + uuid.uuid4().hex[:8], 'call_id': 'call_' + uuid.uuid4().hex[:8],
                            'name': 'apply_patch', 'input': patch, 'status': 'completed'}
                else:
                    item = {'type': 'function_call', 'id': 'fc_' + uuid.uuid4().hex[:8], 'call_id': 'call_' + uuid.uuid4().hex[:8],
                            'name': 'apply_patch', 'arguments': json.dumps({'input': patch}), 'status': 'completed'}
                self.ev('response.output_item.added', output_index=0, item=item)
                self.ev('response.output_item.done', output_index=0, item=item)
                out_items.append(item)
            else:
                idx = 0
                if 'THINK' in txt and not after_tool:
                    rs = {'type': 'reasoning', 'id': 'rs_' + uuid.uuid4().hex[:8], 'summary': []}
                    self.ev('response.output_item.added', output_index=idx, item=rs)
                    self.ev('response.reasoning_summary_part.added', item_id=rs['id'], output_index=idx, summary_index=0, part={'type': 'summary_text', 'text': ''})
                    for piece in ['**先想一想**\n\n', '用户要我', '回复。']:
                        self.ev('response.reasoning_summary_text.delta', item_id=rs['id'], output_index=idx, summary_index=0, delta=piece)
                        time.sleep(0.05)
                    full = '**先想一想**\n\n用户要我回复。'
                    self.ev('response.reasoning_summary_text.done', item_id=rs['id'], output_index=idx, summary_index=0, text=full)
                    rs = dict(rs, summary=[{'type': 'summary_text', 'text': full}])
                    self.ev('response.output_item.done', output_index=idx, item=rs)
                    out_items.append(rs)
                    idx += 1
                mid = 'msg_' + uuid.uuid4().hex[:8]
                msg = {'type': 'message', 'id': mid, 'role': 'assistant', 'status': 'in_progress', 'content': []}
                self.ev('response.output_item.added', output_index=idx, item=msg)
                self.ev('response.content_part.added', item_id=mid, output_index=idx, content_index=0, part={'type': 'output_text', 'text': ''})
                m_slow = re.search(r'SLOW(?::(\d+))?', txt)
                if after_tool:
                    pieces = ['命令完成' if not (m_patch and not m_cmd) else '改好了']
                elif m_slow:
                    secs = int(m_slow.group(1) or 20)
                    pieces = ['第 %d 段。' % i for i in range(int(secs / 0.25))]
                else:
                    pieces = ['收到：', txt[:20]]
                full = ''
                for p in pieces:
                    full += p
                    self.ev('response.output_text.delta', item_id=mid, output_index=idx, content_index=0, delta=p)
                    if m_slow and not after_tool:
                        time.sleep(0.25)
                self.ev('response.output_text.done', item_id=mid, output_index=idx, content_index=0, text=full)
                msg = dict(msg, status='completed', content=[{'type': 'output_text', 'text': full, 'annotations': []}])
                self.ev('response.output_item.done', output_index=idx, item=msg)
                out_items.append(msg)
            usage = {'input_tokens': 1200, 'input_tokens_details': {'cached_tokens': 800}, 'output_tokens': 34,
                     'output_tokens_details': {'reasoning_tokens': 12}, 'total_tokens': 1234}
            self.ev('response.completed', response={'id': rid, 'object': 'response', 'status': 'completed', 'output': out_items, 'usage': usage})
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    srv.daemon_threads = True
    print('fakecodex listening', PORT, flush=True)
    srv.serve_forever()
