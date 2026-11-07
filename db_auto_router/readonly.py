# -*- coding: utf-8 -*-
"""只读通道第三层：数据库只读兜底。

设计文档：docs/superpowers/specs/2026-11-07-read-guard-three-layers-design.md

定位：第一层（正则）与第二层（AST）都是服务端字符串/语法判定，判错的那一刻
数据已经能被改动；本层让数据库自己进入只读状态，是"数据库亲自把关"的兜底。
它不承诺绝对边界——MySQL 的 DDL 会隐式提交逃出只读事务，所以返回值字段叫
`read_only_enforced`（该层已生效）而不是"写操作绝无可能"。

按方言的形态（为什么各不相同）：
  mysql              START TRANSACTION READ ONLY   事务级——put_connection 的
                                                     rollback 自动清除，无需恢复
  postgres/gaussdb   SET default_transaction_read_only=on
                                                     会话级——必须恢复原值，
                                                     恢复失败只能弃连接
  oracle/dm          SET TRANSACTION READ ONLY      事务级，随 commit/rollback 失效

自动降级即验证：enable 语句真机执行一次，库不认 → 记入能力缓存并跳过，
调用方在返回值 note 里说明。用户无需手动测任何东西。
"""
from __future__ import annotations

__author__ = "fengkeju"
import logging
from typing import Any, Dict, Optional

from .config import READONLY_DB_GUARD
from .errutil import is_transient_error, safe_cursor
from .logcfg import logger
from .pool import discard_connection

# (开关语句, 是否需要恢复)。没有入口的方言（未知 db_type）本层直接弃权。
_READONLY_SQL = {
    "mysql": ("START TRANSACTION READ ONLY", False),
    "postgres": ("SET default_transaction_read_only=on", True),
    "gaussdb": ("SET default_transaction_read_only=on", True),
    "oracle": ("SET TRANSACTION READ ONLY", False),
    "dm": ("SET TRANSACTION READ ONLY", False),
}

# 能力缓存：pool_key → 是否支持。**只存 negative**——A3 明确禁止缓存 positive，
# 否则"支持过一次"就会被当成"以后不用再设只读"，本层名存实亡。
# 只记"数据库明确拒绝"；连接级/瞬时故障不记，一次网络抖动不许永久毒化该层
# （MCP 进程可能活好几天，毒化一次等于整周只剩两层）。
_capability: Dict[str, bool] = {}
# pool_key → PG 原值（恢复用）。首次开启时读一次，后续查询不再读。
_original: Dict[str, str] = {}

_PG_ORIGINAL_SQL = "SELECT current_setting('default_transaction_read_only')"


def enable_read_only(conn, dialect: str, pool_key: str) -> bool:
    """在借出的连接上开启数据库只读。

    返回 True=已生效；False=本层未兜上（方言不支持/开关关闭/库不认/连接故障），
    调用方据此在返回值 note 里说明。
    """
    if READONLY_DB_GUARD != "on":
        return False
    spec = _READONLY_SQL.get((dialect or "").lower().strip())
    if not spec:
        return False
    if _capability.get(pool_key) is False:
        return False        # 已探明该连接不支持，不再重复探测

    sql, needs_restore = spec
    try:
        with safe_cursor(conn) as cur:
            cur.execute(sql)
            if needs_restore and pool_key not in _original:
                # 读原值以便恢复。读不到按 off 处理：池连接由本工具创建，
                # 默认状态就是 off；宁可恢复成一个已知值，也不把 on 留给后续写操作
                try:
                    cur.execute(_PG_ORIGINAL_SQL)
                    row = cur.fetchone()
                    _original[pool_key] = str(row[0]).strip() if row else "off"
                except Exception as e:
                    logger.debug(f"读取 default_transaction_read_only 原值失败，按 off 处理: {e}")
                    _original[pool_key] = "off"
        return True
    except Exception as e:
        if is_transient_error(e):
            # 连接级/瞬时故障：不写缓存，下次重试——毒化一次要付出整整一周的代价
            logger.warning(f"数据库只读兜底开启失败（瞬时错误，不缓存，下次重试）: {e}")
        else:
            _capability[pool_key] = False
            logger.info(f"数据库只读兜底在该连接上不受支持，已记入能力缓存并跳过: {e}")
        return False


def release_read_only(conn, dialect: str, pool_key: str, ds: Dict[str, str],
                      target_db: str = None) -> bool:
    """查询结束后调用：需要恢复的方言恢复原值。

    返回 True=连接可正常回池；False=恢复失败，连接已被弃（调用方 MUST NOT
    再 put_connection）——带着只读标记回池会让后续 execute 写库失败。
    """
    spec = _READONLY_SQL.get((dialect or "").lower().strip())
    if not spec or not spec[1]:
        return True         # 事务级方言无需恢复（随 rollback 失效）
    original = _original.get(pool_key, "off")
    try:
        with safe_cursor(conn) as cur:
            cur.execute(f"SET default_transaction_read_only={original}")
        return True
    except Exception as e:
        logger.error(f"数据库只读状态恢复失败，连接已弃（不回池）: {e}")
        discard_connection(ds, conn, target_db)
        return False


def readonly_field(dialect: str, enforced: bool,
                   exempt_reason: Optional[str] = None) -> Dict[str, Any]:
    """统一的返回值契约：三个只读工具的成功返回都带这个字段。

    enforced=True  → {"read_only_enforced": True}
    enforced=False → {"read_only_enforced": False, "note": <哪层没兜上、为什么>}

    注意语义：False 不等于"没有保护"——一二两层仍在工作，note 必须把这件事说清，
    否则调用方（模型）会向用户报告"查询无保护"，那是比没有字段更坏的事实错告。
    """
    if enforced:
        return {"read_only_enforced": True}
    if exempt_reason == "txn":
        note = ("事务内执行未套数据库只读（在飞的事务上开只读会隐式提交你未提交的工作）；"
                "本语句仍过了一二层只读校验")
    elif exempt_reason == "explain":
        note = ("EXPLAIN 未套数据库只读（Oracle/达梦的 EXPLAIN PLAN FOR 要写 PLAN_TABLE，"
                "只读事务里会报错）；仍过了一二层")
    else:
        note = (f"该数据源（{dialect}）未启用数据库只读兜底"
                f"（库不支持该语法、开关已关或连接故障），已跳过；仍有一二层保护")
    return {"read_only_enforced": False, "note": note}
