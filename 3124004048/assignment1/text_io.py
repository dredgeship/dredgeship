# -*- coding: utf-8 -*-
"""文件读写模块。

负责把"文件路径"翻译成"文本"，以及把重复率写入答案文件。
所有文件相关错误都会被转换成 :mod:`dupcheck.exceptions` 中的自定义异常，
方便上层统一处理，避免程序出现意外的异常退出。
"""

import os
from typing import Sequence

from .exceptions import (
    EmptyDocumentError,
    EncodingDetectionError,
    FileAccessError,
)
from .utils import clamp

#: 候选编码，按命中概率从高到低排列；gb18030 是 gbk / gb2312 的超集
CANDIDATE_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "big5", "utf-16")


def read_text(path: str, encodings: Sequence[str] = CANDIDATE_ENCODINGS) -> str:
    """读取文本文件并自动尝试多种编码。

    :param path: 文件路径。
    :param encodings: 候选编码序列，按顺序尝试。
    :return: 文件解码后的文本（尚未做规范化处理）。
    :raises FileAccessError: 路径为空、文件不存在或不是普通文件、读取失败。
    :raises EmptyDocumentError: 文件内容为空或只含空白字符。
    :raises EncodingDetectionError: 所有候选编码均解码失败。
    """
    if path is None or not str(path).strip():
        raise FileAccessError("文件路径不能为空")
    path = str(path)
    if not os.path.exists(path):
        raise FileAccessError("文件不存在: {0}".format(path))
    if not os.path.isfile(path):
        raise FileAccessError("路径不是普通文件: {0}".format(path))
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        raise FileAccessError("读取文件失败 {0}: {1}".format(path, error))

    if not raw.strip():
        raise EmptyDocumentError("文档内容为空: {0}".format(path))
    return _decode(raw, encodings, path)


def _decode(raw: bytes, encodings: Sequence[str], path: str) -> str:
    """按候选编码依次尝试解码，全部失败时抛出异常。"""
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise EncodingDetectionError("无法识别文件编码: {0}".format(path))


def write_result(path: str, similarity: float) -> str:
    """把重复率写入答案文件，保留两位小数。

    :param path: 答案文件路径（父目录不存在时会自动创建）。
    :param similarity: 重复率，超出 ``[0, 1]`` 的部分会被截断。
    :return: 实际写入的字符串内容。
    :raises FileAccessError: 路径为空、路径是目录或写入失败。
    """
    if path is None or not str(path).strip():
        raise FileAccessError("答案文件路径不能为空")
    path = str(path)
    if os.path.isdir(path):
        raise FileAccessError("答案路径指向的是一个目录: {0}".format(path))

    content = "{0:.2f}".format(clamp(similarity))
    directory = os.path.dirname(os.path.abspath(path))
    try:
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as error:
        raise FileAccessError("写入答案文件失败 {0}: {1}".format(path, error))
    return content
