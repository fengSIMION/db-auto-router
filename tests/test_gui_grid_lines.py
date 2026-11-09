# -*- coding: utf-8 -*-
"""数据源管理 GUI 的格栅线：行线 + 列线

同一目录下相邻几行同色（DIR_COLORS 按目录分组），没有边界线就糊成一片，
19 条配置扫下来很累。格栅线因此不是装饰，是长列表可读性的前提。

ttk 不提供格栅选项，实现是往 item layout 里加 image 元素（_install_grid_style）：
横向 8x1、纵向 1x64 的纯色图，靠 image 元素的平铺铺满整行整列。这里钉住四件
容易被后人改坏的事：
  1. 线元素确实装进了 item layout，且**在文字之前**（同层后画的盖先画的）
  2. 线色对每一种目录底色都明显更深——压在浅底上看不见等于没画
  3. 派生样式仍继承 "Treeview" 的行高/字体/选中色，不能为格栅改了观感
  4. 重复安装不炸（同一解释器里 element_create 会抛 already exists）
"""

__author__ = "fengkeju"
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import manage_datasources_gui as GUI  # noqa: E402


def _flatten(layout):
    """把 style.layout() 的嵌套结构摊平成一串元素名（保序）。"""
    out = []
    for entry in layout:
        name, opts = entry if isinstance(entry, tuple) else (entry, {})
        out.append(name)
        out.extend(_flatten(opts.get("children", [])))
    return out


def _lum(hexs):
    r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ---------------------------------------------------------------------------
# 线色：对每一种底色都必须看得见，又不能抢字
# ---------------------------------------------------------------------------

def test_grid_color_is_darker_than_every_row_background():
    """线压在底色上必须看得见——用对比度判，不用亮度差。

    亮度差门槛放过过 #BCC9DC：对最浅的底色只有 1.19:1，几乎等于没画，用户反馈
    "行与行之间没有明显的分界，感觉一团数据"。行边界现在全靠这条线（底色只表示
    项目，组内不再深浅），所以线必须真的可见。
    """
    line = GUI._GRID_COLOR
    for bg in list(GUI.App.DIR_COLORS) + ["#FFFFFF"]:
        ratio = _contrast(line, bg)
        assert ratio >= 1.8, (
            f"格栅线 {line} 压在 {bg} 上只有 {ratio:.2f}:1，行边界会看不见")
        # 但不能压过正文——线是边界，不是内容
        assert ratio < _contrast(GUI.App.TEXT_COLOR, bg), (
            f"格栅线 {line} 在 {bg} 上比正文还显眼，抢字了")


def test_grid_color_is_a_valid_hex():
    import re
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", GUI._GRID_COLOR), GUI._GRID_COLOR


# ---------------------------------------------------------------------------
# 安装结果（需要 Tk）
# ---------------------------------------------------------------------------

@pytest.fixture
def grid_style(tk_root):
    """按 App.__init__ 的同一条路径装一次格栅，返回 (style, images)。"""
    from tkinter import ttk
    style = ttk.Style(tk_root)
    try:
        style.theme_use("clam")
    except Exception:                           # pragma: no cover
        pytest.skip("clam 主题不可用")
    style.configure("Treeview", rowheight=32, font=("Microsoft YaHei UI", 10),
                    background="#FFFFFF", fieldbackground="#FFFFFF")
    images = GUI._install_grid_style(style, tk_root)
    return style, images


def test_install_returns_images_to_keep_alive(grid_style):
    """图片必须返回给调用方持有，否则 GC 后元素成空引用、线时有时无。"""
    _style, images = grid_style
    assert len(images) == 2, f"应返回横线+纵线两张图，得到 {images!r}"


def test_item_layout_has_both_line_elements(grid_style):
    style, _images = grid_style
    names = _flatten(style.layout(GUI._GRID_STYLE + ".Item"))
    assert "Grid.hline" in names, f"item layout 里没有行线: {names}"
    assert "Grid.vline" in names, f"item layout 里没有列线: {names}"
    # 原有元素一个不能少——少一个文字/箭头就没了
    for keep in ("Treeitem.padding", "Treeitem.indicator", "Treeitem.text"):
        assert keep in names, f"item layout 丢了 {keep}: {names}"


def test_lines_are_drawn_below_the_text(grid_style):
    """同一层里后画的盖先画的：线必须在文字之前，否则文字被线压。"""
    style, _images = grid_style
    names = _flatten(style.layout(GUI._GRID_STYLE + ".Item"))
    assert names.index("Grid.hline") < names.index("Treeitem.text")
    assert names.index("Grid.vline") < names.index("Treeitem.text")


def test_heading_layout_also_has_the_vertical_line(grid_style):
    """表头不补纵线，格栅就只有下半截——表头是"敞口"的。"""
    style, _images = grid_style
    names = _flatten(style.layout(GUI._GRID_STYLE + ".heading"))
    assert "Grid.vline" in names, f"表头 layout 里没有列线: {names}"
    for keep in ("Treeheading.cell", "Treeheading.border", "Treeheading.text"):
        assert keep in names, f"表头 layout 丢了 {keep}: {names}"


def test_derived_style_keeps_inheriting_treeview_options(grid_style):
    """派生样式只改 layout：行高/字体仍继承 "Treeview"，不能为格栅改了观感。"""
    style, _images = grid_style
    assert style.lookup(GUI._GRID_STYLE, "rowheight") == 32
    assert "Microsoft YaHei UI" in str(style.lookup(GUI._GRID_STYLE, "font"))


def test_installing_twice_does_not_raise(tk_root):
    """同一解释器里装第二次：element_create 会抛 already exists，必须安静复用。"""
    from tkinter import ttk
    style = ttk.Style(tk_root)
    style.theme_use("clam")
    first = GUI._install_grid_style(style, tk_root)
    second = GUI._install_grid_style(style, tk_root)
    assert len(first) == 2 and len(second) == 2
    names = _flatten(style.layout(GUI._GRID_STYLE + ".Item"))
    assert "Grid.hline" in names and "Grid.vline" in names
