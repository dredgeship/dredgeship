"""智能体主循环：把「对话 → 决策 → 工具执行 → 结果回填」串成闭环。

一次 ``chat()`` 的完整流程：

1. 写入用户消息 → 组装上下文（系统提示 + 长期摘要 + 最近 N 轮短期记忆）；
2. 调用大模型（流式），把文本增量实时抛给调用方（网页端因此能"边想边显示"）；
3. 若模型返回工具调用：逐个经 MCP 执行，把结果作为 ``role=tool`` 回填，
   回到第 2 步（最多 ``AGENT_MAX_TURNS`` 轮，防止死循环）；
4. 模型给出最终自然语言答复 → 落库 → 必要时压缩历史记忆。

工具执行失败不会中断会话：错误会以结构化文本回填给模型，由模型自己
决定重试、换工具还是向用户解释——这样偶发的网络抖动不会毁掉整轮对话。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Sequence

from agent.llm import LLMClient, LLMError, LLMResult, ToolCall
from agent.mcp_client import MCPToolClient
from agent.memory import MemoryManager
from agent.prompts import build_session_title, build_system_prompt
from common.config import Settings, get_settings
from storage.repository import PreferenceRepository

logger = logging.getLogger(__name__)

__all__ = ["AgentCore", "AgentEvent"]


@dataclass(slots=True)
class AgentEvent:
    """对外统一的事件对象（CLI 直接渲染，Web 层转成 SSE）。"""

    type: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


class AgentCore:
    """记忆 + 模型 + 工具三者的编排者。"""

    def __init__(
        self,
        mcp_client: MCPToolClient,
        llm_client: LLMClient,
        memory: MemoryManager,
        settings: Settings | None = None,
        preferences: PreferenceRepository | None = None,
    ) -> None:
        self.mcp = mcp_client
        self.llm = llm_client
        self.memory = memory
        self.settings = settings or get_settings()
        self.preferences = preferences or PreferenceRepository()
        self.max_turns = max(1, int(self.settings.agent_max_turns))
        self.memory.set_summarizer(self.summarize)

    # ------------------------------------------------------------------ #
    # 构造与生命周期
    # ------------------------------------------------------------------ #
    @classmethod
    async def create(
        cls,
        mcp_client: MCPToolClient | None = None,
        llm_client: LLMClient | None = None,
        memory: MemoryManager | None = None,
        settings: Settings | None = None,
    ) -> "AgentCore":
        """启动 MCP 连接并组装一个可用的智能体。"""
        settings = settings or get_settings()
        client = mcp_client or MCPToolClient()
        if mcp_client is None:
            await client.start()
        instance = cls(
            mcp_client=client,
            llm_client=llm_client or LLMClient(),
            memory=memory
            or MemoryManager(
                recent_turns=settings.memory_recent_turns,
                summary_threshold=settings.memory_summary_threshold,
            ),
            settings=settings,
        )
        return instance

    async def aclose(self) -> None:
        await self.mcp.aclose()
        await self.llm.aclose()

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    def tool_catalog(self) -> list[dict[str, Any]]:
        """工具清单（供前端「设置与关于」页展示）。"""
        return [
            {
                "name": spec.name,
                "description": (spec.description or "").strip().split("\n")[0],
                "parameters": sorted((spec.input_schema.get("properties") or {}).keys()),
            }
            for spec in self.mcp.tools
        ]

    def system_prompt(self) -> str:
        return build_system_prompt(
            tool_count=len(self.mcp.tools),
            preferences=self.preferences.all(),
        )

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    async def chat(
        self,
        text: str,
        session_id: str | None = None,
        *,
        temperature: float = 0.3,
    ) -> AsyncIterator[dict[str, Any]]:
        """执行一轮对话，流式产出事件字典。

        事件类型：``start`` / ``text_delta`` / ``tool_call`` / ``tool_result`` /
        ``final`` / ``done`` / ``error``。
        """
        content = (text or "").strip()
        session_id = session_id or uuid.uuid4().hex[:12]
        if not content:
            yield AgentEvent("error", {"message": "输入内容不能为空"}).to_dict()
            return

        is_new = not self.memory.session_exists(session_id)
        self.memory.ensure_session(session_id, build_session_title(content))
        self.memory.record_user(session_id, content)
        yield AgentEvent("start", {"session_id": session_id, "new_session": is_new}).to_dict()

        started_at = time.perf_counter()
        messages = self.memory.build_context(session_id, self.system_prompt())
        tool_schema = self.mcp.openai_tools()
        usage_total: dict[str, int] = {}
        answer_parts: list[str] = []
        turns = 0

        try:
            for turns in range(1, self.max_turns + 1):
                result = LLMResult()
                async for event in self.llm.chat_stream(
                    messages, tool_schema, temperature=temperature
                ):
                    if event["type"] == "text_delta":
                        # 文本增量实时转发，网页端据此实现"边想边显示"
                        yield AgentEvent("text_delta", {"text": event["text"]}).to_dict()
                    elif event["type"] == "result":
                        result = event["result"]
                _merge_usage(usage_total, result.usage)
                if result.content:
                    answer_parts.append(result.content)

                if not result.has_tool_calls:
                    break

                # 回填 assistant 的 tool_calls 消息（协议要求）
                assistant_payload = {
                    "role": "assistant",
                    "content": result.content or "",
                    "tool_calls": [call.to_openai_message() for call in result.tool_calls],
                }
                messages.append(assistant_payload)
                self.memory.record_tool_call(
                    session_id, [call.to_openai_message() for call in result.tool_calls]
                )

                for call in result.tool_calls:
                    yield AgentEvent(
                        "tool_call",
                        {
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                            "arguments_raw": call.arguments_raw,
                        },
                    ).to_dict()
                    output, ok, summary, elapsed_ms = await self._execute_tool(call)
                    self.memory.record_tool_result(session_id, call.id, call.name, output)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": output,
                        }
                    )
                    yield AgentEvent(
                        "tool_result",
                        {
                            "id": call.id,
                            "name": call.name,
                            "ok": ok,
                            "summary": summary,
                            "duration_ms": elapsed_ms,
                            "output": output[:2000],
                        },
                    ).to_dict()
            else:
                answer_parts.append(
                    "\n\n（已达到单轮工具调用上限，先把已完成的结果给你，"
                    "如果还需要继续处理，请再发一条更具体的指令。）"
                )
        except LLMError as exc:
            logger.warning("对话失败：%s", exc)
            yield AgentEvent("error", {"message": str(exc), "kind": "llm"}).to_dict()
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，保证前端总能收到结束信号
            logger.exception("对话发生未预期异常")
            yield AgentEvent(
                "error", {"message": f"处理失败：{exc}", "kind": "internal"}
            ).to_dict()
            return

        final_text = "".join(answer_parts).strip() or "（我没有生成有效回复，请再说一次）"
        self.memory.record_assistant(session_id, final_text)
        yield AgentEvent("final", {"text": final_text, "session_id": session_id}).to_dict()

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        yield AgentEvent(
            "done",
            {
                "session_id": session_id,
                "turns": turns,
                "elapsed_ms": elapsed_ms,
                "usage": usage_total,
                "model": self.llm.model,
            },
        ).to_dict()

        try:
            await self.memory.compress_if_needed(session_id)
        except Exception as exc:  # noqa: BLE001 - 记忆压缩失败不影响本轮
            logger.warning("记忆压缩失败：%s", exc)

    async def ask(self, text: str, session_id: str | None = None) -> str:
        """便捷方法：只取最终回复文本（CLI 与测试使用）。"""
        final = ""
        async for event in self.chat(text, session_id):
            if event["type"] == "final":
                final = event["text"]
            elif event["type"] == "error":
                raise LLMError(event["message"])
        return final

    # ------------------------------------------------------------------ #
    # 记忆摘要（注入给 MemoryManager 的回调）
    # ------------------------------------------------------------------ #
    async def summarize(self, previous: str, transcript: str) -> str:
        """把旧对话压缩成中文要点，作为长期记忆保存。"""
        user_prompt = (
            f"已有摘要：\n{previous or '（无）'}\n\n新增对话：\n{transcript}\n\n"
            "请输出合并后的新摘要。"
        )
        result = await self.llm.chat(
            [
                {"role": "system", "content": self.memory.build_summary_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        return result.content.strip()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    async def _execute_tool(self, call: ToolCall) -> tuple[str, bool, str, int]:
        """执行单个工具调用，返回 ``(输出文本, 是否成功, 摘要, 耗时毫秒)``。"""
        started = time.perf_counter()
        try:
            output = await self.mcp.call_tool(call.name, call.arguments)
            ok, summary = _summarize_tool_output(output)
        except Exception as exc:  # noqa: BLE001 - 失败也回填给模型，让它自行处理
            logger.warning("工具 %s 执行失败：%s", call.name, exc)
            output = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
            ok, summary = False, str(exc)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return output, ok, summary, elapsed_ms


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def _summarize_tool_output(output: str) -> tuple[bool, str]:
    """从工具返回的 JSON 里提取成败与一句话摘要。"""
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return True, output.strip()[:120] or "已完成"
    if not isinstance(payload, dict):
        return True, str(payload)[:120]
    ok = bool(payload.get("ok", True))
    summary = str(payload.get("message") or payload.get("error") or "").strip()
    if not summary:
        summary = "已完成" if ok else "执行失败"
    return ok, summary[:200]


def _merge_usage(total: dict[str, int], usage: dict[str, Any]) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
