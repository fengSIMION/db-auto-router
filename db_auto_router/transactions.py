# -*- coding: utf-8 -*-
"""事务支持（v4.0）：专用连接 + 空闲超时自动回滚 + 事务内执行
——原 1204–1348 行 + 新增 list_active_transactions()
（安全清理：原 list_transactions 工具内联直读 _transactions/_txn_lock 的逻辑
收拢为本模块的 list_active_transactions()，返回结构键名保持完全一致）

v4.5 续期能力：空闲阈值从"全局一个常量"改为"每事务一份"，
配合 extend_transaction_impl() 支持运行中续期。
语义未变——仍是纯空闲超时，事务内任何活动都会经 _get_transaction 刷新 last_used，
所以活跃事务永远不会被清掉；只有真的没人理它满 N 秒才回滚。
全局默认（TXN_IDLE_TIMEOUT=600s）保持不变，长流程按事务自行放宽。"""

__author__ = "fengkeju"
import time
import uuid
import threading
from typing import Dict, Any, List, Optional

from .config import TXN_IDLE_TIMEOUT, TXN_MAX_IDLE, MAX_RESULT_ROWS, SUMMARY_DISPLAY_ROWS
from .errors import ToolFailure
from .logcfg import logger
from .directconn import _open_direct_connection, _set_conn_autocommit
from .sqlguard import split_sql_statements, is_ddl_operation

# 事务注册表：{txn_id: {conn, ds, db_code, target_db, created_at, last_used, stmt_count, idle_timeout}}
_transactions: Dict[str, Dict[str, Any]] = {}
_txn_lock = threading.Lock()


def _resolve_idle_timeout(requested: Optional[int]) -> int:
    """校验并归一化调用方请求的空闲超时。

    None → 用全局默认；越界（<=0 或超过 TXN_MAX_IDLE）直接抛 ToolFailure。
    为什么必须设上限：误传一个巨大值会让事务连同伴随的行锁/表锁永不释放，
    而这种事故在交互式会话里极难发现——连接看着"还活着"，库里的锁却一直挂着。
    """
    if requested is None:
        return TXN_IDLE_TIMEOUT
    try:
        val = int(requested)
    except (TypeError, ValueError):
        raise ToolFailure(f"idle_timeout 必须是整数秒，收到: {requested!r}")
    if val <= 0:
        raise ToolFailure(f"idle_timeout 必须大于 0，收到: {val}")
    if val > TXN_MAX_IDLE:
        raise ToolFailure(
            f"idle_timeout={val}s 超过上限 {TXN_MAX_IDLE}s（MCP_TXN_MAX_IDLE 可调）。"
            f"交互式事务不建议超过 24 小时；确需更长请改用批处理脚本")
    return val


def _cleanup_expired_transactions() -> List[str]:
    """回滚并释放超过空闲时限的事务，返回被清理的 txn_id 列表"""
    expired = []
    now = time.time()
    with _txn_lock:
        for txn_id, info in list(_transactions.items()):
            # 每事务各自的阈值；老注册表项（v4.5 之前创建的）缺字段时回落全局默认
            idle = info.get("idle_timeout", TXN_IDLE_TIMEOUT)
            if now - info.get("last_used", now) > idle:
                expired.append(txn_id)
    for txn_id in expired:
        try:
            _end_transaction(txn_id, commit=False, reason=f"空闲超过阈值自动回滚")
            logger.info(f"事务 {txn_id} 空闲超时，已自动回滚")
        except Exception as e:
            logger.warning(f"清理过期事务 {txn_id} 失败: {e}")
    return expired


def _get_transaction(txn_id: str) -> Dict[str, Any]:
    """取事务上下文，不存在则抛异常"""
    with _txn_lock:
        info = _transactions.get(txn_id)
    if not info:
        raise ToolFailure(f"事务 {txn_id} 不存在（可能已提交/回滚/超时释放）。用 list_transactions 查看活跃事务")
    info["last_used"] = time.time()
    return info


def _end_transaction(txn_id: str, commit: bool, reason: str = "") -> Dict[str, Any]:
    """结束事务：commit/rollback 并关闭专用连接"""
    with _txn_lock:
        info = _transactions.pop(txn_id, None)
    if not info:
        raise ToolFailure(f"事务 {txn_id} 不存在")
    # 与 execute_in_transaction 用同一把锁：否则"正在执行中"与"正在提交"会交叉，
    # 可能出现 commit 先跑、执行中的语句随后落在一个已提交的连接上
    with info.get("exec_lock", threading.Lock()):
        conn = info["conn"]
        try:
            if commit:
                conn.commit()
                action = "已提交"
            else:
                try:
                    conn.rollback()
                except Exception:
                    pass
                action = f"已回滚（{reason}）" if reason else "已回滚"
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return {
        "txn_id": txn_id,
        "action": action,
        "db_code": info.get("db_code"),
        "statements_in_txn": info.get("stmt_count", 0),
    }


def begin_transaction_impl(ds: Dict[str, str], db_code: str, target_db: str = None,
                           idle_timeout: Optional[int] = None) -> Dict[str, Any]:
    """开启事务：创建专用连接（不入池），autocommit 关闭。返回 txn_id 供后续工具使用

    idle_timeout: 本事务的空闲超时秒数。None = 用全局默认 MCP_TXN_IDLE_TIMEOUT（600s）。
    长流程（要等用户确认、跨多轮对话）建议在这里就放宽，例如 idle_timeout=3600，
    比中途发现事务没了再重来代价小得多。上限受 MCP_TXN_MAX_IDLE 约束。
    """
    _cleanup_expired_transactions()
    effective_idle = _resolve_idle_timeout(idle_timeout)
    conn = _open_direct_connection(ds, target_db)
    typ = ds.get("db_type", "").lower().strip()
    _set_conn_autocommit(conn, typ, False)
    txn_id = f"txn-{uuid.uuid4().hex[:8]}"
    with _txn_lock:
        _transactions[txn_id] = {
            "conn": conn,
            "ds": ds,
            "db_code": db_code,
            "target_db": target_db,
            "created_at": time.time(),
            "last_used": time.time(),
            "stmt_count": 0,
            "idle_timeout": effective_idle,
            # 本事务专属执行锁。事务的全部语义就是"所有语句共用同一条连接"，
            # 而一条连接同时只能跑一个游标——两个并发调用带同一个 txn_id 时，
            # 会互相读走对方的结果集（数据串台），且一方报错后异常路径会
            # rollback + pop + close 连接，另一方还在执行中，收到"连接已关闭"，
            # 而调用方以为还开着的事务已被静默终止。_txn_lock 只保护注册表字典，
            # 保护不了"用连接"的那一段，必须单独一把锁。
            "exec_lock": threading.Lock(),
        }
    logger.info(f"开启事务 {txn_id} (数据源: {db_code}, 空闲超时 {effective_idle}s)")
    return {"txn_id": txn_id, "db_code": db_code, "status": "open",
            "idle_timeout_seconds": effective_idle,
            "idle_remaining_seconds": effective_idle,
            "hint": "事务内活动会自动刷新空闲计时；长时间无人操作前可调 extend_transaction 续期"
                    "（过期后只能重新开始）"}


def extend_transaction_impl(txn_id: str, extend_seconds: Optional[int] = None) -> Dict[str, Any]:
    """续期事务（v4.5）：刷新空闲计时，可选放宽本事务的空闲阈值。

    两件事一起做，缺一不可：
    1. last_used 推到当前时刻——立即解除"即将因空闲超时被回滚"的危险；
    2. extend_seconds 提供时，把本事务的 idle_timeout 提升到 max(当前值, extend_seconds)，
       让后续也能安心离开更久。只做第 1 件，放宽后的阈值仍是老值，续期意义有限。

    必须在事务过期之前调用：过期后 txn_id 已从注册表移除，本函数抛 ToolFailure，
    此时只能重新 begin_transaction 从头再来。

    extend_seconds 同样受 MCP_TXN_MAX_IDLE 上限约束。
    """
    info = _get_transaction(txn_id)  # 不存在即抛 ToolFailure（含 list_transactions 提示）
    now = time.time()
    with _txn_lock:
        old_idle = info.get("idle_timeout", TXN_IDLE_TIMEOUT)
        new_idle = old_idle
        extended = False
        if extend_seconds is not None:
            new_idle = _resolve_idle_timeout(extend_seconds)
            if new_idle > old_idle:
                extended = True
            else:
                # 只收紧不放宽：调用方传一个比当前更小的值，不能借此把阈值降下来
                new_idle = old_idle
            info["idle_timeout"] = new_idle
        info["last_used"] = now
        statements = info.get("stmt_count", 0)

    logger.info(f"事务 {txn_id} 续期: 空闲阈值 {old_idle}s → {new_idle}s")
    return {
        "txn_id": txn_id,
        "db_code": info.get("db_code"),
        "idle_timeout_seconds": new_idle,
        "previous_idle_timeout_seconds": old_idle,
        "idle_remaining_seconds": new_idle,
        "statements_in_txn": statements,
        "extended": extended,
        "message": (f"已续期：空闲计时重置，空闲阈值 {old_idle}s → {new_idle}s"
                    if extended else
                    f"已续期：空闲计时重置（阈值维持 {new_idle}s，"
                    f"extend_seconds 需大于当前值才会放宽）"),
    }


def execute_in_transaction(txn_id: str, sql: str) -> Dict[str, Any]:
    """在事务中执行 SQL（不 commit，由 commit/rollback 决定）"""
    info = _get_transaction(txn_id)
    statements = split_sql_statements(sql)
    if not statements:
        raise ValueError("SQL 为空")
    # DDL 一律拒绝进事务：MySQL/Oracle/达梦的 DDL 会隐式提交当前事务，
    # 此前的 INSERT/UPDATE 在 DDL 那一刻就已落库，而 rollback_transaction
    # 仍会返回"已回滚"——静默的数据完整性事故。事务的契约是"跨语句原子性"，
    # DDL 从定义上就不满足，宁可明确拒绝也不要给出假的原子性承诺。
    ddl_hit = next((s for s in statements if is_ddl_operation(s)), None)
    if ddl_hit is not None:
        raise ToolFailure(
            "事务内不允许执行 DDL（CREATE/ALTER/DROP/TRUNCATE/RENAME）："
            "DDL 会隐式提交当前事务，导致本事务中此前的语句全部落库且无法回滚，"
            "而 commit/rollback 仍会报告成功。请先 commit/rollback 结束事务，"
            f"再用不带 txn_id 的 execute 执行该 DDL。问题语句: {ddl_hit[:120]}"
        )
    # 事务专属执行锁：整个"取游标→执行→取结果"序列必须原子。
    # 少了它，两个并发调用带同一个 txn_id 会共用一条物理连接——互相读走对方的
    # 结果集（数据串台），且一方报错后异常路径会 rollback + pop + close，
    # 另一方还在执行中，收到"连接已关闭"而它以为事务还开着。
    # 代价只是同一事务内的语句被串行化，而这本来就是事务的语义。
    with info.get("exec_lock", threading.Lock()):
        conn = info["conn"]
        dialect = info["ds"].get("db_type", "").lower().strip()
        cursor = None
        results = []
        total_affected = 0
        try:
            start_time = time.time()
            for idx, stmt in enumerate(statements, start=1):
                cursor = conn.cursor()
                cursor.execute(stmt)
                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    rows = [dict(zip(columns, row)) for row in cursor.fetchmany(MAX_RESULT_ROWS)]
                    results.append({"statement": idx, "type": "resultset", "rows": len(rows),
                                    "data": rows[:SUMMARY_DISPLAY_ROWS]})
                else:
                    rc = cursor.rowcount if cursor.rowcount and cursor.rowcount >= 0 else 0
                    total_affected += rc
                    results.append({"statement": idx, "type": "affected", "affected_rows": rc})
                cursor.close()
                cursor = None
            execution_time_ms = round((time.time() - start_time) * 1000, 2)
            with _txn_lock:
                info["stmt_count"] = info.get("stmt_count", 0) + len(statements)
            out = {
                "txn_id": txn_id,
                "committed": False,
                "statements_executed": len(statements),
                "total_affected_rows": total_affected,
                "details": results,
                "execution_time_ms": execution_time_ms,
                "dialect": dialect,
                "message": f"事务内执行 {len(statements)} 条语句（未提交，等待 commit/rollback）"
            }
            # 单语句结果集场景补顶层 data/total_rows，与不带 txn_id 的 query 返回形状对齐。
            # 否则同一个 query 工具在有/无 txn_id 时结果形状不同，调用方（模型或脚本）
            # 得按参数分叉解析——纯增量字段，details 原样保留，向后兼容。
            if len(results) == 1 and results[0]["type"] == "resultset":
                out["data"] = results[0]["data"]
                out["total_rows"] = results[0]["rows"]
            return out
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            # 事务内出错：自动终止事务，防止半提交状态被继续误用
            with _txn_lock:
                _transactions.pop(txn_id, None)
            try:
                conn.close()
            except Exception:
                pass
            raise RuntimeError(f"事务 {txn_id} 执行失败已自动回滚并终止: {e}") from e
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass


def list_active_transactions() -> Dict[str, Any]:
    """列出活跃事务（含超时清理），返回结构与原 list_transactions 工具内联逻辑一致

    v4.5 每条补 idle_remaining_seconds / idle_timeout_seconds：
    没有可见的剩余时间，调用方（模型）无法判断"该续期了"，
    续期工具也就永远不会被用起来。这两个字段是续期能力真正生效的前提。
    """
    expired = _cleanup_expired_transactions()
    now = time.time()
    with _txn_lock:
        items = []
        for txn_id, info in _transactions.items():
            idle = info.get("idle_timeout", TXN_IDLE_TIMEOUT)
            remaining = max(0, round(idle - (now - info.get("last_used", now)), 1))
            items.append({
                "txn_id": txn_id,
                "db_code": info.get("db_code"),
                "statements": info.get("stmt_count", 0),
                "age_seconds": round(now - info.get("created_at", now), 1),
                "idle_timeout_seconds": idle,
                "idle_remaining_seconds": remaining,
                "expires_soon": remaining < 60,
            })
    # 顶层 idle_timeout_seconds 保持 v4.0 以来的含义：全局默认值。
    # 每条事务各自的实际阈值在 items[].idle_timeout_seconds 里，二者可能不同。
    result = {"transactions": items, "idle_timeout_seconds": TXN_IDLE_TIMEOUT}
    if expired:
        result["expired_cleaned"] = expired
    if items:
        result["hint"] = ("idle_remaining_seconds 是剩余可空闲时长；即将归零（expires_soon=true）时"
                          "调 extend_transaction 续期，过期后只能重新开始")
    return result
