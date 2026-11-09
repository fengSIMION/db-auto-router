# -*- coding: utf-8 -*-
"""report_progress 的线程→循环桥 + 节流（progress.make_progress_reporter）。

这个 helper 是纯附加、尽力而为：任何一步出错都自己吞掉，绝不让"上报进度"
反过来拖垮或改变数据库操作。测试覆盖：no-op、节流、final 强制、异常吞噬。
"""
from db_auto_router import progress
from db_auto_router.progress import make_progress_reporter

# _dispatch 被 patch 掉，loop 只是个"非 None 即激活"的哨兵，用占位对象即可
_DUMMY_LOOP = object()


def _fake_report(recorded):
    """造一个 report_coro：被调用时记录 (done, total)，返回一个占位对象。

    测试里 _dispatch 被 patch 掉、不会真去 await，所以不必返真协程——
    返协程反而会触发"coroutine was never awaited"警告，污染输出。
    """
    def report(done, total=None):
        recorded.append((done, total))
        return ("coro", done, total)
    return report


def test_noop_when_loop_is_none(monkeypatch):
    dispatched = []
    monkeypatch.setattr(progress, "_dispatch", lambda coro, loop: dispatched.append(coro))
    reporter = make_progress_reporter(None, _fake_report([]))
    reporter(5, 10)
    reporter(10, 10, final=True)
    assert dispatched == []


def test_noop_when_report_coro_is_none(monkeypatch):
    dispatched = []
    monkeypatch.setattr(progress, "_dispatch", lambda coro, loop: dispatched.append(coro))
    reporter = make_progress_reporter(_DUMMY_LOOP, None)
    reporter(5, 10)
    assert dispatched == []


def test_first_and_final_always_emit_intermediate_throttled(monkeypatch):
    dispatched = []
    monkeypatch.setattr(progress, "_dispatch", lambda coro, loop: dispatched.append(coro))
    recorded = []
    # min_interval 设成很大：除非 final，否则中间的更新全被节流掉
    reporter = make_progress_reporter(_DUMMY_LOOP, _fake_report(recorded),
                                      min_interval=10_000)

    reporter(5, 100)               # 首次：必发
    reporter(6, 100)               # 距上次太近：被节流
    reporter(7, 100)               # 仍被节流
    reporter(100, 100, final=True) # final：强制发

    assert recorded == [(5, 100), (100, 100)]
    assert len(dispatched) == 2    # 只有真正发出的才 dispatch


def test_emits_every_call_when_no_throttle(monkeypatch):
    monkeypatch.setattr(progress, "_dispatch", lambda coro, loop: None)
    recorded = []
    reporter = make_progress_reporter(_DUMMY_LOOP, _fake_report(recorded),
                                      min_interval=0)
    reporter(1, 3)
    reporter(2, 3)
    reporter(3, 3, final=True)
    assert recorded == [(1, 3), (2, 3), (3, 3)]


def test_swallows_dispatch_errors(monkeypatch):
    def boom(coro, loop):
        raise RuntimeError("loop closed / client gone")
    monkeypatch.setattr(progress, "_dispatch", boom)
    recorded = []
    reporter = make_progress_reporter(_DUMMY_LOOP, _fake_report(recorded),
                                      min_interval=0)
    # 上报失败绝不能向上抛——进度是尽力而为
    reporter(1, 3)
    reporter(2, 3, final=True)
    assert recorded == [(1, 3), (2, 3)]  # 参数已记录，只是 dispatch 炸了被吞
