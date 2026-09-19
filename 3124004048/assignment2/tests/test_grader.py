"""判题模块单元测试：答案比对、等价写法与 Grade.txt 输出格式。"""

import os
import tempfile
import unittest

from arith.grader import grade


class TestGrade(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.directory = self._temp.name

    def tearDown(self):
        self._temp.cleanup()

    def _write(self, name, content):
        path = os.path.join(self.directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def _run(self, exercises, answers):
        exercise_file = self._write("Exercises.txt", exercises)
        answer_file = self._write("Answers.txt", answers)
        grade_file = os.path.join(self.directory, "Grade.txt")
        result = grade(exercise_file, answer_file, grade_file)
        with open(grade_file, encoding="utf-8") as handle:
            return result, handle.read()

    def test_mixed_correct_and_wrong(self):
        exercises = "1 + 1 =\n2 × 3 =\n1/2 + 1/2 =\n10 − 4 =\n2'1/2 + 1/2 =\n"
        answers = "2\n7\n1\n6\n3\n"
        result, text = self._run(exercises, answers)
        self.assertEqual(result["correct"], [1, 3, 4, 5])
        self.assertEqual(result["wrong"], [2])
        self.assertEqual(text, "Correct: 4 (1, 3, 4, 5)\nWrong: 1 (2)\n")

    def test_equivalent_answer_formats(self):
        """2'3/8 与 19/8 是同一个答案。"""
        result, _ = self._run("2'3/8 + 0 =\n", "19/8\n")
        self.assertEqual(result["correct"], [1])

    def test_unparsable_answer_counts_as_wrong(self):
        result, _ = self._run("1 + 1 =\n", "abc\n")
        self.assertEqual(result["wrong"], [1])

    def test_missing_answer_counts_as_wrong(self):
        result, _ = self._run("1 + 1 =\n2 + 2 =\n", "2\n")
        self.assertEqual(result["correct"], [1])
        self.assertEqual(result["wrong"], [2])

    def test_index_prefix_is_tolerated(self):
        result, _ = self._run("1. 1 + 1 =\n", "1. 2\n")
        self.assertEqual(result["correct"], [1])

    def test_all_correct(self):
        result, text = self._run("1 + 1 =\n2 + 2 =\n", "2\n4\n")
        self.assertEqual(text, "Correct: 2 (1, 2)\nWrong: 0 ()\n")


def run_tests() -> None:
    """``python -m tests.test_grader`` 单独运行本文件时的入口。"""
    program = unittest.main(exit=False)
    if not program.result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    run_tests()
