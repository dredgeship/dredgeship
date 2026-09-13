# -*- coding: utf-8 -*-
"""文件读写模块的单元测试。

测试目标：``dupcheck.text_io.read_text`` / ``write_result``。

构造思路：真实磁盘文件 + ``tempfile`` 临时目录，覆盖"正常读取、多编码、
文件缺失、目标是目录、空文件、无法解码、写出格式、越界值截断、自动建目录"
等场景；所有异常路径都用 ``assertRaises`` 断言抛出的是自定义异常类型。
"""

import os
import tempfile
import unittest
from unittest import mock

from dupcheck.exceptions import (
    EmptyDocumentError,
    EncodingDetectionError,
    FileAccessError,
)
from dupcheck.text_io import read_text, write_result

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


class ReadTextTest(unittest.TestCase):
    """``read_text`` 的正常与异常路径。"""

    def test_reads_utf8_file(self):
        text = read_text(os.path.join(DATA_DIR, "original_example.txt"))
        self.assertIn("今天是星期天", text)

    def test_reads_gbk_encoded_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "gbk.txt")
            with open(path, "wb") as handle:
                handle.write("中文编码测试".encode("gb18030"))
            self.assertEqual(read_text(path), "中文编码测试")

    def test_missing_file_raises_file_access_error(self):
        with self.assertRaises(FileAccessError):
            read_text(os.path.join(DATA_DIR, "not_exists.txt"))

    def test_directory_path_raises_file_access_error(self):
        with self.assertRaises(FileAccessError):
            read_text(DATA_DIR)

    def test_empty_path_raises_file_access_error(self):
        with self.assertRaises(FileAccessError):
            read_text("")

    def test_empty_file_raises_empty_document_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "empty.txt")
            open(path, "w", encoding="utf-8").close()
            with self.assertRaises(EmptyDocumentError):
                read_text(path)

    def test_whitespace_only_file_raises_empty_document_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "blank.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("   \n\t  ")
            with self.assertRaises(EmptyDocumentError):
                read_text(path)

    def test_undecodable_file_raises_encoding_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "binary.txt")
            with open(path, "wb") as handle:
                handle.write(b"\xff\xfe\x00\x01\xff")
            with self.assertRaises(EncodingDetectionError):
                read_text(path, encodings=("utf-8",))


class WriteResultTest(unittest.TestCase):
    """``write_result`` 的输出格式与异常路径。"""

    def test_writes_two_decimal_places(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "ans.txt")
            self.assertEqual(write_result(path, 0.777777), "0.78")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "0.78")

    def test_rounds_down_correctly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "ans.txt")
            self.assertEqual(write_result(path, 0.123456), "0.12")

    def test_clamps_out_of_range_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "ans.txt")
            self.assertEqual(write_result(path, 1.5), "1.00")
            self.assertEqual(write_result(path, -0.2), "0.00")

    def test_creates_missing_parent_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "nested", "deep", "ans.txt")
            write_result(path, 0.5)
            self.assertTrue(os.path.isfile(path))

    def test_directory_as_answer_path_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FileAccessError):
                write_result(temp_dir, 0.5)

    def test_empty_answer_path_raises(self):
        with self.assertRaises(FileAccessError):
            write_result("", 0.5)


class IoFailureTest(unittest.TestCase):
    """借助 mock 模拟底层 IO 失败，验证 OSError 会被包装成自定义异常。"""

    def test_read_failure_is_wrapped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "readable.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("内容")
            with mock.patch("builtins.open", side_effect=OSError("模拟读取失败")):
                with self.assertRaises(FileAccessError):
                    read_text(path)

    def test_write_failure_is_wrapped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "ans.txt")
            with mock.patch("builtins.open", side_effect=OSError("模拟写入失败")):
                with self.assertRaises(FileAccessError):
                    write_result(path, 0.5)


if __name__ == "__main__":
    unittest.main()
