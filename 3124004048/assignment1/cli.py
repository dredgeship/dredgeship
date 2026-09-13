# -*- coding: utf-8 -*-
"""命令行交互模块。

负责三件事：

1. 解析命令行参数（原文文件、抄袭版论文文件、答案文件，共三个路径）；
2. 串联"读取文件 → 文本规范化 → 计算重复率 → 写入答案"的完整流程；
3. 把 :mod:`dupcheck.exceptions` 中的异常转换成友好的提示信息与退出码，
   保证程序在任何情况下都能优雅地结束，而不是抛出未捕获异常。
"""

import sys
from typing import Optional, Sequence, Tuple

from .exceptions import (
    DuplicateCheckError,
    EmptyDocumentError,
    InvalidArgumentError,
)
from .similarity import compute_similarity
from .text_io import read_text, write_result

#: 命令行用法提示
USAGE = "python main.py <原文文件> <抄袭版论文文件> <答案文件>"

EXIT_SUCCESS = 0
"""正常运行结束的退出码。"""

EXIT_RUNTIME_ERROR = 1
"""运行期错误（文件不存在、答案文件不可写等）的退出码。"""

EXIT_USAGE_ERROR = 2
"""参数个数错误的退出码，与 argparse 的约定保持一致。"""


def parse_args(argv: Optional[Sequence[str]] = None) -> Tuple[str, str, str]:
    """解析命令行参数。

    :param argv: 不含程序名的参数列表；``None`` 时读取 ``sys.argv[1:]``。
    :return: ``(原文路径, 抄袭版路径, 答案路径)`` 三元组。
    :raises InvalidArgumentError: 参数个数不等于 3。
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3:
        raise InvalidArgumentError(
            "需要 3 个参数（原文、抄袭版论文、答案文件），实际收到 {0} 个".format(
                len(arguments)
            )
        )
    return arguments[0], arguments[1], arguments[2]


def _read_optional(path: str) -> str:
    """读取文件；若文件为空则返回空串而不是中断整个流程。

    空文档与任意文档的重复率是 0，属于合理结果，因此这里采用"降级处理"，
    把 :class:`EmptyDocumentError` 转成空字符串。
    """
    try:
        return read_text(path)
    except EmptyDocumentError as error:
        sys.stderr.write("[警告] {0}，按重复率 0.00 处理\n".format(error))
        return ""


def check_similarity(original_path: str, suspect_path: str, answer_path: str) -> float:
    """执行一次完整的查重流程。

    :param original_path: 原文文件路径。
    :param suspect_path: 抄袭版论文文件路径。
    :param answer_path: 答案文件路径。
    :return: 计算得到的重复率。
    :raises DuplicateCheckError: 文件无法访问或答案文件无法写入。
    """
    original_text = _read_optional(original_path)
    suspect_text = _read_optional(suspect_path)
    similarity = compute_similarity(original_text, suspect_text)
    write_result(answer_path, similarity)
    return similarity


def main(argv: Optional[Sequence[str]] = None) -> int:
    """程序主入口。

    :param argv: 不含程序名的参数列表；``None`` 时读取 ``sys.argv[1:]``。
    :return: 进程退出码（0 成功 / 1 运行期错误 / 2 参数错误）。
    """
    try:
        original_path, suspect_path, answer_path = parse_args(argv)
    except InvalidArgumentError as error:
        sys.stderr.write("参数错误: {0}\n用法: {1}\n".format(error, USAGE))
        return EXIT_USAGE_ERROR

    try:
        check_similarity(original_path, suspect_path, answer_path)
    except DuplicateCheckError as error:
        sys.stderr.write("运行错误: {0}\n".format(error))
        return EXIT_RUNTIME_ERROR
    return EXIT_SUCCESS
