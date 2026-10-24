@echo off
REM ============================================================
REM  屏幕翻译器（独立版）启动器
REM  双击即可运行，不依赖 Hermes
REM ============================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 找不到虚拟环境: %PY%
    echo.
    echo 首次使用请在此目录运行:
    echo     uv venv --python 3.12 .venv
    echo     uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM API Key：优先读本目录 .env，其次读系统环境变量
if exist "%~dp0.env" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0.env") do (
        if not "%%a"=="" set "%%a=%%b"
    )
)

start "" "%PY%" app.py %*
endlocal
