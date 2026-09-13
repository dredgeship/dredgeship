# -*- coding: utf-8 -*-
"""自定义异常体系的单元测试。

测试目标：``dupcheck.exceptions`` 中 4 种自定义异常的继承关系与提示信息。

构造思路：异常体系的"契约"有两条，测试就围绕这两条展开：

1. **调用方只要捕获基类就能兜住全部已知错误** —— 逐条验证继承关系；
2. **提示信息在"不传 / 传空串 / 传具体信息"三种情况下都可用** ——
   验证 ``default_message`` 的兜底分支，避免出现空白错误提示。
"""

import unittest

from dupcheck.exceptions import (
    DuplicateCheckError,
    EmptyDocumentError,
    EncodingDetectionError,
    FileAccessError,
    InvalidArgumentError,
)

#: 需要逐个验证的异常类型
ALL_ERROR_TYPES = (
    InvalidArgumentError,
    FileAccessError,
    EncodingDetectionError,
    EmptyDocumentError,
)


class ExceptionHierarchyTest(unittest.TestCase):
    """异常之间的继承关系。"""

    def test_all_errors_share_base_class(self):
        for error_type in ALL_ERROR_TYPES:
            with self.subTest(error_type=error_type.__name__):
                self.assertTrue(issubclass(error_type, DuplicateCheckError))

    def test_encoding_error_is_a_file_access_error(self):
        # 编码问题本质上是"文件读不出来"，因此特意设计成 FileAccessError 的子类，
        # 这样只关心"读不到文件"的调用方也能捕获它
        self.assertTrue(issubclass(EncodingDetectionError, FileAccessError))

    def test_catching_base_class_catches_everything(self):
        for error_type in ALL_ERROR_TYPES:
            with self.subTest(error_type=error_type.__name__):
                with self.assertRaises(DuplicateCheckError):
                    raise error_type("测试用的错误")


class ExceptionMessageTest(unittest.TestCase):
    """默认提示与显式提示。"""

    def test_default_message_is_used_when_omitted(self):
        for error_type in (DuplicateCheckError,) + ALL_ERROR_TYPES:
            with self.subTest(error_type=error_type.__name__):
                self.assertEqual(str(error_type()), error_type.default_message)

    def test_explicit_message_overrides_default(self):
        error = FileAccessError("文件不存在: a.txt")
        self.assertEqual(str(error), "文件不存在: a.txt")

    def test_blank_message_falls_back_to_default(self):
        # 传入空串等价于不传，避免向用户输出一条空白的错误提示
        self.assertEqual(str(FileAccessError("")), FileAccessError.default_message)


if __name__ == "__main__":
    unittest.main()
