# -*- coding: utf-8 -*-
"""数据源管理 GUI 的行样式：同项目同色（底色只表示项目）

列表按 dir_path 排序，同项目的数据源本就挨在一起，一个底色就是一个项目分组。

**三个视觉通道各管一件事，这是本文件守护的设计：**
    底色（DIR_COLORS 第 i 个）= 哪个项目
    格栅线（_GRID_COLOR）     = 行与行的边界
    字色（TEXT_COLOR）        = 统一深色，不携带任何分组信息

曾经底色兼过两份工、字色也兼过一份，都不成立，别再改回去：
  · 字色跟目录走：2.28~3.11:1，不达 WCAG AA 的 4.5:1，且每行字色都在变，
    眼睛找不到落点。要改回去的先算：7 个色相只有深蓝/深紫能在白底过 AA。
  · 底色组内隔行深浅（DIR_COLORS_DEEP）："同项目同色"的信号被行号稀释，
    用户反馈"同项目颜色虽然同色但有深浅"。行边界已交还给加深的格栅线。

本文件最主要的一条断言：TEXT_COLOR 对每一种底色、对白底，对比度都达 4.5:1。
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

# 设计契约：每个 tag 只设置背景一个维度。
LAYER_OPTIONS = {}


def _options_of(tag):
    if tag in LAYER_OPTIONS:
        return LAYER_OPTIONS[tag]
    if re.fullmatch(r"dir\d+", tag):
        return {"background"}
    raise AssertionError(f"未登记的 tag: {tag!r}")


def _lum(hexs):
    r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ---------------------------------------------------------------------------
# 调色板：一串项目底色，互不重复
# ---------------------------------------------------------------------------

def test_dir_colors_are_plain_hex_backgrounds():
    colors = GUI.App.DIR_COLORS
    assert colors, "DIR_COLORS 不能为空"
    for c in colors:
        assert isinstance(c, str), f"每一项就是一个背景色串，得到 {c!r}"
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", c), f"不是 #RRGGBB: {c!r}"


def test_dir_colors_are_distinct():
    """底色撞色会让两个项目看起来是一组。"""
    lowered = [c.lower() for c in GUI.App.DIR_COLORS]
    assert len(set(lowered)) == len(lowered), f"背景色重复: {lowered}"


def test_no_deep_shade_list_anymore():
    """组内深浅已删掉：底色只表示项目，行边界归格栅线。

    谁把 DIR_COLORS_DEEP 加回来，这条立刻红——那会让"同项目同色"的信号被行号
    稀释（用户明确反馈过"同项目颜色虽然同色但有深浅"）。
    """
    assert not hasattr(GUI.App, "DIR_COLORS_DEEP"), (
        "DIR_COLORS_DEEP 又回来了：底色开始兼职行号，项目分组信号会被稀释")


# ---------------------------------------------------------------------------
# 正文字色：对每种底色、对白底都要达 WCAG AA
# ---------------------------------------------------------------------------

def test_text_color_is_readable_on_every_background_and_white():
    """4.5:1 是硬线——分组要看得见，字更不能看不清。"""
    for bg in list(GUI.App.DIR_COLORS) + ["#FFFFFF"]:
        ratio = _contrast(GUI.App.TEXT_COLOR, bg)
        assert ratio >= 4.5, (
            f"正文 {GUI.App.TEXT_COLOR} 压在 {bg} 上只有 {ratio:.2f}:1，"
            f"低于 WCAG AA 的 4.5:1")


def test_text_color_is_dark_in_absolute_terms():
    """别改成浅色字：相对亮度单独兜一道，防止底色集合变化时漏判。"""
    assert _lum(GUI.App.TEXT_COLOR) < 0.25, (
        f"TEXT_COLOR {GUI.App.TEXT_COLOR} 太亮，压在浅底上读不清")


# ---------------------------------------------------------------------------
# 格栅线：行边界靠它，必须真的看得见
# ---------------------------------------------------------------------------

def test_grid_line_is_visible_against_every_background():
    """线是行边界的唯一载体，对比度必须够——这是 #BCC9DC（1.19:1）换掉的教训。

    用对比度而不用亮度差：亮度差 0.1 在浅底上几乎看不出来，却能让一个
    "看起来有测试"的门槛通过。
    """
    for bg in list(GUI.App.DIR_COLORS) + ["#FFFFFF"]:
        ratio = _contrast(GUI._GRID_COLOR, bg)
        assert ratio >= 1.8, (
            f"格栅线 {GUI._GRID_COLOR} 压在 {bg} 上只有 {ratio:.2f}:1，"
            f"行边界会看不见（用户反馈过'行与行之间没有明显的分界'）")


def test_grid_line_is_lighter_than_the_text():
    """线不能抢字：可见度要够，但不能压过正文。"""
    for bg in list(GUI.App.DIR_COLORS):
        assert _contrast(GUI._GRID_COLOR, bg) < _contrast(GUI.App.TEXT_COLOR, bg), (
            f"格栅线在 {bg} 上比正文还显眼，抢字了")


# ---------------------------------------------------------------------------
# tag 选择：一个目录一个底色
# ---------------------------------------------------------------------------

def test_row_tags_picks_one_background_per_directory():
    for i in range(len(GUI.App.DIR_COLORS)):
        assert GUI.App._row_tags(i) == [f"dir{i}"]


def test_row_tags_never_set_more_than_background():
    """字色/字重/第二层背景谁加回来，这条立刻红。"""
    n = len(GUI.App.DIR_COLORS)
    checked = 0
    for color_idx in range(n):
        for t in GUI.App._row_tags(color_idx):
            assert _options_of(t) == {"background"}, f"tag {t!r} 设置了背景之外的选项"
            checked += 1
    assert checked >= 5, f"组合数太少（{checked}），断言可能没跑开"


# ---------------------------------------------------------------------------
# 真实注册结果与设计契约一致（需要 Tk；没有显示环境时跳过）
# ---------------------------------------------------------------------------

@pytest.fixture
def tree(tk_root):
    from tkinter import ttk
    tv = ttk.Treeview(tk_root)
    try:
        yield tv
    finally:
        tv.destroy()


class _TagHost:
    """只提供 _configure_row_tags 用到的一个属性。

    不构造真正的 App：那需要配置库、屏幕几何和一次后台 reload，
    而这里要验的只是 tag 注册那一小段接线。
    """
    DIR_COLORS = GUI.App.DIR_COLORS

    def __init__(self):
        # 必须用 MethodType 绑：未绑定函数存成实例属性不会触发描述符协议，
        # 调用时会少传一个 self。
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
    host = _TagHost()
    host._configure_row_tags(tree)

    n = len(GUI.App.DIR_COLORS)
    expected = {f"dir{i}" for i in range(n)}
    baseline = tree.tag_configure("__never_registered__")   # 全空串 = 默认记录
    registered = {t for t in expected if _explicit_options(tree, t, baseline)}
    assert registered == expected, f"有 tag 没注册上: {expected - registered}"

    # 每个 tag 只设置背景这一个选项——字色/字重/第二层背景谁加回来，这条立刻红
    for tag in sorted(expected):
        assert _explicit_options(tree, tag, baseline) == _options_of(tag), \
            f"tag {tag!r} 设置的不是设计契约里那一个选项"


def test_registered_tag_values(tree):
    """选项对还不够，值也得对：配错色，分层正确也白搭。"""
    host = _TagHost()
    host._configure_row_tags(tree)
    for i, bg in enumerate(GUI.App.DIR_COLORS):
        assert tree.tag_configure(f"dir{i}")["background"] == bg
