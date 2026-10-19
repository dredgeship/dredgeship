"""预下载 faster-whisper 语音识别模型（一次性，之后完全离线）。

为什么需要单独一个下载步骤：
    模型约 480MB（small）。虽然程序首次使用时也会自动下载，但那会让第一次说话
    卡上好几分钟。答辩/演示前先跑一次本脚本，可以把等待提前消化掉。

用法：
    python scripts/download_asr_model.py                 # 默认 small
    python scripts/download_asr_model.py --model base    # 更小更快
    python scripts/download_asr_model.py --mirror https://huggingface.co   # 改用官方源

模型默认存放在 .data/models/whisper（已在 .gitignore 中），不会污染仓库。
"""

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_ENDPOINT = "https://hf-mirror.com"
DEFAULT_ROOT = PROJECT_ROOT / ".data" / "models" / "whisper"


def main() -> int:
    parser = argparse.ArgumentParser(description="预下载 faster-whisper 模型")
    parser.add_argument("--model", default="small", help="模型规格：tiny/base/small/medium/large-v3")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="模型存放目录")
    parser.add_argument("--mirror", default=DEFAULT_ENDPOINT, help="HuggingFace 镜像地址，留空则用官方源")
    parser.add_argument("--device", default="cpu", help="cpu 或 cuda")
    parser.add_argument("--compute-type", default="int8", help="int8 / int8_float16 / float16 / float32")
    parser.add_argument("--retries", type=int, default=8, help="下载中断时的自动重试次数（断点续传）")
    args = parser.parse_args()

    if args.mirror:
        # 必须在导入 faster_whisper 之前设置，huggingface_hub 会在导入期读取
        os.environ["HF_ENDPOINT"] = args.mirror
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("✗ 未安装 faster-whisper，请先执行：python -m pip install faster-whisper")
        return 1

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    print(f"模型规格：{args.model}")
    print(f"下载源  ：{args.mirror or '官方源（huggingface.co）'}")
    print(f"存放目录：{root}")
    print("开始下载/加载（首次下载约 480MB，请耐心等待）…")

    # 国内镜像下载大文件时连接中断很常见。huggingface_hub 会把未完成的下载
    # 保留为 .incomplete 文件，重试即可从断点继续，不必从头再来。
    model = None
    started = time.perf_counter()
    for attempt in range(1, max(1, args.retries) + 1):
        try:
            model = WhisperModel(
                args.model, device=args.device, compute_type=args.compute_type, download_root=str(root)
            )
            break
        except Exception as exc:  # noqa: BLE001 - 下载失败原因五花八门，直接展示
            print(f"  第 {attempt} 次尝试失败：{type(exc).__name__}: {str(exc)[:180]}")
            if attempt >= args.retries:
                print("✗ 已达到最大重试次数")
                print("  可尝试：换用更小的模型（--model base，仅需 145MB）、")
                print("          或者指定官方源 --mirror https://huggingface.co")
                return 1
            print("  3 秒后从断点续传…")
            time.sleep(3)

    assert model is not None
    elapsed = time.perf_counter() - started
    print(f"✓ 模型就绪，用时 {elapsed:.1f} 秒")

    # 用一段静音做一次推理，确认模型真的可以跑（也顺带触发 VAD 资源准备）
    try:
        import numpy as np

        silence = np.zeros(16000, dtype=np.float32)
        segments, info = model.transcribe(silence, language="zh", vad_filter=True)
        list(segments)
        print(f"✓ 推理自检通过（语言={info.language}，时长={info.duration:.2f}s）")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠ 推理自检失败（模型已下载，但运行可能有问题）：{type(exc).__name__}: {exc}")

    size_mb = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"✓ 模型目录当前占用 {size_mb:.0f} MB")
    print("\n接下来：在 .env 中设置 VOICE_STT_PROVIDER=auto（或 whisper）即可启用高精度识别。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
