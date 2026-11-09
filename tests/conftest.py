# -*- coding: utf-8 -*-
"""整个测试进程共用一个 Tk root。

Windows 上同一进程里建第二个 Tk() 偶发失败（test_gui_row_styles.py 的实测：
5 次里 2 次建不起来），失败时注册级断言会被动 skip——而它们恰恰是最关键的几条。
GUI 相关的三个测试文件（行样式 / 列宽 / 格栅线）都要 Tk，各建各的 root 就会互相
踩；共用一个 root，且只在真的用到时才建（session 级，未用到不建）。
"""

__author__ = "fengkeju"
import pytest


@pytest.fixture(scope="session")
def tk_root():
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as e:                      # pragma: no cover - 依赖显示环境
        pytest.skip(f"Tk 不可用，跳过需要显示的断言: {type(e).__name__}: {e}")
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()
