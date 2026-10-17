# -*- coding: utf-8 -*-
"""数据源维护可视化小工具（v4.7 双后端版）

用法：python tools/manage_datasources_gui.py 或双击 start_datasource_manager.bat
可加参数强制后端：--backend sqlite 或 --backend mysql（默认跟随 .env 的 MCP_CONFIG_MODE）

v4.7 变更（小白向）：
- 底部状态栏「共 N 条」行尾展示 MCP 版本号
- mysql 模式启动时若配置库缺 mcp_datasource_config 表（或缺 mcp 库本身）：
  弹框询问是否一键初始化——选「是」则幂等建库建表（CREATE IF NOT EXISTS，
  绝不 DROP，既有数据原样不动），再自动重载；选「否」提示手动执行
  docs/mysql_config.sql。连不上与"表没建"从此分开提示，不再一律报"无法连接"

v4.2 变更：
- 双后端直写：GUI 编辑的永远是「MCP 正在读的那个配置库」——
  .env 为 mysql 模式时直接编辑 MySQL 配置库，sqlite 模式编辑 datasources.db
- 添加/编辑弹窗顶部标注当前写入目标（MySQL 配置库 / SQLite 文件）
- 写完 MySQL 配置库自动 bump 版本号（mcp_config_meta），MCP 下一次查询秒级感知；
  写 SQLite 靠文件 mtime，MCP 同样秒级感知
- 「刷新」按钮升级为「刷新数据源」：重读列表并显示当前后端与变更指纹
"""

__author__ = "fengkeju"
import argparse
import os
import sys
import sqlite3
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent   # 项目根（tools/ 的上一级）
sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
    if (BASE_DIR / ".env").exists():
        load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

from db_auto_router import config as cfg  # noqa: E402
from db_auto_router.datasources import (bump_mysql_config_version,  # noqa: E402
                                        upgrade_sqlite_config_schema,
                                        mysql_config_has_column,
                                        ensure_mysql_config_schema)
from db_auto_router.directconn import explain_dm_connect_error  # noqa: E402
import tkinter as tk  # noqa: E402
from tkinter import ttk, messagebox  # noqa: E402

VALID_TYPES = ("mysql", "dm", "postgres", "gaussdb", "oracle")
PIP_PKGS = {"mysql": "mysql-connector-python", "dm": "dmPython",
            "postgres": "psycopg2-binary", "gaussdb": "psycopg2-binary",
            "oracle": "oracledb"}

# mysql.connector 连接/权限类错误号：判为"连不上"，不再试探建库建表
# （1045 拒绝访问 / 2002 2003 找不到或连不上服务器 / 2013 连接丢失 …）
_CONNECT_ERRNOS = {1045, 1698, 2002, 2003, 2005, 2006, 2013}


# ------------------------------------------------------------------------------
# 多显示器定位：按「目标显示器的工作区」算尺寸与居中坐标（Windows 专用，失败回退）
# ------------------------------------------------------------------------------


def _target_work_area(win=None):
    """定位目标显示器的工作区，返回 (left, top, width, height)；探测不到返回 None。

    win=None：鼠标所在屏（主窗口启动用——双击图标在哪块屏，就在哪块屏打开）；
    win=主窗口：该窗口所在屏（弹窗用——弹窗跟着主窗口，不跑别屏去）。

    为什么不能直接用 tkinter 的 winfo_screenwidth/height：它只反映主屏。
    多显示器下按主屏尺寸算居中坐标，窗口会被甩到主屏上去——主窗口在副屏时，
    弹窗尤其明显（人在大屏上操作，弹窗蹦到小屏去了）。工作区已排除任务栏，
    居中才是视觉正中。非 Windows / 探测失败：返回 None，调用方回退 Tk 度量。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        user32 = ctypes.windll.user32
        if win is None:
            p = POINT()
            if not user32.GetCursorPos(ctypes.byref(p)):
                return None
            px, py = p.x, p.y
        else:
            px = win.winfo_rootx() + win.winfo_width() // 2
            py = win.winfo_rooty() + win.winfo_height() // 2
        # MONITOR_DEFAULTTONEAREST：点不在任何显示器上时取最近的一块
        hmon = user32.MonitorFromPoint(POINT(px, py), 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not hmon or not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        return (mi.rcWork.left, mi.rcWork.top,
                mi.rcWork.right - mi.rcWork.left,
                mi.rcWork.bottom - mi.rcWork.top)
    except Exception:
        return None


def _center_on(win, w, h, area):
    """把 w x h 的窗口放到 area=(left, top, aw, ah) 工作区正中。

    尺寸上限 = 工作区的 90%：留足余量，表单长高点也不会把内容挤出窗外
    （旧代码弹窗高度封顶 70% 屏高，偏紧）。
    """
    left, top, aw, ah = area
    w = min(int(w), int(aw * 0.9))
    h = min(int(h), int(ah * 0.9))
    win.geometry(f"{w}x{h}+{left + max(0, (aw - w) // 2)}"
                 f"+{top + max(0, (ah - h) // 2)}")


# ------------------------------------------------------------------------------
# 存储后端抽象：SqliteBackend / MysqlBackend（统一接口，GUI 不感知差异）
# ------------------------------------------------------------------------------

class SqliteBackend:
    """SQLite 本地文件配置库（datasources.db）"""
    kind = "sqlite"

    def __init__(self):
        self.path = cfg.SQLITE_DB_PATH

    @property
    def desc(self):
        return f"SQLite 本地文件 {self.path}"

    @property
    def desc_color(self):
        return "#1B5E9E"   # 蓝色

    def _conn(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("""CREATE TABLE IF NOT EXISTS mcp_datasource_config (
            dir_path TEXT NOT NULL DEFAULT '*', db_name TEXT, db_code TEXT NOT NULL,
            db_type TEXT NOT NULL, conn_url TEXT NOT NULL, db_user TEXT NOT NULL,
            db_password TEXT NOT NULL, db_schema TEXT, is_default INTEGER DEFAULT 0,
            write_confirm INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (dir_path, db_code))""")
        # 老配置库补 db_schema / write_confirm 列（与 MCP 侧共用同一实现，避免 DDL 漂移）
        upgrade_sqlite_config_schema(c)
        c.commit()
        return c

    def fetch_all(self):
        c = self._conn()
        try:
            # 行形状（9 字段，MysqlBackend 同口径）：... is_default, write_confirm
            # 排序：目录分组，组内默认数据源（is_default=1）置顶，再按数据库名称——
            # 与 MCP 侧选默认源的口径一致（is_default DESC 优先）
            return c.execute("SELECT dir_path, db_name, db_code, db_type, conn_url,"
                             " db_user, db_schema, is_default, write_confirm"
                             " FROM mcp_datasource_config"
                             " ORDER BY dir_path, is_default DESC, db_code").fetchall()
        finally:
            c.close()

    def fetch_pwd(self, d, code):
        c = self._conn()
        try:
            r = c.execute("SELECT db_password FROM mcp_datasource_config"
                          " WHERE dir_path=? AND db_code=?", (d, code)).fetchone()
            return r[0] if r else ""
        finally:
            c.close()

    def save_row(self, orig, v):
        """orig=None 新增；否则 (dir, code) 为旧行主键。返回 (ok, 错误信息)"""
        c = self._conn()
        try:
            if orig is None:
                c.execute("INSERT INTO mcp_datasource_config (dir_path, db_name, db_code,"
                          " db_type, conn_url, db_user, db_password, db_schema, is_default,"
                          " write_confirm) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (v["dir"], v["name"], v["code"], v["type"], v["url"],
                           v["user"], v["pwd"], v.get("schema", ""), v["default"],
                           v["confirm"]))
            else:
                c.execute("UPDATE mcp_datasource_config SET dir_path=?, db_name=?, db_code=?,"
                          " db_type=?, conn_url=?, db_user=?, db_password=?, db_schema=?,"
                          " is_default=?, write_confirm=? WHERE dir_path=? AND db_code=?",
                          (v["dir"], v["name"], v["code"], v["type"], v["url"],
                           v["user"], v["pwd"], v.get("schema", ""), v["default"],
                           v["confirm"], orig[0], orig[1]))
            c.commit()
            return True, ""
        except sqlite3.IntegrityError:
            return False, f"已存在同目录同数据库名称的记录：目录={v['dir']!r}, 数据库名称={v['code']!r}"
        finally:
            c.close()

    def delete_row(self, d, code):
        c = self._conn()
        try:
            c.execute("DELETE FROM mcp_datasource_config WHERE dir_path=? AND db_code=?",
                      (d, code))
            c.commit()
        finally:
            c.close()

    def bump_version(self):
        """SQLite 靠文件 mtime 被动感知，无需版本号（写操作本身即更新 mtime）"""

    def fingerprint(self):
        """变更指纹：主文件 + -wal + -shm 取最大 mtime_ns（与 MCP 侧同口径）"""
        latest = -1
        for suffix in ("", "-wal", "-shm"):
            try:
                latest = max(latest, os.stat(self.path + suffix).st_mtime_ns)
            except OSError:
                continue
        return latest

    def fingerprint_text(self):
        import datetime
        fp = self.fingerprint()
        if fp < 0:
            return "文件尚未创建"
        return "最后修改: " + datetime.datetime.fromtimestamp(fp / 1e9)\
            .strftime("%Y-%m-%d %H:%M:%S")


class MysqlBackend:
    """MySQL 配置库直写（.env 的 MCP_CONFIG_* 连接信息）"""
    kind = "mysql"

    def __init__(self):
        self.host = cfg.CFG_HOST
        self.port = cfg.CFG_PORT
        self.user = cfg.CFG_USER
        self.pwd = cfg.CFG_PWD
        self.db = cfg.CFG_DB

    @property
    def desc(self):
        return f"MySQL 配置库 {self.host}:{self.port}/{self.db}"

    @property
    def desc_color(self):
        return "#B35400"   # 橙色

    def _conn(self):
        try:
            import mysql.connector
        except ImportError:
            raise RuntimeError("缺少驱动：pip install mysql-connector-python")
        return mysql.connector.connect(
            host=self.host, port=self.port, user=self.user, password=self.pwd,
            database=self.db, charset="utf8mb4",
            connection_timeout=cfg.CONNECT_TIMEOUT, autocommit=True)

    # 列探测结果缓存（v4.4 db_schema / v4.6 write_confirm）：None=未探测。老配置库没这些列时，
    # GUI 仍正常维护其余字段——与 MCP 侧 datasources.mysql_config_has_column 同口径
    # （探测实现共用一份，不在这里再写一遍 information_schema 查询）。
    _columns = {}

    def has_column(self, column):
        if column not in MysqlBackend._columns:
            try:
                c = self._conn()
                try:
                    MysqlBackend._columns[column] = mysql_config_has_column(c, column)
                finally:
                    c.close()
            except Exception:
                MysqlBackend._columns[column] = False
        return MysqlBackend._columns[column]

    def has_db_schema(self):
        return self.has_column("db_schema")

    def has_write_confirm(self):
        return self.has_column("write_confirm")

    def fetch_all(self):
        c = self._conn()
        try:
            cur = c.cursor(dictionary=True)
            # 缺列时按旧列清单查，字典游标 + .get 自动补空，无需按位置插占位
            cols = ["dir_path", "db_name", "db_code", "db_type", "conn_url", "db_user"]
            if self.has_db_schema():
                cols.append("db_schema")
            # 列顺序与 SqliteBackend 保持一致（is_default 在前、write_confirm 在后），
            # 行形状（9 字段）以下方元组构造为准——位置解包的消费方
            # （_apply_rows / on_edit / on_delete / on_test）都按这个顺序取
            cols.append("is_default")
            if self.has_write_confirm():
                cols.append("write_confirm")
            # 排序与 SqliteBackend 同口径：目录分组，组内默认数据源置顶，再按数据库名称
            cur.execute(f"SELECT {', '.join(cols)} FROM mcp_datasource_config"
                        " ORDER BY dir_path, is_default DESC, db_code")
            # 行形状与 SqliteBackend 一致（9 字段）；缺列时补安全默认
            # （schema='' / write_confirm=1=需审核），保证上层解包不炸
            rows = [(r["dir_path"], r["db_name"], r["db_code"], r["db_type"],
                     r["conn_url"], r["db_user"], r.get("db_schema") or "",
                     r["is_default"],
                     1 if r.get("write_confirm") is None else int(r["write_confirm"]))
                    for r in cur.fetchall()]
            cur.close()
            return rows
        finally:
            c.close()

    def fetch_pwd(self, d, code):
        c = self._conn()
        try:
            cur = c.cursor()
            cur.execute("SELECT db_password FROM mcp_datasource_config"
                        " WHERE dir_path=%s AND db_code=%s", (d, code))
            r = cur.fetchone()
            cur.close()
            return r[0] if r else ""
        finally:
            c.close()

    def save_row(self, orig, v):
        c = self._conn()
        try:
            cur = c.cursor()
            sql_set = ("dir_path=%s, db_name=%s, db_code=%s, db_type=%s, conn_url=%s,"
                       " db_user=%s, db_password=%s")
            vals = (v["dir"], v["name"], v["code"], v["type"], v["url"],
                    v["user"], v["pwd"])
            if self.has_db_schema():
                sql_set += ", db_schema=%s"
                vals += (v.get("schema", ""),)
            sql_set += ", is_default=%s"
            vals += (v["default"],)
            # v4.6：write_confirm 缺列时跳过该列（勾选暂不生效，对话框里会禁用并提示）
            if self.has_write_confirm():
                sql_set += ", write_confirm=%s"
                vals += (v["confirm"],)
            if orig is None:
                cur.execute(f"INSERT INTO mcp_datasource_config SET {sql_set}", vals)
            else:
                cur.execute(f"UPDATE mcp_datasource_config SET {sql_set}"
                            " WHERE dir_path=%s AND db_code=%s",
                            vals + (orig[0], orig[1]))
            c.commit()
            cur.close()
            return True, ""
        except Exception as e:
            msg = str(e)
            if "Duplicate entry" in msg:
                return False, f"已存在同目录同数据库名称的记录：目录={v['dir']!r}, 数据库名称={v['code']!r}"
            if not self.has_db_schema() and "db_schema" in msg:
                return False, ("配置库缺少 db_schema 列，请先在 mcp 库执行：\n"
                               "ALTER TABLE mcp_datasource_config ADD COLUMN db_schema"
                               " varchar(128) NULL;")
            return False, f"{type(e).__name__}: {e}"
        finally:
            c.close()

    def delete_row(self, d, code):
        c = self._conn()
        try:
            cur = c.cursor()
            cur.execute("DELETE FROM mcp_datasource_config WHERE dir_path=%s AND db_code=%s",
                        (d, code))
            c.commit()
            cur.close()
        finally:
            c.close()

    def bump_version(self):
        """写完配置版本号 +1，MCP 侧下一次查询自动感知。

        DDL 与 bump SQL 统一走 datasources.bump_mysql_config_version()——这是唯一实现，
        此处不再内联一份（否则与 MCP 侧 _mysql_probe_version 的三处 DDL 会漂移）。
        失败仅告警：配置本身已写入成功，最坏情况是 MCP 晚一点才感知。
        """
        err = bump_mysql_config_version()
        if err:
            messagebox.showwarning(
                "版本号更新失败",
                f"配置已写入，但 mcp_config_meta 版本号更新失败：\n{err}\n\n"
                "MCP 侧可能要等下一次全量重读才能感知本次变更。", parent=self)

    def fingerprint(self):
        """变更指纹：mcp_config_meta 版本号（探测失败返回 None）"""
        try:
            c = self._conn()
            try:
                cur = c.cursor()
                cur.execute("SELECT version FROM mcp_config_meta WHERE id=1")
                r = cur.fetchone()
                cur.close()
                return int(r[0]) if r else 0
            finally:
                c.close()
        except Exception:
            return None

    def fingerprint_text(self):
        fp = self.fingerprint()
        return f"配置版本号: v{fp}" if fp is not None else "版本号不可用（退回全量重读模式）"

    # ---- v4.7 配置表缺失自愈：探测 + 一键初始化（小白向）----
    def _server_conn(self):
        """连 MySQL 服务器本身（不带 database）——建库 / SHOW DATABASES 用。"""
        import mysql.connector
        return mysql.connector.connect(
            host=self.host, port=self.port, user=self.user, password=self.pwd,
            charset="utf8mb4", connection_timeout=cfg.CONNECT_TIMEOUT)

    def database_exists(self):
        c = self._server_conn()
        try:
            cur = c.cursor()
            cur.execute("SHOW DATABASES LIKE %s", (self.db,))
            ok = cur.fetchone() is not None
            cur.close()
            return ok
        finally:
            c.close()

    def table_exists(self):
        """配置库里是否已有 mcp_datasource_config 表（连不上会抛异常，由调用方归类）。"""
        c = self._conn()
        try:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='mcp_datasource_config'",
                        (self.db,))
            n = cur.fetchone()[0]
            cur.close()
            return bool(n)
        finally:
            c.close()

    def classify_problem(self, err):
        """把 fetch_all 的失败归类，供上层决定弹什么框。
        返回 'no_database' / 'no_table' / 'no_connect' / 'other'。
        只在 mysql 后端已失败时调用（后台线程），可再探一两次库。"""
        # 明显的连接/权限类错误号直接判死刑，避免白白再等一次连接超时
        if getattr(err, "errno", None) in _CONNECT_ERRNOS:
            return "no_connect"
        try:
            if not self.database_exists():
                return "no_database"
        except Exception:
            return "no_connect"
        try:
            return "other" if self.table_exists() else "no_table"
        except Exception:
            return "no_connect"

    def init_schema(self):
        """一键初始化配置库：库不存在则建库，再幂等建 mcp_datasource_config +
        mcp_config_meta 两张表。返回 (ok, 消息)。CREATE IF NOT EXISTS、绝不 DROP，
        已存在的内容原样不动。"""
        try:
            import mysql.connector
        except ImportError:
            return False, "缺少驱动：pip install mysql-connector-python"
        # 1) 连服务器，库不存在就建
        try:
            srv = self._server_conn()
        except Exception as e:
            return False, (f"无法连接 MySQL 服务器 {self.host}:{self.port}\n"
                           f"{type(e).__name__}: {e}\n\n"
                           f"请确认 MySQL 已启动，且 .env 的 MCP_CONFIG_* 连接信息正确。")
        try:
            cur = srv.cursor()
            cur.execute("SHOW DATABASES LIKE %s", (self.db,))
            db_existed = cur.fetchone() is not None
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.db}` "
                        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            srv.commit()
            cur.close()
            created_db = not db_existed
        except Exception as e:
            return False, f"创建/确认配置库 {self.db} 失败：{type(e).__name__}: {e}"
        finally:
            srv.close()
        # 2) 连到配置库建表（DDL 收敛在 datasources.ensure_mysql_config_schema）
        try:
            conn = self._conn()
        except Exception as e:
            return False, f"无法连接配置库 {self.desc}\n{type(e).__name__}: {e}"
        try:
            ensure_mysql_config_schema(conn)
        except Exception as e:
            return False, f"创建配置表失败：{type(e).__name__}: {e}\n\n可手动执行 docs/mysql_config.sql"
        finally:
            conn.close()
        # 3) 版本号 +1 让 MCP 秒级感知（尽力而为，失败不影响初始化结果）
        bump_mysql_config_version()
        msg = (f"已初始化配置库 {self.host}:{self.port}/{self.db}\n"
               f"  · {'新建了配置库 ' + self.db if created_db else '配置库已存在'}\n"
               f"  · 创建/确认 mcp_datasource_config（数据源配置表）\n"
               f"  · 创建/确认 mcp_config_meta（版本号表）")
        return True, msg


# ------------------------------------------------------------------------------
# 连接测试 + 后端选择
# ------------------------------------------------------------------------------


def test_conn(db_type, url, user, pwd, dbname=""):
    """测试连接。dbname 非空时同时校验该数据库存在（连上但库不存在 → ❌）"""
    host, _, rest = url.partition(":")
    port, _, svc = rest.partition("/")
    port = port or {"mysql": "3306", "dm": "5236", "postgres": "5432",
                    "gaussdb": "5432", "oracle": "1521"}.get(db_type, "")
    try:
        if db_type == "mysql":
            import mysql.connector
            mysql.connector.connect(host=host, port=int(port), user=user,
                                    password=pwd, database=dbname or None,
                                    connection_timeout=8).close()
        elif db_type in ("postgres", "gaussdb"):
            import psycopg2
            psycopg2.connect(host=host, port=int(port), user=user,
                             password=pwd, dbname=dbname or "postgres",
                             connect_timeout=8).close()
        elif db_type == "dm":
            import dmPython
            dmPython.connect(server=host, port=int(port), user=user,
                             password=pwd).close()
        elif db_type == "oracle":
            import oracledb
            oracledb.connect(user=user, password=pwd,
                             dsn=f"{host}:{port}/{svc or 'orcl'}").close()
        else:
            return False, f"暂不支持类型: {db_type}"
        return True, "连接成功（含数据库校验）" if dbname else "连接成功"
    except ImportError:
        return False, f"缺少驱动：pip install {PIP_PKGS.get(db_type, '?')}"
    except Exception as e:
        # 达梦驱动对一切连接失败只回报一句 "returned a result with an exception set"，
        # 单看这句分不出是网络不通还是密码错。翻译逻辑收敛在 db_auto_router 里
        # （MCP 真实连接用的就是它），GUI 不另写一遍——两处各翻必然漂移，
        # 于是出现"测试连接说连不上、MCP 说连上了"这种最难查的分裂。
        if db_type == "dm":
            return False, explain_dm_connect_error(e, url)
        return False, f"{type(e).__name__}: {e}"


def run_async(win, fn):
    """后台线程跑 fn，完成后回主线程弹窗展示结果"""
    win.configure(cursor="watch")

    def work():
        ok, msg = fn()
        win.after(0, lambda: (win.configure(cursor=""),
                              messagebox.showinfo("测试连接", ("✅ " if ok else "❌ ") + msg,
                                                  parent=win)))
    threading.Thread(target=work, daemon=True).start()


def get_backend(forced=None):
    """选择存储后端：--backend 参数优先，否则跟随 .env 的 MCP_CONFIG_MODE。
    返回 (backend, 是否为强制覆盖且与 CFG_MODE 不一致)。"""
    kind = (forced or cfg.CFG_MODE).strip().lower()
    if kind not in ("sqlite", "mysql"):
        kind = "sqlite"
    backend = SqliteBackend() if kind == "sqlite" else MysqlBackend()
    return backend, (bool(forced) and kind != cfg.CFG_MODE)


# ------------------------------------------------------------------------------
# 添加/编辑表单（顶部标注当前写入目标）
# ------------------------------------------------------------------------------


class RecordDialog(tk.Toplevel):
    F = ("Microsoft YaHei UI", 10)   # 统一字体

    def __init__(self, master, title, backend, v=None):
        super().__init__(master)
        # 构造期间先隐藏：Tk 窗口一旦被映射就按默认尺寸画一帧，之后 geometry()
        # 才拉伸到位——用户会看到"小框闪一下再展开"。撤回到 deiconify 之前都
        # 不可见，首次亮相即最终尺寸/位置（主窗口同理）
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.bind("<Escape>", lambda e: self.destroy())
        self.result = None
        v = v or {}

        # ★ v4.2 写入目标标注：明确告知本次修改落在哪个配置库
        banner = tk.Label(
            self, text=f"当前修改写入：{backend.desc}",
            font=("Microsoft YaHei UI", 10, "bold"),
            fg=backend.desc_color, bg="#FFF7E6" if backend.kind == "mysql" else "#EAF3FB",
            anchor="w", padx=16, pady=8)
        banner.pack(fill="x")
        if backend.kind == "mysql":
            tk.Label(self, text="（MCP 正读取此 MySQL 配置库，保存后自动 bump 版本号秒级生效）",
                     font=("Microsoft YaHei UI", 8), fg="#888", anchor="w",
                     padx=16).pack(fill="x")
        else:
            tk.Label(self, text="（MCP 通过文件 mtime 秒级感知变更，保存即生效）",
                     font=("Microsoft YaHei UI", 8), fg="#888", anchor="w",
                     padx=16).pack(fill="x")

        self.var = {k: tk.StringVar(value=str(v.get(k, d)))
                    for k, d in (("dir", "*"), ("name", ""), ("code", ""),
                                 ("type", "mysql"), ("url", ""), ("user", "root"),
                                 ("pwd", ""), ("schema", ""))}
        self.var_def = tk.BooleanVar(value=bool(v.get("default", 0)))
        # v4.6：写操作人工审核开关。默认打勾（安全默认，与老行为一致）；
        # 开发库取消打勾后该数据源的 execute/import_csv/dump_database 全部免确认
        self.var_confirm = tk.BooleanVar(value=bool(v.get("confirm", 1)))
        self.var_show = tk.BooleanVar(value=False)

        frm = ttk.Frame(self, padding=(20, 12, 20, 8))
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        hints = {
            "dir": "留空或 * = 任意目录兜底（单项目推荐）；多项目区分目录才填",
            "code": "必填，小写下划线（如 ess_db）；跨库查询用 db_code.table 引用",
            "url": "地址[:端口]，如 127.0.0.1:3306；Oracle 如 10.0.0.1:1521/orcl",
            "schema": ("PG/GaussDB 填 schema（默认 public）、Oracle/达梦 填 owner"
                       "（默认当前登录用户）；MySQL 留空——库名取 db_code"),
        }
        labels = {"dir": "工作目录", "name": "项目名称", "code": "数据库名称",
                  "url": "地址", "user": "用户名", "schema": "Schema/Owner"}
        order = ["dir", "name", "code", "type", "url", "user", "schema", "pwd"]
        widgets = {}

        def add(label, widget, hint=None):
            r = frm.grid_size()[1]
            ttk.Label(frm, text=label + "：", font=self.F)\
                .grid(row=r, column=0, sticky="e", padx=(0, 10), pady=4)
            widget.grid(row=r, column=1, sticky="ew", pady=4)
            if hint:
                ttk.Label(frm, text=hint, foreground="#888")\
                    .grid(row=r + 1, column=1, sticky="w", pady=(0, 4))
            return r + (2 if hint else 1)

        widgets["type"] = ttk.Combobox(frm, textvariable=self.var["type"],
                                       values=list(VALID_TYPES), state="readonly",
                                       width=15, font=self.F)
        ent_pwd = ttk.Entry(frm, textvariable=self.var["pwd"], width=30,
                            font=self.F, show="*")
        for key in order:
            if key == "type":
                add("数据库类型", widgets["type"])
            elif key == "pwd":
                add("密码", ent_pwd)
                ttk.Checkbutton(frm, text="显示密码", variable=self.var_show,
                                command=lambda: ent_pwd.configure(
                                    show="" if self.var_show.get() else "*"))\
                    .grid(column=1, sticky="w")
            else:
                add(labels[key], ttk.Entry(frm, textvariable=self.var[key],
                                           width=30, font=self.F), hints.get(key))
        ttk.Checkbutton(frm, text="设为该目录默认数据源", variable=self.var_def)\
            .grid(column=1, sticky="w", pady=(8, 0))
        self.chk_confirm = ttk.Checkbutton(frm, text="写操作需人工审核",
                                           variable=self.var_confirm)
        self.chk_confirm.grid(column=1, sticky="w", pady=(8, 0))
        ttk.Label(frm, text="取消打勾 = 该数据源的 execute/import_csv/dump_database 免确认"
                            "（开发库随便造数据用）；审计日志照旧记录每一次写操作",
                  foreground="#888").grid(column=1, sticky="w")
        if backend.kind == "mysql" and not backend.has_write_confirm():
            # 配置库缺列时勾选了也存不进去——禁用并说明，避免"保存成功但不生效"的静默丢失
            self.chk_confirm.configure(state="disabled")
            ttk.Label(frm, text="配置库缺 write_confirm 列，此项暂不生效；补列语句见"
                                " docs/mysql_config.sql v4.6 迁移段",
                      foreground="#C00000").grid(column=1, sticky="w")

        btns = ttk.Frame(self, padding=(20, 4, 20, 16))
        btns.pack(fill="x")
        ttk.Button(btns, text="测试连接", command=self._on_test).pack(side="left")
        ttk.Button(btns, text="取消", width=9, command=self.destroy).pack(side="right")
        ttk.Button(btns, text="确定", width=9, command=self._on_ok)\
            .pack(side="right", padx=(0, 10))

        self.update_idletasks()
        # 弹窗在「主窗口所在的那块屏幕」上居中：多显示器下 tkinter 的 screenwidth
        # 只反映主屏，按它算坐标会把弹窗甩到主屏去（主窗口在副屏时最明显）。
        # 尺寸取表单自然尺寸——由 _center_on 封顶在该屏工作区 90% 以内，
        # 小屏也放得下，不会裁掉底部的「确定/取消」按钮
        area = _target_work_area(master) or (
            0, 0, self.winfo_screenwidth(), self.winfo_screenheight())
        _center_on(self, self.winfo_reqwidth(), self.winfo_reqheight(), area)
        # 尺寸/位置都定了才亮相，消除默认尺寸那一帧的闪烁
        self.deiconify()
        self.grab_set()
        self.wait_window()

    def _collect(self):
        return {"dir": self.var["dir"].get().strip() or "*",
                "name": self.var["name"].get().strip(),
                "code": self.var["code"].get().strip(),
                "type": self.var["type"].get(),
                "url": self.var["url"].get().strip(),
                "user": self.var["user"].get().strip(),
                "schema": self.var["schema"].get().strip(),
                "pwd": self.var["pwd"].get(),
                "default": 1 if self.var_def.get() else 0,
                "confirm": 1 if self.var_confirm.get() else 0}

    def _on_ok(self):
        v = self._collect()
        if not v["code"]:
            messagebox.showwarning("提示", "数据库名称必填", parent=self)
            return
        if not v["url"] or not v["user"]:
            messagebox.showwarning("提示", "地址和用户名必填", parent=self)
            return
        self.result = v
        self.destroy()

    def _on_test(self):
        v = self._collect()
        if not v["url"] or not v["user"]:
            messagebox.showwarning("提示", "测试前请先填好「地址」和「用户名」", parent=self)
            return
        run_async(self, lambda: test_conn(v["type"], v["url"], v["user"],
                                          v["pwd"], v["code"]))


# ------------------------------------------------------------------------------
# 主窗口
# ------------------------------------------------------------------------------


class App(tk.Tk):
    HEADS = ("目录", "项目名称", "数据库名称", "类型", "地址", "用户", "Schema/Owner", "默认", "审核")
    # 列宽 = [最小宽度, 权重]：窗口拉伸时按权重分配多余空间
    COLS = {"目录":       (280, 5),
            "项目名称":   (120, 1),
            "数据库名称": (120, 1),
            "类型":       (70,  0),
            "地址":       (150, 2),
            "用户":       (90,  1),
            "Schema/Owner": (110, 1),
            "默认":       (50,  0),
            "审核":       (70,  0)}

    def __init__(self, backend):
        super().__init__()
        # 构造期间先隐藏，理由同 RecordDialog：否则 Tk 先按默认尺寸映射一帧，
        # 再被 geometry() 拉伸，启动时会看到"小框闪一下再展开"
        self.withdraw()
        self.backend = backend
        self.title(f"数据源管理 · db-auto-router 【{backend.desc}】")
        self._rows = []
        self._first = not os.path.exists(backend.path) if backend.kind == "sqlite" else False

        # 双击启动即占所在屏幕 90%（宽）× 80%（高，给任务栏留空间），并居中。
        # 多显示器：按鼠标所在屏的工作区算——否则按主屏尺寸/坐标开会把小屏
        # 或副屏上的窗口开错地方（用户双击图标在哪块屏，就该在哪块屏打开）
        self.update_idletasks()
        area = _target_work_area() or (
            0, 0, self.winfo_screenwidth(), self.winfo_screenheight())
        left, top, aw, ah = area
        _center_on(self, int(aw * 0.9), int(ah * 0.8), area)
        self.minsize(int(aw * 0.5), int(ah * 0.45))
        # 尺寸/位置/最小尺寸都定了才亮相：消除启动时默认尺寸小框先闪一帧
        self.deiconify()

        if sys.platform == "win32":
            try:
                import tkinter.font as tkfont
                for f in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
                    tkfont.nametofont(f).configure(family="Microsoft YaHei UI", size=9)
            except Exception:
                pass
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", rowheight=32, font=("Microsoft YaHei UI", 10),
                        background="#FFFFFF", fieldbackground="#FFFFFF")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"),
                        background="#E8EEF7", foreground="#1F3864", relief="flat")
        style.map("Treeview", background=[("selected", "#2F6FDB")],
                  foreground=[("selected", "#FFFFFF")])

        bar = ttk.Frame(self, padding=12)
        bar.pack(fill="x")
        for text, cmd in (("刷新数据源", self.on_refresh), ("添加", self.on_add),
                          ("编辑", self.on_edit), ("删除", self.on_delete),
                          ("测试连接", self.on_test), ("关闭", self.destroy)):
            ttk.Button(bar, text=text, command=cmd, width=12).pack(side="left", padx=3)

        frame = ttk.Frame(self, padding=(12, 0, 12, 0))
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=self.HEADS, show="headings",
                                 selectmode="browse")
        for col in self.HEADS:
            minw, _weight = self.COLS[col]
            self.tree.heading(col, text=col)
            # stretch=True + 随后按权重分配：窗口变化时列宽自适应
            self.tree.column(col, width=minw, minwidth=minw, anchor="w", stretch=True)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.on_edit())
        self.tree.bind("<Configure>", self._on_tree_resize)
        # 隔行浅蓝底（zebra）+ 默认数据源整行绿字 + 免人工审核整行橙字（v4.6）
        self.tree.tag_configure("zebra", background="#EBF1F9")
        self.tree.tag_configure("default_row", foreground="#1E7B34")
        self.tree.tag_configure("no_confirm", foreground="#B35400")

        self.status = ttk.Label(self, text="", padding=12, foreground="#555")
        self.status.pack(fill="x", side="bottom")
        # 窗口先行：配置库读取放后台线程。
        # 原来 main() 探测一次 + 这里 reload() 一次，两轮 MySQL 往返都发生在
        # mainloop() 之前，窗口要等它们跑完才出现——这是"半天才弹出来"的直接原因。
        self.reload(async_=True)

    def _on_tree_resize(self, event):
        """表格宽度变化时按权重重新分配列宽（字段自适应布局）。
        目录列吃大头（长路径），地址其次，类型/默认列固定不参与伸缩。"""
        total = event.width - 24          # 扣除垂直滚动条宽度
        weights = [self.COLS[c][1] for c in self.HEADS]
        mins = [self.COLS[c][0] for c in self.HEADS]
        fixed_total = sum(mins)
        extra = total - fixed_total
        if extra > 0:
            wsum = sum(weights)
            if wsum > 0:
                mins = [m + extra * wt // wsum for m, wt in zip(mins, weights)]
        # 防抖：与当前列宽相同则跳过重设
        cur = [self.tree.column(c)["width"] for c in self.HEADS]
        if cur != mins:
            for col, wd in zip(self.HEADS, mins):
                self.tree.column(col, width=wd)

    def reload(self, async_=False):
        """读取配置源并刷新列表。

        async_=True（仅启动路径用）：把 MySQL/SQLite 读取丢到后台线程，窗口先出来，
        数据到了再由 self.after(0, ...) 回到主线程刷 UI。
        async_=False（刷新数据源 / 增删改后）：仍在调用方线程同步执行，
        用户刚做完操作，理所应当立刻看到结果。
        """
        if async_:
            self.status.configure(foreground="#555",
                                  text=f"正在连接 {self.backend.desc} …")
            threading.Thread(target=self._reload_worker, daemon=True).start()
        else:
            self._reload_worker()

    def _reload_worker(self):
        """后台线程：只碰后端（连库、读列表、取指纹），不碰任何 tkinter 控件。"""
        try:
            rows = self.backend.fetch_all()
            err = None
        except Exception as e:
            rows, err = [], e
        # v4.7：mysql 失败时归类——是「配置表/库还没建」还是「连不上」，决定弹什么框
        problem = None
        if err is not None and self.backend.kind == "mysql":
            try:
                problem = self.backend.classify_problem(err)
            except Exception:
                problem = None
        try:
            fp_text = self.backend.fingerprint_text()
        except Exception:
            fp_text = ""
        if threading.current_thread() is threading.main_thread():
            self._apply_rows(rows, err, fp_text, problem)
        else:
            # tkinter 非线程安全：UI 更新必须回主线程
            self.after(0, lambda: self._apply_rows(rows, err, fp_text, problem))

    def _apply_rows(self, rows, err, fp_text, problem=None):
        """主线程：把读到的数据渲染进表格。problem 见 _reload_worker（mysql 错误归类）。"""
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        if err is not None:
            self._rows = []
            # v4.7：区分「配置表/库还没建」与「连不上」——前者一键初始化，
            # 后者才提示检查连接。小白第一次用多半是前者。
            if problem == "no_table":
                self._prompt_init("配置库里还没有数据源配置表 mcp_datasource_config")
                return
            if problem == "no_database":
                self._prompt_init(f"配置库 {self.backend.db} 还不存在")
                return
            self.status.configure(
                text=f"❌ 读取 {self.backend.desc} 失败: {type(err).__name__}: {err}\n"
                     f"（可用 --backend sqlite|mysql 切换后端）", foreground="#C00000")
            if self.backend.kind == "mysql":
                # 连不上时给可行动的选项，而不是只留一行红字——窗口已经在，不会一片空白
                messagebox.showerror(
                    "无法连接 MySQL 配置库",
                    f"{self.backend.desc}\n\n{type(err).__name__}: {err}\n\n"
                    "可选操作：\n"
                    "  · 确认 MySQL 服务已启动\n"
                    "  · 检查 .env 的 MCP_CONFIG_* 连接信息（账号/密码/库名）\n"
                    "  · 用 --backend sqlite 编辑本地文件模式")
            return
        self._rows = rows
        for i, (d, name, code, t, url, user, schema, isdef, wconf) in enumerate(self._rows):
            tags = []
            if i % 2 == 0:
                tags.append("zebra")
            if isdef == 1:
                tags.append("default_row")
            if wconf != 1:
                # 免审核行整行橙色：扫一眼就知道哪个库写操作不弹窗
                tags.append("no_confirm")
            self.tree.insert("", "end", iid=str(i), tags=tags,
                             values=(d if d not in ("*", "") else "*（任意目录兜底）",
                                     name, code, t, url, user, schema or "",
                                     "✓" if isdef == 1 else "",
                                     "✓" if wconf == 1 else "✗ 免"))
        self.status.configure(
            foreground="#555",
            text=f"共 {len(self._rows)} 条 ｜ {fp_text} ｜ 双击行编辑 ｜ "
                 f"保存后 MCP 下一次查询自动生效（秒级），无需调 refresh_datasources ｜ "
                 f"MCP 版本 v{cfg.MCP_SERVER_INFO['version']}")
        if self._first:
            self._first = False
            messagebox.showinfo("提示", f"已创建配置库：\n{self.backend.path}\n\n"
                                       "当前为空，点击「添加」注册第一个数据源。", parent=self)

    def _prompt_init(self, reason):
        """mysql 模式：配置表/库缺失，弹框问是否一键初始化（小白向）。"""
        self.status.configure(text=f"⚠ {reason}，等待是否初始化…", foreground="#B35400")
        go = messagebox.askyesno(
            "初始化配置表",
            f"{self.backend.desc}\n\n{reason}。\n\n"
            "是否现在一键初始化？将自动：\n"
            "  · 创建配置库（若不存在）\n"
            "  · 创建 mcp_datasource_config（数据源配置表）\n"
            "  · 创建 mcp_config_meta（版本号表）\n"
            "  · 已存在的内容不会被改动或删除\n\n"
            "选「是」立即初始化；选「否」可稍后手动执行 docs/mysql_config.sql。",
            icon="question", parent=self)
        if not go:
            self.status.configure(
                text="已跳过初始化 ｜ 稍后可重新打开本工具，或手动执行 docs/mysql_config.sql",
                foreground="#555")
            return
        self._do_init()

    def _do_init(self):
        """后台线程执行初始化，完成后回主线程提示并重载列表。"""
        self.status.configure(text="正在初始化配置表，请稍候…", foreground="#B35400")
        self.configure(cursor="watch")

        def work():
            ok, msg = self.backend.init_schema()

            def done():
                self.configure(cursor="")
                if ok:
                    messagebox.showinfo(
                        "初始化完成",
                        f"{msg}\n\n配置表已就绪，点击「添加」注册第一个数据源。",
                        parent=self)
                    self.reload(async_=True)
                else:
                    self.status.configure(text=f"❌ 初始化失败：{msg}", foreground="#C00000")
                    messagebox.showerror("初始化失败", msg, parent=self)

            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def on_refresh(self):
        """刷新数据源：重读配置源全量列表 + 显示变更指纹（mtime/版本号）。
        MCP 侧靠指纹自动感知变更，此操作用于人工核对当前状态。"""
        prev_fp = self.backend.fingerprint()
        self.reload()
        new_fp = self.backend.fingerprint()
        changed = (prev_fp is not None and new_fp is not None and prev_fp != new_fp)
        fp_text = self.backend.fingerprint_text()
        self.status.configure(
            text=f"已重读 {self.backend.desc}，共 {len(self._rows)} 条 ｜ "
                 f"{fp_text} ｜ "
                 f"MCP 侧自动感知变更（{('刚检测到新变更，' if changed else '')}"
                 f"下一次查询即为新配置） ｜ MCP 版本 v{cfg.MCP_SERVER_INFO['version']}")
        messagebox.showinfo(
            "刷新数据源完成",
            f"已重新读取：{self.backend.desc}\n\n"
            f"数据源数量：{len(self._rows)} 条\n"
            f"变更指纹：{fp_text}\n\n"
            + ("⚠ 刚检测到外部修改，MCP 下一次查询即为新配置。\n\n"
               if changed else
               "MCP 侧自动感知变更（SQLite 文件 mtime / MySQL 配置版本号），\n"
               "改完配置后的下一次查询即为新配置，无需调 refresh_datasources。\n\n"))

    def _sel(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一条数据源", parent=self)
            return None
        return self._rows[int(sel[0])]

    def on_add(self):
        vals = None
        while True:
            dlg = RecordDialog(self, "添加数据源", self.backend, vals)
            if not dlg.result:
                return
            ok, msg = self.backend.save_row(None, dlg.result)
            if ok:
                self.backend.bump_version()
                return self.reload()
            messagebox.showerror("添加失败", msg, parent=self)
            vals = dlg.result

    def on_edit(self):
        row = self._sel()
        if not row:
            return
        d, name, code, t, url, user, schema, isdef, wconf = row
        base = {"dir": d, "name": name, "code": code, "type": t, "url": url,
                "user": user, "schema": schema or "",
                "pwd": self.backend.fetch_pwd(d, code), "default": isdef,
                "confirm": wconf}
        while True:
            dlg = RecordDialog(self, f"编辑数据源：{code}", self.backend, base)
            if not dlg.result:
                return
            ok, msg = self.backend.save_row((d, code), dlg.result)
            if ok:
                self.backend.bump_version()
                return self.reload()
            messagebox.showerror("保存失败", msg, parent=self)
            base = dlg.result

    def on_delete(self):
        row = self._sel()
        if not row:
            return
        d, name, code, t, url, user, schema, isdef, wconf = row
        if messagebox.askyesno("确认删除",
                               f"确定删除 {code}（{t} @ {url}）吗？\n\n"
                               f"写入目标：{self.backend.desc}",
                               parent=self, icon="warning"):
            self.backend.delete_row(d, code)
            self.backend.bump_version()
            self.reload()

    def on_test(self):
        row = self._sel()
        if not row:
            return
        d, name, code, t, url, user, schema, isdef, wconf = row
        run_async(self, lambda: test_conn(t, url, user,
                                          self.backend.fetch_pwd(d, code), code))


def main():
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="数据源管理 GUI（v4.6 双后端）")
    ap.add_argument("--backend", choices=("sqlite", "mysql"), default=None,
                    help="强制指定编辑的后端（默认跟随 .env 的 MCP_CONFIG_MODE）")
    args = ap.parse_args()

    backend, mismatch = get_backend(args.backend)
    # v4.7：删掉这里的 mysql 连通性预探测。
    # 它和 App.__init__ 里的 reload() 会连查两次配置库，且都堵在 mainloop() 之前，
    # 窗口要等两轮 MySQL 往返才出现。现在连接与读取全部走 App 的后台线程，
    # 连不上时由 App._apply_rows() 弹同样的提示（窗口已在，不再一片空白后直接退出）。

    if mismatch:
        # --backend 强制值与 .env 模式不一致：MCP 读的不是这个库，改动不生效
        root = tk.Tk(); root.withdraw()
        go_on = messagebox.askyesno(
            "模式提醒",
            f"当前 .env 为 MCP_CONFIG_MODE={cfg.CFG_MODE}，MCP 读的是 {cfg.CFG_MODE} 配置库；\n"
            f"本次通过 --backend 强制编辑 {backend.kind}，改动不会生效。\n\n仍要继续吗？",
            icon="warning")
        root.destroy()
        if not go_on:
            sys.exit(0)

    app = App(backend)
    app.mainloop()


if __name__ == "__main__":
    main()
