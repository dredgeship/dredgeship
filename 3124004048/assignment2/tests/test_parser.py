"""解析模块单元测试：词法分析、优先级、结合性与错误处理。"""

import unittest
from fractions import Fraction

from arith.parser import ExpressionError, evaluate, parse_expression, tokenize


class TestTokenize(unittest.TestCase):
    def test_tokens(self):
        tokens = tokenize("1/2 + 2'3/8 × (3 − 1)")
        kinds = [kind for kind, _ in tokens]
        self.assertEqual(kinds, ["number", "operator", "number", "operator", "lparen",
                                 "number", "operator", "number", "rparen"])

    def test_unknown_character(self):
        with self.assertRaises(ExpressionError):
            tokenize("1 & 2")


class TestEvaluate(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(evaluate("1 + 2 × 3"), Fraction(7))

    def test_left_associativity(self):
        self.assertEqual(evaluate("10 − 3 − 2"), Fraction(5))
        self.assertEqual(evaluate("10 − (3 − 2)"), Fraction(9))
        self.assertEqual(evaluate("12 ÷ 3 ÷ 2"), Fraction(2))

    def test_fraction_expression(self):
        self.assertEqual(evaluate("1/6 + 1/8"), Fraction(7, 24))
        self.assertEqual(evaluate("2'3/8 − 1/4"), Fraction(17, 8))

    def test_ascii_operators(self):
        self.assertEqual(evaluate("(1 + 2) * 3 / 2"), Fraction(9, 2))

    def test_question_line_with_equal_sign(self):
        self.assertEqual(evaluate("3 + 4 ="), Fraction(7))
        self.assertEqual(evaluate("3. 3 + 4 ="), Fraction(7))


class TestErrors(unittest.TestCase):
    def test_division_by_zero(self):
        with self.assertRaises(ZeroDivisionError):
            evaluate("1 ÷ 0")

    def test_unbalanced_parenthesis(self):
        with self.assertRaises(ExpressionError):
            evaluate("(1 + 2")

    def test_empty_expression(self):
        with self.assertRaises(ExpressionError):
            evaluate("   ")

    def test_trailing_content(self):
        with self.assertRaises(ExpressionError):
            evaluate("1 2")


class TestParseTree(unittest.TestCase):
    def test_operator_count(self):
        self.assertEqual(parse_expression("1 + 2 × (3 − 4) =").operator_count(), 3)


def run_tests() -> None:
    """``python -m tests.test_parser`` 单独运行本文件时的入口。"""
    program = unittest.main(exit=False)
    if not program.result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    run_tests()
