@echo off
chcp 65001 >nul
setlocal

rem ================================================================
rem  Rewards - task runner for Windows startup and Task Scheduler
rem  usage: Run-Task.bat /silent [/boot]
rem ================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\browser"
if not defined REWARDS_DATA_DIR set "REWARDS_DATA_DIR=%ROOT%\data"
set "REWARDS_APP_ROOT=%ROOT%"
set "SOURCE=manual"
if /i "%~1"=="/silent" set "SOURCE=auto"
if /i "%~2"=="/boot" set "SOURCE=boot"
set "REWARDS_PYTHON=%ROOT%\runtime\python\python.exe"
if not exist "%REWARDS_PYTHON%" set "REWARDS_PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%REWARDS_PYTHON%" set "REWARDS_PYTHON=python.exe"

echo ============================================================
echo  start : %date% %time%
echo  root  : %ROOT%
echo ============================================================
echo.

cd /d "%ROOT%"
"%REWARDS_PYTHON%" "%ROOT%\run_history.py" %SOURCE%
set "RC=%ERRORLEVEL%"

echo.
echo ============================================================
echo  end   : %date% %time%    exit=%RC%
echo  log   : %ROOT%\app\logs\  by date
echo ============================================================

if /i not "%~1"=="/silent" pause
exit /b %RC%
