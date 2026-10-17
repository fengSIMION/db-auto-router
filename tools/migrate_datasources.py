# -*- coding: utf-8 -*-
"""migrate_datasources.py — MySQL 配置库 → SQLite 本地配置库 一键迁移 (v4.2)

把 mysql 模式配置库（mcp.mcp_datasource_config）里的全部数据源搬进
SQLite 配置文件 datasources.db，两表结构同构、字段原样搬运。

用法（在项目根目录执行）：
    python tools/migrate_datasources.py --dry-run     # 预览将迁移的行，不写入
    python tools/migrate_datasources.py               # 执行迁移（自动备份现有文件）

行为：
1. 读 .env 的 MCP_CONFIG_* 连接 MySQL 配置库（MCP_CONFIG_MODE 是什么无所谓，
   本脚本永远从 MySQL 侧读源）
2. 若 data/datasources.db 已存在 → 自动备份为 .bak-YYYYmmddHHMMSS
3. INSERT OR REPLACE 全量写入（同主键 dir_path+db_code 覆盖，其余保留）
4. 打印逐条核对报告；.env 切 MCP_CONFIG_MODE=sqlite 由你自己决定何时改
"""

__author__ = "fengkeju"
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # 项目根（tools/ 的上一级）
sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
    if (BASE_DIR / ".env").exists():
        load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

# 强制 sqlite 模式只为拿到正确的 SQLITE_DB_PATH（本脚本只写 SQLite 文件）
os.environ.setdefault("MCP_CONFIG_MODE", "sqlite")
from db_auto_router.config import SQLITE_DB_PATH  # noqa: E402
from db_auto_router.datasources import upgrade_sqlite_config_schema  # noqa: E402

SQLITE_PATH = SQLITE_DB_PATH

# v4.4/v4.6：db_schema、write_confirm 可能还没加到 MySQL 配置库（老库）。
# 缺列时按旧字段集读取——db_schema 缺失则迁移过来的行该列为空（PG/Oracle 用户
# 事后再用 GUI/CLI 补）；write_confirm 缺失则目标行按默认 1=需审核（安全默认）。
BASE_FIELDS = ("dir_path", "db_name", "db_code", "db_type",
               "conn_url", "db_user", "db_password", "is_default")
OPTIONAL_FIELDS = ("db_schema", "write_confirm")

# sqlite 模式的配置库文件路径（相对路径按本目录解析；可写绝对路径）
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_datasource_config (
    dir_path    TEXT NOT NULL DEFAULT '*',
    db_name     TEXT,
    db_code     TEXT NOT NULL,
    db_type     TEXT NOT NULL,
    conn_url    TEXT NOT NULL,
    db_user     TEXT NOT NULL,
    db_password TEXT NOT NULL,
    db_schema   TEXT,
    is_default  INTEGER DEFAULT 0,
    write_confirm INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (dir_path, db_code)
)
"""


def mysql_config_conn():
    """连接 .env 指向的 MySQL 配置库"""
    try:
        import mysql.connector
    except ImportError:
        print("❌ 缺少驱动: pip install mysql-connector-python")
        sys.exit(1)
    cfg = {k: os.environ.get(k) for k in
           ("MCP_CONFIG_HOST", "MCP_CONFIG_PORT", "MCP_CONFIG_USER",
            "MCP_CONFIG_PASSWORD", "MCP_CONFIG_DATABASE")}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        print(f"❌ .env 缺少配置项: {', '.join(missing)}")
        sys.exit(1)
    return mysql.connector.connect(
        host=cfg["MCP_CONFIG_HOST"], port=int(cfg["MCP_CONFIG_PORT"]),
        user=cfg["MCP_CONFIG_USER"], password=cfg["MCP_CONFIG_PASSWORD"],
        database=cfg["MCP_CONFIG_DATABASE"], charset="utf8mb4",
        connection_timeout=10)


def source_fields(conn):
    """探测 MySQL 配置库实际有哪些可选字段：有就一起搬，没有就按旧字段集。"""
    cur = conn.cursor()
    present = []
    try:
        for col in OPTIONAL_FIELDS:
            cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() "
                        "AND TABLE_NAME = 'mcp_datasource_config' AND COLUMN_NAME = %s",
                        (col,))
            if cur.fetchone()[0]:
                present.append(col)
            else:
                print(f"ℹ️ MySQL 配置库还没有 {col} 列，按旧字段集迁移"
                      f"（补列语句见 docs/mysql_config.sql）")
    finally:
        cur.close()
    return BASE_FIELDS + tuple(present)


def fetch_mysql_rows():
    conn = mysql_config_conn()
    try:
        fields = source_fields(conn)
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(fields)} FROM mcp_datasource_config")
        rows = cur.fetchall()
        cur.close()
        return fields, rows
    finally:
        conn.close()


def backup_sqlite():
    """已有 datasources.db → 备份为带时间戳的 .bak 文件，返回备份路径（无文件返回 None）"""
    if not os.path.exists(SQLITE_PATH):
        return None
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    bak = f"{SQLITE_PATH}.bak-{ts}"
    shutil.copy2(SQLITE_PATH, bak)
    return bak


def main():
    ap = argparse.ArgumentParser(description="MySQL 配置库 → SQLite 一键迁移")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    args = ap.parse_args()

    print(f"源:   MySQL 配置库 {os.environ.get('MCP_CONFIG_HOST')}:"
          f"{os.environ.get('MCP_CONFIG_PORT')}/{os.environ.get('MCP_CONFIG_DATABASE')}")
    print(f"目标: SQLite 文件 {SQLITE_PATH}")

    try:
        fields, rows = fetch_mysql_rows()
    except Exception as e:
        print(f"❌ 读取 MySQL 配置库失败: {type(e).__name__}: {e}")
        sys.exit(1)

    if not rows:
        print("⚠️ MySQL 配置库为空，没有可迁移的数据")
        sys.exit(0)

    print(f"\n共 {len(rows)} 条数据源将迁移：")
    for r in rows:
        rec = dict(zip(fields, r))
        show_dir = rec["dir_path"] if rec["dir_path"] not in ("*", "") else "* (兜底)"
        print(f"  [{'默认' if rec['is_default'] == 1 else '    '}] {show_dir:<45} "
              f"{rec['db_code']:<18} {rec['db_type']:<8} {rec['conn_url']}"
              + (f"  schema={rec['db_schema']}" if rec.get("db_schema") else "")
              + ("  免人工审核" if rec.get("write_confirm") == 0 else ""))

    if args.dry_run:
        print("\n(dry-run 模式：未写入任何数据。去掉 --dry-run 执行迁移)")
        return

    bak = backup_sqlite()
    if bak:
        print(f"\n已备份现有配置库 → {bak}")

    conn = sqlite3.connect(SQLITE_PATH)
    try:
        conn.execute(SQLITE_SCHEMA)
        upgrade_sqlite_config_schema(conn)   # 老文件补 db_schema / write_confirm 列
        cur = conn.executemany(
            f"INSERT OR REPLACE INTO mcp_datasource_config ({', '.join(fields)})"
            f" VALUES ({', '.join('?' * len(fields))})", rows)
        conn.commit()
        written = cur.rowcount if cur.rowcount >= 0 else len(rows)
    except sqlite3.Error as e:
        print(f"❌ 写入 SQLite 失败: {e}")
        sys.exit(1)
    finally:
        conn.close()

    # 核对
    conn = sqlite3.connect(SQLITE_PATH)
    n = conn.execute("SELECT COUNT(*) FROM mcp_datasource_config").fetchone()[0]
    conn.close()

    print(f"\n✅ 迁移完成：写入 {len(rows)} 条，目标表现有 {n} 条")
    if n < len(rows):
        print(f"⚠️ 目标表行数({n})少于迁移行数({len(rows)})——可能存在主键冲突覆盖，"
              "请用 DB Browser 核对")
    print("\n后续步骤：")
    print("  1. .env 中把 MCP_CONFIG_MODE 改为 sqlite（改完重启 AI 会话生效）")
    print("  2. 用「start_datasource_manager.bat」或 DB Browser 核对/维护数据源")
    print(f"  3. MySQL 配置库保持原样即可（未做任何删除，可随时切回 mysql 模式）")


if __name__ == "__main__":
    main()
