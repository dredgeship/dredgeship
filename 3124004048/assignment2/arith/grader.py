"""判题模块：比对题目文件与答案文件，并输出 Grade.txt 统计结果。

判定方式：先用 :mod:`arith.parser` 重新求解题目得到标准答案，
再把用户答案解析为 Fraction 后与标准答案比较（因此 ``2'3/8`` 与 ``19/8``
视为同一答案），任何无法解析的行都记为错误。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from .fraction import parse_number
from .parser import ExpressionError, parse_expression, strip_index

DEFAULT_GRADE_FILE = "Grade.txt"


def _read_lines(path: str, keep_empty: bool = False) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle.read().splitlines()]
    if not keep_empty:
        lines = [line for line in lines if line]
    else:  # 仅去掉末尾的空行，避免文件结尾换行符造成编号错位
        while lines and not lines[-1]:
            lines.pop()
    return lines


def _expected_answer(line: str) -> Optional[Fraction]:
    try:
        return parse_expression(strip_index(line)).value
    except (ExpressionError, ValueError, ZeroDivisionError):
        return None


def _given_answer(line: str) -> Optional[Fraction]:
    try:
        return parse_number(strip_index(line))
    except (ValueError, ZeroDivisionError):
        return None


def _format_result(label: str, numbers: list[int]) -> str:
    joined = ", ".join(str(number) for number in numbers)
    return f"{label}: {len(numbers)} ({joined})"


def grade(
    exercise_file: str,
    answer_file: str,
    grade_file: str = DEFAULT_GRADE_FILE,
) -> dict[str, list[int]]:
    """判题并把统计结果写入 Grade.txt，返回 {"correct": [...], "wrong": [...]}。"""
    exercises = _read_lines(exercise_file)
    answers = _read_lines(answer_file, keep_empty=True)

    correct: list[int] = []
    wrong: list[int] = []
    for index in range(max(len(exercises), len(answers))):
        number = index + 1
        expected = _expected_answer(exercises[index]) if index < len(exercises) else None
        given = _given_answer(answers[index]) if index < len(answers) else None
        if expected is not None and given is not None and expected == given:
            correct.append(number)
        else:
            wrong.append(number)

    with open(grade_file, "w", encoding="utf-8") as handle:
        handle.write(_format_result("Correct", correct) + "\n")
        handle.write(_format_result("Wrong", wrong) + "\n")
    return {"correct": correct, "wrong": wrong}
