# -*- coding: utf-8 -*-
"""数据源注册表：从统一配置库查询数据源（目录变化检测 + 会话内永久缓存）
——原 105–115、243–357 行

v4.1 双模式（MCP_CONFIG_MODE，见 config.py）：
- mysql  ：配置存独立 MySQL 服务（原有路径，行为不变）
- sqlite ：配置存包基目录下 datasources.db 文件（stdlib sqlite3，零额外依赖），
           首次运行自动建库建表；dir_path='*' 或空串作为「任意目录兜底数据源」，
           精确匹配未命中时启用——接收方只填 conn_url/user/password 即可跑通。

v4.2 变更感知（替代原 3 秒 TTL）：
- sqlite ：每次 load 前 stat 配置文件（主文件 + -wal + -shm 取最大 mtime_ns），
           mtime 变化 → 立即重读。外部工具（GUI/DB Browser）改完配置，
           下一次查询秒级自动生效，无需调 refresh_datasources。
- mysql  ：配置库自动建 mcp_config_meta 版本表（幂等，权限不足自动降级），
           每次 load 先 SELECT version（主键单行，极轻量），版本变了才全量重读；
           GUI/管理工具写完配置后 version+1 即可实现秒级感知。
"""

__author__ = "fengkeju"
import os
import time
import threading
from typing import Dict, Any, Optional

from .config import (CFG_MODE, SQLITE_DB_PATH, CONFIG_PROBE_TTL,
                     CFG_HOST, CFG_PORT, CFG_USER, CFG_PWD, CFG_DB,
                     CONNECT_TIMEOUT, READ_TIMEOUT, MAX_RETRY_COUNT, RETRY_DELAY)
from .errutil import is_transient_error, safe_close
from .logcfg import logger
from .util import normalize_path
from . import workspace  # 通过属性访问 _workspace_dir（global 重绑定，禁止 from-import）

# ------------------------------------------------------------------------------
# 全局配置缓存 (线程安全 + 目录变化检测 + 配置源变更感知)
# v4.2：缓存有效性 = 目录相同 且 配置源指纹相同（sqlite→文件 mtime_ns；mysql→版本号）。
# 外部工具改完配置，下一次查询自动重读，秒级生效，无需 refresh_datasources。
# ------------------------------------------------------------------------------
_config_cache = {
    "data": {},
    "cached_dir": "",       # 记录缓存时的目录
    "cached_fp": None,      # 配置源变更指纹：sqlite→mtime_ns；mysql→版本号
    "lock": threading.Lock(),
}

# MySQL 配置库列探测缓存（v4.4 db_schema / v4.6 write_confirm）。空 dict=未探测；
# 缺列时按旧列清单查询并降级。refresh_datasources 会清空（用户可能刚补了列）。
_MYSQL_COLUMN_CACHE: Dict[str, bool] = {}

# 最近一次加载失败的真实原因。load_datasources 的降级策略是"不杀进程"：有缓存用
# 缓存，没缓存返回空数据源。但空数据源传到 workspace._resolve_datasource 只会变成
# 一句"没有可用的数据源，请检查 dir_path 是否匹配"——配置库连不上、密码错、驱动
# 没装、配置表不存在，全都被这句提示带偏方向，而真实错误只留在 stderr 日志里
# （MCP 的 stderr 用户日常看不到）。故在这里留一份，让工具层能把真实原因一起报出来。
# 加载成功即清空，不会把陈旧原因留到下一次正常调用里。
_last_load_error: Optional[str] = None


def _note_load_failure(err: Optional[BaseException]) -> None:
    """记录（传异常）或清除（传 None）最近一次加载失败的真实原因。"""
    global _last_load_error
    _last_load_error = None if err is None else f"{type(err).__name__}: {err}"


def get_last_load_error() -> Optional[str]:
    """最近一次数据源加载失败的真实原因；上次加载成功则为 None。

    给 workspace._resolve_datasource 用：它报"没有可用的数据源"时必须能区分
    「配置读到了但当前目录没配」和「配置根本没读到」——后者说 dir_path 是错的。
    """
    return _last_load_error

# 缺列时的降级说明（日志用）：讲清"缺这列时行为退回成什么样、去哪补"
_COLUMN_MISSING_NOTES = {
    "db_schema": ("[mysql] 配置库 mcp_datasource_config 缺 db_schema 列：PostgreSQL/GaussDB/Oracle 的"
                  "表结构查询将按方言默认 schema 过滤。补列语句见 docs/mysql_config.sql 末尾迁移段"),
    "write_confirm": ("[mysql] 配置库 mcp_datasource_config 缺 write_confirm 列：全部数据源按"
                      "「写操作需人工审核」处理（安全默认，行为与 v4.6 之前一致）。"
                      "补列语句见 docs/mysql_config.sql v4.6 迁移段"),
}


def mysql_config_has_column(cfg_conn, column: str) -> bool:
    """探测 MySQL 配置库 mcp_datasource_config 是否已有某一列。

    没有该列时不能直接 SELECT 它——MySQL 报 1054 Unknown column，整个数据源加载
    会失败。故先探测：缺列就按旧列清单查询，行为退回加该列之前的版本，并在日志里
    给出补列语句的位置。结果进程内缓存（每次 load 都探测等于白开一条连接），
    refresh_datasources 会重置。

    探测本身失败（连不上/无权限）时按"没有该列"处理：旧行为至少能跑，
    不会因为一次探测失败让所有查询挂掉。

    公开给 tools/ 下的 GUI 复用（它同样直写同一张配置表，探测口径必须一致）。
    """
    if column in _MYSQL_COLUMN_CACHE:
        return _MYSQL_COLUMN_CACHE[column]
    try:
        cur = cfg_conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'mcp_datasource_config' "
                "AND COLUMN_NAME = %s",
                (CFG_DB, column))
            row = cur.fetchone()
            _MYSQL_COLUMN_CACHE[column] = bool(row and row[0])
        finally:
            cur.close()
    except Exception as e:
        logger.warning(f"[mysql] 探测 {column} 列失败，按无该列处理: {e}")
        _MYSQL_COLUMN_CACHE[column] = False
    if not _MYSQL_COLUMN_CACHE[column]:
        logger.warning(_COLUMN_MISSING_NOTES.get(
            column, f"[mysql] 配置库 mcp_datasource_config 缺 {column} 列，按旧行为处理"))
    return _MYSQL_COLUMN_CACHE[column]


# ------------------------------------------------------------------------------
# SQLite 配置库（v4.1）：建库建表 + 查询
# ------------------------------------------------------------------------------
# 目录兜底行的 dir_path 约定值：'*' 或空串。
# v4.7 起 MySQL 与 SQLite 两种配置模式都支持（此前仅 sqlite 分支实现了回退）。
_FALLBACK_DIR_PATHS = ("*", "")

_SQLITE_SCHEMA = """
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

# 老 SQLite 配置库要补的列（按版本递增）。`CREATE TABLE IF NOT EXISTS` 对已存在的表
# 不会加列，必须显式 ALTER；SQLite 的 ADD COLUMN 是 O(1) 元数据操作，老行自动补默认值，
# 重复执行由 PRAGMA table_info 幂等挡住。新列一律带"安全默认"：缺列/老行的行为必须
# 与加列之前一致（write_confirm 默认 1 = 仍需人工审核），不能因为升级 quietly 放宽安全策略。
_SQLITE_MIGRATION_COLUMNS = (
    ("db_schema", "TEXT", "PG/Oracle 表结构查询可指定 schema"),
    ("write_confirm", "INTEGER NOT NULL DEFAULT 1",
     "写操作是否需人工审核：0-开发库免确认，1-需审核"),
)


def upgrade_sqlite_config_schema(conn) -> None:
    """老 SQLite 配置库补后续版本新增的列（db_schema / write_confirm）。

    公开给 tools/ 下的 GUI/CLI 复用：三处各自内联一份 DDL 必然漂移
    （与 bump_mysql_config_version 收敛 DDL 同一个理由）。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(mcp_datasource_config)")}
    for col, ddl, why in _SQLITE_MIGRATION_COLUMNS:
        if col not in cols:
            conn.execute(f"ALTER TABLE mcp_datasource_config ADD COLUMN {col} {ddl}")
            conn.commit()
            logger.info(f"SQLite 配置库已自动升级：新增 {col} 列（{why}）")


def _sqlite_connect():
    """连接 SQLite 配置库；文件不存在时自动建库建表（幂等）。

    WAL 模式：MCP 运行中用户用 DB Browser 同时编辑互不阻塞；
    busy_timeout 兜底极短写锁竞争。
    check_same_thread=False：MCP 工具经 asyncio.to_thread 等多线程进入，
    每次查询均为「连接→查询→关闭」的短生命周期用法，无跨线程共享连接。
    """
    import sqlite3
    db_path = SQLITE_DB_PATH
    first_create = not os.path.exists(db_path)
    if first_create:
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=CONNECT_TIMEOUT, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(_SQLITE_SCHEMA)
        upgrade_sqlite_config_schema(conn)
        conn.commit()
    except Exception:
        safe_close(conn)
        raise
    if first_create:
        logger.info(f"SQLite 配置库不存在，已自动创建: {db_path}")
    return conn


def _sqlite_config_mtime_ns() -> int:
    """SQLite 配置源变更指纹：主文件 + -wal + -shm 三文件 mtime_ns 取最大值。

    WAL 模式下写操作先落 -wal 文件，checkpoint 才合并回主文件——只 stat 主文件
    可能感知不到 GUI/DB Browser 的写入，必须三文件一起看。文件全部不存在 → -1
    （被删后重读会触发 _sqlite_connect 自动重建空库）。
    """
    base = SQLITE_DB_PATH
    latest = -1
    for suffix in ("", "-wal", "-shm"):
        try:
            latest = max(latest, os.stat(base + suffix).st_mtime_ns)
        except OSError:
            continue
    return latest


def _load_datasources_sqlite(current_dir_normalized: str) -> Dict[str, Any]:
    """SQLite 模式：按目录精确匹配查询数据源，未命中时回退兜底行（dir_path='*' 或空串）。"""
    conn = _sqlite_connect()
    try:
        # 精确匹配：与 MySQL 分支同一条 SQL（REPLACE 归一化反斜杠；占位符 %s→?）
        rows = conn.execute(
            """SELECT db_code, db_type, conn_url, db_user, db_password, db_schema, is_default, write_confirm
               FROM mcp_datasource_config
               WHERE REPLACE(dir_path, ?, '/') = ?
               ORDER BY is_default DESC, db_code""",
            ("\\", current_dir_normalized)
        ).fetchall()

        matched_dir = current_dir_normalized
        if not rows:
            # 兜底：dir_path='*' 或空串的行作为任意目录默认数据源
            placeholders = ", ".join(["?"] * len(_FALLBACK_DIR_PATHS))
            rows = conn.execute(
                f"""SELECT db_code, db_type, conn_url, db_user, db_password, db_schema, is_default, write_confirm
                    FROM mcp_datasource_config
                    WHERE dir_path IN ({placeholders})
                    ORDER BY is_default DESC, db_code""",
                _FALLBACK_DIR_PATHS
            ).fetchall()
            if rows:
                matched_dir = "(兜底 *)"
                logger.info(f"目录 {current_dir_normalized} 无精确数据源，启用兜底数据源行")

        sources: Dict[str, Any] = {}
        default_db_code = None
        for db_code, db_type, conn_url, db_user, db_password, db_schema, is_default, write_confirm in rows:
            sources[db_code] = {
                "db_code": db_code,
                "db_type": db_type,
                "conn_url": conn_url,
                "db_user": db_user,
                "db_password": db_password,
                "db_schema": db_schema,
                "is_default": is_default or 0,
                # v4.6：写操作人工审核开关。None（老行显式插了 NULL）按 1=需审核——安全默认
                "write_confirm": 1 if write_confirm is None else int(write_confirm),
            }
            # 标记默认数据源（is_default=1 或第一个）——与 MySQL 分支语义一致
            if is_default == 1 or default_db_code is None:
                default_db_code = db_code

        sources["_default"] = default_db_code
        logger.info(f"[sqlite] 已加载 {len(sources)-1} 个数据源 (目录: {matched_dir}): "
                    f"{[k for k in sources if k != '_default']}, 默认: {default_db_code}")
        return sources
    finally:
        safe_close(conn)


# ------------------------------------------------------------------------------
# 1. 从统一配置库查询数据源 (支持目录变化检测 + 配置源变更感知，会话内永久缓存)
# ------------------------------------------------------------------------------
def _fingerprint_changed() -> bool:
    """检测配置源变更指纹（sqlite→文件 mtime；mysql→版本号）相对缓存是否变化。
    变化返回 True（调用方需强制重读）。MySQL 探测失败时降级返回 False（用缓存），
    全量读取阶段自会重试真实查询。"""
    with _config_cache["lock"]:
        cached_fp = _config_cache.get("cached_fp")
        has_cache = bool(_config_cache["data"])

    if CFG_MODE == "sqlite":
        fp = _sqlite_config_mtime_ns()
        # 无缓存或指纹变化 → 需要重读
        return (not has_cache) or fp != cached_fp

    # ---- mysql：探测 mcp_config_meta 版本号（表不存在时自动创建；失败降级）----
    fp = _mysql_probe_version()
    if fp is None:
        # 版本探测不可用（连不上/无权限）：有缓存则视为未变化继续用缓存，
        # 由全量读取阶段的降级路径兜底
        return not has_cache
    return (not has_cache) or fp != cached_fp


# 版本探测缓存（仅 mysql 模式）。失败也缓存：配置库暂时连不上时，
# 避免每次工具调用都去重试连接、把超时叠加到调用延迟上。
_probe_cache = {"fp": None, "ts": 0.0}
_probe_lock = threading.Lock()


def _mysql_probe_version(use_cache: bool = True) -> Optional[int]:
    """读取配置库 mcp_config_meta 版本号（幂等建表+初始化）。
    返回 None 表示探测不可用（连不上 / 无建表权限）——调用方降级处理。

    use_cache 只决定「是否从缓存读」，缓存始终会被写入：
    - use_cache=True ：命中 TTL 缓存直接返回，不碰数据库
    - use_cache=False：绕过缓存读真值，但仍把结果写回缓存
    第二次行为很关键——全量重载后取新指纹走的就是这条路。若只读不写，
    重载完缓存里还是旧值，紧接着的第一次缓存探测必然落空、白付一次真实连接，
    而且会把陈旧指纹和 cached_fp 摆在一起，诱发多跑一次全量重载。

    TTL 内的重复探测结果必然相同（探测的唯一目的就是感知外部改动），
    缓存它省掉的是每次工具调用一条到配置库的连接。
    """
    now = time.monotonic()
    if use_cache and CONFIG_PROBE_TTL > 0:
        with _probe_lock:
            if now - _probe_cache["ts"] < CONFIG_PROBE_TTL:
                logger.debug(f"[mysql] 版本探测命中缓存: {_probe_cache['fp']}")
                return _probe_cache["fp"]

    fp = _mysql_probe_version_uncached()

    if CONFIG_PROBE_TTL > 0:
        with _probe_lock:
            _probe_cache["fp"] = fp
            _probe_cache["ts"] = time.monotonic()
    return fp


def _mysql_probe_version_uncached() -> Optional[int]:
    """真正连配置库读版本号（无缓存）。"""
    try:
        import mysql.connector  # 懒加载：sqlite 用户无需安装
        conn = mysql.connector.connect(
            host=CFG_HOST, port=CFG_PORT, user=CFG_USER, password=CFG_PWD,
            database=CFG_DB, charset='utf8mb4',
            connect_timeout=CONNECT_TIMEOUT, autocommit=True)
    except Exception as e:
        logger.debug(f"[mysql] 版本探测连接失败（降级处理）: {e}")
        return None
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT version FROM mcp_config_meta WHERE id=1")
            row = cur.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            # 表不存在 → 尝试幂等创建并初始化（权限不足则放弃，降级为无版本号模式）
            try:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS mcp_config_meta ("
                    "  id TINYINT UNSIGNED NOT NULL PRIMARY KEY,"
                    "  version BIGINT UNSIGNED NOT NULL DEFAULT 1"
                    ") ENGINE=InnoDB")
                cur.execute("INSERT IGNORE INTO mcp_config_meta (id, version) VALUES (1, 1)")
                conn.commit()
                return 1
            except Exception as e2:
                logger.warning(f"[mysql] mcp_config_meta 版本表不可用"
                               f"（将退回每次全量查询模式）: {e2}")
                return None
        finally:
            cur.close()
    finally:
        safe_close(conn)


def bump_mysql_config_version() -> Optional[str]:
    """管理工具（GUI/CLI）写完 MySQL 配置库后调用：版本号 +1 触发 MCP 秒级感知。
    独立暴露为模块级函数供外部脚本 import，是 mcp_config_meta DDL + bump SQL 的唯一实现
    （GUI 等调用方不要各自内联一份，否则三处 DDL 会漂移）。

    返回 None = 成功；返回错误描述字符串 = 失败（调用方可自行决定怎么提示）。
    失败绝不抛出：配置已写入成功，版本号 bump 失败最坏情况只是 MCP 等下一次全量重读
    才发现变更。
    """
    try:
        import mysql.connector
        conn = mysql.connector.connect(
            host=CFG_HOST, port=CFG_PORT, user=CFG_USER, password=CFG_PWD,
            database=CFG_DB, charset='utf8mb4',
            connect_timeout=CONNECT_TIMEOUT, autocommit=True)
    except Exception as e:
        msg = f"[mysql] 版本号 bump 连接失败: {e}"
        logger.warning(msg)
        return msg
    try:
        cur = conn.cursor()
        # 幂等：表不存在先建（与 _mysql_probe_version 同 DDL）
        cur.execute(
            "CREATE TABLE IF NOT EXISTS mcp_config_meta ("
            "  id TINYINT UNSIGNED NOT NULL PRIMARY KEY,"
            "  version BIGINT UNSIGNED NOT NULL DEFAULT 1"
            ") ENGINE=InnoDB")
        cur.execute("INSERT IGNORE INTO mcp_config_meta (id, version) VALUES (1, 1)")
        cur.execute("UPDATE mcp_config_meta SET version = version + 1 WHERE id = 1")
        conn.commit()
        cur.close()
        logger.info("[mysql] 配置版本号已 +1，MCP 下一次查询将自动感知")
        return None
    except Exception as e:
        msg = f"[mysql] 版本号 bump 失败（配置本身已写入）: {e}"
        logger.warning(msg)
        return msg
    finally:
        safe_close(conn)


# MySQL 配置库 mcp_datasource_config 建表 DDL（当前完整 schema：含 v4.4 db_schema /
# v4.6 write_confirm）。与 docs/mysql_config.sql 保持同源，但这里是 CREATE IF NOT EXISTS
# 幂等版、绝不 DROP——供数据源管理 GUI 的「初始化配置表」使用。GUI 等调用方不要各自内联
# 一份 DDL（与 bump_mysql_config_version 收敛 mcp_config_meta DDL 是同一个理由：三处必漂移）。
_MYSQL_DATASOURCE_DDL = """
CREATE TABLE IF NOT EXISTS mcp_datasource_config (
  dir_path      varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '*' COMMENT '工作空间(*或空=任意目录兜底)',
  db_name       varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '业务表述/项目名称',
  db_code       varchar(64)  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '数据库编码',
  db_type       varchar(32)  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '数据库类型',
  conn_url      varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '数据库ip+端口',
  db_user       varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '登录用户名',
  db_password   varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '登录密码',
  db_schema     varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT 'PG/GaussDB的schema或Oracle/达梦的owner；MySQL留空(库名取db_code)',
  write_confirm tinyint(4) NULL DEFAULT 1 COMMENT '写操作是否需人工审核：0-否(开发库免确认)，1-是',
  is_default    tinyint(4) NULL DEFAULT 0 COMMENT '是否默认数据源：0-否，1-是',
  PRIMARY KEY (dir_path, db_code) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci ROW_FORMAT = Dynamic
"""

# mcp_config_meta DDL：与 _mysql_probe_version_uncached / bump_mysql_config_version 内联的
# 那份同构，此处仅供 ensure_mysql_config_schema 复用。
_MYSQL_META_DDL = (
    "CREATE TABLE IF NOT EXISTS mcp_config_meta ("
    "  id TINYINT UNSIGNED NOT NULL PRIMARY KEY,"
    "  version BIGINT UNSIGNED NOT NULL DEFAULT 1"
    ") ENGINE=InnoDB"
)


def ensure_mysql_config_schema(conn) -> None:
    """在已连接到 mcp 配置库的连接上幂等创建配置表（mcp_datasource_config + mcp_config_meta）。

    供数据源管理 GUI 的「初始化配置表」使用。两张表都走 CREATE IF NOT EXISTS、绝不 DROP——
    重复执行或表已存在时原样不动，已写入的数据源不会被改动或删除。调用方需自行保证 conn
    已连接到 .env 指向的配置库（database=CFG_DB）。
    """
    cur = conn.cursor()
    try:
        cur.execute(_MYSQL_META_DDL)
        cur.execute("INSERT IGNORE INTO mcp_config_meta (id, version) VALUES (1, 1)")
        cur.execute(_MYSQL_DATASOURCE_DDL)
        conn.commit()
    finally:
        cur.close()


def load_datasources(force_refresh: bool = False) -> Dict[str, Any]:
    """带缓存的数据源加载器，会话内永久缓存 + 目录变化/配置源变更自动刷新。
    v4.1 起按 MCP_CONFIG_MODE 分发到 MySQL / SQLite 配置库。"""

    # 强制刷新时重置列探测结果：用户可能刚执行了 docs/mysql_config.sql
    # 的补列语句，不重置就得重启 MCP 才能用上新列
    if force_refresh:
        _MYSQL_COLUMN_CACHE.clear()

    # 优先使用 Claude Code 工作空间目录，否则使用当前 cwd
    current_dir = workspace._workspace_dir if workspace._workspace_dir else os.getcwd()
    current_dir_normalized = normalize_path(current_dir)

    # 检测目录变化，自动强制刷新
    with _config_cache["lock"]:
        cached_dir = _config_cache.get("cached_dir", "")
        if current_dir_normalized != cached_dir:
            force_refresh = True
            logger.info(f"目录已切换: {cached_dir} -> {current_dir_normalized}，强制刷新缓存")

    # 缓存有效检查：目录没变 且 配置源指纹（mtime/版本号）没变 → 直接返回；
    # 指纹变化（外部工具改了配置）自动重读——本地 stat / 单行 SELECT 极轻量
    if not force_refresh and not _fingerprint_changed():
        logger.debug(f"使用缓存数据源，目录: {_config_cache.get('cached_dir')}")
        return _config_cache["data"]

    logger.info("刷新数据源配置...")

    # ---- v4.1：SQLite 模式（本地文件配置库，无重试必要——本地文件读取不存在瞬态故障）----
    if CFG_MODE == "sqlite":
        try:
            sources = _load_datasources_sqlite(current_dir_normalized)
            _note_load_failure(None)
            with _config_cache["lock"]:
                _config_cache["data"] = sources
                _config_cache["cached_dir"] = current_dir_normalized
                _config_cache["cached_fp"] = _sqlite_config_mtime_ns()
            return sources
        except Exception as e:
            _note_load_failure(e)
            # 与 MySQL 分支同策略：有缓存降级用旧缓存，否则返回空数据源（不杀进程）
            logger.error(f"[sqlite] 加载数据源失败: {e}", exc_info=True)
            with _config_cache["lock"]:
                if _config_cache["data"]:
                    logger.warning("[sqlite] 使用旧缓存数据")
                    return _config_cache["data"]
            logger.error("[sqlite] 无缓存可降级，返回空数据源（服务器继续运行）")
            return {"_default": None}

    # ---- MySQL 模式（原有路径，行为不变）----
    cfg_conn = None

    # 重试机制
    retry_count = 0
    last_error = None

    while retry_count < MAX_RETRY_COUNT:
        try:
            import mysql.connector  # v4.1 懒加载：sqlite/非 mysql 用户无需安装此驱动
            cfg_conn = mysql.connector.connect(
                host=CFG_HOST,
                port=CFG_PORT,
                user=CFG_USER,
                password=CFG_PWD,
                database=CFG_DB,
                charset='utf8mb4',
                connect_timeout=CONNECT_TIMEOUT,
                read_timeout=READ_TIMEOUT,
                autocommit=True
            )
            cursor = cfg_conn.cursor(dictionary=True)

            logger.info(f"当前目录(标准化): {current_dir_normalized} (原始: {current_dir})")

            # v4.4/v4.6：db_schema、write_confirm 列可能还没补（老配置库）。
            # 缺列时按旧列清单查，row.get(...) 自然拿到 None，行为退回加列之前的版本
            # （db_schema=None→方言默认 schema；write_confirm=None→需人工审核）。
            optional_cols = [c for c in ("db_schema", "write_confirm")
                             if mysql_config_has_column(cfg_conn, c)]
            select_cols = "db_code, db_type, conn_url, db_user, db_password"
            if optional_cols:
                select_cols += ", " + ", ".join(optional_cols)
            select_cols += ", is_default"

            # 查询配置表，新增 is_default 字段
            # 路径匹配：支持正斜杠和反斜杠两种格式
            # 配置库存储可能是 Windows 反斜杠格式，roots 传入的是正斜杠格式
            # 注意：MySQL 中反斜杠需要双重转义，Python "\\\\" → MySQL '\\'
            cursor.execute(
                f"""SELECT {select_cols}
                   FROM mcp_datasource_config
                   WHERE REPLACE(dir_path, '\\\\', '/') = %s
                   ORDER BY is_default DESC, db_code""",
                (current_dir_normalized,)
            )
            rows = cursor.fetchall()
            cursor.close()

            # v4.7：目录未精确命中时回退兜底行（dir_path='*' 或空串），语义与
            # _load_datasources_sqlite 对齐。此前 MySQL 模式只做等值匹配，用户按
            # README「dir_path='*' = 任意目录兜底数据源」配的兜底行静默失效——
            # 不报错也不被读到，表现为"明明配了 * 却查不到数据源"。
            matched_dir = current_dir_normalized
            if not rows:
                fb_placeholders = ", ".join(["%s"] * len(_FALLBACK_DIR_PATHS))
                cursor = cfg_conn.cursor(dictionary=True)
                cursor.execute(
                    f"""SELECT {select_cols}
                       FROM mcp_datasource_config
                       WHERE dir_path IN ({fb_placeholders})
                       ORDER BY is_default DESC, db_code""",
                    _FALLBACK_DIR_PATHS
                )
                rows = cursor.fetchall()
                cursor.close()
                if rows:
                    matched_dir = "(兜底 *)"
                    logger.info(f"目录 {current_dir_normalized} 无精确数据源，启用兜底数据源行")

            # v4.2：读取当前版本号作为变更指纹（表不可用时 None → 缓存指纹置 None，
            # 下次 load 仍会全量查询，行为退回 v4.1 之前）
            # use_cache=False：刚重载完，必须取真值。用缓存里的旧指纹会让下一次探测
            # 误判"版本变了"而白跑一次全量重载。
            new_fp = _mysql_probe_version(use_cache=False)

            # 构建 sources 字典
            sources = {}
            default_db_code = None
            for row in rows:
                db_code = row["db_code"]
                sources[db_code] = {
                    "db_code": db_code,
                    "db_type": row["db_type"],
                    "conn_url": row["conn_url"],
                    "db_user": row["db_user"],
                    "db_password": row["db_password"],
                    "db_schema": row.get("db_schema"),
                    "is_default": row.get("is_default", 0),
                    # v4.6：写操作人工审核开关。缺列/老行 NULL 按 1=需审核——安全默认，
                    # 升级 MCP 不会让任何数据源悄悄免掉确认
                    "write_confirm": 1 if row.get("write_confirm") is None else int(row["write_confirm"]),
                }
                # 标记默认数据源（is_default=1 或第一个）
                if row.get("is_default") == 1 or (default_db_code is None):
                    default_db_code = db_code

            # 将默认数据源信息存入缓存
            sources["_default"] = default_db_code

            # 与 sqlite 分支同口径：打一行"实际用哪个目录命中的"，兜底命中时可见 "(兜底 *)"
            logger.info(f"[mysql] 已加载 {len(sources)-1} 个数据源 (目录: {matched_dir}): "
                        f"{[k for k in sources if k != '_default']}, 默认: {default_db_code}")

            cfg_conn.close()

            _note_load_failure(None)
            with _config_cache["lock"]:
                _config_cache["data"] = sources
                _config_cache["cached_dir"] = current_dir_normalized
                _config_cache["cached_fp"] = new_fp
            return sources

        except Exception as e:
            last_error = e
            _note_load_failure(e)
            retry_count += 1
            if cfg_conn:
                safe_close(cfg_conn)
            # 只重试瞬时错误（连不上/超时/死锁）。密码错误、配置库不存在、无权限这类
            # 永久性错误重试只是白等 (MAX_RETRY_COUNT-1)×RETRY_DELAY——而本函数在每个
            # 工具调用的热路径上（workspace._resolve_datasource），这点延迟会被放大成
            # "每次调用都多等两秒"。判定口径与 readengine/writeengine/introspection 一致。
            if retry_count < MAX_RETRY_COUNT and is_transient_error(e):
                logger.warning(f"加载数据源失败(第{retry_count}次，瞬时错误)，{RETRY_DELAY}秒后重试: {e}")
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"加载数据源失败（永久性错误，不重试）: {e}")
                break

    # 重试全部失败，降级策略：缓存存在则返回旧数据，否则返回空数据源（不杀进程）
    logger.error(f"加载数据源失败(重试{MAX_RETRY_COUNT}次后): {last_error}")
    with _config_cache["lock"]:
        if _config_cache["data"]:
            logger.warning("使用旧缓存数据")
            return _config_cache["data"]
    # 无缓存可降级，返回空数据源，让服务器继续运行
    logger.error("无缓存可降级，返回空数据源（服务器继续运行）")
    return {"_default": None}


def get_default_db_code(sources: Dict[str, Any]) -> Optional[str]:
    """获取默认数据源编码"""
    return sources.get("_default")
