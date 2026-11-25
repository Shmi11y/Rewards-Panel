@echo off
chcp 65001 >nul
setlocal

rem ================================================================
rem  Rewards - task runner for Windows startup and Task Scheduler
rem  usage: Run-Task.bat /silent
rem ================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\browser"
if not defined REWARDS_DATA_DIR set "REWARDS_DATA_DIR=%ROOT%\data"
set "REWARDS_APP_ROOT=%ROOT%"
set "RC=1"

if not exist "%ROOT%\app\dist\index.js" (
    echo [ERROR] missing app\dist\index.js
    goto :hold
)
if not exist "%ROOT%\task-data.cjs" (
    echo [ERROR] missing task-data.cjs
    goto :hold
)
if not exist "%REWARDS_DATA_DIR%\.env" (
    echo [ERROR] missing data\.env
    goto :hold
)

rem findstr needs CRLF line endings
findstr /r /c:"^ACCOUNT_1_EMAIL=." "%REWARDS_DATA_DIR%\.env" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] ACCOUNT_1_EMAIL is empty in data\.env
    goto :hold
)

dir /b /ad "%ROOT%\browser\chromium-*" >nul 2>&1
if errorlevel 1 (
    echo [WARN] bundled chromium not found under browser\
)

echo ============================================================
echo  start : %date% %time%
echo  root  : %ROOT%
echo ============================================================
echo.

cd /d "%ROOT%\app"
"%ROOT%\app\node.exe" --require "%ROOT%\task-data.cjs" "%ROOT%\app\dist\index.js"
set "RC=%ERRORLEVEL%"

echo.
echo ============================================================
echo  end   : %date% %time%    exit=%RC%
echo  log   : %ROOT%\app\logs\  by date
echo ============================================================

:hold
if /i not "%~1"=="/silent" pause
exit /b %RC%
