# -*- coding: utf-8 -*-
"""只读查询引擎：跨库语法 + 大数据量保护 + 重试机制 + 自动摘要 + 执行耗时
——原 835–978 行"""

__author__ = "fengkeju"
import time
from typing import Dict, Any, Optional

from .config import (MAX_RESULT_ROWS, MAX_RETRY_COUNT, RETRY_DELAY,
                     READ_TIMEOUT, SUMMARY_THRESHOLD, SUMMARY_DISPLAY_ROWS,
                     QUERY_MAX_ROWS_CAP, QUERY_TIMEOUT_MAX, clamp_override)
from .directconn import _open_direct_connection_with_timeout
from .errutil import is_transient_error, safe_close, safe_cursor
from .logcfg import logger
from .pool import get_connection, put_connection, pool_key_for
from .readonly import enable_read_only, release_read_only, readonly_field
from .sqlguard import validate_read_only_sql, check_sql_limit, strip_trailing_comments
from .util import extract_db_and_table_from_sql

def execute_read_only_query(sql: str, ds: Dict[str, str], default_db: str,
                            max_rows: Optional[int] = None, timeout: Optional[int] = None) -> Any:
    """执行只读查询（无需确认），支持跨库语法 + 大数据量保护 + 重试机制 + 自动摘要 + 执行耗时

    参数：
    - max_rows: 覆盖默认行数上限（MAX_RESULT_ROWS），用于重分析场景需要更多行。
        上限受 QUERY_MAX_ROWS_CAP 硬顶约束，超顶会被夹回去并在返回值 warning 里说明
    - timeout: 覆盖默认读超时（READ_TIMEOUT 秒），超时临时创建专用连接执行。
        上限受 QUERY_TIMEOUT_MAX 硬顶约束，同上
    """
    # 硬顶：max_rows/timeout 都是调用方（模型）自己决定传什么的参数，
    # 不设顶就能传个 10000000 把整份结果集撑爆上下文。夹到顶时必须告知，
    # 不能默默照做、也不能静默改小。
    effective_max_rows, rows_clamped = clamp_override(max_rows, MAX_RESULT_ROWS, QUERY_MAX_ROWS_CAP)
    effective_timeout, timeout_clamped = clamp_override(timeout, READ_TIMEOUT, QUERY_TIMEOUT_MAX)
    clamp_warning = "\n".join(w for w in (
        (f"⚠️ 请求的 max_rows={max_rows} 超过单次返回硬顶 {QUERY_MAX_ROWS_CAP} 行，"
         f"已限制为 {effective_max_rows} 行（更大结果集请用 export_csv 导出到文件）") if rows_clamped else None,
        (f"⚠️ 请求的 timeout={timeout} 秒超过单次查询超时硬顶 {QUERY_TIMEOUT_MAX} 秒，"
         f"已限制为 {effective_timeout} 秒") if timeout_clamped else None,
    ) if w)

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
    dialect = ds.get("db_type", "").lower().strip()
    # 第三层（数据库只读兜底）的连接身份与生效标记。初始化在循环外：
    # 循环内每借出一条连接就重开一次，finally 统一恢复；direct_conn 分支不设
    # （用完即关，无需恢复），此时返回字段为 false + note
    ro_key = None
    ro_enforced = False
    if effective_timeout != READ_TIMEOUT:
        direct_conn = _open_direct_connection_with_timeout(ds, target_db if is_cross_db else None, effective_timeout)

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
                    # 第三层：数据库只读兜底。该库不认只读语法就降级，
                    # 调用方在返回值 note 里说明，不假装安全
                    ro_key = pool_key_for(ds, target_db if is_cross_db else None)
                    ro_enforced = enable_read_only(conn, dialect, ro_key)
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
                    err_result = {
                        "error": "查询失败：数据库游标异常（常见于结果集过大）",
                        "hint": f"请在 SQL 中显式添加 LIMIT {effective_max_rows} 限制返回行数后重试",
                        "original_error": error_msg
                    }
                    # 请求被夹过也要在这里说：否则调用方看到的只是"结果集过大"，
                    # 会拿着同一个 max_rows 反复重试，每次都在同一处失败。
                    if clamp_warning:
                        err_result["warning"] = clamp_warning
                    return err_result
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
                    # 恢复必须先于归还：连接一旦回池就可能被别的调用者取走，
                    # 带着只读标记会让他们后续的 execute 写库失败
                    if ro_enforced and not release_read_only(
                            conn, dialect, ro_key, ds, target_db if is_cross_db else None):
                        pooled_conn = None   # 恢复失败：连接已弃，MUST NOT 回池
                if pooled_conn is not None:
                    put_connection(ds, pooled_conn, target_db if is_cross_db else None)
    finally:
        if direct_conn is not None:
            # 专用超时连接用完即关（整个重试过程只关一次）
            safe_close(direct_conn)

    # 结果自动摘要：超过阈值时只返回部分数据 + 统计信息
    # （dialect 已在连接前算好，第三层要用）

    # 统一出口："参数被夹"与"结果被截断"两类提示合并进同一个 warning 键
    # （调用方只认这一个键，不新增字段），两类同时出现时都可见。
    warning = "\n".join(w for w in (clamp_warning, limit_warning) if w)

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
        if warning:
            summary_info["warning"] = warning
        summary_info.update(readonly_field(dialect, ro_enforced))
        return summary_info

    # 返回结果，统一为 dict 格式（包含执行耗时和方言）
    result = {
        "data": result_data,
        "total_rows": len(result_data),
        "execution_time_ms": execution_time_ms,
        "dialect": dialect
    }
    if warning:
        result["warning"] = warning
    result.update(readonly_field(dialect, ro_enforced))
    return result
