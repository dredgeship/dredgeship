# -*- coding: utf-8 -*-
"""相似度计算模块的单元测试。

测试目标：``multiset_cosine`` / ``dice_coefficient`` / ``jaccard_similarity``
/ ``compute_similarity`` / ``explain_similarity``。

构造思路：

* 用"完全相同 / 部分重叠 / 完全无关 / 空集合"四类输入验证底层度量函数；
* 用题目给出的样例文本验证整体得分落在合理区间；
* 用"更相似的一对得分更高"验证结果的单调性；
* 用超长文本做一次性能回归，保证实现不会退化成 O(n*m)。
"""

import time
import unittest
from collections import Counter

from dupcheck.similarity import (
    DEFAULT_WEIGHTS,
    compute_similarity,
    dice_coefficient,
    explain_similarity,
    jaccard_similarity,
    multiset_cosine,
)

# 题目给出的样例
SAMPLE_ORIGINAL = "今天是星期天，天气晴，今天晚上我要去看电影。"
SAMPLE_SUSPECT = "今天是周天，天气晴朗，我晚上要去看电影。"


class SetMetricTest(unittest.TestCase):
    """集合类相似度指标。"""

    def test_dice_identical_sets(self):
        self.assertAlmostEqual(dice_coefficient({"a", "b"}, {"a", "b"}), 1.0)

    def test_dice_disjoint_sets(self):
        self.assertEqual(dice_coefficient({"a"}, {"b"}), 0.0)

    def test_dice_empty_set(self):
        self.assertEqual(dice_coefficient(set(), {"a"}), 0.0)

    def test_jaccard_partial_overlap(self):
        self.assertAlmostEqual(jaccard_similarity({"a", "b"}, {"b", "c"}), 1.0 / 3.0)

    def test_jaccard_empty_sets(self):
        self.assertEqual(jaccard_similarity(set(), set()), 0.0)


class CosineTest(unittest.TestCase):
    """多重集余弦相似度。"""

    def test_identical_counters(self):
        self.assertAlmostEqual(multiset_cosine(Counter("ab"), Counter("ab")), 1.0)

    def test_disjoint_counters(self):
        self.assertEqual(multiset_cosine(Counter("ab"), Counter("xy")), 0.0)

    def test_empty_counter(self):
        self.assertEqual(multiset_cosine(Counter(), Counter("ab")), 0.0)

    def test_cosine_is_symmetric(self):
        left = multiset_cosine(Counter("banana"), Counter("bandana"))
        right = multiset_cosine(Counter("bandana"), Counter("banana"))
        self.assertAlmostEqual(left, right)


class ComputeSimilarityTest(unittest.TestCase):
    """整体重复率计算。"""

    def test_identical_text_returns_one(self):
        self.assertEqual(compute_similarity(SAMPLE_ORIGINAL, SAMPLE_ORIGINAL), 1.0)

    def test_identical_ignoring_punctuation(self):
        self.assertEqual(compute_similarity("你好，世界！", "你好世界"), 1.0)

    def test_completely_different_text_is_zero(self):
        self.assertEqual(compute_similarity("今天天气很好", "计算机网络原理"), 0.0)

    def test_sample_pair_in_expected_range(self):
        score = compute_similarity(SAMPLE_ORIGINAL, SAMPLE_SUSPECT)
        self.assertGreater(score, 0.30)
        self.assertLess(score, 0.80)

    def test_more_similar_pair_scores_higher(self):
        base = "软件工程的个人项目要求实现一个论文查重程序，需要输入三个文件路径。"
        close = "软件工程的个人项目要求实现一个论文查重程序，需要输入三个文件路径！"
        far = "今天的晚饭是番茄炒蛋和米饭，饭后我要去操场跑步锻炼身体。"
        self.assertGreater(compute_similarity(base, close), compute_similarity(base, far))

    def test_empty_input_returns_zero(self):
        self.assertEqual(compute_similarity("", "abc"), 0.0)
        self.assertEqual(compute_similarity("abc", ""), 0.0)

    def test_single_character_inputs(self):
        self.assertEqual(compute_similarity("a", "a"), 1.0)
        self.assertEqual(compute_similarity("a", "b"), 0.0)

    def test_custom_weights_are_used(self):
        score = compute_similarity("abc", "abd", weights={"unigram_cosine": 1.0})
        self.assertAlmostEqual(score, 2.0 / 3.0, places=4)

    def test_all_zero_weights_returns_zero(self):
        self.assertEqual(compute_similarity("abc", "abd", weights={}), 0.0)

    def test_default_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(DEFAULT_WEIGHTS.values()), 1.0)

    def test_explain_similarity_reports_features(self):
        report = explain_similarity(SAMPLE_ORIGINAL, SAMPLE_SUSPECT)
        self.assertFalse(report["identical"])
        self.assertGreater(report["length_a"], 0)
        self.assertGreater(report["length_b"], 0)
        for name in DEFAULT_WEIGHTS:
            self.assertIn(name, report["features"])
        self.assertLessEqual(report["score"], 1.0)

    def test_explain_similarity_marks_identical(self):
        report = explain_similarity("相同的文本", "相同的文本")
        self.assertTrue(report["identical"])
        self.assertEqual(report["score"], 1.0)
        self.assertEqual(report["features"], {})


class PerformanceRegressionTest(unittest.TestCase):
    """性能回归：长文本必须线性完成，不能退化成 O(n*m)。"""

    def test_long_text_finishes_well_under_limit(self):
        text = "软件工程论文查重算法性能优化" * 5000  # 约 7 万字符
        start = time.perf_counter()
        score = compute_similarity(text, text[: len(text) // 2])
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 5.0)
        self.assertGreater(score, 0.0)


if __name__ == "__main__":
    unittest.main()
