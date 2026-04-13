@echo off
cd /d "%~dp0"
if not exist "transcription_sender_ui.pid" (
  echo Sender PID file not found. It may already be stopped.
  pause
  exit /b 0
)
set /p SENDER_PID=<transcription_sender_ui.pid
taskkill /PID %SENDER_PID% /T /F
if errorlevel 1 (
  echo Could not stop sender process %SENDER_PID%.
  pause
  exit /b 1
)
del /q transcription_sender_ui.pid >nul 2>nul
echo Sender stopped.
