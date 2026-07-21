@echo off
setlocal
title Build WebDownloader.exe
cd /d "%~dp0"

echo ============================================
echo   Building WebDownloader.exe (standalone)
echo ============================================
echo.

rem --- Locate Python ---------------------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )

if not defined PY (
    echo [ERROR] Python was not found.
    echo Install Python 3.9+ from https://www.python.org/downloads/ and
    echo make sure "Add python.exe to PATH" is checked during setup.
    echo.
    pause
    exit /b 1
)

echo Using Python:
%PY% --version
echo.

echo Installing build dependencies (Flask, requests, beautifulsoup4, PyInstaller)...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)
echo.

echo Building the executable...
%PY% build_exe.py
if errorlevel 1 (
    echo [ERROR] Build failed. See the messages above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Done!  Your standalone app is here:
echo       dist\WebDownloader.exe
echo.
echo   Copy that single file to any Windows PC and
echo   double-click it - no Python needed there.
echo ============================================
echo.
pause
