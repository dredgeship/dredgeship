"""语音相关接口。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | ``/api/voice/status``     | 语音能力总状态（前端据此决定是否显示语音入口） |
| GET  | ``/api/voice/hints``      | 按场景分组的语音指令参考清单 |
| POST | ``/api/voice/transcribe`` | 上传 WAV 字节流，返回识别文本（可能已纠错） |
| POST | ``/api/voice/speak``      | 提交文本，返回 WAV 音频字节流 |

## 两个接口设计决定

1. **上传走原始请求体（``audio/wav``）而不是 multipart**。
   浏览器侧本来就会把录音转成标准 WAV 字节，直接 ``fetch(url, {body: blob})``
   最简单，也不用为此引入 ``python-multipart`` 依赖。
2. **识别失败返回 200 + ``ok: false``**，只有"整个语音子系统不可用"才返回 503。
   因为"没听清"是高频正常情况，前端应当弹提示让用户重说，而不是当成接口故障。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from agent.io.voice import SpeechRecognitionError, VoiceError, VoiceUnavailableError
from agent.voice_service import VoiceService
from api.schemas import SpeakRequest, VoiceStatusResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["语音"])

# 单次上传上限：15 秒 16kHz 16bit 单声道约 480KB，这里给足余量
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
STATUS_WAIT_SECONDS = 25.0


def _service(request: Request) -> VoiceService:
    service = getattr(request.app.state, "voice", None)
    if service is None:
        raise HTTPException(status_code=503, detail="语音服务未初始化")
    return service


async def _wait_ready(request: Request, timeout: float = STATUS_WAIT_SECONDS) -> None:
    """等待语音初始化完成（探测引擎需要拉起 PowerShell，约 1~3 秒）。"""
    import asyncio

    event = getattr(request.app.state, "voice_ready", None)
    if event is None or event.is_set():
        return
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("等待语音初始化超时")


@router.get("/status", response_model=VoiceStatusResponse)
async def voice_status(request: Request) -> dict[str, Any]:
    """返回语音能力总状态。

    即使语音不可用也返回 200，由 ``available`` / ``reason`` 字段描述情况 ——
    前端据此隐藏入口并给出文字说明，而不是把页面搞成报错。
    """
    await _wait_ready(request)
    return await _service(request).status()


@router.get("/hints")
async def voice_hints(request: Request) -> dict[str, Any]:
    """返回按场景分组的语音指令参考清单。

    这份清单同时也是本机识别引擎受限语法的候选短语，照说着说识别率最高。
    """
    service = _service(request)
    await _wait_ready(request, timeout=5.0)
    return service.hints()


@router.post("/transcribe")
async def transcribe(request: Request, mode: str | None = None) -> JSONResponse:
    """把录音识别为文本。

    Args:
        mode: ``auto``（默认，自由听写 + 受限指令）或 ``commands``（仅受限指令）。
              请求体为 16kHz / 22.05kHz、16bit、单声道的 PCM WAV 字节流。
    """
    service = _service(request)
    await _wait_ready(request)

    body = await request.body()
    if not body:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "请求体为空，没有收到音频数据", "stage": "request"},
        )
    if len(body) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            status_code=413,
            content={
                "ok": False,
                "error": f"音频过大（{len(body) // 1024}KB），请缩短录音时长",
                "stage": "request",
            },
        )

    result = await service.transcribe(body, mode=mode)
    if not result.get("ok") and result.get("stage") == "unavailable":
        raise HTTPException(status_code=503, detail=result.get("error") or "语音识别不可用")
    return JSONResponse(content=result)


@router.post("/speak")
async def speak(request: Request, payload: SpeakRequest) -> Response:
    """把文本合成为 WAV 并直接返回音频字节流。"""
    service = _service(request)
    await _wait_ready(request)

    try:
        audio, media_type = await service.speak(payload.text, voice=payload.voice)
    except VoiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SpeechRecognitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VoiceError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # 同一句话的合成结果稳定，允许浏览器缓存，避免重复请求
    return Response(
        content=audio,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Length": str(len(audio)),
            "X-Voice-Engine": "windows-sapi",
        },
    )
