"""命令行端到端测试：生成模式、判题模式与参数校验。"""

import os
import tempfile
import unittest

from arith.cli import main


class TestCli(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.directory = self._temp.name
        self._cwd = os.getcwd()
        os.chdir(self.directory)

    def tearDown(self):
        os.chdir(self._cwd)
        self._temp.cleanup()

    def test_generate_default_count(self):
        self.assertEqual(main(["-r", "10"]), 0)
        with open("Exercises.txt", encoding="utf-8") as handle:
            exercises = [line for line in handle.read().splitlines() if line]
        with open("Answers.txt", encoding="utf-8") as handle:
            answers = [line for line in handle.read().splitlines() if line]
        self.assertEqual(len(exercises), 10)
        self.assertEqual(len(answers), 10)
        self.assertTrue(all(line.endswith("=") for line in exercises))

    def test_generate_then_grade_round_trip(self):
        self.assertEqual(main(["-n", "20", "-r", "10"]), 0)
        self.assertEqual(main(["-e", "Exercises.txt", "-a", "Answers.txt"]), 0)
        with open("Grade.txt", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Correct: 20 (", text)
        self.assertIn("Wrong: 0 ()", text)

    def test_missing_range_reports_error(self):
        with self.assertRaises(SystemExit) as context:
            main(["-n", "10"])
        self.assertEqual(context.exception.code, 2)

    def test_invalid_range_reports_error(self):
        with self.assertRaises(SystemExit) as context:
            main(["-n", "10", "-r", "0"])
        self.assertEqual(context.exception.code, 2)

    def test_grade_requires_both_files(self):
        with self.assertRaises(SystemExit) as context:
            main(["-e", "Exercises.txt"])
        self.assertEqual(context.exception.code, 2)

    def test_ascii_style(self):
        self.assertEqual(main(["-n", "5", "-r", "10", "--ascii"]), 0)
        with open("Exercises.txt", encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("×", text)
        self.assertIn("*", text)


def run_tests() -> None:
    """``python -m tests.test_cli`` 单独运行本文件时的入口。"""
    program = unittest.main(exit=False)
    if not program.result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    run_tests()
