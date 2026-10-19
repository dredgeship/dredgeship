"""输入输出扩展层：语音输入 / 输出。"""

from agent.io.voice import (
    SpeechRecognitionError,
    SpeechToText,
    TextToSpeech,
    TranscriptionResult,
    VoiceError,
    VoiceUnavailableError,
    create_stt,
    create_tts,
    speech_ready,
    strip_markdown_for_speech,
)

__all__ = [
    "SpeechToText",
    "TextToSpeech",
    "TranscriptionResult",
    "VoiceError",
    "VoiceUnavailableError",
    "SpeechRecognitionError",
    "create_stt",
    "create_tts",
    "speech_ready",
    "strip_markdown_for_speech",
]
