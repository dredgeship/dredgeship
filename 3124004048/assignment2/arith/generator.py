"""生成模块：带约束的随机四则运算题目生成与去重。

约束（需求 3）：
    1. 计算过程不能产生负数：形如 e1 − e2 的子表达式必须 e1 ≥ e2；
    2. 形如 e1 ÷ e2 的子表达式，其结果必须是真分数（0 < 结果 < 1）；
    3. 每道题目中运算符个数不超过 3 个；
    4. 一次运行生成的题目不能重复（用规范化键判重）。

生成策略：自顶向下随机拆分运算符个数 → 自底向上即时校验约束 →
失败则在该节点内重试，仍失败则交由上层重新拆分。这样避免「先生成后丢弃」
造成的大量无效计算，是支撑一万道题生成的关键。
"""

from __future__ import annotations

import random
from typing import Optional

from .expression import (
    DIVIDE,
    MINUS,
    OPERATORS,
    Expr,
    build,
    canonical_key,
    leaf,
)
from .fraction import number_pool

# 运算符权重：+ 与 − 略多，× 与 ÷ 稍少（÷ 的约束最严，命中率低）
_OPERATOR_WEIGHTS = (0.3, 0.3, 0.2, 0.2)
# 权重放大 10 倍后展开成抽样袋，用一次 randrange 代替 choices 的累积权重计算
_OPERATOR_SAMPLE_SCALE = 10


class ExerciseGenerator:
    """四则运算题目生成器。"""

    def __init__(
        self,
        r: int,
        seed: Optional[int] = None,
        max_operators: int = 3,
        retry: int = 24,
    ):
        if r is None or r < 1:
            raise ValueError("-r 参数必须是自然数且不小于 1")
        if max_operators < 1:
            raise ValueError("运算符个数上限必须不小于 1")
        self.r = r
        self.max_operators = max_operators
        self.retry = retry
        self.rng = random.Random(seed)
        # 性能优化 1：数值候选集一次性预取（拒绝采样只做一次），
        # 之后每次取数只需一次 randrange，且复用同一个 Fraction 对象。
        self._pool = number_pool(r)
        # 性能优化 2：运算符按权重展开为抽样袋，避免 choices() 的累积权重计算。
        self._operator_bag: list[str] = []
        for op, weight in zip(OPERATORS, _OPERATOR_WEIGHTS):
            self._operator_bag.extend([op] * int(weight * _OPERATOR_SAMPLE_SCALE))

    # ------------------------------------------------------------------
    # 构造表达式树
    # ------------------------------------------------------------------
    def _random_operand(self) -> Expr:
        return leaf(self.rng.choice(self._pool))

    def _pick_operator(self) -> str:
        return self.rng.choice(self._operator_bag)

    def _combine(self, op: str, left: Expr, right: Expr) -> Optional[Expr]:
        """在满足约束的前提下合并两个子表达式，不满足返回 None。"""
        if op == MINUS and left.value < right.value:
            return None  # 计算过程不能产生负数
        if op == DIVIDE:
            if left.value <= 0 or right.value <= 0:
                return None  # 除数与被除数都必须是正数，且结果不能为 0
            if left.value >= right.value:
                return None  # 除法结果必须是真分数（< 1）
        return build(op, left, right)

    def _build(self, operators: int) -> Optional[Expr]:
        """构造一棵恰含 ``operators`` 个运算符且满足全部约束的表达式树。"""
        if operators <= 0:
            return self._random_operand()
        for _ in range(self.retry):
            op = self._pick_operator()
            left_operators = self.rng.randrange(operators)
            right_operators = operators - 1 - left_operators
            left = self._build(left_operators)
            if left is None:
                continue
            right = self._build(right_operators)
            if right is None:
                continue
            node = self._combine(op, left, right)
            if node is not None:
                return node
        return None

    def random_expression(self) -> Optional[Expr]:
        """随机生成一道题目（运算符个数 1 ~ max_operators）。"""
        operators = self.rng.randint(1, self.max_operators)
        return self._build(operators)

    # ------------------------------------------------------------------
    # 批量生成与去重
    # ------------------------------------------------------------------
    def generate(self, count: int) -> list[Expr]:
        """生成 ``count`` 道互不重复的题目；空间不足时返回实际可生成的结果。"""
        if count <= 0:
            return []
        exercises: list[Expr] = []
        seen: set[tuple[object, ...]] = set()
        stale = 0  # 连续失败（重复或不满足约束）次数
        stale_limit = max(2000, count * 20)
        while len(exercises) < count and stale < stale_limit:
            expr = self.random_expression()
            if expr is None:
                stale += 1
                continue
            key = canonical_key(expr)
            if key in seen:
                stale += 1
                continue
            seen.add(key)
            exercises.append(expr)
            stale = 0
        return exercises
