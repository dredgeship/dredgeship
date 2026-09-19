"""数值模块：自然数 / 真分数 / 带分数的表示、格式化、解析与随机生成。

需求约定：
    * 自然数：0, 1, 2, ...
    * 真分数：1/2, 1/3, 2/3, 1/4, ...
    * 带分数：1'1/2, 2'3/8, ...
    * 取值范围由 ``-r`` 控制：自然数与带分数的整数部分取自 [0, r)，
      真分数（含带分数的真分数部分）的分母取自 (1, r)。

模块内部统一使用 :class:`fractions.Fraction` 表示数值，
从而彻底避免浮点误差，保证 1/6 + 1/8 精确等于 7/24。
"""

from __future__ import annotations

import random
from fractions import Fraction
from math import gcd

# 带分数分隔符，形如 2'3/8
MIXED_SEPARATOR = "'"
# 解析时兼容可能被误输入的全角 / 中文分隔符
_SEPARATOR_ALIASES = {
    "’": MIXED_SEPARATOR,
    "‘": MIXED_SEPARATOR,
    "′": MIXED_SEPARATOR,
    "`": MIXED_SEPARATOR,
}

ZERO = Fraction(0)


def format_number(value: Fraction) -> str:
    """把数值格式化为题目与答案中的书写形式（自然数 / 真分数 / 带分数）。"""
    value = Fraction(value)
    sign = "-" if value < ZERO else ""
    if value < ZERO:
        value = -value
    integer, remainder = divmod(value.numerator, value.denominator)
    if remainder == 0:
        return f"{sign}{integer}"
    numerator, denominator = remainder, value.denominator
    if integer:
        return f"{sign}{integer}{MIXED_SEPARATOR}{numerator}/{denominator}"
    return f"{sign}{numerator}/{denominator}"


def _parse_fraction(text: str) -> Fraction:
    """解析形如 ``a/b`` 的分数文本，分母为 0 时报错。"""
    numerator_text, _, denominator_text = text.partition("/")
    if not numerator_text or not denominator_text:
        raise ValueError(f"非法的分数：{text!r}")
    denominator = int(denominator_text)
    if denominator == 0:
        raise ValueError(f"分母不能为 0：{text!r}")
    return Fraction(int(numerator_text), denominator)


def parse_number(text: str) -> Fraction:
    """解析 ``3``、``3/5``、``2'3/8``（也兼容 ``2’3/8``）等写法。"""
    token = text.strip()
    if not token:
        raise ValueError("空字符串不是合法的数值")
    for alias, separator in _SEPARATOR_ALIASES.items():
        token = token.replace(alias, separator)

    negative = token.startswith("-")
    if token[0] in "+-":
        token = token[1:]
    if not token:
        raise ValueError(f"非法的数值：{text!r}")

    if MIXED_SEPARATOR in token:
        integer_text, _, fraction_text = token.partition(MIXED_SEPARATOR)
        if not integer_text or "/" not in fraction_text:
            raise ValueError(f"非法的带分数：{text!r}")
        value = Fraction(int(integer_text)) + _parse_fraction(fraction_text)
    elif "/" in token:
        value = _parse_fraction(token)
    else:
        value = Fraction(int(token))
    return -value if negative else value


def _random_proper_fraction(r: int, rng: random.Random) -> Fraction:
    """随机生成一个分母小于 r 的最简真分数（0 < 值 < 1）。"""
    for _ in range(12):  # 拒绝采样：只要分子分母互质
        denominator = rng.randrange(2, r)
        numerator = rng.randrange(1, denominator)
        if gcd(numerator, denominator) == 1:
            return Fraction(numerator, denominator)
    return Fraction(1, max(2, r - 1))  # 极端情况下的兜底值


def random_number(r: int, rng: random.Random) -> Fraction:
    """在 [0, r) 范围内随机生成一个自然数、真分数或带分数。"""
    if r < 3:  # 真分数分母至少为 2，故 r < 3 时只可能取自然数
        return Fraction(rng.randrange(r))
    roll = rng.random()
    if roll < 0.5:
        return Fraction(rng.randrange(r))
    if roll < 0.75:
        return _random_proper_fraction(r, rng)
    return Fraction(rng.randrange(1, r)) + _random_proper_fraction(r, rng)


def number_pool(r: int) -> list[Fraction]:
    """穷举 [0, r) 内所有合法的自然数、最简真分数与带分数。

    生成器可预先构建该候选集，把「每次取数都要拒绝采样」变成 O(1) 抽样，
    这是生成一万道题时的主要性能优化点之一。
    """
    pool: list[Fraction] = [Fraction(i) for i in range(r)]
    for denominator in range(2, r):
        for numerator in range(1, denominator):
            if gcd(numerator, denominator) != 1:
                continue
            proper = Fraction(numerator, denominator)
            pool.append(proper)
            for integer_part in range(1, r):
                pool.append(integer_part + proper)
    return pool
