# Myapp：小学四则运算题目自动生成与判题程序

用 Python 实现的命令行程序，能够按数值范围批量生成小学四则运算题目（自然数 / 真分数 / 带分数），
自动生成答案，并对给定的题目文件与答案文件判分。

## 运行环境

- Python 3.8 及以上（仅使用标准库，无需安装第三方依赖）
- 性能分析与绘图脚本可选依赖：`matplotlib`（`pip install matplotlib`）

## 使用方式

```bash
# 生成 10 道 10 以内（不含 10）的题目
python Myapp.py -n 10 -r 10

# 生成一万道题目
python Myapp.py -n 10000 -r 10

# 判定答案，结果写入 Grade.txt
python Myapp.py -e Exercises.txt -a Answers.txt

# 查看帮助
python Myapp.py -h
```

参数说明：

| 参数 | 说明 |
| --- | --- |
| `-n` | 生成题目的个数，默认 10 |
| `-r` | 数值（自然数、真分数及真分数分母）的上界，**不含**该值；生成题目时必填 |
| `-e` | 题目文件路径（判题模式，需与 `-a` 同时使用） |
| `-a` | 答案文件路径（判题模式，需与 `-e` 同时使用） |
| `--ascii` | 用 ASCII 运算符 `+ - * /` 输出，默认使用 `+ − × ÷` |
| `--seed` | 随机种子，便于复现同一批题目 |

输出文件（写入**执行程序时的当前目录**）：

- `Exercises.txt`：题目，每行形如 `1 + 2'1/2 × 3 =`
- `Answers.txt`：答案，每行形如 `2'3/8`
- `Grade.txt`：判题统计，形如 `Correct: 5 (1, 3, 5, 7, 9)` / `Wrong: 5 (2, 4, 6, 8, 10)`

打包为可执行文件 `Myapp.exe`：

```bat
build.bat                      :: 一键打包（内部调用 pyinstaller，需先 pip install pyinstaller）
```
或手动执行：
```bash
pip install pyinstaller
python -m PyInstaller -F -n Myapp Myapp.py --clean --noconfirm --distpath dist
```

产物为 `dist/Myapp.exe`（约 6.5 MB，单文件、无需 Python 环境），
参数与源码运行完全一致，题目同样输出到**运行 exe 时的当前目录**：

```bat
Myapp.exe -n 10 -r 10
Myapp.exe -e Exercises.txt -a Answers.txt
```
实测 `Myapp.exe -n 10000 -r 10` 约 2.1 s（含单文件解包开销），
源码运行 `python Myapp.py -n 10000 -r 10` 约 1.1 s。

## 目录结构

```
Myapp.py                程序入口
arith/
    fraction.py         数值层：自然数/真分数/带分数的格式化、解析、随机取值
    expression.py       表达式层：表达式树构造、求值、最小括号渲染、规范化判重键
    parser.py           解析层：词法分析 + 递归下降解析求值
    generator.py        生成层：带约束随机出题与去重
    grader.py           判题层：答案比对与 Grade.txt 统计
    cli.py              接口层：命令行参数解析与流程编排
tests/                  单元测试（55 个用例）
tools/
    profile_gen.py      性能分析（cProfile）与耗时柱状图
    verify.py           需求符合性批量校验
docs/                   性能分析图
```

## 测试与校验

```bash
python -m unittest discover -s tests        # 单元测试
python tools/verify.py -n 10000 -r 10       # 按需求逐条校验一万道题
python tools/profile_gen.py --png docs/perf_top_functions.png   # 性能分析
```

## 需求对照

| 需求 | 实现位置 |
| --- | --- |
| 1. `-n` 控制题目个数 | `arith/cli.py`、`arith/generator.py` |
| 2. `-r` 控制数值范围，缺失时报错并给出帮助 | `arith/cli.py`（`parser.error` 打印帮助并以 2 退出） |
| 3. 计算过程不产生负数 | `arith/generator.py::_combine` |
| 4. 除法结果为真分数 | `arith/generator.py::_combine` |
| 5. 运算符不超过 3 个 | `arith/generator.py::_build` |
| 6. 题目不重复 | `arith/expression.py::canonical_key` |
| 7. 生成 Exercises.txt / Answers.txt，支持一万道 | `arith/cli.py::run_generate` |
| 8. 判题并输出 Grade.txt | `arith/grader.py` |
