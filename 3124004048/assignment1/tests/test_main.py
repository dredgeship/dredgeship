# -*- coding: utf-8 -*-
"""以子进程方式运行 ``main.py`` 的端到端测试。

这是最贴近评测机真实用法的测试：直接执行
``python main.py <原文文件> <抄袭版论文文件> <答案文件>``，
检查进程退出码与答案文件内容。
"""

import os
import runpy
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_SCRIPT = os.path.join(PROJECT_ROOT, "main.py")
DATA_DIR = os.path.join(PROJECT_ROOT, "tests", "data")


class MainScriptTest(unittest.TestCase):
    """通过子进程验证命令行参数与文件输入输出。"""

    def _run(self, arguments):
        """运行 ``python main.py <arguments>``，返回 CompletedProcess。"""
        return subprocess.run(
            [sys.executable, MAIN_SCRIPT] + list(arguments),
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_sample_pair_outputs_expected_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            answer = os.path.join(temp_dir, "ans.txt")
            result = self._run([
                os.path.join(DATA_DIR, "original_example.txt"),
                os.path.join(DATA_DIR, "suspect_example.txt"),
                answer,
            ])
            self.assertEqual(result.returncode, 0)
            with open(answer, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "0.49")

    def test_output_uses_absolute_path_from_other_working_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            answer = os.path.join(temp_dir, "ans.txt")
            result = subprocess.run(
                [sys.executable, MAIN_SCRIPT,
                 os.path.join(DATA_DIR, "original_long.txt"),
                 os.path.join(DATA_DIR, "suspect_add.txt"),
                 answer],
                cwd=temp_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0)
            self.assertTrue(os.path.isfile(answer))

    def test_missing_argument_exits_with_usage_code(self):
        self.assertEqual(self._run([]).returncode, 2)

    def test_missing_file_exits_with_runtime_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            answer = os.path.join(temp_dir, "ans.txt")
            result = self._run([
                os.path.join(DATA_DIR, "no_such_file.txt"),
                os.path.join(DATA_DIR, "suspect_example.txt"),
                answer,
            ])
            self.assertEqual(result.returncode, 1)
            self.assertFalse(os.path.exists(answer))


class MainModuleTest(unittest.TestCase):
    """在当前进程内执行 ``main.py``，便于覆盖率工具统计入口文件。"""

    def test_entry_point_returns_zero_and_writes_answer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            answer = os.path.join(temp_dir, "ans.txt")
            original_argv = sys.argv
            sys.argv = [
                MAIN_SCRIPT,
                os.path.join(DATA_DIR, "original_example.txt"),
                os.path.join(DATA_DIR, "suspect_example.txt"),
                answer,
            ]
            try:
                with self.assertRaises(SystemExit) as context:
                    runpy.run_path(MAIN_SCRIPT, run_name="__main__")
            finally:
                sys.argv = original_argv
            self.assertEqual(context.exception.code, 0)
            with open(answer, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "0.49")


if __name__ == "__main__":
    unittest.main()
