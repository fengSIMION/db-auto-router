# -*- coding: utf-8 -*-
"""MCP SDK 2.x 启动冒烟测试。

背景：官方 mcp SDK 2.x 删掉了 `mcp.server.fastmcp`（FastMCP 改名 MCPServer、
搬到 `mcp.server.mcpserver`），协议类型拆到独立的 `mcp_types` 包。
requirements.txt 不写上界时 `pip install` 会装到 2.x，旧 import 启动即
ModuleNotFoundError（墓碑模块带迁移提示）。

本测试钉住三件事，任何一件回归都意味着「新机器装上就跑不起来」：
  1. server 模块可导入（旧代码在 2.x 上必崩在这一步）
  2. 17 个工具全部注册且可列举
  3. initialize 上报的 version 是项目版本，不被 SDK 自己的版本号顶替
"""
import asyncio
import os
import sys
import tempfile

# 按本文件位置定位仓库根（tests/ 的上一级），换机器/换目录都不用改这里
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 不往真实 logs/ 里写东西
os.environ["MCP_AUDIT_LOG_DIR"] = tempfile.mkdtemp(prefix="audit_startup_")

import db_auto_router.server as S
from db_auto_router.config import MCP_SERVER_INFO

ok = True


def chk(label, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print("  %s %s %s" % ("✅" if cond else "❌", label, extra))


print("=== 1. server 模块导入（2.x 上旧 import 会抛 ModuleNotFoundError）===")
chk("db_auto_router.server 导入成功", S is not None)

print("=== 2. 工具注册完整性 ===")
tools = asyncio.run(S.mcp.list_tools())
names = sorted(t.name for t in tools)
expected = sorted([
    "query", "execute", "list_datasources", "refresh_datasources", "search_tables",
    "get_schema_info", "begin_transaction", "commit_transaction", "rollback_transaction",
    "list_transactions", "extend_transaction", "export_csv", "import_csv",
    "dump_database", "query_audit_log", "explain_query", "slow_queries",
])
chk("17 个工具全部注册", names == expected, "→ %d 个: %s" % (len(names), names))

print("=== 3. version 上报（低层 Server 的 version 字段）===")
# 1.x 是 FastMCP._mcp_server，2.x 是 MCPServer._lowlevel_server；
# 两条路都要拿到「上报给客户端的版本号 = 项目版本」。
low = getattr(S.mcp, "_lowlevel_server", None) or getattr(S.mcp, "_mcp_server", None)
chk("低层 Server.version 等于项目版本",
    getattr(low, "version", None) == MCP_SERVER_INFO["version"],
    "→ %r (期望 %r)" % (getattr(low, "version", None), MCP_SERVER_INFO["version"]))

print()
print("=" * 56)


# pytest 入口（与 test_mcp_fixes.py 同一约定：模块级 chk 汇总成一个断言）
def test_mcp2_startup():
    assert ok, "test_mcp2_startup 有失败项，见上方 ❌ 明细"


if __name__ == "__main__":
    print("✅ 全部通过" if ok else "❌ 有失败项")
    sys.exit(0 if ok else 1)
