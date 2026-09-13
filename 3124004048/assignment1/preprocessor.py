# -*- coding: utf-8 -*-
"""文本预处理模块。

职责：把"人写的文本"转换成"算法能直接比较的规范化字符序列"。

规范化步骤
----------
1. **NFKC 兼容性归一化**：把全角字母、数字、标点折叠成半角，
   例如 ``ＡＢＣ``、``１２３``、``，`` 会分别变成 ``ABC``、``123``、``,``；
2. **统一大小写**：使用 :meth:`str.casefold`（比 ``lower()`` 覆盖更广，
   例如德语 ``ß``）；
3. **只保留有效字符**：汉字、字母、数字；剔除空白、标点、表情符号等噪声。

这样处理后，``"晴天，天气晴朗"`` 与 ``"晴天 天气 晴朗"`` 会得到完全相同的
字符序列，标点与空白不再影响查重结果。
"""

import unicodedata
from typing import List


def _is_meaningful(char: str) -> bool:
    """判断单个字符是否为需要保留的有效字符。

    :param char: 长度为 1 的字符串。
    :return: 汉字 / 字母 / 数字返回 ``True``，其余（标点、空白等）返回 ``False``。
    """
    # str.isalnum() 对汉字同样返回 True，因此无需额外判断 CJK 区间
    return char.isalnum()


def normalize(text: str) -> str:
    """把原始文本规范化为紧凑的小写字符序列。

    :param text: 原始文本，允许为空串或 ``None``。
    :return: 仅含有效字符的小写字符串；输入无有效内容时返回空串。
    """
    if not text:
        return ""
    normalized = text if isinstance(text, str) else str(text)
    # NFKC 把全角字符折叠为半角，随后 casefold 统一大小写。
    normalized = unicodedata.normalize("NFKC", normalized).casefold()
    return "".join(char for char in normalized if _is_meaningful(char))


def build_ngrams(text: str, size: int) -> List[str]:
    """把字符序列切分成固定长度的连续片段（n-gram）。

    :param text: 已规范化的文本。
    :param size: 片段长度，必须为正整数。
    :return: n-gram 列表；当 ``len(text) < size`` 时返回空列表。
    :raises ValueError: ``size`` 不是正整数时抛出。
    """
    if size <= 0:
        raise ValueError("n-gram 长度必须为正整数，当前为 {0}".format(size))
    if len(text) < size:
        return []
    return [text[index:index + size] for index in range(len(text) - size + 1)]
