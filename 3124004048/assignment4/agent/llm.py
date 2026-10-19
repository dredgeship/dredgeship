"""大模型客户端：DeepSeek（OpenAI 兼容接口）封装。

职责边界：本模块**只负责与模型通信**（流式增量、工具调用增量聚合、错误翻译），
不含任何工具调度逻辑——调度在 :mod:`agent.core`，便于单独替换模型服务商。

要点：
1. 统一流式：无论最终是文本还是工具调用，都走同一条流式通道，
   这样网页端可以边生成边显示，工具调用也不会被"先问一次再要一次"打断。
2. 工具调用增量聚合：OpenAI 兼容协议把 ``tool_calls`` 拆成多个 delta 下发，
   需要按 ``index`` 拼接 ``name`` 与 ``arguments`` 片段。
3. 错误翻译：把 SDK 异常转成中文可读提示，便于直接展示给用户。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from common.config import get_settings

logger = logging.getLogger(__name__)

__all__ = ["LLMClient", "LLMError", "LLMResult", "ToolCall"]


class LLMError(RuntimeError):
    """大模型调用失败（凭据、网络、限流、服务端错误）。"""


@dataclass(slots=True)
class ToolCall:
    """一次工具调用请求。"""

    id: str
    name: str
    arguments_raw: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_openai_message(self) -> dict[str, Any]:
        """还原为回填给模型的消息格式。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments_raw or "{}"},
        }

    def to_event(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class LLMResult:
    """一轮模型调用的最终结果。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMClient:
    """DeepSeek Chat Completions 的异步封装。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        settings = get_settings()
        self.model = model or settings.model
        self.api_key = (api_key if api_key is not None else settings.api_key).strip()
        self.base_url = base_url or settings.base_url
        self.timeout = timeout if timeout is not None else settings.llm_timeout
        self.max_retries = max_retries if max_retries is not None else settings.llm_max_retries
        self._client: AsyncOpenAI | None = None

    # ------------------------------------------------------------------ #
    # 客户端
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> AsyncOpenAI:
        if not self.api_key:
            raise LLMError(get_settings().missing_api_key_hint())
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ------------------------------------------------------------------ #
    # 调用
    # ------------------------------------------------------------------ #
    async def chat_stream(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.3,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式调用，产出 ``text_delta`` / ``result`` 两类事件。

        事件格式：
            ``{"type": "text_delta", "text": "..."}``
            ``{"type": "result", "result": LLMResult}``
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        content_parts: list[str] = []
        tool_buffer: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] = {}

        try:
            stream = await self.client.chat.completions.create(**payload)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = _usage_to_dict(chunk.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is None:
                    continue

                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    yield {"type": "text_delta", "text": text}

                for partial in getattr(delta, "tool_calls", None) or []:
                    slot = tool_buffer.setdefault(
                        partial.index or 0, {"id": "", "name": "", "arguments": ""}
                    )
                    if getattr(partial, "id", None):
                        slot["id"] = partial.id
                    function = getattr(partial, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            slot["name"] += function.name
                        if getattr(function, "arguments", None):
                            slot["arguments"] += function.arguments

                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        except AuthenticationError as exc:
            raise LLMError("大模型鉴权失败：API Key 无效或已过期，请检查 .env 中的 DEEPSEEK_API_KEY") from exc
        except RateLimitError as exc:
            raise LLMError("大模型限流或余额不足，请稍后重试或检查账户余额") from exc
        except APITimeoutError as exc:
            raise LLMError(f"大模型响应超时（{self.timeout:g} 秒），请重试或改用更短的输入") from exc
        except APIConnectionError as exc:
            raise LLMError(
                f"无法连接大模型服务（{self.base_url}），请检查网络或代理设置"
            ) from exc
        except APIStatusError as exc:
            detail = _read_error_detail(exc)
            raise LLMError(f"大模型服务返回错误 {exc.status_code}：{detail}") from exc
        except Exception as exc:  # noqa: BLE001 - 兜底，避免异常穿透到 UI
            raise LLMError(f"大模型调用失败：{exc}") from exc

        result = LLMResult(
            content="".join(content_parts),
            tool_calls=_build_tool_calls(tool_buffer),
            finish_reason=finish_reason,
            usage=usage,
            model=self.model,
        )
        yield {"type": "result", "result": result}

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.3,
    ) -> LLMResult:
        """非流式便捷封装（内部仍走流式通道，保证行为一致）。"""
        result: LLMResult | None = None
        async for event in self.chat_stream(messages, tools, temperature=temperature):
            if event["type"] == "result":
                result = event["result"]
        if result is None:  # pragma: no cover - 理论上不可达
            raise LLMError("大模型未返回任何结果")
        return result


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def _build_tool_calls(buffer: dict[int, dict[str, str]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index in sorted(buffer):
        slot = buffer[index]
        name = slot["name"].strip()
        if not name:
            continue
        raw = slot["arguments"].strip() or "{}"
        calls.append(
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=name,
                arguments_raw=raw,
                arguments=_safe_json_object(raw),
            )
        )
    return calls


def _safe_json_object(raw: str) -> dict[str, Any]:
    """解析模型生成的参数 JSON；解析失败时降级为空对象并由工具层报错。"""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("工具参数不是合法 JSON，已降级为空对象：%s", raw[:200])
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if value is not None:
            data[key] = value
    return data


def _read_error_detail(exc: APIStatusError) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    text = str(getattr(exc, "message", "") or exc)
    return text[:200]
