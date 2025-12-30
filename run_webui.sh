#!/usr/bin/env bash
set -euo pipefail

# Always run from the repo root (this file's folder)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f ".venv/bin/activate" ]]; then
  echo
  echo "Virtual environment not found: .venv"
  echo "Create it and install dependencies first:"
  echo "  ./install.sh"
  echo
  exit 1
fi

# shellcheck disable=SC1091
source ".venv/bin/activate"

python webui.py
