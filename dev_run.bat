@echo off
setlocal
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"

set "PYC=C:\Tools\.venv\Scripts\python.exe"
set "PYW=C:\Tools\.venv\Scripts\pythonw.exe"
if not exist "%PYC%" (
  echo ERROR: shared venv python not found at:
  echo   C:\Tools\.venv\Scripts\python.exe
  pause
  exit /b 1
)
if not exist "%PYW%" (
  set "PYW=%PYC%"
)

set "LOG=%APPDIR%\truck_nest_explorer_launch.log"
set "DEV_HOT_RELOAD=1"
set "HOT_RELOAD_INTERVAL=0.6"
set "HOT_RELOAD_DEBOUNCE=5.0"
set "HOT_RELOAD_MIN_UPTIME=1.2"
set "HOT_RELOAD_DECISION_TIMEOUT=10.0"

if not "%TNE_HOT_RELOAD%"=="" set DEV_HOT_RELOAD=%TNE_HOT_RELOAD%
if not "%TNE_HOT_INTERVAL%"=="" set HOT_RELOAD_INTERVAL=%TNE_HOT_INTERVAL%
if not "%TNE_HOT_DEBOUNCE%"=="" set HOT_RELOAD_DEBOUNCE=%TNE_HOT_DEBOUNCE%
if not "%TNE_HOT_MIN_UPTIME%"=="" set HOT_RELOAD_MIN_UPTIME=%TNE_HOT_MIN_UPTIME%
if not "%TNE_HOT_DECISION_TIMEOUT%"=="" set HOT_RELOAD_DECISION_TIMEOUT=%TNE_HOT_DECISION_TIMEOUT%

cd /d "%APPDIR%" || exit /b 1

echo ===== %date% %time% ===== > "%LOG%"
echo BAT: %~f0 >> "%LOG%"
echo PythonW: %PYW% >> "%LOG%"
echo Python: %PYC% >> "%LOG%"
echo Args: %* >> "%LOG%"

set "APPDIR_PS=%APPDIR:\=\\%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^pythonw?\.exe$') -and (($_.CommandLine -like '*%APPDIR_PS%\\app.py*') -or ($_.CommandLine -like '*%APPDIR_PS%\\dev_hot_restart.py*')) }; " ^
  "foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }"

if "%DEV_HOT_RELOAD%"=="1" (
  echo MODE=hot_reload >> "%LOG%"
  echo HOT_RELOAD_INTERVAL=%HOT_RELOAD_INTERVAL% >> "%LOG%"
  echo HOT_RELOAD_DEBOUNCE=%HOT_RELOAD_DEBOUNCE% >> "%LOG%"
  echo HOT_RELOAD_MIN_UPTIME=%HOT_RELOAD_MIN_UPTIME% >> "%LOG%"
  echo HOT_RELOAD_DECISION_TIMEOUT=%HOT_RELOAD_DECISION_TIMEOUT% >> "%LOG%"
  "%PYC%" "%APPDIR%\dev_hot_restart.py" --interval %HOT_RELOAD_INTERVAL% --debounce %HOT_RELOAD_DEBOUNCE% --min-uptime %HOT_RELOAD_MIN_UPTIME% --decision-timeout %HOT_RELOAD_DECISION_TIMEOUT% %*
  set EXITCODE=%ERRORLEVEL%
  if not "%EXITCODE%"=="0" (
    echo.
    echo Hot reload launcher exited with code %EXITCODE%.
    echo See %LOG% for startup details.
  )
  endlocal & exit /b %EXITCODE%
)

echo MODE=stable >> "%LOG%"
"%PYW%" "%APPDIR%\app.py" %* >> "%LOG%" 2>&1

set HAD_ERROR=0
if errorlevel 1 set HAD_ERROR=1
findstr /I /C:"Traceback (most recent call last):" "%LOG%" >nul && set HAD_ERROR=1
findstr /I /C:"Fatal Python error" "%LOG%" >nul && set HAD_ERROR=1

if "%HAD_ERROR%"=="1" (
  echo.
  echo Launch failed. Log:
  type "%LOG%"
  echo.
)

endlocal
