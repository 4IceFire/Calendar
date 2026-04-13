@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Create .venv and install requirements_sender.txt first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe tools\transcription_sender_ui.py
