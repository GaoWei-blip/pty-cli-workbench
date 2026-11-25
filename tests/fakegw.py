"""第十三轮 E：测试用假网关（Anthropic Messages 接口的最小实现），只在本机回环地址监听，不转发任何请求。

用法：.venv/bin/python -I tests/fakegw.py <端口> <日志目录>
- 每个请求记一行到 <日志目录>/requests.jsonl：路径、User-Agent、x-app、模型、是否流式、是否带工具、最后一条用户消息开头
- 主对话请求（带 tools）按最后一条用户消息里的关键字决定回复：
    SLOWTEST[:秒数]  慢速流式返回成功回复（默认 30 秒），模拟回复进行中
    TOOLTEST         先调用 Bash 工具（需要授权），拿到工具结果后再回复
    FAKE[[<JSON 数组>]]END（可写多次）  第十五轮 A1：第 k 步回数组里的工具调用 [{"name":..,"input":{..}}]，步数用完后回 SAY[[文本]]END 或默认短句
    CACHETEST        第十五轮联调：回复的 usage 里带缓存读写的 token 数（状态行据此算出提示缓存还热着、何时过期）
    其他             立即回复
- 不带 tools 的旁路请求（标题等）立即回复一句短文本
- 第十四轮 I：日志另记请求体顶层键名（keys）、speed、service_tier 和 anthropic-beta 请求头；流式回复 usage 里带 speed（请求带 speed=fast 时为 fast）
- 第十五轮 A2（取自 r15 实测用的 fakegw2）：
    FAKE[[<JSON 数组>]]END   每出现一次算一步：最后一条带文字的用户消息之后已有 k 条回复时，第 k 步回数组里的 tool_use
                              （[{"name":..,"input":{..}}]，@PLANFILE@ 换成 CLI 在提示里给的计划文件路径）；步数用完回 SAY 或默认短句
    SAY[[文本]]END           最终文本
    压缩请求（最后一条消息里有 <summary>）回 <summary> 格式的假摘要；输入建议请求（SUGGESTION MODE）回空文本
  环境变量 FAKEGW_BODIES=1 时每个请求体另存 <日志目录>/bodies/NNNN.json
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18791
LOGDIR = sys.argv[2] if len(sys.argv) > 2 else '/tmp/cwb-e-gw'
os.makedirs(LOGDIR, exist_ok=True)
LOG = os.path.join(LOGDIR, 'requests.jsonl')
LK = threading.Lock()


def last_user(msgs):
    for m in reversed(msgs or []):
        if m.get('role') != 'user':
            continue
        c = m.get('content')
        if isinstance(c, str):
            return c, False
        texts, tool_result = [], False
        for b in c or []:
            if isinstance(b, dict):
                if b.get('type') == 'tool_result':
                    tool_result = True
                elif b.get('type') == 'text':
                    texts.append(b.get('text') or '')
        return '\n'.join(texts), tool_result
    return '', False


def strip_tags(t):
    return re.sub(r'<([a-z][\w-]*)>.*?</\1>', '', t, flags=re.S).strip()


def find_prompt(msgs):
    """最后一条带非空文字的用户消息（去掉 CLI 附带的标签块）和它之后的回复条数。"""
    msgs = msgs or []
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get('role') != 'user':
            continue
        c = m.get('content')
        t = strip_tags(c if isinstance(c, str) else '\n'.join(
            b.get('text') or '' for b in c or [] if isinstance(b, dict) and b.get('type') == 'text'))
        if t:
            return t, sum(1 for x in msgs[i + 1:] if x.get('role') == 'assistant')
    return '', 0


SEQ = [0]


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _rec(self, extra):
        rec = {'t': time.strftime('%H:%M:%S'), 'method': self.command, 'path': self.path,
               'ua': self.headers.get('User-Agent', ''), 'x_app': self.headers.get('x-app', '')}
        rec.update(extra)
        with LK, open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    def do_GET(self):
        self._rec({})
        self._json({'data': [], 'has_more': False})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        try:
            req = json.loads(raw or b'{}')
        except Exception:
            req = {}
        if 'count_tokens' in self.path:
            self._rec({'kind': 'count_tokens'})
            return self._json({'input_tokens': 100})
        if '/v1/messages' not in self.path:
            self._rec({'kind': 'other'})
            return self._json({})
        text, tool_result = last_user(req.get('messages'))
        text = re.sub(r'<([a-z][\w-]*)>.*?</\1>', '', text, flags=re.S).strip()  # CLI 附带的提醒、命令块不算用户的话
        has_tools = bool(req.get('tools'))
        if has_tools and self._fake(req):  # 第十五轮 A1：FAKE[[工具调用]]END 按步回工具调用
            return
        # 第十四轮 I：另记请求体顶层键名、speed / service_tier 之类字段的值和 anthropic-beta 请求头（Fast 实测用）
        self._rec({'kind': 'messages', 'model': req.get('model'), 'stream': bool(req.get('stream')), 'tools': has_tools,
                   'tool_result': tool_result, 'last': text[-80:].replace('\n', ' '), 'len': len(text),
                   'keys': sorted(req), 'speed': req.get('speed'), 'service_tier': req.get('service_tier'),
                   'beta': self.headers.get('anthropic-beta', ''),
                   'effort': (req.get('output_config') or {}).get('effort') if isinstance(req.get('output_config'), dict) else None})
        if os.environ.get('FAKEGW_BODIES') == '1':
            with LK:
                SEQ[0] += 1
                seq = SEQ[0]
            os.makedirs(os.path.join(LOGDIR, 'bodies'), exist_ok=True)
            with open(os.path.join(LOGDIR, 'bodies', f'{seq:04d}.json'), 'wb') as f:
                f.write(raw)
        ptext, k = find_prompt(req.get('messages'))
        steps = re.findall(r'FAKE\[\[(.*?)\]\]END', ptext, flags=re.S) if has_tools else []
        say = re.search(r'SAY\[\[(.*?)\]\]END', ptext, flags=re.S) if has_tools else None
        msgs = req.get('messages') or [{}]
        lastmsg = json.dumps(msgs[-1], ensure_ascii=False)
        lastuser = json.dumps(next((m for m in reversed(msgs) if m.get('role') == 'user'), {}), ensure_ascii=False)
        tools, mode, secs, parts = None, 'quick', 0, None
        # 压缩请求：摘要指令在最后一条用户消息里（2.1.321 之后还跟一条 system 角色的日期消息），也可能和后台任务通知合在一条里；
        # 只带 <summary> 字样的任务通知不算
        if has_tools and any('<summary>' in s and 'Tool calls will be rejected' in s for s in (lastmsg, lastuser)):
            parts = ['<analysis>假分析。</analysis>\n\n<summary>\n这是假网关给的压缩摘要：前面几轮的要点。\n</summary>']
        elif has_tools and 'SUGGESTION MODE' in ptext:
            parts = ['跑一下测试']  # 像真实模型那样给一句输入建议（它也会触发 agent_type 为空的 SubagentStop）
        elif steps or say:
            if k < len(steps):
                m = re.search(r'(/[^\s"\\]+/\.claude/plans/[^\s"\\]+?\.md)', json.dumps(req.get('messages'), ensure_ascii=False))
                try:
                    tools = json.loads(steps[k].replace('@PLANFILE@', m.group(1) if m else '/tmp/cwb-noplan.md'))
                    mode = 'tool'
                except Exception as e:
                    parts = [f'FAKE 解析失败：{e}']
            else:
                parts = [say.group(1) if say else ('工具已经执行完毕，结果已收到。' if tool_result else '收到。')]
        elif has_tools and not tool_result:
            if 'SLOWTEST' in text:
                mode, secs = 'slow', 30
                try:
                    secs = int(text.split('SLOWTEST:', 1)[1].split()[0])
                except Exception:
                    pass
            elif 'TOOLTEST' in text:
                mode = 'tool'
        if parts is not None:
            pass
        elif not has_tools:
            parts = ['测试标题']
        elif tool_result:
            parts = ['工具已经执行完毕，结果已收到。']
        elif mode == 'slow':
            parts = [f'第{i}段：这是慢速流式回复的内容，用来模拟回复进行中。\n\n' for i in range(1, 41)]
        else:
            parts = ['收到：' + (text.strip().splitlines() or [''])[0][:40] + f'（共 {len(text)} 字）']
        if not req.get('stream'):
            content = [{'type': 'tool_use', 'id': 'toolu_' + uuid.uuid4().hex[:20], 'name': x['name'], 'input': x['input']}
                       for x in tools] if tools else [{'type': 'text', 'text': ''.join(parts or [])}]
            return self._json({'id': 'msg_' + uuid.uuid4().hex[:20], 'type': 'message', 'role': 'assistant',
                               'model': req.get('model') or 'fake', 'content': content,
                               'stop_reason': 'tool_use' if tools else 'end_turn', 'stop_sequence': None,
                               'usage': {'input_tokens': 100, 'output_tokens': 20, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        def ev(name, data):
            self.wfile.write(f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode())
            self.wfile.flush()
        try:
            ev('message_start', {'type': 'message_start', 'message': {
                'id': 'msg_' + uuid.uuid4().hex[:20], 'type': 'message', 'role': 'assistant', 'model': req.get('model') or 'fake',
                'content': [], 'stop_reason': None, 'stop_sequence': None,
                'usage': dict({'input_tokens': 100, 'output_tokens': 1, 'cache_creation_input_tokens': 2000 if 'CACHETEST' in text else 0,
                               'cache_read_input_tokens': 150000 if 'CACHETEST' in text else 0},
                              speed='fast' if req.get('speed') == 'fast' else 'standard')}})
            if tools:
                for i, x in enumerate(tools):
                    ev('content_block_start', {'type': 'content_block_start', 'index': i, 'content_block': {
                        'type': 'tool_use', 'id': 'toolu_' + uuid.uuid4().hex[:20], 'name': x['name'], 'input': {}}})
                    ev('content_block_delta', {'type': 'content_block_delta', 'index': i, 'delta': {
                        'type': 'input_json_delta', 'partial_json': json.dumps(x['input'], ensure_ascii=False)}})
                    ev('content_block_stop', {'type': 'content_block_stop', 'index': i})
                ev('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 30}})
                ev('message_stop', {'type': 'message_stop'})
                return
            if mode == 'tool':
                ev('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {
                    'type': 'tool_use', 'id': 'toolu_' + uuid.uuid4().hex[:20], 'name': 'Bash', 'input': {}}})
                inp = json.dumps({'command': 'echo cwb-perm-ok > perm_ok.txt', 'description': '写入测试文件'})
                ev('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': inp}})
                ev('content_block_stop', {'type': 'content_block_stop', 'index': 0})
                ev('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 30}})
                ev('message_stop', {'type': 'message_stop'})
                return
            ev('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})
            gap = secs / max(1, len(parts)) if mode == 'slow' else 0
            for p in parts:
                if gap:
                    time.sleep(gap)
                    ev('ping', {'type': 'ping'})
                ev('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': p}})
            ev('content_block_stop', {'type': 'content_block_stop', 'index': 0})
            ev('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 50}})
            ev('message_stop', {'type': 'message_stop'})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _fake(self, req):
        """第十五轮 A1：最后一条带文字的用户消息里写 FAKE[[<JSON 数组>]]END（可写多次，每次算一步），
        第 k 步回数组里的工具调用 [{"name":..,"input":{..}}]；步数用完后回 SAY[[文本]]END 或默认短句。不含 FAKE 的返回 False。"""
        msgs = req.get('messages') or []
        k, text = 0, ''
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m.get('role') != 'user':
                continue
            c = m.get('content')
            if not isinstance(c, str) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c or []):
                continue  # 第十七轮 M：工具结果那条（ToolSearch 会附一句 “Tool loaded.”）不算提问
            t = c if isinstance(c, str) else '\n'.join(b.get('text') or '' for b in c or [] if isinstance(b, dict) and b.get('type') == 'text')
            t = re.sub(r'<([a-z][\w-]*)>.*?</\1>', '', t, flags=re.S).strip()
            if t:
                text, k = t, sum(1 for x in msgs[i + 1:] if x.get('role') == 'assistant')
                break
        steps = re.findall(r'FAKE\[\[(.*?)\]\]END', text, flags=re.S)
        if not steps:
            return False
        say = re.search(r'SAY\[\[(.*?)\]\]END', text, flags=re.S)
        tools, parts = None, None
        if k < len(steps):
            try:
                # 第十五轮联调：合并后 FAKE 都先走这里，A2 的 @PLANFILE@（CLI 在提示里给的计划文件路径）也要在这里换
                m = re.search(r'(/[^\s"\\]+/\.claude/plans/[^\s"\\]+?\.md)', json.dumps(msgs, ensure_ascii=False)) if '@PLANFILE@' in steps[k] else None
                tools = json.loads(steps[k].replace('@PLANFILE@', m.group(1) if m else '/tmp/cwb-noplan.md'))
            except Exception as e:
                parts = [f'FAKE 解析失败：{e}']
        else:
            parts = [say.group(1) if say else '按步骤做完了。']
        self._rec({'kind': 'messages', 'fake_step': k, 'reply': 'tool_use' if tools else 'text', 'last': text[-80:].replace('\n', ' ')})
        mid = 'msg_' + uuid.uuid4().hex[:20]
        usage = {'input_tokens': 1200, 'output_tokens': 1, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        def ev(name, data):
            self.wfile.write(f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode())
            self.wfile.flush()
        try:
            ev('message_start', {'type': 'message_start', 'message': {
                'id': mid, 'type': 'message', 'role': 'assistant', 'model': req.get('model') or 'fake',
                'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': usage}})
            if tools:
                for i, t in enumerate(tools):
                    ev('content_block_start', {'type': 'content_block_start', 'index': i, 'content_block': {
                        'type': 'tool_use', 'id': 'toolu_' + uuid.uuid4().hex[:20], 'name': t['name'], 'input': {}}})
                    ev('content_block_delta', {'type': 'content_block_delta', 'index': i, 'delta': {
                        'type': 'input_json_delta', 'partial_json': json.dumps(t['input'], ensure_ascii=False)}})
                    ev('content_block_stop', {'type': 'content_block_stop', 'index': i})
                ev('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 30}})
            else:
                ev('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})
                ev('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': ''.join(parts)}})
                ev('content_block_stop', {'type': 'content_block_stop', 'index': 0})
                ev('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 50}})
            ev('message_stop', {'type': 'message_stop'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    srv.daemon_threads = True
    print('假网关', PORT, LOG, flush=True)
    srv.serve_forever()
