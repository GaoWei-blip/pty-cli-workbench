"""11-14：回复进行中换模型 / 思考强度“待生效”的显示与“立即切换”。
不启动 claude、不碰正式 config.json：配置放临时目录，Runner 用假对象，重启只记调用。
运行：.venv/bin/python -I tests/test_pending_opts.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S  # noqa: E402

FAIL = []


def check(cond, msg):
    print(('通过 ' if cond else '失败 ') + msg, flush=True)
    if not cond:
        FAIL.append(msg)


class FakeRunner:
    def __init__(self, sid, opts, busy):
        self.sid, self.opts, self.busy = sid, dict(opts), busy
        self.state = {'alive': True, 'busy': busy, 'bg': 0, 'dialog': None}
        self.restart_when_idle, self.stopping, self.queue, self.sending = False, False, [], False
        self.turn, self.turn_t = busy, 0

    def active(self):
        return self.busy

    def sync_meta(self):
        pass

    pending_view = S.Runner.pending_view


def main():
    d = tempfile.mkdtemp(prefix='cwb-pend-')
    orig = (S.CONF_PATH, S.user_settings, S.restart_runner)
    S.CONF_PATH = os.path.join(d, 'config.json')
    S.user_settings = lambda: {}
    restarted = []
    S.restart_runner = lambda r: restarted.append(r.sid)
    sid = 'test-pending-0001'
    try:
        S.save_session_opts(sid, {'effort': 'xhigh', 'mode': 'bypass permissions'})
        r = FakeRunner(sid, {'effort': 'xhigh', 'mode': 'bypass permissions'}, busy=True)
        S.RUNNERS[sid] = r
        dflt = S.cli_default_model()
        check(r.pending_view() is None, '没有待生效时 pending 为 None')

        out = S.api_control({'sid': sid, 'model': 'claude-fable-5-2'})
        check(out.get('pending') is True and r.restart_when_idle, '回复中换模型：标记待生效，接口返回 pending=True')
        check(not restarted, '回复中换模型：不立即重启')
        pv = r.pending_view()
        check(pv == {'model': 'claude-fable-5-2', 'effort': 'xhigh', 'run_model': dflt, 'run_effort': 'xhigh'},
              f'pending 给出正在用的（默认模型按实际 ID {dflt}）和已选的：{pv}')

        out = S.api_control({'sid': sid, 'model': dflt})
        check(out.get('pending') is False and not r.restart_when_idle, '选回正在用的默认模型（显式 ID）：取消待生效')
        check(not restarted, '选回正在用的：不重启')

        S.api_control({'sid': sid, 'effort': 'max'})
        pv = r.pending_view()
        check(r.restart_when_idle and pv['effort'] == 'max' and pv['run_effort'] == 'xhigh', '回复中换思考强度：也待生效')
        S.api_control({'sid': sid, 'effort': 'xhigh'})
        check(not r.restart_when_idle, '思考强度选回原值：取消待生效')

        S.api_control({'sid': sid, 'model': 'claude-fable-5-2'})
        r.queue = [('x', [], False)]
        try:
            S.api_apply_opts({'sid': sid})
            check(False, '有没发出的消息时立即切换应报错')
        except ValueError as e:
            check('没发出' in str(e) and r.restart_when_idle, '有没发出的消息：拒绝立即切换，待生效保留')
        r.queue = []
        out = S.api_apply_opts({'sid': sid})
        check(out.get('ok') and restarted == [sid] and not r.restart_when_idle and r.turn is False,
              '立即切换：清掉待生效、按用户主动停止处理、调用重启')

        restarted.clear()
        r2 = FakeRunner(sid, {'effort': 'xhigh'}, busy=False)
        S.RUNNERS[sid] = r2
        S.save_session_opts(sid, {'effort': 'xhigh'})
        out = S.api_control({'sid': sid, 'model': 'claude-sonnet-5-5'})
        check(restarted == [sid] and out.get('pending') is False, '空闲时换模型：照旧立即重启，不显示待生效')
        restarted.clear()
        out = S.api_apply_opts({'sid': sid})
        check(out.get('ok') and not restarted, '没有待生效时立即切换：什么都不做')
    finally:
        S.RUNNERS.pop(sid, None)
        S.CONF_PATH, S.user_settings, S.restart_runner = orig
    print('全部通过' if not FAIL else f'失败 {len(FAIL)} 项')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
