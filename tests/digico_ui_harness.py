"""Local browser-test harness for the DiGiCo pages (not used in production)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_digico import _DeskSimulator  # noqa: E402
import webui  # noqa: E402


DESK_PORT = 19090
LISTEN_PORT = 18080
HTTP_PORT = 5057

CONFIG = {
    **webui.utils.get_config(),
    "auth_enabled": False,
    "digico_enabled": True,
    "digico_ip": "127.0.0.1",
    "digico_port": DESK_PORT,
    "digico_listen_address": "127.0.0.1",
    "digico_listen_port": LISTEN_PORT,
    "digico_request_interval": 0.025,
    "digico_retry_interval": 0.1,
    "digico_stale_after": 2.0,
    "digico_auxes": [
        {"enabled": True, "label": "Vocals", "colour": "#b449d8", "icon": "🎤", "order": 1},
        {"enabled": True, "label": "Band", "colour": "#2878e4", "icon": "🎧", "order": 2},
    ],
    "digico_channels": [
        {"enabled": True, "label": "Lead Vocal", "group": "Vocals", "icon": "🎤", "order": 1},
        {"enabled": True, "label": "Keys", "group": "Band", "icon": "🎹", "order": 2},
    ],
    "digico_external_devices": [],
}


def _get_config():
    return CONFIG


def _save_config(value):
    replacement = dict(value)
    CONFIG.clear()
    CONFIG.update(replacement)


if __name__ == "__main__":
    webui.utils.get_config = _get_config
    webui.utils.save_config = _save_config
    webui.utils.reload_config = lambda force=False: CONFIG
    webui._auth_enabled = lambda: False
    desk = _DeskSimulator(DESK_PORT)
    desk.start()
    try:
        webui.app.run(host="127.0.0.1", port=HTTP_PORT, threaded=True, use_reloader=False)
    finally:
        desk.close()
