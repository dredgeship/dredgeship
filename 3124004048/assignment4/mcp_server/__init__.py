"""MCP Server 包：以官方 MCP Python SDK 暴露「待办管理」能力。"""

__all__ = ["build_server"]


def build_server():
    """惰性构建 FastMCP 实例（避免 import 时即拉起全部工具依赖）。"""
    from mcp_server.server import build_server as _build

    return _build()
