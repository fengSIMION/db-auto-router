#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Router v4.7 - 生产级多数据源查询路由服务（薄启动入口）
支持 MySQL/达梦/PostgreSQL/Oracle/GaussDB(高斯)

v4.0 起本体拆分为包 `db_auto_router/`（config/logcfg/errors/util/audit/sqlguard/
workspace/datasources/pool/directconn/transactions/csvio/readengine/writeengine/
introspection/confirm/tools/server），本文件仅负责：
1. 加载 .env（路径必须保持在基目录解析）
2. 打印启动 banner（保持与旧版 stderr 日志一致）
3. 转发到 db_auto_router.server.main()

行为与拆分前完全等价；`~/.claude.json` 中的启动命令不变。
"""

__author__ = "fengkeju"
import os
import sys
from pathlib import Path

# 加载环境变量（优先从 .env 文件）
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✅ 已加载 .env 配置: {env_path}", file=sys.stderr)
except ImportError:
    print(f"⚠️ 未安装 python-dotenv，使用默认配置", file=sys.stderr)

print(f"🔍 PYTHONPATH: {os.environ.get('PYTHONPATH')}", file=sys.stderr)
print(f"🔍 当前 cwd: {os.getcwd()}", file=sys.stderr)

from db_auto_router.server import main

if __name__ == "__main__":
    main()
