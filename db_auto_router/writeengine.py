# -*- coding: utf-8 -*-
"""写执行引擎：任意SQL（含写操作）+ 跨库语法 + 多语句批量原子执行 + 影响行数预估
——原 1525–1631 行。v4.x 清理：引入 is_transient_error / safe_cursor，消除样板代码。"""

__author__ = "fengkeju"
import time
from typing import Dict, Any, Optional

from .config import MAX_RETRY_COUNT, RETRY_DELAY, MAX_RESULT_ROWS, SUMMARY_DISPLAY_ROWS
from .directconn import _open_direct_connection, _set_conn_autocommit
from .errutil import is_transient_error, safe_close, safe_cursor
from .logcfg import logger
from .pool import get_connection, put_connection
from .sqlguard import split_sql_statements, is_ddl_operation, ddl_breaks_transaction
from .util import extract_db_and_table_from_sql


def execute_any_sql(sql: str, ds: Dict[str, str], default_db: str,
                    conn: Any = None, auto_commit: bool = True) -> Any:
    """执行任意SQL（包括写操作），支持跨库语法 + 执行耗时 + 多语句批量

    安全设计：SQL 执行阶段绝不重试——若 commit 已成功但响应阶段失败（超时/断连），
    盲目重试会导致重复写入（INSERT 执行两次）。仅"获取连接"阶段允许重试，
    因为连接未建立时语句一定没有执行。

    多语句（分号分隔）：一次解析、顺序执行、统一提交/回滚（批量原子性）。
    conn 参数：事务模式下传入事务专用连接，此时不 commit（由事务结束控制）。

    原子性的真实边界（v4.8）：纯 DML 批在所有方言上都可整批回滚；批内一旦
    混入 DDL，mysql/oracle/达梦的 DDL 隐式提交，rollback 撤不掉 DDL 之前
    已落地的语句。此时 atomicity=False，失败信息 MUST 明说"可能已提交"，
    MUST NOT 沿用"整批回滚"的假承诺。详见 sqlguard.ddl_breaks_transaction。
    """
    statements = split_sql_statements(sql)
    if not statements:
        raise ValueError("SQL 为空")
    is_multi = len(statements) > 1
    dialect = ds.get("db_type", "").lower().strip()

    # 原子性判定：只有"纯 DML"或"方言的 DDL 可回滚"时才敢承诺整批回滚
    has_ddl = any(is_ddl_operation(s) for s in statements)
    atomicity = not (is_multi and has_ddl and ddl_breaks_transaction(dialect))
    atomicity_note = None if atomicity else (
        f"本批含 DDL，{dialect} 的 DDL 隐式提交、无法随批回滚；"
        f"若中途失败，已执行的语句可能已提交生效，需逐条核对"
    )

    # 解析跨库语法（以第一条语句/整条 SQL 判断目标库）
    target_db, table_name, is_cross_db = extract_db_and_table_from_sql(statements[0], default_db)

    # 阶段1：获取连接（失败可安全重试，语句尚未执行）
    owns_conn = conn is None
    if owns_conn:
        for attempt in range(MAX_RETRY_COUNT):
            try:
                conn = get_connection(ds, target_db if is_cross_db else None)
                break
            except Exception as e:
                if attempt < MAX_RETRY_COUNT - 1 and is_transient_error(e):
                    logger.warning(f"获取连接失败(第{attempt+1}次，瞬时错误)，{RETRY_DELAY}秒后重试: {e}")
                    time.sleep(RETRY_DELAY)
                else:
                    raise
        if is_multi:
            _set_conn_autocommit(conn, ds.get("db_type", "").lower().strip(), False)

    # 阶段2：执行 SQL（绝不重试，防止 commit 后失败导致重复写入）
    current_idx = 0
    try:
        start_time = time.time()
        results = []
        total_affected = 0
        for idx, stmt in enumerate(statements, start=1):
            current_idx = idx
            # safe_cursor 收拢每条语句的游标生命周期，异常路径也会关游标
            with safe_cursor(conn) as cursor:
                cursor.execute(stmt)
                if cursor.description:
                    columns = [desc[0] for desc in cursor.description]
                    rows = [dict(zip(columns, row)) for row in cursor.fetchmany(MAX_RESULT_ROWS)]
                    results.append({"statement": idx, "type": "resultset", "rows": len(rows), "data": rows})
                else:
                    rc = cursor.rowcount if cursor.rowcount and cursor.rowcount >= 0 else 0
                    total_affected += rc
                    results.append({"statement": idx, "type": "affected", "affected_rows": rc})

        should_commit = auto_commit and owns_conn
        if should_commit:
            conn.commit()
        execution_time_ms = round((time.time() - start_time) * 1000, 2)

        if is_multi:
            details = []
            for r in results:
                if r["type"] == "resultset":
                    details.append({**r, "data": r["data"][:SUMMARY_DISPLAY_ROWS]})
                else:
                    details.append(r)
            return {
                "batch": True,
                "statements_executed": len(statements),
                "total_affected_rows": total_affected,
                "details": details,
                "committed": should_commit,
                "atomicity": atomicity,
                "atomicity_note": atomicity_note,
                "message": f"批量执行成功：{len(statements)} 条语句，共影响 {total_affected} 行" + ("（已提交）" if should_commit else "（事务中，未提交）"),
                "execution_time_ms": execution_time_ms,
                "dialect": dialect
            }

        first = results[0] if results else None
        if first and first["type"] == "resultset":
            return {"data": first["data"], "execution_time_ms": execution_time_ms, "dialect": dialect}
        affected = first["affected_rows"] if first else 0
        return {"affected_rows": affected, "message": f"执行成功，影响 {affected} 行",
                "execution_time_ms": execution_time_ms, "dialect": dialect}

    except Exception as e:
        # 回滚失败通常意味着连接已断，吞掉次要异常避免掩盖主错误
        try:
            conn.rollback()
        except Exception:
            pass
        if is_multi:
            done = current_idx - 1
            if atomicity:
                # 纯 DML 批（或 PG/GaussDB）：rollback 真起作用，可以承诺
                rollback_note = f"前 {done} 条已回滚" if done > 0 else "尚无语句生效"
                raise RuntimeError(
                    f"批量执行失败：第 {current_idx}/{len(statements)} 条语句报错，"
                    f"{rollback_note}: {e}") from e
            # 含 DDL 且方言隐式提交：rollback 撤不回已落地的语句。
            # 这里 MUST 说真话——用户需要知道要去手工核对，而不是被告知"已回滚"。
            partial_note = (f"前 {done} 条可能已提交生效，MUST 逐条核对"
                            if done > 0 else "尚无语句生效")
            raise RuntimeError(
                f"批量执行失败：第 {current_idx}/{len(statements)} 条语句报错: {e}\n"
                f"⚠️ 本批含 DDL，{dialect} 的 DDL 隐式提交、无法随批回滚——"
                f"{partial_note}，MUST NOT 假定已回滚。"
            ) from e
        raise RuntimeError(f"执行失败: {e}") from e
    finally:
        if is_multi and owns_conn:
            # 归还池前恢复 autocommit——成功与失败路径都必须恢复。
            # 只在成功路径恢复的话，批量失败后连接会带着 autocommit=False 回到池里，
            # 停留在隐式事务态，未提交的锁一直持有到 idle 超时（默认 300s）。
            _set_conn_autocommit(conn, dialect, True)
        if owns_conn:
            # 借出的池连接必须归还。此前没有归还 API，连接被永久留在池里，
            # 导致同 key 的并发调用共用同一条物理连接（见 pool.py 的说明）。
            put_connection(ds, conn, target_db if is_cross_db else None)


# ------------------------------------------------------------------------------
# 影响行数预估（v4.4）：确认弹窗的"这条语句会动多少行"
# ------------------------------------------------------------------------------

def estimate_affected_rows(sql: str, ds: Dict[str, str], default_db: str) -> Dict[str, Any]:
    """试执行测影响行数：专用连接上跑一遍 → 读 rowcount → 立即回滚，绝不提交。

    返回 {"rows": int|None, "note": str}。rows 为 None 时 note 说明原因
    （预执行报错 / 未取得行数），调用方把 note 一并显示，避免"没有数字"被
    误读成"没有影响"。

    为什么是"跑完回滚"而不是把 SQL 改写成的 SELECT COUNT(*)：
    改写要解析 UPDATE/DELETE 的整个句型（JOIN、子查询、多表、别名、提示符），
    正则漏一种就算错——而错的行数比没有行数更危险，它会给出虚假的信心。
    试执行对任何句型都精确，且零解析成本。

    副作用边界（调用方已 gate 掉 DDL，只剩纯 DML）：
    - 自增/序列值会被消耗掉（回滚不归还），号段出现空洞
    - 触发器会真实触发一次（带外部副作用的触发器会被点燃）
    这两点无法通过回滚避免，故 MCP_WRITE_ESTIMATE=off 可整体关闭本函数。
    """
    statements = split_sql_statements(sql)
    if not statements:
        return {"rows": None, "note": "空语句，无法预估"}

    # 跨库判定与真执行同源：写语句目标表优先（util.extract_db_and_table_from_sql），
    # 保证试执行和真执行落在同一个库
    target_db, _, is_cross_db = extract_db_and_table_from_sql(statements[0], default_db)
    conn = _open_direct_connection(ds, target_db if is_cross_db else None)
    try:
        try:
            # auto_commit=False + 外部 conn → execute_any_sql 既不提交也不归还连接，
            # 连接始终在本函数手里，回滚后关闭，不进池
            result = execute_any_sql(sql, ds, default_db, conn=conn, auto_commit=False)
        except Exception as e:
            # 试执行就失败 → 真执行也会失败。把错误带回确认窗，用户不必先点
            # "接受执行"再收到同一条报错
            logger.info(f"影响行数预估：预执行未通过 {type(e).__name__}: {e}")
            return {"rows": None, "note": f"预执行未通过（{e}）"}

        rows = (result.get("total_affected_rows") if result.get("batch")
                else result.get("affected_rows"))
        if not isinstance(rows, int):
            return {"rows": None, "note": "未取得影响行数"}
        note = "试执行后已回滚（自增值可能被消耗）"
        return {"rows": rows, "note": note}
    finally:
        # execute_any_sql 成功路径不 commit、失败路径自己 rollback；这里再兜底一次，
        # 然后关闭专用连接
        try:
            conn.rollback()
        except Exception:
            pass
        safe_close(conn)
