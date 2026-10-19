"""MCP 工具公共基础设施。

三个统一约定，保证 Agent 侧解析逻辑简单且不会因工具异常而中断会话：

1. **统一返回**：所有工具都返回 JSON 字符串，形如
   ``{"ok": true, "message": "...", ...}`` 或 ``{"ok": false, "error": "..."}``。
   模型可以稳定地读懂成败与原因，无需猜测格式。
2. **异常兜底**：``@tool`` 装饰器捕获所有异常并转成结构化错误，
   绝不把异常抛回协议层（否则客户端会收到 JSON-RPC error，模型无法自我纠正）。
3. **仓储单例**：每个进程内共享同一个 SQLite 连接，避免反复建连。
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
from typing import Any, Callable, TypeVar

from storage import ConversationRepository, PreferenceRepository, TaskRepository

logger = logging.getLogger(__name__)

__all__ = [
    "ok",
    "fail",
    "json_result",
    "tool",
    "task_repo",
    "conversation_repo",
    "preference_repo",
]

F = TypeVar("F", bound=Callable[..., Any])

_TASK_REPO: TaskRepository | None = None
_CONV_REPO: ConversationRepository | None = None
_PREF_REPO: PreferenceRepository | None = None


# --------------------------------------------------------------------------- #
# 返回格式
# --------------------------------------------------------------------------- #
def json_result(payload: Any) -> str:
    """序列化为 JSON 字符串（保留中文，兼容 datetime 等非原生类型）。"""
    return json.dumps(payload, ensure_ascii=False, default=str)


def ok(**payload: Any) -> str:
    """成功返回。约定必须包含 ``message``（给模型和用户看的一句话总结）。"""
    payload.setdefault("message", "操作成功")
    return json_result({"ok": True, **payload})


def fail(message: Any, *, code: str = "tool_error", **payload: Any) -> str:
    """失败返回。``message`` 会直接进入模型上下文，因此必须写给模型看。"""
    return json_result({"ok": False, "error": str(message), "code": code, **payload})


# --------------------------------------------------------------------------- #
# 装饰器
# --------------------------------------------------------------------------- #
def tool(mcp: Any) -> Callable[[F], F]:
    """把函数注册为 MCP 工具并附加异常兜底。

    通过显式设置 ``__signature__``，保证 FastMCP 生成的 JSON Schema 与
    原函数签名完全一致（``functools.wraps`` 的 ``__wrapped__`` 也保留，
    双重保险）。
    """

    def decorator(func: F) -> F:
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return func(*args, **kwargs)  # type: ignore[misc]
            except ValueError as exc:
                logger.info("工具 %s 参数/业务校验失败: %s", func.__name__, exc)
                return fail(exc, code="invalid_argument")
            except Exception as exc:  # noqa: BLE001 - 兜底，绝不允许抛回协议层
                logger.exception("工具 %s 执行异常", func.__name__)
                return fail(f"{type(exc).__name__}: {exc}", code="internal_error")

        wrapper.__signature__ = signature  # type: ignore[attr-defined]
        wrapper.__doc__ = func.__doc__
        return mcp.tool()(wrapper)  # type: ignore[return-value]

    return decorator


# --------------------------------------------------------------------------- #
# 仓储访问
# --------------------------------------------------------------------------- #
def task_repo() -> TaskRepository:
    global _TASK_REPO
    if _TASK_REPO is None:
        _TASK_REPO = TaskRepository()
    return _TASK_REPO


def conversation_repo() -> ConversationRepository:
    global _CONV_REPO
    if _CONV_REPO is None:
        _CONV_REPO = ConversationRepository()
    return _CONV_REPO


def preference_repo() -> PreferenceRepository:
    global _PREF_REPO
    if _PREF_REPO is None:
        _PREF_REPO = PreferenceRepository()
    return _PREF_REPO
