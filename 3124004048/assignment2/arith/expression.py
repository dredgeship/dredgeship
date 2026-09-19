"""表达式模块：表达式树的构造、求值、字符串化与规范化（用于题目去重）。

表达式文法（需求原文）：
    e = n | e1 + e2 | e1 − e2 | e1 × e2 | e1 ÷ e2 | (e)

设计要点：
    1. 树节点在构造时即完成求值，因此约束检查（不出现负数、除法结果为真分数）
       可以在生成阶段自底向上即时判定，无需二次遍历；
    2. 输出时按「左结合 + 优先级」最小化括号：左孩子优先级更低才加括号，
       右孩子优先级更低或相等都要加括号（如 a − (b − c) 不能写成 a − b − c）；
    3. 规范化键用于判重：对 + 与 × 递归地对左右子树排序，
       即把「有限次交换 + 和 × 左右表达式」视为等价。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterator, Mapping, Optional

from .fraction import format_number

# 运算符内部记号（ASCII）
PLUS = "+"
MINUS = "-"
TIMES = "*"
DIVIDE = "/"

OPERATORS = (PLUS, MINUS, TIMES, DIVIDE)
COMMUTATIVE_OPERATORS = (PLUS, TIMES)

# 输出符号：默认使用需求中的排版符号，--ascii 时切换为 ASCII
UNICODE_SYMBOLS = {PLUS: "+", MINUS: "−", TIMES: "×", DIVIDE: "÷"}
ASCII_SYMBOLS = {PLUS: "+", MINUS: "-", TIMES: "*", DIVIDE: "/"}

PRECEDENCE = {PLUS: 1, MINUS: 1, TIMES: 2, DIVIDE: 2}


def apply(op: str, left: Fraction, right: Fraction) -> Fraction:
    """对两个数值执行四则运算，除数为 0 时抛出 ZeroDivisionError。"""
    if op == PLUS:
        return left + right
    if op == MINUS:
        return left - right
    if op == TIMES:
        return left * right
    if op == DIVIDE:
        if right == 0:
            raise ZeroDivisionError("除数不能为 0")
        return left / right
    raise ValueError(f"不支持的运算符：{op!r}")


@dataclass(frozen=True)
class Expr:
    """四则运算表达式树；叶子节点的 ``op`` 为 None。"""

    value: Fraction
    op: Optional[str] = None
    left: Optional["Expr"] = None
    right: Optional["Expr"] = None

    @property
    def is_leaf(self) -> bool:
        return self.op is None

    def children(self) -> tuple["Expr", "Expr"]:
        """返回左右子树；叶子节点没有子树，调用即报错。

        提供该方法是为了让外部代码拿到非 Optional 的子节点，
        避免静态检查中出现「可能为 None」的成员访问告警。
        """
        if self.left is None or self.right is None:
            raise ValueError("叶子节点没有子树")
        return self.left, self.right

    def operator_count(self) -> int:
        """统计表达式中运算符的个数（需求：每道题不超过 3 个）。"""
        if self.op is None:
            return 0
        left, right = self.children()
        return 1 + left.operator_count() + right.operator_count()

    def walk(self) -> Iterator["Expr"]:
        """前序遍历整棵树（含自身），用于约束校验与测试。"""
        yield self
        if self.op is None:
            return
        left, right = self.children()
        yield from left.walk()
        yield from right.walk()

    def to_string(self, symbols: Optional[Mapping[str, str]] = None) -> str:
        """渲染为题目文本（含最小必要括号），不含等号。"""
        return self._render(symbols or UNICODE_SYMBOLS, None, False)

    def _render(
        self,
        symbols: Mapping[str, str],
        parent_precedence: Optional[int],
        is_right_child: bool,
    ) -> str:
        if self.op is None:
            return format_number(self.value)
        left, right = self.children()
        precedence = PRECEDENCE[self.op]
        need_parenthesis = parent_precedence is not None and (
            precedence < parent_precedence
            or (precedence == parent_precedence and is_right_child)
        )
        text = "{0} {1} {2}".format(
            left._render(symbols, precedence, False),
            symbols[self.op],
            right._render(symbols, precedence, True),
        )
        return f"({text})" if need_parenthesis else text

    def __str__(self) -> str:  # pragma: no cover - 便于调试
        return self.to_string()


def leaf(value: Fraction) -> Expr:
    """构造叶子节点（自然数 / 真分数 / 带分数）。

    性能说明：叶子节点是生成阶段最频繁构造的对象，
    因此当传入值已经是 Fraction 时跳过 ``Fraction()`` 的重新归一化（含 gcd 计算）。
    """
    return Expr(value=value if type(value) is Fraction else Fraction(value))


def build(op: str, left: Expr, right: Expr) -> Expr:
    """构造运算节点，构造时即完成求值。"""
    return Expr(value=apply(op, left.value, right.value), op=op, left=left, right=right)


def canonical_key(expr: Expr) -> tuple[object, ...]:
    """计算表达式的规范化键（判重依据）。

    规则：+ 与 × 的左右子树可以有限次交换，因此对子树的规范化键排序；
    而 − 与 ÷ 不可交换，保持原序。这样 23 + 45 与 45 + 23、
    6 × 8 与 8 × 6、3 + (2 + 1) 与 1 + 2 + 3 都会得到相同的键；
    而 1 + 2 + 3（即 (1+2)+3）与 3 + 2 + 1（即 (3+2)+1）键不同。
    """
    if expr.op is None:
        return ("v", expr.value)
    left, right = expr.children()
    if expr.op in COMMUTATIVE_OPERATORS:
        left_key = canonical_key(left)
        right_key = canonical_key(right)
        return (expr.op, (left_key, right_key) if left_key <= right_key else (right_key, left_key))
    return (expr.op, canonical_key(left), canonical_key(right))
