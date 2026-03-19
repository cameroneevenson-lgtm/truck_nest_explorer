@echo off
setlocal

set "ROOT=%~dp0"
set "PY=C:\Tools\.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo ERROR: venv Python not found at:
  echo   %PY%
  echo.
  pause
  exit /b 1
)

"%PY%" "%ROOT%app.py"
set "RC=%ERRORLEVEL%"

echo.
echo Truck Nest Explorer exited with code %RC%.
pause
exit /b %RC%
