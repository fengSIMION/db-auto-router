# -*- coding: utf-8 -*-
"""数据库驱动懒加载。

背景：dmPython / psycopg2 / oracledb 在模块顶层 eager import，实测边际耗时
合计约 1 秒；而一个工作目录通常只用得上其中一个方言。更要紧的是任一驱动
缺失或初始化失败（如 Oracle 缺客户端库），会让整个 MCP server 卡在 import
阶段起不来，连不相干的 MySQL 都用不了。

策略（v4.1 更新）：
- 全部方言驱动均懒加载，包括 mysql.connector——v4.0 时代配置库固定走 MySQL
  故将其 eager import；v4.1 配置库支持 SQLite（MCP_CONFIG_MODE=sqlite）后该
  前提消失，改为与其它方言一致，在首次真正连接 MySQL 时才 import。接收方
  （如只查 PG/达梦）不再被强制安装 mysql-connector-python。
- 驱动首次用到时才 import，失败时报错带上安装命令。"""

__author__ = "fengkeju"
import importlib
import threading

from .logcfg import logger

_lock = threading.Lock()
_cache = {}

# 方言 -> (模块名, 安装提示)
_DRIVER_BY_TYPE = {
    "mysql": ("mysql.connector", "pip install mysql-connector-python"),
    "dm": ("dmPython", "pip install dmPython"),
    "postgres": ("psycopg2", "pip install psycopg2-binary"),
    "gaussdb": ("psycopg2", "pip install psycopg2-binary"),  # 高斯兼容 PG 协议，复用同一驱动
    "oracle": ("oracledb", "pip install oracledb"),
}


def get_driver(db_type: str):
    """按方言返回驱动模块，首次调用时才真正 import，之后走缓存。

    未知方言抛 ValueError（与各调用点原有的兜底一致）；
    驱动装不上或初始化失败抛 ImportError，信息里带安装命令。
    """
    typ = (db_type or "").lower().strip()
    entry = _DRIVER_BY_TYPE.get(typ)
    if entry is None:
        raise ValueError(f"不支持的数据库类型: {db_type}")
    mod_name, hint = entry

    mod = _cache.get(mod_name)
    if mod is not None:
        return mod

    with _lock:
        mod = _cache.get(mod_name)  # 双检，避免并发下重复 import
        if mod is not None:
            return mod
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            # 除 ImportError 外，驱动自身初始化也可能抛别的异常，统一归一
            raise ImportError(
                f"{typ} 方言需要驱动 {mod_name}，加载失败: {type(e).__name__}: {e}。"
                f"请执行: {hint}"
            ) from e
        _cache[mod_name] = mod
        logger.info(f"懒加载驱动: {mod_name} (方言 {typ})")
        return mod
