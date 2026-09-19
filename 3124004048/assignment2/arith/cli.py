"""命令行模块：参数解析与两种运行模式（生成 / 判题）的流程编排。

用法：
    Myapp.exe -n 10 -r 10              生成 10 道 10 以内的四则运算题目
    Myapp.exe -e Exercises.txt -a Answers.txt   判定答案并输出 Grade.txt
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from .expression import ASCII_SYMBOLS, UNICODE_SYMBOLS
from .fraction import format_number
from .generator import ExerciseGenerator
from .grader import DEFAULT_GRADE_FILE, grade

DEFAULT_EXERCISE_FILE = "Exercises.txt"
DEFAULT_ANSWER_FILE = "Answers.txt"
DEFAULT_COUNT = 10

DESCRIPTION = "小学四则运算题目自动生成与判题程序"
EPILOG = (
    "示例：\n"
    "  Myapp.exe -n 10 -r 10            生成 10 道数值范围在 10 以内的题目\n"
    "  Myapp.exe -n 10000 -r 10         生成一万道题目\n"
    "  Myapp.exe -e Exercises.txt -a Answers.txt   判定答案，结果写入 Grade.txt\n"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Myapp",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-n", dest="count", type=int, default=DEFAULT_COUNT,
                        help=f"生成题目的个数（默认 {DEFAULT_COUNT}）")
    parser.add_argument("-r", dest="range", type=int, default=None,
                        help="数值（自然数、真分数及真分数分母）的上界，不含该值；生成题目时必填")
    parser.add_argument("-e", dest="exercise", default=None, help="题目文件路径（判题模式）")
    parser.add_argument("-a", dest="answer", default=None, help="答案文件路径（判题模式）")
    parser.add_argument("--ascii", dest="ascii", action="store_true",
                        help="用 ASCII 运算符（+ - * /）输出，默认使用需求中的排版运算符")
    parser.add_argument("--seed", dest="seed", type=int, default=None, help="随机种子，便于复现")
    return parser


def configure_stdio() -> None:
    """中文 Windows 控制台默认 GBK，打印 − × ÷ 等符号会抛 UnicodeEncodeError。

    这里把标准输出/错误的编码错误策略改成 replace，保证帮助信息在任何终端都能打印。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # Python 3.6 及以下或已被重定向为普通文件
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 极端环境忽略
            pass


def run_generate(count: int, r: int, ascii_symbols: bool = False,
                 seed: Optional[int] = None) -> int:
    """生成题目与答案，分别写入当前目录下的 Exercises.txt 与 Answers.txt。"""
    generator = ExerciseGenerator(r=r, seed=seed)
    exercises = generator.generate(count)
    if len(exercises) < count:
        print(
            f"警告：-r {r} 的取值范围内最多只能生成 {len(exercises)} 道互不重复的题目。",
            file=sys.stderr,
        )
    symbols = ASCII_SYMBOLS if ascii_symbols else UNICODE_SYMBOLS
    with open(DEFAULT_EXERCISE_FILE, "w", encoding="utf-8") as exercise_file, open(
        DEFAULT_ANSWER_FILE, "w", encoding="utf-8"
    ) as answer_file:
        for expr in exercises:
            exercise_file.write(f"{expr.to_string(symbols)} =\n")
            answer_file.write(f"{format_number(expr.value)}\n")
    print(f"已生成 {len(exercises)} 道题目：{os.path.abspath(DEFAULT_EXERCISE_FILE)}")
    print(f"对应答案已写入：{os.path.abspath(DEFAULT_ANSWER_FILE)}")
    return 0


def run_grade(exercise_file: str, answer_file: str,
              grade_file: str = DEFAULT_GRADE_FILE) -> int:
    """判定答案对错并输出 Grade.txt。"""
    for path in (exercise_file, answer_file):
        if not os.path.isfile(path):
            print(f"错误：找不到文件 {path}", file=sys.stderr)
            return 1
    result = grade(exercise_file, answer_file, grade_file)
    correct, wrong = result["correct"], result["wrong"]
    print(f"Correct: {len(correct)} ({', '.join(str(n) for n in correct)})")
    print(f"Wrong: {len(wrong)} ({', '.join(str(n) for n in wrong)})")
    print(f"统计结果已写入：{os.path.abspath(grade_file)}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.exercise or args.answer:
        if not (args.exercise and args.answer):
            build_parser().error("判题模式需要同时给定 -e <题目文件> 与 -a <答案文件>")
        return run_grade(args.exercise, args.answer)

    if args.range is None:
        build_parser().error("生成题目必须使用 -r 指定数值范围，例如：-r 10")
    if args.range < 1:
        build_parser().error("-r 必须是正整数")
    if args.count < 1:
        build_parser().error("-n 必须是正整数")
    return run_generate(args.count, args.range, args.ascii, args.seed)
