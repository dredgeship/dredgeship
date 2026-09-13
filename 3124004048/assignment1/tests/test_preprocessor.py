# -*- coding: utf-8 -*-
"""文本预处理模块的单元测试。

测试目标：``dupcheck.preprocessor.normalize`` / ``build_ngrams``。

构造思路：覆盖"标点剔除、全角折叠、大小写统一、空输入、纯噪声输入、
n-gram 切分边界"等典型与边界场景。
"""

import unittest

from dupcheck.preprocessor import build_ngrams, normalize


class NormalizeTest(unittest.TestCase):
    """``normalize`` 的行为验证。"""

    def test_removes_chinese_punctuation(self):
        self.assertEqual(normalize("你好，世界！"), "你好世界")

    def test_removes_english_punctuation_and_space(self):
        self.assertEqual(normalize("Hello, world!"), "helloworld")

    def test_converts_fullwidth_characters(self):
        # 全角字母、数字、逗号都应被折叠为半角
        self.assertEqual(normalize("ＡＢＣ１２３，"), "abc123")

    def test_lowercases_english(self):
        self.assertEqual(normalize("PyThOn"), "python")

    def test_keeps_chinese_characters(self):
        self.assertEqual(normalize("论文查重"), "论文查重")

    def test_empty_and_none_input(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None), "")

    def test_punctuation_only_returns_empty(self):
        self.assertEqual(normalize("，。！？ \n\t"), "")


class BuildNgramsTest(unittest.TestCase):
    """``build_ngrams`` 的边界验证。"""

    def test_build_trigrams(self):
        self.assertEqual(build_ngrams("abcd", 3), ["abc", "bcd"])

    def test_build_bigrams(self):
        self.assertEqual(build_ngrams("abc", 2), ["ab", "bc"])

    def test_text_shorter_than_size(self):
        self.assertEqual(build_ngrams("ab", 3), [])

    def test_invalid_size_raises_value_error(self):
        with self.assertRaises(ValueError):
            build_ngrams("abc", 0)


if __name__ == "__main__":
    unittest.main()
