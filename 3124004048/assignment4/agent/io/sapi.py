"""Windows SAPI 语音桥接：常驻 PowerShell 进程 + 单请求单响应协议。

## 为什么要常驻一个 PowerShell 进程

``Add-Type -AssemblyName System.Speech`` 加上进程启动约需 1 秒以上。若每次识别或
合成都新起进程，"自动朗读"会明显卡顿。因此这里维护一个常驻 worker，
进程内复用 ``SpeechRecognitionEngine`` 与 ``SpeechSynthesizer`` 实例。

## 协议（stdin/stdout 上只出现 ASCII）

PowerShell 5.1 的管道编码在不同区域设置下并不一致，直接传中文极易乱码。
因此 stdin 上只投递两种 ASCII 形式之一：

* ``D:/path/req_x.json``  —— 请求文件的绝对路径（内容为 UTF-8 JSON，便于人工排查）
* ``B64:<base64>``        —— 请求内联成 base64（用于路径含中文或不想落临时文件）

worker 的 stdout 同样只输出 JSON 行：``ConvertTo-Json`` 会把非 ASCII 转义成
``\\uXXXX``，所以 Python 侧永远只看到 ASCII，解析零风险。

## 并发模型

SAPI 引擎为单实例、天然串行。这里用「一个 reader 线程 + 每请求一个队列」把
异步的 stdout 变成同步的 ``request()`` 调用，并用唯一 id 配对，乱序返回也不会错配。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from agent.io.voice import (
    SpeechRecognitionError,
    TranscriptionResult,
    VoiceUnavailableError,
)
# 这几个 WAV 工具同时被 SAPI 与 faster-whisper 两个引擎复用，因此独立成模块；
# 这里重新导出以保持向后兼容（voice_service 仍可从本模块导入）。
from agent.io.wavutil import LOW_LEVEL_DBFS, WavInfo, analyze_level, parse_wav
from common.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

__all__ = [
    "SapiWorker",
    "SapiSpeechToText",
    "SapiTextToSpeech",
    "WavInfo",
    "parse_wav",
    "analyze_level",
    "LOW_LEVEL_DBFS",
    "probe_engines",
    "get_shared_worker",
    "close_shared_worker",
    "VOICE_DIR",
    "WORKER_SCRIPT",
]

VOICE_DIR = PROJECT_ROOT / ".data" / "voice"
WORKER_LOG = PROJECT_ROOT / ".data" / "logs" / "voice-worker.log"
WORKER_SCRIPT = Path(__file__).resolve().with_name("voice_worker.ps1")

_UTF8_BOM = b"\xef\xbb\xbf"
_QUIT_COMMAND = "__QUIT__"
_DEFAULT_TIMEOUT = 25.0


# --------------------------------------------------------------------------- #
# 环境与脚本准备
# --------------------------------------------------------------------------- #
def _powershell_candidates() -> list[str]:
    """按优先级返回可用的 PowerShell 可执行文件。"""
    if os.name != "nt":
        return []
    found: list[str] = []
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        path = shutil.which(name)
        if path and path not in found:
            found.append(path)
    return found


def ensure_worker_script() -> Path:
    """确保 worker 脚本存在且带 UTF-8 BOM。

    PowerShell 5.1 读取 ``.ps1`` 时若无 BOM，会按系统 ANSI 代码页（简体中文环境即
    GBK）解析源码，脚本里的中文字面量会变成乱码并直接导致语法错误。这道"自愈"
    检查保证即使用编辑器重存丢失了 BOM，程序仍能自动修好。
    """
    if not WORKER_SCRIPT.exists():
        raise VoiceUnavailableError(f"缺少语音 worker 脚本：{WORKER_SCRIPT}")
    raw = WORKER_SCRIPT.read_bytes()
    if not raw.startswith(_UTF8_BOM):
        logger.warning("voice_worker.ps1 缺少 UTF-8 BOM，已自动补写（否则 PowerShell 5.1 会把中文读成乱码）")
        WORKER_SCRIPT.write_bytes(_UTF8_BOM + raw)
    return WORKER_SCRIPT


# --------------------------------------------------------------------------- #
# 常驻 worker
# --------------------------------------------------------------------------- #
class SapiWorker:
    """常驻 PowerShell SAPI 进程的 Python 侧封装。"""

    def __init__(self, *, timeout: float = _DEFAULT_TIMEOUT, retries: int = 1) -> None:
        self._timeout = float(timeout)
        self._retries = max(0, int(retries))
        self._proc: subprocess.Popen[str] | None = None
        self._log_handle = None
        self._reader: threading.Thread | None = None
        self._pending: dict[str, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._start_lock = threading.RLock()
        self._quotes: tuple[str, ...] = ()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._start_locked()

    def _start_locked(self) -> None:
        candidates = _powershell_candidates()
        if not candidates:
            raise VoiceUnavailableError("未找到 PowerShell，语音功能需要 Windows PowerShell 或 pwsh")

        script = ensure_worker_script()
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        WORKER_LOG.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(WORKER_LOG, "a", encoding="utf-8", buffering=1)

        command = [
            candidates[0],
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log_handle,
            cwd=str(PROJECT_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(
            target=self._read_loop, args=(self._proc,), name="sapi-reader", daemon=True
        )
        self._reader.start()
        logger.info("SAPI voice worker 已启动 (pid=%s)", self._proc.pid)

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(_QUIT_COMMAND + "\n")
                    proc.stdin.flush()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - 关闭失败直接强杀
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_handle = None
        self._wake_all(None)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #
    def request(self, action: str, *, timeout: float | None = None, **payload: Any) -> dict[str, Any]:
        """投递一次请求并等待结果；失败会自动重启 worker 重试一次。"""
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                return self._request_once(action, timeout=timeout, **payload)
            except VoiceUnavailableError as exc:
                last_error = exc
                logger.warning("voice worker 调用失败（第 %s 次）：%s", attempt + 1, exc)
                self._reset()
        raise last_error or VoiceUnavailableError("语音服务不可用")

    def _request_once(self, action: str, *, timeout: float | None, **payload: Any) -> dict[str, Any]:
        self._ensure_started()
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise VoiceUnavailableError("语音 worker 未在运行")

        request_id = uuid.uuid4().hex[:12]
        body: dict[str, Any] = {"id": request_id, "action": action, **payload}
        line, temp_path = self._encode_request(body)

        box: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = box
        try:
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise VoiceUnavailableError(f"语音 worker 通信中断：{exc}") from exc

            try:
                response = box.get(timeout=float(timeout or self._timeout))
            except queue.Empty as exc:
                raise VoiceUnavailableError(
                    f"语音引擎响应超时（{float(timeout or self._timeout):g} 秒）"
                ) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        if response is None:
            raise VoiceUnavailableError("语音 worker 已退出，请查看 .data/logs/voice-worker.log")
        return response

    def _encode_request(self, body: dict[str, Any]) -> tuple[str, Path | None]:
        """把请求编码成一行 ASCII。

        纯 ASCII 的请求（多数场景）落成临时文件，保留可人工排查的痕迹；
        含中文或路径非 ASCII 时退回 base64 内联，避免 PowerShell 5.1 的管道编码问题。
        """
        text = json.dumps(body, ensure_ascii=False)
        path = VOICE_DIR / f"req_{body['id']}.json"
        path_str = str(path)
        if path_str.isascii():
            try:
                VOICE_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                return path_str, path
            except OSError as exc:
                logger.debug("写请求文件失败，改用 base64：%s", exc)
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return f"B64:{encoded}", None

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def _read_loop(self, proc: subprocess.Popen[str]) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    # worker 若误把日志写到 stdout 会走到这里；丢弃并记录，避免污染协议
                    logger.warning("voice worker 输出无法解析：%s", line[:200])
                    continue
                request_id = str(payload.get("id", ""))
                with self._pending_lock:
                    box = self._pending.get(request_id)
                if box is not None:
                    box.put(payload)
        except Exception as exc:  # noqa: BLE001 - 读线程异常不应影响主流程
            logger.warning("voice worker 读取线程异常：%s", exc)
        finally:
            self._wake_all(None)

    def _wake_all(self, value: Any) -> None:
        with self._pending_lock:
            boxes = list(self._pending.values())
        for box in boxes:
            try:
                box.put_nowait(value)
            except queue.Full:
                pass

    def _reset(self) -> None:
        """丢弃当前进程，下一次请求会重新拉起。"""
        with self._start_lock:
            self.close()


# --------------------------------------------------------------------------- #
# 共享实例
# --------------------------------------------------------------------------- #
_SHARED_WORKER: SapiWorker | None = None
_SHARED_LOCK = threading.Lock()


def get_shared_worker(timeout: float = _DEFAULT_TIMEOUT) -> SapiWorker:
    """进程内共享的 worker（VoiceService 使用，避免反复拉起进程）。"""
    global _SHARED_WORKER
    with _SHARED_LOCK:
        if _SHARED_WORKER is None:
            _SHARED_WORKER = SapiWorker(timeout=timeout)
        return _SHARED_WORKER


def close_shared_worker() -> None:
    """关闭共享 worker（服务关闭时调用）。"""
    global _SHARED_WORKER
    with _SHARED_LOCK:
        if _SHARED_WORKER is not None:
            _SHARED_WORKER.close()
            _SHARED_WORKER = None


# --------------------------------------------------------------------------- #
# 能力探测
# --------------------------------------------------------------------------- #
def probe_engines(*, timeout: float = 60.0) -> dict[str, Any]:
    """探测本机可用的识别引擎与合成音色。

    用一个临时的短生命周期 worker 完成，这样即使共享 worker 不可用，
    状态接口依然能给出准确结论。
    """
    worker = SapiWorker(timeout=timeout, retries=0)
    try:
        response = worker.request("probe", timeout=timeout)
    except VoiceUnavailableError:
        raise
    finally:
        worker.close()

    if not response.get("ok"):
        raise VoiceUnavailableError(str(response.get("error") or "语音引擎探测失败"))
    engines = _as_list(response.get("engines"))
    voices = _as_list(response.get("voices"))
    return {"engines": engines, "voices": voices, "powershell": response.get("psver", "")}


def _as_list(value: Any) -> list[dict[str, Any]]:
    """把 worker 返回值归一成 list，容忍单元素被序列化成对象的情况。"""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


# --------------------------------------------------------------------------- #
# 协议实现
# --------------------------------------------------------------------------- #
class SapiSpeechToText:
    """:class:`~agent.io.voice.SpeechToText` 的 Windows SAPI 实现。"""

    def __init__(
        self,
        worker: SapiWorker | None = None,
        *,
        engine: str | None = None,
        mode: str = "auto",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._worker = worker or get_shared_worker(timeout)
        self._engine = engine or ""
        self._mode = mode or "auto"
        self._timeout = timeout

    def transcribe(self, audio: bytes, *, sample_rate: int = 16000) -> str:
        return self.transcribe_detailed(audio, sample_rate=sample_rate)

    def transcribe_detailed(
        self,
        audio: bytes,
        *,
        sample_rate: int = 16000,
        mode: str | None = None,
    ) -> TranscriptionResult:
        """识别音频并返回带元信息的结果。

        Raises:
            SpeechRecognitionError: 音频格式不合规或没有识别到有效语音。
            VoiceUnavailableError: 语音子系统不可用。
        """
        info = parse_wav(audio, require_sapi_format=True)
        if sample_rate and info.sample_rate != sample_rate:
            logger.debug("音频采样率 %s 与声明值 %s 不同，以文件头为准", info.sample_rate, sample_rate)

        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        audio_path = VOICE_DIR / f"in_{uuid.uuid4().hex[:12]}.wav"
        audio_path.write_bytes(audio)
        try:
            response = self._worker.request(
                "transcribe",
                timeout=self._timeout,
                audio=str(audio_path),
                mode=mode or self._mode,
                engine=self._engine,
            )
        finally:
            # 用户录音属于隐私数据，识别完立即删除
            try:
                audio_path.unlink(missing_ok=True)
            except OSError:
                pass

        if not response.get("ok"):
            raise SpeechRecognitionError(str(response.get("error") or "识别失败"))
        text = str(response.get("text") or "").strip()
        if not text:
            raise SpeechRecognitionError("没有识别到有效语音，请靠近麦克风再说一次")
        return TranscriptionResult(
            text,
            confidence=float(response.get("confidence") or 0.0),
            mode=str(response.get("mode") or self._mode),
            engine=str(response.get("engine") or self._engine),
            grammar=str(response.get("grammar") or ""),
        )


class SapiTextToSpeech:
    """:class:`~agent.io.voice.TextToSpeech` 的 Windows SAPI 实现。"""

    def __init__(
        self,
        worker: SapiWorker | None = None,
        *,
        voice: str | None = None,
        rate: int = 0,
        volume: int = 100,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._worker = worker or get_shared_worker(timeout)
        self._voice = voice or ""
        self._rate = int(rate)
        self._volume = int(volume)
        self._timeout = timeout

    @property
    def voice(self) -> str:
        return self._voice

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        out_path: Path | str | None = None,
    ) -> bytes:
        """合成 WAV 字节流。

        Args:
            text: 待朗读文本（调用方应先做 Markdown 清洗）。
            voice: 临时覆盖音色。
            out_path: 直接写到指定路径（命中缓存时由上层指定，避免再复制一次）。
        """
        content = (text or "").strip()
        if not content:
            raise SpeechRecognitionError("朗读文本不能为空")

        target = Path(out_path) if out_path else VOICE_DIR / f"tts_{uuid.uuid4().hex[:12]}.wav"
        temporary = out_path is None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            response = self._worker.request(
                "synthesize",
                timeout=self._timeout,
                text=content,
                out=str(target),
                voice=voice or self._voice,
                rate=self._rate,
                volume=self._volume,
            )
            if not response.get("ok"):
                raise VoiceUnavailableError(str(response.get("error") or "语音合成失败"))
            if not target.exists():
                raise VoiceUnavailableError("语音引擎未生成音频文件")
            return target.read_bytes()
        finally:
            if temporary:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
