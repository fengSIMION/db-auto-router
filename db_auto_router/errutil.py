# -*- coding: utf-8 -*-
"""轻量级工具：瞬态错误判定 / 安全的游标上下文管理器。
聚合原本散落在 sqlguard、introspection、readengine、writeengine、datasources、pool 中的同类片段。"""

__author__ = "fengkeju"
from contextlib import contextmanager


@contextmanager
def safe_cursor(conn):
    """带连接保护的游标上下文管理器。退出时无论正常还是异常都会尝试关闭游标，
    屏蔽底层驱动的偶发报错（如达梦 'Not Open'），调用方无需手写 try/except."""
    cursor = None
    try:
        cursor = conn.cursor()
        yield cursor
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass


_TRANSIENT_MARKERS = (
    "gone away", "lost connection", "connection refused", "connection reset",
    "timed out", "timeout", "deadlock", "lock wait timeout", "too many connections",
    "can't connect", "could not connect", "connection aborted", "broken pipe",
    "connection is closed", "server closed the thread", "eof",
    "not open",  # 达梦游标异常（原逻辑依赖重试恢复）
)


def is_transient_error(e: BaseException) -> bool:
    """判断是否为可重试的瞬时错误（连接断开/超时/死锁）。
    语法错误、权限不足等永久性错误返回 False——重试只会白白增加延迟。"""
    msg = str(e).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def safe_close(obj) -> None:
    """静默关闭任意具备 close() 方法的对象，吞掉所有非 SystemExit 类异常。
    用在清理路径上，避免次要异常掩盖主异常。"""
    if obj is None:
        return
    try:
        obj.close()
    except Exception:
        pass
