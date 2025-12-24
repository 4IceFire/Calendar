import json
import threading
from datetime import datetime
from typing import Any, Dict
import os

from companion import Companion
from package.apps.calendar.storage import DEFAULT_EVENTS_FILE
import logging
from logging.handlers import RotatingFileHandler

CONFIG_FILE = "config.json"

_defaults = {
    "EVENTS_FILE": DEFAULT_EVENTS_FILE,
    "companion_ip": "127.0.0.1",
    "companion_port": 8000,
    "poll_interval": 1.0,
    "debug": False,
}


def load_config(path: str = CONFIG_FILE) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            data = json.load(f) or {}
    except FileNotFoundError:
        save_config(_defaults, path)
        return _defaults.copy()
    except json.JSONDecodeError:
        save_config(_defaults, path)
        return _defaults.copy()

    changed = False
    for k, v in _defaults.items():
        if k not in data:
            data[k] = v
            changed = True
    if changed:
        save_config(data, path)

    return data


def save_config(cfg: Dict[str, Any], path: str = CONFIG_FILE) -> None:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


# Materialize runtime config and debug flag
_CONFIG = load_config(CONFIG_FILE)
_RUNTIME_DEBUG = bool(_CONFIG.get("debug", False))
_debug_lock = threading.Lock()
try:
    _config_mtime = os.path.getmtime(CONFIG_FILE)
except Exception:
    _config_mtime = None


def get_config() -> Dict[str, Any]:
    return dict(_CONFIG)


def get_debug() -> bool:
    with _debug_lock:
        return _RUNTIME_DEBUG


def set_debug(value: bool, persist: bool = True) -> None:
    global _RUNTIME_DEBUG, _CONFIG
    with _debug_lock:
        _RUNTIME_DEBUG = bool(value)
    if persist:
        _CONFIG["debug"] = _RUNTIME_DEBUG
        save_config(_CONFIG, CONFIG_FILE)
    # propagate to companion client if present
    try:
        if _companion_client is not None:
            _companion_client.debug = _RUNTIME_DEBUG
    except Exception:
        pass


def reload_config(force: bool = False) -> bool:
    """Reload `config.json` into module state if it changed on disk.

    Returns True if a reload occurred (and module state updated).
    """
    global _CONFIG, _RUNTIME_DEBUG, _companion_client, _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except Exception:
        mtime = None

    if not force and _config_mtime is not None and mtime == _config_mtime:
        return False

    cfg = load_config(CONFIG_FILE)
    _CONFIG = cfg
    with _debug_lock:
        _RUNTIME_DEBUG = bool(_CONFIG.get("debug", False))

    # recreate or update companion client
    try:
        _companion_client = Companion(_CONFIG.get("companion_ip", "127.0.0.1"), int(_CONFIG.get("companion_port", 8000)))
        _companion_client.debug = _RUNTIME_DEBUG
    except Exception:
        _companion_client = None

    _config_mtime = mtime
    return True


# Companion client singleton
_companion_client = None
try:
    _companion_client = Companion(_CONFIG.get("companion_ip", "127.0.0.1"), int(_CONFIG.get("companion_port", 8000)))
    _companion_client.debug = _RUNTIME_DEBUG
except Exception:
    _companion_client = None


def get_companion() -> Companion | None:
    return _companion_client


# Configure a simple file logger for calendar events. Use a rotating file to avoid unbounded growth.
_LOGGER = logging.getLogger("calendar")
if not _LOGGER.handlers:
    _LOGGER.setLevel(logging.INFO)
    handler = RotatingFileHandler("calendar.log", maxBytes=256 * 1024, backupCount=3)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(fmt)
    _LOGGER.addHandler(handler)


def get_logger():
    return _LOGGER
