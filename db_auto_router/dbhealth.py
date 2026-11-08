# -*- coding: utf-8 -*-
"""db_health：索引卫生检查（只读诊断，尽力而为）。

定位：把"哪些索引冗余、哪些疑似没用"的判断逻辑和五方言各自的 catalog SQL 编码好，
免得模型每次手写 5 套查询还写错。**只建议、绝不动手**——不 DROP、不 ALTER。

两条检查：
- 冗余索引（结构判断，高置信）：某索引的列是另一索引的最左前缀，或完全重复。
  安全边界：唯一/主键索引提供约束，更长索引不覆盖前缀的唯一性，故**永不判它们可删**。
- 未用索引（usage 判断，低置信）：统计窗口内零扫描。窗口会清零（PG idx_scan 随
  重启/pg_stat_reset、MySQL perf_schema 自启动、Oracle 需先开 MONITORING），
  所以必须表述成"观测窗口内无扫描"的**候选**，绝不讲成"可安全删除"。

按方言尽力而为 + 降级（沿用 slow_queries 的模式）：拿不到统计/缺权限就在 note 里
说清缺什么，不假装有结果，绝不抛异常中断主流程。
"""
from __future__ import annotations

__author__ = "fengkeju"
import time
from typing import Any, Dict, List

from .errutil import safe_cursor
from .logcfg import logger
from .pool import get_connection, put_connection


def find_redundant_indexes(indexes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """找出可删候选的冗余索引（纯函数，输入已规整的索引清单）。

    输入每项：{table, name, columns:[...], unique:bool, primary:bool}（columns 有序）。
    规则：
    - 只把"非唯一、非主键"的索引判为可删——唯一/主键提供约束，长索引不覆盖前缀唯一性；
    - 最左前缀：a 的列是 b 的列的严格前缀（a 更短）→ a 被 b 覆盖；
    - 完全重复：同表同列的非约束索引，保留一个（字典序第一个），其余标冗余。
    """
    findings: List[Dict[str, Any]] = []
    by_table: Dict[str, List[Dict[str, Any]]] = {}
    for idx in indexes:
        by_table.setdefault(idx["table"], []).append(idx)

    for table, idxs in by_table.items():
        for a in idxs:
            if a.get("unique") or a.get("primary"):
                continue  # 约束索引不判冗余
            acols = list(a["columns"])
            covered = None
            reason = None
            covered_cols = None
            # 1) 严格最左前缀：被更长的索引覆盖
            for b in idxs:
                if b["name"] == a["name"]:
                    continue
                bcols = list(b["columns"])
                if len(bcols) > len(acols) and bcols[:len(acols)] == acols:
                    covered, reason, covered_cols = b["name"], "最左前缀", bcols
                    break
            # 2) 没有更长覆盖者时，才看是否是完全重复（保留字典序第一个）
            if covered is None:
                dups = sorted(
                    (x for x in idxs
                     if not x.get("unique") and not x.get("primary")
                     and list(x["columns"]) == acols),
                    key=lambda x: x["name"])
                if len(dups) > 1 and a["name"] != dups[0]["name"]:
                    covered, reason, covered_cols = dups[0]["name"], "完全重复", acols
            if covered is not None:
                findings.append({
                    "table": table,
                    "index": a["name"],
                    "columns": acols,
                    "covered_by": covered,
                    "reason": reason,
                    "covered_by_columns": covered_cols,
                })
    return findings


# ------------------------------------------------------------------------------
# 编排 + 各方言接线
# ------------------------------------------------------------------------------
def _collect(cursor) -> List[Dict[str, Any]]:
    """游标结果集 → dict 列表（列名取 cursor.description）。"""
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description] if cursor.description else []
    return [dict(zip(cols, r)) for r in rows]


def _drop_sql(dialect: str, table: str, index: str) -> str:
    """按方言给一条"删这个索引"的建议 SQL（只展示，不执行）。"""
    if dialect == "mysql":
        return f"ALTER TABLE {table} DROP INDEX {index}"
    return f"DROP INDEX {index}"  # postgres / gaussdb / oracle / dm


def _result(dialect, db_code, redundant, unused, note, start):
    return {
        "dialect": dialect,
        "db_code": db_code,
        "redundant_indexes": redundant,
        "unused_indexes": unused,
        "note": note,
        "execution_time_ms": round((time.time() - start) * 1000, 2),
    }


def _pg_index_health(conn, dialect: str):
    """PG/GaussDB：pg_index 拿索引列（判冗余）+ pg_stat_user_indexes 拿 idx_scan（判未用）。"""
    notes: List[str] = []
    with safe_cursor(conn) as cur:
        cur.execute("""
            SELECT i.relname AS index_name, t.relname AS table_name,
                   ix.indisunique, ix.indisprimary, a.attname AS column_name,
                   array_position(ix.indkey, a.attnum) AS seq
            FROM pg_index ix
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
            WHERE n.nspname = current_schema() AND ix.indisvalid
            ORDER BY i.relname, seq
        """)
        rows = _collect(cur)

    grouped: Dict[tuple, list] = {}
    meta: Dict[tuple, tuple] = {}
    for r in rows:
        key = (r["table_name"], r["index_name"])
        grouped.setdefault(key, []).append((r["seq"], r["column_name"]))
        meta[key] = (bool(r["indisunique"]), bool(r["indisprimary"]))
    indexes = []
    for (table, name), cols in grouped.items():
        ordered = [c for _, c in sorted(cols, key=lambda x: (x[0] is None, x[0]))]
        uniq, pri = meta[(table, name)]
        indexes.append({"table": table, "name": name, "columns": ordered,
                        "unique": uniq, "primary": pri})

    unused: List[Dict[str, Any]] = []
    try:
        with safe_cursor(conn) as cur:
            cur.execute("""
                SELECT t.relname AS table_name, i.relname AS index_name, s.idx_scan
                FROM pg_stat_user_indexes s
                JOIN pg_class i ON i.oid = s.indexrelid
                JOIN pg_class t ON t.oid = s.indrelid
                JOIN pg_index ix ON ix.indexrelid = s.indexrelid
                WHERE s.idx_scan = 0 AND NOT ix.indisprimary
            """)
            urows = _collect(cur)
        lookup = {(i["table"], i["name"]): i for i in indexes}
        for r in urows:
            info = lookup.get((r["table_name"], r["index_name"]), {})
            unused.append({
                "table": r["table_name"], "index": r["index_name"],
                "columns": info.get("columns", []), "unique": info.get("unique", False),
                "scans": r["idx_scan"],
            })
        notes.append("未用判定基于 pg_stat_user_indexes.idx_scan（自上次 pg_stat_reset/重启以来的累计扫描数）")
    except Exception as e:
        notes.append(f"未用索引检查跳过（读不到索引统计）: {type(e).__name__}: {e}")
    return indexes, unused, notes


def _mysql_index_health(conn):
    """MySQL：information_schema.STATISTICS 拿索引列（判冗余）+ performance_schema 判未用。"""
    notes: List[str] = []
    with safe_cursor(conn) as cur:
        cur.execute("""
            SELECT TABLE_NAME AS table_name, INDEX_NAME AS index_name,
                   NON_UNIQUE AS non_unique, SEQ_IN_INDEX AS seq_in_index,
                   COLUMN_NAME AS column_name
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
            ORDER BY INDEX_NAME, SEQ_IN_INDEX
        """)
        rows = _collect(cur)

    grouped: Dict[tuple, list] = {}
    meta: Dict[tuple, tuple] = {}
    for r in rows:
        key = (r["table_name"], r["index_name"])
        grouped.setdefault(key, []).append((r["seq_in_index"], r["column_name"]))
        meta[key] = (int(r["non_unique"]) == 0, r["index_name"] == "PRIMARY")
    indexes = []
    for (table, name), cols in grouped.items():
        ordered = [c for _, c in sorted(cols, key=lambda x: x[0])]
        uniq, pri = meta[(table, name)]
        indexes.append({"table": table, "name": name, "columns": ordered,
                        "unique": uniq, "primary": pri})

    unused: List[Dict[str, Any]] = []
    try:
        with safe_cursor(conn) as cur:
            cur.execute("""
                SELECT OBJECT_NAME AS table_name, INDEX_NAME AS index_name, COUNT_STAR AS scans
                FROM performance_schema.table_io_waits_summary_by_index_usage
                WHERE INDEX_NAME IS NOT NULL AND INDEX_NAME <> 'PRIMARY' AND COUNT_STAR = 0
            """)
            urows = _collect(cur)
        lookup = {(i["table"], i["name"]): i for i in indexes}
        for r in urows:
            info = lookup.get((r["table_name"], r["index_name"]), {})
            unused.append({
                "table": r["table_name"], "index": r["index_name"],
                "columns": info.get("columns", []), "unique": info.get("unique", False),
                "scans": r["scans"],
            })
        notes.append("未用判定基于 performance_schema.table_io_waits_summary_by_index_usage（自 performance_schema 启用/上次重启以来）")
    except Exception as e:
        notes.append(f"未用索引检查跳过（performance_schema 可能未开或无权限）: {type(e).__name__}: {e}")
    return indexes, unused, notes


def _oracle_dm_index_health(conn, dialect: str):
    """Oracle/达梦：USER_IND_COLUMNS+USER_INDEXES 判冗余；未用靠 V$OBJECT_USAGE（需先开监控）。"""
    notes: List[str] = []
    with safe_cursor(conn) as cur:
        cur.execute("""
            SELECT ic.INDEX_NAME AS index_name, ic.TABLE_NAME AS table_name,
                   i.UNIQUENESS AS uniqueness, ic.COLUMN_NAME AS column_name,
                   ic.COLUMN_POSITION AS seq
            FROM USER_IND_COLUMNS ic
            JOIN USER_INDEXES i ON i.INDEX_NAME = ic.INDEX_NAME
            ORDER BY ic.INDEX_NAME, ic.COLUMN_POSITION
        """)
        rows = _collect(cur)

    grouped: Dict[tuple, list] = {}
    meta: Dict[tuple, tuple] = {}
    for r in rows:
        key = (r["table_name"], r["index_name"])
        grouped.setdefault(key, []).append((r["seq"], r["column_name"]))
        # UNIQUENESS='UNIQUE' → 唯一索引（含主键），保守不判它冗余
        meta[key] = (str(r["uniqueness"]).upper() == "UNIQUE", False)
    indexes = []
    for (table, name), cols in grouped.items():
        ordered = [c for _, c in sorted(cols, key=lambda x: (x[0] is None, x[0]))]
        uniq, pri = meta[(table, name)]
        indexes.append({"table": table, "name": name, "columns": ordered,
                        "unique": uniq, "primary": pri})

    unused: List[Dict[str, Any]] = []
    try:
        with safe_cursor(conn) as cur:
            cur.execute("SELECT NAME AS index_name FROM V$OBJECT_USAGE WHERE USED = 'NO'")
            urows = _collect(cur)
        by_name = {i["name"]: i for i in indexes}
        for r in urows:
            info = by_name.get(r["index_name"], {})
            unused.append({
                "table": info.get("table", ""), "index": r["index_name"],
                "columns": info.get("columns", []), "unique": info.get("unique", False),
                "scans": 0,
            })
    except Exception as e:
        notes.append(f"未用索引检查跳过（V$OBJECT_USAGE 需权限）: {type(e).__name__}: {e}")
    # 无论成没成，都要说清 Oracle/达梦的未用判定前置条件
    notes.append("未用判定基于 V$OBJECT_USAGE，只覆盖已执行 ALTER INDEX ... MONITORING USAGE 的索引；未开监控的索引不在此列")
    return indexes, unused, notes


def db_health_impl(ds: Dict[str, str], db_code: str) -> Dict[str, Any]:
    """索引卫生检查（只读诊断，尽力而为，绝不抛异常、绝不动手删）。

    返回 {dialect, db_code, redundant_indexes, unused_indexes, note, execution_time_ms}。
    redundant=冗余（结构，高置信）；unused=未用候选（usage，低置信，带窗口口径）。
    """
    dialect = (ds.get("db_type") or "").lower().strip()
    start = time.time()
    conn = None
    try:
        try:
            conn = get_connection(ds, None)
            if dialect in ("postgres", "gaussdb"):
                indexes, unused, notes = _pg_index_health(conn, dialect)
            elif dialect == "mysql":
                indexes, unused, notes = _mysql_index_health(conn)
            elif dialect in ("oracle", "dm"):
                indexes, unused, notes = _oracle_dm_index_health(conn, dialect)
            else:
                return _result(dialect, db_code, [], [],
                               f"索引卫生暂不支持的数据库类型: {dialect}", start)
        except Exception as e:
            logger.warning(f"db_health 失败（尽力而为，不抛）: {type(e).__name__}: {e}")
            return _result(dialect, db_code, [], [],
                           f"索引卫生检查失败: {type(e).__name__}: {e}", start)

        redundant = find_redundant_indexes(indexes)
        for f in redundant:
            f["suggestion"] = f"冗余索引（{f['reason']}），已被 {f['covered_by']} 覆盖，可删候选"
            f["drop_sql"] = _drop_sql(dialect, f["table"], f["index"])
        for f in unused:
            extra = "（且它是唯一索引，删除会丢掉唯一约束）" if f.get("unique") else ""
            f["suggestion"] = (f"观测窗口内零扫描的未用候选索引，删前务必确认没有低频/周期性查询依赖它{extra}")
            f["drop_sql"] = _drop_sql(dialect, f["table"], f["index"])
        return _result(dialect, db_code, redundant, unused,
                       "；".join(notes) if notes else None, start)
    finally:
        if conn is not None:
            put_connection(ds, conn, None)
