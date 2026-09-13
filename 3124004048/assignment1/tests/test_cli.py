# -*- coding: utf-8 -*-
"""命令行流程的端到端单元测试。

测试目标：``dupcheck.cli.parse_args`` / ``main``。

构造思路：用 ``tempfile`` 生成临时答案文件，直接以"参数列表"的方式调用
``main()``（等价于命令行传参），断言退出码与答案文件内容；同时覆盖参数
个数错误、输入文件缺失、空文档降级处理三种异常场景。
"""

import os
import re
import tempfile
import unittest

from dupcheck.cli import (
    EXIT_RUNTIME_ERROR,
    EXIT_SUCCESS,
    EXIT_USAGE_ERROR,
    main,
    parse_args,
)
from dupcheck.exceptions import InvalidArgumentError

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

ORIGINAL = os.path.join(DATA_DIR, "original_example.txt")
SUSPECT = os.path.join(DATA_DIR, "suspect_example.txt")


def _read_answer(path):
    """读取答案文件内容。"""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ParseArgsTest(unittest.TestCase):
    """``parse_args`` 的返回值与异常。"""

    def test_returns_three_paths(self):
        self.assertEqual(parse_args(["a", "b", "c"]), ("a", "b", "c"))

    def test_wrong_argument_count_raises(self):
        for arguments in ([], ["a"], ["a", "b"], ["a", "b", "c", "d"]):
            with self.assertRaises(InvalidArgumentError):
                parse_args(arguments)


class MainEndToEndTest(unittest.TestCase):
    """``main`` 的端到端行为。"""

    def test_full_run_writes_formatted_answer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            answer = os.path.join(temp_dir, "ans.txt")
            code = main([ORIGINAL, SUSPECT, answer])
            self.assertEqual(code, EXIT_SUCCESS)
            content = _read_answer(answer)
            self.assertRegex(content, re.compile(r"^\d+\.\d{2}$"))
            self.assertGreaterEqual(float(content), 0.0)
            self.assertLessEqual(float(content), 1.0)

    def test_identical_files_give_one(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            answer = os.path.join(temp_dir, "ans.txt")
            main([ORIGINAL, ORIGINAL, answer])
            self.assertEqual(_read_answer(answer), "1.00")

    def test_unrelated_files_score_lower_than_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copy_answer = os.path.join(temp_dir, "copy.txt")
            unrelated_answer = os.path.join(temp_dir, "unrelated.txt")
            main([os.path.join(DATA_DIR, "original_long.txt"),
                  os.path.join(DATA_DIR, "suspect_add.txt"), copy_answer])
            main([os.path.join(DATA_DIR, "original_long.txt"),
                  os.path.join(DATA_DIR, "suspect_unrelated.txt"), unrelated_answer])
            self.assertGreater(
                float(_read_answer(copy_answer)),
                float(_read_answer(unrelated_answer)),
            )

    def test_missing_input_file_returns_runtime_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            answer = os.path.join(temp_dir, "ans.txt")
            code = main([os.path.join(DATA_DIR, "no_such.txt"), SUSPECT, answer])
            self.assertEqual(code, EXIT_RUNTIME_ERROR)
            self.assertFalse(os.path.exists(answer))

    def test_wrong_argument_count_returns_usage_error(self):
        self.assertEqual(main([]), EXIT_USAGE_ERROR)

    def test_empty_document_is_downgraded_to_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            empty = os.path.join(temp_dir, "empty.txt")
            open(empty, "w", encoding="utf-8").close()
            answer = os.path.join(temp_dir, "ans.txt")
            code = main([empty, SUSPECT, answer])
            self.assertEqual(code, EXIT_SUCCESS)
            self.assertEqual(_read_answer(answer), "0.00")

    def test_answer_file_in_new_directory_is_created(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            answer = os.path.join(temp_dir, "output", "ans.txt")
            code = main([ORIGINAL, SUSPECT, answer])
            self.assertEqual(code, EXIT_SUCCESS)
            self.assertTrue(os.path.isfile(answer))


if __name__ == "__main__":
    unittest.main()
