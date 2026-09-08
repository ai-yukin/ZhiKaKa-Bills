@echo off
title ZhiKaKa Bills - Credit Card Manager

echo ================================================
echo   ZhiKaKa Bills - Credit Card Manager
echo ================================================
echo.

cd /d "%~dp0"

REM Use project virtual environment first, then system Python
set PYTHON_CMD=
if exist ".venv\Scripts\python.exe" set PYTHON_CMD=.venv\Scripts\python.exe

if "%PYTHON_CMD%"=="" (
    python --version >nul 2>&1
    if not errorlevel 1 set PYTHON_CMD=python
)

if "%PYTHON_CMD%"=="" (
    echo [ERROR] Python not found!
    echo.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    echo Or create a virtual environment with uv:
    echo   uv venv .venv --python 3.12
    echo   uv pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo [INFO] Using Python: %PYTHON_CMD%
"%PYTHON_CMD%" --version

REM Check dependencies
"%PYTHON_CMD%" -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo [INFO] First run, installing dependencies...
    "%PYTHON_CMD%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies. Check your network.
        pause
        exit /b 1
    )
)

echo.
echo Starting ZhiKaKa Bills...
echo URL: http://localhost:5000
echo Browser will open automatically. If not, visit the URL above.
echo Press Ctrl+C to stop.
echo.

"%PYTHON_CMD%" server.py

pause
