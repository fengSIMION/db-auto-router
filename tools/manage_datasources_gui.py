# -*- coding: utf-8 -*-
"""数据源维护可视化小工具（v4.9 双后端版）

用法：python tools/manage_datasources_gui.py 或双击 start_datasource_manager.bat
可加参数强制后端：--backend sqlite 或 --backend mysql（默认跟随 .env 的 MCP_CONFIG_MODE）

v4.9 变更：
- 窗口标题 / 弹窗横幅 / 状态栏改显示短描述（新增 backend.desc_short）：
  SQLite 只显示「SQLite 数据库：datasources.db」，不再顶一长条全路径——标题栏和
  横幅是单行标签，全路径只会被截断。全路径 desc 原样留给诊断类弹窗（读取失败、
  无法连接、一键初始化询问、刷新完成、删除确认），那些会换行，路径越完整越容易
  定位"连的到底是哪个库"
- 保存前预检两条规则，命中就拒绝保存并把原因说清楚（GUI 与 CLI 共用同一份实现，
  见 db_auto_router.datasources.check_datasource_conflict）：
    1) 同一工作目录下，数据库名称不能重复
    2) 同一目录只能有一个默认数据源
  第 2 条以前完全没有校验，而 MCP 取默认的口径是 is_default DESC, db_code——
  多个默认时字母序第一个胜出，用户在 B 上勾"设为默认"、界面也打了勾，实际生效的
  却是 A，且不报错。数据库主键继续兜底并发/外部改库的场景
- 命令行 template 子命令：示例行原本写死 is_default=1，'*' 目录已有默认时会插出
  同一目录两个默认；撞了自动退成非默认并说明

v4.8 变更：
- 列表不再展示 Schema/Owner 列：它对绝大多数 MySQL 数据源是空值，却常年占着
  一列的宽度。字段本身不动，「编辑」弹窗里照样能改（PG/GaussDB/Oracle/达梦才填）
- 列宽改为「内容自适应」：按当前数据实测每一列的文本像素宽，窗口多余空间再按
  权重分给可伸缩列；窗口不够时由水平滚动条兜底，不再把列挤成看不见
- 修复「拖不动列宽」：旧实现在每次 <Configure> 里把按权重算好的宽度强写回每一列，
  用户手工拖出来的宽度会被下一次事件抹掉。现在手工拖过的列会被记住，不再自动改，
  点「重置列宽」可恢复自适应
- 新增「复制」按钮：复制选中数据源，工作目录名后加 _copy（数据库名称不变）——
  副本是"给另一个目录用的同一套连接参数"
- 同目录同色：列表按 dir_path 排序，同项目的数据源本就挨在一起，一个浅色底就是
  一个项目分组，一眼看出哪几条属于同一个项目。只有一个目录时（全是 * 兜底）
  着色没有区分意义，退回原来的隔行浅蓝
- 修复「编辑/复制弹窗里长路径看不全」：所有输入框原为固定 width=30，一条 63 字符的
  目录路径有一半以上挡在框外。改为按当前取值实测像素宽设定（封顶 72 字符），
  弹窗整体仍受 90% 屏宽上限约束

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
# datasources / directconn 故意不放在模块顶层导入：datasources → workspace → asyncio
# （连带 ssl / pywin32）实测约 210ms，directconn 约 150ms，而启动路径只用得到 cfg。
# 改为首次使用时惰性导入（下面各用点处 from ... import ...）——窗口因此不必等它们，
# 这笔钱挪到后台线程的数据加载里付，用户先看到窗口。
import tkinter as tk  # noqa: E402
from tkinter import ttk, messagebox  # noqa: E402
from tkinter import font as tkfont  # noqa: E402  列宽内容实测：measure 文本像素宽

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
    def desc_short(self):
        """窗口标题/弹窗横幅/状态栏用的短描述：只留文件名，不带全路径。

        全路径留给诊断类提示（读取失败、刷新完成、删除确认、一键初始化）——那些是
        弹窗、会换行，路径越完整越容易定位"到底连的是哪个库"（换机器拷贝文件夹后
        尤其如此）。而标题栏和横幅是单行标签，全路径只会被截断，看着还是一长条。
        """
        return f"SQLite 数据库：{Path(self.path).name}"

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
        from db_auto_router.datasources import upgrade_sqlite_config_schema
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
            # v4.9：保存前预检（目录+名称唯一、同目录默认唯一）。规则在
            # datasources.check_datasource_conflict 只写一份，GUI 与 CLI 共用。
            # 走已打开的连接，不额外开连接；主键约束继续兜底
            from db_auto_router.datasources import check_datasource_conflict
            err = check_datasource_conflict(
                lambda sql, p: c.execute(sql, p).fetchall(), orig, v)
            if err:
                return False, err
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
    def desc_short(self):
        # MySQL 的 desc 本来就是"主机:端口/库名"，够短，不需要再缩
        return self.desc

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

    def has_column(self, column, conn=None):
        """探测配置库是否已有某一列。

        conn 由调用方传入时复用该连接（fetch_all 用）：原来 has_db_schema() 与
        has_write_confirm() 各自 _conn() 开一条、主查询再开一条，一次列表读要开
        三条 MySQL 连接，而类级 _columns 缓存只在进程内有效——每次双击都是新进程，
        缓存对启动路径毫无帮助。不传 conn 时才自开一条，连接生命周期归调用方。
        """
        if column in MysqlBackend._columns:
            return MysqlBackend._columns[column]
        own = conn is None
        if own:
            try:
                conn = self._conn()
            except Exception:
                MysqlBackend._columns[column] = False
                return False
        try:
            # 与 MCP 侧 datasources.mysql_config_has_column 同口径（共用一份实现，
            # 不在这里内联 information_schema 查询，否则两处探测会漂移）
            from db_auto_router.datasources import mysql_config_has_column
            MysqlBackend._columns[column] = mysql_config_has_column(conn, column)
        except Exception:
            MysqlBackend._columns[column] = False
        finally:
            if own:
                conn.close()
        return MysqlBackend._columns[column]

    def has_db_schema(self):
        return self.has_column("db_schema")

    def has_write_confirm(self):
        return self.has_column("write_confirm")

    def fetch_all(self):
        # 一条连接走完列探测 + 列表查询：原来两次 has_column 各开一条、主查询再开
        # 一条，共三条连接（见 has_column 注释）。
        c = self._conn()
        try:
            has_schema = self.has_column("db_schema", conn=c)
            has_confirm = self.has_column("write_confirm", conn=c)
            cur = c.cursor(dictionary=True)
            # 缺列时按旧列清单查，字典游标 + .get 自动补空，无需按位置插占位
            cols = ["dir_path", "db_name", "db_code", "db_type", "conn_url", "db_user"]
            if has_schema:
                cols.append("db_schema")
            # 列顺序与 SqliteBackend 保持一致（is_default 在前、write_confirm 在后），
            # 行形状（9 字段）以下方元组构造为准——位置解包的消费方
            # （_apply_rows / on_edit / on_delete / on_test）都按这个顺序取
            cols.append("is_default")
            if has_confirm:
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
            # v4.9：保存前预检，同 SqliteBackend。占位符换成就近的 %s，
            # 规则本身仍在 datasources.check_datasource_conflict 那一份里
            from db_auto_router.datasources import check_datasource_conflict

            def _fetch(sql, params):
                cur.execute(sql, params)
                return cur.fetchall()

            err = check_datasource_conflict(_fetch, orig, v, ph="%s")
            if err:
                return False, err
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
        from db_auto_router.datasources import bump_mysql_config_version
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
            from db_auto_router.datasources import ensure_mysql_config_schema
            ensure_mysql_config_schema(conn)
        except Exception as e:
            return False, f"创建配置表失败：{type(e).__name__}: {e}\n\n可手动执行 docs/mysql_config.sql"
        finally:
            conn.close()
        # 3) 版本号 +1 让 MCP 秒级感知（尽力而为，失败不影响初始化结果）
        from db_auto_router.datasources import bump_mysql_config_version
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
            from db_auto_router.directconn import explain_dm_connect_error
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
            self, text=f"当前修改写入：{backend.desc_short}",
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

        # 输入框宽度按当前取值算：编辑一条 63 字符的路径时，固定 width=30 会把一大半
        # 挡在框外，只能按方向键一点点蹭过去看（列表里早就能完整显示，弹窗反而看不清）。
        # ttk 的 Entry width 以字符 '0' 的宽度为单位，故用像素宽换算。封顶 72 字符，
        # 更长的交给方向键 / Home-End；弹窗整体仍受 _center_on 的 90% 屏高屏宽上限约束
        f_entry = tkfont.Font(font=self.F)
        unit = f_entry.measure("0") or 1

        def entry_w(val):
            return max(30, min(72, int((f_entry.measure(str(val)) + 28) / unit) + 1))

        widgets["type"] = ttk.Combobox(frm, textvariable=self.var["type"],
                                       values=list(VALID_TYPES), state="readonly",
                                       width=15, font=self.F)
        ent_pwd = ttk.Entry(frm, textvariable=self.var["pwd"],
                            width=entry_w(self.var["pwd"].get()),
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
                                           width=entry_w(self.var[key].get()),
                                           font=self.F), hints.get(key))
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
    # 列表列。v4.8 起不再展示 Schema/Owner：它只对 PG/GaussDB/Oracle/达梦有意义，
    # MySQL 数据源这一列恒为空，却常年占着一列宽度。字段本身没动，「编辑」弹窗
    # 里照样能改——只是不再占列表的地方。
    HEADS = ("目录", "项目名称", "数据库名称", "类型", "地址", "用户", "默认", "审核")
    # 列宽 = [最小宽度, 伸缩权重]：宽度先按内容实测（见 _natural_width），窗口多余的
    # 空间再按权重分给权重>0 的列；权重 0 的列（类型/默认/审核）恒为内容宽度，不伸缩
    COLS = {"目录":       (140, 5),
            "项目名称":   (90,  1),
            "数据库名称": (110, 1),
            "类型":       (70,  0),
            "地址":       (130, 2),
            "用户":       (80,  1),
            "默认":       (50,  0),
            "审核":       (70,  0)}
    # 内容实测宽度的上限。这个帽子只是"别让一列把表格撑爆"的保险丝，不是截断用的——
    # 真实项目路径普遍 50~70 字符（开启 DPI awareness 后约 500~700px），所以目录列
    # 放到 1000px 仍装得下绝大部分真实路径，超出的才是真异常，交给水平滚动条与手工拖宽。
    # 曾经给目录列设 620px，结果两条 63 字符的路径被切——"内容自适应"却自带截断，
    # 等于没做。测试必须走 main() 的真实路径（带 DPI awareness），否则测出来的
    # 文本像素宽比真实渲染窄一大截，会漏掉这类问题
    MAXW = {"目录": 1000, "项目名称": 300, "数据库名称": 300, "类型": 100,
            "地址": 400, "用户": 200, "默认": 60, "审核": 90}
    # 同目录同色用的浅色底（低饱和：黑字/默认行绿字/免审核橙字压在上面都读得清）。
    # 第一个就是原来的隔行蓝，单目录退回 zebra 时观感和之前完全一致。
    # 颜色数量只是"几个项目后开始循环"，相邻两组异色靠 _dir_colors 的顺序分配保证，
    # 与用掉几个颜色无关
    DIR_COLORS = ("#EBF1F9",   # 浅蓝
                  "#F1EBF9",   # 浅紫
                  "#EAF6EE",   # 浅绿
                  "#FCF1DC",   # 浅黄
                  "#F7EAEA",   # 浅红
                  "#E3F0EF",   # 浅青
                  "#EFEFEF")   # 浅灰
    # 内容实测时"文本之外的富余"候选值，从宽到紧。_autofit 取第一个能让总宽塞进可视区的，
    # 所以列宽自适应内容、又不会因为太松而挤出水平滚动条。ttk 单元格内边距实测约 3px，
    # 下限取 6 仍有余量——调小 pad 只会让列紧一点，不会截断文本
    PAD_CHOICES = (24, 18, 14, 10, 8, 6)

    def __init__(self, backend):
        super().__init__()
        # 构造期间先隐藏，理由同 RecordDialog：否则 Tk 先按默认尺寸映射一帧，
        # 再被 geometry() 拉伸，启动时会看到"小框闪一下再展开"
        self.withdraw()
        self.backend = backend
        self.title(f"数据源管理 · db-auto-router 【{backend.desc_short}】")
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

        # 列宽管理状态（v4.8 内容自适应）。三份字典分工：
        #   _natural     —— 本次数据算出来的内容宽度，基准值
        #   _user_widths —— 用户手工拖过的列宽，拖过就不再自动改，直到「重置列宽」
        #   _applied     —— 代码上一次写入的列宽，用于识别"这次变宽是用户拖的"
        self._natural = {}
        self._user_widths = {}
        self._applied = {}
        self._display = []          # 表格里实际显示的值（列宽实测的输入）
        self._laid_out = False      # 是否已完成过一次布局（否则无法区分拖动与初值）
        self._border = 0            # ttk 边框占掉的像素，实测得到，见 _on_xscroll
        self._w_cache = {}          # 文本像素宽记忆，见 _text_width
        self._f_cell = tkfont.Font(font=("Microsoft YaHei UI", 10))
        self._f_head = tkfont.Font(font=("Microsoft YaHei UI", 10, "bold"))

        bar = ttk.Frame(self, padding=12)
        bar.pack(fill="x")
        for text, cmd in (("刷新数据源", self.on_refresh), ("复制", self.on_copy),
                          ("添加", self.on_add), ("编辑", self.on_edit),
                          ("删除", self.on_delete), ("测试连接", self.on_test),
                          ("重置列宽", self.on_reset_cols), ("关闭", self.destroy)):
            ttk.Button(bar, text=text, command=cmd, width=12).pack(side="left", padx=3)

        frame = ttk.Frame(self, padding=(12, 0, 12, 0))
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=self.HEADS, show="headings",
                                 selectmode="browse")
        for col in self.HEADS:
            minw, _weight = self.COLS[col]
            self.tree.heading(col, text=col)
            # stretch=False：列宽完全交给 _layout_columns 管（内容实测 + 权重分配 +
            # 记住手工拖拽）。原来 stretch=True 会让 ttk 自行拉伸，与按权重算出来的
            # 宽度互相打架，也是"拖完又被拽回去"的成因之一
            self.tree.column(col, width=minw, minwidth=minw, anchor="w",
                             stretch=False)
            self._applied[col] = minw
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        # 水平滚动条：列宽按内容算，窗口装不下时就该能横向滚到，而不是把列挤没
        self.hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self._hsb_shown = False
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=self._on_xscroll)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.on_edit())
        self.tree.bind("<Configure>", self._on_tree_resize)
        # 手工拖列：松手时拿当前宽度和"代码上次写入的宽度"对比，变了的就是用户拖的
        self.tree.bind("<ButtonRelease-1>", self._on_click_release)
        # 隔行浅蓝底（zebra）+ 默认数据源整行绿字 + 免人工审核整行橙字（v4.6）
        self.tree.tag_configure("zebra", background="#EBF1F9")
        self.tree.tag_configure("default_row", foreground="#1E7B34")
        self.tree.tag_configure("no_confirm", foreground="#B35400")
        # 同目录同色（v4.8）：一个浅色底 = 一个项目分组。全部预注册，用不到的无害
        for _i, _c in enumerate(self.DIR_COLORS):
            self.tree.tag_configure(f"dir{_i}", background=_c)

        self.status = ttk.Label(self, text="", padding=12, foreground="#555")
        self.status.pack(fill="x", side="bottom")
        # 窗口先行：配置库读取放后台线程。
        # 原来 main() 探测一次 + 这里 reload() 一次，两轮 MySQL 往返都发生在
        # mainloop() 之前，窗口要等它们跑完才出现——这是"半天才弹出来"的直接原因。
        self.reload(async_=True)

    # ---- v4.8 列宽：内容自适应 + 手工拖拽不被抹掉 ----

    @staticmethod
    def _dir_key(d):
        """目录分组键：'*' 与空串是同一个意思（任意目录兜底），必须归成同一组。

        否则配置里同时有这两行时，排序后它们挨在一起却是两个颜色，看起来像两个项目。
        """
        return "*" if (d or "").strip() in ("*", "") else d

    def _dir_colors(self, dir_keys):
        """给每个不同目录分配一个浅色底；相邻目录颜色必不相同。

        按出现顺序依次取下一个颜色（循环）。列表按 dir_path 排过序，所以相邻两组
        一定异色——分组边界一眼能看出来。同一个目录永远拿同一个颜色，刷新、滚动、
        增删行之后都不会跳色（这点比"按行号取色"重要：按行号的话删一行后半段的颜色
        全变，刚才记住的分组就乱了）。
        """
        out = {}
        prev = -1
        for k in dict.fromkeys(dir_keys):        # dict.fromkeys 保序去重
            i = (prev + 1) % len(self.DIR_COLORS)
            out[k] = i
            prev = i
        return out

    def _text_width(self, text, bold=False):
        """文本像素宽（列宽实测用）。测量失败时按字符数粗估，不至于让布局崩掉。

        带记忆：类型/用户/审核这几列的取值高度重复，几百行配置实测时能省掉大量
        重复的 measure() Tcl 往返。路径列取值基本不重复，命中不了缓存，也无妨。
        """
        key = (text, bold)
        hit = self._w_cache.get(key)
        if hit is not None:
            return hit
        f = self._f_head if bold else self._f_cell
        try:
            w = f.measure(text)
        except Exception:
            w = len(str(text)) * 8
        self._w_cache[key] = w
        return w

    def _natural_width(self, idx, col, pad):
        """一列的内容宽度 = max(表头宽, 各行取值宽) + pad，夹在 [最小, 上限] 之间。

        pad 是"文本之外的富余"，由 _autofit 按能否塞进可视区挑选（见 PAD_CHOICES）。
        真正决定放不放得下内容的是 ttk 那约 3px 的单元格内边距，pad 只要 ≥3 就不会截断，
        所以这里怎么调都安全——调小只是列变紧一点。
        """
        w = self._text_width(col, bold=True) + pad + 8   # 表头文字 + 排序箭头
        for row in self._display:
            w = max(w, self._text_width(row[idx]) + pad)
        minw, _weight = self.COLS[col]
        return max(minw, min(w, self.MAXW[col]))

    def _autofit(self):
        """按当前数据重算每一列的内容宽度（用户手工拖过的列除外），再排布一次。

        富余边距 pad 从宽到紧逐个试，取第一个能让总宽塞进可视区的：内容本来就装得下时
        用最松的（列好看），装不下时收紧空白而不是切文本——所以永远不会出现"自适应
        却把内容截断"。收到最紧凑仍塞不下（真·超宽内容），保留最紧凑值，超出的部分
        交给水平滚动条，或用户手工把列拖宽。
        """
        trial = None
        for pad in self.PAD_CHOICES:
            trial = {c: self._natural_width(i, c, pad)
                     for i, c in enumerate(self.HEADS) if c not in self._user_widths}
            total = sum(self._user_widths.get(c, trial.get(c, self.COLS[c][0]))
                        for c in self.HEADS)
            if total <= self.tree.winfo_width() - self._border:
                break
        self._natural = trial or {}
        self._layout_columns()

    def _on_tree_resize(self, event):
        """表格尺寸变化时重算列宽（内容自适应布局）。

        与旧实现的区别：旧代码在每次 <Configure> 里把按权重分配的宽度强写回每一列，
        用户手工拖出来的宽度会被下一次事件直接抹掉——这就是"拖不动列宽"的直接原因。
        现在以 _natural（内容实测）/ _user_widths（用户拖过）为基准，只在有剩余空间时
        把余量按权重分给可伸缩列；空间不足时不动宽度，交给水平滚动条。
        """
        self._layout_columns()

    def _layout_columns(self):
        """内容宽度为基准 + 余量按权重分给可伸缩列，正好填满表格。

        宽度只会"长"不会被压到内容宽度以下：压不下去就横向滚，这是用户能看见长目录的
        前提。整数除法的余数补给最后一个可伸缩列，避免右侧留一条空带。
        """
        # winfo_width() 含 ttk 边框，直接拿它当基准会把列宽填到溢出边框那几个像素，
        # 白闪一条用不上的水平滚动条。减掉实测出来的 _border 才是真正的可视宽度。
        avail = self.tree.winfo_width() - self._border
        if avail <= 1:                    # 窗口还没映射完，等 <Configure> 再来
            return
        widths = []
        for col in self.HEADS:
            if col in self._user_widths:
                widths.append(self._user_widths[col])
            else:
                widths.append(self._natural.get(col, self.COLS[col][0]))
        extra = avail - sum(widths)
        if extra > 0:
            wsum = sum(self.COLS[c][1] for c in self.HEADS)
            if wsum > 0:
                flex = [i for i, c in enumerate(self.HEADS)
                        if self.COLS[c][1] > 0 and c not in self._user_widths]
                if flex:
                    fsum = sum(self.COLS[self.HEADS[i]][1] for i in flex)
                    given = 0
                    for i in flex[:-1]:
                        add = extra * self.COLS[self.HEADS[i]][1] // fsum
                        widths[i] += add
                        given += add
                    widths[flex[-1]] += extra - given
        # _applied 要先于"相同就跳过"的判断更新：否则一次不写回的布局会让
        # _applied 停留在旧值，用户下次点击就被误判成拖拽
        for col, wd in zip(self.HEADS, widths):
            self._applied[col] = wd
        self._laid_out = True
        if [self.tree.column(c)["width"] for c in self.HEADS] == widths:
            return
        for col, wd in zip(self.HEADS, widths):
            # 双保险取整：widths 全链路是整数运算，但 _user_widths / _natural 是外部
            # 写入的字典，混进浮点会让 Tcl 抛 "expected integer"，整个布局中断在半路
            self.tree.column(col, width=int(round(wd)))

    def _on_click_release(self, event):
        """松手时识别手工拖列：与代码上次写入的宽度不同的列记下来，此后不再自动改。"""
        if not self._laid_out:
            return                     # 还没排布过，当前宽度只是初始最小值，不算用户拖的
        for col in self.HEADS:
            cur = self.tree.column(col)["width"]
            if self._applied.get(col) != cur:
                self._user_widths[col] = cur

    def _on_xscroll(self, first, last):
        """水平滚动条回调：同步滑块，实测边框宽度，并按需显隐滚动条本身。

        显隐只改 frame 内的纵向排布，不影响 tree 的宽度，所以不会触发新的 <Configure>
        回来重算列宽（没有重入循环）。pack 操作用 after_idle 推迟：这个函数是在滚动条
        更新的 C 回调里被叫到的，直接改布局有重入风险。

        为什么要实测 _border：Tk 滚动条协议的 first/last 是文档像素占比，于是内容
        **溢出**时 (last-first) * 总列宽 就是表格真正的可视宽度，与 winfo_width() 的
        差值正是 ttk 边框。winfo_width() 含边框，直接拿它当排布基准会把列宽填到溢出
        边框那几个像素，闪出一条根本用不上的水平滚动条。内容没溢出时 Tk 一律报
        (0, 1)，此时反算出来的是"总列宽"而非可视宽度，不能采信——所以只在 last < 1
        （确实溢出了）时测量。测到新值就重排一次，让列宽按真实可视宽度收敛。
        """
        if self.hsb is not None:
            self.hsb.set(first, last)
        total = sum(self.tree.column(c)["width"] for c in self.HEADS)
        first, last = float(first), float(last)
        if total > 0 and last < 1.0 and last > first:
            border = self.tree.winfo_width() - (last - first) * total
            # 取整：列宽必须是整数，Tcl 不认 "432.0"。这一处若漏了取整，avail 会变成
            # 浮点、算出浮点列宽，tree.column() 当场抛 TclError 把整个布局中断在半路，
            # 留下上一轮的旧宽度——看起来就像"列宽突然全乱了"
            if abs(border - self._border) > 0.5:
                self._border = max(0, int(round(border)))
                self.after_idle(self._layout_columns)      # 按真实可视宽度重排收敛
        # need 放在边框更新之后算：边框一变可视宽度就变，用旧值判断会慢一拍
        need = total > self.tree.winfo_width() - self._border + 1
        if need != self._hsb_shown:
            self.after_idle(self._toggle_hsb, need)

    def _toggle_hsb(self, need):
        if need == self._hsb_shown or self.hsb is None:
            return
        if need:
            self.hsb.pack(side="bottom", fill="x")
        else:
            self.hsb.pack_forget()
        self._hsb_shown = need

    def on_reset_cols(self):
        """重置列宽：忘掉手工拖过的宽度，恢复按当前内容自适应。"""
        self._user_widths.clear()
        self._autofit()

    def reload(self, async_=False):
        """读取配置源并刷新列表。

        async_=True（仅启动路径用）：把 MySQL/SQLite 读取丢到后台线程，窗口先出来，
        数据到了再由 self.after(0, ...) 回到主线程刷 UI。
        async_=False（刷新数据源 / 增删改后）：仍在调用方线程同步执行，
        用户刚做完操作，理所应当立刻看到结果。
        """
        if async_:
            self.status.configure(foreground="#555",
                                  text=f"正在连接 {self.backend.desc_short} …")
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
        self._display = []
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
        display = []
        # 同目录同色：一个浅色底 = 一个项目分组。只有一个目录时（比如全是 * 兜底）
        # 着色没有区分意义，退回隔行浅蓝，长列表仍读得舒服
        color_of = self._dir_colors([self._dir_key(r[0]) for r in rows])
        banded = len(color_of) > 1
        for i, (d, name, code, t, url, user, schema, isdef, wconf) in enumerate(self._rows):
            tags = []
            if banded:
                tags.append(f"dir{color_of[self._dir_key(d)]}")
            elif i % 2 == 0:
                tags.append("zebra")
            if isdef == 1:
                tags.append("default_row")
            if wconf != 1:
                # 免审核行整行橙色：扫一眼就知道哪个库写操作不弹窗
                tags.append("no_confirm")
            # v4.8：db_schema 不再进 values——列表不展示 Schema/Owner 列。
            # self._rows 里仍保留该字段，编辑/测试/删除都按 9 元组解包，不受影响
            vals = (d if d not in ("*", "") else "*（任意目录兜底）",
                    name, code, t, url, user,
                    "✓" if isdef == 1 else "",
                    "✓" if wconf == 1 else "✗ 免")
            display.append(vals)
            self.tree.insert("", "end", iid=str(i), tags=tags, values=vals)
        self._display = display
        # 数据变了内容宽度就可能变（新增一行更长的路径），重算后再排布
        self._autofit()
        self.status.configure(
            foreground="#555",
            text=f"共 {len(self._rows)} 条 ｜ {fp_text} ｜ 双击行编辑 ｜ 同色=同一目录（项目）｜ "
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
            text=f"数据源列表已刷新，共 {len(self._rows)} 条")
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

    def on_copy(self):
        """复制选中数据源：除「工作目录」外全部沿用，目录名后加 _copy。

        v4.8：主键是 (dir_path, db_code)，两边都照抄必然撞唯一约束，所以副本必须换个
        身份。按既定做法 _copy 加在工作目录上（C:\\proj → C:\\proj_copy），数据库名称
        不动——副本的用途是"给另一个目录用的同一套连接参数"，改完地址就能指向另一个库。

        例外：源行的目录是兜底值 '*' 或空串时，'*_copy' 是一个永远匹配不到任何工作区的
        死路径（dir_path 是精确匹配当前工作目录的，见 datasources._load_datasources_*）。
        此时退回给数据库名称加 _copy、目录保持兜底值，并强制取消默认标记——同一目录下
        两个 is_default=1 会让"哪个是默认"变没准。弹框说明原因，不做静默降级。
        """
        row = self._sel()
        if not row:
            return
        d, name, code, t, url, user, schema, isdef, wconf = row
        fallback = (d or "").strip() in ("*", "")
        base = {"dir": d if fallback else d + "_copy",
                "name": name,
                "code": code + "_copy" if fallback else code,
                "type": t, "url": url, "user": user, "schema": schema or "",
                "pwd": self.backend.fetch_pwd(d, code),
                "default": 0 if fallback else isdef,
                "confirm": wconf}
        if fallback:
            messagebox.showinfo(
                "复制说明",
                f"源数据源「{code}」的工作目录是兜底值 {d!r}（任意目录生效）。\n\n"
                f"若给它加 _copy 会得到 {d + '_copy'!r}——一个匹配不到任何工作区的死路径，\n"
                f"所以本次改为复制出「{code}_copy」，工作目录仍为 {d!r}，并取消默认标记。\n\n"
                f"保存后可在「编辑」弹窗里把工作目录改成目标项目路径。", parent=self)
        vals = base
        while True:
            dlg = RecordDialog(self, f"复制数据源：{code}", self.backend, vals)
            if not dlg.result:
                return
            ok, msg = self.backend.save_row(None, dlg.result)
            if ok:
                self.backend.bump_version()
                return self.reload()
            messagebox.showerror("复制失败", msg, parent=self)
            vals = dlg.result

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


def _prewarm_imports(backend):
    """后台预热重导入，与随后的窗口构造重叠，避免串行叠加到启动时间上。

    datasources → workspace → asyncio（连带 ssl / pywin32）实测约 190ms，
    mysql.connector 约 600ms。这两笔原来都要等 reload 后台线程跑到才第一次导入，
    那时窗口已经建好——用户看到的就是"窗口出来了，数据空转好几秒"。
    这里提前在子线程里导一遍，靠导入锁与后续真实导入安全共存（同一模块只初始化一次，
    后到者直接拿缓存）。失败无所谓：真正的用点会自己再报一次错，这里只求预热。
    """
    def _work():
        try:
            import db_auto_router.datasources  # noqa: F401  列探测用（两种后端都要）
        except Exception:
            pass
        if backend.kind == "mysql":
            try:
                import mysql.connector  # noqa: F401  仅 mysql 后端需要驱动
            except Exception:
                pass

    threading.Thread(target=_work, daemon=True).start()


def main():
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="数据源管理 GUI（v4.9 双后端）")
    ap.add_argument("--backend", choices=("sqlite", "mysql"), default=None,
                    help="强制指定编辑的后端（默认跟随 .env 的 MCP_CONFIG_MODE）")
    args = ap.parse_args()

    backend, mismatch = get_backend(args.backend)
    # v4.7：删掉这里的 mysql 连通性预探测。
    # 它和 App.__init__ 里的 reload() 会连查两次配置库，且都堵在 mainloop() 之前，
    # 窗口要等两轮 MySQL 往返才出现。现在连接与读取全部走 App 的后台线程，
    # 连不上时由 App._apply_rows() 弹同样的提示（窗口已在，不再一片空白后直接退出）。

    # 重导入预热：放在窗口构造之前，让 asyncio/mysql.connector 的导入与
    # App.__init__ 的控件构建、以及下面可能出现的模式提醒对话框重叠进行
    _prewarm_imports(backend)

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
