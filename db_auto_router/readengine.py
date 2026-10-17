# -*- coding: utf-8 -*-
"""只读查询引擎：跨库语法 + 大数据量保护 + 重试机制 + 自动摘要 + 执行耗时
——原 835–978 行"""

__author__ = "fengkeju"
import time
from typing import Dict, Any, Optional

from .config import (MAX_RESULT_ROWS, MAX_RETRY_COUNT, RETRY_DELAY,
                     READ_TIMEOUT, SUMMARY_THRESHOLD, SUMMARY_DISPLAY_ROWS)
from .directconn import _open_direct_connection_with_timeout
from .errutil import is_transient_error, safe_close, safe_cursor
from .logcfg import logger
from .pool import get_connection, put_connection
from .sqlguard import validate_read_only_sql, check_sql_limit, strip_trailing_comments
from .util import extract_db_and_table_from_sql

def execute_read_only_query(sql: str, ds: Dict[str, str], default_db: str,
                            max_rows: Optional[int] = None, timeout: Optional[int] = None) -> Any:
    """执行只读查询（无需确认），支持跨库语法 + 大数据量保护 + 重试机制 + 自动摘要 + 执行耗时

    参数：
    - max_rows: 覆盖默认行数上限（MAX_RESULT_ROWS），用于重分析场景需要更多行
    - timeout: 覆盖默认读超时（READ_TIMEOUT 秒），超时临时创建专用连接执行
    """
    effective_max_rows = max_rows if (max_rows and max_rows > 0) else MAX_RESULT_ROWS

    # SQL 安全拦截
    validate_read_only_sql(sql)

    # 解析跨库语法
    target_db, table_name, is_cross_db = extract_db_and_table_from_sql(sql, default_db)

    # 检查是否需要 LIMIT 保护（suggested_limit 跟随 effective_max_rows，
    # 保证"自动追加的 LIMIT"与"行数硬上限"一致）
    limit_info = check_sql_limit(sql, effective_max_rows)
    need_protect = limit_info["need_protect"]
    suggested_limit = limit_info["suggested_limit"]
    has_limit = limit_info["has_limit"]
    existing_limit = limit_info["existing_limit"]

    # 如果需要保护，自动追加行数限制（避免大结果集导致达梦等驱动游标异常 Not Open，并让摘要机制生效）
    limit_warning = None
    auto_limit = None  # 本次自动注入的 LIMIT 值；None 表示没有注入（已有 LIMIT 或非 SELECT）
    if need_protect:
        # 先剥掉尾部注释、再去掉末尾分号，最后才拼 LIMIT。
        # 少了第一步，`SELECT * FROM t -- 备注` 会变成 `... -- 备注 LIMIT 1000`，
        # LIMIT 被行注释一起吞掉，行数保护形同虚设（整表全量拉取）。
        stripped = strip_trailing_comments(sql).rstrip()
        if stripped.endswith(';'):
            stripped = stripped[:-1].rstrip()
        dialect_for_limit = ds.get("db_type", "").lower().strip()
        protect_limit = suggested_limit if suggested_limit and suggested_limit <= effective_max_rows else effective_max_rows
        if dialect_for_limit == "oracle":
            # Oracle 不支持 LIMIT，用 ROWNUM 包装（子查询先物化再过滤，ORDER BY 语义不变）
            sql = f"SELECT * FROM ({stripped}) WHERE ROWNUM <= {protect_limit}"
        else:
            # MySQL / PostgreSQL / GaussDB / 达梦 均支持 LIMIT
            sql = f"{stripped} LIMIT {protect_limit}"
        # 不在这里就定警告文案：注入 LIMIT 只是保护动作，是否值得打扰调用方要看结果。
        # 见执行结束后按实际行数补提示的那段。
        auto_limit = protect_limit

    # 超时覆盖：不同于默认超时时创建专用连接（不经过池），执行完即关闭
    conn = None
    execution_time_ms = 0.0
    direct_conn = None
    columns: list = []
    if timeout and timeout > 0 and timeout != READ_TIMEOUT:
        direct_conn = _open_direct_connection_with_timeout(ds, target_db if is_cross_db else None, timeout)

    # 带重试机制执行
    # 外层 try/finally：专用超时连接必须在整个重试过程结束后才关闭一次。
    # 放在循环内的 finally 里，第 1 次瞬时失败后连接即被关闭，第 2 次重试拿到的是
    # 已关闭的连接，重试对 timeout 覆盖场景完全失效（且报错信息会误导排查方向）。
    try:
        for retry in range(MAX_RETRY_COUNT):
            # 每轮重置累积容器：原先 result_data 在循环外初始化、循环内追加，
            # 第 1 轮 fetch 到 300 行后遇瞬时错误 → 第 2 轮重新执行又追加 300 行 →
            # 返回 600 行且每一行重复一遍，调用方对 data 求和/计数得到 2 倍真值，
            # 且没有任何警告。第 108 行的硬上限只兜内存，兜不住数据重复。
            result_data: list = []
            # 池连接每轮单独借、单独还：下一轮重试拿到的必须是另一条（或同一条
            # 干净状态的）连接，不能把上一轮可能已被打断的连接继续用下去
            pooled_conn = None
            try:
                if direct_conn is not None:
                    conn = direct_conn
                else:
                    conn = get_connection(ds, target_db if is_cross_db else None)
                    pooled_conn = conn
                start_time = time.time()
                # safe_cursor 收拢"取游标→用完关游标"的样板，退出时自动关闭
                with safe_cursor(conn) as cursor:
                    cursor.execute(sql)

                    if cursor.description:
                        columns = [desc[0] for desc in cursor.description]

                        # 分批获取数据，避免一次性加载过多
                        rows_fetched = 0
                        batch_size = 500

                        while True:
                            rows = cursor.fetchmany(batch_size)
                            if not rows:
                                break
                            result_data.extend([dict(zip(columns, row)) for row in rows])
                            rows_fetched += len(rows)

                            # 行数硬上限对"有无 LIMIT"一律生效：显式 LIMIT 99999999 这类写法
                            # 之前会绕过保护把大表整表拉进内存，现在同样只保留 effective_max_rows 行
                            if rows_fetched > effective_max_rows:
                                logger.warning(f"查询返回超过 {effective_max_rows} 行上限（已取 {rows_fetched} 行），结果截断")
                                result_data = result_data[:effective_max_rows]
                                if has_limit:
                                    limit_warning = (f"⚠️ 显式 LIMIT 超过单次返回上限，结果已截断至 {effective_max_rows} 行"
                                                     f"（如需更多数据请传 max_rows 参数提高上限）")
                                else:
                                    limit_warning = f"⚠️ 结果已截断至 {effective_max_rows} 行（实际查询返回更多数据，建议添加 LIMIT 或增大 max_rows）"
                                break

                execution_time_ms = round((time.time() - start_time) * 1000, 2)

                # 成功执行，跳出重试循环
                break

            except Exception as e:
                error_msg = str(e)
                # 达梦等驱动在大结果集/游标异常时报 "Not Open" 等不友好错误，重试后仍失败则转友好提示
                if "not open" in error_msg.lower() and retry >= MAX_RETRY_COUNT - 1:
                    return {
                        "error": "查询失败：数据库游标异常（常见于结果集过大）",
                        "hint": f"请在 SQL 中显式添加 LIMIT {effective_max_rows} 限制返回行数后重试",
                        "original_error": error_msg
                    }
                # 仅瞬时错误（连接/超时/死锁）重试；语法/权限等永久性错误立即抛出，
                # 避免每次 SQL 报错白等 2 次重试延迟（db-diagnose 诊断闭环依赖快速拿到错误原文）
                if retry < MAX_RETRY_COUNT - 1 and is_transient_error(e):
                    logger.warning(f"查询执行失败(第{retry+1}次，瞬时错误)，{RETRY_DELAY}秒后重试: {e}")
                    time.sleep(RETRY_DELAY)
                else:
                    raise e
            finally:
                # 借出的池连接每轮用完即还（成功 break、提前 return、异常重试都走到这）
                if pooled_conn is not None:
                    put_connection(ds, pooled_conn, target_db if is_cross_db else None)
    finally:
        if direct_conn is not None:
            # 专用超时连接用完即关（整个重试过程只关一次）
            safe_close(direct_conn)

    # 结果自动摘要：超过阈值时只返回部分数据 + 统计信息
    dialect = ds.get("db_type", "").lower().strip()

    # 自动 LIMIT 的提示：只在结果真的触达上限时才给。
    # 聚合查询（SELECT COUNT(*) 只回 1 行）和任何小结果集都被注入了 LIMIT，但毫无影响——
    # 一律提示等于每次查询都挂一条噪音，调用方（模型/脚本）很快就会学会忽略警告，
    # 而那恰恰会连带忽略真正表示"数据被截断"的提示。所以这里按实际行数决定说不说。
    if auto_limit is not None and not limit_warning and len(result_data) >= auto_limit:
        limit_warning = (f"⚠️ 未指定行数限制，已自动限制返回 {auto_limit} 行，"
                         f"结果可能不完整（如需更多数据请显式指定 LIMIT 或传 max_rows 参数）")

    if len(result_data) > SUMMARY_THRESHOLD:
        display_rows = result_data[:SUMMARY_DISPLAY_ROWS]
        summary_info = {
            "summary": True,
            "display_rows": display_rows,
            "total_rows": len(result_data),
            "summary_message": f"📊 结果摘要：显示前 {SUMMARY_DISPLAY_ROWS} 行，共 {len(result_data)} 行数据",
            "columns": columns,
            "hint": "完整数据已获取，如需查看全部请使用 LIMIT 或指定范围",
            "execution_time_ms": execution_time_ms,
            "dialect": dialect
        }
        if limit_warning:
            summary_info["warning"] = limit_warning
        return summary_info

    # 返回结果，统一为 dict 格式（包含执行耗时和方言）
    result = {
        "data": result_data,
        "total_rows": len(result_data),
        "execution_time_ms": execution_time_ms,
        "dialect": dialect
    }
    if limit_warning:
        result["warning"] = limit_warning
    return result
