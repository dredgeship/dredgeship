@echo off
REM ============================================================
REM  把 Myapp.py 打包成单文件可执行程序 Myapp.exe
REM  首次使用请先安装：pip install pyinstaller
REM ============================================================

python -m PyInstaller -F -n Myapp Myapp.py --clean --noconfirm 

if errorlevel 1 (
    echo 打包失败，请确认已安装 pyinstaller：pip install pyinstaller
) else (
    echo 打包完成：Myapp.exe
    echo 用法示例：Myapp.exe -n 10 -r 10
)

pause
