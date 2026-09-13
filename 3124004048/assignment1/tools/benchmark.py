# -*- coding: utf-8 -*-
"""性能基准与 cProfile 分析脚本（仅供开发与博客展示使用）。

用法::

    python tools/benchmark.py              # 各规模文本耗时
    python tools/benchmark.py --profile    # 额外输出 cProfile 耗时排名
    python tools/benchmark.py --naive      # 与 O(n*m) 动态规划基线对比

说明：脚本只在内存中生成文本，不会读写任何文件，也不参与正式查重流程。
"""

import cProfile
import io
import os
import pstats
import random
import sys
import time
from typing import List

# 让 tools/ 目录下的脚本可以直接 import 到项目根目录的 dupcheck 包
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dupcheck.similarity import compute_similarity  # noqa: E402

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


def run_benchmark(sizes: List[int]) -> None:
    """输出各规模文本的查重耗时。"""
    print("规模(字符)   重复率   耗时(秒)   速度(字/秒)")
    print("-" * 52)
    for size in sizes:
        original = generate_text(size)
        suspect = mutate(original)
        start = time.perf_counter()
        score = compute_similarity(original, suspect)
        elapsed = time.perf_counter() - start
        speed = size / elapsed if elapsed > 0 else float("inf")
        print("{0:>10}   {1:>6.2f}   {2:>8.4f}   {3:>12.0f}".format(
            size, score, elapsed, speed))


def run_naive_comparison(size: int = 1200) -> None:
    """对比 O(n*m) 动态规划基线与当前实现。"""
    original = generate_text(size)
    suspect = mutate(original)
    normalized_a = "".join(ch for ch in original if ch.isalnum()).casefold()
    normalized_b = "".join(ch for ch in suspect if ch.isalnum()).casefold()

    start = time.perf_counter()
    lcs = naive_lcs_length(normalized_a, normalized_b)
    naive_elapsed = time.perf_counter() - start
    naive_score = 2.0 * lcs / (len(normalized_a) + len(normalized_b))

    start = time.perf_counter()
    score = compute_similarity(original, suspect)
    fast_elapsed = time.perf_counter() - start

    print("输入规模: {0} 字符".format(size))
    print("动态规划基线: 得分 {0:.4f}, 耗时 {1:.4f} 秒".format(naive_score, naive_elapsed))
    print("当前实现:     得分 {0:.4f}, 耗时 {1:.4f} 秒".format(score, fast_elapsed))
    if fast_elapsed > 0:
        print("加速比: {0:.1f}x".format(naive_elapsed / fast_elapsed))


def run_profile(size: int = 20000, top: int = 15) -> None:
    """使用 cProfile 输出耗时排名，用于定位性能瓶颈。"""
    original = generate_text(size)
    suspect = mutate(original)
    profiler = cProfile.Profile()
    profiler.enable()
    compute_similarity(original, suspect)
    profiler.disable()

    buffer = io.StringIO()
    stats = pstats.Stats(profiler, stream=buffer).sort_stats("cumulative")
    stats.print_stats(top)
    print(buffer.getvalue())


def main() -> int:
    """脚本入口。"""
    sizes = [10000, 50000, 100000, 500000]
    if "--profile" in sys.argv:
        run_profile()
        return 0
    if "--naive" in sys.argv:
        run_naive_comparison()
        return 0
    run_benchmark(sizes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
