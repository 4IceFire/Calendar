@echo off
setlocal

REM Always run from the repo root (this file's folder)
cd /d %~dp0

if not exist ".venv\Scripts\activate.bat" (
  echo.
  echo Virtual environment not found: .venv
  echo Create it and install dependencies first:
  echo   python -m venv .venv
  echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat"

REM Start the Web UI
python webui.py

endlocal
