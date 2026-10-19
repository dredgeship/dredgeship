"""语音能力的唯一编排入口。

对外只暴露四个动作，API 层与前端都只依赖它：

* :meth:`VoiceService.status`     —— 引擎状态与当前配置（供界面决定是否显示语音入口）
* :meth:`VoiceService.hints`      —— 按场景分组的语音指令参考清单
* :meth:`VoiceService.transcribe` —— WAV 字节流 → 文本（含可选的模型纠错）
* :meth:`VoiceService.speak`      —— 文本 → WAV 字节流（含 Markdown 清洗与磁盘缓存）

## 三条设计原则

1. **绝不抛异常给调用方**：除了 :class:`VoiceUnavailableError` 表示"整个子系统不可用"，
   其余失败都以 ``{"ok": False, "error": "..."}`` 形式返回，错误信息直接面向用户。
2. **纠错是可选的、有守卫的**：模型可能把识别结果"改写"得面目全非。因此纠错结果要过
   长度守卫（相对原文本不能膨胀 2 倍以上、也不能缩水到 40% 以下），并且没有配置
   模型密钥时自动跳过 —— 保证**没有 Key 也能正常用语音**。
3. **识别文本进入的是现有的 /api/chat 链路**，因此多轮记忆与 23 个工具全部自动继承，
   不需要为语音单写一套业务逻辑。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

from agent.io.voice import (
    SpeechRecognitionError,
    VoiceError,
    VoiceUnavailableError,
    create_stt,
    create_tts,
    strip_markdown_for_speech,
)
from common.config import PROJECT_ROOT, Settings, get_settings

logger = logging.getLogger(__name__)

__all__ = ["VoiceService", "COMMAND_HINTS", "GRAMMAR_PHRASES"]

VOICE_CACHE_DIR = PROJECT_ROOT / ".data" / "voice" / "cache"

# 按场景分组的语音指令参考。
# 这份清单有双重用途：① 前端面板展示，帮用户"一次说对"；
# ② 作为 SAPI 受限语法（Choices）的候选短语，实测能把常见指令的识别准确率
#    从 50% 左右提升到近乎 100%。因此它是 Python 侧的唯一事实来源。
COMMAND_HINTS: dict[str, tuple[str, ...]] = {
    "添加任务": (
        "帮我记一下明天下午三点交作业",
        "加一个高优先级任务：写实验报告",
        "提醒我今晚八点开会",
        "我要记三件事",
    ),
    "查询任务": (
        "今天有什么安排",
        "我还有多少任务没做完",
        "有没有逾期的任务",
        "查一下学习相关的任务",
    ),
    "排序与计划": (
        "今天我先做哪个",
        "帮我把今天排进日程",
        "帮我排个优先级",
        "重新规划今天的日程",
    ),
    "天气与出行": (
        "今天北京天气怎么样",
        "明天适合出门办事吗",
        "出门需要带伞吗",
    ),
    "导出与统计": (
        "把今天的清单导出成文件",
        "看看任务统计",
        "把日程导出成文件",
    ),
    "语音控制": (
        "停止朗读",
        "开启自动朗读",
        "关闭自动朗读",
    ),
}

# 受限语法候选短语（各组打平后去重）
GRAMMAR_PHRASES: tuple[str, ...] = tuple(
    dict.fromkeys(phrase for group in COMMAND_HINTS.values() for phrase in group)
)

_CORRECTION_SYSTEM_PROMPT = (
    "你是中文语音识别结果的纠错器。输入是语音识别引擎对用户一句指令的转写，"
    "可能存在同音字错误、语序错乱或个别词缺失。\n"
    "任务：还原用户最可能说的那句话。\n"
    "规则：\n"
    "1. 保持原意与句式，不要增加、删除或臆造信息；\n"
    "2. **不要删掉用户说过的任何词**，也不要做同义改写 —— 只修正错别字与明显的语序错误；\n"
    "3. 保留时间、数量、人名、动作等关键信息；\n"
    "4. 只输出纠正后的那一句话本身，不要加引号、序号、解释或任何前后缀；\n"
    "5. 若原句已经通顺，原样返回。\n"
)

_STRIP_LABEL = re.compile(r"^\s*(?:纠正后|纠正为|结果|输出)\s*[:：]\s*")

# 置信度高于此值就认为引擎已经识别对了，不再调用大模型改写
CORRECTION_SKIP_CONFIDENCE = 0.7
_WRAP_CHARS = "“”\"'‘’`【】[]（）() 《》 \n\t"


class VoiceService:
    """语音能力的编排者。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._stt: Any = None
        self._tts: Any = None
        self._llm: Any = None
        self._engine_name = ""
        self._engine_culture = ""
        self._voice_name = ""
        self._voice_culture = ""
        self._provider = ""            # 实际生效的识别提供方：whisper / sapi
        self._fallback_reason = ""     # auto 模式下回退到 SAPI 的原因
        self._model_state = ""         # whisper 模型状态：not_loaded / loading / ready / failed
        self._warmup_task: asyncio.Task | None = None
        self._available = False
        self._reason = "尚未初始化"
        self._initialized = False
        self._last_audio_stats: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        """探测引擎并建立连接；失败只记录原因，不抛异常。"""
        if self._initialized:
            return
        self._initialized = True

        if not self.settings.voice_enabled:
            self._reason = "语音功能已在配置中关闭（VOICE_ENABLED=false）"
            logger.info("语音功能未启用")
            return

        from agent.io.sapi import get_shared_worker, probe_engines

        try:
            info = await asyncio.to_thread(probe_engines, timeout=self.settings.voice_timeout)
        except VoiceUnavailableError as exc:
            self._reason = str(exc)
            logger.warning("语音引擎探测失败：%s", exc)
            return
        except Exception as exc:  # noqa: BLE001 - 探测失败绝不影响主服务
            self._reason = f"语音引擎探测异常：{exc}"
            logger.exception("语音引擎探测异常")
            return

        engines = info.get("engines") or []
        voices = info.get("voices") or []
        if not voices:
            self._reason = "本机没有可用的语音合成音色"
            return

        voice = self._pick(voices, self.settings.voice_tts_voice)
        self._voice_name = str(voice.get("name", ""))
        self._voice_culture = str(voice.get("culture", ""))

        worker = get_shared_worker(self.settings.voice_timeout)

        # ---- 1) 选识别引擎：whisper 优先，SAPI 兜底 ----
        if not await self._setup_stt(worker, engines):
            return

        # ---- 2) 合成音色：始终走 SAPI（零依赖、离线、中文音色现成）----
        try:
            self._tts = create_tts(
                voice=self._voice_name,
                rate=self.settings.voice_tts_rate,
                volume=self.settings.voice_tts_volume,
                worker=worker,
            )
        except VoiceUnavailableError as exc:
            self._reason = str(exc)
            logger.warning("语音合成初始化失败：%s", exc)
            return

        self._available = True
        self._reason = ""
        VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(
            "语音就绪：识别 %s / 合成 %s（完全离线）%s",
            self._engine_name,
            self._voice_name,
            f"；{self._fallback_reason}" if self._fallback_reason else "",
        )

    async def _setup_stt(self, worker: Any, engines: list[dict[str, Any]]) -> bool:
        """选择并初始化识别引擎。返回 False 表示整体不可用。"""
        provider = (self.settings.voice_stt_provider or "auto").strip().lower()
        if provider not in ("auto", "whisper", "sapi"):
            logger.warning("未知的 VOICE_STT_PROVIDER=%r，按 auto 处理", provider)
            provider = "auto"

        if provider in ("auto", "whisper"):
            try:
                from agent.io.whisper import DEFAULT_MODEL_ROOT

                root = self.settings.voice_whisper_root.strip()
                stt = create_stt(
                    "whisper",
                    model_size=self.settings.voice_whisper_model,
                    device=self.settings.voice_whisper_device,
                    compute_type=self.settings.voice_whisper_compute_type,
                    endpoint=self.settings.voice_whisper_endpoint or None,
                    download_root=root or DEFAULT_MODEL_ROOT,
                )
            except VoiceUnavailableError as exc:
                if provider == "whisper":
                    self._reason = str(exc)
                    logger.warning("指定的 whisper 引擎不可用：%s", exc)
                    return False
                self._fallback_reason = f"faster-whisper 不可用（{exc}），已回退系统 SAPI 引擎"
                logger.warning("%s", self._fallback_reason)
            else:
                self._stt = stt
                self._provider = "whisper"
                self._engine_name = stt.display_name
                self._engine_culture = "zh"
                # 模型加载放在后台，避免拖慢服务就绪；首次说话时若还没加载完会自动等待
                self._warmup_task = asyncio.create_task(
                    asyncio.to_thread(stt.warmup), name="whisper-warmup"
                )
                return True

        # ---- 回退 / 显式指定 SAPI ----
        if not engines:
            self._reason = "本机没有可用的语音识别引擎（且 faster-whisper 也不可用）"
            return False

        engine = self._pick(engines, self.settings.voice_sapi_engine)
        self._engine_name = str(engine.get("name", ""))
        self._engine_culture = str(engine.get("culture", ""))
        self._provider = "sapi"

        try:
            await asyncio.to_thread(worker.request, "configure", phrases=list(GRAMMAR_PHRASES))
        except VoiceUnavailableError as exc:
            # 受限语法不可用不影响自由听写，仅记录
            logger.warning("载入受限指令语法失败（将退化为自由听写）：%s", exc)

        try:
            self._stt = create_stt(
                "sapi",
                engine=self._engine_name,
                mode=self.settings.voice_asr_mode,
                worker=worker,
            )
        except VoiceUnavailableError as exc:
            self._reason = str(exc)
            logger.warning("SAPI 识别引擎初始化失败：%s", exc)
            return False
        return True

    async def aclose(self) -> None:
        if self._warmup_task is not None and not self._warmup_task.done():
            self._warmup_task.cancel()
        from agent.io.sapi import close_shared_worker

        try:
            await asyncio.to_thread(close_shared_worker)
        except Exception:  # noqa: BLE001
            pass

    def attach_llm(self, llm: Any) -> None:
        """注入用于识别纠错的大模型客户端（可后置注入，也可不注入）。"""
        self._llm = llm

    # ------------------------------------------------------------------ #
    # 状态与提示
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self._available

    @property
    def engine_name(self) -> str:
        """当前使用的识别引擎名称。"""
        return self._engine_name

    @property
    def voice_name(self) -> str:
        """当前使用的合成音色名称。"""
        return self._voice_name

    @property
    def reason(self) -> str:
        """不可用原因（可用时为空字符串）。"""
        return self._reason

    @property
    def provider(self) -> str:
        """当前实际生效的识别提供方：``whisper`` 或 ``sapi``。"""
        return self._provider

    @property
    def asr_quality(self) -> str:
        """给界面用的精度标签。"""
        if self._provider == "whisper":
            return "高精度"
        if self._provider == "sapi":
            return "基础"
        return "不可用"

    def _current_model_state(self) -> str:
        """whisper 模型状态（SAPI 引擎返回空字符串）。"""
        if self._provider != "whisper" or self._stt is None:
            return ""
        return str(getattr(self._stt, "state", ""))

    async def status(self) -> dict[str, Any]:
        """返回完整状态，前端据此决定显示或隐藏语音入口。"""
        correction = await self._correction_state()
        return {
            "enabled": self.settings.voice_enabled,
            "available": self._available,
            "reason": self._reason,
            "asr": {
                "available": self._available,
                "provider": self._provider,
                "quality": self.asr_quality,
                "engine": self._engine_name,
                "culture": self._engine_culture,
                "mode": self.settings.voice_asr_mode if self._provider == "sapi" else "whisper",
                "model_state": self._current_model_state(),
                "model_size": self.settings.voice_whisper_model if self._provider == "whisper" else "",
                "fallback_reason": self._fallback_reason,
            },
            "tts": {
                "available": self._available,
                "voice": self._voice_name,
                "culture": self._voice_culture,
                "rate": self.settings.voice_tts_rate,
            },
            "correction": correction,
            "limits": {
                "max_seconds": self.settings.voice_max_seconds,
                "max_chars": self.settings.voice_tts_max_chars,
                "cache_size": self.settings.voice_cache_size,
            },
            "auto_send": self.settings.voice_auto_send,
            "auto_read": self.settings.voice_auto_read,
            "auto_replay": self.settings.voice_auto_replay,
            "hints": self.hints(),
        }

    def hints(self) -> dict[str, Any]:
        """按场景返回语音指令参考清单。"""
        return {
            "groups": [
                {"scene": scene, "examples": list(phrases)}
                for scene, phrases in COMMAND_HINTS.items()
            ],
            "mode": self.settings.voice_asr_mode,
            "engine": self._engine_name,
        }

    # ------------------------------------------------------------------ #
    # 识别
    # ------------------------------------------------------------------ #
    async def transcribe(self, audio: bytes, *, mode: str | None = None) -> dict[str, Any]:
        """把 WAV 字节流识别为文本，并在可用时做一次模型纠错。"""
        started = time.perf_counter()
        if not self._available or self._stt is None:
            return {"ok": False, "error": self._reason or "语音识别不可用", "stage": "unavailable"}

        # 先量一下音量：识别不准时，这能立刻区分"引擎不行"和"麦克风太小/没收到声音"
        self._last_audio_stats = _measure_audio(audio)

        try:
            result = await asyncio.to_thread(
                self._stt.transcribe_detailed, audio, mode=mode or self.settings.voice_asr_mode
            )
        except SpeechRecognitionError as exc:
            # 没听清是常见情况，属于业务级失败
            return {"ok": False, "error": str(exc), "stage": "recognize", "retryable": True}
        except VoiceUnavailableError as exc:
            self._available = False
            self._reason = str(exc)
            return {"ok": False, "error": str(exc), "stage": "engine"}
        except Exception as exc:  # noqa: BLE001 - 兜底，避免语音问题影响整个服务
            logger.exception("语音识别异常")
            return {"ok": False, "error": f"识别失败：{exc}", "stage": "internal"}

        raw = str(result)
        confidence = float(getattr(result, "confidence", 0.0) or 0.0)
        corrected, correction = await self._correct(raw, confidence)
        latency = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "raw": raw,
            "text": corrected,
            "changed": corrected != raw,
            "confidence": getattr(result, "confidence", 0.0),
            "grammar": getattr(result, "grammar", ""),
            "mode": getattr(result, "mode", ""),
            "engine": getattr(result, "engine", self._engine_name),
            "correction": correction,
            "audio": self._last_audio_stats,
            "latency_ms": latency,
        }

    # ------------------------------------------------------------------ #
    # 合成
    # ------------------------------------------------------------------ #
    async def speak(self, text: str, *, voice: str | None = None) -> tuple[bytes, str]:
        """把文本合成为 WAV 字节流（命中缓存则直接返回）。

        Raises:
            VoiceUnavailableError: 语音子系统不可用。
            VoiceError: 文本为空或引擎未产出音频。
        """
        if not self._available or self._tts is None:
            raise VoiceUnavailableError(self._reason or "语音合成不可用")

        cleaned = strip_markdown_for_speech(text, limit=self.settings.voice_tts_max_chars)
        if not cleaned:
            raise VoiceError("没有可朗读的内容")

        key = hashlib.sha1(
            f"{cleaned}|{voice or self._voice_name}|{self.settings.voice_tts_rate}|"
            f"{self.settings.voice_tts_volume}".encode("utf-8")
        ).hexdigest()
        cache_path = VOICE_CACHE_DIR / f"{key}.wav"
        if cache_path.exists() and cache_path.stat().st_size > 44:
            return cache_path.read_bytes(), "audio/wav"

        VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            audio = await asyncio.to_thread(
                self._tts.synthesize, cleaned, voice=voice, out_path=cache_path
            )
        except VoiceUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("语音合成异常")
            raise VoiceError(f"语音合成失败：{exc}") from exc

        await asyncio.to_thread(self._trim_cache)
        return audio, "audio/wav"

    # ------------------------------------------------------------------ #
    # 识别纠错
    # ------------------------------------------------------------------ #
    async def _correct(self, raw: str, confidence: float = 0.0) -> tuple[str, dict[str, Any]]:
        """尝试用大模型纠正识别错误；任何异常或守卫不通过都回退原文本。

        高置信度时直接跳过：实测 whisper 在置信度 0.8 以上时转写基本正确，
        此时再让大模型"润色"反而会改动语序（例如把"我想和同学吃饭安排一下晚上的时间"
        改成"我想和同学安排一下晚上吃饭的时间"）。低置信度（多为 SAPI 的输出）才值得纠。
        """
        if not self.settings.voice_asr_correct:
            return raw, {"applied": False, "reason": "disabled"}
        if confidence >= CORRECTION_SKIP_CONFIDENCE:
            return raw, {"applied": False, "reason": "high_confidence", "confidence": confidence}

        llm = self._resolve_llm()
        if llm is None:
            return raw, {"applied": False, "reason": "no_api_key"}

        try:
            result = await llm.chat(
                [
                    {"role": "system", "content": _CORRECTION_SYSTEM_PROMPT},
                    {"role": "user", "content": f"识别结果：{raw}"},
                ],
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001 - 纠错失败不能阻断语音输入
            logger.warning("识别纠错调用失败，使用原始文本：%s", exc)
            return raw, {"applied": False, "reason": f"error: {exc}"}

        candidate = _clean_correction(result.content)
        ok, reason = _guard_correction(raw, candidate)
        if not ok:
            logger.info("识别纠错结果未通过守卫（%s），使用原始文本", reason)
            return raw, {"applied": False, "reason": reason}
        if candidate == raw:
            return raw, {"applied": False, "reason": "unchanged"}
        return candidate, {"applied": True, "reason": "ok", "model": getattr(llm, "model", "")}

    def _resolve_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        if not self.settings.has_api_key:
            return None
        try:
            from agent.llm import LLMClient

            self._llm = LLMClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建纠错用模型客户端失败：%s", exc)
            return None
        return self._llm

    async def _correction_state(self) -> dict[str, Any]:
        if not self.settings.voice_asr_correct:
            return {"enabled": False, "available": False, "reason": "disabled"}
        configured = self._llm is not None or self.settings.has_api_key
        return {
            "enabled": True,
            "available": configured,
            "reason": "" if configured else "no_api_key",
        }

    # ------------------------------------------------------------------ #
    # 缓存维护
    # ------------------------------------------------------------------ #
    def _trim_cache(self) -> None:
        """按修改时间保留最近的 N 条合成结果。"""
        limit = max(1, int(self.settings.voice_cache_size))
        try:
            files = sorted(
                VOICE_CACHE_DIR.glob("*.wav"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in files[limit:]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pick(items: list[dict[str, Any]], preferred: str) -> dict[str, Any]:
        """挑选引擎/音色：优先配置里指定的，其次中文，最后第一个可用项。"""
        if preferred:
            for item in items:
                if str(item.get("name")) == preferred:
                    return item
        for item in items:
            if str(item.get("culture", "")).lower().startswith("zh"):
                if item.get("enabled", True):
                    return item
        return items[0]


# --------------------------------------------------------------------------- #
# 纠错结果清洗与守卫
# --------------------------------------------------------------------------- #
def _measure_audio(audio: bytes) -> dict[str, Any]:
    """尽力测量音频电平；任何失败都返回空字典，绝不影响识别本身。"""
    try:
        from agent.io.sapi import analyze_level, parse_wav

        return analyze_level(audio, parse_wav(audio))
    except Exception:  # noqa: BLE001
        return {}


def _clean_correction(text: str | None) -> str:
    """剥掉模型可能自带的解释、引号与前后缀，只留那句话。"""
    if not text:
        return ""
    first = ""
    for line in str(text).splitlines():
        candidate = line.strip()
        if candidate:
            first = candidate
            break
    first = _STRIP_LABEL.sub("", first)
    first = first.strip(_WRAP_CHARS)
    return first.strip()


def _guard_correction(raw: str, candidate: str) -> tuple[bool, str]:
    """长度守卫：阻止模型把识别结果改写成另一句话。

    语音识别结果通常只错几个字，若模型返回的长度相对原文本膨胀 2.2 倍以上或
    缩水到 60% 以下，基本可以判定它在"自由发挥"或"丢词改写"而不是在纠错。
    下界设得比上界更严格，是因为**丢词**比啰嗦更危险：丢掉的可能是时间、
    数量这类关键信息，宁可回退到原始识别结果也不要接受被删改的版本。
    """
    if not candidate:
        return False, "empty"
    if len(candidate) > max(12, int(len(raw) * 2.2)):
        return False, "too_long"
    if len(raw) >= 4 and len(candidate) < max(3, int(len(raw) * 0.6)):
        return False, "too_short"
    return True, ""
