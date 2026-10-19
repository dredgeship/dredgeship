"""API 请求 / 响应模型。

统一用 Pydantic 声明，好处是：请求体自动校验（非法状态/优先级直接 422）、
OpenAPI 文档自动生成（``/docs`` 可直接演示接口）。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "ChatRequest",
    "TaskCreate",
    "TaskUpdate",
    "TaskOut",
    "MetaResponse",
    "AgentEventOut",
    "VoiceEngineInfo",
    "VoiceTtsInfo",
    "VoiceCorrectionInfo",
    "VoiceLimits",
    "VoiceStatusResponse",
    "TranscribeResponse",
    "SpeakRequest",
]

Priority = Literal["low", "medium", "high"]
Status = Literal["todo", "doing", "done"]


class ChatRequest(BaseModel):
    """一次对话请求。"""

    message: str = Field(..., min_length=1, max_length=2000, description="用户自然语言输入")
    session_id: str | None = Field(default=None, max_length=64, description="会话 ID，不传则新建")
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)

    @field_validator("message")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message 不能为空白")
        return cleaned


class TaskCreate(BaseModel):
    """新建任务。"""

    title: str = Field(..., min_length=1, max_length=200)
    priority: Priority = "medium"
    status: Status = "todo"
    due_date: str | None = Field(default=None, description="支持「明天下午3点」等口语时间")
    tags: list[str] = Field(default_factory=list)
    estimated_minutes: int = Field(default=30, ge=0, le=1440)
    notes: str = Field(default="", max_length=2000)


class TaskUpdate(BaseModel):
    """局部更新（未传字段保持不变）。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    priority: Priority | None = None
    status: Status | None = None
    due_date: str | None = None
    clear_due_date: bool = False
    tags: list[str] | None = None
    estimated_minutes: int | None = Field(default=None, ge=0, le=1440)
    notes: str | None = Field(default=None, max_length=2000)


class TaskOut(BaseModel):
    """任务输出（与仓储返回的 dict 结构一致）。"""

    id: int
    title: str
    status: str
    status_label: str
    priority: str
    priority_label: str
    due_date: str | None = None
    due_human: str | None = None
    tags: list[str] = Field(default_factory=list)
    estimated_minutes: int = 0
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    overdue: bool = False


class MetaResponse(BaseModel):
    """运行环境元信息（前端「设置与关于」页使用）。"""

    model: str
    base_url: str
    has_api_key: bool
    db_path: str
    tools: list[dict[str, Any]]
    stats: dict[str, int]
    mcp_server: dict[str, Any] = Field(default_factory=dict)
    voice: dict[str, Any] = Field(default_factory=dict, description="语音能力状态摘要")


class AgentEventOut(BaseModel):
    """SSE 事件（文档用途，实际以 JSON 行下发）。"""

    type: str
    data: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 语音（第 5 个加分项）
# --------------------------------------------------------------------------- #
class VoiceEngineInfo(BaseModel):
    """语音识别（ASR）状态。"""

    available: bool = False
    provider: str = Field(default="", description="实际生效的引擎：whisper（高精度）或 sapi（零依赖兜底）")
    quality: str = Field(default="", description="精度标签：高精度 / 基础")
    engine: str = Field(default="", description="识别引擎显示名")
    culture: str = Field(default="", description="引擎语言，如 zh-CN")
    mode: str = Field(default="auto", description="SAPI 模式：auto=自由听写+受限指令；commands=仅受限指令")
    model_state: str = Field(default="", description="whisper 模型状态：not_loaded/loading/ready/failed")
    model_size: str = Field(default="", description="whisper 模型规格，如 small")
    fallback_reason: str = Field(default="", description="auto 模式下回退到 SAPI 的原因")


class VoiceTtsInfo(BaseModel):
    """语音合成（TTS）状态。"""

    available: bool = False
    voice: str = Field(default="", description="合成音色名称")
    culture: str = ""
    rate: int = Field(default=0, description="语速，-10~10")


class VoiceCorrectionInfo(BaseModel):
    """识别结果纠错状态。"""

    enabled: bool = Field(default=True, description="配置是否开启纠错")
    available: bool = Field(default=False, description="是否具备纠错条件（需要模型密钥）")
    reason: str = Field(default="", description="不可用的原因，如 no_api_key")


class VoiceLimits(BaseModel):
    """前端需要遵守的限制。"""

    max_seconds: int = Field(default=15, description="单次录音时长上限")
    max_chars: int = Field(default=800, description="单次朗读字符上限")
    cache_size: int = Field(default=40, description="合成缓存条数上限")


class VoiceStatusResponse(BaseModel):
    """语音能力总状态。"""

    enabled: bool
    available: bool = Field(description="语音子系统整体是否可用")
    reason: str = Field(default="", description="不可用时的原因（面向用户）")
    asr: VoiceEngineInfo
    tts: VoiceTtsInfo
    correction: VoiceCorrectionInfo
    limits: VoiceLimits
    auto_send: bool = Field(default=True, description="识别完成后是否自动发送")
    auto_read: bool = Field(default=False, description="助理回复是否自动朗读")
    auto_replay: bool = Field(
        default=True, description="录音结束后是否自动回放一遍自己的录音，便于确认录进去的声音"
    )
    hints: dict[str, Any] = Field(default_factory=dict, description="按场景分组的语音指令参考")


class TranscribeResponse(BaseModel):
    """一次语音识别的结果。"""

    ok: bool
    raw: str = Field(default="", description="识别引擎的原始输出")
    text: str = Field(default="", description="最终文本（可能已被纠错）")
    changed: bool = Field(default=False, description="是否发生了纠错")
    confidence: float = 0.0
    grammar: str = Field(default="", description="命中的语法：dictation=自由听写，commands=受限指令")
    mode: str = ""
    engine: str = ""
    correction: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    error: str = Field(default="", description="失败原因（面向用户）")
    stage: str = Field(default="", description="失败阶段：recognize / engine / internal / unavailable")
    retryable: bool = Field(default=False, description="是否建议用户重说一次")


class SpeakRequest(BaseModel):
    """语音合成请求。"""

    text: str = Field(..., min_length=1, max_length=2000, description="待朗读文本（Markdown 会被自动清洗）")
    voice: str | None = Field(default=None, description="临时指定音色，不传则用默认音色")

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text 不能为空白")
        return cleaned
