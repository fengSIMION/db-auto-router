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
                                                     会话级——用 RESET 恢复
  oracle/dm          SET TRANSACTION READ ONLY      事务级，但**必须先把 autocommit
                                                     关掉**：开着它驱动会立刻提交
                                                     那个只含 SET 的事务，只读标记
                                                     随之失效；释放时回滚并恢复

自动降级即验证：enable 语句真机执行一次，库不认 → 记入能力缓存并跳过，
调用方在返回值 note 里说明。用户无需手动测任何东西。

能力缓存只存 negative，且**只在错误被正面识别为"这条语句永久不被接受"时才存**。
默认方向必须是不缓存：瞬态标记表覆盖不全（PG 的
server closed the connection unexpectedly 就不在表里），误缓存一次等于
第三层静默关闭好几天；漏缓存的代价只是每次查询多跑一次失败的 SET。
"""
from __future__ import annotations

__author__ = "fengkeju"
import logging
from typing import Any, Dict, Optional

from .config import READONLY_DB_GUARD
from .directconn import _set_conn_autocommit
from .errutil import is_transient_error, safe_cursor
from .logcfg import logger
from .pool import discard_connection

# (开关语句, 释放动作)。没有入口的方言（未知 db_type）本层直接弃权。
#   none         无需恢复（事务级，随 put_connection 的 rollback 失效）
#   reset        RESET 回会话默认值（会话级设置）
#   txn_rollback 回滚 + 恢复 autocommit（关掉 autocommit 才留得住只读标记）
_DIALECT_SPEC = {
    "mysql": ("START TRANSACTION READ ONLY", "none"),
    "postgres": ("SET default_transaction_read_only=on", "reset"),
    "gaussdb": ("SET default_transaction_read_only=on", "reset"),
    "oracle": ("SET TRANSACTION READ ONLY", "txn_rollback"),
    "dm": ("SET TRANSACTION READ ONLY", "txn_rollback"),
}

# 能力缓存：pool_key → 是否支持。**只存 negative**——A3 明确禁止缓存 positive，
# 否则"支持过一次"就会被当成"以后不用再设只读"，本层名存实亡。
_capability: Dict[str, bool] = {}

# 只有命中这些才认定为"这条语句永久不被接受"，才允许缓存 negative。
_PERMANENT_MARKERS = ("syntax", "not support", "unsupported", "unknown", "unrecognized")

_PG_RESET_SQL = "RESET default_transaction_read_only"


def _is_permanent_rejection(e: BaseException) -> bool:
    """是否"这条语句永久不被接受"——只有这时才允许缓存 negative。

    默认方向必须是不缓存：瞬态标记表覆盖不全（PG 的
    `server closed the connection unexpectedly` 不在 `is_transient_error` 的表里），
    按"不是瞬态就缓存"的旧逻辑，它会被误判成"库不支持只读"并永久缓存，
    第三层从此静默关闭。漏缓存的代价仅是每次查询多跑一次失败的 SET。
    """
    msg = str(e).lower()
    return any(marker in msg for marker in _PERMANENT_MARKERS)


def enable_read_only(conn, dialect: str, pool_key: str) -> bool:
    """在借出的连接上开启数据库只读。

    返回 True=已生效；False=本层未兜上（方言不支持/开关关闭/库不认/连接故障），
    调用方据此在返回值 note 里说明。
    """
    if READONLY_DB_GUARD != "on":
        return False
    spec = _DIALECT_SPEC.get((dialect or "").lower().strip())
    if not spec:
        return False
    if _capability.get(pool_key) is False:
        return False        # 已探明该连接不支持，不再重复探测

    sql, release = spec
    try:
        if release == "txn_rollback":
            # Oracle/达梦：autocommit 开着的话，驱动会立刻提交这个只含 SET 的
            # 事务，只读标记随之失效。先关掉，让 SET 与随后的查询同一个事务
            _set_conn_autocommit(conn, dialect, False)
        with safe_cursor(conn) as cur:
            cur.execute(sql)
        return True
    except Exception as e:
        if release == "txn_rollback":
            # 开失败也要把 autocommit 还回去：否则连接带着 False 回池，
            # 后续借用者的语句全跑在隐式事务里
            _set_conn_autocommit(conn, dialect, True)
        if _is_permanent_rejection(e):
            _capability[pool_key] = False
            logger.info(f"数据库只读兜底在该连接上不受支持，已记入能力缓存并跳过: {e}")
        else:
            # 瞬时/未识别故障：不缓存，下次重试——毒化一次要付出数天的代价
            logger.warning(f"数据库只读兜底开启失败（未缓存，下次重试）: {e}")
        return False


def release_read_only(conn, dialect: str, pool_key: str, ds: Dict[str, str],
                      target_db: str = None) -> bool:
    """查询结束后调用：需要恢复的方言恢复原状。

    返回 True=连接可正常回池；False=恢复失败，连接已被弃（调用方 MUST NOT
    再 put_connection）——带着只读标记回池会让后续 execute 写库失败。
    """
    spec = _DIALECT_SPEC.get((dialect or "").lower().strip())
    if not spec:
        return True
    _, release = spec
    if release == "none":
        return True         # 事务级方言无需恢复（随 rollback 失效）
    try:
        if release == "reset":
            # RESET 回到会话默认值，而不是把"读回来的值"写回去——原值若在 SET
            # 之后读，读到的就是 on，恢复语句会把自己刚打开的只读状态原样写回
            with safe_cursor(conn) as cur:
                cur.execute(_PG_RESET_SQL)
        else:
            conn.rollback()
            _set_conn_autocommit(conn, dialect, True)
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
    elif exempt_reason == "direct":
        note = ("本条走超时专用连接（用完即关，不存在标记泄漏给其它调用者的风险），"
                "未套数据库只读；仍过了一二层")
    else:
        note = (f"该数据源（{dialect}）未启用数据库只读兜底"
                f"（库不支持该语法、开关已关或连接故障），已跳过；仍有一二层保护")
    return {"read_only_enforced": False, "note": note}


def merge_note(result: Dict[str, Any], field: Dict[str, Any]) -> None:
    """把 readonly_field 的结果合进工具返回；note 已存在时拼接而非覆盖。

    explain_query_impl 自己会写 note（如"EXPLAIN ANALYZE 已真实执行该 SELECT"），
    直接 update 会把它盖掉——调用方从此不知道这条查询真的执行过了，
    那是比没有字段更坏的信息丢失。分隔符用"；"，与 explain.py 既有约定一致。
    """
    old = result.get("note")
    result.update(field)
    new = field.get("note")
    if old and new and old != new:
        result["note"] = "；".join(x for x in (old, new) if x)
