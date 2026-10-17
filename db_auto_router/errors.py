# -*- coding: utf-8 -*-
"""共享异常类型——原 2234–2242 行（放在叶子模块避免循环依赖）"""

__author__ = "fengkeju"


# 低层 SDK 的 call_tool 处理器捕获工具异常 → CallToolResult(isError=True)，
# 错误原文作为工具输出返回给模型（db-diagnose 诊断闭环依赖此行为）。
# 为让错误文本干净（不带 SDK 自动前缀），在工具层做统一包装：
class ToolFailure(Exception):
    """工具执行失败。抛出后由 SDK 转为 isError=True 结果，文本为 str(exc)"""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message
