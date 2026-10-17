# -*- coding: utf-8 -*-
"""表结构内省（多方言）：索引 / 约束 / 表结构 / 按业务含义搜表
——原 1633–2217 行。v4.x 清理：用 safe_cursor 干掉四处重复的 try/finally 关闭游标样板。"""

__author__ = "fengkeju"
import time
from typing import Dict, List, Optional, Tuple

from .config import MAX_RETRY_COUNT, RETRY_DELAY
from .errutil import is_transient_error, safe_cursor
from .logcfg import logger
from .pool import get_connection, put_connection
from .util import split_table_ref


def _collect_dicts(cursor) -> List[Dict]:
    """把当前游标的结果集打包成 dict 列表（列名来自 cursor.description）。"""
    rows = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


# ------------------------------------------------------------------------------
# schema 解析（v4.4）
# ------------------------------------------------------------------------------
# 背景：MySQL 的 db_code 就是库名，`TABLE_SCHEMA = db_code` 天经地义。但另外四个
# 方言不是——PG/GaussDB 的库名来自 conn_url 的 dbname，Oracle/DM 的 schema 来自
# 连接账号，db_code 对它们只是业务标签。原来四个方言都拿 db_code 当 schema 过滤，
# 于是 PG 上 `nspname = 'report'`（真实 schema 是 public）、Oracle 上
# `OWNER = 'REPORT'`（真实 owner 是登录用户），永远查不到表，且不报错——只是静默
# 返回空列表，表现为"表结构查不出来"。
#
# 现在的优先级：调用方显式写的 schema. 前缀 > 配置里的 db_schema 列 > 方言默认。
# 方言默认不再是 db_code：Oracle 用 USER、PG 用 current_schema()、DM 用 USER_* 视图。


def _configured_schema(ds: Dict[str, str]) -> Optional[str]:
    """配置里显式指定的 schema/owner（db_schema 列）。None = 未配置，用方言默认。"""
    return (ds.get("db_schema") or "").strip() or None


def _owner_clause(schema: Optional[str], fallback_sql: str, placeholder: str) -> Tuple[str, Optional[str]]:
    """schema 过滤条件：配了就用占位符绑定，没配就用方言内置字面量。

    返回 (SQL 片段, 待绑定值 或 None)。没配时给字面量而不是塞一个空绑定——
    dmPython/oracledb 对多余的命名绑定变量直接报错，psycopg2 也会因参数个数
    不匹配报错。Oracle 的 USER、PG 的 current_schema() 都是标准 SQL 且零开销，
    还能自动跟随连接上的 search_path / 当前登录用户。

    schema 明确时返回原样（大小写由调用方按方言决定：Oracle/DM 折大写，
    PG 保持原样——PG 的未加引号标识符折小写，但这里传入的通常已是实际形式）。
    """
    if schema:
        return placeholder, schema
    return fallback_sql, None


def _resolve_target(table_name: str, ds: Dict[str, str], default_db: str,
                    db_type: str) -> Tuple[Optional[str], str, Optional[str]]:
    """解析一次表名引用，返回 (连接目标库, 真实表名, schema 或 None)。

    - 连接目标库：只有 MySQL 需要切默认库（db_code 或显式前缀就是库名）；
      其余方言 conn_url 已定库，驱动根本不看 db_name，传 None 让连接池 key
      保持稳定（否则同一个库会按 schema 不同建出多条连接）。
    - schema：显式前缀 > db_schema 列 > None（None 交给各方言用内置默认）。
    """
    prefix, real_table = split_table_ref(table_name)
    schema = prefix or _configured_schema(ds)
    if db_type == "mysql":
        conn_db = prefix or _configured_schema(ds) or default_db
    else:
        conn_db = prefix or None
    return conn_db, real_table, schema


# ------------------------------------------------------------------------------
# 4. 表结构查询功能（带重试机制）
# ------------------------------------------------------------------------------
def get_table_indexes(table_name: str, ds: Dict[str, str], default_db: str) -> List[Dict]:
    """查询表的索引信息（带重试机制，大小写不敏感）

    返回字段：index_name, column_name, is_unique, is_primary, seq_in_index, index_type, comment
    """
    conn_db, real_table, schema = _resolve_target(table_name, ds, default_db,
                                                  ds.get("db_type", "").lower().strip())
    db_type = ds.get("db_type", "").lower().strip()

    for retry in range(MAX_RETRY_COUNT):
        conn = None
        try:
            conn = get_connection(ds, conn_db)
            with safe_cursor(conn) as cursor:
                if db_type == "mysql":
                    cursor.execute("""
                        SELECT TABLE_NAME FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = %s AND LOWER(TABLE_NAME) = LOWER(%s)
                        LIMIT 1
                    """, (conn_db, real_table))
                    actual_table_row = cursor.fetchone()
                    actual_table = actual_table_row[0] if actual_table_row else real_table

                    cursor.execute("""
                        SELECT
                            INDEX_NAME as index_name,
                            COLUMN_NAME as column_name,
                            CASE WHEN NON_UNIQUE = 0 THEN 1 ELSE 0 END as is_unique,
                            CASE WHEN INDEX_NAME = 'PRIMARY' THEN 1 ELSE 0 END as is_primary,
                            SEQ_IN_INDEX as seq_in_index,
                            INDEX_TYPE as index_type,
                            'NORMAL' as index_comment
                        FROM information_schema.STATISTICS
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        ORDER BY INDEX_NAME, SEQ_IN_INDEX
                    """, (conn_db, actual_table))

                elif db_type == "dm":
                    # 配了 db_schema → 查 ALL_* 视图并按 OWNER 过滤（跨 schema 内省）；
                    # 没配 → USER_* 视图，天然只看当前用户的对象（DM 的常规用法）
                    if schema:
                        cursor.execute("""
                            SELECT
                                i.INDEX_NAME as index_name,
                                ic.COLUMN_NAME as column_name,
                                CASE WHEN i.UNIQUENESS = 'UNIQUE' THEN 1 ELSE 0 END as is_unique,
                                CASE WHEN i.INDEX_NAME IN (
                                    SELECT CONSTRAINT_NAME FROM ALL_CONSTRAINTS
                                    WHERE CONSTRAINT_TYPE = 'P' AND TABLE_NAME = :table AND OWNER = :owner
                                ) THEN 1 ELSE 0 END as is_primary,
                                ic.COLUMN_POSITION as seq_in_index,
                                i.INDEX_TYPE as index_type,
                                '' as index_comment
                            FROM ALL_INDEXES i
                            JOIN ALL_IND_COLUMNS ic ON i.INDEX_NAME = ic.INDEX_NAME AND i.OWNER = ic.INDEX_OWNER
                            WHERE i.TABLE_NAME = :table AND i.OWNER = :owner
                            ORDER BY i.INDEX_NAME, ic.COLUMN_POSITION
                        """, {"table": real_table, "owner": schema.upper()})
                    else:
                        cursor.execute("""
                            SELECT
                                i.INDEX_NAME as index_name,
                                ic.COLUMN_NAME as column_name,
                                CASE WHEN i.UNIQUENESS = 'UNIQUE' THEN 1 ELSE 0 END as is_unique,
                                CASE WHEN i.INDEX_NAME IN (
                                    SELECT CONSTRAINT_NAME FROM USER_CONSTRAINTS WHERE CONSTRAINT_TYPE = 'P' AND TABLE_NAME = :1
                                ) THEN 1 ELSE 0 END as is_primary,
                                ic.COLUMN_POSITION as seq_in_index,
                                i.INDEX_TYPE as index_type,
                                '' as index_comment
                            FROM USER_INDEXES i
                            JOIN USER_IND_COLUMNS ic ON i.INDEX_NAME = ic.INDEX_NAME
                            WHERE i.TABLE_NAME = :1
                            ORDER BY i.INDEX_NAME, ic.COLUMN_POSITION
                        """, (real_table,))

                elif db_type in ("postgres", "gaussdb"):
                    # schema 未指定时用 current_schema()：跟随连接上的 search_path，
                    # 比写死 'public' 更准（用户在 conn_url 的 options 里设过
                    # search_path 时，写死 public 反而查不到）
                    ns_cond, ns = _owner_clause(schema, "current_schema()", "%s")
                    cursor.execute(f"""
                        SELECT
                            i.relname as index_name,
                            a.attname as column_name,
                            CASE WHEN ix.indisunique THEN 1 ELSE 0 END as is_unique,
                            CASE WHEN ix.indisprimary THEN 1 ELSE 0 END as is_primary,
                            (array_position(ix.indkey, a.attnum)) as seq_in_index,
                            am.amname as index_type,
                            '' as index_comment
                        FROM pg_index ix
                        JOIN pg_class c ON c.oid = ix.indrelid
                        JOIN pg_class i ON i.oid = ix.indexrelid
                        JOIN pg_namespace n ON c.relnamespace = n.oid
                        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(ix.indkey)
                        JOIN pg_am am ON i.relam = am.oid
                        WHERE c.relname = %s AND n.nspname = {ns_cond}
                        ORDER BY i.relname, seq_in_index
                    """, (real_table, ns) if ns is not None else (real_table,))

                elif db_type == "oracle":
                    # 未指定 schema 时 OWNER = USER（当前登录用户），不是 db_code
                    owner_cond, owner = _owner_clause(schema, "USER", ":owner")
                    cursor.execute(f"""
                        SELECT
                            i.INDEX_NAME as index_name,
                            ic.COLUMN_NAME as column_name,
                            CASE WHEN i.UNIQUENESS = 'UNIQUE' THEN 1 ELSE 0 END as is_unique,
                            CASE WHEN i.INDEX_NAME IN (
                                SELECT CONSTRAINT_NAME FROM ALL_CONSTRAINTS
                                WHERE CONSTRAINT_TYPE = 'P' AND OWNER = {owner_cond} AND TABLE_NAME = :table
                            ) THEN 1 ELSE 0 END as is_primary,
                            ic.COLUMN_POSITION as seq_in_index,
                            i.INDEX_TYPE as index_type,
                            '' as index_comment
                        FROM ALL_INDEXES i
                        JOIN ALL_IND_COLUMNS ic ON i.INDEX_NAME = ic.INDEX_NAME AND i.OWNER = ic.INDEX_OWNER
                        WHERE i.TABLE_NAME = :table AND i.OWNER = {owner_cond}
                        ORDER BY i.INDEX_NAME, ic.COLUMN_POSITION
                    """, {"table": real_table.upper(), "owner": owner.upper()} if owner is not None
                        else {"table": real_table.upper()})

                else:
                    return []

                return _collect_dicts(cursor)

        except Exception as e:
            # 只重试瞬时错误（连接断开/超时/死锁）。表不存在、权限不足、SQL 语法错误
            # 这类永久性错误重试只是白等 2×RETRY_DELAY，还会拖慢调用方拿到错误原文。
            # 与 readengine 的 is_transient_error 判定保持一致。
            if retry < MAX_RETRY_COUNT - 1 and is_transient_error(e):
                logger.warning(f"获取索引信息失败(第{retry+1}次，瞬时错误)，{RETRY_DELAY}秒后重试: {e}")
                time.sleep(RETRY_DELAY)
            else:
                raise
        finally:
            # 借出的池连接每轮用完即还（成功 return、瞬时错误重试、永久错误抛错
            # 都走到这）。此前没有归还 API，连接被永久留在池里，同 key 的并发
            # 调用会共用同一条物理连接。
            if conn is not None:
                put_connection(ds, conn, conn_db)


def get_table_constraints(table_name: str, ds: Dict[str, str], default_db: str) -> List[Dict]:
    """查询表的约束信息（带重试机制，大小写不敏感）

    返回字段：constraint_name, constraint_type, column_name, referenced_table, referenced_column, delete_rule
    """
    conn_db, real_table, schema = _resolve_target(table_name, ds, default_db,
                                                  ds.get("db_type", "").lower().strip())
    db_type = ds.get("db_type", "").lower().strip()

    for retry in range(MAX_RETRY_COUNT):
        conn = None
        try:
            conn = get_connection(ds, conn_db)
            with safe_cursor(conn) as cursor:
                if db_type == "mysql":
                    # MySQL 5.7 的 information_schema 三表 JOIN 不会把 WHERE 谓词下推到
                    # 被 join 的视图，于是物化整个 KEY_COLUMN_USAGE（全库 0.85s），
                    # 单表约束查询要 1.4s——而同文件的 get_table_indexes 只用 2ms。
                    # 改成「三次各自带 WHERE 过滤的查询 + Python 侧合并 LEFT JOIN」：
                    # 单表约 2ms，快 300 倍以上，且结果与原 JOIN 完全等价
                    # （已用带 ON DELETE CASCADE 的真实外键表逐行比对确认）。
                    cursor.execute("""
                        SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE
                        FROM information_schema.TABLE_CONSTRAINTS
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        ORDER BY CONSTRAINT_NAME
                    """, (conn_db, real_table))
                    constraints = cursor.fetchall()

                    cursor.execute("""
                        SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME,
                               REFERENCED_COLUMN_NAME, ORDINAL_POSITION
                        FROM information_schema.KEY_COLUMN_USAGE
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
                    """, (conn_db, real_table))
                    key_usage = cursor.fetchall()

                    # DELETE_RULE 只在真有外键时才查。REFERENTIAL_CONSTRAINTS 按 schema
                    # 过滤仍要扫全库（约 60ms），按约束名过滤只要 2ms，故用 IN 列表。
                    delete_rules = {}
                    fk_names = [name for name, ctype in constraints if ctype == "FOREIGN KEY"]
                    if fk_names:
                        placeholders = ", ".join(["%s"] * len(fk_names))
                        cursor.execute(
                            "SELECT CONSTRAINT_NAME, DELETE_RULE "
                            "FROM information_schema.REFERENTIAL_CONSTRAINTS "
                            f"WHERE CONSTRAINT_SCHEMA = %s AND CONSTRAINT_NAME IN ({placeholders})",
                            (conn_db, *fk_names))
                        delete_rules = {row[0]: row[1] for row in cursor.fetchall()}

                    # Python 侧复现原 JOIN 的 LEFT JOIN 语义：约束为行主轴，
                    # 无对应 KEY_COLUMN_USAGE 行的约束保留、列字段置 NULL。
                    rows: List[Dict] = []
                    for cname, ctype in constraints:
                        rule = delete_rules.get(cname)
                        matched = [k for k in key_usage if k[0] == cname]
                        if matched:
                            for _, col, ref_tbl, ref_col, _ in matched:
                                rows.append({
                                    "constraint_name": cname,
                                    "constraint_type": ctype,
                                    "column_name": col,
                                    "referenced_table": ref_tbl,
                                    "referenced_column": ref_col,
                                    "delete_rule": rule,
                                })
                        else:
                            rows.append({
                                "constraint_name": cname,
                                "constraint_type": ctype,
                                "column_name": None,
                                "referenced_table": None,
                                "referenced_column": None,
                                "delete_rule": rule,
                            })
                    return rows

                elif db_type == "dm":
                    # 同 get_table_indexes：配了 db_schema 走 ALL_* + OWNER，否则 USER_*
                    if schema:
                        cursor.execute("""
                            SELECT
                                uc.CONSTRAINT_NAME as constraint_name,
                                uc.CONSTRAINT_TYPE as constraint_type,
                                ucc.COLUMN_NAME as column_name,
                                fc.DELETE_RULE as delete_rule,
                                '' as referenced_table,
                                '' as referenced_column
                            FROM ALL_CONSTRAINTS uc
                            LEFT JOIN ALL_CONS_COLUMNS ucc
                                ON uc.CONSTRAINT_NAME = ucc.CONSTRAINT_NAME AND uc.OWNER = ucc.OWNER
                            LEFT JOIN (
                                SELECT c1.CONSTRAINT_NAME, c2.CONSTRAINT_NAME AS FK_NAME,
                                       c1.DELETE_RULE
                                FROM ALL_CONSTRAINTS c1
                                JOIN ALL_CONSTRAINTS c2
                                    ON c1.R_CONSTRAINT_NAME = c2.CONSTRAINT_NAME AND c1.OWNER = c2.OWNER
                                WHERE c1.CONSTRAINT_TYPE = 'R'
                            ) fc ON uc.CONSTRAINT_NAME = fc.FK_NAME
                            WHERE uc.TABLE_NAME = :table AND uc.OWNER = :owner
                            ORDER BY uc.CONSTRAINT_NAME, ucc.POSITION
                        """, {"table": real_table, "owner": schema.upper()})
                    else:
                        cursor.execute("""
                            SELECT
                                uc.CONSTRAINT_NAME as constraint_name,
                                uc.CONSTRAINT_TYPE as constraint_type,
                                ucc.COLUMN_NAME as column_name,
                                fc.DELETE_RULE as delete_rule,
                                '' as referenced_table,
                                '' as referenced_column
                            FROM USER_CONSTRAINTS uc
                            LEFT JOIN USER_CONS_COLUMNS ucc ON uc.CONSTRAINT_NAME = ucc.CONSTRAINT_NAME
                            LEFT JOIN (
                                SELECT c1.CONSTRAINT_NAME, c2.CONSTRAINT_NAME AS FK_NAME,
                                       c1.DELETE_RULE
                                FROM USER_CONSTRAINTS c1
                                JOIN USER_CONSTRAINTS c2 ON c1.R_CONSTRAINT_NAME = c2.CONSTRAINT_NAME
                                WHERE c1.CONSTRAINT_TYPE = 'R'
                            ) fc ON uc.CONSTRAINT_NAME = fc.FK_NAME
                            WHERE uc.TABLE_NAME = :1
                            ORDER BY uc.CONSTRAINT_NAME, ucc.POSITION
                        """, (real_table,))

                elif db_type in ("postgres", "gaussdb"):
                    ns_cond, ns = _owner_clause(schema, "current_schema()", "%s")
                    cursor.execute(f"""
                        SELECT
                            tc.constraint_name,
                            tc.constraint_type,
                            kcu.column_name,
                            ccu.table_name AS referenced_table,
                            ccu.column_name AS referenced_column,
                            rc.delete_rule
                        FROM information_schema.table_constraints tc
                        LEFT JOIN information_schema.key_column_usage kcu
                            ON tc.constraint_name = kcu.constraint_name
                            AND tc.table_schema = kcu.table_schema
                        LEFT JOIN information_schema.constraint_column_usage ccu
                            ON tc.constraint_name = ccu.constraint_name
                            AND tc.table_schema = ccu.table_schema
                        LEFT JOIN information_schema.referential_constraints rc
                            ON tc.constraint_name = rc.constraint_name
                        WHERE tc.table_name = %s AND tc.table_schema = {ns_cond}
                        ORDER BY tc.constraint_name, kcu.ordinal_position
                    """, (real_table, ns) if ns is not None else (real_table,))

                elif db_type == "oracle":
                    owner_cond, owner = _owner_clause(schema, "USER", ":owner")
                    cursor.execute(f"""
                        SELECT
                            uc.CONSTRAINT_NAME as constraint_name,
                            uc.CONSTRAINT_TYPE as constraint_type,
                            ucc.COLUMN_NAME as column_name,
                            ac.TABLE_NAME as referenced_table,
                            acc.COLUMN_NAME as referenced_column,
                            uc.DELETE_RULE as delete_rule
                        FROM ALL_CONSTRAINTS uc
                        JOIN ALL_CONS_COLUMNS ucc ON uc.CONSTRAINT_NAME = ucc.CONSTRAINT_NAME AND uc.OWNER = ucc.OWNER
                        LEFT JOIN ALL_CONSTRAINTS ac ON uc.R_CONSTRAINT_NAME = ac.CONSTRAINT_NAME AND uc.OWNER = ac.OWNER
                        LEFT JOIN ALL_CONS_COLUMNS acc ON ac.CONSTRAINT_NAME = acc.CONSTRAINT_NAME AND acc.OWNER = ac.OWNER
                            AND ucc.POSITION = acc.POSITION
                        WHERE uc.TABLE_NAME = :table AND uc.OWNER = {owner_cond}
                        ORDER BY uc.CONSTRAINT_NAME, ucc.POSITION
                    """, {"table": real_table.upper(), "owner": owner.upper()} if owner is not None
                        else {"table": real_table.upper()})

                else:
                    return []

                return _collect_dicts(cursor)

        except Exception as e:
            # 只重试瞬时错误；永久性错误立即抛出（理由同 get_table_indexes）
            if retry < MAX_RETRY_COUNT - 1 and is_transient_error(e):
                logger.warning(f"获取约束信息失败(第{retry+1}次，瞬时错误)，{RETRY_DELAY}秒后重试: {e}")
                time.sleep(RETRY_DELAY)
            else:
                raise
        finally:
            # 借出的池连接每轮用完即还（成功 return、瞬时错误重试、永久错误抛错
            # 都走到这）。此前没有归还 API，连接被永久留在池里，同 key 的并发
            # 调用会共用同一条物理连接。
            if conn is not None:
                put_connection(ds, conn, conn_db)


def get_table_schema(table_name: str, ds: Dict[str, str], default_db: str) -> Dict:
    """查询表结构信息（带重试机制，表名大小写不敏感）

    返回格式：
    {
        "columns": [...],        # 字段列表
        "indexes": [...],        # 索引列表
        "constraints": [...]     # 约束列表
    }

    参数：
    - table_name: 表名，支持跨库语法 db_code.table_name
    - ds: 数据源配置
    - default_db: 默认库名
    """
    conn_db, real_table, schema = _resolve_target(table_name, ds, default_db,
                                                  ds.get("db_type", "").lower().strip())
    db_type = ds.get("db_type", "").lower().strip()

    for retry in range(MAX_RETRY_COUNT):
        conn = None
        try:
            conn = get_connection(ds, conn_db)
            with safe_cursor(conn) as cursor:
                if db_type == "mysql":
                    cursor.execute("""
                        SELECT TABLE_NAME FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = %s AND LOWER(TABLE_NAME) = LOWER(%s)
                        LIMIT 1
                    """, (conn_db, real_table))
                    actual_table_row = cursor.fetchone()
                    actual_table = actual_table_row[0] if actual_table_row else real_table

                    cursor.execute("""
                        SELECT
                            COLUMN_NAME as field,
                            COLUMN_TYPE as type,
                            IS_NULLABLE as nullable,
                            COLUMN_KEY as key_type,
                            COLUMN_DEFAULT as default_value,
                            EXTRA as extra,
                            COLUMN_COMMENT as comment
                        FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        ORDER BY ORDINAL_POSITION
                    """, (conn_db, actual_table))

                elif db_type == "dm":
                    # 配了 db_schema → ALL_* + OWNER（跨 schema 内省）；否则 USER_*（当前用户）
                    if schema:
                        cursor.execute("""
                            SELECT ic.COLUMN_NAME
                            FROM ALL_CONSTRAINTS uc
                            JOIN ALL_IND_COLUMNS ic
                                ON uc.CONSTRAINT_NAME = ic.INDEX_NAME AND uc.OWNER = ic.INDEX_OWNER
                            WHERE uc.CONSTRAINT_TYPE = 'P'
                              AND ic.TABLE_NAME = :table AND ic.INDEX_OWNER = :owner
                        """, {"table": real_table, "owner": schema.upper()})
                    else:
                        cursor.execute("""
                            SELECT ic.COLUMN_NAME
                            FROM USER_CONSTRAINTS uc
                            JOIN USER_IND_COLUMNS ic ON uc.CONSTRAINT_NAME = ic.INDEX_NAME
                            WHERE uc.CONSTRAINT_TYPE = 'P' AND ic.TABLE_NAME = :1
                        """, (real_table,))
                    pk_columns = [row[0] for row in cursor.fetchall()]

                    # 第二步：查表结构和注释
                    if schema:
                        cursor.execute("""
                            SELECT
                                c.COLUMN_NAME as field,
                                CASE
                                    WHEN c.DATA_TYPE IN ('NUMBER','NUMERIC','DECIMAL','DEC') AND c.DATA_PRECISION IS NOT NULL
                                        THEN c.DATA_TYPE || '(' || c.DATA_PRECISION || ',' || c.DATA_SCALE || ')'
                                    WHEN c.DATA_TYPE IN ('VARCHAR','VARCHAR2','CHAR','NVARCHAR','NVARCHAR2','CLOB','BLOB','RAW')
                                        THEN c.DATA_TYPE || '(' || c.DATA_LENGTH || ')'
                                    ELSE c.DATA_TYPE
                                END as type,
                                c.NULLABLE as nullable,
                                c.DATA_DEFAULT as default_value,
                                c.COLUMN_ID as column_id,
                                cc.COMMENTS as col_comment
                            FROM ALL_TAB_COLUMNS c
                            LEFT JOIN ALL_COL_COMMENTS cc
                                ON c.TABLE_NAME = cc.TABLE_NAME AND c.COLUMN_NAME = cc.COLUMN_NAME AND c.OWNER = cc.OWNER
                            WHERE c.TABLE_NAME = :table AND c.OWNER = :owner
                            ORDER BY c.COLUMN_ID
                        """, {"table": real_table, "owner": schema.upper()})
                    else:
                        cursor.execute("""
                            SELECT
                                c.COLUMN_NAME as field,
                                CASE
                                    WHEN c.DATA_TYPE IN ('NUMBER','NUMERIC','DECIMAL','DEC') AND c.DATA_PRECISION IS NOT NULL
                                        THEN c.DATA_TYPE || '(' || c.DATA_PRECISION || ',' || c.DATA_SCALE || ')'
                                    WHEN c.DATA_TYPE IN ('VARCHAR','VARCHAR2','CHAR','NVARCHAR','NVARCHAR2','CLOB','BLOB','RAW')
                                        THEN c.DATA_TYPE || '(' || c.DATA_LENGTH || ')'
                                    ELSE c.DATA_TYPE
                                END as type,
                                c.NULLABLE as nullable,
                                c.DATA_DEFAULT as default_value,
                                c.COLUMN_ID as column_id,
                                cc.COMMENTS as col_comment
                            FROM USER_TAB_COLUMNS c
                            LEFT JOIN USER_COL_COMMENTS cc ON c.TABLE_NAME = cc.TABLE_NAME AND c.COLUMN_NAME = cc.COLUMN_NAME
                            WHERE c.TABLE_NAME = :1
                            ORDER BY c.COLUMN_ID
                        """, (real_table,))

                    rows = cursor.fetchall()
                    result = []
                    for row in rows:
                        field, type_, nullable, default_value, column_id, col_comment = row
                        result.append({
                            'field': field,
                            'type': type_,
                            'nullable': nullable,
                            'key_type': 'PRI' if field in pk_columns else '',
                            'default_value': default_value if default_value else '',
                            'extra': '',
                            'comment': col_comment if col_comment else ''
                        })

                    indexes = get_table_indexes(table_name, ds, default_db)
                    constraints = get_table_constraints(table_name, ds, default_db)
                    return {"columns": result, "indexes": indexes, "constraints": constraints}

                elif db_type in ("postgres", "gaussdb"):
                    ns_cond, ns = _owner_clause(schema, "current_schema()", "%s")
                    cursor.execute(f"""
                        SELECT
                            a.attname as field,
                            pg_catalog.format_type(a.atttypid, a.atttypmod) as type,
                            CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END as nullable,
                            CASE WHEN a.attname IN (
                                SELECT a2.attname
                                FROM pg_index i
                                JOIN pg_attribute a2 ON a2.attrelid = i.indrelid AND a2.attnum = ANY(i.indkey)
                                WHERE i.indisprimary AND i.indrelid = c.oid
                            ) THEN 'PRI' ELSE '' END as key_type,
                            pg_get_expr(d.adbin, d.adrelid) as default_value,
                            '' as extra,
                            COALESCE(dsc.description, '') as comment
                        FROM pg_attribute a
                        JOIN pg_class c ON a.attrelid = c.oid
                        JOIN pg_namespace n ON c.relnamespace = n.oid
                        LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
                        LEFT JOIN pg_description dsc ON dsc.objoid = c.oid AND dsc.objsubid = a.attnum
                        WHERE c.relname = %s AND n.nspname = {ns_cond}
                          AND a.attnum > 0 AND NOT a.attisdropped
                        ORDER BY a.attnum
                    """, (real_table, ns) if ns is not None else (real_table,))

                elif db_type == "oracle":
                    # 未指定 schema 时 OWNER = USER（当前登录用户），不是 db_code
                    owner_cond, owner = _owner_clause(schema, "USER", ":owner")
                    cursor.execute(f"""
                        SELECT
                            c.COLUMN_NAME as field,
                            CASE
                                WHEN c.DATA_TYPE IN ('NUMBER','NUMERIC','DECIMAL','DEC') AND c.DATA_PRECISION IS NOT NULL
                                    THEN c.DATA_TYPE || '(' || c.DATA_PRECISION || ',' || c.DATA_SCALE || ')'
                                WHEN c.DATA_TYPE IN ('VARCHAR','VARCHAR2','CHAR','NVARCHAR','NVARCHAR2','CLOB','BLOB','RAW','NCHAR')
                                    THEN c.DATA_TYPE || '(' || c.DATA_LENGTH || ')'
                                ELSE c.DATA_TYPE
                            END as type,
                            CASE WHEN c.NULLABLE = 'Y' THEN 'YES' ELSE 'NO' END as nullable,
                            CASE WHEN c.COLUMN_NAME IN (
                                SELECT ic.COLUMN_NAME
                                FROM ALL_CONSTRAINTS uc
                                JOIN ALL_IND_COLUMNS ic ON uc.CONSTRAINT_NAME = ic.INDEX_NAME AND uc.OWNER = ic.TABLE_OWNER
                                WHERE uc.CONSTRAINT_TYPE = 'P' AND uc.OWNER = {owner_cond} AND ic.TABLE_NAME = :table
                            ) THEN 'PRI' ELSE '' END as key_type,
                            c.DATA_DEFAULT as default_value,
                            '' as extra,
                            cc.COMMENTS as comment
                        FROM ALL_TAB_COLUMNS c
                        LEFT JOIN ALL_COL_COMMENTS cc ON c.TABLE_NAME = cc.TABLE_NAME AND c.COLUMN_NAME = cc.COLUMN_NAME AND c.OWNER = cc.OWNER
                        WHERE c.OWNER = {owner_cond} AND c.TABLE_NAME = :table
                        ORDER BY c.COLUMN_ID
                    """, {"table": real_table.upper(), "owner": owner.upper()} if owner is not None
                        else {"table": real_table.upper()})

                else:
                    return {"columns": [], "indexes": [], "constraints": []}

                col_result = _collect_dicts(cursor)

                # 索引/约束查询失败不应拖垮整个 get_schema_info：字段列表才是调用方的核心诉求。
                # 任一降级时记 warning 并返回空列表，保持 {columns, indexes, constraints} 结构不变。
                try:
                    indexes = get_table_indexes(table_name, ds, default_db)
                except Exception as e:
                    logger.warning(f"获取索引信息失败，降级为空列表: {e}")
                    indexes = []
                try:
                    constraints = get_table_constraints(table_name, ds, default_db)
                except Exception as e:
                    logger.warning(f"获取约束信息失败，降级为空列表: {e}")
                    constraints = []

                return {"columns": col_result, "indexes": indexes, "constraints": constraints}

        except Exception as e:
            # 只重试瞬时错误；永久性错误立即抛出（理由同 get_table_indexes）
            if retry < MAX_RETRY_COUNT - 1 and is_transient_error(e):
                logger.warning(f"获取表结构失败(第{retry+1}次，瞬时错误)，{RETRY_DELAY}秒后重试: {e}")
                time.sleep(RETRY_DELAY)
            else:
                raise
        finally:
            # 借出的池连接每轮用完即还（成功 return、瞬时错误重试、永久错误抛错
            # 都走到这）。此前没有归还 API，连接被永久留在池里，同 key 的并发
            # 调用会共用同一条物理连接。
            if conn is not None:
                put_connection(ds, conn, conn_db)


# ------------------------------------------------------------------------------
# 5. 表搜索功能（优化版 + 重试机制）
# ------------------------------------------------------------------------------
def search_tables_by_keyword(keyword: str, ds: Dict[str, str], default_db: str, limit: int = 10) -> Dict:
    """根据关键词搜索匹配的表（带重试机制，大小写不敏感）。
    返回 {data, total_count, returned, limit}。row_count 已移除（元数据估算不准，需精确行数请用 query COUNT）。
    """
    db_type = ds.get("db_type", "").lower().strip()
    # schema 解析与 get_schema_info 同规则：显式 db_schema 列 > 方言默认
    # （MySQL 用 db_code，PG 用 current_schema()，Oracle 用 USER，DM 用 USER_* 视图）。
    # 原来这里一律拿 db_code 当 schema，PG/Oracle 上永远搜不到表。
    schema = _configured_schema(ds)

    keyword_lower = keyword.lower()
    kw_like = f"%{keyword}%"

    # 构造 data_sql 与 count_sql（按方言）
    if db_type == "mysql":
        ns = schema or default_db
        data_sql = """
            SELECT t.TABLE_NAME as table_name, t.TABLE_COMMENT as table_comment
            FROM information_schema.TABLES t
            WHERE t.TABLE_SCHEMA = %s
            AND (LOWER(t.TABLE_NAME) LIKE LOWER(%s) OR LOWER(t.TABLE_COMMENT) LIKE LOWER(%s))
            ORDER BY
                CASE WHEN LOWER(t.TABLE_NAME) LIKE LOWER(%s) THEN 0 ELSE 1 END,
                CASE WHEN LOWER(t.TABLE_COMMENT) LIKE LOWER(%s) THEN 0 ELSE 1 END,
                t.TABLE_NAME
            LIMIT %s
        """
        data_params = (ns, kw_like, kw_like, kw_like, kw_like, limit)
        count_sql = """
            SELECT COUNT(*) FROM information_schema.TABLES t
            WHERE t.TABLE_SCHEMA = %s
            AND (LOWER(t.TABLE_NAME) LIKE LOWER(%s) OR LOWER(t.TABLE_COMMENT) LIKE LOWER(%s))
        """
        count_params = (ns, kw_like, kw_like)

    elif db_type == "dm":
        # 同 get_table_schema：配了 db_schema 走 ALL_* + OWNER，否则 USER_*
        # 占位符一律用命名式（:owner/:kw1/:kw2）+ dict 传参，与下方 oracle 分支同构。
        # 原实现混用命名绑定 :owner 与位置绑定 :1/:2、传的却是位置 tuple——
        # dmPython 若按名称解析就会直接报参数绑定错误，按位置解析则属侥幸正确。
        if schema:
            data_sql = """
                SELECT t.TABLE_NAME as table_name, c.COMMENTS as table_comment
                FROM ALL_TABLES t
                LEFT JOIN ALL_TAB_COMMENTS c
                    ON t.TABLE_NAME = c.TABLE_NAME AND t.OWNER = c.OWNER
                WHERE t.OWNER = :owner
                AND (LOWER(t.TABLE_NAME) LIKE LOWER(:kw1) OR LOWER(c.COMMENTS) LIKE LOWER(:kw2))
                ORDER BY t.TABLE_NAME
            """
            count_sql = """
                SELECT COUNT(*) FROM ALL_TABLES t
                LEFT JOIN ALL_TAB_COMMENTS c
                    ON t.TABLE_NAME = c.TABLE_NAME AND t.OWNER = c.OWNER
                WHERE t.OWNER = :owner
                AND (LOWER(t.TABLE_NAME) LIKE LOWER(:kw1) OR LOWER(c.COMMENTS) LIKE LOWER(:kw2))
            """
            data_params = {"kw1": kw_like, "kw2": kw_like, "owner": schema.upper()}
            count_params = dict(data_params)
        else:
            data_sql = """
                SELECT t.TABLE_NAME as table_name, c.COMMENTS as table_comment
                FROM USER_TABLES t
                LEFT JOIN USER_TAB_COMMENTS c ON t.TABLE_NAME = c.TABLE_NAME
                WHERE LOWER(t.TABLE_NAME) LIKE LOWER(:kw1) OR LOWER(c.COMMENTS) LIKE LOWER(:kw2)
                ORDER BY t.TABLE_NAME
            """
            count_sql = """
                SELECT COUNT(*) FROM USER_TABLES t
                LEFT JOIN USER_TAB_COMMENTS c ON t.TABLE_NAME = c.TABLE_NAME
                WHERE LOWER(t.TABLE_NAME) LIKE LOWER(:kw1) OR LOWER(c.COMMENTS) LIKE LOWER(:kw2)
            """
            data_params = {"kw1": kw_like, "kw2": kw_like}
            count_params = dict(data_params)

    elif db_type in ("postgres", "gaussdb"):
        ns_cond, ns = _owner_clause(schema, "current_schema()", "%s")
        data_sql = f"""
            SELECT c.relname as table_name,
                   COALESCE(d.description, '') as table_comment
            FROM pg_catalog.pg_class c
            LEFT JOIN pg_catalog.pg_description d ON c.oid = d.objoid AND d.objsubid = 0
            JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
            WHERE c.relkind = 'r'
            AND n.nspname = {ns_cond}
            AND (LOWER(c.relname) LIKE LOWER(%s) OR LOWER(d.description) LIKE LOWER(%s))
            ORDER BY
                CASE WHEN LOWER(c.relname) LIKE LOWER(%s) THEN 0 ELSE 1 END,
                CASE WHEN LOWER(d.description) LIKE LOWER(%s) THEN 0 ELSE 1 END,
                c.relname
            LIMIT %s
        """
        # ns 为 None 表示用 current_schema() 字面量，不占参数位
        data_params = ((ns, kw_like, kw_like, kw_like, kw_like, limit) if ns is not None
                       else (kw_like, kw_like, kw_like, kw_like, limit))
        count_sql = f"""
            SELECT COUNT(*) FROM pg_catalog.pg_class c
            LEFT JOIN pg_catalog.pg_description d ON c.oid = d.objoid AND d.objsubid = 0
            JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
            WHERE c.relkind = 'r'
            AND n.nspname = {ns_cond}
            AND (LOWER(c.relname) LIKE LOWER(%s) OR LOWER(d.description) LIKE LOWER(%s))
        """
        count_params = ((ns, kw_like, kw_like) if ns is not None else (kw_like, kw_like))

    elif db_type == "oracle":
        owner_cond, owner = _owner_clause(schema, "USER", ":owner")
        data_sql = f"""
            SELECT t.TABLE_NAME as table_name, c.COMMENTS as table_comment
            FROM ALL_TABLES t
            LEFT JOIN ALL_TAB_COMMENTS c ON t.TABLE_NAME = c.TABLE_NAME AND t.OWNER = c.OWNER
            WHERE t.OWNER = {owner_cond}
            AND (LOWER(t.TABLE_NAME) LIKE LOWER(:kw1) OR LOWER(c.COMMENTS) LIKE LOWER(:kw2))
            ORDER BY
                CASE WHEN LOWER(t.TABLE_NAME) LIKE LOWER(:kw1) THEN 0 ELSE 1 END,
                CASE WHEN LOWER(c.COMMENTS) LIKE LOWER(:kw2) THEN 0 ELSE 1 END,
                t.TABLE_NAME
        """
        data_params = {"kw1": kw_like, "kw2": kw_like}
        if owner is not None:
            data_params["owner"] = owner.upper()
        count_sql = f"""
            SELECT COUNT(*) FROM ALL_TABLES t
            LEFT JOIN ALL_TAB_COMMENTS c ON t.TABLE_NAME = c.TABLE_NAME AND t.OWNER = c.OWNER
            WHERE t.OWNER = {owner_cond}
            AND (LOWER(t.TABLE_NAME) LIKE LOWER(:kw1) OR LOWER(c.COMMENTS) LIKE LOWER(:kw2))
        """
        count_params = {"kw1": kw_like, "kw2": kw_like}
        if owner is not None:
            count_params["owner"] = owner.upper()

    else:
        return {"data": [], "total_count": 0, "returned": 0, "limit": limit, "error": f"不支持的数据源类型: {db_type}"}

    for retry in range(MAX_RETRY_COUNT):
        conn = None
        try:
            # 连接目标库：只有 MySQL 需要切库；其余方言 conn_url 已定库，
            # 传 None 让连接池 key 保持稳定
            conn_db = (schema or default_db) if db_type == "mysql" else None
            conn = get_connection(ds, conn_db)
            with safe_cursor(conn) as cursor:
                cursor.execute(data_sql, data_params)
                data = _collect_dicts(cursor)
                # dm/oracle 不支持参数化 LIMIT，手动切片
                if db_type in ("dm", "oracle"):
                    data = data[:limit]

                cursor.execute(count_sql, count_params)
                count_row = cursor.fetchone()
                total_count = count_row[0] if count_row else 0

                return {
                    "data": data,
                    "total_count": total_count,
                    "returned": len(data),
                    "limit": limit
                }

        except Exception as e:
            # 只重试瞬时错误；永久性错误立即抛出（理由同 get_table_indexes）
            if retry < MAX_RETRY_COUNT - 1 and is_transient_error(e):
                logger.warning(f"表搜索失败(第{retry+1}次，瞬时错误)，{RETRY_DELAY}秒后重试: {e}")
                time.sleep(RETRY_DELAY)
            else:
                raise
        finally:
            # 借出的池连接每轮用完即还（成功 return、瞬时错误重试、永久错误抛错
            # 都走到这）。此前没有归还 API，连接被永久留在池里，同 key 的并发
            # 调用会共用同一条物理连接。
            if conn is not None:
                put_connection(ds, conn, conn_db)
