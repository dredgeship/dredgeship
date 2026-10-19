"""faster-whisper 本地语音识别实现（高精度引擎）。

## 为什么加这个引擎

原先唯一可用的引擎是 Windows 自带的 `Microsoft Speech Recognizer 8.0`（2006 年前后），
实测在**音质完美的合成音频**上也只有约 83% 字面准确率，且句子越长越崩；真实人声更差 ——
用户反馈"说的话被扭曲"正是它的典型表现。

faster-whisper 把 OpenAI Whisper 模型跑在 CTranslate2 上：

* **完全离线**（模型一次性下载后不再联网）、**不需要任何 API Key**；
* 中文准确率相对 SAPI 是量级提升，长句口语也能正确转写；
* 不依赖 torch（本机 torch 是 CPU 版），也不依赖系统 ffmpeg（PyAV 自带解码）——
  而我们前端本来就已产出 16kHz 单声道 WAV，直接在内存里转 float32 交给模型即可。

## 与 SAPI 的分工

本模块只负责"高精度但需要下载模型"的路线；SAPI 继续作为零依赖兜底。
两者的选择逻辑在 :class:`agent.voice_service.VoiceService` 里，
支持 ``auto``（优先本引擎）/ ``whisper`` / ``sapi`` 三种配置。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

from agent.io.voice import SpeechRecognitionError, TranscriptionResult, VoiceUnavailableError
from agent.io.wavutil import parse_wav, wav_to_float32
from common.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

__all__ = ["WhisperSpeechToText", "whisper_available", "DEFAULT_MODEL_ROOT", "DEFAULT_HF_ENDPOINT"]

DEFAULT_MODEL_ROOT = PROJECT_ROOT / ".data" / "models" / "whisper"

# 默认走国内镜像：huggingface.co 在部分网络环境下不可达（本项目实测超时），
# 而 hf-mirror.com 可用。可用 VOICE_WHISPER_ENDPOINT 覆盖，留空则使用官方源。
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"

DEFAULT_MODEL_SIZE = "small"

# 领域提示词。除了提示场景，它还负责把输出**钉在简体中文**上 ——
# Whisper 不加提示时偶尔会输出繁体，对国内用户来说很突兀。
INITIAL_PROMPT = "以下是普通话待办管理助手的日常语音指令，请用简体中文转写。"

# 低于这个时长（秒）基本不可能是有效语音
MIN_AUDIO_SECONDS = 0.25


def whisper_available() -> bool:
    """检查 faster-whisper 依赖是否已安装（不加载模型，很快）。"""
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001 - 任何导入期错误都视为不可用
        return False
    return True


class WhisperSpeechToText:
    """:class:`~agent.io.voice.SpeechToText` 的 faster-whisper 实现。"""

    name = "faster-whisper"

    def __init__(
        self,
        *,
        model_size: str = DEFAULT_MODEL_SIZE,
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "zh",
        download_root: str | Path | None = None,
        endpoint: str | None = DEFAULT_HF_ENDPOINT,
        beam_size: int = 1,
        vad: bool = True,
    ) -> None:
        # HF_ENDPOINT 必须在导入 huggingface_hub 之前写入环境变量，
        # 而 faster_whisper 在导入期就会拉取 huggingface_hub，因此顺序不能调换。
        if endpoint:
            os.environ.setdefault("HF_ENDPOINT", endpoint)
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceUnavailableError(
                "未安装 faster-whisper；如需高精度识别请执行 "
                "`python -m pip install faster-whisper`，"
                "或把 VOICE_STT_PROVIDER 设为 sapi 使用系统自带引擎"
            ) from exc

        self._model_class = WhisperModel
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = max(1, int(beam_size))
        self.vad = bool(vad)
        self.download_root = Path(download_root) if download_root else DEFAULT_MODEL_ROOT
        self.endpoint = endpoint or ""

        self._model: Any = None
        self._lock = threading.RLock()
        self._state = "not_loaded"     # not_loaded / loading / ready / failed
        self._error = ""
        self.loaded_at: float | None = None

    # ------------------------------------------------------------------ #
    # 模型生命周期
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str:
        return self._error

    @property
    def display_name(self) -> str:
        return f"faster-whisper ({self.model_size})"

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def warmup(self) -> bool:
        """提前加载模型（会触发首次下载）。返回是否成功。"""
        try:
            self._ensure_model()
            return True
        except Exception as exc:  # noqa: BLE001 - 失败原因交给调用方决定如何降级
            self._state = "failed"
            self._error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning("faster-whisper 模型加载失败：%s", self._error)
            return False

    def _ensure_model(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            self._state = "loading"
            started = time.perf_counter()
            self.download_root.mkdir(parents=True, exist_ok=True)
            try:
                model = self._model_class(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=str(self.download_root),
                )
            except Exception as exc:  # noqa: BLE001
                self._state = "failed"
                self._error = f"{type(exc).__name__}: {str(exc)[:200]}"
                raise VoiceUnavailableError(
                    f"faster-whisper 模型加载失败：{self._error}。"
                    "可先运行 `python scripts/download_asr_model.py` 预下载模型"
                ) from exc
            self._model = model
            self._state = "ready"
            self._error = ""
            self.loaded_at = time.time()
            logger.info(
                "faster-whisper 模型就绪：%s（%s/%s），用时 %.1fs",
                self.model_size,
                self.device,
                self.compute_type,
                time.perf_counter() - started,
            )
            return model

    # ------------------------------------------------------------------ #
    # 识别
    # ------------------------------------------------------------------ #
    def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> str:
        return self.transcribe_detailed(audio, sample_rate=sample_rate)

    def transcribe_detailed(
        self,
        audio: bytes,
        *,
        sample_rate: int = 16000,
        mode: str | None = None,
    ) -> TranscriptionResult:
        """识别音频。

        Args:
            audio: 16bit PCM WAV 字节流（采样率任意，本引擎内部按 float32 处理）。
            sample_rate: 调用方声明的采样率，仅用于日志。
            mode: 兼容 SAPI 的参数，本引擎忽略（模型自带语言建模，无需受限语法）。

        Raises:
            SpeechRecognitionError: 音频不合规或没有识别到语音。
            VoiceUnavailableError: 依赖/模型不可用。
        """
        info = parse_wav(audio)
        if info.duration < MIN_AUDIO_SECONDS:
            raise SpeechRecognitionError("录音太短了，请说完一整句话再结束录音")

        samples = wav_to_float32(audio, info)
        model = self._ensure_model()

        options: dict[str, Any] = {
            "language": self.language or None,
            "beam_size": self.beam_size,
            # 单段音频，不依赖前文可显著降低"复读幻觉"的概率
            "condition_on_previous_text": False,
            "initial_prompt": INITIAL_PROMPT,
            "temperature": 0.0,
        }
        if self.vad:
            # 语音活动检测：剔除静音段，既提速又能抑制"对静音胡说"
            options["vad_filter"] = True
            options["vad_parameters"] = {"min_silence_duration_ms": 300}

        started = time.perf_counter()
        try:
            segments, info_out = model.transcribe(samples, **options)
            texts: list[str] = []
            log_probs: list[float] = []
            for segment in segments:      # 实际推理发生在这个迭代里
                piece = (segment.text or "").strip()
                if piece:
                    texts.append(piece)
                    if segment.avg_logprob is not None:
                        log_probs.append(float(segment.avg_logprob))
        except VoiceUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("faster-whisper 推理异常")
            raise SpeechRecognitionError(f"识别引擎异常：{type(exc).__name__}: {str(exc)[:160]}") from exc

        elapsed = time.perf_counter() - started
        # 中文不需要词间空格，直接拼接
        text = "".join(texts).strip()
        if not text:
            raise SpeechRecognitionError("no-speech：没有检测到有效语音，请靠近麦克风再说一次")

        confidence = 0.0
        if log_probs:
            # avg_logprob 是平均对数概率（越接近 0 越好），取指数映射到 0~1 便于展示
            confidence = round(min(1.0, math.exp(sum(log_probs) / len(log_probs))), 3)

        logger.info(
            "whisper 识别完成：%.1fs 音频 → %.1fs 推理，语言=%s(%.2f)，置信度=%.3f，文本=%s",
            info.duration,
            elapsed,
            getattr(info_out, "language", "?"),
            getattr(info_out, "language_probability", 0.0),
            confidence,
            text[:40],
        )
        return TranscriptionResult(
            text,
            confidence=confidence,
            mode="whisper",
            engine=self.display_name,
            grammar="model",
        )
