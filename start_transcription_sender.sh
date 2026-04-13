#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x ".venv/bin/python" ]]; then
  echo "Virtual environment not found. Create .venv and install requirements_sender.txt first."
  exit 1
fi
".venv/bin/python" tools/transcription_sender_ui.py
