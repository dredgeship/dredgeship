# -*- coding: utf-8 -*-
"""论文查重核心包。

对外暴露的主要能力：

* :func:`dupcheck.similarity.compute_similarity` —— 计算两段文本的重复率；
* :func:`dupcheck.text_io.read_text` / :func:`dupcheck.text_io.write_result`
  —— 负责输入文件的读取与答案文件的写出；
* :class:`dupcheck.exceptions.DuplicateCheckError` —— 全部自定义异常的基类。

命令行入口见项目根目录的 ``main.py``。
"""

from .exceptions import (
    DuplicateCheckError,
    EmptyDocumentError,
    EncodingDetectionError,
    FileAccessError,
    InvalidArgumentError,
)
from .similarity import compute_similarity, explain_similarity
from .text_io import read_text, write_result

__all__ = [
    "compute_similarity",
    "explain_similarity",
    "read_text",
    "write_result",
    "DuplicateCheckError",
    "EmptyDocumentError",
    "EncodingDetectionError",
    "FileAccessError",
    "InvalidArgumentError",
]

__version__ = "1.0.0"
