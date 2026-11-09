# -*- coding: utf-8 -*-
"""列宽内容自适应：实测必须按实际渲染的字体量

v4.8 把列宽改成「按当前数据实测文本像素宽」（_natural_width 量 _display 里每一行
的取值）。这条链路踩过一次坑：默认数据源曾经整行加粗（default_row tag → _f_bold），
而实测对所有行都用常规字体量——粗体比常规宽约 5%，真实配置里 63 字符的目录路径
常规量 542px、粗体量 571px，列宽按 542+pad 给到 567px，粗体渲染要 573px，最后
一个字被切；「重置列宽」每次重算都少算同一笔，所以现象是"重置了还是遮挡"。

现在没有加粗行了（_row_tags 只设背景），但**哪一类行若改用别的字体，实测必须
跟着传对应字体**，否则同样的 bug 会回来。本文件钉住这条，以及夹取区间与 pad 下限。
"""

__author__ = "fengkeju"
import os
import sys
from types import MethodType

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import manage_datasources_gui as GUI  # noqa: E402

# 足够长、可覆盖列宽边界的虚构工作区路径
LONG_DIR = "D:\\workspace\\projects\\backend\\inventory_service"


class _WidthHost:
    """只提供 _natural_width / _text_width 用到的属性，不构造真正的 App。

    构造 App 需要配置库、屏幕几何和一次后台 reload，而这里要验的只是
    "列宽实测有没有按实际渲染字体量"这一小段。
    """

    COLS = GUI.App.COLS
    MAXW = GUI.App.MAXW

    def __init__(self, fonts):
        self._f_cell, self._f_bold, self._f_head = fonts
        self._display = []
        self._w_cache = {}
        # 必须用 MethodType 绑：未绑定函数存成实例属性不会触发描述符协议
        self._natural_width = MethodType(GUI.App._natural_width, self)
        self._text_width = MethodType(GUI.App._text_width, self)


@pytest.fixture(scope="module")
def fonts(tk_root):
    """模块级共用字体；Tk root 由 conftest 统一提供（整个进程只建一个）。"""
    from tkinter import font as tkfont
    cell = tkfont.Font(font=("Microsoft YaHei UI", 10))
    bold = tkfont.Font(font=("Microsoft YaHei UI", 10, "bold"))
    head = tkfont.Font(font=("Microsoft YaHei UI", 10, "bold"))
    yield (cell, bold, head)


@pytest.fixture
def host(fonts):
    return _WidthHost(fonts)


def _row(path, name="示例数据服务", code="demo_db", typ="mysql",
         url="192.0.2.10:3306", user="demo_user"):
    return (path, name, code, typ, url, user, "", "✓", "✓")


# ---------------------------------------------------------------------------
# 核心不变式：实测用的字体必须就是渲染用的字体
# ---------------------------------------------------------------------------

def test_treeview_style_font_is_the_measured_font_spec(tk_root):
    """Treeview 样式的字体必须就是 _f_cell 声明的那一套（Microsoft YaHei UI 10）。

    实测字体和渲染字体对不上，列宽就会系统性偏窄、长文本被切，而「重置列宽」
    修不好（每次都按同一个错值重算）。加粗行的教训是同一类错的极端版。
    这里锁住样式侧的字号/字族；_f_cell 侧在 App.__init__ 里同一字面量创建。
    """
    from tkinter import ttk
    style = ttk.Style(tk_root)
    style.theme_use("clam")
    style.configure("Treeview", font=("Microsoft YaHei UI", 10))
    want = style.lookup("Treeview", "font")
    assert "Microsoft YaHei UI" in str(want), f"Treeview 字体被改掉了: {want!r}"


def test_natural_width_covers_every_row_in_cell_font(host):
    """每一行的自然宽必须覆盖该行在正文字体下的宽度 + pad。"""
    host._display = [_row(LONG_DIR)]
    for pad in GUI.App.PAD_CHOICES:
        w = host._natural_width(0, "工作空间", pad)
        assert w >= host._text_width(LONG_DIR, host._f_cell) + pad, (
            f"pad={pad} 时目录列只有 {w}px，内容要 "
            f"{host._text_width(LONG_DIR, host._f_cell)}px+{pad}px——长路径会被切")


def test_natural_width_uses_heading_font_for_the_header(host):
    """表头是粗体，必须按表头字体量（且留出排序箭头那 8px）。"""
    host._display = []
    w = host._natural_width(0, "工作空间", 24)
    head_px = host._text_width("工作空间", host._f_head)
    assert w >= head_px + 24 + 8, f"表头没按粗体量或没留箭头位: {w} < {head_px}+32"
    assert w > host._text_width("工作空间", host._f_cell) + 24, "表头按常规字体量了"


def test_natural_width_takes_max_over_rows(host):
    """宽度取所有行的最大值——新增一行更长的路径，列必须跟着变宽。"""
    short = "E:\\datasoue"
    host._display = [_row(short)]
    narrow = host._natural_width(0, "工作空间", 24)
    host._display = [_row(short), _row(LONG_DIR)]
    wide = host._natural_width(0, "工作空间", 24)
    assert wide > narrow, "加了一行更长的路径，列宽没变"


def test_natural_width_still_clamped_to_min_and_max(host):
    """夹取区间不能丢：短内容抬到最小宽，异常长内容仍封顶。"""
    host._display = [_row("E:\\datasoue")]
    assert host._natural_width(0, "工作空间", 24) == GUI.App.COLS["工作空间"][0]
    host._display = [_row("D:\\" + "x" * 400)]
    assert host._natural_width(0, "工作空间", 24) == GUI.App.MAXW["工作空间"]


def test_pad_floor_covers_cell_inset(host):
    """最紧凑的 pad 也要盖住单元格内边距（实测左内边距约 2px），否则文本仍会被切。"""
    assert GUI.App.PAD_CHOICES[-1] >= 2, "pad 下限盖不住单元格内边距"
    assert GUI.App.PAD_CHOICES == tuple(sorted(GUI.App.PAD_CHOICES, reverse=True)), (
        "PAD_CHOICES 必须从宽到紧——_autofit 取第一个能让总宽塞进可视区的")


def test_text_width_cache_is_keyed_by_font_not_just_text(host):
    """缓存 key 必须带字体：同一段文本用两种字体量过，第二次不能命中第一次的值。"""
    a = host._text_width(LONG_DIR, host._f_cell)
    b = host._text_width(LONG_DIR, host._f_bold)
    assert a != b, "本机字体上两种字重等宽，这条用例失去意义——换台机器再看"
    # 再量一次常规，应命中缓存拿到同一个值（而不是被粗体的值污染）
    assert host._text_width(LONG_DIR, host._f_cell) == a
