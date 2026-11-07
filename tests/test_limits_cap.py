# -*- coding: utf-8 -*-
"""调用方可覆盖参数的硬顶

背景：query(max_rows=...) / query(timeout=...) / export_csv(max_rows=..., timeout=...)
这几个参数是**模型自己决定传什么**的。原实现是
    effective = max_rows if (max_rows and max_rows > 0) else MAX_RESULT_ROWS
模型传 max_rows=10000000 就照单全收——一千万行会全部拉进内存、再整份塞进 MCP
响应，直接把这个会话撑爆。export 侧同一个洞：EXPORT_MAX_ROWS 本是防爆盘的硬顶，
但 max_rows 参数能把它覆盖到更大，等于没有顶。timeout 同理，能传任意大。

修复方向：所有"调用方可覆盖"的行数与超时，一律夹到一个绝对天花板内；
夹到顶时**如实告知调用方**（返回值带 warning），不默默照做、也不静默改小。

本文件用 pytest 收集（tests/ 下另外三个是 print+chk 脚本，pytest 收集不到）。
"""

__author__ = "fengkeju"
import importlib
import os
import sys

# 按本文件位置定位仓库根（tests/ 的上一级），换机器/换目录都不用改这里
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db_auto_router.config as config

_CAP_ENV_KEYS = ("MCP_QUERY_MAX_ROWS_CAP", "MCP_QUERY_TIMEOUT_MAX")


@pytest.fixture
def reload_config():
    """改环境变量后重载 config；用例结束后恢复原环境并重载，避免污染后续用例。

    不用 monkeypatch：它的 teardown 与本 fixture 的 teardown 顺序不确定，
    可能出现"环境已恢复、模块常量还是被改过的值"的中间态。
    """
    before = {k: os.environ.get(k) for k in _CAP_ENV_KEYS}
    try:
        yield
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)


# ---------------------------------------------------------------------------
# clamp_override 的语义
# ---------------------------------------------------------------------------

def test_none_uses_default():
    assert config.clamp_override(None, 1000, 50000) == (1000, False)


def test_zero_and_negative_use_default():
    assert config.clamp_override(0, 1000, 50000) == (1000, False)
    assert config.clamp_override(-5, 1000, 50000) == (1000, False)


def test_value_below_ceiling_is_honored_unchanged():
    assert config.clamp_override(500, 1000, 50000) == (500, False)


def test_value_above_ceiling_is_clamped_and_flagged():
    # 这条就是本文件存在的理由：10_000_000 不许照单全收
    assert config.clamp_override(10_000_000, 1000, 50000) == (50000, True)


def test_exactly_at_ceiling_is_not_clamped():
    assert config.clamp_override(50000, 1000, 50000) == (50000, False)


def test_ceiling_plus_one_is_clamped():
    assert config.clamp_override(50001, 1000, 50000) == (50000, True)


def test_timeout_uses_the_same_semantics():
    assert config.clamp_override(99999, 30, 600) == (600, True)
    assert config.clamp_override(60, 30, 600) == (60, False)
    assert config.clamp_override(None, 30, 600) == (30, False)


# ---------------------------------------------------------------------------
# 环境变量接线：拼错变量名 = 旋钮静默失效，必须被测试钉住
# ---------------------------------------------------------------------------

def test_cap_defaults(reload_config):
    for k in _CAP_ENV_KEYS:
        os.environ.pop(k, None)
    importlib.reload(config)
    assert config.QUERY_MAX_ROWS_CAP == 50000
    assert config.QUERY_TIMEOUT_MAX == 600


def test_cap_env_override(reload_config):
    # 取值必须高于各自默认值，否则会被"硬顶不得低于默认值"的不变式抬回去，
    # 测的就不是环境变量接线了（那条不变式由下面两个用例单独覆盖）。
    os.environ["MCP_QUERY_MAX_ROWS_CAP"] = "7777"
    os.environ["MCP_QUERY_TIMEOUT_MAX"] = "45"
    importlib.reload(config)
    assert config.QUERY_MAX_ROWS_CAP == 7777
    assert config.QUERY_TIMEOUT_MAX == 45


# ---------------------------------------------------------------------------
# 不变式：硬顶不得低于默认值，否则默认查询会绕过硬顶
# ---------------------------------------------------------------------------

def test_rows_cap_below_default_is_raised_to_default(reload_config):
    os.environ.pop("MCP_MAX_RESULT_ROWS", None)
    os.environ["MCP_QUERY_MAX_ROWS_CAP"] = "10"
    importlib.reload(config)
    assert config.MAX_RESULT_ROWS == 1000
    assert config.QUERY_MAX_ROWS_CAP == 1000


def test_timeout_cap_below_default_is_raised_to_default(reload_config):
    os.environ.pop("MCP_READ_TIMEOUT", None)
    os.environ["MCP_QUERY_TIMEOUT_MAX"] = "5"
    importlib.reload(config)
    assert config.READ_TIMEOUT == 30
    assert config.QUERY_TIMEOUT_MAX == 30
