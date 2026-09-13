# -*- coding: utf-8 -*-
"""论文查重程序的自定义异常体系。

设计目标
--------
命令行程序在运行过程中可能遇到不同种类的问题。为了让上层调用者
（``main.py``）能够针对不同问题给出不同的处理策略，这里把它们拆分成
互相独立的异常类型：

==========================  ============================================
异常类型                    触发场景
==========================  ============================================
``InvalidArgumentError``    命令行参数个数不为 3
``FileAccessError``         路径为空、文件不存在、不是普通文件、读写失败
``EncodingDetectionError``  穷举候选编码后仍无法解码（``FileAccessError`` 子类）
``EmptyDocumentError``      文件存在但内容为空（或仅含空白字符）
==========================  ============================================

所有异常都继承自 :class:`DuplicateCheckError`，调用方只要捕获这个基类，
就能兜住全部已知错误，避免程序出现"未捕获异常"式的异常退出。
"""

from typing import Optional


class DuplicateCheckError(Exception):
    """查重相关异常的基类。"""

    #: 未显式传入 message 时使用的默认提示
    default_message = "论文查重过程中发生未知错误"

    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(message if message else self.default_message)


class InvalidArgumentError(DuplicateCheckError):
    """命令行参数个数或顺序不合法。"""

    default_message = "命令行参数不合法"


class FileAccessError(DuplicateCheckError):
    """路径为空、文件不存在、不是普通文件，或者读写失败。"""

    default_message = "无法访问指定文件"


class EncodingDetectionError(FileAccessError):
    """所有候选编码均解码失败。"""

    default_message = "无法识别文件编码"


class EmptyDocumentError(DuplicateCheckError):
    """文件存在但没有任何有效内容。"""

    default_message = "文档内容为空"
