@echo off
setlocal

REM Always run from the repo root (this file's folder)
cd /d %~dp0

REM Prefer the Python launcher if available
where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set PY=python
  ) else (
    echo.
    echo ERROR: Python not found.
    echo Install Python 3.10+ and ensure it is on PATH.
    echo.
    pause
    exit /b 1
  )
)

echo.
echo Using: %PY%
echo.

REM Create virtual environment if needed
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment in .venv ...
  %PY% -m venv .venv
  if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to create virtual environment.
    echo.
    pause
    exit /b 1
  )
)

echo Installing dependencies from requirements.txt ...
.\.venv\Scripts\python.exe -m pip install --upgrade pip
if %errorlevel% neq 0 (
  echo.
  echo ERROR: Failed to upgrade pip.
  echo.
  pause
  exit /b 1
)

.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if %errorlevel% neq 0 (
  echo.
  echo ERROR: Failed to install requirements.
  echo.
  pause
  exit /b 1
)

echo.
echo Install complete.
echo Start the Web UI with: run_webui.bat
echo.
pause

endlocal
