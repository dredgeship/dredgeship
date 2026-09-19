#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Myapp：小学四则运算题目自动生成与判题程序（命令行入口）。

    python Myapp.py -n 10 -r 10                      # 生成题目
    python Myapp.py -e Exercises.txt -a Answers.txt   # 判题
"""

import sys

from arith.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
