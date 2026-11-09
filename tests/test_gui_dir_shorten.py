# -*- coding: utf-8 -*-
"""目录列的中段省略：_shorten_dir

大量项目路径通常有很长的共同前缀（如 D:\\workspace\\projects\\），每行开头
一多半都一样，差异全挤在末尾，眼睛扫不出区别。中段省略只留首尾各两段：
D:\\workspace\\…\\platform\\sample_service，首两段认得出是哪棵树、末两段认得出
是哪个项目，中间的 projects 之流正是
最没信息量的那段。

只改显示文本：self._rows 里始终是完整路径，编辑/复制/删除/测试都读它（_sel）。
"""

__author__ = "fengkeju"
import os
from types import MethodType
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import manage_datasources_gui as GUI  # noqa: E402

SHORTEN = GUI.App._shorten_dir


# ---------------------------------------------------------------------------
# 段数足够多才省略：留首两段 + 末两段，中间换 …
# ---------------------------------------------------------------------------

def test_long_path_keeps_head_and_tail_only():
    got = SHORTEN(r"D:\workspace\projects\platform\sample_service")
    assert got == r"D:\workspace\…\platform\sample_service"


def test_four_segment_path_is_left_alone():
    """4 段不动：首两段+末两段正好等于全部，插 … 只会变更长、一段都省不下。"""
    p = r"D:\workspace\projects\demo_service"
    assert SHORTEN(p) == p


def test_省略后保留首段与末段_identifying_parts():
    """首两段（盘符+工作区根）和末两段（项目目录）必须原样留下。"""
    p = r"D:\workspace\projects\backend\inventory_service"
    got = SHORTEN(p)
    assert got.startswith("D:\\workspace\\")
    assert got.endswith("backend\\inventory_service")
    assert got.count("…") == 1


def test_short_paths_are_untouched():
    """段数 ≤3 本来就短，动它只会丢信息。"""
    for p in (r"D:\workspace\personal",
              r"E:\datasoue",
              r"D:\a\b"):
        assert SHORTEN(p) == p, p


def test_values_without_separator_are_untouched():
    for v in ("ORACLE工作空间", "*（任意目录兜底）", "", "mysql"):
        assert SHORTEN(v) == v, v


def test_省略总是让字符串变短():
    p = r"D:\workspace\projects\platform\sample_service"
    assert len(SHORTEN(p)) < len(p)


def test_forward_slash_paths_keep_their_separator():
    """POSIX 风格路径不能被改写成反斜杠。

    前导空段是根目录，占掉一个"首段"名额——/home 之后就是 …，这是对的：
    根和 home 已经认出这是哪棵树，中间几层正是要省掉的。
    """
    got = SHORTEN("/home/user/projects/foo/bar/baz")
    assert got == "/home/…/bar/baz"
    assert "\\" not in got


def test_five_segments_is_the_threshold():
    """5 段才是省略的门槛：5 段起才真有中间段可省，4 段省不下（见上一个用例）。"""
    five = r"D:\ws\a\b\c"
    assert SHORTEN(five) == r"D:\ws\…\b\c"
    four = r"D:\ws\a\b"
    assert SHORTEN(four) == four


def test_non_string_input_passes_through():
    """配置库里混进 None/数字时原样返回，不崩、也不硬转成字符串。"""
    assert SHORTEN(None) is None
    assert SHORTEN(123) == 123


# ---------------------------------------------------------------------------
# 整列版：碰撞保护——两条不同目录不能显示成同一串
# ---------------------------------------------------------------------------

class _ShortenHost:
    """只绑定 _shorten_dirs 用到的两个方法，不构造真正的 App。

    _shorten_dir 是 staticmethod、_shorten_dirs 是实例方法，两种绑法都要对上：
    前者存成实例属性（不触发描述符协议，调用时不传 self），后者必须 MethodType
    手动绑，否则会少传一个 self。
    """
    def __init__(self):
        self._shorten_dir = GUI.App._shorten_dir
        self._shorten_dirs = MethodType(GUI.App._shorten_dirs, self)


def _dirs(values):
    return _ShortenHost()._shorten_dirs(values)


def test_column_version_elides_each_value():
    dirs = [r"D:\ws\projects\platform\sample_service",
            r"D:\ws\projects\backend\inventory_service",
            r"E:\datasoue"]
    got = _dirs(dirs)
    assert got == [r"D:\ws\…\platform\sample_service",
                   r"D:\ws\…\backend\inventory_service",
                   r"E:\datasoue"]


def test_colliding_values_fall_back_to_full_paths():
    """中间段不同、首尾两段相同的两条目录，省略后会撞成同一串——必须整体退回。"""
    dirs = [r"D:\ws\dept\x\scm", r"D:\ws\projects\x\scm", r"D:\ws\a\b\c"]
    got = _dirs(dirs)
    assert got[0] == r"D:\ws\dept\x\scm", "撞车的两条必须退回完整路径"
    assert got[1] == r"D:\ws\projects\x\scm"
    # 没撞车的照常省略（同进同退只针对撞车那一组）
    assert got[2] == r"D:\ws\…\b\c"


def test_identical_values_do_not_count_as_collision():
    """同一条目录出现多行是分组的常态，不能算撞车。"""
    dirs = [r"D:\ws\projects\x\demo_service"] * 3
    got = _dirs(dirs)
    assert got == [r"D:\ws\…\x\demo_service"] * 3


def test_column_version_never_changes_the_number_of_rows():
    dirs = [r"D:\a\b\c\d", r"E:\x", "ORACLE工作空间", r"D:\ws\a\b\c"]
    assert len(_dirs(dirs)) == len(dirs)
