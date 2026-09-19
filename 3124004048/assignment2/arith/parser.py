"""解析模块：四则运算表达式的词法分析与递归下降解析。

支持需求中的 ``+ − × ÷`` 以及常用的 ASCII 写法 ``+ - * /``，
支持括号与真分数 / 带分数（``1/2``、``2'3/8``、``2’3/8``）。

文法（消除左递归，体现左结合与优先级）：
    expr   := term (('+' | '−') term)*
    term   := factor (('×' | '÷') factor)*
    factor := number | '(' expr ')'
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Optional

from .expression import DIVIDE, MINUS, PLUS, TIMES, Expr, build, leaf
from .fraction import parse_number

_TOKEN_PATTERN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>\d+(?:['’]\d+)?(?:/\d+)?)
  | (?P<operator>[+\-−*×/÷])
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<equal>=)
    """,
    re.VERBOSE,
)

_OPERATOR_ALIASES = {
    "+": PLUS,
    "−": MINUS,
    "-": MINUS,
    "*": TIMES,
    "×": TIMES,
    "/": DIVIDE,
    "÷": DIVIDE,
}

# 形如 "3."、"3、"、"3)" 的题号前缀，判题时容错剥离
_INDEX_PATTERN = re.compile(r"^\s*\d+\s*[.、)）:：]\s*")


class ExpressionError(ValueError):
    """表达式解析失败。"""


def tokenize(text: str) -> list[tuple[str, str]]:
    """把表达式文本切分为 (类型, 文本) 记号序列。"""
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        match = _TOKEN_PATTERN.match(text, position)
        if match is None:
            raise ExpressionError(f"无法识别的字符：{text[position]!r}（位于第 {position + 1} 个字符）")
        position = match.end()
        kind = match.lastgroup
        if kind == "space":
            continue
        tokens.append((kind, match.group()))
    return tokens


class _RecursiveDescentParser:
    """递归下降解析器：解析的同时自底向上构造表达式树并求值。"""

    def __init__(self, tokens: list[tuple[str, str]]):
        self._tokens = tokens
        self._position = 0

    def _peek(self) -> Optional[tuple[str, str]]:
        if self._position < len(self._tokens):
            return self._tokens[self._position]
        return None

    def _accept_operator(self, allowed: str) -> Optional[str]:
        """若下一个记号是 allowed 中的运算符则消费它并返回内部记号。"""
        token = self._peek()
        if token is None or token[0] != "operator":
            return None
        op = _OPERATOR_ALIASES.get(token[1])
        if op is None or op not in allowed:
            return None
        self._position += 1
        return op

    def parse(self) -> Expr:
        node = self._parse_expression()
        if self._position != len(self._tokens):
            token = self._tokens[self._position]
            raise ExpressionError(f"表达式末尾存在多余内容：{token[1]!r}")
        return node

    def _parse_expression(self) -> Expr:
        node = self._parse_term()
        while True:
            op = self._accept_operator(PLUS + MINUS)
            if op is None:
                return node
            node = build(op, node, self._parse_term())

    def _parse_term(self) -> Expr:
        node = self._parse_factor()
        while True:
            op = self._accept_operator(TIMES + DIVIDE)
            if op is None:
                return node
            node = build(op, node, self._parse_factor())

    def _parse_factor(self) -> Expr:
        token = self._peek()
        if token is None:
            raise ExpressionError("表达式不完整")
        kind, text = token
        if kind == "number":
            self._position += 1
            return leaf(parse_number(text))
        if kind == "lparen":
            self._position += 1
            node = self._parse_expression()
            closing = self._peek()
            if closing is None or closing[0] != "rparen":
                raise ExpressionError("括号不匹配：缺少 ')'")
            self._position += 1
            return node
        raise ExpressionError(f"意外的记号：{text!r}")


def strip_index(line: str) -> str:
    """剥离行首可能存在的题号（如 ``3.``、``3、``）。"""
    return _INDEX_PATTERN.sub("", line, count=1)


def parse_expression(text: str) -> Expr:
    """解析一行题目文本（可带等号与题号），返回表达式树。"""
    cleaned = strip_index(text).strip()
    if cleaned.endswith("="):  # 题目行的等号只是题面的一部分
        cleaned = cleaned[:-1].strip()
    if not cleaned:
        raise ExpressionError("空表达式")
    return _RecursiveDescentParser(tokenize(cleaned)).parse()


def evaluate(text: str) -> Fraction:
    """解析并求值一行题目文本，返回 Fraction 形式的答案。"""
    return parse_expression(text).value
