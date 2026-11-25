#!/usr/bin/env python3
"""Claude 工作台终端守护进程（第十三轮 E）。

持有所有 claude 的伪终端主端，由它 fork CLI、waitpid 取退出码。server.py 重启（部署）时 CLI 不受影响，
server 重新连上后先拿屏幕快照、再接实时输出，对话照常进行。

- 单例：fcntl.flock(.run/ptyd-<端口>.lock)，拿不到锁立即退出，绝不动套接字。锁文件里写着自己的 PID。
- Unix 套接字 .run/ptyd-<端口>.sock，权限 0600。
- 消息：8 字节前缀 struct('>II')＝(头长, 体长)，后跟 UTF-8 JSON 头和二进制体。PTY 字节放在体里，不做 base64。
- 单线程 selectors 循环处理所有 fd（伪终端、套接字、信号唤醒管道）。结束 CLI、清理子进程要跑 ps、要等待，放在后台线程，
  这些线程只发信号、读 session.reaped，不碰任何 fd。
- 每个会话：非阻塞写队列（大段粘贴不卡循环）、输出 seq（累计字节数）、512KB 环形缓冲、一份 pyte 屏幕（出快照）、
  退出码和退出前屏幕最后 20 行、server 写入的不透明 meta。
"""
import fcntl
import hashlib
import heapq
import json
import os
import pty
import re
import selectors
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from collections import deque

import pyte
from wcwidth import wcwidth

PROTO = 1  # 协议版本：消息格式或语义变了就加一。server.py 直接 import 这个常量，对不上就走“待升级”
ROOT = os.path.dirname(os.path.abspath(__file__))
RING = 512 * 1024
MAX_HDR, MAX_BODY = 8 << 20, 64 << 20
CLIENT_BUF_MAX = 64 << 20  # 某个连接 64MB 发不出去（对方卡死）就断开它，对方重连后重新订阅
IDLE_QUIT = 600  # 没有连接、也没有在跑的 CLI，10 分钟后自己退出
DEAD_KEEP = 86400  # 已退出的会话记录留一天，等 server 来取退出码（server 不在时退出的）


def log(*a):
    print(time.strftime('%H:%M:%S'), '[ptyd]', *a, file=sys.stderr, flush=True)


def build_id(path=None):
    """代码指纹：ptyd.py 改了（哪怕协议版本没变）也要换掉正在运行的守护进程。"""
    try:
        with open(path or os.path.abspath(__file__), 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()[:12]
    except OSError:
        return ''


BUILD = build_id()


def paths(run_dir, port):
    return {'sock': os.path.join(run_dir, f'ptyd-{port}.sock'), 'lock': os.path.join(run_dir, f'ptyd-{port}.lock'),
            'log': os.path.join(run_dir, f'ptyd-{port}.log')}


def pack(hdr, body=b''):
    h = json.dumps(hdr, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return struct.pack('>II', len(h), len(body)) + h + body


class Framer:
    """按长度前缀切消息。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 8:
            hl, bl = struct.unpack_from('>II', self.buf, 0)
            if hl > MAX_HDR or bl > MAX_BODY:
                raise ValueError('消息过大')
            n = 8 + hl + bl
            if len(self.buf) < n:
                break
            hdr = json.loads(bytes(self.buf[8:8 + hl]).decode('utf-8'))
            body = bytes(self.buf[8 + hl:n])
            del self.buf[:n]
            out.append((hdr, body))
        return out


# ---------------------------------------------------------------- 屏幕快照
def screen_rows(screen):
    """逐行取字符（和 server 的 safe_display 一样容忍孤立的宽字符占位格），行尾空格去掉。"""
    out = []
    for y in range(screen.lines):
        line, row, wide = screen.buffer[y], [], False
        for x in range(screen.columns):
            if wide:
                wide = False
                continue
            ch = line[x].data
            if not ch:
                row.append(' ')
                continue
            row.append(ch)
            wide = wcwidth(ch[0]) == 2
        out.append(''.join(row).rstrip())
    return out


def snapshot(screen):
    m = screen.margins
    return {'lines': screen_rows(screen), 'cursor': [screen.cursor.x, screen.cursor.y], 'rows': screen.lines,
            'cols': screen.columns, 'margins': [m.top, m.bottom] if m else None}


def paint_bytes(snap):
    """把快照画回一块新屏幕的字节序列：清屏 + 逐行定位写入 + 还原滚动区和光标（server 喂给自己的 pyte）。"""
    out = ['\x1b[0m\x1b[2J\x1b[H']
    for y, line in enumerate(snap.get('lines') or []):
        if line:
            out.append(f'\x1b[{y + 1};1H{line}')
    m = snap.get('margins')
    if m:
        out.append(f'\x1b[{m[0] + 1};{m[1] + 1}r')
    cx, cy = (snap.get('cursor') or [0, 0])[:2]
    out.append(f'\x1b[{cy + 1};{cx + 1}H')
    return ''.join(out).encode('utf-8')


def tail_lines(lines, n=20):
    """连续空行并成一行，首尾空行去掉，取最后 n 行（CLI 意外退出时给界面看）。"""
    tail = []
    for l in lines:
        if l.strip() or (tail and tail[-1].strip()):
            tail.append(l.rstrip())
    while tail and not tail[-1].strip():
        tail.pop()
    return tail[-n:]


# ---------------------------------------------------------------- 结束 CLI 时清理它留下的子进程（第十一轮 A，从 server.py 搬来，规则不变）
def _ps_rows():
    """ps -axo pid=,ppid=,pgid=,sess=。macOS 的 sess 列恒为 0（实测），会话号用 os.getsid 补。"""
    out = {}
    try:
        txt = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid=,sess='], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return out
    for l in txt.splitlines():
        p = l.split()
        try:
            pid, ppid, pgid = int(p[0]), int(p[1]), int(p[2])
            out[pid] = (ppid, pgid, os.getsid(pid))
        except (ValueError, IndexError, OSError):
            continue
    return out


def _gen_marked(gen):
    """环境变量里带这个代次的进程：CLI 起的进程都继承它，已经被系统收养的也能找到（改写过参数区的进程如 perl 读不到）。"""
    pat = re.compile(r'(?:^|\s)CWB_GEN=' + re.escape(gen) + r'(?:\s|$)')
    out = set()
    try:
        txt = subprocess.run(['ps', '-Eww', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=5, errors='replace').stdout
    except Exception:
        return out
    for l in txt.splitlines():
        if 'CWB_GEN=' in l and pat.search(l):
            try:
                out.add(int(l.split(None, 1)[0]))
            except (ValueError, IndexError):
                pass
    return out


_LIBC = []


def sighup_ignored(pid):
    """进程忽略了 SIGHUP（nohup 启动的）：读 sysctl kern.proc.pid 的 kinfo_proc，p_sigignore 在偏移 232（macOS，实测），p_pid 在 40。"""
    try:
        import ctypes
        import ctypes.util
        if not _LIBC:
            _LIBC.append(ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True))
        libc = _LIBC[0]
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0 or size.value < 236:
            return False
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 236:
            return False
        if struct.unpack_from('<i', buf.raw, 40)[0] != pid:
            return False  # 结构对不上就当没忽略
        return bool(struct.unpack_from('<I', buf.raw, 232)[0] & 1)
    except Exception:
        return False


def cli_family(pid, gen):
    """结束前记下要清理的后代进程：只算 CLI 自己的会话和进程组（MCP 服务等 CLI 直接带起来的）。
    Bash 工具的命令跑在 CLI 另建的会话里，里面用 & 起的开发服务在终端里关掉窗口也会留下，
    用户可能还在预览，这里同样不动；忽略 SIGHUP 的（nohup）也不清理。"""
    rows = _ps_rows()
    if pid not in rows:
        return None
    _, pg, ss = rows[pid]
    sess = {ss}
    procs = {p: v for p, v in rows.items() if p != pid and (v[2] in sess or v[1] == pg) and not sighup_ignored(p)}
    return {'pgid': pg, 'sess': sess, 'procs': procs}


def reap_family(fam, sid=''):
    """CLI 已退出：记下的后代里还活着、仍在原会话或进程组里的，先 SIGTERM，2 秒后还在就 SIGKILL。"""
    def member(p):
        try:
            return os.getsid(p) in fam['sess'] or os.getpgid(p) == fam['pgid']
        except OSError:
            return False
    left = [p for p in fam['procs'] if member(p)]
    if not left:
        return []
    for p in left:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + 2
    while time.time() < end and any(member(p) for p in left):
        time.sleep(0.1)
    killed = []
    for p in left:
        if member(p):
            try:
                os.kill(p, signal.SIGKILL)
                killed.append(p)
            except OSError:
                pass
    log('清理 claude 留下的子进程', sid, '结束', left, '其中强制', killed)
    return left


# ---------------------------------------------------------------- 会话与连接
class Session:
    def __init__(self, key, pid, fd, rows, cols, cwd, meta):
        self.key, self.pid, self.fd, self.rows, self.cols, self.cwd = key, pid, fd, rows, cols, cwd
        self.meta = dict(meta or {})
        self.started = time.time()
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.seq, self.ring = 0, bytearray()
        self.wq = deque()  # [数据, 已写字节, 回执 id, 连接]
        self.events = 0
        self.alive, self.eof, self.reaped, self.eof_timer = True, False, False, False
        self.code, self.tail, self.exited_at = None, '', 0.0
        self.closing, self.closed_by = False, ''  # closed_by：close＝server 要求结束；ptyd-exit＝守护进程自己退出时结束的
        self.subs = set()

    def info(self):
        return {'key': self.key, 'pid': self.pid, 'alive': self.alive, 'code': self.code, 'tail': self.tail if not self.alive else '',
                'closing': self.closing, 'closed_by': self.closed_by, 'seq': self.seq, 'started': self.started,
                'exited_at': self.exited_at, 'rows': self.rows, 'cols': self.cols, 'cwd': self.cwd, 'meta': self.meta,
                'pending': sum(len(x[0]) - x[1] for x in self.wq)}


class Client:
    def __init__(self, sock):
        self.sock, self.fr, self.out, self.subs = sock, Framer(), bytearray(), set()
        self.closed, self.events, self.who = False, selectors.EVENT_READ, ''


class Daemon:
    def __init__(self, run_dir, port):
        self.run_dir, self.port = run_dir, port
        self.p = paths(run_dir, port)
        self.sel = selectors.DefaultSelector()
        self.sessions, self.clients = {}, set()
        self.timers, self.tn = [], 0
        self.quitting, self.quit_at, self.quit_why = False, 0.0, ''
        self.started = self.last_busy = time.time()
        self.lockfd = -1

    # ---- 启动
    def acquire(self):
        """单例：拿不到锁说明已有守护进程，直接退出，不碰套接字（否则会把正在用的套接字删掉）。"""
        os.makedirs(self.run_dir, exist_ok=True)
        fd = os.open(self.p['lock'], os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f'{os.getpid()}\n'.encode())
        self.lockfd = fd  # 一直开着；进程退出时锁自动释放（fd 默认不继承，CLI 拿不到）
        return True

    def listen(self):
        try:
            os.unlink(self.p['sock'])  # 拿到了锁，残留的套接字文件是上一个守护进程的
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            srv.bind(self.p['sock'])
        finally:
            os.umask(old)
        os.chmod(self.p['sock'], 0o600)
        srv.listen(16)
        srv.setblocking(False)
        self.srv = srv
        self.sel.register(srv, selectors.EVENT_READ, ('listen', None))
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        self.wake_r = r
        signal.set_wakeup_fd(w, warn_on_full_buffer=False)
        for s in (signal.SIGCHLD, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, lambda *_: None)  # 真正的处理在循环里读唤醒管道；Python 层的处理函数在 exec 后自动恢复默认，不影响 CLI
        self.sel.register(r, selectors.EVENT_READ, ('wake', None))

    def at(self, delay, fn):
        self.tn += 1
        heapq.heappush(self.timers, (time.time() + delay, self.tn, fn))

    # ---- 主循环
    def run(self):
        log('已启动', 'pid', os.getpid(), 'proto', PROTO, 'build', BUILD, self.p['sock'])
        self.at(1.0, self.tick)
        while True:
            now = time.time()
            while self.timers and self.timers[0][0] <= now:
                _, _, fn = heapq.heappop(self.timers)
                try:
                    fn()
                except Exception as e:
                    log('定时任务异常', repr(e))
            if self.quitting and self.done_quitting():
                break
            wait = min(1.0, max(0.0, self.timers[0][0] - time.time())) if self.timers else 1.0
            for key, ev in self.sel.select(wait):
                kind, obj = key.data
                try:
                    if kind == 'pty':
                        if ev & selectors.EVENT_READ:
                            self.pty_read(obj)
                        if ev & selectors.EVENT_WRITE and obj.fd >= 0:
                            self.pty_flush(obj)
                    elif kind == 'client':
                        if ev & selectors.EVENT_READ:
                            self.client_read(obj)
                        if ev & selectors.EVENT_WRITE and not obj.closed:
                            self.client_flush(obj)
                    elif kind == 'listen':
                        self.accept()
                    elif kind == 'wake':
                        self.on_signals()
                except Exception as e:
                    log('处理事件异常', kind, repr(e))
        self.exit_now()

    def tick(self):
        """每秒：补查一次子进程（SIGCHLD 可能合并）、清理过期的已退出记录、没人用时自己退出。"""
        now = time.time()
        for s in list(self.sessions.values()):
            if s.alive:
                self.check_exit(s)
            elif now - s.exited_at > DEAD_KEEP:
                self.sessions.pop(s.key, None)
        if self.clients or any(s.alive for s in self.sessions.values()):
            self.last_busy = now
        elif not self.quitting and now - self.last_busy > IDLE_QUIT:
            log('没有连接也没有在跑的 CLI，退出')
            self.begin_quit('idle', close=False)
        self.at(1.0, self.tick)

    def on_signals(self):
        try:
            data = os.read(self.wake_r, 512)
        except OSError:
            return
        for n in data:
            if n == signal.SIGCHLD:
                for s in list(self.sessions.values()):
                    if s.alive and not s.reaped:
                        self.check_exit(s)
            elif n in (signal.SIGTERM, signal.SIGINT) and not self.quitting:
                log('收到停止信号：结束所有 CLI 后退出')
                self.begin_quit('ptyd-exit', close=True)

    # ---- 伪终端
    def set_events(self, fd, obj, want, kind):
        if want != obj.events:
            if obj.events:
                self.sel.modify(fd, want, (kind, obj))
            else:
                self.sel.register(fd, want, (kind, obj))
            obj.events = want

    def spawn(self, key, argv, env, cwd, rows, cols, meta):
        winsz = struct.pack('HHHH', rows, cols, 0, 0)
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsz)
                os.chdir(cwd)
                os.execve(argv[0], argv, env)
            except BaseException as e:
                try:
                    os.write(2, f'启动失败: {e}\n'.encode())
                finally:
                    os._exit(127)
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsz)
        except OSError:
            pass
        os.set_blocking(fd, False)
        s = Session(key, pid, fd, rows, cols, cwd, meta)
        self.sessions[key] = s
        self.set_events(fd, s, selectors.EVENT_READ, 'pty')
        log('启动 claude', key, 'pid', pid, cwd)
        return s

    def pty_read(self, s):
        if s.fd < 0:
            return
        try:
            data = os.read(s.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b''  # EIO：从端都关了
        if not data:
            self.on_eof(s)
            return
        s.seq += len(data)
        s.ring += data
        if len(s.ring) > RING:
            del s.ring[:len(s.ring) - RING]
        try:
            s.stream.feed(data)
        except Exception as e:
            log('屏幕解析异常', s.key, repr(e))
        hdr = {'op': 'out', 'key': s.key, 'seq': s.seq}
        for c in list(s.subs):
            self.csend(c, hdr, data)

    def pty_flush(self, s):
        while s.wq and s.fd >= 0:
            item = s.wq[0]
            data, off = item[0], item[1]
            if off < len(data):
                try:
                    n = os.write(s.fd, data[off:off + 65536])
                except BlockingIOError:
                    break
                except OSError:
                    self.fail_writes(s)
                    break
                item[1] += n
                if item[1] < len(data):
                    continue
            s.wq.popleft()
            if item[2] is not None:
                self.csend(item[3], {'op': 'ok', 'id': item[2]})  # 这段输入已经全部写进终端
        if s.fd >= 0 and not s.eof:
            self.set_events(s.fd, s, selectors.EVENT_READ | (selectors.EVENT_WRITE if s.wq else 0), 'pty')

    def fail_writes(self, s):
        while s.wq:
            item = s.wq.popleft()
            if item[2] is not None:
                self.csend(item[3], {'op': 'err', 'id': item[2], 'error': '终端已关闭'})

    def on_eof(self, s):
        if s.fd >= 0 and s.events:
            try:
                self.sel.unregister(s.fd)
            except Exception:
                pass
            s.events = 0
        s.eof = True
        self.check_exit(s)

    def check_exit(self, s):
        if not s.alive:
            return
        if not s.reaped:
            try:
                pid, st = os.waitpid(s.pid, os.WNOHANG)
            except ChildProcessError:
                pid, st = s.pid, None
            if pid == s.pid:
                s.reaped = True
                s.code = os.waitstatus_to_exitcode(st) if st is not None else None
        if s.reaped and s.eof:
            self.finish(s)
        elif s.reaped and not s.eof_timer:
            # CLI 已退出但从端还被别的进程拿着：再收 1.5 秒输出就当结束
            s.eof_timer = True
            self.at(1.5, lambda: None if s.eof else self.on_eof(s))
        elif s.eof and not s.reaped and not s.eof_timer:
            s.eof_timer = True  # 终端断了但进程还在：靠 SIGCHLD 和每秒补查

    def finish(self, s):
        s.alive, s.exited_at = False, time.time()
        try:
            s.tail = '\n'.join(tail_lines(screen_rows(s.screen)))
        except Exception:
            s.tail = ''
        self.fail_writes(s)
        if s.fd >= 0:
            try:
                os.close(s.fd)
            except OSError:
                pass
            s.fd = -1
        log('claude 已退出', s.key, 'pid', s.pid, '退出码', s.code, s.closed_by or '自行退出')
        ev = {'op': 'exit', 'key': s.key, 'code': s.code, 'tail': s.tail, 'closed_by': s.closed_by, 't': s.exited_at}
        for c in list(s.subs):
            self.csend(c, ev)

    def close_session(self, s, grace, why='close'):
        if not s.alive:
            return
        if s.closing:
            if grace <= 0 and not s.reaped:
                try:
                    os.kill(s.pid, signal.SIGKILL)
                except OSError:
                    pass
            return
        s.closing, s.closed_by = True, why
        threading.Thread(target=self.terminate, args=(s, grace), daemon=True).start()

    def terminate(self, s, grace):
        """后台线程：先记下后代进程，给 CLI 的进程组发 SIGHUP（相当于关掉终端，CLI 自己收尾），超时 SIGKILL，最后清理留下的子进程。
        只发信号、读 s.reaped（主循环 waitpid 后置位），不碰 fd。"""
        pid = s.pid
        try:
            fam = cli_family(pid, s.meta.get('gen') or s.key)
        except Exception as e:
            log('记录子进程失败', repr(e))
            fam = None
        if not s.reaped:
            try:
                os.killpg(os.getpgid(pid), signal.SIGHUP)
            except OSError:
                try:
                    os.kill(pid, signal.SIGHUP)
                except OSError:
                    pass
        end = time.time() + grace
        while not s.reaped and time.time() < end:
            time.sleep(0.1)
        if not s.reaped:
            log('claude 没响应 SIGHUP，强制结束', s.key)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            end = time.time() + 2
            while not s.reaped and time.time() < end:
                time.sleep(0.1)
        if fam and fam['procs']:
            reap_family(fam, s.meta.get('sid') or s.key)

    # ---- 连接
    def accept(self):
        try:
            conn, _ = self.srv.accept()
        except (BlockingIOError, InterruptedError):
            return
        conn.setblocking(False)
        c = Client(conn)
        self.clients.add(c)
        self.sel.register(conn, selectors.EVENT_READ, ('client', c))

    def drop(self, c, why=''):
        if c.closed:
            return
        c.closed = True
        try:
            self.sel.unregister(c.sock)
        except Exception:
            pass
        try:
            c.sock.close()
        except OSError:
            pass
        self.clients.discard(c)
        for k in c.subs:
            s = self.sessions.get(k)
            if s:
                s.subs.discard(c)
        if why:
            log('断开连接', c.who or '?', why)

    def csend(self, c, hdr, body=b''):
        if c.closed:
            return
        c.out += pack(hdr, body)
        if len(c.out) > CLIENT_BUF_MAX:
            self.drop(c, '对方长时间不读，缓冲超过 64MB')
            return
        self.client_flush(c)

    def client_flush(self, c):
        while c.out:
            try:
                with memoryview(c.out) as mv, mv[:1 << 20] as part:
                    n = c.sock.send(part)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self.drop(c)
                return
            del c.out[:n]
        if not c.closed:
            want = selectors.EVENT_READ | (selectors.EVENT_WRITE if c.out else 0)
            if want != c.events:
                self.sel.modify(c.sock, want, ('client', c))
                c.events = want

    def client_read(self, c):
        try:
            data = c.sock.recv(1 << 18)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b''
        if not data:
            self.drop(c)
            return
        try:
            msgs = c.fr.feed(data)
        except Exception as e:
            self.drop(c, '消息格式错误 ' + repr(e))
            return
        for hdr, body in msgs:
            try:
                self.handle(c, hdr, body)
            except Exception as e:
                log('处理消息异常', hdr.get('op'), repr(e))
                if hdr.get('id') is not None:
                    self.csend(c, {'op': 'err', 'id': hdr['id'], 'error': str(e)})
            if c.closed:
                return

    def reply(self, c, hdr, **kw):
        if hdr.get('id') is not None:
            kw.update(op='ok', id=hdr['id'])
            self.csend(c, kw)

    # ---- 消息
    def handle(self, c, hdr, body):
        op = hdr.get('op')
        s = self.sessions.get(hdr.get('key') or '')
        if op == 'hello':
            c.who = str(hdr.get('who') or '')
            self.reply(c, hdr, proto=PROTO, build=BUILD, pid=os.getpid(), started=self.started,
                       sessions=sum(1 for x in self.sessions.values() if x.alive), quitting=self.quitting)
        elif op == 'ping':
            self.reply(c, hdr)
        elif op == 'open':
            if self.quitting:
                raise RuntimeError('终端守护进程正在退出')
            key = str(hdr['key'])
            if s and s.alive:
                raise RuntimeError('这个会话已经在运行')
            s = self.spawn(key, list(hdr['argv']), dict(hdr['env']), hdr['cwd'], int(hdr.get('rows') or 50),
                           int(hdr.get('cols') or 160), hdr.get('meta'))
            self.reply(c, hdr, key=key, pid=s.pid)
        elif op == 'input':
            if not s or not s.alive or s.fd < 0:
                if hdr.get('id') is not None:
                    self.csend(c, {'op': 'err', 'id': hdr['id'], 'error': '这个会话没有在运行'})
                return
            s.wq.append([body, 0, hdr.get('id'), c])
            self.pty_flush(s)
        elif op == 'resize':
            if s and s.alive and s.fd >= 0:
                rows, cols = int(hdr['rows']), int(hdr['cols'])
                fcntl.ioctl(s.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
                s.rows, s.cols = rows, cols
                s.screen.resize(rows, cols)
            self.reply(c, hdr)
        elif op == 'redraw':
            # 兜底：缩一行再还原，CLI 收到 SIGWINCH 后整屏重画（实测 2.1.321 会重画）。这份屏幕和 server 的都跟着收到重画的字节
            if s and s.alive and s.fd >= 0:
                fd, rows, cols = s.fd, s.rows, s.cols
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', max(2, rows - 1), cols, 0, 0))

                def back():
                    if s.alive and s.fd == fd:
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', s.rows, s.cols, 0, 0))
                self.at(0.15, back)
            self.reply(c, hdr)
        elif op == 'close':
            if s:
                self.close_session(s, float(hdr.get('grace', 3.0)))
            self.reply(c, hdr, found=bool(s))
        elif op == 'list':
            self.reply(c, hdr, sessions=[x.info() for x in self.sessions.values()])
        elif op == 'subscribe':
            if not s:
                self.csend(c, {'op': 'snap', 'key': hdr.get('key'), 'missing': True})
                return
            since, start = hdr.get('since'), s.seq - len(s.ring)
            if isinstance(since, int) and start <= since <= s.seq:
                # 断线重连、屏幕还在：从环形缓冲补上漏掉的字节
                self.csend(c, {'op': 'snap', 'key': s.key, 'mode': 'replay', 'seq': since, 'alive': s.alive})
                if since < s.seq:
                    self.csend(c, {'op': 'out', 'key': s.key, 'seq': s.seq}, bytes(s.ring[since - start:]))
            else:
                self.csend(c, {'op': 'snap', 'key': s.key, 'mode': 'snapshot', 'seq': s.seq, 'alive': s.alive, 'screen': snapshot(s.screen)})
            s.subs.add(c)
            c.subs.add(s.key)
            if not s.alive:
                self.csend(c, {'op': 'exit', 'key': s.key, 'code': s.code, 'tail': s.tail, 'closed_by': s.closed_by, 't': s.exited_at})
        elif op == 'unsubscribe':
            if s:
                s.subs.discard(c)
            c.subs.discard(hdr.get('key'))
        elif op == 'snapshot':
            if not s:
                raise RuntimeError('没有这个会话')
            d = s.info()
            d.update(screen=snapshot(s.screen), ring=len(s.ring))
            self.csend(c, dict(d, op='ok', id=hdr.get('id')), bytes(s.ring[-8192:]))
        elif op == 'set-meta':
            if s:
                if hdr.get('replace'):
                    s.meta = {}
                for k, v in (hdr.get('meta') or {}).items():
                    if v is None:
                        s.meta.pop(k, None)
                    else:
                        s.meta[k] = v
            self.reply(c, hdr, found=bool(s))
        elif op == 'forget':
            if s and not s.alive:
                self.sessions.pop(s.key, None)
            self.reply(c, hdr)
        elif op == 'shutdown':
            self.reply(c, hdr)
            self.begin_quit(str(hdr.get('why') or 'shutdown'), close=hdr.get('close', True) is not False)
        else:
            raise RuntimeError('不认识的操作 ' + str(op))

    # ---- 退出
    def begin_quit(self, why, close=True):
        if self.quitting:
            return
        self.quitting, self.quit_at, self.quit_why = True, time.time(), why
        if close:
            for s in list(self.sessions.values()):
                if s.alive:
                    self.close_session(s, 3.0, 'ptyd-exit' if why == 'ptyd-exit' else 'close')

    def done_quitting(self):
        alive = [s for s in self.sessions.values() if s.alive]
        return not alive or time.time() - self.quit_at > 9

    def exit_now(self):
        for c in list(self.clients):
            self.csend(c, {'op': 'bye', 'why': self.quit_why})
        end = time.time() + 1
        while time.time() < end and any(c.out and not c.closed for c in self.clients):
            for key, ev in self.sel.select(0.1):
                if key.data[0] == 'client' and ev & selectors.EVENT_WRITE:
                    self.client_flush(key.data[1])
        try:
            os.unlink(self.p['sock'])  # 还拿着锁，删的是自己的套接字
        except OSError:
            pass
        log('退出', self.quit_why)
        os._exit(0)


def main():
    port, run_dir = int(os.environ.get('CWB_PORT') or 47821), os.path.join(ROOT, '.run')
    a = sys.argv[1:]
    for i, x in enumerate(a):
        if x == '--port' and i + 1 < len(a):
            port = int(a[i + 1])
        elif x == '--run-dir' and i + 1 < len(a):
            run_dir = a[i + 1]
    d = Daemon(run_dir, port)
    if not d.acquire():
        log('已有终端守护进程在运行，退出')
        sys.exit(3)
    d.listen()
    d.run()


if __name__ == '__main__':
    main()
