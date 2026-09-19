"""性能分析脚本：统计生成一万道题时各函数的耗时，并可选绘制柱状图。

用法：
    python tools/profile_gen.py                 # 默认生成 10000 道、-r 10
    python tools/profile_gen.py -n 20000 -r 20  # 自定义规模
    python tools/profile_gen.py --png docs/perf_top_functions.png

依赖：仅标准库；绘制 PNG 需要 matplotlib（可选）。
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arith.generator import ExerciseGenerator  # noqa: E402


def run_once(count: int, r: int, seed: int) -> List:
    return ExerciseGenerator(r=r, seed=seed).generate(count)


def collect_stats(count: int, r: int, seed: int, top: int = 15) -> tuple[str, list[tuple[str, float]]]:
    profiler = cProfile.Profile()
    profiler.enable()
    run_once(count, r, seed)
    profiler.disable()

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats("tottime")
    stats.print_stats(top)

    rows: list[tuple[str, float]] = []
    for func_key, (_, _, tottime, cumulative, _) in stats.stats.items():
        label = f"{func_key[2]}"
        rows.append((label, tottime))
    rows.sort(key=lambda item: item[1], reverse=True)
    return stream.getvalue(), rows[:top]


def _use_chinese_font() -> None:
    """若系统存在中文字体则注册，避免绘图出现方块（失败不影响出图）。"""
    try:
        import matplotlib
        from matplotlib import font_manager

        candidates = ["Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC"]
        available = {font.name for font in font_manager.fontManager.ttflist}
        for name in candidates:
            if name in available:
                matplotlib.rcParams["font.sans-serif"] = [name]
                matplotlib.rcParams["axes.unicode_minus"] = False
                return
    except Exception:  # pragma: no cover - 字体配置失败可忽略
        return


def draw_png(rows: list[tuple[str, float]], path: str) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("未安装 matplotlib，跳过绘图（pip install matplotlib 后可生成 PNG）")
        return False

    _use_chinese_font()
    labels = [row[0] for row in rows][::-1]
    values = [row[1] for row in rows][::-1]
    figure, axes = plt.subplots(figsize=(9, 6))
    axes.barh(labels, values, color="#4C78A8")
    axes.set_xlabel("tottime (s)")
    axes.set_title("Myapp profiling: top functions by tottime")
    figure.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    figure.savefig(path, dpi=150)
    print(f"性能分析图已保存：{os.path.abspath(path)}")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Myapp 性能分析")
    parser.add_argument("-n", type=int, default=10000, help="生成题目数量")
    parser.add_argument("-r", type=int, default=10, help="数值范围")
    parser.add_argument("--seed", type=int, default=20240919, help="随机种子")
    parser.add_argument("--top", type=int, default=15, help="打印前 N 个函数")
    parser.add_argument("--png", default=None, help="性能分析图输出路径")
    args = parser.parse_args(argv)

    report, rows = collect_stats(args.n, args.r, args.seed, args.top)
    print(report)
    print("耗时最高的函数（tottime, 秒）：")
    for label, tottime in rows:
        print(f"  {tottime * 1000:8.2f} ms  {label}")
    if args.png:
        draw_png(rows, args.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
