"""WAV 解析、格式校验与电平分析。

为什么要独立成一个模块：这套工具被**两个完全不同的识别引擎**共用 ——

* SAPI（`agent.io.sapi`）只接受 16kHz/22.05kHz、8/16bit 的单声道 PCM；
* faster-whisper（`agent.io.whisper`）内部用 float32 数组，对位深与采样率更宽容。

因此这里把「结构解析」与「格式约束」拆开：:func:`parse_wav` 负责通用解析，
:func:`parse_wav` 的 ``require_sapi_format=True`` 才附加 SAPI 的严格校验。

另外提供 :func:`analyze_level`，这是排查「识别不准」的关键工具 ——
它能立刻区分「引擎不行」和「麦克风根本没收到声音」。
"""

from __future__ import annotations

import array
import math
import struct
from typing import Any, NamedTuple

from agent.io.voice import SpeechRecognitionError

__all__ = [
    "WavInfo",
    "parse_wav",
    "analyze_level",
    "wav_to_float32",
    "LOW_LEVEL_DBFS",
    "SAPI_SAMPLE_RATES",
    "SAPI_BITS",
]

# SAPI 识别引擎实际支持的音频格式（本机实测枚举结果）
SAPI_SAMPLE_RATES = (16000, 22050)
SAPI_BITS = (8, 16)

# 低于这个峰值电平（dBFS）基本可以判断「麦克风音量太小」，识别必然不准
LOW_LEVEL_DBFS = -30.0

_PCM_FORMAT = 1


class WavInfo(NamedTuple):
    """WAV 头解析结果。"""

    channels: int
    sample_rate: int
    bits_per_sample: int
    frames: int
    data_offset: int = 0
    data_size: int = 0

    @property
    def duration(self) -> float:
        if not self.sample_rate:
            return 0.0
        return self.frames / float(self.sample_rate)


def parse_wav(data: bytes, *, require_sapi_format: bool = False) -> WavInfo:
    """解析 WAV 头。

    Args:
        data: 完整 WAV 字节流。
        require_sapi_format: 为 ``True`` 时额外校验 SAPI 识别引擎的格式约束
            （单声道、16k/22.05k、8/16bit）。

    Raises:
        SpeechRecognitionError: 结构损坏或格式不合规（错误信息直接面向用户）。
    """
    if len(data) < 44:
        raise SpeechRecognitionError("音频数据太短，不是完整的 WAV 文件")
    if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise SpeechRecognitionError("音频不是 WAV 格式（缺少 RIFF/WAVE 标识），请检查浏览器录音转换逻辑")

    offset = 12
    fmt: tuple[int, int, int] | None = None
    data_offset = 0
    data_size = 0
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        body = offset + 8
        if chunk_id == b"fmt ":
            if chunk_size < 16 or body + 16 > len(data):
                raise SpeechRecognitionError("WAV 的 fmt 数据块不完整")
            audio_format, channels, rate, _byte_rate, _align, bits = struct.unpack_from(
                "<HHIIHH", data, body
            )
            if audio_format != _PCM_FORMAT:
                raise SpeechRecognitionError("只支持 PCM 编码的 WAV（当前不是 PCM），请检查录音转换逻辑")
            fmt = (channels, rate, bits)
        elif chunk_id == b"data":
            data_offset = body
            data_size = min(chunk_size, max(0, len(data) - body))
            break
        offset = body + chunk_size + (chunk_size % 2)

    if fmt is None:
        raise SpeechRecognitionError("WAV 中找不到 fmt 数据块")
    channels, rate, bits = fmt

    bytes_per_frame = max(1, channels * bits // 8)
    frame_count = data_size // bytes_per_frame if data_size else 0
    if frame_count <= 0:
        raise SpeechRecognitionError("音频里没有任何采样数据")

    if require_sapi_format:
        if channels != 1:
            raise SpeechRecognitionError(f"识别引擎只接受单声道音频（当前 {channels} 声道）")
        if rate not in SAPI_SAMPLE_RATES:
            raise SpeechRecognitionError(
                f"识别引擎接受 {SAPI_SAMPLE_RATES[0]}Hz 或 {SAPI_SAMPLE_RATES[1]}Hz 采样率（当前 {rate}Hz）"
            )
        if bits not in SAPI_BITS:
            raise SpeechRecognitionError(f"识别引擎接受 8bit 或 16bit（当前 {bits}bit）")

    return WavInfo(
        channels=channels,
        sample_rate=rate,
        bits_per_sample=bits,
        frames=frame_count,
        data_offset=data_offset,
        data_size=data_size,
    )


def analyze_level(wav: bytes, info: WavInfo) -> dict[str, Any]:
    """统计音频电平，用于判断「识别不准是不是麦克风音量太低造成的」。

    环境噪声、麦克风静音、系统输入音量过低，都会表现为峰值电平远低于正常说话
    （正常说话峰值一般在 -6 ~ -18 dBFS）。

    Returns:
        ``{"seconds", "peak_dbfs", "rms_dbfs", "level_ok"}``
    """
    result: dict[str, Any] = {
        "seconds": round(info.duration, 2),
        "peak_dbfs": -120.0,
        "rms_dbfs": -120.0,
        "level_ok": False,
    }
    if info.bits_per_sample not in (8, 16):
        # 罕见位深不做电平判断，避免误报
        result["level_ok"] = True
        return result

    payload = wav[info.data_offset : info.data_offset + info.data_size]
    if info.bits_per_sample == 16:
        samples = array.array("h")
        samples.frombytes(payload[: len(payload) - len(payload) % 2])
        scale = 32768.0
    else:  # 8bit PCM 是无符号的，中点 128
        samples = array.array("B")
        samples.frombytes(payload)
        samples = array.array("i", (value - 128 for value in samples))
        scale = 128.0

    if not samples:
        return result

    peak = max(max(samples), -min(samples)) / scale
    square_sum = 0.0
    for value in samples:
        square_sum += float(value) * float(value)
    rms = math.sqrt(square_sum / len(samples)) / scale

    result["peak_dbfs"] = round(_to_dbfs(peak), 1)
    result["rms_dbfs"] = round(_to_dbfs(rms), 1)
    result["level_ok"] = result["peak_dbfs"] >= LOW_LEVEL_DBFS
    return result


def wav_to_float32(wav: bytes, info: WavInfo) -> Any:
    """把 PCM WAV 转成 ``float32`` 单声道数组（取值范围 -1 ~ 1）。

    faster-whisper 需要的就是这个形式：16kHz、float32、[-1, 1]。在内存里直接转换，
    既不落临时文件，也用不到 ffmpeg。
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - numpy 是本项目已有依赖
        raise SpeechRecognitionError("缺少 numpy，无法解析音频数据") from exc

    payload = wav[info.data_offset : info.data_offset + info.data_size]
    bits = info.bits_per_sample
    if bits == 16:
        dtype, scale = np.int16, 32768.0
    elif bits == 32:
        dtype, scale = np.int32, 2147483648.0
    elif bits == 8:
        dtype, scale = np.uint8, None  # 8bit 需要先减去中点
    else:
        raise SpeechRecognitionError(f"暂不支持 {bits}bit 的 PCM 音频，请在前端统一转成 16bit")

    usable = len(payload) - (len(payload) % (bits // 8))
    samples = np.frombuffer(payload[:usable], dtype=dtype).astype(np.float32)
    if scale is None:
        samples = samples - 128.0
        scale = 128.0
    samples = samples / scale
    samples = np.clip(samples, -1.0, 1.0)

    if info.channels > 1:
        # 多声道下混为单声道（当前前端固定产出单声道，这里只做兜底）
        samples = samples.reshape(-1, info.channels).mean(axis=1)

    return np.ascontiguousarray(samples, dtype=np.float32)


def _to_dbfs(amplitude: float) -> float:
    if amplitude <= 1e-6:
        return -120.0
    return 20.0 * math.log10(amplitude)
