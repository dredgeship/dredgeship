# -*- coding: utf-8 -*-
"""通用小工具函数。"""

from typing import Union

Number = Union[int, float]


def clamp(value: Number, lower: float = 0.0, upper: float = 1.0) -> float:
    """把数值限制在 ``[lower, upper]`` 闭区间内。

    :param value: 待处理的数值。
    :param lower: 下界，默认 0.0。
    :param upper: 上界，默认 1.0。
    :return: 截断后的浮点数。
    """
    return max(lower, min(upper, float(value)))
