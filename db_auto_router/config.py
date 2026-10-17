# -*- coding: utf-8 -*-
"""配置项（从环境变量读取）——原 db_mcp_router.py 52–93 / 124–132 行"""

__author__ = "fengkeju"
import os
from pathlib import Path

from .logcfg import logger

# ------------------------------------------------------------------------------
# 配置项（从环境变量读取）
# ------------------------------------------------------------------------------
# v4.1 配置存储双模式（MCP_CONFIG_MODE）：
#   mysql  — 数据源配置存独立 MySQL 服务的配置库（原有方式，默认）
#   sqlite — 数据源配置存包基目录下的 datasources.db 文件（随目录分发，
#            DB Browser (SQLite) 维护，首次运行自动建库建表）
CFG_MODE = os.environ.get("MCP_CONFIG_MODE", "mysql").strip().lower()
if CFG_MODE not in ("mysql", "sqlite"):
    logger.warning(f"MCP_CONFIG_MODE={CFG_MODE!r} 无法识别（仅支持 mysql/sqlite），回退为 mysql 模式")
    CFG_MODE = "mysql"

# SQLite 配置库文件路径：相对路径按包基目录解析（与 AUDIT_LOG_DIR 同一基准），支持绝对路径
_SQLITE_PATH_RAW = os.environ.get("MCP_CONFIG_SQLITE_PATH", "datasources.db").strip()
if os.path.isabs(_SQLITE_PATH_RAW):
    SQLITE_DB_PATH = _SQLITE_PATH_RAW
else:
    SQLITE_DB_PATH = str(Path(__file__).resolve().parent.parent / _SQLITE_PATH_RAW)

CFG_HOST = os.environ.get("MCP_CONFIG_HOST", "127.0.0.1")
CFG_PORT = int(os.environ.get("MCP_CONFIG_PORT", "3306"))
CFG_USER = os.environ.get("MCP_CONFIG_USER", "root")
CFG_PWD = os.environ.get("MCP_CONFIG_PASSWORD", "")
CFG_DB = os.environ.get("MCP_CONFIG_DATABASE", "mcp")
CONNECT_TIMEOUT = int(os.environ.get("MCP_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = int(os.environ.get("MCP_READ_TIMEOUT", "30"))

# mysql 配置模式下版本探测结果的缓存时长（秒）。
# 每次工具调用都要探一次配置库版本号以感知外部改动，不缓存就等于每次调用都新开
# 一条到配置库的连接。配置库在远端时这个固定开销会累积。
# 缓存只影响"多久之内不再重复探测"：配置改完后最迟 TTL 秒后被感知，
# 仍属秒级；refresh_datasources 工具永远强制全量重读，不受此限。
# 设为 0 关闭缓存（恢复每次探测）。sqlite 模式是本地 stat，本就微秒级，不需要。
CONFIG_PROBE_TTL = max(0.0, float(os.environ.get("MCP_CONFIG_PROBE_TTL", "2.0")))

# 查询保护配置
# 注意：曾经有一个 MCP_AUTO_LIMIT_THRESHOLD（"超过此行数警告"），代码里 0 处使用，
# 属于死配置——真实行为是"无 LIMIT 的 SELECT 一律追加 LIMIT MAX_RESULT_ROWS"，
# 与阈值无关。已从本文件与 .env 删除，避免照着它调参却发现毫无效果。
MAX_RESULT_ROWS = int(os.environ.get("MCP_MAX_RESULT_ROWS", "1000"))  # 最大返回行数
MAX_RETRY_COUNT = int(os.environ.get("MCP_MAX_RETRY_COUNT", "3"))  # 最大重试次数
RETRY_DELAY = float(os.environ.get("MCP_RETRY_DELAY", "1.0"))  # 重试延迟（秒）

# 连接池配置
MAX_POOL_SIZE = int(os.environ.get("MCP_MAX_POOL_SIZE", "10"))  # 连接池最大连接数
POOL_IDLE_TIME = int(os.environ.get("MCP_POOL_IDLE_TIME", "300"))  # 连接最大空闲时间（秒）

# 结果摘要配置
SUMMARY_THRESHOLD = int(os.environ.get("MCP_SUMMARY_THRESHOLD", "50"))  # 超过此行数自动摘要
SUMMARY_DISPLAY_ROWS = int(os.environ.get("MCP_SUMMARY_DISPLAY_ROWS", "10"))  # 摘要时显示的行数

# 写操作确认策略：on（弹原生确认窗，默认）/ off（跳过确认，保持 v2.x 行为）
WRITE_CONFIRM_MODE = os.environ.get("MCP_WRITE_CONFIRM", "on").strip().lower()
# 确认弹窗中 SQL 最多展示的行数，超出折叠并提示（完整语句始终记录到 MCP stderr 日志）
DIALOG_SQL_PREVIEW_LINES = int(os.environ.get("MCP_WRITE_CONFIRM_PREVIEW_LINES", "8"))
# 写操作影响行数预估（v4.4）：确认弹窗前先"试执行→读 rowcount→回滚"，
# 把「这条 UPDATE 会动多少行」直接显示在弹窗里。
# 为什么不用改写 SQL 成 SELECT COUNT(*)：改写要解析整句句型（JOIN/子查询/多表），
# 正则漏一种就算错，而错的行数比没有行数更危险。
# 试执行的副作用：自增/序列值会被消耗（回滚不归还）、触发器会真实触发一次。
# 介意就设 MCP_WRITE_ESTIMATE=off 关闭，弹窗改回只显示 SQL。
WRITE_ESTIMATE_MODE = os.environ.get("MCP_WRITE_ESTIMATE", "on").strip().lower()

# v4.0 重武器配置
# 审计日志目录（每条 SQL 落一行 JSON）；设为 "off" 关闭审计
# 相对路径与 MCP_CONFIG_SQLITE_PATH 同一基准：一律按包基目录解析。
# 早先直接取环境变量，相对路径会落到 MCP 进程的 cwd（由客户端启动方式决定，
# 用户不可控），同一份配置换个启动方式审计日志就散落到别处。
_AUDIT_DIR_RAW = os.environ.get("MCP_AUDIT_LOG_DIR", "logs").strip()
if _AUDIT_DIR_RAW.lower() == "off":
    AUDIT_LOG_DIR = _AUDIT_DIR_RAW  # 保持 "off" 字面量，交由下方 AUDIT_ENABLED 判定
elif os.path.isabs(_AUDIT_DIR_RAW):
    AUDIT_LOG_DIR = _AUDIT_DIR_RAW
else:
    AUDIT_LOG_DIR = str(Path(__file__).resolve().parent.parent / _AUDIT_DIR_RAW)
AUDIT_ENABLED = AUDIT_LOG_DIR.lower() != "off"
# query_audit_log 默认扫描最近多少天的审计文件（按文件名里的 YYYYMMDD 判定，不看 mtime）。
# 原实现硬编码只取最近 3 个文件——一个用于追溯"到底执行了什么"的工具，第 4 天以前的
# 记录就查不到了，违背它存在的目的。传 0 表示不限天数、扫全部文件。
AUDIT_SCAN_DAYS = int(os.environ.get("MCP_AUDIT_LOG_SCAN_DAYS", "30"))
# 审计日志保留天数：>0 时 MCP 启动会删除更早的 audit-*.jsonl；0 = 永久保留（默认）。
# 日志按天一个文件、增速很慢，默认不清理；担心无界增长再设这个值。
AUDIT_RETAIN_DAYS = int(os.environ.get("MCP_AUDIT_LOG_RETAIN_DAYS", "0"))
# 事务空闲超时（秒），超时自动回滚释放锁。
# 这是全局默认值；单条事务可在 begin_transaction(idle_timeout=) 里覆盖，
# 长流程（模型等用户确认、跨轮次操作）按需放宽，不必把全局默认一起调大——
# 全局默认调大的代价是"忘记提交的事务"更久才放锁。
TXN_IDLE_TIMEOUT = int(os.environ.get("MCP_TXN_IDLE_TIMEOUT", "600"))
# 单事务空闲超时的天花板（秒）。begin/extend 传进来的 idle_timeout 超过此值即被拒绝。
# 存在的意义：防止误传一个巨大值（如 999999999）让事务永不释放、锁永不放开。
# 默认 24 小时——真需要更久的事务，应该是批处理脚本而不是交互式会话。
TXN_MAX_IDLE = int(os.environ.get("MCP_TXN_MAX_IDLE", "86400"))
# 导出单批读取行数
EXPORT_BATCH = int(os.environ.get("MCP_EXPORT_BATCH", "2000"))
# 导入单批提交行数
IMPORT_BATCH = int(os.environ.get("MCP_IMPORT_BATCH", "500"))
# 导出/导入最大行数硬上限（防爆盘），默认 500 万
EXPORT_MAX_ROWS = int(os.environ.get("MCP_EXPORT_MAX_ROWS", "5000000"))

# 只读 SQL 前缀
READ_ONLY_SQL_PREFIXES = {"SELECT", "SHOW", "DESC", "DESCRIBE", "EXPLAIN", "WITH"}
# 写操作 SQL 前缀（需要确认）
WRITE_SQL_PREFIXES = {"INSERT", "DELETE", "UPDATE", "DROP", "TRUNCATE", "ALTER", "CREATE"}

# MCP 服务器信息
# version 会在 initialize 响应里上报给客户端，也是启动 banner 的版本号——
# 与 README 标题 / GUI / 启动脚本保持同一个号，别只改一处。
MCP_SERVER_INFO = {
    "name": "db-auto-router",
    "version": "4.7"
}
