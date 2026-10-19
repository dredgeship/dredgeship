"""语音输入 / 输出（加分项 ④）—— 接口定义、文本清洗与工厂。

## 实现路线

默认实现走 **Windows SAPI 离线引擎**（见 :mod:`agent.io.sapi`）：

* 识别：``System.Speech.Recognition`` + 本机已安装的中文识别引擎
* 合成：``System.Speech.Synthesis`` + 本机已安装的中文音色

因此整条链路 **不需要任何 API Key、不需要下载模型、不需要联网**，
即使没有配置大模型密钥也能正常说话与朗读（只是少了"识别结果纠错"这一层）。

## 为什么接口是同步的

``SpeechToText`` / ``TextToSpeech`` 保持同步签名，因为底层 SAPI 调用本身就是
阻塞式的。需要异步的调用方（FastAPI）用 ``asyncio.to_thread`` 包一层即可，
这样 CLI、Web、脚本三种入口都能直接复用同一套实现。

## 降级策略

* 本机没有中文引擎 → :func:`create_stt` / :func:`create_tts` 抛
  :class:`VoiceUnavailableError`，调用方据此隐藏入口而不是报错崩溃。
* 想要接入云端或本地大模型语音（Whisper、FunASR、edge-tts 等）时，只要实现
  下面两个 Protocol 并在工厂里增加一个分支即可，**Agent 主循环一行都不用改**。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "SpeechToText",
    "TextToSpeech",
    "VoiceError",
    "VoiceUnavailableError",
    "SpeechRecognitionError",
    "TranscriptionResult",
    "create_stt",
    "create_tts",
    "strip_markdown_for_speech",
    "speech_ready",
]

# 朗读文本长度上限：超过则截断，避免一次念完整篇简报
MAX_SPEECH_CHARS = 800

# Markdown 结构符号：这些字符如果直接念出来会变成"星号星号""井号"等噪音
_INLINE_CODE = re.compile(r"`([^`]*)`")
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]*)\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_LIST = re.compile(r"^\s*(?:[-*+·]|\d+[.、)])\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|(.+?)\|\s*$", re.MULTILINE)
# emoji 与常见装饰性符号（朗读时没有意义）
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d\u2190-\u21ff]"
)
_WHITESPACE = re.compile(r"[ \t\u3000]+")
_BLANK_LINES = re.compile(r"\n{2,}")


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class VoiceError(Exception):
    """语音相关错误的基类。"""


class VoiceUnavailableError(VoiceError):
    """语音子系统不可用（缺少引擎、缺少 PowerShell、进程无法启动等）。

    调用方应当捕获它并**优雅降级**（隐藏语音入口），而不是把它当成致命错误。
    """


class SpeechRecognitionError(VoiceError):
    """识别本身失败（没听清、音频格式不对等），属于可重试的业务级错误。"""


# --------------------------------------------------------------------------- #
# 协议
# --------------------------------------------------------------------------- #
@runtime_checkable
class SpeechToText(Protocol):
    """语音转文字。"""

    def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> str:
        """把音频字节流转写为文本。"""
        ...


@runtime_checkable
class TextToSpeech(Protocol):
    """文字转语音。"""

    def synthesize(self, text: str, *, voice: str | None = None) -> bytes:
        """把文本合成为音频字节流（WAV）。"""
        ...


class TranscriptionResult(str):
    """识别结果。

    刻意继承 ``str``，这样它既能直接当字符串用（满足 :class:`SpeechToText`
    协议的返回类型），又能携带置信度、识别模式等附加信息供上层展示与调试。
    """

    confidence: float
    mode: str
    engine: str
    grammar: str

    def __new__(
        cls,
        text: str,
        *,
        confidence: float = 0.0,
        mode: str = "",
        engine: str = "",
        grammar: str = "",
    ) -> "TranscriptionResult":
        instance = super().__new__(cls, text)
        instance.confidence = float(confidence)
        instance.mode = mode
        instance.engine = engine
        instance.grammar = grammar
        return instance


# --------------------------------------------------------------------------- #
# 文本清洗
# --------------------------------------------------------------------------- #
def strip_markdown_for_speech(text: str, *, limit: int = MAX_SPEECH_CHARS) -> str:
    """把 Markdown 文本清洗成适合朗读的纯文本。

    助理的回复是 Markdown（含 ``**加粗**``、表格、列表、表情符号）。若原样交给
    语音合成，会念出"星号星号""竖线"之类的噪音。这里做一次面向"听感"的清洗：
    保留语义与标点，去掉一切视觉装饰。

    Args:
        text: 原始 Markdown 文本。
        limit: 最长保留字符数，超出部分截断并加省略提示。
    """
    if not text:
        return ""

    cleaned = str(text)
    cleaned = _CODE_FENCE.sub(" ", cleaned)
    cleaned = _IMAGE.sub(r"\1", cleaned)
    cleaned = _LINK.sub(r"\1", cleaned)
    cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = _HR.sub("", cleaned)
    # 表格：分隔行整行丢弃，数据行改成顿号连接的句子
    cleaned = _TABLE_SEP.sub("", cleaned)
    cleaned = _TABLE_ROW.sub(lambda m: "，".join(part.strip() for part in m.group(1).split("|")), cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _QUOTE.sub("", cleaned)
    cleaned = _LIST.sub("", cleaned)
    cleaned = _EMPHASIS.sub("", cleaned)
    cleaned = _EMOJI.sub("", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    cleaned = _BLANK_LINES.sub("\n", cleaned)
    cleaned = cleaned.strip()

    if limit and len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "……内容较长，剩余部分请查看文字。"
    return cleaned


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def create_stt(provider: str = "auto", **options: Any) -> SpeechToText:
    """创建语音识别实现。

    Args:
        provider:
            ``whisper`` —— faster-whisper 本地模型，精度高，需一次性下载模型；
            ``sapi``    —— Windows 自带引擎，零依赖，精度一般；
            ``auto``    —— 优先 whisper，依赖或模型不可用时自动回退 sapi。
        options: 透传给具体实现的参数。两种实现的参数并不相同，因此自动回退时
            会按目标实现的签名过滤掉无关参数（见 :func:`_filter_kwargs`）。

    Raises:
        VoiceUnavailableError: 指定的提供方不可用（``auto`` 不会抛，只会回退）。
    """
    name = (provider or "auto").strip().lower()
    if name in ("whisper", "faster-whisper", "faster_whisper", "auto"):
        try:
            from agent.io.whisper import WhisperSpeechToText

            return WhisperSpeechToText(**_filter_kwargs(WhisperSpeechToText.__init__, options))
        except VoiceUnavailableError:
            if name != "auto":
                raise
            logger.info("faster-whisper 不可用，自动回退 Windows SAPI 识别引擎")
    if name in ("sapi", "windows", "auto"):
        from agent.io.sapi import SapiSpeechToText

        return SapiSpeechToText(**_filter_kwargs(SapiSpeechToText.__init__, options))
    raise VoiceUnavailableError(
        f"暂不支持语音识别提供方 {provider!r}（可选 auto / whisper / sapi）；"
        "如需接入云端 ASR，请实现 SpeechToText 协议并在此注册。"
    )


def _filter_kwargs(func: Any, options: dict[str, Any]) -> dict[str, Any]:
    """按目标函数的签名过滤参数，避免两种引擎的参数互相污染。"""
    import inspect

    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
        return dict(options)
    return {key: value for key, value in options.items() if key in parameters}


def create_tts(provider: str = "auto", **options: Any) -> TextToSpeech:
    """创建语音合成实现（参数含义同 :func:`create_stt`）。"""
    name = (provider or "auto").strip().lower()
    if name not in ("auto", "sapi", "windows"):
        raise VoiceUnavailableError(
            f"暂不支持语音合成提供方 {provider!r}；"
            "如需接入云端或本地大模型语音，请实现 TextToSpeech 协议并在此注册。"
        )
    from agent.io.sapi import SapiTextToSpeech

    return SapiTextToSpeech(**options)


def speech_ready() -> bool:
    """快速判断本机语音能力是否可用（不抛异常，供状态展示使用）。"""
    try:
        from agent.io.sapi import probe_engines

        info = probe_engines()
    except Exception:  # noqa: BLE001 - 探测失败即视为不可用
        return False
    return bool(info.get("engines") or info.get("voices"))
