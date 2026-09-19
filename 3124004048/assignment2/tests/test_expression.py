"""表达式模块单元测试：括号渲染、求值、规范化判重键。"""

import unittest
from fractions import Fraction
from typing import Union

from arith.expression import (
    ASCII_SYMBOLS,
    DIVIDE,
    MINUS,
    PLUS,
    TIMES,
    Expr,
    build,
    canonical_key,
    leaf,
)
from arith.parser import parse_expression


def n(value: Union[int, Fraction]) -> Expr:
    """构造叶子节点的测试辅助函数。"""
    return leaf(Fraction(value))


class TestRender(unittest.TestCase):
    def test_precedence_without_redundant_parenthesis(self):
        expr = build(PLUS, n(1), build(TIMES, n(2), n(3)))
        self.assertEqual(expr.to_string(ASCII_SYMBOLS), "1 + 2 * 3")

    def test_parenthesis_needed_on_left(self):
        expr = build(TIMES, build(PLUS, n(1), n(2)), n(3))
        self.assertEqual(expr.to_string(ASCII_SYMBOLS), "(1 + 2) * 3")

    def test_parenthesis_needed_on_right_same_precedence(self):
        """a - (b - c) 与 a - b - c 不同，必须保留括号。"""
        expr = build(MINUS, n(9), build(MINUS, n(5), n(2)))
        self.assertEqual(expr.to_string(ASCII_SYMBOLS), "9 - (5 - 2)")

    def test_left_associative_chain(self):
        expr = build(PLUS, build(PLUS, n(1), n(2)), n(3))
        self.assertEqual(expr.to_string(ASCII_SYMBOLS), "1 + 2 + 3")


class TestCanonicalKey(unittest.TestCase):
    def test_addition_commutative(self):
        left = build(PLUS, n(23), n(45))
        right = build(PLUS, n(45), n(23))
        self.assertEqual(canonical_key(left), canonical_key(right))

    def test_multiplication_commutative(self):
        left = build(TIMES, n(6), n(8))
        right = build(TIMES, n(8), n(6))
        self.assertEqual(canonical_key(left), canonical_key(right))

    def test_associative_plus_is_duplicate(self):
        """3 + (2 + 1) 与 1 + 2 + 3（即 (1+2)+3）是重复题目。"""
        first = build(PLUS, n(3), build(PLUS, n(2), n(1)))
        second = build(PLUS, build(PLUS, n(1), n(2)), n(3))
        self.assertEqual(canonical_key(first), canonical_key(second))

    def test_different_order_is_not_duplicate(self):
        """1 + 2 + 3 与 3 + 2 + 1 不是重复题目。"""
        first = build(PLUS, build(PLUS, n(1), n(2)), n(3))
        second = build(PLUS, build(PLUS, n(3), n(2)), n(1))
        self.assertNotEqual(canonical_key(first), canonical_key(second))

    def test_subtraction_not_commutative(self):
        first = build(MINUS, n(5), n(3))
        second = build(MINUS, n(3), n(5))
        self.assertNotEqual(canonical_key(first), canonical_key(second))

    def test_division_not_commutative(self):
        first = build(DIVIDE, n(1), n(2))
        second = build(DIVIDE, n(2), n(1))
        self.assertNotEqual(canonical_key(first), canonical_key(second))


class TestRoundTrip(unittest.TestCase):
    def test_rendered_text_evaluates_to_same_value(self):
        """渲染结果可被解析且值不变，说明括号规则正确。"""
        expr = build(DIVIDE, build(PLUS, n(1), n(2)), build(MINUS, n(7), n(Fraction(1, 3))))
        self.assertEqual(parse_expression(expr.to_string()).value, expr.value)


def run_tests() -> None:
    """``python -m tests.test_expression`` 单独运行本文件时的入口。"""
    program = unittest.main(exit=False)
    if not program.result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    run_tests()
