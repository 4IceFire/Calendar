#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f "transcription_sender_ui.pid" ]]; then
  echo "Sender PID file not found. It may already be stopped."
  exit 0
fi
PID="$(cat transcription_sender_ui.pid)"
kill "$PID" 2>/dev/null || true
rm -f transcription_sender_ui.pid
echo "Sender stopped."
