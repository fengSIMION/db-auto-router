# -*- coding: utf-8 -*-
"""数据源管理 GUI 的行样式分层：背景 / 字色 / 字重

背景：原来只有背景按目录分色（同目录同色），字色只有两种特例——默认数据源整行
绿字、免人工审核整行橙字。现在字色一律按目录走，默认数据源改为加粗；
免审核行也不再单独给橙色——同一项目就同色，审核状态只看「审核」列的文字。

**核心设计约束（本文件最主要的那条断言）：同一行的多个 tag 绝不设置同一个选项。**
ttk 的 tag 优先级（tag_configure 的先后顺序、item tag 列表里的位置）跨版本/主题
并不稳定，靠"后设置的赢"去叠颜色，等于把渲染结果交给运气。所以分三层，每层只
设置一个维度：
    背景   dir{i}（按目录分色）｜ zebra（只有一个目录时隔行，退回老观感）
    字色   fgdir{i}（按目录分色，所有行一律跟随）
    字重   default_row（加粗）
三层互不重叠，任意组合的渲染结果都是确定的，与优先级规则无关。
"""

__author__ = "fengkeju"
import os
import re
import sys
from types import MethodType

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import manage_datasources_gui as GUI  # noqa: E402

# 设计契约：每个 tag 只设置一个维度的选项。测试用同一份口径去校验注册结果，
# 注册代码若给某个 tag 多加了一个选项，这里立刻红。
LAYER_OPTIONS = {
    "zebra": {"background"},
    "default_row": {"font"},
}


def _options_of(tag):
    if tag in LAYER_OPTIONS:
        return LAYER_OPTIONS[tag]
    if re.fullmatch(r"dir\d+", tag):
        return {"background"}
    if re.fullmatch(r"fgdir\d+", tag):
        return {"foreground"}
    raise AssertionError(f"未登记的 tag: {tag!r}")


# ---------------------------------------------------------------------------
# 调色板：必须是 (背景, 字色) 对
# ---------------------------------------------------------------------------

def test_dir_colors_are_background_foreground_pairs():
    colors = GUI.App.DIR_COLORS
    assert colors, "DIR_COLORS 不能为空"
    for entry in colors:
        assert isinstance(entry, tuple) and len(entry) == 2, \
            f"每一项必须是 (背景, 字色) 二元组，得到 {entry!r}"
        for c in entry:
            assert re.fullmatch(r"#[0-9A-Fa-f]{6}", c), f"不是 #RRGGBB: {c!r}"


def test_dir_colors_backgrounds_and_foregrounds_are_each_distinct():
    """背景撞色会让两个项目看起来是一组；字色撞色则让"字色跟着目录走"失去意义。"""
    colors = GUI.App.DIR_COLORS
    bgs = [c[0].lower() for c in colors]
    fgs = [c[1].lower() for c in colors]
    assert len(set(bgs)) == len(bgs), f"背景色重复: {bgs}"
    assert len(set(fgs)) == len(fgs), f"字色重复: {fgs}"


def test_dir_foregrounds_are_dark_enough_to_read_on_their_background():
    """字色压在浅底上必须读得清：用相对亮度粗判，字色不得比底色还亮。"""
    def lum(hexs):
        r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    for bg, fg in GUI.App.DIR_COLORS:
        assert lum(fg) < lum(bg), f"字色 {fg} 不够深，压在 {bg} 上读不清"


# ---------------------------------------------------------------------------
# 核心不变式：一行的 tag 之间，选项互不重叠
# ---------------------------------------------------------------------------

def test_row_tags_never_set_the_same_option_twice():
    """任意 (目录/默认/隔行) 组合，都不许出现两个 tag 抢同一个选项。"""
    n = len(GUI.App.DIR_COLORS)
    checked = 0
    for banded in (True, False):
        for is_default in (0, 1):
            for color_idx in range(n):
                for row_index in (0, 1, 5):
                    tags = GUI.App._row_tags(color_idx, is_default, banded, row_index)
                    assert tags, f"空 tag 列表: {color_idx},{is_default}"
                    seen = {}
                    for t in tags:
                        for opt in _options_of(t):
                            assert opt not in seen, (
                                f"选项 {opt} 同时被 {seen[opt]!r} 和 {t!r} 设置"
                                f"（banded={banded} is_default={is_default} "
                                f"color_idx={color_idx}）")
                            seen[opt] = t
                    checked += 1
    assert checked >= 50, f"组合数太少（{checked}），断言可能没跑开"


# ---------------------------------------------------------------------------
# 各状态的 tag 选择
# ---------------------------------------------------------------------------

def test_row_tags_banded_uses_dir_background_and_foreground():
    tags = GUI.App._row_tags(2, 0, True, 7)
    assert tags == ["dir2", "fgdir2"]


def test_row_tags_single_directory_falls_back_to_zebra():
    """只有一个目录时背景没有区分意义，退回隔行浅蓝（老观感）。"""
    assert GUI.App._row_tags(0, 0, False, 0) == ["zebra", "fgdir0"]
    assert GUI.App._row_tags(0, 0, False, 1) == ["fgdir0"]


def test_row_tags_default_gets_bold_layer_only():
    """默认数据源：加粗，且不碰字色（字色归目录那一层）。"""
    tags = GUI.App._row_tags(1, 1, True, 0)
    assert tags == ["dir1", "fgdir1", "default_row"]
    assert _options_of("default_row") == {"font"}


def test_row_tags_no_confirm_row_uses_the_same_project_color():
    """免审核行不再有特殊字色：与同项目其它行完全同色。

    审核状态从此只看「审核」列的文字（✓ / ✗ 免），不再靠整行橙色表达——
    用户明确要求"同一项目同色"。
    """
    assert GUI.App._row_tags(3, 0, True, 0) == ["dir3", "fgdir3"]
    assert GUI.App._row_tags(3, 0, True, 1) == ["dir3", "fgdir3"]


def test_row_tags_default_row_bold_layer_stacks_on_project_color():
    tags = GUI.App._row_tags(3, 1, True, 0)
    assert tags == ["dir3", "fgdir3", "default_row"]


# ---------------------------------------------------------------------------
# 真实注册结果与设计契约一致（需要 Tk；没有显示环境时跳过）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tk_root():
    """整个文件共用一个 Tk root。

    不用函数级：同一进程里建第二个 Tk() 在 Windows 上偶发失败（实测 5 次里
    2 次建不起来），会让注册级断言随机跳过——而它正是本文件最关键的两条。
    """
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as e:                      # pragma: no cover - 依赖显示环境
        pytest.skip(f"Tk 不可用，跳过注册级断言: {type(e).__name__}: {e}")
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


@pytest.fixture
def tree(tk_root):
    from tkinter import ttk
    tv = ttk.Treeview(tk_root)
    try:
        yield tv
    finally:
        tv.destroy()


class _TagHost:
    """只提供 _configure_row_tags 用到的两个属性。

    不构造真正的 App：那需要配置库、屏幕几何和一次后台 reload，
    而这里要验的只是 tag 注册那一小段接线。
    """
    DIR_COLORS = GUI.App.DIR_COLORS

    def __init__(self, bold_font):
        self._f_bold = bold_font
        # 必须用 MethodType 绑：未绑定函数存成实例属性不会触发描述符协议，
        # 调用时会少传一个 self。
        # 绑在实例上而不是类体里：被测方法不存在时，类体引用会让整个文件
        # 收集期报错，其它用例就连"单独失败"的机会都没有了。
        self._configure_row_tags = MethodType(GUI.App._configure_row_tags, self)


def _explicit_options(tree, tag, baseline):
    """这个 tag 到底设置了哪些选项。

    `tag_configure(tag)` 会把**所有**选项连默认值一起返回（没设过的是空串），
    直接拿返回值判断"设了没设"会得出"设了六个"的错误结论。所以拿一个从未注册过的
    tag 当基线做差——差集才是真正设置上去的选项。
    """
    cur = tree.tag_configure(tag)
    return {k for k in cur if cur[k] != baseline.get(k)}


def test_registered_tags_match_the_layer_design(tree):
    from tkinter import font as tkfont
    host = _TagHost(tkfont.Font(font=("Microsoft YaHei UI", 10, "bold")))
    host._configure_row_tags(tree)

    n = len(GUI.App.DIR_COLORS)
    expected = {"zebra", "default_row"}
    expected |= {f"dir{i}" for i in range(n)}
    expected |= {f"fgdir{i}" for i in range(n)}
    baseline = tree.tag_configure("__never_registered__")   # 全空串 = 默认记录
    registered = {t for t in expected if _explicit_options(tree, t, baseline)}
    assert registered == expected, f"有 tag 没注册上: {expected - registered}"

    # 每个 tag 只设置自己那一层的一个选项——这是"渲染不依赖 tag 优先级"的根据。
    # 谁要是给 default_row 添个 foreground，这条立刻红
    for tag in sorted(expected):
        assert _explicit_options(tree, tag, baseline) == _options_of(tag), \
            f"tag {tag!r} 设置的不是设计契约里那一个选项"


def test_registered_tag_values(tree):
    """选项对还不够，值也得对：配错色、字重没加上，分层正确也白搭。"""
    from tkinter import font as tkfont
    bold = tkfont.Font(font=("Microsoft YaHei UI", 10, "bold"))
    host = _TagHost(bold)
    host._configure_row_tags(tree)

    colors = GUI.App.DIR_COLORS
    for i, (bg, fg) in enumerate(colors):
        assert tree.tag_configure(f"dir{i}")["background"] == bg
        assert tree.tag_configure(f"fgdir{i}")["foreground"] == fg
    # zebra 沿用第一个目录色的底：单目录时观感和改动前一致
    assert tree.tag_configure("zebra")["background"] == colors[0][0]
    # 默认行加粗：既验 tag 引用的就是这个字体对象，也验该字体真的是 bold
    # （不靠"我传了个叫 bold 的 Font 进去"自证）
    assert tree.tag_configure("default_row")["font"] == str(bold)
    assert bold.actual()["weight"] == "bold"
