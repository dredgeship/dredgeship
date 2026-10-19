"""用户偏好工具组：长期记忆的显式读写。

与「对话摘要」这类隐式记忆互补：偏好是**用户显式声明且长期有效**的设定
（默认城市、工作时段、常用标签、称呼等），跨会话保留。

落地位置为 SQLite ``preferences`` 表，因此进程重启、换会话都不会丢。
"""

from typing import Any

from mcp_server.tools.base import fail, ok, preference_repo, tool

__all__ = ["register"]


def register(mcp: Any) -> None:
    @tool(mcp)
    def remember_preference(key: str, value: str, reason: str = "") -> str:
        """记住一条长期有效的用户偏好，供以后的会话使用。

        当用户说出「以后都……」「我一般……」「默认……」这类长期设定，
        或纠正了你的默认行为时调用。

        Args:
            key: 偏好键名，使用简短英文或中文短语，例如 ``default_city``、
                ``work_start``、``preferred_priority``、``称呼``。
            value: 偏好值，例如 ``上海``、``09:30``、``high``。
            reason: 记录该偏好的原因，便于以后解释（可选）。
        """
        key = (key or "").strip()
        if not key:
            return fail("key 不能为空")
        repo = preference_repo()
        previous = repo.get(key)
        repo.set(key, str(value))
        if reason:
            repo.set(f"{key}__reason", str(reason))
        message = f"已记住偏好：{key} = {value}"
        if previous is not None and str(previous) != str(value):
            message += f"（覆盖原值 {previous}）"
        return ok(message=message, key=key, value=str(value), previous=previous)

    @tool(mcp)
    def recall_preference(key: str) -> str:
        """读取某一条已记住的偏好。

        Args:
            key: 偏好键名。
        """
        repo = preference_repo()
        if key not in repo.all():
            return fail(f"没有名为 {key} 的偏好记录", code="not_found")
        return ok(message=f"{key} = {repo.get(key)}", key=key, value=repo.get(key))

    @tool(mcp)
    def list_preferences() -> str:
        """列出全部已记住的用户偏好。在需要参考长期设定时调用。"""
        prefs = {
            k: v for k, v in preference_repo().all().items() if not k.startswith("_")
        }
        return ok(message=f"共 {len(prefs)} 条偏好记录", preferences=prefs)

    @tool(mcp)
    def forget_preference(key: str) -> str:
        """删除一条用户偏好（用户说「不用再记住……」时调用）。

        Args:
            key: 偏好键名。
        """
        repo = preference_repo()
        if key not in repo.all():
            return fail(f"没有名为 {key} 的偏好记录", code="not_found")
        repo.delete(key)
        repo.delete(f"{key}__reason")
        return ok(message=f"已忘记偏好 {key}", key=key)
