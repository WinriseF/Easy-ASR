@echo off
cd /d "%~dp0"

echo Starting EASY-ASR...
echo.
echo Detailed logs will be written to package_logs\runtime_*.log
echo Fatal crash traces will be written to package_logs\fatal_*.log
echo.

Easy-ASR.exe

set EXIT_CODE=%ERRORLEVEL%

echo.
echo EASY-ASR exited. Exit code: %EXIT_CODE%
echo.

if not "%EXIT_CODE%"=="0" (
    echo If this was a crash, check the package_logs folder.
    echo If there is no crash log, it may be a native DLL or driver crash.
    echo.
)

pause
