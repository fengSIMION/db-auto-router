# -*- coding: utf-8 -*-
"""report_progress：把长任务的进度从工作线程回传到 MCP 的异步 Context。

背景：tools.py 的 DB 执行走 `_run_db` → `asyncio.to_thread`，真正的活在**工作
线程**里跑；而 `Context.report_progress` 是个协程，得回到**事件循环**上 await。
本模块搭这座桥，并顺带做两件事：

- 节流：进展中最多约 min_interval 秒发一次，避免每批数据都刷一条通知；
- 尽力而为：任何一步（loop 关了、客户端断开、调度失败）都自己吞掉——
  进度是纯附加，绝不允许它反过来拖垮或改变数据库操作。

`Context` 把 request_context 存成实例属性（不是 ContextVar），所以从工作线程
经 run_coroutine_threadsafe 把协程丢回 loop 是安全的；且客户端没带
progressToken 时 report_progress 自动 no-op，无需自己判断。
"""
from __future__ import annotations

__author__ = "fengkeju"

import asyncio
import threading
import time
from typing import Callable, Optional

# progress(done, total=None, final=False)——引擎侧就按这个签名调，节流在桥里做。
ProgressFn = Callable[..., None]


def _dispatch(coro, loop) -> None:
    """把上报协程调度到事件循环上执行（即发即忘）。

    单独抽成模块级函数：测试可 monkeypatch 掉，免去为单元测试起真 loop。
    add_done_callback 取一次 exception()：既吞掉 future 的异常（客户端断开时
    report_progress 可能抛），也避免 asyncio 打"exception was never retrieved"。
    """
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    fut.add_done_callback(lambda f: f.exception())


def make_progress_reporter(loop, report_coro: Optional[Callable],
                           min_interval: float = 1.0) -> ProgressFn:
    """造一个引擎可用的进度回调。

    参数：
    - loop: 事件循环（工作线程→循环桥的目标）。None → 返回 no-op。
    - report_coro: 可调用 (done, total) → 协程，通常是 ctx.report_progress。
      None → 返回 no-op（例如没传 ctx 的调用）。
    - min_interval: 进展中的最小上报间隔（秒）。final 永远强制发，不受此限。

    返回 progress(done, total=None, final=False)：
    - 引擎在批次循环里调 progress(done, total)（被节流）；
    - 收尾调 progress(done, total, final=True)（强制发一次）。
    """
    if loop is None or report_coro is None:
        return lambda done, total=None, final=False: None

    state = {"last": None}
    lock = threading.Lock()

    def progress(done, total=None, final=False):
        now = time.monotonic()
        with lock:
            due = final or state["last"] is None or (now - state["last"]) >= min_interval
            if due:
                state["last"] = now
        if not due:
            return
        try:
            _dispatch(report_coro(done, total), loop)
        except Exception:
            # 尽力而为：上报失败（loop 已关/客户端已断）绝不影响正在跑的数据库操作
            pass

    return progress
