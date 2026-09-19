"""数值模块单元测试：格式化、解析与随机取值。"""

import unittest
from fractions import Fraction

from arith.fraction import format_number, number_pool, parse_number, random_number


class TestFormatNumber(unittest.TestCase):
    def test_natural_number(self):
        self.assertEqual(format_number(Fraction(0)), "0")
        self.assertEqual(format_number(Fraction(23)), "23")

    def test_proper_fraction(self):
        self.assertEqual(format_number(Fraction(3, 5)), "3/5")
        self.assertEqual(format_number(Fraction(1, 2)), "1/2")

    def test_mixed_number(self):
        self.assertEqual(format_number(Fraction(19, 8)), "2'3/8")
        self.assertEqual(format_number(Fraction(3, 2)), "1'1/2")

    def test_fraction_arithmetic_is_exact(self):
        """1/6 + 1/8 = 7/24，验证 Fraction 运算无精度误差。"""
        self.assertEqual(format_number(Fraction(1, 6) + Fraction(1, 8)), "7/24")


class TestParseNumber(unittest.TestCase):
    def test_parse_natural(self):
        self.assertEqual(parse_number("12"), Fraction(12))

    def test_parse_proper_fraction(self):
        self.assertEqual(parse_number("3/5"), Fraction(3, 5))

    def test_parse_mixed_number(self):
        self.assertEqual(parse_number("2'3/8"), Fraction(19, 8))

    def test_parse_full_width_separator(self):
        """兼容全角分隔符 2’3/8。"""
        self.assertEqual(parse_number("2’3/8"), Fraction(19, 8))

    def test_parse_invalid(self):
        for text in ("", "abc", "1/", "1/0", "'1/2"):
            with self.assertRaises(ValueError):
                parse_number(text)


class TestRandomNumber(unittest.TestCase):
    def test_values_within_range(self):
        import random

        rng = random.Random(1)
        for _ in range(2000):
            value = random_number(10, rng)
            self.assertGreaterEqual(value, 0)
            # 自然数 < 10；带分数整数部分 < 10，故整体 < 10
            self.assertLess(value, 10)
            self.assertLessEqual(value.denominator, 10)

    def test_range_one_only_produces_zero(self):
        import random

        rng = random.Random(2)
        self.assertEqual({random_number(1, rng) for _ in range(50)}, {Fraction(0)})

    def test_number_pool_covers_all_values(self):
        pool = number_pool(5)
        self.assertIn(Fraction(0), pool)
        self.assertIn(Fraction(4), pool)
        self.assertIn(Fraction(1, 2), pool)
        self.assertIn(Fraction(4, 2) + Fraction(1, 3), pool)  # 4'1/3
        self.assertEqual(len(pool), len(set(pool)))  # 只保留最简分数，无重复值
        # r=5：5 个自然数 + 5 个最简真分数（1/2, 1/3, 2/3, 1/4, 3/4）各配 4 个整数部分
        self.assertEqual(len(pool), 5 + 5 * 5)


def run_tests() -> None:
    """``python -m tests.test_fraction`` 单独运行本文件时的入口。"""
    program = unittest.main(exit=False)
    if not program.result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    run_tests()
