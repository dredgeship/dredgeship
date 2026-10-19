"""集中式配置：把环境变量收敛成强类型配置对象。

为什么不让各模块各自读环境变量：默认值、类型转换与校验规则散落各处时，
很容易出现「CLI 读到了 .env、Web 没读到」或「字符串 '8' 当成 bool」这类问题。
统一入口后，任何模块只需 ``get_settings()`` 即可。
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class Settings:
    """运行期配置（只读）。"""

    project_root: Path
    # 大模型
    api_key: str
    base_url: str
    model: str
    llm_timeout: float
    llm_max_retries: int
    # Agent 行为
    agent_max_turns: int
    memory_recent_turns: int
    memory_summary_threshold: int
    # 数据与外部服务
    db_path: str
    default_city: str
    weather_timeout: float
    # Web
    web_host: str
    web_port: int
    # MCP
    mcp_server_command: str
    # 语音（完全离线，不需要任何密钥）
    voice_enabled: bool
    voice_stt_provider: str      # auto / whisper / sapi
    voice_sapi_engine: str       # 留空 = 自动挑选中文引擎
    voice_whisper_model: str
    voice_whisper_device: str
    voice_whisper_compute_type: str
    voice_whisper_endpoint: str
    voice_whisper_root: str
    voice_asr_mode: str
    voice_asr_correct: bool
    voice_tts_voice: str
    voice_tts_rate: int
    voice_tts_volume: int
    voice_tts_max_chars: int
    voice_auto_send: bool
    voice_auto_read: bool
    voice_auto_replay: bool
    voice_max_seconds: int
    voice_timeout: float
    voice_cache_size: int

    @property
    def export_dir(self) -> Path:
        return self.project_root / "exports"

    @property
    def has_api_key(self) -> bool:
        key = self.api_key.strip()
        return bool(key) and key.lower() not in {"your-api-key", "sk-xxx", "changeme"}

    def missing_api_key_hint(self) -> str:
        return (
            "未检测到 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入密钥，"
            "或设置同名环境变量后重试。申请地址：https://platform.deepseek.com/api_keys"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取并缓存配置（首次调用时加载项目根目录的 .env）。"""
    _load_env()
    return Settings(
        project_root=PROJECT_ROOT,
        api_key=_env("DEEPSEEK_API_KEY", ""),
        base_url=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=_env("DEEPSEEK_MODEL", "deepseek-chat"),
        llm_timeout=_env_float("DEEPSEEK_TIMEOUT", 60.0),
        llm_max_retries=_env_int("DEEPSEEK_MAX_RETRIES", 2),
        agent_max_turns=_env_int("AGENT_MAX_TURNS", 8),
        memory_recent_turns=_env_int("MEMORY_RECENT_TURNS", 8),
        memory_summary_threshold=_env_int("MEMORY_SUMMARY_THRESHOLD", 24),
        db_path=_env("TODO_DB_PATH", ".data/todo.db"),
        default_city=_env("DEFAULT_CITY", "北京"),
        weather_timeout=_env_float("WEATHER_TIMEOUT", 10.0),
        web_host=_env("WEB_HOST", "127.0.0.1"),
        web_port=_env_int("WEB_PORT", 8000),
        mcp_server_command=_env("MCP_SERVER_COMMAND", ""),
        voice_enabled=_env_bool("VOICE_ENABLED", True),
        # auto = 优先 faster-whisper（高精度），不可用时回退 SAPI（零依赖）
        voice_stt_provider=_env("VOICE_STT_PROVIDER", "auto"),
        # 留空表示自动挑选中文识别引擎
        voice_sapi_engine=_env("VOICE_SAPI_ENGINE", ""),
        voice_whisper_model=_env("VOICE_WHISPER_MODEL", "small"),
        voice_whisper_device=_env("VOICE_WHISPER_DEVICE", "cpu"),
        voice_whisper_compute_type=_env("VOICE_WHISPER_COMPUTE_TYPE", "int8"),
        # 留空则使用 HuggingFace 官方源；默认走国内镜像
        voice_whisper_endpoint=_env("VOICE_WHISPER_ENDPOINT", "https://hf-mirror.com"),
        voice_whisper_root=_env("VOICE_WHISPER_ROOT", ""),
        voice_asr_mode=_env("VOICE_ASR_MODE", "auto"),
        voice_asr_correct=_env_bool("VOICE_ASR_CORRECT", True),
        voice_tts_voice=_env("VOICE_TTS_VOICE", ""),
        voice_tts_rate=_env_int("VOICE_TTS_RATE", 0),
        voice_tts_volume=_env_int("VOICE_TTS_VOLUME", 100),
        voice_tts_max_chars=_env_int("VOICE_TTS_MAX_CHARS", 800),
        voice_auto_send=_env_bool("VOICE_AUTO_SEND", True),
        voice_auto_read=_env_bool("VOICE_AUTO_READ", False),
        voice_auto_replay=_env_bool("VOICE_AUTO_REPLAY", True),
        voice_max_seconds=_env_int("VOICE_MAX_SECONDS", 15),
        voice_timeout=_env_float("VOICE_TIMEOUT", 25.0),
        voice_cache_size=_env_int("VOICE_CACHE_SIZE", 40),
    )


def reset_settings_cache() -> None:
    """清空配置缓存（测试用）。"""
    get_settings.cache_clear()


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # 未安装 python-dotenv 时仅依赖系统环境变量
        return
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)


def _env(key: str, default: str) -> str:
    value = os.getenv(key)
    return default if value is None or not value.strip() else value.strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


_TRUE_VALUES = {"1", "true", "yes", "y", "on", "是", "开"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "否", "关"}


def _env_bool(key: str, default: bool) -> bool:
    """解析布尔环境变量，兼容 1/0、true/false、是/否 等写法。"""
    raw = _env(key, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default
