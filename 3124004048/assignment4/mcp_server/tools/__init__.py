"""MCP 工具集汇总入口。

每个子模块暴露 ``register(mcp)``，本模块负责按顺序调用，保证工具注册逻辑
与工具实现分离：新增一组能力只需新增一个模块并在此登记一行。
"""

from typing import Any

__all__ = ["register_all", "TOOL_GROUPS"]

TOOL_GROUPS = ("tasks", "planner", "weather", "reminders", "prefs")


def register_all(mcp: Any) -> None:
    """把全部工具组注册到给定的 FastMCP 实例。"""
    from importlib import import_module

    for module_name in TOOL_GROUPS:
        module = import_module(f"mcp_server.tools.{module_name}")
        module.register(mcp)
