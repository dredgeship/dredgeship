"""智能体层：LLM 调用、MCP 工具调度、记忆管理与主循环。"""

from typing import Any

__all__ = ["AgentCore", "MemoryManager", "LLMClient", "MCPToolClient", "ToolSpec"]


def __getattr__(name: str) -> Any:
    """惰性导出，避免仅使用子模块时拉起整个依赖链。"""
    if name == "AgentCore":
        from agent.core import AgentCore

        return AgentCore
    if name == "MemoryManager":
        from agent.memory import MemoryManager

        return MemoryManager
    if name == "LLMClient":
        from agent.llm import LLMClient

        return LLMClient
    if name == "MCPToolClient":
        from agent.mcp_client import MCPToolClient

        return MCPToolClient
    if name == "ToolSpec":
        from agent.mcp_client import ToolSpec

        return ToolSpec
    raise AttributeError(f"module 'agent' has no attribute {name!r}")
