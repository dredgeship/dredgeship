"""生成模块单元测试：约束校验、去重与边界取值。"""

import unittest
from fractions import Fraction

from arith.expression import DIVIDE, MINUS, Expr, canonical_key
from arith.generator import ExerciseGenerator
from arith.parser import parse_expression


def check_constraints(expr: Expr) -> list[str]:
    """校验生成结果是否满足需求中的全部约束，返回违规描述列表。"""
    problems: list[str] = []
    for node in expr.walk():
        if node.op is None:  # 叶子节点：只检查取值范围
            continue
        left, right = node.children()
        if node.op == MINUS and left.value < right.value:
            problems.append(f"减法产生负数：{node.to_string()}")
        if node.op == DIVIDE and not (Fraction(0) < node.value < Fraction(1)):
            problems.append(f"除法结果不是真分数：{node.to_string()} = {node.value}")
    if expr.operator_count() > 3:
        problems.append("运算符个数超过 3")
    return problems


def sample_expressions(generator: ExerciseGenerator, size: int) -> list[Expr]:
    """连续取样 size 道题目，并保证返回值不是 Optional（便于类型检查）。"""
    samples: list[Expr] = []
    for _ in range(size):
        expr = generator.random_expression()
        assert expr is not None, "生成器在合法参数下不应返回 None"
        samples.append(expr)
    return samples


class TestConstraints(unittest.TestCase):
    def test_no_negative_and_proper_division(self):
        generator = ExerciseGenerator(r=10, seed=2024)
        for index, expr in enumerate(sample_expressions(generator, 500), start=1):
            for problem in check_constraints(expr):
                self.fail(f"第 {index} 道题违规：{problem}")

    def test_operator_count_not_exceed_three(self):
        generator = ExerciseGenerator(r=6, seed=7)
        counts = {expr.operator_count() for expr in sample_expressions(generator, 300)}
        self.assertTrue(counts.issubset({1, 2, 3}))

    def test_rendered_expression_is_parseable(self):
        generator = ExerciseGenerator(r=10, seed=99)
        for expr in sample_expressions(generator, 200):
            self.assertEqual(parse_expression(expr.to_string()).value, expr.value)


class TestUniqueness(unittest.TestCase):
    def test_generated_exercises_are_unique(self):
        generator = ExerciseGenerator(r=10, seed=314)
        exercises = generator.generate(2000)
        self.assertEqual(len(exercises), 2000)
        keys = [canonical_key(expr) for expr in exercises]
        self.assertEqual(len(set(keys)), 2000)

    def test_duplicate_expressions_are_rejected(self):
        """同一道题被生成两次时只会保留一次（用固定种子验证不崩溃）。"""
        generator = ExerciseGenerator(r=3, seed=5)
        exercises = generator.generate(50)
        keys = [canonical_key(expr) for expr in exercises]
        self.assertEqual(len(keys), len(set(keys)))


class TestEdgeCases(unittest.TestCase):
    def test_range_one_does_not_crash(self):
        generator = ExerciseGenerator(r=1, seed=1)
        exercises = generator.generate(10)
        self.assertGreater(len(exercises), 0)
        self.assertEqual({expr.value for expr in exercises}, {Fraction(0)})

    def test_invalid_range(self):
        with self.assertRaises(ValueError):
            _ = ExerciseGenerator(r=0)

    def test_generate_zero(self):
        self.assertEqual(ExerciseGenerator(r=10).generate(0), [])


def run_tests() -> None:
    """``python -m tests.test_generator`` 单独运行本文件时的入口。"""
    program = unittest.main(exit=False)
    if not program.result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    run_tests()
