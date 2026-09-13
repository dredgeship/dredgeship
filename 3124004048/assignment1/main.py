#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""论文查重程序入口。

用法::

    python main.py <原文文件> <抄袭版论文文件> <答案文件>

三个参数都是文件路径（推荐使用绝对路径），答案文件的内容为保留两位小数的
浮点数，表示"抄袭版论文相对于原文的重复率"，取值范围 ``[0.00, 1.00]``。

示例::

    python main.py C:\\tests\\orig.txt C:\\tests\\orig_add.txt C:\\tests\\ans.txt
"""

import sys

from dupcheck.cli import main

if __name__ == "__main__":
    sys.exit(main())
