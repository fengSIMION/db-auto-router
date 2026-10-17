# -*- coding: utf-8 -*-
"""17 个 MCP 工具包装（docstring 即工具描述；原 14 个的签名与 docstring 保持兼容，
v4.5 新增 explain_query / slow_queries / extend_transaction，并在 begin_transaction
追加可选参数 idle_timeout）
——原 2620–2897、2933–2937 行。
安全清理：list_transactions 改为调用 transactions.list_active_transactions()，
不再直读 _transactions/_txn_lock 全局。"""

__author__ = "fengkeju"
import os
import re
import json
import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import Context

from .config import (AUDIT_ENABLED, AUDIT_LOG_DIR, AUDIT_SCAN_DAYS,
                     AUDIT_RETAIN_DAYS, MAX_POOL_SIZE, WRITE_CONFIRM_MODE,
                     WRITE_ESTIMATE_MODE)
from .logcfg import logger
from .errors import ToolFailure
from .audit import audit_log
from .workspace import _discover_workspace_dir
from .datasources import load_datasources, get_default_db_code
from . import workspace  # 通过属性访问 _workspace_dir（global 重绑定，禁止 from-import）
from .util import normalize_path
from .sqlguard import (split_sql_statements, is_ddl_operation,
                       validate_read_only_sql, is_provably_read_only, is_pure_dml,
                       ddl_breaks_transaction)
from .readengine import execute_read_only_query
from .writeengine import execute_any_sql, estimate_affected_rows
from .transactions import (begin_transaction_impl, execute_in_transaction, _end_transaction,
                           _cleanup_expired_transactions, list_active_transactions,
                           extend_transaction_impl)
from .csvio import export_csv_impl, import_csv_impl, count_csv_rows
from .introspection import get_table_schema, search_tables_by_keyword
from .explain import explain_query_impl, slow_queries_impl
from .confirm import _confirm_write_if_needed
from . import dump as dump_module


# ------------------------------------------------------------------------------
# 阻塞调用卸载：DB 执行一律在工作线程里跑，不占 MCP 事件循环
# ------------------------------------------------------------------------------
# 背景：工具函数是 async，但底层执行引擎全是同步阻塞调用。直接在 async 函数里调用，
# 一次长查询（READ_TIMEOUT 默认 30s）或大导出会把同 server 的其它工具调用全部排队，
# 连 list_transactions 这种只想看状态的轻量调用也得干等。
#
# 并发度必须收敛：卸载后模型并发发起的查询会同时进到连接池，若各自 key 不同就会
# 互相 LRU 驱逐，连接池反复建/关，比串行还慢。故用信号量把并发 DB 执行数压到
# 连接池容量，超额请求排队等待而不是抢池。
_db_gate = asyncio.Semaphore(MAX_POOL_SIZE)


async def _run_db(fn, *args, **kwargs):
    """在工作线程执行阻塞的 DB 操作，并发度受 _db_gate 限制（取值=连接池容量）。

    不涉及数据库连接的轻量操作（审计日志读取、事务列表、数据源缓存）不必过闸门，
    直接 asyncio.to_thread 即可——否则它们会被长查询堵在闸门后面。
    """
    async with _db_gate:
        return await asyncio.to_thread(fn, *args, **kwargs)


def _audit_error(tool: str, db_code: str, sql: str, exc: BaseException, txn_id: str = None) -> None:
    """错误路径统一落审计——detail 与 db_code 都不许丢。

    原实现 `except ToolFailure: audit_log(..., status="error")` 不传 detail，
    而 ToolFailure 恰好覆盖最需要区分的一类失败："用户拒绝了确认"、
    "未知数据源 X"、"没有可用的数据源"、"事务 X 不存在"、"事务内不允许执行 DDL"。
    实证 logs 里 84 条 error 有 12 条 detail 为空、67 条 db_code 为空——事后翻审计
    只剩一串空 reason，"追溯到底执行了什么"的目的直接落空。

    db_code 由调用方传解析后的实际数据源：原先传的是原始入参 `db_code or ""`,
    而不传 db_code（走默认数据源）时日志里就是空串,查不出这条 SQL 打了哪个库。
    """
    audit_log(tool, db_code or "", sql, status="error",
              detail=(getattr(exc, "message", None) or str(exc) or type(exc).__name__),
              txn_id=txn_id)


# --- 工具实现（参数名/工具名与 v2.x 兼容；docstring 即工具 description） ---

async def query(sql: str, db_code: Optional[str] = None, max_rows: Optional[int] = None, timeout: Optional[int] = None, txn_id: Optional[str] = None, ctx: Context = None) -> dict[str, Any]:
    """执行只读SQL查询。根据当前工作目录自动路由到对应数据库(默认走 is_default=1 的数据源,可用 list_datasources 查看)。仅支持 SELECT/SHOW/DESC/EXPLAIN/WITH 语句。支持跨库查询语法：db_code.table_name 或 database.table_name。未指定 LIMIT 时自动添加 LIMIT 1000;结果超过50行返回摘要(前10行 + total_rows 总数)。返回值含 execution_time_ms(执行耗时,>1000ms 视为慢查询)、dialect(数据库类型,如 mysql/dm/postgres/oracle/gaussdb)。v4.0 新增参数：max_rows(覆盖默认1000行上限,重分析场景用)、timeout(覆盖默认30秒超时,长查询用,单位秒)、txn_id(在指定事务中执行,配合 begin_transaction 使用;事务内同样仅允许只读语句,事务内写数据请用 execute 配合 txn_id,会走服务端确认)。注意：传了 txn_id 时走事务专用连接，max_rows/timeout 均不生效（按默认上限与超时执行）。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not sql:
        raise ToolFailure("缺少必要参数: sql")
    # 只读守卫前置：必须在解析数据源之前跑。
    # 原先只在 txn_id 分支校验，非事务路径要等执行引擎内部才拦，导致目录未配数据源时
    # 用户先看到"没有可用的数据源"，而不是更可操作的"只读通道不允许写操作"。
    # 守卫是纯字符串判定、不碰数据库，前置零成本，且让事务/非事务两条路径行为一致。
    try:
        validate_read_only_sql(sql)
    except PermissionError as e:
        # 守卫拦截也必须留痕：否则事后无法证明"有人试图从只读通道写库"。
        # 实证 617 条审计里 detail 含"安全拦截"的是 0 条——全在日志外。
        _audit_error("query", db_code or "", sql, e)
        raise
    resolved_db_code = db_code or ""
    try:
        resolved = await workspace._resolve_datasource(db_code)
        resolved_db_code = resolved["db_code"]
        if txn_id:
            result_data = await _run_db(execute_in_transaction, txn_id, sql)
        else:
            result_data = await _run_db(execute_read_only_query, sql, resolved["ds"],
                                        resolved["db_code"], max_rows=max_rows, timeout=timeout)
        audit_log("query", resolved["db_code"], sql, status="ok",
                  execution_time_ms=result_data.get("execution_time_ms") if isinstance(result_data, dict) else None,
                  rows=result_data.get("total_rows") if isinstance(result_data, dict) else None, txn_id=txn_id)
        return _normalize_result(result_data)
    except ToolFailure as e:
        _audit_error("query", resolved_db_code, sql, e, txn_id)
        raise
    except Exception as e:
        _audit_error("query", resolved_db_code, sql, e, txn_id)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def execute(sql: str, db_code: Optional[str] = None, txn_id: Optional[str] = None, ctx: Context = None) -> dict[str, Any]:
    """执行任意SQL语句（包括INSERT/UPDATE/DELETE等写操作）。写操作执行前会弹出确认框，用户确认后才真正执行（服务端强制，无法绕过）；数据源配置里取消打勾「写操作需人工审核」的开发库除外（v4.6，免确认）。支持跨库查询语法。v4.0 支持多语句批量执行：用分号分隔多条 SQL 会一次确认、顺序执行、全部成功才提交（v4.8 明确边界：纯 DML 批保证原子性、任一失败整批回滚；批内混入 DDL 时 mysql/oracle/达梦的 DDL 隐式提交、无法随批回滚，此时返回值带 atomicity=false 与 atomicity_note，确认弹窗也会提前警示，MUST NOT 对用户说"整批回滚"）；CREATE PROCEDURE/FUNCTION/TRIGGER 等带 BEGIN...END 体的语句整条执行。返回值包含 execution_time_ms（执行耗时毫秒数）；批量时含 batch/total_affected_rows/details。txn_id 参数可在指定事务中执行（配合 begin_transaction）。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not sql:
        raise ToolFailure("缺少必要参数: sql")
    resolved_db_code = db_code or ""
    try:
        resolved = await workspace._resolve_datasource(db_code)
        resolved_db_code = resolved["db_code"]
        # v4.6 数据源级开关：该数据源在配置里取消打勾「写操作需人工审核」（开发库）时，
        # 确认弹窗与影响行数试执行预估一起跳过。审计日志不受影响，照旧记录。
        need_confirm = bool(resolved["ds"].get("write_confirm", 1))
        # 写操作服务端确认（物理拦截）——在任何数据库动作之前。
        # 多语句时按语句逐条判断，任一条不能证明只读就触发确认。
        #
        # 判据是"可证明只读"的白名单（is_provably_read_only），不是写关键字黑名单：
        # 黑名单漏"能间接写但不以写前缀开头"的语句，实测 SET+PREPARE+EXECUTE、
        # EXPLAIN ANALYZE DELETE（PG 单语句即真实执行）、LOAD DATA INFILE、
        # COPY t FROM、FLUSH TABLES WITH READ LOCK、ANALYZE/KILL/DO/USE
        # 全部零确认直接执行，与"服务端强制，无法绕过"的承诺相反。
        statements = split_sql_statements(sql)
        has_write = any(not is_provably_read_only(s) for s in statements)
        has_ddl = any(is_ddl_operation(s) for s in statements)
        # v4.4 影响行数预估：在弹确认窗前先试执行测行数，让用户看到"这条 UPDATE 会动多少行"。
        # 三道 gate：只估纯 DML（DDL 回滚不了，估了也是假数字；SET/PREPARE/EXECUTE 等
        # 非 DML 有确认撤销不掉的副作用，也不能拿来估行数）；事务内不估（嵌套执行会
        # 打断调用方的事务语义）；功能可通过 MCP_WRITE_ESTIMATE=off 关闭。
        # v4.6 补第四道：该数据源免人工审核时也不估——没有人要看这个数字，
        # 省掉一次试执行+回滚（自增值消耗、触发器触发都是真实副作用）。
        dml_only = bool(statements) and all(is_pure_dml(s) for s in statements)
        # v4.8 批量原子性预警：多语句批内混入 DDL，且该方言的 DDL 隐式提交时，
        # "任一失败整批回滚"不成立。必须在用户点"接受执行"之前就说清楚，
        # 不能等执行失败后再解释——那时用户已经被误导着批准过一次了。
        batch_atomic = not (len(statements) > 1 and has_ddl
                            and ddl_breaks_transaction(resolved["ds"].get("db_type", "")))
        atomicity_warning = None if batch_atomic else (
            f"‼ 本批 {len(statements)} 条语句中含 DDL，{resolved['ds'].get('db_type', '')} 的 DDL "
            f"会隐式提交、无法随批回滚——中途失败时，已执行的语句可能已提交生效，"
            f"需逐条核对，不能假定整批回滚。"
        )
        estimate = None
        if (dml_only and not txn_id and need_confirm
                and WRITE_ESTIMATE_MODE == "on" and WRITE_CONFIRM_MODE == "on"):
            estimate = await _run_db(estimate_affected_rows, sql,
                                     resolved["ds"], resolved["db_code"])
        if has_write:
            await _confirm_write_if_needed(ctx, sql, resolved["db_code"],
                                           resolved["ds"].get("db_type", ""),
                                           force_confirm=True, estimate=estimate,
                                           write_confirm=need_confirm,
                                           atomicity_warning=atomicity_warning)
        if txn_id:
            result_data = await _run_db(execute_in_transaction, txn_id, sql)
        else:
            result_data = await _run_db(execute_any_sql, sql, resolved["ds"], resolved["db_code"])
        # 标注 DDL 类型，供 skill 层安全确认
        if isinstance(result_data, dict):
            result_data["is_ddl"] = has_ddl
        # rows 取值必须判 is not None：`a or b` 会把合法的 0 行（UPDATE 命中 0 行）
        # 吞成 None，与"行数未知"（DDL）在审计里无法区分
        _rows = result_data.get("affected_rows") if isinstance(result_data, dict) else None
        if _rows is None and isinstance(result_data, dict):
            _rows = result_data.get("total_affected_rows")
        audit_log("execute", resolved["db_code"], sql, status="ok",
                  execution_time_ms=result_data.get("execution_time_ms") if isinstance(result_data, dict) else None,
                  rows=_rows, txn_id=txn_id)
        return _normalize_result(result_data)
    except ToolFailure as e:
        _audit_error("execute", resolved_db_code, sql, e, txn_id)
        raise
    except Exception as e:
        _audit_error("execute", resolved_db_code, sql, e, txn_id)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def begin_transaction(db_code: Optional[str] = None, idle_timeout: Optional[int] = None, ctx: Context = None) -> dict[str, Any]:
    """开启数据库事务（v4.0）。创建专用连接并关闭自动提交，返回 txn_id。后续用 query/execute 传 txn_id 在事务中执行，最后用 commit_transaction 提交或 rollback_transaction 回滚。事务空闲超过默认600秒自动回滚释放（MCP_TXN_IDLE_TIMEOUT 可调）。同一事务内所有语句共用同一连接，保证跨语句原子性。v4.5 新增 idle_timeout 参数：按事务覆盖空闲超时秒数（None=用全局默认600），长流程建议直接放宽（如 3600），上限受 MCP_TXN_MAX_IDLE 约束。事务内活动会自动刷新空闲计时；长时间无人操作前可调 extend_transaction 续期，过期后只能重新开始。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    try:
        resolved = await workspace._resolve_datasource(db_code)
        result = await _run_db(begin_transaction_impl, resolved["ds"], resolved["db_code"],
                               None, idle_timeout)
        audit_log("begin_transaction", resolved["db_code"], "", status="ok", detail=result["txn_id"])
        return _normalize_result(result)
    except ToolFailure as e:
        _audit_error("begin_transaction", db_code or "", "", e)
        raise
    except Exception as e:
        _audit_error("begin_transaction", db_code or "", "", e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def extend_transaction(txn_id: str, extend_seconds: Optional[int] = None, ctx: Context = None) -> dict[str, Any]:
    """续期事务（v4.5）。刷新该事务的空闲计时，并可用 extend_seconds 放宽它的空闲阈值（只升不降）。必须在事务过期之前调用——过期后 txn_id 已释放，只能重新 begin_transaction。用 list_transactions 看 idle_remaining_seconds 判断该不该续期。参数：txn_id(必填)、extend_seconds(可选,把空闲阈值提升到至少这么多秒;受 MCP_TXN_MAX_IDLE 上限约束)。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not txn_id:
        raise ToolFailure("缺少必要参数: txn_id")
    try:
        # 先清理过期事务，保证"续期一个已超时事务"得到的是明确的"不存在"提示，
        # 而不是让它悄悄复活（连接可能已被回滚关闭）
        await asyncio.to_thread(_cleanup_expired_transactions)
        result = await _run_db(extend_transaction_impl, txn_id, extend_seconds)
        audit_log("extend_transaction", result.get("db_code", ""), "", status="ok", detail=txn_id)
        return _normalize_result(result)
    except ToolFailure as e:
        # 失败时拿不到 db_code（事务已不在注册表里），至少把 txn_id 与原因带上——
        # 原实现 detail 只有 txn_id，"续期一个已过期事务"和"txn_id 打错"分不开
        _audit_error("extend_transaction", "", f"txn {txn_id}", e)
        raise
    except Exception as e:
        _audit_error("extend_transaction", "", f"txn {txn_id}", e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def commit_transaction(txn_id: str, ctx: Context = None) -> dict[str, Any]:
    """提交事务（v4.0）。提交后事务内所有语句的修改永久生效，专用连接关闭。必须传 begin_transaction 返回的 txn_id。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not txn_id:
        raise ToolFailure("缺少必要参数: txn_id")
    try:
        _cleanup_expired_transactions()
        result = await _run_db(_end_transaction, txn_id, True)
        audit_log("commit_transaction", result.get("db_code", ""), "", status="ok", detail=txn_id)
        return _normalize_result(result)
    except ToolFailure as e:
        _audit_error("commit_transaction", "", f"txn {txn_id}", e)
        raise
    except Exception as e:
        _audit_error("commit_transaction", "", f"txn {txn_id}", e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def rollback_transaction(txn_id: str, ctx: Context = None) -> dict[str, Any]:
    """回滚事务（v4.0）。撤销事务内所有未提交修改，专用连接关闭。必须传 begin_transaction 返回的 txn_id。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not txn_id:
        raise ToolFailure("缺少必要参数: txn_id")
    try:
        result = await _run_db(_end_transaction, txn_id, False)
        audit_log("rollback_transaction", result.get("db_code", ""), "", status="ok", detail=txn_id)
        return _normalize_result(result)
    except ToolFailure as e:
        _audit_error("rollback_transaction", "", f"txn {txn_id}", e)
        raise
    except Exception as e:
        _audit_error("rollback_transaction", "", f"txn {txn_id}", e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def list_transactions(ctx: Context = None) -> dict[str, Any]:
    """列出当前活跃的事务（v4.0）。返回每个事务的 txn_id、数据源、已执行语句数、存活时长、空闲阈值与剩余可空闲时长（v4.5 新增 idle_timeout_seconds / idle_remaining_seconds / expires_soon）。超时事务会先被清理。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    # 轻量操作：不占数据库连接，直接卸载不过闸门（否则会被长查询堵在闸门后面）
    result = await asyncio.to_thread(list_active_transactions)
    return _normalize_result(result)


async def export_csv(sql: str, file_path: str, db_code: Optional[str] = None, timeout: Optional[int] = None, max_rows: Optional[int] = None, ctx: Context = None) -> dict[str, Any]:
    """导出查询结果为 CSV 文件（v4.0 重武器）。只读查询，绕过常规 1000 行返回限制，游标流式分批写入，支持大结果集（默认上限500万行，MCP_EXPORT_MAX_ROWS 可调）。参数：sql(只读查询)、file_path(输出 CSV 路径,目录不存在会自动创建,用 utf-8-sig 编码 Excel 可直接打开)、timeout(可选,长查询超时秒数)、max_rows(可选,覆盖行数上限)。返回 file_path/rows_exported/columns。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not sql or not file_path:
        raise ToolFailure("缺少必要参数: sql / file_path")
    resolved_db_code = db_code or ""
    try:
        resolved = await workspace._resolve_datasource(db_code)
        resolved_db_code = resolved["db_code"]
        result = await _run_db(export_csv_impl, sql, file_path, resolved["ds"],
                               resolved["db_code"], timeout=timeout, max_rows=max_rows)
        audit_log("export_csv", resolved["db_code"], sql, status="ok",
                  execution_time_ms=result.get("execution_time_ms"), rows=result.get("rows_exported"),
                  detail=f"file={result.get('file_path')}")
        return _normalize_result(result)
    except ToolFailure as e:
        _audit_error("export_csv", resolved_db_code, sql, e)
        raise
    except Exception as e:
        _audit_error("export_csv", resolved_db_code, sql, e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def import_csv(file_path: str, table_name: str, db_code: Optional[str] = None, truncate_first: bool = False, ctx: Context = None) -> dict[str, Any]:
    """导入 CSV 文件到表（v4.0 重武器）。按 CSV 表头映射列名，executemany 分批插入（默认500行/批），全部成功才提交、任一失败整批回滚（原子性）。写操作会先弹确认框（数据源取消打勾「写操作需人工审核」的开发库除外，v4.6）。参数：file_path(CSV 路径,首行为列名表头)、table_name(目标表,支持 db.table 跨库语法)、truncate_first(True=导入前先清空表,危险操作;清空走 DELETE FROM 而非 TRUNCATE,以保住"失败整批回滚"的原子性)。注意：CSV 表头列名必须与目标表字段名一致；CSV 中的空字符串按 NULL 写入目标列。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not file_path or not table_name:
        raise ToolFailure("缺少必要参数: file_path / table_name")
    resolved_db_code = db_code or ""
    try:
        resolved = await workspace._resolve_datasource(db_code)
        resolved_db_code = resolved["db_code"]
        # v4.6 数据源级开关：免人工审核的数据源（开发库）连 CSV 行数预览一起跳过
        need_confirm = bool(resolved["ds"].get("write_confirm", 1))
        # 服务端写确认（物理拦截）——导入是写操作，强制确认。
        # v4.4：CSV 行数作为影响行数预览一并显示（只读文件，不进数据库）
        danger_note = "（⚠️ truncate_first=True：导入前清空目标表！）" if truncate_first else ""
        confirm_sql = f"批量导入: {file_path} → 表 {table_name}{danger_note}\n按表头列名匹配，executemany 分批插入，失败整批回滚"
        estimate = None
        if need_confirm and WRITE_ESTIMATE_MODE == "on" and WRITE_CONFIRM_MODE == "on":
            estimate = await _run_db(count_csv_rows, file_path)
        await _confirm_write_if_needed(ctx, confirm_sql, resolved["db_code"], resolved["ds"].get("db_type", ""), force_confirm=True, estimate=estimate, write_confirm=need_confirm)
        result = await _run_db(import_csv_impl, file_path, table_name, resolved["ds"],
                               resolved["db_code"], truncate_first=truncate_first)
        audit_log("import_csv", resolved["db_code"], f"import {file_path} -> {table_name}", status="ok",
                  execution_time_ms=result.get("execution_time_ms"), rows=result.get("rows_inserted"))
        return _normalize_result(result)
    except ToolFailure as e:
        # 用户拒绝确认框就走这里。原实现裸 raise，CSV 没导入、审计里一条记录都没有——
        # 而"有人试图往表里灌一个 CSV 并被拦下"恰恰是最该留痕的事件
        _audit_error("import_csv", resolved_db_code, f"import {file_path} -> {table_name}", e)
        raise
    except Exception as e:
        _audit_error("import_csv", resolved_db_code, f"import {file_path} -> {table_name}", e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def dump_database(db_code: str, file_path: str, include_data: bool = True, seed_only_tables: Optional[list] = None, exclude_patterns: Optional[list] = None, routines: bool = False, skip_confirm: bool = False, ctx: Context = None) -> dict[str, Any]:
    """导出整个数据库的结构与（可选）数据为 .sql 文件，等价于对应厂商的原生 dump 工具。MySQL 数据源会调用本机 mysqldump；其它方言暂不支持会在返回中标明。参数：db_code(已注册的数据源编码)、file_path(输出 .sql 文件的绝对路径)、include_data(非种子表是否带数据,默认 true;false=仅 seed_only_tables 里的表带数据,其余表纯 DDL)、seed_only_tables(仅这些数据表带 INSERT,其余表只导 DDL;典型用法:标准代码表随版本发布;include_data=true 时它们本就包含在全库数据里,不会重复追加)、exclude_patterns(要排除的表名列表,整表排除含结构)、routines(是否一并导出存储过程/函数/触发器/事件)、skip_confirm(跳过二次确认弹窗;仅当环境变量 MCP_DUMP_SKIP_CONFIRM=on 或该数据源关闭了「写操作需人工审核」时才生效,防止调用方自行绕过确认)。密码通过 MYSQL_PWD 环境变量传给 mysqldump,不出现在进程命令行。返回 file_path / size_bytes / table_count / duration_sec。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not db_code or not file_path:
        raise ToolFailure("缺少必要参数: db_code / file_path")
    dump_sql = f"dump -> {file_path}"
    try:
        resolved = await workspace._resolve_datasource(db_code)
        # 方言预检必须在弹确认框之前：让用户批准一个必然跑不了的操作属于确认疲劳。
        # 原先检查长在 dump_module.dump_database 内部，用户点完"接受执行"、
        # 输出目录也已建出来，才被告知"仅支持 MySQL"。
        dump_module.ensure_supported_dialect(
            resolved["ds"].get("db_type", "").lower().strip())
        # 服务端写确认（物理拦截）——导出涉及磁盘 IO + 凭证消费，强制确认。
        # skip_confirm 仅在 MCP_DUMP_SKIP_CONFIRM=on 时生效（脚本/CI 场景显式放开），
        # 防止模型自行传 skip_confirm=True 绕过确认。
        # v4.6：数据源取消打勾「写操作需人工审核」（开发库）时同样免确认——
        # 与 execute/import_csv 同一口径，开发库导出一把梭。
        need_confirm = bool(resolved["ds"].get("write_confirm", 1))
        skip = (skip_confirm and os.environ.get("MCP_DUMP_SKIP_CONFIRM", "").strip().lower() == "on") or not need_confirm
        if not skip:
            confirm_sql = (f"mysqldump {resolved['db_code']} → {file_path}\n"
                           f"include_data={include_data}, seed_only_tables={seed_only_tables}, "
                           f"routines={routines}")
            await _confirm_write_if_needed(ctx, confirm_sql, resolved["db_code"],
                                            resolved["ds"].get("db_type", ""), force_confirm=True)

        fp = Path(file_path).expanduser()
        # mysqldump 是子进程长任务（timeout 600s），不占连接池，故不过闸门
        summary = await asyncio.to_thread(
            dump_module.dump_database,
            db_code=resolved["db_code"], file_path=fp,
            include_data=include_data,
            seed_only_tables=seed_only_tables,
            exclude_patterns=exclude_patterns,
            routines=routines,
        )
        audit_log("dump_database", resolved["db_code"], dump_sql, status="ok",
                  execution_time_ms=int(summary.get("duration_sec", 0) * 1000),
                  rows=summary.get("table_count"),
                  detail=f"file={summary.get('file_path')};bytes={summary.get('size_bytes')}")
        return _normalize_result(summary)
    except ToolFailure as e:
        # 用户拒绝整库导出就走这里。原实现裸 raise，审计里一条记录都没有——
        # 而"有人试图导全库并动用凭据、被拦下"恰恰是最该留痕的一类事件
        _audit_error("dump_database", db_code or "", dump_sql, e)
        raise
    except dump_module.UnsupportedDialectError as e:
        _audit_error("dump_database", db_code or "", dump_sql, e)
        raise ToolFailure(str(e))
    except dump_module.CliMissingError as e:
        _audit_error("dump_database", db_code or "", dump_sql, e)
        raise ToolFailure(str(e))
    except dump_module.CredentialLookupError as e:
        _audit_error("dump_database", db_code or "", dump_sql, e)
        raise ToolFailure(str(e))
    except dump_module.DumpError as e:
        _audit_error("dump_database", db_code or "", dump_sql, e)
        raise ToolFailure(str(e))
    except Exception as e:
        _audit_error("dump_database", db_code or "", dump_sql, e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def query_audit_log(limit: int = 20, tool: Optional[str] = None, db_code: Optional[str] = None, days: Optional[int] = None, ctx: Context = None) -> dict[str, Any]:
    """查询审计日志（v4.0）。每条 SQL 操作（query/execute/事务/导入导出）都落盘为 JSON 行（按天分文件），本工具倒序返回最近记录，用于追溯"到底执行了什么"。参数：limit(返回条数,默认20)、tool(按工具名过滤,如 execute)、db_code(按数据源过滤)、days(扫描最近几天的日志文件,默认取环境变量 MCP_AUDIT_LOG_SCAN_DAYS=30;传 0 表示不限天数、扫描全部文件)。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    # 审计日志是本地文件读取，不占数据库连接，直接卸载不过闸门
    if not AUDIT_ENABLED:
        return {"entries": [], "message": "审计日志已关闭（MCP_AUDIT_LOG_DIR=off）"}
    try:
        return await asyncio.to_thread(_read_audit_entries, limit, tool, db_code, days)
    except Exception as e:
        raise ToolFailure(f"{type(e).__name__}: {e}")


# 审计文件名：audit-YYYYMMDD.jsonl。用文件名里的日期而非 mtime 判定新旧——
# mtime 会因拷贝/同步工具而失真，文件名是写入时生成的，可信。
_AUDIT_FILE_RE = re.compile(r"^audit-(\d{8})\.jsonl$")


def _audit_file_day(fp: Path) -> Optional[str]:
    """从审计文件名取 YYYYMMDD；不符合命名的返回 None。"""
    m = _AUDIT_FILE_RE.match(fp.name)
    return m.group(1) if m else None


def _read_audit_entries(limit: int, tool: Optional[str], db_code: Optional[str],
                        days: Optional[int]) -> dict:
    """读取审计日志（同步，由 asyncio.to_thread 调用）。

    days 为 None 时用配置的 AUDIT_SCAN_DAYS；为 0 时扫描全部文件。
    原实现硬编码只取最近 3 个文件，超过 3 天的记录用本工具永远查不到。
    """
    audit_dir = Path(AUDIT_LOG_DIR)
    scan_days = AUDIT_SCAN_DAYS if days is None else days
    files = sorted(audit_dir.glob("audit-*.jsonl"), reverse=True)
    if scan_days and scan_days > 0:
        cutoff = (date.today() - timedelta(days=scan_days)).strftime("%Y%m%d")
        files = [f for f in files if (_audit_file_day(f) or "99999999") >= cutoff]

    entries = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if tool and e.get("tool") != tool:
                        continue
                    if db_code and e.get("db_code") != db_code:
                        continue
                    entries.append(e)
        except Exception:
            continue
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return {"entries": entries[:limit], "total_scanned": len(entries),
            "files_scanned": len(files), "scan_days": scan_days,
            "audit_dir": str(audit_dir.resolve())}


def cleanup_old_audit_logs(retain_days: int) -> list:
    """删除超过保留天数的审计日志文件，返回被删文件名。MCP 启动时调用一次。

    retain_days <= 0 或审计关闭时直接返回空列表（默认永久保留）。
    """
    if retain_days <= 0 or not AUDIT_ENABLED:
        return []
    audit_dir = Path(AUDIT_LOG_DIR)
    cutoff = (date.today() - timedelta(days=retain_days)).strftime("%Y%m%d")
    removed = []
    for fp in sorted(audit_dir.glob("audit-*.jsonl")):
        day = _audit_file_day(fp)
        if day and day < cutoff:
            try:
                fp.unlink()
                removed.append(fp.name)
                logger.info(f"删除过期审计日志: {fp.name}（早于 {cutoff}）")
            except OSError as e:
                logger.warning(f"删除过期审计日志失败 {fp.name}: {e}")
    return removed


async def list_datasources(ctx: Context = None) -> dict[str, Any]:
    """列出当前工作目录可用的所有数据源，包括默认数据源标记、写操作是否需人工审核（v4.6：write_confirm=0 的数据源是开发库，execute/import_csv/dump_database 对其免确认）"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    # 数据源缓存是本地文件/配置库探测，不占业务连接池，直接卸载不过闸门
    sources = await asyncio.to_thread(load_datasources)
    items = []
    for code, info in sources.items():
        if code == "_default":
            continue
        items.append({
            "db_code": code,
            "db_type": info.get("db_type"),
            "is_default": info.get("is_default", 0) == 1,
            "is_default_mark": "✓" if info.get("is_default", 0) == 1 else "",
            "write_confirm": bool(info.get("write_confirm", 1)),
            "write_confirm_mark": "需审核" if info.get("write_confirm", 1) else "免审核(开发库)"
        })
    return {"data_sources": items}


async def refresh_datasources(ctx: Context = None) -> dict[str, Any]:
    """强制刷新当前工作目录的数据源配置缓存。配置数据库新增或修改数据源后，无需重启 MCP 即可调用此工具使配置生效。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    refreshed_sources = await asyncio.to_thread(load_datasources, True)
    refreshed_codes = [code for code in refreshed_sources.keys() if code != "_default"]
    return {
        "success": True,
        "workspace_dir": normalize_path(workspace._workspace_dir) if workspace._workspace_dir else normalize_path(os.getcwd()),
        "datasource_count": len(refreshed_codes),
        "datasources": refreshed_codes,
        "default_datasource": get_default_db_code(refreshed_sources),
        "message": "数据源配置缓存刷新成功"
    }


async def search_tables(keyword: str, db_code: Optional[str] = None, limit: int = 10, ctx: Context = None) -> dict[str, Any]:
    """根据业务含义搜索匹配的表名。支持表名关键词和表注释模糊匹配(传 % 可浏览全表)。返回 {data:[表名/注释], total_count:匹配总数, returned:本次返回条数, limit}。不再返回 row_count(估算不准,需精确行数请用 query COUNT)。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not keyword:
        raise ToolFailure("缺少必要参数: keyword")
    resolved_db_code = db_code or ""
    try:
        resolved = await workspace._resolve_datasource(db_code)
        resolved_db_code = resolved["db_code"]
        result_data = await _run_db(search_tables_by_keyword, keyword, resolved["ds"],
                                    resolved["db_code"], limit)
        return _normalize_result(result_data)
    except ToolFailure as e:
        _audit_error("search_tables", resolved_db_code, f"search {keyword}", e)
        raise
    except Exception as e:
        _audit_error("search_tables", resolved_db_code, f"search {keyword}", e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def get_schema_info(table_name: str, db_code: Optional[str] = None, ctx: Context = None) -> dict[str, Any]:
    """查询表结构信息，返回字段名、类型、是否为空、键类型、注释等。同时返回索引和约束详情。返回值包含 columns（字段列表）、indexes（索引列表）、constraints（约束列表）。帮助用户了解表结构避免字段名错误。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not table_name:
        raise ToolFailure("缺少必要参数: table_name")
    resolved_db_code = db_code or ""
    try:
        resolved = await workspace._resolve_datasource(db_code)
        resolved_db_code = resolved["db_code"]
        result_data = await _run_db(get_table_schema, table_name, resolved["ds"], resolved["db_code"])
        if not result_data or not result_data.get("columns"):
            return {
                "columns": [],
                "indexes": [],
                "constraints": [],
                "error": f"表 '{table_name}' 不存在或无字段信息"
            }
        return _normalize_result(result_data)
    except ToolFailure as e:
        _audit_error("get_schema_info", resolved_db_code, f"schema {table_name}", e)
        raise
    except Exception as e:
        _audit_error("get_schema_info", resolved_db_code, f"schema {table_name}", e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def explain_query(sql: str, db_code: Optional[str] = None, analyze: bool = False, fmt: Optional[str] = None, ctx: Context = None) -> dict[str, Any]:
    """查看 SQL 的执行计划并自动诊断（v4.5）。方言自适应：MySQL 用 EXPLAIN [FORMAT=JSON|TREE]，PostgreSQL/GaussDB 用 EXPLAIN [(ANALYZE, BUFFERS) FORMAT TEXT|JSON]，Oracle 用 EXPLAIN PLAN FOR + DBMS_XPLAN，达梦用 EXPLAIN。返回 plan（表格方言）或 plan_text（文本方言）、execution_time_ms、warnings（自动诊断：全表扫描/有索引未用/filesort/临时表/行数估算偏差等）、verdict（ok/needs_attention/poor）。参数：sql(要分析的 SELECT)、analyze(默认 false；true 取实际执行计划——MySQL 需 8.0.18+、PG 用 EXPLAIN ANALYZE BUFFERS，会真实执行一遍该 SELECT，大表有实际耗时成本)、fmt(输出格式，MySQL 支持 JSON/TREE、PG 支持 TEXT/JSON，其它方言忽略)。诊断只在表格格式（MySQL TRADITIONAL / PG TEXT）下进行，JSON/TREE 输出会在 note 中说明跳过。方言不支持或版本不够时自动降级为普通 EXPLAIN 并在 note 说明，不会报错。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    if not sql:
        raise ToolFailure("缺少必要参数: sql")
    # 与 query 工具同一道只读守卫：EXPLAIN ANALYZE 会真实执行，
    # 放行面必须与 query 完全一致，不能因为套了层 EXPLAIN 就放宽
    validate_read_only_sql(sql)
    resolved_db_code = db_code or ""
    try:
        resolved = await workspace._resolve_datasource(db_code)
        resolved_db_code = resolved["db_code"]
        result_data = await _run_db(explain_query_impl, sql, resolved["ds"],
                                    resolved["db_code"], analyze, fmt)
        audit_log("explain_query", resolved["db_code"], sql, status="ok",
                  execution_time_ms=result_data.get("execution_time_ms"),
                  detail=f"mode={result_data.get('mode')};verdict={result_data.get('verdict')}")
        return _normalize_result(result_data)
    except ToolFailure as e:
        _audit_error("explain_query", resolved_db_code, sql, e)
        raise
    except Exception as e:
        _audit_error("explain_query", resolved_db_code, sql, e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


async def slow_queries(db_code: Optional[str] = None, limit: int = 20, include_running: bool = True, ctx: Context = None) -> dict[str, Any]:
    """查看慢查询与正在执行的语句（v4.5）。按方言尽力而为：MySQL 读 mysql.slow_log（需 slow_query_log=ON 且 log_output=TABLE，否则回落 SHOW FULL PROCESSLIST 给正在执行的语句），PostgreSQL/GaussDB 读 pg_stat_statements + pg_stat_activity，Oracle/达梦查 v$session/v$sql（需动态性能视图权限）。返回 source / entries / note。注意：本工具不承诺一定有历史慢查询——各方言前置条件差异很大，拿不到会在 note 里说明缺什么并回落给在跑语句，不会报错。参数：limit(返回条数,默认20,上限200)、include_running(是否包含正在执行的语句,默认 true)。"""
    if ctx is not None:
        await _discover_workspace_dir(ctx)
    resolved_db_code = db_code or ""
    try:
        resolved = await workspace._resolve_datasource(db_code)
        resolved_db_code = resolved["db_code"]
        result_data = await _run_db(slow_queries_impl, resolved["ds"],
                                    resolved["db_code"], limit, include_running)
        audit_log("slow_queries", resolved["db_code"], f"slow_queries(limit={limit})",
                  status="ok", rows=result_data.get("entry_count"),
                  detail=f"source={result_data.get('source')}")
        return _normalize_result(result_data)
    except ToolFailure as e:
        _audit_error("slow_queries", resolved_db_code, "slow_queries", e)
        raise
    except Exception as e:
        _audit_error("slow_queries", resolved_db_code, "slow_queries", e)
        raise ToolFailure(f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------------------
# 结果规范化：结构化输出只允许 JSON 可序列化值（outputSchema 校验前置兜底）
# ------------------------------------------------------------------------------
def _normalize_result(result_data: Any) -> Any:
    """保证工具返回值为纯 JSON 可序列化结构（结构化输出经 pydantic 校验，
    datetime/Decimal/bytes 等数据库值会导致 schema 校验失败，这里提前转换，
    与 v2.x json.dumps(default=str) 行为一致）"""
    return json.loads(json.dumps(result_data, ensure_ascii=False, default=str))
