"""Run the FastAPI app using uvicorn and read host/port from config.json.

Usage:
  python run_server.py

This will read `config.json` in the repository root for the keys:
  - api_host (optional, default: 127.0.0.1)
  - api_port (optional, default: 8001)

If `api_port` is not present the script defaults to 8001 to avoid colliding
with an existing Companion service that may be using port 8000.
"""
from pathlib import Path
import json
import sys

def load_config(path: Path):
    try:
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def main():
    root = Path(__file__).parent
    cfg_path = root / 'config.json'
    cfg = load_config(cfg_path)
    host = cfg.get('api_host', '127.0.0.1')
    port = int(cfg.get('api_port', 8001))

    try:
        import uvicorn
    except Exception:
        print('uvicorn is required. Install with: pip install uvicorn')
        sys.exit(1)

    print(f'Starting API on {host}:{port} (from {cfg_path})')
    uvicorn.run('package.apps.calendar.api:app', host=host, port=port, reload=True)

if __name__ == '__main__':
    main()
