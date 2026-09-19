"""需求符合性校验脚本：对生成的题目逐条核对需求 1~8 的约束。

用法：
    python tools/verify.py -n 10000 -r 10      # 生成并校验一万道题
    python tools/verify.py --exercises Exercises.txt --answers Answers.txt  # 校验已有文件

校验项：
    1. 运算符个数不超过 3；
    2. 计算过程不产生负数（每个 e1 − e2 均满足 e1 ≥ e2）；
    3. 每个 e1 ÷ e2 的结果为真分数（0 < 结果 < 1）；
    4. 题目中的数值（自然数、带分数整数部分、真分数分母）均在 [0, r) 内；
    5. 题目互不重复（规范化键唯一）；
    6. 答案与重新求解的结果一致。
"""

from __future__ import annotations

import argparse
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arith.expression import DIVIDE, MINUS, Expr, canonical_key  # noqa: E402
from arith.fraction import format_number, parse_number  # noqa: E402
from arith.generator import ExerciseGenerator  # noqa: E402
from arith.parser import parse_expression  # noqa: E402


def check_expression(expr: Expr, r: int) -> list[str]:
    problems: list[str] = []
    if expr.operator_count() > 3:
        problems.append("运算符个数超过 3")
    for node in expr.walk():
        if node.op is None:  # 叶子节点：检查数值与分母的取值范围
            if node.value < 0:
                problems.append(f"出现负数：{node.value}")
            if node.value >= r:
                problems.append(f"数值超出范围：{node.value} >= {r}")
            if node.value.denominator != 1 and node.value.denominator >= r:
                problems.append(f"分母超出范围：{node.value.denominator} >= {r}")
            continue
        left, right = node.children()
        if node.op == MINUS and left.value < right.value:
            problems.append(f"减法产生负数：{node.to_string()}")
        elif node.op == DIVIDE and not (Fraction(0) < node.value < Fraction(1)):
            problems.append(f"除法结果不是真分数：{node.to_string()} = {node.value}")
    return problems


def check_answer(expr: Expr, answer: str) -> list[str]:
    expected = expr.value
    try:
        given = parse_number(answer)
    except ValueError:
        return [f"答案无法解析：{answer!r}"]
    return [] if given == expected else [f"答案错误：{expr.to_string()} 应为 {expected}，实为 {given}"]


def report(name: str, ok: bool, detail: str = "") -> bool:
    status = "通过" if ok else "不通过"
    print(f"[{status}] {name}{('：' + detail) if detail else ''}")
    return ok


def verify(exercises: list[Expr], answers: list[str], r: int) -> bool:
    passed = True
    problems: list[tuple[int, str]] = []
    keys = set()
    duplicates = 0
    for index, expr in enumerate(exercises, start=1):
        problems.extend((index, problem) for problem in check_expression(expr, r))
        key = canonical_key(expr)
        if key in keys:
            duplicates += 1
        keys.add(key)
    passed &= report("运算符个数 <= 3 / 无负数 / 除法为真分数 / 数值在范围内",
                     not problems, f"{len(problems)} 处违规")
    for index, problem in problems[:5]:
        print(f"        第 {index} 题：{problem}")
    passed &= report("题目互不重复", duplicates == 0, f"重复 {duplicates} 道")
    answer_problems: list[tuple[int, str]] = []
    for index, (expr, answer) in enumerate(zip(exercises, answers), start=1):
        answer_problems.extend((index, problem) for problem in check_answer(expr, answer))
    passed &= report("答案与重新求解结果一致", not answer_problems, f"{len(answer_problems)} 处不一致")
    return passed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Myapp 需求符合性校验")
    parser.add_argument("-n", type=int, default=10000, help="生成题目数量")
    parser.add_argument("-r", type=int, default=10, help="数值范围")
    parser.add_argument("--seed", type=int, default=20240919, help="随机种子")
    parser.add_argument("--exercises", default=None, help="已有题目文件（与 --answers 同时使用）")
    parser.add_argument("--answers", default=None, help="已有答案文件（与 --exercises 同时使用）")
    args = parser.parse_args(argv)

    if args.exercises and args.answers:
        with open(args.exercises, encoding="utf-8") as handle:
            exercise_lines = [line.strip() for line in handle if line.strip()]
        with open(args.answers, encoding="utf-8") as handle:
            answer_lines = [line.strip() for line in handle if line.strip()]
        exercises = [parse_expression(line) for line in exercise_lines]
        source = f"文件 {args.exercises}"
    else:
        exercises = ExerciseGenerator(r=args.r, seed=args.seed).generate(args.n)
        answer_lines = [format_number(expr.value) for expr in exercises]
        source = f"-n {args.n} -r {args.r}"

    print(f"校验对象：{source}，共 {len(exercises)} 道题")
    ok = verify(exercises, answer_lines, args.r)
    print("结论：全部通过" if ok else "结论：存在不通过项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
