@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

rem ================================================================
rem  Rewards - desktop launcher
rem  Everything lives under this folder: app / browser / data / panel
rem ================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "REWARDS_APP_ROOT=%ROOT%"
set "REWARDS_DATA_DIR=%ROOT%\data"
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\browser"

if not exist "%ROOT%\app\node.exe" (
    echo [ERROR] missing app\node.exe
    pause
    exit /b 1
)
if not exist "%ROOT%\desktop.py" (
    echo [ERROR] missing desktop.py
    pause
    exit /b 1
)

rem ---- pick a pythonw that has pywebview ----
set "PYW="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PYW (
    for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%P"
)
if not defined PYW (
    echo [ERROR] pythonw not found, install Python 3 or run: pip install pywebview
    pause
    exit /b 1
)

rem ---- start desktop shell; it boots the backend by itself ----
start "" "%PYW%" "%ROOT%\desktop.py"
exit /b 0
