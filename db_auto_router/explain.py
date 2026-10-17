# -*- coding: utf-8 -*-
"""执行计划与慢查询（v4.5）——方言自适应的 EXPLAIN + 自动诊断 + 慢查询/在跑语句

为什么单独一个模块：五类方言的 EXPLAIN 语法互不兼容（MySQL 的 FORMAT=/ANALYZE、
PG 的选项括号、Oracle 的 EXPLAIN PLAN FOR + DBMS_XPLAN 两步走、达梦的自有形态），
把这些差异摊进 tools.py 会让工具层瞬间充斥方言分支。收拢到这里，tools.py 只做
参数校验与审计，与 readengine/writeengine 的分工保持一致。

复用既有件，不重写第二套：
- pool.get_connection —— 只读，走连接池，与 readengine.execute_read_only_query 一致
- errutil.safe_cursor / safe_close —— 取游标/关游标/关连接的样板
- sqlguard.validate_read_only_sql —— 与 query 工具同一道只读守卫（EXPLAIN ANALYZE
  会真实执行，绝不能另造一套更宽的判定）
- util.extract_db_and_table_from_sql —— 跨库 db.table 语法解析

安全边界：本模块只做只读诊断。EXPLAIN ANALYZE 对 SELECT 会真实跑一遍（见
explain_query_impl 的 docstring），但守卫已拦住一切非 SELECT 的 ANALYZE，
与 query 工具的放行面完全一致——不新增任何执行能力。
"""

__author__ = "fengkeju"
import re
import time
from typing import Dict, Any, Optional, List, Tuple

from .errutil import safe_close, safe_cursor
from .logcfg import logger
from .pool import get_connection, put_connection
from .sqlguard import validate_read_only_sql
from .util import extract_db_and_table_from_sql

# MySQL 8.0.18 起才支持 EXPLAIN ANALYZE，之前的版本只有普通 EXPLAIN。
_MYSQL_ANALYZE_MIN_VER = (8, 0, 18)

# 行数估算超过此值视为大扫描（MySQL EXPLAIN 的 rows 列）
_LARGE_SCAN_ROWS = 100_000

# PG 估算行数与实际行数偏差超过此倍数，提示统计信息可能过期
_STALE_STATS_RATIO = 10

# FROM/JOIN 后的 "表 [AS] 别名"：把 EXPLAIN 的 table 列（用别名时只给别名）
# 翻回真实表名。诊断说"表 a 全表扫描"等于没说，必须落到真实表名才可执行。
_ALIAS_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+([`"]?[A-Za-z_][\w$]*[`"]?)(?:\s+(?:AS\s+)?([`"]?[A-Za-z_]\w*[`"]?))?',
    re.IGNORECASE)
# 别名位可能吞到的非别名词（这些词后面跟的不是别名）
_ALIAS_STOPWORDS = frozenset({
    "ON", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "STRAIGHT_JOIN",
    "GROUP", "ORDER", "LIMIT", "UNION", "HAVING", "SET", "VALUES", "USE", "AS",
    "PARTITION", "FORCE", "IGNORE", "INDEX",
})


def _build_alias_map(sql: str) -> Dict[str, str]:
    """解析 SQL 里的 别名 → 真实表名 映射。无别名时返回空字典。"""
    mapping: Dict[str, str] = {}
    for table, alias in _ALIAS_RE.findall(sql or ""):
        real = table.strip('`"')
        nick = (alias or "").strip('`"')
        if not nick or nick.upper() in _ALIAS_STOPWORDS:
            continue
        if nick != real:
            mapping[nick] = real
    return mapping


# ------------------------------------------------------------------------------
# 底层取结果
# ------------------------------------------------------------------------------
def _fetch_all(conn, sql: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """执行一条语句并取回全部结果为 dict 列表。无结果集时返回 ([], [])。"""
    with safe_cursor(conn) as cur:
        cur.execute(sql)
        if not cur.description:
            return [], []
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        return columns, rows


def _get_ci(row: Dict[str, Any], *names: str) -> Any:
    """大小写不敏感地取行内字段。

    必须这么做：MySQL EXPLAIN 的列名（type/key/rows/Extra）由服务端决定大小写，
    mysql.connector 原样透传，不同版本/配置下见过全小写与首字母大写两种。
    写死一种会让诊断在某些库上静默失效——而静默失效比报错危险得多。
    """
    lowered = {str(k).lower(): v for k, v in row.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v is not None:
            return v
    return None


def _to_int(v: Any) -> Optional[int]:
    """尽力把值转成 int（None / 非数字 / Decimal 都能安全处理）。"""
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _mysql_server_version(conn) -> Tuple[int, ...]:
    """探测 MySQL 服务端版本号，返回 (major, minor, patch)。探测失败返回 (0,)。"""
    try:
        _, rows = _fetch_all(conn, "SELECT VERSION() AS v")
        if not rows:
            return (0,)
        nums = re.findall(r"\d+", str(list(rows[0].values())[0]))
        return tuple(int(n) for n in nums[:3]) if nums else (0,)
    except Exception as e:
        logger.debug(f"MySQL 版本探测失败: {e}")
        return (0,)


# ------------------------------------------------------------------------------
# 方言适配：拼 EXPLAIN 语句
# ------------------------------------------------------------------------------
def build_explain_sql(dialect: str, sql: str, analyze: bool,
                      fmt: Optional[str], mysql_ver: Tuple[int, ...] = ()) -> Tuple[str, str, Optional[str]]:
    """按方言拼出 EXPLAIN 语句。

    返回 (要执行的SQL, mode, note)：
    - mode: "explain" | "explain_analyze"
    - note: 发生了降级时说明原因；无降级为 None

    降级原则：方言不支持或版本不够时退回普通 EXPLAIN 并在 note 里说清楚，
    不报错。诊断工具的可用性不该被方言细节卡死——拿个不够精确的计划，
    也好过工具在该方言上完全不可用。
    """
    sql = sql.strip().rstrip(";").strip()

    if dialect == "mysql":
        f = (fmt or "").strip().upper()
        if analyze:
            if mysql_ver and mysql_ver < _MYSQL_ANALYZE_MIN_VER:
                # 版本不够：退回普通 EXPLAIN，并把真实版本号告诉调用方
                return (f"EXPLAIN {sql}", "explain",
                        f"MySQL {'.'.join(str(n) for n in mysql_ver)} 不支持 EXPLAIN ANALYZE"
                        f"（需 ≥ 8.0.18），已退回普通 EXPLAIN")
            # EXPLAIN ANALYZE 与 FORMAT= 不能同时用（其输出固定为 TREE 形态）
            if f:
                return (f"EXPLAIN ANALYZE {sql}", "explain_analyze",
                        "EXPLAIN ANALYZE 不支持 FORMAT= 选项，已忽略")
            return (f"EXPLAIN ANALYZE {sql}", "explain_analyze", None)
        if f in ("JSON", "TREE"):
            return (f"EXPLAIN FORMAT={f} {sql}", "explain", None)
        if f:
            return (f"EXPLAIN {sql}", "explain",
                    f"MySQL EXPLAIN 仅支持 FORMAT=JSON/TREE，已忽略 fmt={fmt}")
        return (f"EXPLAIN {sql}", "explain", None)

    if dialect in ("postgres", "gaussdb"):
        f = (fmt or "TEXT").strip().upper()
        if f not in ("TEXT", "JSON"):
            return (f"EXPLAIN {sql}", "explain",
                    f"PostgreSQL/GaussDB EXPLAIN 仅支持 FORMAT TEXT/JSON，已忽略 fmt={fmt}")
        if analyze:
            # ANALYZE + BUFFERS 一起开：实际执行时间与缓冲区命中一起给，
            # 判断"慢在 CPU 还是在 IO"全靠后者
            return (f"EXPLAIN (ANALYZE, BUFFERS, FORMAT {f}) {sql}", "explain_analyze", None)
        return (f"EXPLAIN (FORMAT {f}) {sql}", "explain", None)

    if dialect == "oracle":
        if analyze:
            # Oracle 没有"执行计划 + 实际耗时"二合一的等价物，最接近的是
            # SQL Monitor / DBMS_XPLAN.DISPLAY_CURSOR，但都要额外权限。
            # 不假装支持：给普通计划 + 说明，调用方自己决定要不要去查 Monitor。
            return (f"EXPLAIN PLAN FOR {sql}", "explain",
                    "Oracle 无 EXPLAIN ANALYZE 等价物（实际耗时需 SQL Monitor 或 "
                    "DBMS_XPLAN.DISPLAY_CURSOR，且需额外权限），已退回普通执行计划")
        return (f"EXPLAIN PLAN FOR {sql}", "explain", None)

    if dialect == "dm":
        if analyze:
            return (f"EXPLAIN {sql}", "explain",
                    "达梦 EXPLAIN ANALYZE 未经真机验证，已退回普通 EXPLAIN")
        return (f"EXPLAIN {sql}", "explain", None)

    return ("", "explain", f"不支持的数据库类型: {dialect}")


# ------------------------------------------------------------------------------
# 诊断层：把无语义的计划行翻成可执行结论
# ------------------------------------------------------------------------------
def _warn(level: str, code: str, message: str) -> Dict[str, str]:
    return {"level": level, "code": code, "message": message}


def _diagnose_mysql(rows: List[Dict[str, Any]], alias_map: Dict[str, str] = None) -> List[Dict[str, str]]:
    """解读 MySQL EXPLAIN（TRADITIONAL 表格格式）的行。

    判读优先级：全表扫描 > 有索引没用上 > 排序/临时表 > 大扫描估算。
    顺序即严重度，调用方拿到的 warnings 数组也是这个次序。

    alias_map：SQL 里的 别名→真实表名。EXPLAIN 的 table 列在用别名时只给别名，
    不翻回去的话诊断说的是"表 a 全表扫描"，读的人还得自己对一遍 SQL。
    """
    alias_map = alias_map or {}
    warnings: List[Dict[str, str]] = []
    for row in rows:
        raw_table = str(_get_ci(row, "table") or "?")
        table = alias_map.get(raw_table, raw_table)
        access = str(_get_ci(row, "type") or "").upper()
        key = _get_ci(row, "key")
        possible = _get_ci(row, "possible_keys")
        extra = str(_get_ci(row, "Extra") or "")
        rows_est = _to_int(_get_ci(row, "rows")) or 0

        if access == "ALL":
            # 全表扫描：扫描量越大越严重
            if rows_est >= _LARGE_SCAN_ROWS:
                warnings.append(_warn(
                    "critical", "full_scan_large",
                    f"表 {table} 全表扫描（type=ALL），估算扫描 {rows_est:,} 行"))
            else:
                warnings.append(_warn(
                    "warning", "full_scan",
                    f"表 {table} 全表扫描（type=ALL），估算扫描 {rows_est:,} 行"))

        # key 为空但 possible_keys 有值 = 优化器认为该走索引却没走。
        # 这比"压根没有可用索引"更值得说：通常是统计信息过期或函数/隐式转换破坏了索引。
        # 消息里带上行数估算——小表上全表扫描本就是优化器的正确选择，
        # 读的人看到"估算 144 行"就知道这条可以放过，不用回去质疑优化器。
        if key is None and possible:
            warnings.append(_warn(
                "warning", "no_index_used",
                f"表 {table} 有可用索引（{possible}）但优化器未使用（估算 {rows_est:,} 行）"))

        if "using filesort" in extra.lower():
            warnings.append(_warn(
                "warning", "filesort",
                f"表 {table} 使用 Using filesort（额外排序，无法走索引有序性）"))
        if "using temporary" in extra.lower():
            warnings.append(_warn(
                "warning", "temp_table",
                f"表 {table} 使用 Using temporary（生成临时表，GROUP BY/DISTINCT 代价高）"))
        if "block nested loop" in extra.lower():
            warnings.append(_warn(
                "warning", "bnl_join",
                f"表 {table} 使用 Block Nested Loop Join（被驱动表无可用索引时的连接方式）"))

        if rows_est >= _LARGE_SCAN_ROWS and access != "ALL":
            warnings.append(_warn(
                "warning", "large_scan_estimate",
                f"表 {table} 估算扫描 {rows_est:,} 行（访问方式 {access or '未知'}）"))
    return warnings


def _diagnose_pg(plan_text: str) -> List[Dict[str, str]]:
    """解读 PostgreSQL/GaussDB 的文本执行计划（TEXT 格式）。

    PG 的计划是树形文本而非表格，只能按行匹配。判读优先级：
    全表扫描 > 磁盘排序 > Hash Join 分批落盘 > 统计信息过期。

    估算/实际行数配对的格式（实测确认，别凭记忆写）：
        (cost=0.00..12.00 rows=3 width=8) (actual time=0.01..45.20 rows=45678 loops=1)
    即先 rows=N，再 actual time=a..b rows=M——不存在 "actual rows=" 这个字面量。
    """
    warnings: List[Dict[str, str]] = []

    # --- 统计信息过期：估算与实际行数偏差过大 ---
    for est_s, act_s in re.findall(
            r"\brows=(\d+)\b.*?\bactual\s+time=[\d.]+(?:\s*\.\.\s*[\d.]+)?\s+rows=(\d+)\b",
            plan_text, flags=re.DOTALL):
        est, act = int(est_s), int(act_s)
        if est > 0 and act > max(est * _STALE_STATS_RATIO, 100):
            warnings.append(_warn(
                "warning", "stale_stats",
                f"行数估算 {est:,} 与实际 {act:,} 偏差超过 {_STALE_STATS_RATIO}×，"
                f"统计信息可能过期，建议对相关表执行 ANALYZE"))

    for line in plan_text.splitlines():
        low = line.lower()

        # 全表扫描：Seq Scan on <table>。表名要抽出来，只说"顺序扫描"等于没说
        seq_m = re.search(
            r"\bSeq\s+Scan\s+on\s+([`\"]?[\w$.]+[`\"]?)", line, re.IGNORECASE)
        if seq_m:
            table = seq_m.group(1).strip('`"')
            warnings.append(_warn("warning", "full_scan", f"表 {table} 顺序全表扫描"))

        # 磁盘排序：external merge 表示排序放不进 work_mem
        if "sort method: external merge" in low:
            warnings.append(_warn(
                "warning", "disk_sort",
                "排序使用磁盘（external merge），work_mem 可能不足"))
        if "sort method: external sort" in low:
            warnings.append(_warn(
                "warning", "disk_sort",
                "排序使用磁盘（external sort），work_mem 可能不足"))

        # Hash Join 分批落盘：真实格式是 "Batches: N"，N>1 即分批
        batch_m = re.search(r"\bBatches:\s*(\d+)", line)
        if batch_m and int(batch_m.group(1)) > 1:
            warnings.append(_warn(
                "warning", "hash_batch",
                f"Hash Join 分批落盘（Batches: {batch_m.group(1)}），work_mem 不足"))

        # 索引扫描的 filter：表示索引没完全覆盖，回了堆再过滤
        if re.search(r"\bIndex\s+(?:Only\s+)?Scan\s+using\s+\S+\s+on\s+.*\bFilter:", line, re.IGNORECASE):
            tbl_m = re.search(r"\bon\s+([`\"]?[\w$.]+[`\"]?)", line, re.IGNORECASE)
            tbl = tbl_m.group(1).strip('`"') if tbl_m else "?"
            warnings.append(_warn(
                "info", "index_filter",
                f"表 {tbl} 索引扫描后仍有 Filter（索引未完全覆盖查询条件）"))
    return warnings


def _verdict(warnings: List[Dict[str, str]]) -> str:
    """汇总 verdict：无 warning=ok；仅 info=ok（提示性，不算问题）；
    有 warning=needs_attention；有 critical=poor。

    info 单独存在时不抬结论——它只是"顺带提醒"，把 verdict 变成
    needs_attention 会让调用方对每条 info 也紧张，等于没有分级。
    """
    levels = {w.get("level") for w in warnings}
    if "critical" in levels:
        return "poor"
    if "warning" in levels:
        return "needs_attention"
    return "ok"


# ------------------------------------------------------------------------------
# 执行计划主入口
# ------------------------------------------------------------------------------
def explain_query_impl(sql: str, ds: Dict[str, str], db_code: str,
                       analyze: bool = False, fmt: Optional[str] = None) -> Dict[str, Any]:
    """执行 EXPLAIN 并返回计划 + 自动诊断。

    参数：
    - analyze: True 时取"实际执行"计划（MySQL 8.0.18+ 的 EXPLAIN ANALYZE /
      PG 的 EXPLAIN ANALYZE BUFFERS）。注意这会**真实执行一遍 SELECT**，
      大表上有实际耗时成本，默认关闭。
    - fmt: 计划输出格式。MySQL 支持 JSON/TREE，PG 支持 TEXT/JSON；
      其它方言忽略并在返回的 note 里说明。

    返回：dialect / mode / plan(表格方言) / plan_text(文本方言) /
    execution_time_ms / analyze_supported / warnings / verdict / note

    诊断只在表格格式（MySQL TRADITIONAL / PG TEXT）下进行——JSON/TREE
    输出是单列文本，行级判读无从下手，此时跳过诊断并在 note 中说明，
    而不是给出一份空的 warnings 让人误以为"没问题"。
    """
    dialect = (ds.get("db_type") or "").lower().strip()

    # 跨库语法：EXPLAIN 的 FROM/JOIN 决定了该连哪个库，与 query 工具同一套解析
    target_db, _, is_cross_db = extract_db_and_table_from_sql(sql, db_code)
    # 别名→真实表名：诊断消息里要报真实表名（见 _build_alias_map 的说明）
    alias_map = _build_alias_map(sql)

    notes: List[str] = []
    mysql_ver: Tuple[int, ...] = ()
    analyze_supported = True

    conn = get_connection(ds, target_db if is_cross_db else None)
    try:
        if dialect == "mysql":
            mysql_ver = _mysql_server_version(conn)
            analyze_supported = bool(mysql_ver) and mysql_ver >= _MYSQL_ANALYZE_MIN_VER

        explain_sql, mode, note = build_explain_sql(dialect, sql, analyze, fmt, mysql_ver)
        if note:
            notes.append(note)
        if not explain_sql:
            raise ValueError(f"无法为方言 {dialect} 构造 EXPLAIN 语句")

        start = time.time()
        columns, rows = _fetch_all(conn, explain_sql)
        execution_time_ms = round((time.time() - start) * 1000, 2)

        # Oracle：EXPLAIN PLAN FOR 只把计划写进 PLAN_TABLE，得再查一次才看得见
        if dialect == "oracle":
            try:
                _, disp = _fetch_all(
                    conn,
                    "SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY)")
                plan_text = "\n".join(
                    str(list(r.values())[0]) for r in disp if r)
            except Exception as e:
                plan_text = ""
                notes.append(f"读取 DBMS_XPLAN.DISPLAY 失败（可能无权限）: {e}")

        out: Dict[str, Any] = {
            "dialect": dialect,
            "db_code": db_code,
            "mode": mode,
            "sql": sql,
            "execution_time_ms": execution_time_ms,
            "analyze_supported": analyze_supported if dialect == "mysql" else None,
        }

        # 表格方言给结构化行，文本方言给文本；两者按方言只填一个
        if dialect in ("postgres", "gaussdb") or (dialect == "oracle"):
            # PG 的 TEXT 格式与 Oracle 的 DBMS_XPLAN 都是"单列多行文本"
            if dialect == "oracle":
                out["plan_text"] = plan_text
                out["warnings"] = []
                out["verdict"] = "ok"
                notes.append("Oracle 计划为文本形态，未做自动诊断（请人工阅读 plan_text）")
            else:
                text = "\n".join(
                    str(list(r.values())[0]) for r in rows if r) if rows else ""
                out["plan_text"] = text
                if (fmt or "TEXT").strip().upper() == "JSON":
                    out["warnings"] = []
                    out["verdict"] = "ok"
                    notes.append("FORMAT=JSON 为单列 JSON 文本，未做自动诊断")
                else:
                    w = _diagnose_pg(text)
                    out["warnings"] = w
                    out["verdict"] = _verdict(w)
        else:
            # MySQL / 达梦：表格行
            out["plan"] = rows
            f = (fmt or "").strip().upper()
            if dialect == "mysql" and f in ("JSON", "TREE"):
                out["warnings"] = []
                out["verdict"] = "ok"
                notes.append(f"FORMAT={f} 为单列文本，未做自动诊断")
            elif dialect == "dm":
                out["warnings"] = []
                out["verdict"] = "ok"
                notes.append("达梦计划形态未做自动诊断（请人工阅读 plan）")
            else:
                w = _diagnose_mysql(rows, alias_map)
                out["warnings"] = w
                out["verdict"] = _verdict(w)

        if mode == "explain_analyze":
            notes.append("EXPLAIN ANALYZE 已真实执行该 SELECT，execution_time_ms 为实际耗时")
        out["note"] = "；".join(notes) if notes else None
        return out
    finally:
        # 借出的池连接必须归还（此前只写"放回"却没有归还 API，连接一直留在池里）
        put_connection(ds, conn, target_db if is_cross_db else None)


# ------------------------------------------------------------------------------
# 慢查询 / 在跑语句
# ------------------------------------------------------------------------------
def _mysql_slow_queries(conn, limit: int) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """MySQL：优先读 mysql.slow_log（慢日志落表时），否则回落 SHOW FULL PROCESSLIST。"""
    notes: List[str] = []
    variables: Dict[str, str] = {}
    try:
        _, var_rows = _fetch_all(
            conn,
            "SHOW VARIABLES WHERE Variable_name IN "
            "('slow_query_log','long_query_time','log_output')")
        for r in var_rows:
            k = str(_get_ci(r, "Variable_name") or "")
            variables[k.lower()] = str(_get_ci(r, "Value") or "")
    except Exception as e:
        notes.append(f"读取慢日志相关变量失败: {e}")

    log_output = variables.get("log_output", "FILE").upper()
    slow_on = variables.get("slow_query_log", "OFF").upper() == "ON"

    if slow_on and "TABLE" in log_output:
        try:
            _, rows = _fetch_all(
                conn,
                "SELECT start_time, user_host, db, query_time, rows_examined, sql_text "
                "FROM mysql.slow_log ORDER BY start_time DESC LIMIT " + str(int(limit)))
            return ("mysql.slow_log", rows, notes)
        except Exception as e:
            notes.append(f"读取 mysql.slow_log 失败（可能需要 TABLE 权限）: {e}")

    if not slow_on:
        notes.append("slow_query_log 未开启，无法提供历史慢查询")
    elif "TABLE" not in log_output:
        notes.append(f"log_output={log_output}（未落表），无法通过 SQL 读取慢日志；"
                     f"如需本工具读取，请设 log_output=TABLE 并开启 slow_query_log")

    # 回落：当前正在执行的语句
    _, proc = _fetch_all(conn, "SHOW FULL PROCESSLIST")
    running = [r for r in proc if (_to_int(_get_ci(r, "Time")) or 0) > 0]
    running = running[:limit]
    if running:
        notes.append("以下为正在执行的语句（Time>0），非历史慢查询")
    else:
        notes.append("当前无正在执行的语句（Time>0）")
    return ("SHOW FULL PROCESSLIST", running, notes)


def _pg_slow_queries(conn, limit: int, dialect: str) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """PostgreSQL/GaussDB：pg_stat_statements（装了才有效）+ pg_stat_activity 在跑语句。"""
    notes: List[str] = []
    entries: List[Dict[str, Any]] = []
    source = "pg_stat_activity"
    try:
        _, rows = _fetch_all(
            conn,
            "SELECT round(total_exec_time::numeric, 2) AS total_ms, "
            "round(mean_exec_time::numeric, 2) AS mean_ms, calls, rows, query "
            "FROM pg_stat_statements "
            "ORDER BY total_exec_time DESC LIMIT " + str(int(limit)))
        if rows:
            entries.extend(rows)
            source = "pg_stat_statements"
        else:
            notes.append("pg_stat_statements 无数据（扩展可能未安装或刚重启未积累）")
    except Exception as e:
        notes.append(f"pg_stat_statements 不可用（需 CREATE EXTENSION pg_stat_statements）: {e}")

    try:
        _, act = _fetch_all(
            conn,
            "SELECT pid, round(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 3) "
            "       AS duration_sec, state, wait_event_type, wait_event, query "
            "FROM pg_stat_activity WHERE state = 'active' "
            "ORDER BY duration_sec DESC LIMIT " + str(int(limit)))
        for r in act:
            entries.append({"source": "running", **r})
    except Exception as e:
        notes.append(f"读取 pg_stat_activity 失败: {e}")

    return (source, entries, notes)


def _oracle_dm_slow_queries(conn, limit: int, dialect: str) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """Oracle/达梦：查 v$session 里活跃的会话与其 SQL。需相应权限，拿不到就说明缺什么。"""
    notes: List[str] = []
    try:
        if dialect == "oracle":
            _, rows = _fetch_all(
                conn,
                "SELECT s.sid, s.serial#, s.username, s.status, s.sql_id, "
                "       s.last_call_et AS last_call_sec, q.sql_text "
                "FROM v$session s LEFT JOIN v$sql q ON q.sql_id = s.sql_id "
                "WHERE s.status = 'ACTIVE' AND s.type = 'USER' "
                "ORDER BY s.last_call_et DESC")
        else:
            # 达梦动态视图命名与 Oracle 相近（v$session / v$sql）
            _, rows = _fetch_all(
                conn,
                "SELECT sid, serial_id, user_name, state, sql_id, "
                "       last_send_time FROM v$session WHERE state = 'ACTIVE'")
        if not rows:
            notes.append("当前无活跃用户会话")
        return (f"{dialect}.v$session", rows[:limit], notes)
    except Exception as e:
        notes.append(f"读取 {dialect} 动态性能视图失败（通常需要 DBA 或 SELECT ANY DICTIONARY 权限）: {e}")
        return (f"{dialect}.v$session", [], notes)


def slow_queries_impl(ds: Dict[str, str], db_code: str, limit: int = 20,
                      include_running: bool = True) -> Dict[str, Any]:
    """返回慢查询/正在执行的语句（只读诊断，尽力而为）。

    各方言能力差异很大（MySQL 要 log_output=TABLE，PG 要装 pg_stat_statements，
    Oracle/达梦要动态性能视图权限），因此本工具**不承诺一定有历史慢查询**：
    拿不到就在 note 里说明缺什么、并回落给出正在执行的语句。
    诊断工具的可用性不该阻塞主流程，故失败不抛异常。
    """
    limit = max(1, min(int(limit or 20), 200))
    dialect = (ds.get("db_type") or "").lower().strip()
    conn = get_connection(ds, None)
    try:
        start = time.time()
        if dialect == "mysql":
            source, entries, notes = _mysql_slow_queries(conn, limit)
        elif dialect in ("postgres", "gaussdb"):
            source, entries, notes = _pg_slow_queries(conn, limit, dialect)
        elif dialect in ("oracle", "dm"):
            source, entries, notes = _oracle_dm_slow_queries(conn, limit, dialect)
        else:
            return {"dialect": dialect, "source": None, "entries": [],
                    "note": f"不支持的数据库类型: {dialect}"}

        if not include_running and source == "SHOW FULL PROCESSLIST":
            entries = []
            notes.append("include_running=False，已略过正在执行的语句")

        return {
            "dialect": dialect,
            "db_code": db_code,
            "source": source,
            "entry_count": len(entries),
            "entries": entries,
            "execution_time_ms": round((time.time() - start) * 1000, 2),
            "note": "；".join(notes) if notes else None,
        }
    finally:
        # 借出的池连接必须归还
        put_connection(ds, conn, None)
