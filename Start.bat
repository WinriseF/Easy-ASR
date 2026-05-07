@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Starting EASY-ASR...
echo.

Easy-ASR.exe

set EXIT_CODE=%ERRORLEVEL%

echo.
echo EASY-ASR 已退出，退出码：%EXIT_CODE%
echo.

if not "%EXIT_CODE%"=="0" (
    echo 如果是异常退出，请查看 package_logs 目录中的 crash 日志。
    echo 如果没有 crash 日志，可能是底层 DLL / 驱动 / native 依赖直接崩溃。
    echo.
)

pause