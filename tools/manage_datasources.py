# -*- coding: utf-8 -*-
"""数据源维护小工具（v4.1，SQLite 配置库模式专用）

不装 DB Browser 也能用命令行维护 data/datasources.db（在项目根目录执行）：

    python tools/manage_datasources.py list                     # 查看已注册数据源
    python tools/manage_datasources.py add                      # 交互式添加（一路回车+输入）
    python tools/manage_datasources.py set-pwd <db_code>        # 修改某数据源密码
    python tools/manage_datasources.py del <db_code> [--dir P]  # 删除（默认只删 '*' 兜底行）
    python tools/manage_datasources.py template                 # 一键插入兜底示例行（占位密码）

仅读取 db_auto_router.config 里的 MCP_CONFIG_SQLITE_PATH / MCP_CONFIG_MODE，
不依赖 mysql 等任何第三方驱动。
"""

__author__ = "fengkeju"
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # 项目根（tools/ 的上一级）
sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

# 强制 sqlite 模式读路径（本工具只管 SQLite 配置库）
os.environ.setdefault("MCP_CONFIG_MODE", "sqlite")

from db_auto_router.config import CFG_MODE, SQLITE_DB_PATH  # noqa: E402
from db_auto_router.datasources import upgrade_sqlite_config_schema  # noqa: E402

if CFG_MODE != "sqlite":
    print("⚠️ 当前 .env 为 mysql 模式，本工具只维护 SQLite 配置库。")
    print("   如需切换：.env 中设 MCP_CONFIG_MODE=sqlite 后再运行本工具。")
    sys.exit(1)

import sqlite3  # noqa: E402

DB_PATH = SQLITE_DB_PATH
VALID_TYPES = ("mysql", "dm", "postgres", "gaussdb", "oracle")

# conn_url 按方言给提示与默认值。格式不统一（原样交给对应驱动），
# 写死一套会把非 MySQL 用户带偏：PG/GaussDB 必须带 dbname，DM/Oracle 是 DSN。
_CONN_URL_HINTS = {
    "mysql":    ("主机:端口，如 127.0.0.1:3306（库名取自 db_code，不要写在这里）", "127.0.0.1:3306"),
    "dm":       ("主机:端口，如 10.0.0.1:5236（schema 由 DM 用户决定）", "127.0.0.1:5236"),
    "postgres": ("libpq 连接串，必须带 dbname，如 host=127.0.0.1 port=5432 dbname=mydb",
                 "host=127.0.0.1 port=5432 dbname=postgres"),
    "gaussdb":  ("libpq 连接串，必须带 dbname，如 host=127.0.0.1 port=15432 dbname=mydb",
                 "host=127.0.0.1 port=15432 dbname=postgres"),
    "oracle":   ("Easy Connect 主机:端口/服务名，如 10.0.0.1:1521/orcl",
                 "127.0.0.1:1521/orcl"),
}

# db_schema 按方言给提示（v4.4）。MySQL 的库名就是 db_code，这一列对它无意义；
# PG/GaussDB 默认 public、Oracle/DM 默认当前登录用户，都不填也能用。
_SCHEMA_HINTS = {
    "mysql":    "（MySQL 留空回车——库名取自 db_code）",
    "dm":       "（达梦 owner，留空=当前登录用户）",
    "postgres": "（PG schema，留空=public）",
    "gaussdb":  "（高斯 schema，留空=public）",
    "oracle":   "（Oracle owner，留空=当前登录用户）",
}


def _connect():
    first = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS mcp_datasource_config (
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
        PRIMARY KEY (dir_path, db_code))""")
    # 老配置库补 db_schema / write_confirm 列（与 MCP / GUI 共用同一实现）
    upgrade_sqlite_config_schema(conn)
    conn.commit()
    if first:
        print(f"✅ 已创建配置库: {DB_PATH}")
    return conn


def cmd_list():
    conn = _connect()
    rows = conn.execute(
        "SELECT dir_path, db_name, db_code, db_type, conn_url, db_user, db_schema, is_default, write_confirm "
        "FROM mcp_datasource_config ORDER BY dir_path, db_code").fetchall()
    conn.close()
    if not rows:
        print("（空）还没有数据源。运行: python manage_datasources.py add")
        return
    print(f"{'目录':<40} {'编码':<18} {'类型':<9} {'地址':<24} {'用户':<14} {'Schema':<12} 默认 审核")
    print("-" * 140)
    for dir_path, db_name, db_code, db_type, conn_url, db_user, db_schema, is_def, wconf in rows:
        show_dir = dir_path if dir_path not in ("*", "") else "* (任意目录兜底)"
        print(f"{show_dir:<40} {db_code:<18} {db_type:<9} {conn_url:<24} {db_user:<14} "
              f"{(db_schema or '-'):<12} {'✓' if is_def == 1 else '':<4} "
              f"{'需审核' if (wconf is None or int(wconf) == 1) else '免审核'}")


def _ask(prompt, default="", required=False, secret=False):
    import getpass
    while True:
        if secret:
            v = getpass.getpass(prompt + (f" [{default}]" if default else "") + ": ")
        else:
            v = input(prompt + (f" [{default}]" if default else "") + ": ").strip()
        if not v:
            v = default
        if v or not required:
            return v
        print("  必填，请重新输入")


def cmd_add():
    print("—— 添加数据源（回车采用 [方括号] 默认值）——")
    dir_path = _ask("工作目录（直接回车=任意目录兜底'*'，多项目才填如 D:\\myproject）", "*", )
    db_name = _ask("备注名（给自己看）", "")
    db_code = _ask("数据源编码（小写下划线，如 mydb）", "", required=True)
    while True:
        db_type = _ask(f"数据库类型 {'/'.join(VALID_TYPES)}", "mysql").lower()
        if db_type in VALID_TYPES:
            break
        print(f"  只能填: {' / '.join(VALID_TYPES)}")
    # conn_url 格式随方言而变，提示和默认值都必须跟着变——写死 MySQL 格式会把
    # 配 PG/GaussDB 的人直接带偏（他们必须带 dbname，填 127.0.0.1:5432 会连到默认库）
    hint, default = _CONN_URL_HINTS.get(db_type, _CONN_URL_HINTS["mysql"])
    conn_url = _ask(f"连接地址（{db_type}）{hint}", default, required=True)
    db_user = _ask("用户名", "root", required=True)
    db_password = _ask("密码", "", required=True, secret=True)
    # v4.4：PG/GaussDB/Oracle 的表结构查询按这个 schema/owner 过滤；
    # 不填则用方言默认（PG=public / current_schema()，Oracle/DM=当前登录用户）
    db_schema = _ask(f"Schema/Owner（可留空）{_SCHEMA_HINTS.get(db_type, '')}", "")
    is_def = 1 if _ask("设为默认数据源? (y/n)", "y").lower() == "y" else 0
    # v4.6：写操作人工审核。默认 y=需审核（与老行为一致）；开发库填 n，
    # 之后 execute/import_csv/dump_database 对该数据源全部免确认
    write_confirm = 0 if _ask("写操作需人工审核? (y/n，开发库造数据填 n 免确认)", "y").lower() == "n" else 1

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO mcp_datasource_config "
            "(dir_path, db_name, db_code, db_type, conn_url, db_user, db_password, db_schema, is_default, write_confirm) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (dir_path, db_name, db_code, db_type, conn_url, db_user, db_password, db_schema, is_def, write_confirm))
        conn.commit()
        print(f"✅ 已添加: {db_code} ({db_type} @ {conn_url})" +
              ("，任意目录兜底" if dir_path in ("*", "") else f"，目录 {dir_path}"))
        if write_confirm == 0:
            print("   ℹ️ 已标记免人工审核：该数据源的 execute/import_csv/dump_database 不再弹确认窗"
                  "（审计日志照旧记录）")
        if db_type in ("postgres", "gaussdb") and "dbname" not in conn_url.lower():
            print("   ⚠️ PostgreSQL/GaussDB 的 conn_url 必须带 dbname，"
                  "否则连的是驱动默认库。示例: host=127.0.0.1 port=5432 dbname=mydb")
        if db_type in ("postgres", "gaussdb", "oracle", "dm") and not db_schema:
            print(f"   ℹ️ 未填 Schema/Owner，表结构查询按方言默认过滤"
                  f"（{'PG' if 'postgres' in db_type or db_type=='gaussdb' else '当前登录用户'}）。"
                  f"若 get_schema_info 查不到表，再补这一列。")
    except sqlite3.IntegrityError:
        print(f"❌ 已存在同目录同编码的记录: dir_path={dir_path!r}, db_code={db_code!r}")
    finally:
        conn.close()


def cmd_set_pwd(db_code):
    import getpass
    new_pwd = getpass.getpass(f"输入 {db_code} 的新密码: ")
    if not new_pwd:
        print("❌ 密码不能为空")
        return
    conn = _connect()
    cur = conn.execute("UPDATE mcp_datasource_config SET db_password=? WHERE db_code=?",
                       (new_pwd, db_code))
    conn.commit()
    n = cur.rowcount
    conn.close()
    print(f"✅ 已更新 {n} 条记录的密码" if n else f"❌ 未找到 db_code={db_code}")


def cmd_del(db_code, dir_path="*"):
    conn = _connect()
    cur = conn.execute("DELETE FROM mcp_datasource_config WHERE db_code=? AND dir_path=?",
                       (db_code, dir_path))
    conn.commit()
    n = cur.rowcount
    conn.close()
    print(f"✅ 已删除 {n} 条记录" if n else
          f"❌ 未找到 dir_path={dir_path!r}, db_code={db_code!r}（用 list 查看实际 dir_path）")


def cmd_template():
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO mcp_datasource_config "
        "(dir_path, db_name, db_code, db_type, conn_url, db_user, db_password, db_schema, is_default, write_confirm) "
        "VALUES ('*', '示例数据源', 'mydb', 'mysql', '127.0.0.1:3306', 'root', '改成你的密码', '', 1, 1)")
    conn.commit()
    conn.close()
    print("✅ 已插入兜底示例行（db_code=mydb，占位密码）。")
    print("   用 DB Browser (SQLite) 打开 datasources.db，把 conn_url/db_user/db_password 改成你的数据库即可；")
    print("   或运行: python manage_datasources.py del mydb 删掉后重新 add。")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "list":
        cmd_list()
    elif cmd == "add":
        cmd_add()
    elif cmd == "set-pwd" and len(args) >= 2:
        cmd_set_pwd(args[1])
    elif cmd == "del" and len(args) >= 2:
        cmd_del(args[1], args[3] if len(args) >= 4 and args[2] == "--dir" else "*")
    elif cmd == "template":
        cmd_template()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
