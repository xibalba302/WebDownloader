@echo off
setlocal enabledelayedexpansion
title WebDownloader

cd /d "%~dp0"

echo ============================================
echo   WebDownloader - offline site downloader
echo ============================================
echo.

rem --- Locate a Python interpreter -----------------------------------------
set "PY_LAUNCHER="
where py >nul 2>nul
if not errorlevel 1 (
    set "PY_LAUNCHER=py -3"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PY_LAUNCHER=python"
    )
)

if not defined PY_LAUNCHER (
    echo [ERROR] Python was not found on this system.
    echo.
    echo Install Python 3.9 or newer from https://www.python.org/downloads/
    echo During setup, make sure "Add python.exe to PATH" is checked.
    echo.
    pause
    exit /b 1
)

echo Using Python:
%PY_LAUNCHER% --version
echo.

rem --- Create a virtual environment (only once) ----------------------------
if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment in .\venv ...
    %PY_LAUNCHER% -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists, reusing it.
)
echo.

rem --- Install / update dependencies ----------------------------------------
echo Installing dependencies ^(this may take a minute the first time^)...
"venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)
echo.
echo Dependencies ready.
echo.

rem --- Launch the app and open it in the browser -----------------------------
echo Starting WebDownloader at http://127.0.0.1:5000 ...
echo Close this window (or press Ctrl+C) to stop the server.
echo.

start "" /min powershell -NoLogo -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:5000/'"
"venv\Scripts\python.exe" app.py

pause
