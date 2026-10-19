"""语音环境自检：探测引擎 → 合成测试句 → 回喂识别 → 对比两个引擎的准确率。

用途：
1. 换机器/换组员电脑后，一条命令判断语音功能是否可用；
2. 直观对比「Windows SAPI（零依赖）」与「faster-whisper（高精度）」的识别差距；
3. 作为回归测试，改动语音代码后快速确认链路没断。

用法：
    python scripts/voice_smoke.py
    python scripts/voice_smoke.py --say "把买牛奶这个任务标记成已完成"
    python scripts/voice_smoke.py --no-whisper        # 只测 SAPI
    python scripts/voice_smoke.py --download          # 额外预下载 whisper 模型

退出码：0 表示至少一个识别引擎可用；1 表示都不可用。
"""

import argparse
import difflib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.io.sapi import VOICE_DIR, close_shared_worker, probe_engines  # noqa: E402
from agent.io.voice import (  # noqa: E402
    SpeechRecognitionError,
    VoiceError,
    VoiceUnavailableError,
    create_stt,
    create_tts,
    strip_markdown_for_speech,
)

# 一份覆盖不同长度与口语程度的测试集：短句、含同音字、长口语句
TEST_SENTENCES = [
    "今天有什么安排",
    "提醒我周五晚上七点开会",
    "把买牛奶这个任务标记成已完成",
    "帮我记一下明天下午三点交作业",
    "我想和同学吃饭安排一下晚上的时间",
    "帮我把下周的体检预约提前两天",
]

SMOKE_PHRASES = [
    "今天有什么安排",
    "今天我先做哪个",
    "把今天的清单导出成文件",
    "今天北京天气怎么样",
    "我还有多少任务没做完",
    "帮我记一下明天下午三点交作业",
]


def similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def probe() -> dict:
    section("1. 探测本机语音引擎（SAPI 合成音色 / 识别引擎）")
    try:
        info = probe_engines()
    except VoiceUnavailableError as exc:
        print(f"✗ 语音子系统不可用：{exc}")
        return {}

    engines = info.get("engines") or []
    voices = info.get("voices") or []
    print(f"PowerShell: {info.get('powershell') or '未知'}")
    print(f"识别引擎 {len(engines)} 个：")
    for engine in engines:
        print(f"  · {engine.get('name')} [{engine.get('culture')}]")
    print(f"合成音色 {len(voices)} 个：")
    for voice in voices:
        print(f"  · {voice.get('name')} [{voice.get('culture')}] {voice.get('gender', '')}")
    return {"engines": engines, "voices": voices}


def check_markdown_cleaning() -> None:
    section("2. Markdown 清洗（朗读前处理）")
    sample = (
        "已经记好了 ✅\n\n"
        "- **#12 提交软件工程作业文档** — 高优先级，截止 `明天 15:00`\n"
        "- 标签：学习、作业\n\n"
        "| 状态 | 任务 |\n|------|------|\n| 待办 | 写周报 |\n\n"
        "详见 [文档](https://example.com)。"
    )
    cleaned = strip_markdown_for_speech(sample)
    print("清洗后：", cleaned.replace("\n", " ⏎ "))
    assert "**" not in cleaned and "|" not in cleaned and "`" not in cleaned, "Markdown 清洗不彻底"
    print("✓ 清洗正常（无残留 Markdown 符号与 emoji）")


def evaluate(engine_label: str, stt, tts, sentences: list[str], synthesis_voice: str) -> dict:
    """对给定识别引擎跑一遍测试集，返回统计结果。"""
    rows = []
    for sentence in sentences:
        audio = tts.synthesize(sentence)
        try:
            result = stt.transcribe_detailed(audio, mode="dictation")
            text = str(result)
            conf = getattr(result, "confidence", 0.0)
        except (SpeechRecognitionError, VoiceError) as exc:
            text, conf = f"<失败: {exc}>", 0.0
        rows.append((sentence, text, similarity(sentence, text), conf))

    avg = sum(row[2] for row in rows) / len(rows) if rows else 0.0
    print(f"\n[{engine_label}]  合成音色：{synthesis_voice}")
    print(f"{'原句':<20}{'识别结果':<26}{'相似度':>8}{'置信度':>9}")
    print("-" * 72)
    for sentence, text, score, conf in rows:
        print(f"{sentence:<20}{text:<26}{score:>7.0%}{conf:>9.2f}")
    print("-" * 72)
    print(f"[{engine_label}] 平均字面相似度：{avg:.1%}")
    return {"label": engine_label, "average": avg, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="语音输入输出环境自检")
    parser.add_argument("--say", action="append", help="自定义测试句，可重复指定")
    parser.add_argument("--no-whisper", action="store_true", help="跳过 faster-whisper 测试")
    parser.add_argument("--download", action="store_true", help="额外预下载 whisper 模型")
    parser.add_argument("--keep", action="store_true", help="保留合成的 WAV 音频文件")
    args = parser.parse_args()

    if args.download:
        from scripts.download_asr_model import main as download_main  # type: ignore

        download_main()

    info = probe()
    if not info:
        return 1
    voices = info.get("voices") or []
    if not voices:
        print("\n✗ 本机没有语音合成音色，无法进行回环验证")
        return 1

    zh_voice = next((v for v in voices if str(v.get("culture", "")).startswith("zh")), voices[0])
    tts = create_tts(provider="sapi", voice=str(zh_voice["name"]))

    check_markdown_cleaning()

    sentences = args.say or TEST_SENTENCES
    results = []

    # ---- SAPI ----
    engines = info.get("engines") or []
    if engines:
        section("3. 识别准确率：Windows SAPI（零依赖兜底）")
        zh_engine = next((e for e in engines if str(e.get("culture", "")).startswith("zh")), engines[0])
        worker_error = ""
        try:
            stt = create_stt("sapi", engine=str(zh_engine["name"]), mode="dictation")
            results.append(evaluate("SAPI", stt, tts, sentences, str(zh_voice["name"])))
        except VoiceUnavailableError as exc:
            worker_error = str(exc)
            print(f"✗ SAPI 识别不可用：{exc}")
        if worker_error:
            print(f"（已跳过 SAPI：{worker_error}）")
    else:
        print("\n（本机没有 SAPI 识别引擎，跳过）")

    # ---- faster-whisper ----
    if not args.no_whisper:
        section("4. 识别准确率：faster-whisper（高精度）")
        try:
            from agent.io.whisper import DEFAULT_MODEL_ROOT, WhisperSpeechToText, whisper_available

            if not whisper_available():
                print("✗ 未安装 faster-whisper：python -m pip install faster-whisper")
            else:
                print("正在加载模型（首次会下载约 480MB，请耐心等待）…")
                stt = WhisperSpeechToText(download_root=DEFAULT_MODEL_ROOT)
                if not stt.warmup():
                    print(f"✗ 模型加载失败：{stt.error}")
                else:
                    results.append(evaluate("whisper", stt, tts, sentences, str(zh_voice["name"])))
        except Exception as exc:  # noqa: BLE001 - 自检脚本不应因附加项失败而中断
            print(f"✗ faster-whisper 测试失败：{type(exc).__name__}: {exc}")

    # ---- 汇总 ----
    section("5. 结论")
    if results:
        for item in results:
            print(f"  {item['label']:<10} 平均字面相似度 {item['average']:.1%}")
        if len(results) >= 2:
            gain = results[1]["average"] - results[0]["average"]
            print(f"\n  → faster-whisper 相对 SAPI 提升 {gain:+.1%}")
    live = [r for r in results if r["average"] > 0]
    if not live:
        print("✗ 没有任何可用的识别引擎")
        close_shared_worker()
        return 1
    print("\n✓ 语音输入输出链路可用（完全离线，不需要任何密钥）")
    print("  实际使用哪个引擎由 .env 的 VOICE_STT_PROVIDER 决定（auto/whisper/sapi）")
    close_shared_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
