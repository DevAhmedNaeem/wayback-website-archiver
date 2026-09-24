@echo off
title Wayback Website Archiver
color 0B
echo ========================================================
echo               WAYBACK WEBSITE ARCHIVER
echo ========================================================
echo.

cd /d "%~dp0"

IF EXIST "..\.venv\Scripts\python.exe" (
    set "PY_CMD=..\.venv\Scripts\python.exe"
) ELSE IF EXIST ".venv\Scripts\python.exe" (
    set "PY_CMD=.venv\Scripts\python.exe"
) ELSE (
    set "PY_CMD=python"
)

echo [1/2] Using Python: %PY_CMD%
"%PY_CMD%" --version
IF %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python was not found!
    pause
    exit /b 1
)

echo [2/2] Starting server...
echo Server running at: http://127.0.0.1:8000
echo.
echo Opening browser in 2 seconds...
echo (Keep this black command window open while using the archiver)
echo.

start "" "http://127.0.0.1:8000"
"%PY_CMD%" app.py

pause
