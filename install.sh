#!/usr/bin/env bash
set -euo pipefail

# Always run from the repo root (this file's folder)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "ERROR: python3 not found."
  echo "Install Python 3.10+ and ensure python3 is on PATH."
  echo
  exit 1
fi

echo
echo "Using: $(command -v python3)"
echo

# Create virtual environment if needed
if [[ ! -x ".venv/bin/python" ]]; then
  echo "Creating virtual environment in .venv ..."
  python3 -m venv .venv
fi

echo "Installing dependencies from requirements.txt ..."
".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -r requirements.txt

echo
echo "Install complete."
echo "Start the Web UI with: ./run_webui.sh"
echo
