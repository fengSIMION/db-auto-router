# -*- coding: utf-8 -*-
"""CSV 导出 / 导入（v4.0）——原 1350–1522 行"""

__author__ = "fengkeju"
import csv
import os
import re
import time
from pathlib import Path
from typing import Dict, Any, Optional, List

from .config import EXPORT_BATCH, EXPORT_MAX_ROWS, IMPORT_BATCH
from .directconn import _open_direct_connection_with_timeout, _set_conn_autocommit
from .errutil import safe_cursor
from .pool import get_connection, put_connection
from .sqlguard import validate_read_only_sql
from .util import extract_db_and_table_from_sql, parse_cross_db_table, sql_placeholders


def export_csv_impl(sql: str, file_path: str, ds: Dict[str, str], default_db: str,
                    timeout: Optional[int] = None, max_rows: Optional[int] = None) -> Dict[str, Any]:
    """流式导出查询结果为 CSV（绕过常规行数限制）。

    - 仅允许只读 SQL（复用 validate_read_only_sql 校验，去掉查询工具的 LIMIT 自动追加）
    - 游标分批读取（EXPORT_BATCH），逐批写文件，内存占用恒定
    - 行数硬上限 EXPORT_MAX_ROWS 防爆盘
    """
    validate_read_only_sql(sql)

    out_path = Path(file_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    target_db, table_name, is_cross_db = extract_db_and_table_from_sql(sql, default_db)

    if timeout and timeout > 0:
        conn = _open_direct_connection_with_timeout(ds, target_db if is_cross_db else None, timeout)
        conn_is_direct = True
    else:
        conn = get_connection(ds, target_db if is_cross_db else None)
        conn_is_direct = False

    cap = max_rows if (max_rows and max_rows > 0) else EXPORT_MAX_ROWS
    total = 0
    columns: List[str] = []
    start_time = time.time()
    truncated = False
    # 先写 .tmp、成功后整体改名：原先直接往目标路径写，导 200 万行时在 80 万行处
    # 被客户端断开/Ctrl-C/驱动报错，盘上会留下一个带表头和 80 万行的文件，
    # 没有任何截断标记——后续脚本把它当完整数据读入，静默的数据错误。
    # os.replace 在同分区上是原子 rename，读者只会看到"完整文件"或"没有文件"。
    tmp_path = out_path.with_name(out_path.name + ".part")
    try:
        with safe_cursor(conn) as cursor:
            cursor.execute(sql)
            if not cursor.description:
                raise ValueError("导出 SQL 没有返回结果集")
            columns = [desc[0] for desc in cursor.description]

            with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                while True:
                    rows = cursor.fetchmany(EXPORT_BATCH)
                    if not rows:
                        break
                    for row in rows:
                        if total >= cap:
                            truncated = True
                            break
                        writer.writerow(["" if v is None else v for v in row])
                        total += 1
                    if truncated:
                        break
        execution_time_ms = round((time.time() - start_time) * 1000, 2)
        os.replace(tmp_path, out_path)
        return {
            "success": True,
            "file_path": str(out_path.resolve()),
            "columns": columns,
            "rows_exported": total,
            "truncated": truncated,
            "truncated_at": cap if truncated else None,
            "execution_time_ms": execution_time_ms,
            "dialect": ds.get("db_type", "").lower().strip(),
            "message": f"已导出 {total} 行到 {out_path}" + (f"（达到上限 {cap} 行，已截断）" if truncated else "")
        }
    finally:
        # 失败/中断路径清掉半截文件，绝不留一个看起来像完整导出的 .part
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        if conn_is_direct:
            try:
                conn.close()
            except Exception:
                pass
        else:
            # 借出的池连接必须归还（导出可能跑数分钟，久到被空闲清理盯上）
            put_connection(ds, conn, target_db if is_cross_db else None)


def _quote_identifier(name: str) -> str:
    """校验标识符合法性，防 SQL 注入（表名/列名只允许 字母、数字、下划线、$）。

    刻意保持「原样返回、不加引号」：五种方言对未加引号标识符的大小写折叠规则
    并不一致（DM/Oracle 折成大写，PG 折成小写，MySQL 取决于
    lower_case_table_names），统一加引号能救 PG 的混合大小写表名，却会让达梦的
    常规小写下标直接找不到表。取舍之下保持原样，把限制写明：

    - 表名/列名必须是 字母或下划线开头，后接字母数字下划线 $（$ 在 MySQL /
      PostgreSQL / Oracle / 达梦 / GaussDB 中都合法）
    - 含空格、连字符、点号等特殊字符的名称会被拒绝——请先在数据库侧改名
    - PostgreSQL 侧若表是以带引号的混合大小写创建的（"MyTable"），此处传
      MyTable 会因折叠成 mytable 而找不到表，需传实际存储的小写形式
    """
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_$]*$', name):
        raise ValueError(
            f"非法标识符: {name!r}（仅允许字母、数字、下划线、$，且须以字母或下划线开头）"
        )
    return name


def count_csv_rows(file_path: str) -> Dict[str, Any]:
    """数 CSV 的数据行数（不含表头），供 import_csv 的确认弹窗做行数预览。

    只读文件、流式逐行数，不进数据库。返回 {"rows": int|None, "note": str}，
    与 writeengine.estimate_affected_rows 同形状，直接喂给确认层。
    """
    in_path = Path(file_path)
    if not in_path.exists():
        return {"rows": None, "note": f"CSV 文件不存在: {file_path}"}
    rows = 0
    try:
        with open(in_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)          # 表头不算数据行
            for row in reader:
                if row:
                    rows += 1
    except Exception as e:
        return {"rows": None, "note": f"读取 CSV 失败: {e}"}
    return {"rows": rows, "note": "CSV 数据行数（不含表头）"}


def import_csv_impl(file_path: str, table_name: str, ds: Dict[str, str], default_db: str,
                    batch_size: int = IMPORT_BATCH, truncate_first: bool = False) -> Dict[str, Any]:
    """CSV 批量导入：读 CSV → executemany 分批插入 → 全部成功才提交（原子性）。

    - 列名从 CSV 表头读取，逐一校验合法性
    - CSV 中的空字符串按 NULL 写入目标列
    - 失败整批回滚，不会出现半导入状态
    - truncate_first=True 时先清空表（危险，需要确认层拦截）

    关于 truncate_first 的实现说明：清空走 DELETE FROM 而非 TRUNCATE TABLE。
    TRUNCATE 是 DDL，会隐式提交当前事务——一旦这么做，"失败整批回滚"的原子性
    保证就没了（清空已生效，插入失败也退不回去）。DELETE 是 DML，可随本函数
    的事务一起回滚。代价是大表上 DELETE 慢且产生大量 undo log，
    所以这个参数才标为"危险操作"并要求确认层拦截。
    """
    in_path = Path(file_path)
    if not in_path.exists():
        raise FileNotFoundError(f"CSV 文件不存在: {file_path}")

    target_db, real_table = parse_cross_db_table(table_name, default_db)
    quoted_table = _quote_identifier(real_table)

    conn = get_connection(ds, target_db)
    typ = ds.get("db_type", "").lower().strip()
    _set_conn_autocommit(conn, typ, False)

    inserted = 0
    start_time = time.time()
    try:
        with open(in_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                raise ValueError("CSV 文件为空（无表头）")
            columns = [_quote_identifier(h.strip()) for h in header if h.strip()]
            col_count = len(columns)
            if col_count == 0:
                raise ValueError("CSV 表头无有效列名")

            placeholders = sql_placeholders(typ, col_count)
            insert_sql = f"INSERT INTO {quoted_table} ({', '.join(columns)}) VALUES ({placeholders})"

            # safe_cursor 收拢游标生命周期：CSV 读取或 executemany 中途抛错也保证关游标
            with safe_cursor(conn) as cursor:
                if truncate_first:
                    cursor.execute(f"DELETE FROM {quoted_table}")

                batch: List[tuple] = []
                for row in reader:
                    if not row:
                        continue
                    if len(row) != col_count:
                        # 容忍尾部空字段缺失（补齐），其余报错
                        if len(row) < col_count:
                            row = row + [""] * (col_count - len(row))
                        else:
                            raise ValueError(f"第 {inserted + len(batch) + 2} 行字段数不匹配（期望 {col_count}，实际 {len(row)}）")
                    batch.append(tuple(v if v != "" else None for v in row))
                    if len(batch) >= batch_size:
                        cursor.executemany(insert_sql, batch)
                        inserted += len(batch)
                        batch = []
                if batch:
                    cursor.executemany(insert_sql, batch)
                    inserted += len(batch)

        conn.commit()
        execution_time_ms = round((time.time() - start_time) * 1000, 2)
        return {
            "success": True,
            "table": table_name,
            "rows_inserted": inserted,
            "columns": columns,
            "batch_size": batch_size,
            "execution_time_ms": execution_time_ms,
            "dialect": typ,
            "message": f"导入完成：{inserted} 行已插入 {table_name}（已提交）"
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _set_conn_autocommit(conn, typ, True)  # 归还前恢复会话级 autocommit
        # 导入持连接做大批量 executemany，跑完必须归还，否则这条连接永远占着池子
        put_connection(ds, conn, target_db)
