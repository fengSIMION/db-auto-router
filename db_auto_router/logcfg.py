# -*- coding: utf-8 -*-
"""日志配置：仅输出到 stderr，避免污染 MCP 通信通道——原 95–103 行"""

__author__ = "fengkeju"
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger("MCP_Router")
