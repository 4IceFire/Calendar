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
TIMER_PRESETS_FILE = "timer_presets.json"

_defaults = {
    "EVENTS_FILE": DEFAULT_EVENTS_FILE,
    "companion_ip": "127.0.0.1",
    "companion_port": 8000,
    # Prefix for Companion custom variables storing timer names, e.g. timer_name_1
    "companion_timer_name": "timer_name_",
    "propresenter_ip": "127.0.0.1",
    "propresenter_port": 1025,
    # Timers app defaults
    # Which ProPresenter timer to control
    "propresenter_timer_index": 1,
    # Web UI port
    "webserver_port": 5000,
    "poll_interval": 1.0,
    "debug": False,
}


def _coerce_timer_preset(value: Any) -> Dict[str, str] | None:
    """Coerce a timer preset into a normalized dict form.

    Supported on-disk / API formats:
    - "HH:MM" (string)
    - {"time": "HH:MM", "name": "Some Name"}

    Returns None for unusable entries.
    """
    try:
        if isinstance(value, dict):
            t = str(value.get("time", "")).strip()
            n = str(value.get("name", "")).strip()
        else:
            t = str(value).strip()
            n = ""
    except Exception:
        return None

    if not t:
        return None
    if not n:
        n = t
    return {"time": t, "name": n}


def load_timer_presets(path: str = TIMER_PRESETS_FILE) -> list[Dict[str, str]]:
    """Load timer presets from a dedicated JSON file.

    File format:
    - preferred: JSON array of objects {"time": "HH:MM", "name": "..."}
    - legacy: JSON array of strings "HH:MM"
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        out: list[Dict[str, str]] = []
        for v in data:
            p = _coerce_timer_preset(v)
            if p is not None:
                out.append(p)
        return out
    except FileNotFoundError:
        # create with defaults
        defaults = [
            {"time": "08:15", "name": "08:15"},
            {"time": "08:30", "name": "08:30"},
            {"time": "09:10", "name": "09:10"},
            {"time": "09:30", "name": "09:30"},
        ]
        try:
            save_timer_presets(defaults, path)
        except Exception:
            pass
        return defaults
    except Exception:
        return []


def save_timer_presets(presets: list[Any], path: str = TIMER_PRESETS_FILE) -> None:
    try:
        normalized: list[Dict[str, str]] = []
        for v in presets or []:
            p = _coerce_timer_preset(v)
            if p is not None:
                normalized.append(p)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2)
    except Exception:
        pass


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

    # Backward-compatible migrations for older web server config keys.
    # - `server_port` -> `webserver_port`
    if "webserver_port" not in data and "server_port" in data:
        data["webserver_port"] = data.get("server_port")
        changed = True

    # Migrate old inline presets into the dedicated presets file.
    # This keeps config.json free of large lists.
    if "timer_presets" in data:
        try:
            presets = data.get("timer_presets")
            if isinstance(presets, list) and presets:
                save_timer_presets(presets)
        except Exception:
            pass
        data.pop("timer_presets", None)
        changed = True

    # Backward-compatible migrations for older timers config keys.
    # - `timer_index` -> `propresenter_timer_index`
    if "propresenter_timer_index" not in data and "timer_index" in data:
        data["propresenter_timer_index"] = data.get("timer_index")
        changed = True
    for k, v in _defaults.items():
        if k not in data:
            data[k] = v
            changed = True

    # Prefer the new keys going forward; remove legacy keys if present.
    if "timer_index" in data:
        data.pop("timer_index", None)
        changed = True
    if "companion_timer_variable" in data:
        data.pop("companion_timer_variable", None)
        changed = True

    # Remove request-only unrelated keys from older versions.
    if "companion_preset_variable" in data:
        data.pop("companion_preset_variable", None)
        changed = True
    if "companion_preset_is_one_based" in data:
        data.pop("companion_preset_is_one_based", None)
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
