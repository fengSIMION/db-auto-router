# -*- coding: utf-8 -*-
"""MCP 服务器入口（mcp SDK 2.x）：创建实例 + 注册 17 个工具 + stdio 启动
——原 2218–2233、2900–2978 行"""

__author__ = "fengkeju"
import os
import sys

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from .config import (MCP_SERVER_INFO, CFG_MODE, SQLITE_DB_PATH,
                     CFG_HOST, CFG_PORT, CFG_DB, MAX_POOL_SIZE,
                     POOL_IDLE_TIME, SUMMARY_THRESHOLD, SUMMARY_DISPLAY_ROWS,
                     WRITE_CONFIRM_MODE, TXN_IDLE_TIMEOUT, TXN_MAX_IDLE,
                     AUDIT_ENABLED, AUDIT_LOG_DIR, AUDIT_SCAN_DAYS, AUDIT_RETAIN_DAYS)
from .logcfg import logger
from .pool import start_pool_cleanup_thread
from .transactions import start_transaction_cleanup_thread
from .tools import (query, execute, list_datasources, refresh_datasources, search_tables,
                    get_schema_info, begin_transaction, commit_transaction,
                    rollback_transaction, list_transactions, extend_transaction,
                    export_csv, import_csv, query_audit_log, dump_database,
                    explain_query, slow_queries, cleanup_old_audit_logs)

# ------------------------------------------------------------------------------
# MCPServer 实例 (官方 mcp SDK 2.x 接管协议层：stdio 传输/握手/版本协商/错误码)
# ------------------------------------------------------------------------------
# SDK 2.x 起 FastMCP 改名 MCPServer 并搬到 mcp.server.mcpserver，协议类型拆到
# 独立的 mcp_types 包；构造函数原生收 version（1.x 时代只能篡改私有属性）。
mcp = MCPServer(MCP_SERVER_INFO["name"],
                instructions="多数据源 SQL 查询路由：按工作目录自动选择数据源，支持跨库语法。query 只读；execute 写操作执行前会向用户弹确认。",
                version=MCP_SERVER_INFO["version"])


# --- 注册工具：annotations 补充 title，并声明 structured_output ---

def _register_tools():
    annotations = {
        "query": ToolAnnotations(title="执行只读查询", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        "execute": ToolAnnotations(title="执行任意SQL(写操作需确认)", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
        "list_datasources": ToolAnnotations(title="列出数据源", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        "refresh_datasources": ToolAnnotations(title="刷新数据源缓存", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        "search_tables": ToolAnnotations(title="搜索表", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        "get_schema_info": ToolAnnotations(title="查询表结构", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        "begin_transaction": ToolAnnotations(title="开启事务", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
        "commit_transaction": ToolAnnotations(title="提交事务", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
        "rollback_transaction": ToolAnnotations(title="回滚事务", readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True),
        "list_transactions": ToolAnnotations(title="列出活跃事务", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        "extend_transaction": ToolAnnotations(title="续期事务", readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        "export_csv": ToolAnnotations(title="导出CSV", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        "import_csv": ToolAnnotations(title="导入CSV(需确认)", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
        "dump_database": ToolAnnotations(title="导出整库SQL(需确认)", readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        "query_audit_log": ToolAnnotations(title="查询审计日志", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        "explain_query": ToolAnnotations(title="执行计划与诊断", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        "slow_queries": ToolAnnotations(title="慢查询与在跑语句", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    }
    for fn in (query, execute, list_datasources, refresh_datasources, search_tables, get_schema_info,
               begin_transaction, commit_transaction, rollback_transaction, list_transactions,
               extend_transaction, export_csv, import_csv, dump_database, query_audit_log,
               explain_query, slow_queries):
        mcp.add_tool(
            fn,
            name=fn.__name__,
            annotations=annotations[fn.__name__],
            structured_output=True,  # 声明 outputSchema，返回 structuredContent
        )

_register_tools()


# ------------------------------------------------------------------------------
# 启动入口 (stdio 传输由 SDK 接管；Windows 下确保 utf-8 不被 GBK 控制台破坏)
# ------------------------------------------------------------------------------
def _ensure_stdio_encoding():
    """Windows 控制台默认 GBK，stdout 写中文会抛异常破坏 MCP 响应流。
    SDK 的 stdio_server 会自行包装 buffer 为 utf-8 TextIOWrapper；
    此处只兜底修复 print(..., file=sys.stderr/stdout) 等直接写原始句柄的路径。"""
    if sys.platform != "win32":
        return
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    _ensure_stdio_encoding()
    logger.info(f"MCP Router v{MCP_SERVER_INFO['version']} (mcp SDK 2.x) 启动")
    # v4.1：按配置模式显示配置库位置
    if CFG_MODE == "sqlite":
        logger.info(f"配置库: SQLite 文件 {SQLITE_DB_PATH}（dir_path='*' 为任意目录兜底数据源）")
    else:
        logger.info(f"配置库: MySQL {CFG_HOST}:{CFG_PORT}/{CFG_DB}")
    logger.info("会话级永久缓存（目录变化刷新）")
    logger.info(f"连接池: 最大 {MAX_POOL_SIZE} 个连接, 空闲超时 {POOL_IDLE_TIME}s, 全方言健康检查")
    logger.info(f"结果摘要: 超过 {SUMMARY_THRESHOLD} 行时显示前 {SUMMARY_DISPLAY_ROWS} 行")
    logger.info(f"写操作确认: {'强制(原生弹窗优先,终端兜底)' if WRITE_CONFIRM_MODE == 'on' else '已关闭'} "
                f"[UI通道: {os.environ.get('MCP_WRITE_CONFIRM_UI', 'auto')}]")
    logger.info(f"v4.0 重武器: 事务(空闲{TXN_IDLE_TIMEOUT}s自动回滚,单事务可覆盖至{TXN_MAX_IDLE}s,支持extend_transaction续期) / 多语句批量 / CSV导入导出 / "
                f"审计日志{'开启: ' + AUDIT_LOG_DIR if AUDIT_ENABLED else '关闭'}"
                + (f"（扫描最近 {AUDIT_SCAN_DAYS} 天，保留 {AUDIT_RETAIN_DAYS} 天）"
                   if AUDIT_ENABLED else ""))
    logger.info("v4.5 诊断: explain_query(方言自适应执行计划+自动诊断) / slow_queries(慢查询与在跑语句)")
    logger.info("v4.6 数据源级写审核: mcp_datasource_config.write_confirm=0 的数据源(开发库)"
                " 其 execute/import_csv/dump_database 免确认（审计日志照旧）")

    # 启动连接池清理线程（进程生命周期内清理过期连接）
    start_pool_cleanup_thread()
    # 定期回滚并关闭超过空闲阈值的事务专用连接
    start_transaction_cleanup_thread()

    # 审计日志过期清理（仅当 MCP_AUDIT_LOG_RETAIN_DAYS > 0）。
    # 放在启动时做一次：会话启动频繁，等于定期清理；默认 0 = 永久保留不删。
    if AUDIT_ENABLED and AUDIT_RETAIN_DAYS > 0:
        removed = cleanup_old_audit_logs(AUDIT_RETAIN_DAYS)
        if removed:
            logger.info(f"已清理 {len(removed)} 个过期审计日志文件")

    # SDK 接管 stdio：握手、协议版本协商、tools/list、tools/call、roots 请求
    mcp.run(transport="stdio")
