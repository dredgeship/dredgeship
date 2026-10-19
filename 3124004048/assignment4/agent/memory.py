"""多轮对话记忆（加分项 ①）。

记忆分两层：

* **短期记忆**：最近 N 轮「用户 ↔ 助理」的原始对话，原样注入模型上下文，
  保证指代消解（"把它排到明天"里的"它"）与语气连贯。
* **长期记忆**：更早的对话压缩成一段中文摘要，持久化在 ``sessions.summary``；
  同时用「水位线」记录已压缩到哪条消息，避免重复压缩与丢失。

设计取舍：

1. **上下文只注入用户/助理的成品文本**，不注入中间的工具调用往返。
   原因有两个：一是 OpenAI 兼容协议要求 ``assistant.tool_calls`` 必须紧跟
   对应的 ``tool`` 消息，窗口截断很容易产生"孤立 tool 消息"导致 400；
   二是任务状态的唯一事实来源是 SQLite，模型每轮可用 ``list_tasks`` 重新读取，
   无需依赖历史工具结果。完整轨迹仍会落库，供界面与调试使用。
2. **水位线 + 阈值触发**：每轮仅注入「系统提示 + 摘要 + 最近 N 轮」，
   单轮 token 消耗近似常量，长会话下成本与延迟不随轮数线性增长。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Sequence

from storage.repository import ConversationRepository, PreferenceRepository

logger = logging.getLogger(__name__)

__all__ = ["MemoryManager"]

Summarizer = Callable[[str, str], Awaitable[str]]
"""摘要器签名：``(已有摘要, 新增对话文本) -> 新摘要``。由 LLM 层注入。"""

_WATERMARK_PREFIX = "_memory_watermark:"

_SUMMARY_PROMPT = (
    "你是对话记忆压缩器。请把下面的对话压缩成简洁的中文要点，用于后续会话恢复上下文。\n"
    "必须保留：用户身份与偏好、已明确承诺或拒绝的事项、提到的关键时间与地点、"
    "尚未完成的事项、重要的任务 ID 与标题。\n"
    "不要编造未出现的信息，不要输出客套话，用短句分条，总长度不超过 300 字。\n"
)


class MemoryManager:
    """负责构建模型上下文、写入消息、触发摘要压缩。"""

    def __init__(
        self,
        conversations: ConversationRepository | None = None,
        preferences: PreferenceRepository | None = None,
        *,
        recent_turns: int = 8,
        summary_threshold: int = 24,
        summarizer: Summarizer | None = None,
    ) -> None:
        self.conversations = conversations or ConversationRepository()
        self.preferences = preferences or PreferenceRepository()
        self.recent_turns = max(2, int(recent_turns))
        self.summary_threshold = max(4, int(summary_threshold))
        self._summarizer = summarizer

    def set_summarizer(self, summarizer: Summarizer | None) -> None:
        """注入摘要器（Agent 启动后由 LLM 层提供，避免模块间循环 import）。"""
        self._summarizer = summarizer

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    def ensure_session(self, session_id: str, title: str | None = None) -> dict[str, Any]:
        return self.conversations.ensure_session(session_id, title)

    def session_exists(self, session_id: str) -> bool:
        return self.conversations.get_session(session_id) is not None

    def session_history(self, session_id: str) -> list[dict[str, Any]]:
        """完整消息轨迹（供界面展示历史）。"""
        return self.conversations.recent_messages(session_id, limit=200)

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def record_user(self, session_id: str, text: str) -> None:
        self.conversations.add_message(session_id, "user", text)

    def record_assistant(self, session_id: str, text: str) -> None:
        if text:
            self.conversations.add_message(session_id, "assistant", text)

    def record_tool_call(self, session_id: str, tool_calls: Sequence[dict[str, Any]]) -> None:
        """记录模型发起的工具调用（仅供界面与审计，不参与后续上下文注入）。"""
        if tool_calls:
            self.conversations.add_message(session_id, "assistant", "", tool_calls=list(tool_calls))

    def record_tool_result(
        self, session_id: str, tool_call_id: str, name: str, result: str
    ) -> None:
        self.conversations.add_message(
            session_id, "tool", result, tool_call_id=tool_call_id, name=name
        )

    # ------------------------------------------------------------------ #
    # 构建上下文
    # ------------------------------------------------------------------ #
    def build_context(self, session_id: str, system_prompt: str) -> list[dict[str, Any]]:
        """组装本轮送给模型的消息列表：``system`` + 摘要 + 最近 N 轮。"""
        summary = self.conversations.get_summary(session_id)
        system_content = system_prompt
        if summary:
            system_content = f"{system_prompt}\n\n## 历史对话摘要（长期记忆）\n{summary}"

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
        messages.extend(self._recent_dialogue(session_id))
        return messages

    # ------------------------------------------------------------------ #
    # 摘要压缩
    # ------------------------------------------------------------------ #
    async def compress_if_needed(self, session_id: str) -> bool:
        """当未压缩消息超过阈值时，把较旧部分压缩进摘要。返回是否发生压缩。"""
        if self._summarizer is None:
            return False

        watermark = int(self.preferences.get(_WATERMARK_PREFIX + session_id, 0) or 0)
        rows = self.conversations.db.query(
            "SELECT id FROM messages WHERE session_id = ? AND id > ? ORDER BY id ASC",
            (session_id, watermark),
        )
        pending = len(rows)
        if pending <= self.summary_threshold:
            return False

        # 保留最近 recent_turns 条消息不压缩，其余进入摘要
        keep_from_index = max(0, pending - self.recent_turns * 2)
        if keep_from_index <= 0:
            return False
        cutoff_id = int(rows[keep_from_index - 1]["id"])

        older = self.conversations.messages_upto(session_id, cutoff_id)
        older = [m for m in older if m.get("content")]
        if not older:
            self.preferences.set(_WATERMARK_PREFIX + session_id, cutoff_id)
            return False

        transcript = _render_transcript(older)
        previous = self.conversations.get_summary(session_id)

        try:
            new_summary = await self._summarizer(previous, transcript)
        except Exception as exc:  # 摘要失败不应阻断主流程
            logger.warning("记忆摘要失败，保留原摘要: %s", exc)
            return False

        if new_summary:
            self.conversations.set_summary(session_id, new_summary.strip())
        self.preferences.set(_WATERMARK_PREFIX + session_id, cutoff_id)
        logger.info("会话 %s 记忆已压缩至消息 #%s", session_id, cutoff_id)
        return True

    def build_summary_prompt(self) -> str:
        return _SUMMARY_PROMPT

    def reset_session(self, session_id: str) -> None:
        self.preferences.delete(_WATERMARK_PREFIX + session_id)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _recent_dialogue(self, session_id: str) -> list[dict[str, Any]]:
        """取最近 N 轮成品对话（跳过工具往返与空内容消息）。"""
        raw = self.conversations.recent_messages(session_id, limit=self.recent_turns * 4)
        dialogue = [
            {"role": m["role"], "content": m["content"]}
            for m in raw
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
        ]
        return dialogue[-self.recent_turns * 2:]


def _render_transcript(messages: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = {"user": "用户", "assistant": "助理", "tool": "工具"}.get(
            message.get("role", ""), message.get("role", "?")
        )
        content = (message.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 400:
            content = content[:400] + "…"
        lines.append(f"{role}：{content}")
    return "\n".join(lines)
