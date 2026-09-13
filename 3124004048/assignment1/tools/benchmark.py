# -*- coding: utf-8 -*-
"""性能基准与 cProfile 热点分析脚本（仅供开发与博客展示使用）。

用法::

    python tools/benchmark.py                            # 各规模文本的吞吐基准
    python tools/benchmark.py --size 200000              # 只测一个规模
    python tools/benchmark.py --repeat 5                 # 每个规模重复 5 次取最快值
    python tools/benchmark.py --naive                    # 与 O(n*m) 动态规划基线对比
    python tools/benchmark.py --profile                  # 输出 tottime / cumulative 双排名
    python tools/benchmark.py --profile --size 200000 --dump profile.out

出图（三种方式任选其一，用于博客中的"性能分析图"）::

    # 1) 直接用本脚本渲染 SVG 柱状图，零依赖
    python tools/benchmark.py --svg docs/profile.svg --size 200000

    # 2) snakeviz：浏览器内的交互式旭日图，适合截图
    python -m pip install snakeviz
    python -m snakeviz profile.out

    # 3) gprof2dot + Graphviz：生成调用关系图
    python -m pip install gprof2dot
    gprof2dot -f pstats profile.out | dot -Tpng -o profile.png

说明：脚本只在内存中生成文本，不读写任何文件（``--dump`` 指定的性能数据文件除外），
也不参与正式的查重流程。
"""

import argparse
import cProfile
import io
import os
import pstats
import random
import sys
import time
from typing import List, Optional, Sequence, Tuple

# 让 tools/ 目录下的脚本可以直接 import 到项目根目录的 dupcheck 包
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dupcheck.similarity import compute_similarity  # noqa: E402

#: 默认的吞吐基准规模（字符数）
DEFAULT_SIZES = (10000, 50000, 100000, 500000)

#: ``--profile`` 的默认输入规模。太小的规模会让 tottime 全是 0.000，看不出热点
DEFAULT_PROFILE_SIZE = 200000

#: ``--naive`` 的默认输入规模。动态规划是 O(n*m)，规模必须开得很小
DEFAULT_NAIVE_SIZE = 1200

#: 用于拼装"论文"的句子库
SENTENCES = [
    "软件工程强调以系统化的方法开发和维护软件，覆盖需求分析、设计、编码、测试与维护等阶段。",
    "论文查重算法需要衡量两篇文档在字面上的重合程度，常用的方法包括余弦相似度与编辑距离。",
    "在团队协作中，版本控制工具可以记录每一次改动，出现问题时能够快速回退到历史版本。",
    "单元测试应当覆盖正常输入、边界输入与异常输入，从而保证程序在各种情况下都能正确运行。",
    "性能分析工具能够把程序的运行时间按函数拆分，帮助开发者快速定位真正的性能瓶颈。",
]

#: 模拟"抄袭版"时用于替换的候选字符池
CHARACTER_POOL = "软件工程测试论文查重算法相似度分析设计实现性能优化单元边界"


def generate_text(length: int, seed: int = 2023) -> str:
    """生成指定长度（字符数）的合成文本。"""
    rng = random.Random(seed)
    pieces: List[str] = []
    total = 0
    while total < length:
        sentence = SENTENCES[rng.randrange(len(SENTENCES))]
        pieces.append(sentence)
        total += len(sentence)
    return "".join(pieces)[:length]


def mutate(text: str, ratio: float = 0.02, seed: int = 7) -> str:
    """通过对文本做少量增删改，模拟一份"抄袭版"论文。"""
    rng = random.Random(seed)
    characters = list(text)
    step = max(1, int(1.0 / ratio))
    for index in range(0, len(characters), step):
        characters[index] = rng.choice(CHARACTER_POOL)
    inserted = "新增的段落用于模拟抄袭者补充的内容。" * max(1, len(text) // 1000)
    middle = len(characters) // 2
    return "".join(characters[:middle]) + inserted + "".join(characters[middle:])


def naive_lcs_length(text_a: str, text_b: str) -> int:
    """O(n*m) 时间、O(m) 空间的最长公共子序列（作为性能对比基线）。"""
    if not text_a or not text_b:
        return 0
    previous = [0] * (len(text_b) + 1)
    for char_a in text_a:
        current = [0]
        for index, char_b in enumerate(text_b, start=1):
            if char_a == char_b:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[index - 1]))
        previous = current
    return previous[-1]


def measure_once(text_a: str, text_b: str, repeat: int) -> Tuple[float, float]:
    """重复调用 ``compute_similarity``，返回 ``(最快耗时, 最后一次的得分)``。

    取"最快的一次"而不是平均值，可以降低操作系统调度带来的抖动。
    """
    best = float("inf")
    score = 0.0
    for _ in range(max(1, repeat)):
        start = time.perf_counter()
        score = compute_similarity(text_a, text_b)
        best = min(best, time.perf_counter() - start)
    return best, score


def run_benchmark(sizes: Sequence[int], repeat: int = 1) -> None:
    """输出各规模文本的查重耗时。"""
    print("规模(字符)   重复率   耗时(秒)   速度(字/秒)")
    print("-" * 52)
    for size in sizes:
        original = generate_text(size)
        suspect = mutate(original)
        elapsed, score = measure_once(original, suspect, repeat)
        speed = size / elapsed if elapsed > 0 else float("inf")
        print("{0:>10}   {1:>6.2f}   {2:>8.4f}   {3:>12.0f}".format(
            size, score, elapsed, speed))


def run_naive_comparison(size: int = DEFAULT_NAIVE_SIZE) -> None:
    """对比 O(n*m) 动态规划基线与当前实现。"""
    original = generate_text(size)
    suspect = mutate(original)
    normalized_a = "".join(ch for ch in original if ch.isalnum()).casefold()
    normalized_b = "".join(ch for ch in suspect if ch.isalnum()).casefold()

    start = time.perf_counter()
    lcs = naive_lcs_length(normalized_a, normalized_b)
    naive_elapsed = time.perf_counter() - start
    naive_score = 2.0 * lcs / (len(normalized_a) + len(normalized_b))

    fast_elapsed, score = measure_once(original, suspect, 1)

    print("输入规模: {0} 字符".format(size))
    print("动态规划基线: 得分 {0:.4f}, 耗时 {1:.4f} 秒".format(naive_score, naive_elapsed))
    print("当前实现:     得分 {0:.4f}, 耗时 {1:.4f} 秒".format(score, fast_elapsed))
    if fast_elapsed > 0:
        print("加速比: {0:.1f}x".format(naive_elapsed / fast_elapsed))


def collect_profile(size: int) -> cProfile.Profile:
    """在指定规模上采集一次 cProfile 数据。"""
    original = generate_text(size)
    suspect = mutate(original)
    profiler = cProfile.Profile()
    profiler.enable()
    compute_similarity(original, suspect)
    profiler.disable()
    return profiler


def format_stats(profiler: cProfile.Profile, sort_key: str, top: int) -> str:
    """把 cProfile 数据渲染成一张排名表。"""
    buffer = io.StringIO()
    stats = pstats.Stats(profiler, stream=buffer).sort_stats(sort_key)
    stats.print_stats(top)
    return buffer.getvalue()


def run_profile(
    size: int = DEFAULT_PROFILE_SIZE,
    top: int = 15,
    dump_path: Optional[str] = None,
    svg_path: Optional[str] = None,
) -> None:
    """使用 cProfile 输出耗时排名，用于定位性能瓶颈。

    同时给出 ``tottime``（函数自身耗时）与 ``cumulative``（含被调函数）两张表：
    前者回答"时间花在哪一行代码上"，后者回答"哪个环节整体最重"。
    """
    profiler = collect_profile(size)
    total = pstats.Stats(profiler).total_tt
    print("输入规模: {0} 字符，总耗时 {1:.3f} 秒（已包含 cProfile 自身的开销）".format(size, total))
    print()
    for sort_key, title in (
        ("tottime", "按自身耗时(tottime)排名——真正消耗 CPU 的代码"),
        ("cumulative", "按累计耗时(cumulative)排名——整体最重的环节"),
    ):
        print("# " + title)
        print(format_stats(profiler, sort_key, top))
    if dump_path:
        profiler.dump_stats(dump_path)
        print("cProfile 原始数据已写入 {0}，可用 snakeviz / gprof2dot 出图。".format(dump_path))
    if svg_path:
        render_svg(profiler, svg_path, top)


def _short_name(filename: str, lineno: int, func: str) -> str:
    """把 cProfile 的原始标识压成"相对路径:行号(函数名)"的短标签。"""
    if filename == "~" or not os.path.isabs(filename):
        # 内建函数（例如 _collections._count_elements）没有对应的源文件
        return func
    absolute = os.path.abspath(filename)
    if absolute.startswith(PROJECT_ROOT):
        label = os.path.relpath(absolute, PROJECT_ROOT)
    else:
        # 标准库代码只保留最后一级文件名，避免标签过长把图撑坏
        label = os.path.basename(absolute)
    return "{0}:{1}({2})".format(label.replace("\\", "/"), lineno, func)


def _escape(text: str) -> str:
    """转义 XML 特殊字符。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(profiler: cProfile.Profile, path: str, top: int = 12) -> None:
    """把 cProfile 的 tottime 排名渲染成一张横向柱状图（SVG，无需第三方库）。

    这张图直接取自 :mod:`cProfile` 的真实采样数据，可以插进博客当作"性能分析图"。
    """
    entries: List[Tuple[float, float, int, str, int, str]] = []
    for (filename, lineno, func), (_cc, nc, tt, ct, _callers) in pstats.Stats(profiler).stats.items():
        entries.append((tt, ct, nc, filename, lineno, func))
    entries.sort(reverse=True)
    entries = entries[:top]
    if not entries:
        return
    max_tt = entries[0][0] or 1.0
    total_tt = pstats.Stats(profiler).total_tt

    row_height, label_width, chart_width = 28, 340, 420
    width = label_width + chart_width + 320
    height = 70 + row_height * len(entries) + 24

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="{0}" height="{1}" '
        'viewBox="0 0 {0} {1}" font-family="Consolas, Menlo, monospace">'.format(width, height),
        '<rect width="{0}" height="{1}" fill="#ffffff"/>'.format(width, height),
        '<text x="20" y="30" font-size="18" font-weight="bold" fill="#1f2937">'
        'cProfile 自身耗时(tottime)排名</text>',
        '<text x="20" y="52" font-size="13" fill="#6b7280">'
        '总耗时 {0:.3f} 秒　·　数据来源：python tools/benchmark.py --profile --svg</text>'.format(total_tt),
    ]
    for index, (tt, ct, nc, filename, lineno, func) in enumerate(entries):
        y = 70 + index * row_height
        bar_width = max(1.0, chart_width * tt / max_tt)
        parts.append(
            '<text x="{0}" y="{1}" font-size="12" fill="#374151" text-anchor="end">{2}</text>'.format(
                label_width, y + 17, _escape(_short_name(filename, lineno, func))))
        parts.append(
            '<rect x="{0}" y="{1}" width="{2:.1f}" height="18" fill="#3b82f6" rx="3"/>'.format(
                label_width + 10, y + 2, bar_width))
        parts.append(
            '<text x="{0:.1f}" y="{1}" font-size="12" fill="#6b7280">'
            '{2:.3f}s / 累计 {3:.3f}s / 调用 {4} 次</text>'.format(
                label_width + 18 + bar_width, y + 17, tt, ct, nc))
    parts.append("</svg>")

    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts))
    print("性能分析图已写入 {0}（可直接嵌入博客）。".format(path))


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        description="论文查重程序的性能基准与 cProfile 热点分析工具",
    )
    parser.add_argument("--size", type=int, default=None,
                        help="输入文本的字符数；不指定时按各模式的默认规模执行")
    parser.add_argument("--repeat", type=int, default=3,
                        help="每个规模重复测量次数，取最快值（默认 3）")
    parser.add_argument("--top", type=int, default=15,
                        help="热点排名展示的条目数（默认 15）")
    parser.add_argument("--dump", metavar="PATH", default=None,
                        help="把 cProfile 原始数据写入文件，供 snakeviz / gprof2dot 出图")
    parser.add_argument("--svg", metavar="PATH", default=None,
                        help="把 tottime 排名渲染成 SVG 柱状图（自包含，无需第三方库）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--profile", action="store_true",
                      help="输出 cProfile 热点排名（默认规模 {0} 字符）".format(DEFAULT_PROFILE_SIZE))
    mode.add_argument("--naive", action="store_true",
                      help="与 O(n*m) 动态规划基线对比（默认规模 {0} 字符）".format(DEFAULT_NAIVE_SIZE))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """脚本入口。"""
    args = build_parser().parse_args(argv)
    if args.naive:
        run_naive_comparison(args.size or DEFAULT_NAIVE_SIZE)
    elif args.profile or args.svg:
        run_profile(args.size or DEFAULT_PROFILE_SIZE, args.top, args.dump, args.svg)
    else:
        run_benchmark([args.size] if args.size else DEFAULT_SIZES, args.repeat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
