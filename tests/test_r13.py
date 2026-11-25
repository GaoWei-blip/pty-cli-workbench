"""第十三轮（开发线 E）单元测试：终端守护进程 ptyd.py。不启动 claude，用小脚本代替 CLI；运行目录用临时目录。
运行：.venv/bin/python -I tests/test_r13.py
"""
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import pyte  # noqa: E402
import ptyd as PT  # noqa: E402

FAIL = []
PY = sys.executable
OWN_PIDS = []  # 本测试起的进程，最后只结束这些


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


class Cli:
    """测试用的最小客户端：一个读线程把回复按 id 收好，其余消息按顺序记下。"""

    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(path)
        self.fr, self.n, self.lock = PT.Framer(), 0, threading.Lock()
        self.replies, self.events, self.cv = {}, [], threading.Condition()
        threading.Thread(target=self._rd, daemon=True).start()

    def _rd(self):
        try:
            while True:
                d = self.s.recv(1 << 18)
                if not d:
                    break
                for h, b in self.fr.feed(d):
                    with self.cv:
                        if h.get('op') in ('ok', 'err') and h.get('id') is not None:
                            self.replies[h['id']] = (h, b)
                        else:
                            self.events.append((time.time(), h, b))
                        self.cv.notify_all()
        except OSError:
            pass
        with self.cv:
            self.events.append((time.time(), {'op': '_eof'}, b''))
            self.cv.notify_all()

    def send(self, h, b=b''):
        with self.lock:
            self.s.sendall(PT.pack(h, b))

    def req(self, h, b=b'', timeout=10):
        self.n += 1
        i = self.n
        self.send(dict(h, id=i), b)
        with self.cv:
            self.cv.wait_for(lambda: i in self.replies, timeout)
            return self.replies.pop(i, (None, None))[0]

    def wait_ev(self, pred, timeout=10):
        end = time.time() + timeout
        with self.cv:
            while True:
                for e in self.events:
                    if pred(e[1], e[2]):
                        return e
                left = end - time.time()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def output(self, key):
        with self.cv:
            return b''.join(b for _, h, b in self.events if h.get('op') == 'out' and h.get('key') == key)

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def start_ptyd(run, port):
    p = subprocess.Popen([PY, '-I', os.path.join(HERE, 'ptyd.py'), '--port', str(port), '--run-dir', run],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(os.path.join(run, 'ptyd.log'), 'ab'),
                         start_new_session=True)
    OWN_PIDS.append(p.pid)
    sock = PT.paths(run, port)['sock']
    for _ in range(100):
        if os.path.exists(sock):
            try:
                c = Cli(sock)
                if c.req({'op': 'ping'}, timeout=2):
                    return p, c
                c.close()
            except OSError:
                pass
        time.sleep(0.05)
    raise RuntimeError('ptyd 没起来')


def open_script(c, key, code, meta=None):
    h = c.req({'op': 'open', 'key': key, 'argv': [PY, '-c', code], 'env': dict(os.environ, PYTHONUNBUFFERED='1'),
               'cwd': '/tmp', 'rows': 24, 'cols': 80, 'meta': meta or {}})
    if h and h.get('pid'):
        OWN_PIDS.append(h['pid'])
    return h


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    out = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith('Z')  # 僵尸也算不在了


def procs(tag):
    out = {}
    for l in subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout.splitlines():
        p = l.split(None, 1)
        if len(p) == 2 and ('#' + tag + '-') in p[1] and 'ps -axo' not in p[1]:
            out[p[1].split('#' + tag + '-', 1)[1].split()[0].strip("'\")")] = int(p[0])
    return out


FAKE_CLI = r'''
import os, sys, time, subprocess
try:
    os.setsid()                  # pty.fork 已经让它成了会话首进程
except OSError:
    pass
tag = sys.argv[1]
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300) #%s-own' % tag])
cmd = ("python3 -c 'import time;time.sleep(301)#%(t)s-bg' & "
       "nohup python3 -c 'import time;time.sleep(302)#%(t)s-nohup' >/dev/null 2>&1 & "
       "python3 -c 'import os,time;os.setsid();time.sleep(303)#%(t)s-setsid' & "
       "perl -e 'setpgrp(0,0); select(undef,undef,undef,304); #%(t)s-pgrp' & ") % {'t': tag}
subprocess.Popen(['/bin/sh', '-c', cmd], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait()
time.sleep(300)
'''


def test_frame_and_paint():
    h, b = {'op': 'out', 'key': 'k', '中': '文'}, bytes(range(256)) * 10
    data = PT.pack(h, b) + PT.pack({'op': 'x'})
    fr, got = PT.Framer(), []
    for i in range(len(data)):  # 一个字节一个字节地喂
        got += fr.feed(data[i:i + 1])
    check(got == [(h, b), ({'op': 'x'}, b'')], '消息：长度前缀 + JSON 头 + 二进制体，逐字节切开也能拼回，体不做 base64')
    sc = pyte.Screen(40, 6)
    pyte.ByteStream(sc).feed('第一行 abc\r\n  ⏺ 宽字符：中文测试\r\n\r\n❯ 输入框\x1b[3;7H'.encode())
    snap = PT.snapshot(sc)
    sc2 = pyte.Screen(40, 6)
    pyte.ByteStream(sc2).feed(PT.paint_bytes(snap))
    check(PT.screen_rows(sc2) == snap['lines'] and [sc2.cursor.x, sc2.cursor.y] == snap['cursor'] == [6, 2],
          '快照：清屏 + 逐行定位写入 + 还原光标，画出来的屏幕和光标与原屏幕一致（含中文宽字符）')


def test_daemon():
    run = tempfile.mkdtemp(prefix='cwb-ptyd-')
    port = 40000 + os.getpid() % 20000
    paths = PT.paths(run, port)
    p, c = start_ptyd(run, port)
    try:
        st = os.stat(paths['sock'])
        check(stat.S_IMODE(st.st_mode) == 0o600, f'套接字权限 0600（实际 {oct(stat.S_IMODE(st.st_mode))}）')
        check(open(paths['lock']).read().strip() == str(p.pid), '锁文件里是守护进程的 PID')
        hello = c.req({'op': 'hello', 'proto': PT.PROTO, 'who': 'test'})
        check(bool(hello) and hello['proto'] == PT.PROTO and hello['build'] == PT.BUILD and hello['pid'] == p.pid, '握手：返回协议版本、代码指纹、PID')
        ino = os.stat(paths['sock']).st_ino
        p2 = subprocess.run([PY, '-I', os.path.join(HERE, 'ptyd.py'), '--port', str(port), '--run-dir', run], capture_output=True, timeout=10)
        check(p2.returncode == 3 and os.path.exists(paths['sock']) and os.stat(paths['sock']).st_ino == ino
              and c.req({'op': 'ping'}, timeout=2) is not None, '单例：第二个实例拿不到锁就退出（码 3），套接字原样还在、仍可用')

        # 会话 A：2 秒后才开始读输入（模拟 CLI 正忙），读完回报字节数；会话 B：每 0.05 秒输出一行
        code_a = ('import os,sys,time,tty\ntty.setraw(0)\nprint("READY",flush=True)\ntime.sleep(2)\nn=0\n'
                  'while n<200000:\n  d=os.read(0,65536)\n  if not d: break\n  n+=len(d)\nprint("GOT",n,flush=True)\ntime.sleep(60)\n')
        code_b = 'import time\ni=0\nwhile True:\n  i+=1\n  print("tick",i,flush=True)\n  time.sleep(0.05)\n'
        ha = open_script(c, 'A', code_a, {'sid': 's-a', 'gen': 'A'})
        hb = open_script(c, 'B', code_b, {'sid': 's-b'})
        check(bool(ha and ha.get('pid') and hb and hb.get('pid')), '打开会话：ptyd 自己 fork，返回 PID')
        time.sleep(0.6)
        c.send({'op': 'subscribe', 'key': 'A'})
        c.send({'op': 'subscribe', 'key': 'B'})
        snap = c.wait_ev(lambda h, b: h.get('op') == 'snap' and h.get('key') == 'A')
        check(bool(snap) and 'READY' in '\n'.join(snap[1]['screen']['lines']) and snap[1]['seq'] > 0, '订阅：先回屏幕快照（订阅前的输出已在快照里）和 seq')
        t0 = time.time()
        n0 = len(c.output('B'))
        c.n += 1
        ack_id = c.n
        c.send({'op': 'input', 'key': 'A', 'id': ack_id}, b'x' * 200000)
        lat = []
        for _ in range(8):
            t1 = time.time()
            c.req({'op': 'ping'}, timeout=3)
            lat.append(time.time() - t1)
            time.sleep(0.15)
        mid = len(c.output('B'))
        with c.cv:
            c.cv.wait_for(lambda: ack_id in c.replies, 15)
            acked = c.replies.pop(ack_id, (None,))[0]
        ack_t = time.time() - t0
        got = c.wait_ev(lambda h, b: b'GOT 200000' in c.output('A'), 10)
        check(max(lat) < 0.3 and mid - n0 > 50, f'大段粘贴：200KB 写不进去时循环不卡，ping 最慢 {max(lat):.3f}s，另一会话照常输出 {mid - n0} 字节')
        check(bool(acked) and acked.get('op') == 'ok' and ack_t >= 1.0 and got is not None, f'大段粘贴：全部写进终端后才回执（{ack_t:.1f}s），对方收到 200000 字节')

        c.req({'op': 'set-meta', 'key': 'A', 'meta': {'turn': True, 'since': 123.0, 'gone': None}})
        lst = c.req({'op': 'list'})['sessions']
        a = next(x for x in lst if x['key'] == 'A')
        check(a['meta'] == {'sid': 's-a', 'gen': 'A', 'turn': True, 'since': 123.0} and a['alive'], 'meta：不透明保存、合并，list 带回来')
        c.req({'op': 'resize', 'key': 'A', 'rows': 30, 'cols': 100})
        sn = c.req({'op': 'snapshot', 'key': 'A'})
        check(sn['screen']['rows'] == 30 and sn['screen']['cols'] == 100, '改尺寸：终端和快照屏幕一起改')

        seq_before = sn['seq']
        c.close()
        time.sleep(1.0)
        c = Cli(paths['sock'])
        lst = c.req({'op': 'list'})['sessions']
        bb = next(x for x in lst if x['key'] == 'B')
        check(bb['alive'] and bb['seq'] > mid, '断开：客户端（server）断开后 CLI 照常运行，输出继续累积')
        c.send({'op': 'subscribe', 'key': 'A', 'since': seq_before})
        rp = c.wait_ev(lambda h, b: h.get('op') == 'snap' and h.get('key') == 'A')
        check(bool(rp) and rp[1].get('mode') == 'replay', '重连：带 since 订阅时从环形缓冲补发，不用快照')

        code_c = 'print("第一行");print("bye-bye",flush=True);import sys;sys.exit(7)'
        open_script(c, 'C', code_c, {'sid': 's-c'})
        c.send({'op': 'subscribe', 'key': 'C'})
        ex = c.wait_ev(lambda h, b: h.get('op') == 'exit' and h.get('key') == 'C')
        check(bool(ex) and ex[1]['code'] == 7 and ex[1]['closed_by'] == '' and 'bye-bye' in ex[1]['tail'], 'CLI 自己退出：退出码 7、退出前最后几行、不是被结束的')
        lst = c.req({'op': 'list'})['sessions']
        check(any(x['key'] == 'C' and not x['alive'] for x in lst), '已退出的记录留着，等 server 来取')
        c.req({'op': 'forget', 'key': 'C'})
        check(not any(x['key'] == 'C' for x in c.req({'op': 'list'})['sessions']), 'forget 之后记录删掉')

        tag = 'cwbe' + uuid.uuid4().hex[:6]
        h = c.req({'op': 'open', 'key': 'R', 'argv': [PY, '-c', FAKE_CLI, tag], 'env': dict(os.environ, CWB_GEN='R-' + tag, PATH='/usr/bin:/bin'),
                   'cwd': '/tmp', 'rows': 24, 'cols': 80, 'meta': {'gen': 'R-' + tag}})
        OWN_PIDS.append(h['pid'])
        c.send({'op': 'subscribe', 'key': 'R'})
        for _ in range(60):
            if len(procs(tag)) >= 5:
                break
            time.sleep(0.1)
        before = procs(tag)
        OWN_PIDS.extend(before.values())
        check(set(before) == {'own', 'bg', 'nohup', 'setsid', 'pgrp'}, f'进程清理：模拟出 5 种后代 {sorted(before)}')
        c.req({'op': 'close', 'key': 'R', 'grace': 3})
        ex = c.wait_ev(lambda hh, b: hh.get('op') == 'exit' and hh.get('key') == 'R', 10)
        time.sleep(2.5)
        after = procs(tag)
        check(bool(ex) and ex[1]['closed_by'] == 'close' and not alive(h['pid']), '结束会话：SIGHUP 后 CLI 退出，记为被结束（close）')
        check('own' not in after and {'bg', 'pgrp', 'nohup', 'setsid'} <= set(after),
              f'进程清理：只清 CLI 自己会话里的，工具会话里 & 起的、nohup、setsid 都保留 {sorted(after)}')
        for pp in procs(tag).values():
            try:
                os.kill(pp, signal.SIGKILL)
            except OSError:
                pass

        pa, pb = ha['pid'], hb['pid']
        os.kill(p.pid, signal.SIGTERM)
        exa = c.wait_ev(lambda hh, b: hh.get('op') == 'exit' and hh.get('key') == 'A', 10)
        bye = c.wait_ev(lambda hh, b: hh.get('op') == 'bye', 10)
        p.wait(10)
        check(bool(exa) and exa[1]['closed_by'] == 'ptyd-exit' and bool(bye) and not alive(pa) and not alive(pb) and not os.path.exists(paths['sock']),
              '停止信号：先按规则结束所有 CLI（记为 ptyd-exit）、发告别，再删掉自己的套接字退出')
        c.close()

        p, c = start_ptyd(run, port)
        hd = open_script(c, 'D', code_b, {'sid': 's-d'})
        time.sleep(0.5)
        os.kill(p.pid, signal.SIGKILL)
        p.wait(5)
        eof = c.wait_ev(lambda hh, b: hh.get('op') == '_eof', 5)
        gone = False
        for _ in range(40):
            if not alive(hd['pid']):
                gone = True
                break
            time.sleep(0.1)
        check(eof is not None and gone, 'kill -9：连接断开，CLI 随伪终端关闭一起退出')
        p3, c3 = start_ptyd(run, port)
        check(c3.req({'op': 'list'})['sessions'] == [], 'kill -9 后：残留的套接字文件不影响新守护进程启动（拿到锁后才清理）')
        c3.req({'op': 'shutdown', 'close': True})
        p3.wait(10)
    finally:
        for pid in OWN_PIDS:  # 只结束仍在、且命令行确认是本测试起的进程（防 PID 复用）
            cmd = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True).stdout
            if cmd and any(k in cmd for k in (PY, 'cwbe', 'ptyd.py')):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


if __name__ == '__main__':
    test_frame_and_paint()
    test_daemon()
    print('全部通过' if not FAIL else f'{len(FAIL)} 项失败')
    sys.exit(1 if FAIL else 0)
