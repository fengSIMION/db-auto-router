# -*- coding: utf-8 -*-
"""Claude Code 工作空间发现与数据源解析——原 117–121、2244–2289 行

注意：本模块与 datasources 互相引用，破环方式为函数体内延迟 import datasources。
外部读取 _workspace_dir 必须用 `workspace._workspace_dir`（global 重绑定语义），
禁止 `from .workspace import _workspace_dir`（会冻结初始 None）。"""

from __future__ import annotations

__author__ = "fengkeju"
import asyncio
import os
import threading
from typing import TYPE_CHECKING, Dict, Any, Optional, List

# Context / Root 仅用于类型标注（Root 还是函数内局部注解，运行时不求值）。
# 放 TYPE_CHECKING 里，避免 GUI（manage_datasources_gui.py）为了改个数据源配置，
# 经 datasources → workspace 被拖入整个 mcp/mcp-types/httpx 依赖树（实测 import 10.8s → 0.3s）。
if TYPE_CHECKING:
    from mcp.server.mcpserver import Context
    from mcp_types import Root

from .errors import ToolFailure
from .logcfg import logger
from .util import normalize_path

# Claude Code 工作空间目录（从 initialize 握手阶段的 roots 请求中获取）
_workspace_dir = None
_workspace_lock = threading.Lock()
# 握手阶段是否已刷新过数据源缓存（目录确定后预热一次）
_workspace_primed = False
# roots 发现失败的重试计数。_workspace_primed 只在"成功问到 roots"之后才置 True，
# 失败不计完成——否则一次握手抖动就永久判死整个会话（见 _discover_workspace_dir）。
_workspace_attempts = 0
_WORKSPACE_MAX_ATTEMPTS = 3


async def _discover_workspace_dir(ctx: Context) -> None:
    """握手后首次工具调用时，向客户端请求 roots，提取工作空间目录。
    失败静默降级到 cwd（与原行为一致）。

    `_workspace_primed` 的置位时机是关键：原先一进函数就置 True，导致首次
    `list_roots()` 超时/报错后，`_workspace_dir` 保持 None、primed 已是 True，
    后续所有调用都按进程 cwd 解析数据源；若 cwd 与配置里的 dir_path 不匹配，
    整个会话每次都报"没有可用的数据源"，只能重启 MCP。握手抖动不该有这种
    永久代价——所以改成"问到才算完成"，失败重试，重试够多次才大声认命。
    """
    global _workspace_dir, _workspace_primed, _workspace_attempts
    with _workspace_lock:
        if _workspace_primed:
            return
        if _workspace_attempts >= _WORKSPACE_MAX_ATTEMPTS:
            # 重试够多了：认命回落 cwd，此时才置 primed，避免每次工具调用
            # 都白付一次 list_roots 超时；同时把后果说清楚，别让用户以为
            # "没有可用的数据源"是配置错了
            _workspace_primed = True
            logger.warning(
                f"获取客户端 roots 连续 {_workspace_attempts} 次失败，"
                f"本会话将按进程 cwd（{os.getcwd()}）解析数据源；"
                f"若因此报'没有可用的数据源'，请重启 MCP 或会话"
            )
            return
        _workspace_attempts += 1

    # 延迟 import 破环（workspace ↔ datasources）
    from .datasources import load_datasources

    try:
        roots_result = await ctx.session.list_roots()
        roots: List[Root] = list(roots_result.roots or [])
        for root in roots:
            uri = str(root.uri)
            if uri:
                if uri.startswith("file://"):
                    uri = uri[7:]
                _workspace_dir = normalize_path(uri)
                logger.info(f"从 roots 获取工作空间目录: {_workspace_dir}")
                break
        if _workspace_dir is None:
            logger.info("客户端未提供 roots，数据源目录回退到进程 cwd")
        # 问到了（哪怕返回空列表）才算完成发现——这是 primed 唯一置 True 的地方
        with _workspace_lock:
            _workspace_primed = True
        # 目录确定后预热数据源缓存（非致命）
        try:
            load_datasources(force_refresh=True)
        except Exception as e:
            logger.error(f"初始化加载数据源失败（非致命）: {e}", exc_info=True)
    except Exception as e:
        # 客户端不支持 roots 或超时——降级到进程 cwd（v2.x 之前的行为），
        # 但不置 primed：下次工具调用会重试
        logger.warning(f"获取客户端 roots 失败（第 {_workspace_attempts} 次，稍后将重试）: {e}")


async def _resolve_datasource(db_code: Optional[str]) -> Dict[str, Any]:
    """解析数据源，统一返回 (sources, 使用的db_code, ds配置)。
    无数据源/未知数据源直接抛 ToolFailure（isError 结果）。

    异步：load_datasources 内部会 stat 配置文件 / 探测配置库版本号，
    mysql 模式下每次都要连一次配置库，属阻塞 IO，必须卸载到工作线程，
    否则会占住 MCP 事件循环、拖慢同 server 的其它工具调用。
    """
    # 延迟 import 破环（workspace ↔ datasources）
    from .datasources import (load_datasources, get_default_db_code,
                              get_last_load_error)

    sources = await asyncio.to_thread(load_datasources)
    if not db_code:
        db_code = get_default_db_code(sources)
        if not db_code:
            raise ToolFailure(_no_datasource_message(get_last_load_error()))
    if db_code not in sources:
        raise ToolFailure(_unknown_datasource_message(db_code, sources,
                                                      get_last_load_error()))
    return {"sources": sources, "db_code": db_code, "ds": sources[db_code]}


def _current_dir_text() -> str:
    """本次解析实际使用的工作目录（与 load_datasources 同源：roots 优先，否则 cwd）。"""
    return _workspace_dir if _workspace_dir else os.getcwd()


def _no_datasource_message(load_error: Optional[str]) -> str:
    """组装"解析不到数据源"的报错。

    必须把「配置没读到」和「配置读到了、但当前目录没配」分开说——这两种病的
    下一步动作完全相反。load_datasources 在配置库连不上时会降级成空数据源
    （有缓存用缓存，没缓存返回 {"_default": None}，不杀进程），此时若只报
    "请检查 dir_path 是否匹配当前目录"，用户就会去改一个本来没问题的路径，
    而真实原因（MySQL 服务没起、.env 里 MCP_CONFIG_* 密码错、驱动没装）
    只留在 MCP 的 stderr 日志里——那条日志用户日常根本看不到。
    """
    current_dir = _current_dir_text()
    if load_error:
        return (f"没有可用的数据源。加载数据源配置本身就失败了：{load_error}\n\n"
                f"这不是 dir_path 不匹配的问题——是配置库根本没读到，所以任何目录都"
                f"匹配不上。请先按上面的原因修复配置库连接（.env 的 MCP_CONFIG_*、"
                f"MySQL 服务是否启动、配置库是否存在），或检查 MCP 的 stderr 日志。\n"
                f"当前工作目录：{current_dir}")
    return (f"没有可用的数据源。当前工作目录未配置数据源，"
            f"请检查配置库中 mcp_datasource_config 表的 dir_path 是否匹配当前目录\n"
            f"当前工作目录：{current_dir}")


def _unknown_datasource_message(db_code: str, sources: Dict[str, Any],
                                load_error: Optional[str]) -> str:
    """组装"指定的数据源不存在"的报错。配置加载失败时一并说明，否则"可用列表: []"
    看起来像配置表被清空了，实际是配置库连不上。"""
    available = [k for k in sources.keys() if k != "_default"]
    msg = f"未知数据源: {db_code}。可用列表: {available}"
    if load_error:
        msg += (f"\n\n注意：本次数据源配置加载失败（{load_error}），上面的列表可能不完整——"
                f"配置库没读到时它是空的。修复配置库连接后用 list_datasources 复查。")
    return msg
