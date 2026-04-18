from __future__ import annotations

import json
import threading
from typing import Any, Dict, TYPE_CHECKING
import re
import secrets
from pathlib import Path

from package.json_cache import read_json, remember_json, write_json
from package.apps.calendar.storage import DEFAULT_EVENTS_FILE
import logging
from logging.handlers import RotatingFileHandler

if TYPE_CHECKING:
    from companion import Companion

CONFIG_FILE = "config.json"
TIMER_PRESETS_FILE = "timer_presets.json"
_timer_presets_lock = threading.RLock()
_TIMER_NAME_UNSET = object()
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def get_project_path(*parts: str) -> Path:
    """Return an absolute path anchored at the repository root."""
    return _PROJECT_ROOT.joinpath(*parts)

_defaults = {
    "EVENTS_FILE": DEFAULT_EVENTS_FILE,
    "companion_ip": "127.0.0.1",
    "companion_port": 8888,
    # Prefix for Companion custom variables storing timer names, e.g. timer_name_1
    "companion_timer_name": "timer_name_",
    "propresenter_ip": "127.0.0.1",
    "propresenter_port": 4000,
    # Timers app defaults
    # Which ProPresenter timer to control
    "propresenter_timer_index": 2,
    "propresenter_is_latest": True,
    "propresenter_timer_wait_stop_ms": 200,
    "propresenter_timer_wait_set_ms": 600,
    "propresenter_timer_wait_reset_ms": 1000,
    # Timer preset (1-based) used to build the stream-start stage message.
    # 0 means "not configured".
    "stream_start_preset": 0,
    # Web UI port
    "webserver_port": 5000,
    "poll_interval": 1,
    "debug": False,
    # Web UI theme
    "dark_mode": True,

    # Web UI message/alert auto-hide timeout
    # 0 disables auto-hide.
    "webui_message_timeout_seconds": 4,

    # Auth (Web UI pages only)
    # NOTE: /api/* endpoints are intentionally left open for Companion.
    "auth_enabled": True,
    "auth_idle_timeout_enabled": True,
    "auth_idle_timeout_minutes": 2,
    "auth_min_password_length": 6,

    # Scheduler/internal API
    "internal_api_timeout_seconds": 10,

    # VideoHub defaults
    "videohub_ip": "172.20.10.11",
    "videohub_port": 9990,
    "videohub_timeout": 2,
    "videohub_presets_file": "videohub_presets.json",

    # Realtime transcription defaults
    "transcription_enabled": False,
    "transcription_remote_enabled": True,
    "transcription_ingest_token": "",
    "transcription_chunk_ms": 200,
    "transcription_language": "en",
    "transcription_model": "small.en",
    "transcription_realtime_model": "tiny.en",
    "transcription_device": "cpu",
    "transcription_enable_realtime": True,
    "transcription_pause_soft_seconds": 1.0,
    "transcription_pause_hard_seconds": 2.5,
    "transcription_keep_history": False,
    "transcription_history_limit": 10,
    "transcription_font_scale": 1.0,
    "transcription_line_spacing": 1.25,
    "transcription_show_timestamps": True,
    "transcription_show_live_line": True,
    "transcription_segment_compact_mode": False,
    "transcription_color_live_bg": "#121a2c",
    "transcription_color_live_text": "#f8fafc",
    "transcription_color_segment_bg": "#182235",
    "transcription_color_segment_text": "#e5e7eb",
    "transcription_color_break_soft_bg": "#17354a",
    "transcription_color_break_soft_text": "#7dd3fc",
    "transcription_color_break_hard_bg": "#4f46e5",
    "transcription_color_break_hard_text": "#ffffff",
    "transcription_source_name": "Church Comms",
}


def _coerce_timer_preset(value: Any) -> Dict[str, Any] | None:
    """Coerce a timer preset into a normalized dict form.

    Supported on-disk / API formats:
    - "HH:MM" (string)
    - {"time": "HH:MM", "name": "Some Name", "button_presses": [{"buttonURL": "location/1/0/1/press"}, ...]}

    Returns None for unusable entries.
    """

    def _normalize_button_url(raw: Any) -> str | None:
        try:
            s = str(raw or '').strip()
        except Exception:
            return None
        if not s:
            return None
        if re.match(r'^location/\d+/\d+/\d+/press$', s):
            return s
        if re.match(r'^\d+/\d+/\d+$', s):
            return f'location/{s}/press'
        return None

    def _coerce_button_presses(raw: Any) -> list[dict[str, str]]:
        if raw is None:
            return []
        if isinstance(raw, dict) or isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []

        out: list[dict[str, str]] = []
        for item in raw:
            if isinstance(item, str):
                url = _normalize_button_url(item)
                if url:
                    out.append({'buttonURL': url})
                continue

            if isinstance(item, dict):
                url = _normalize_button_url(item.get('buttonURL') or item.get('url') or item.get('button_url'))
                if url:
                    out.append({'buttonURL': url})
                continue
        return out
    try:
        if isinstance(value, dict):
            t = str(value.get("time", "")).strip()
            n = str(value.get("name", "")).strip()
            presses = _coerce_button_presses(
                value.get('button_presses')
                if 'button_presses' in value
                else value.get('buttonPresses')
                if 'buttonPresses' in value
                else value.get('actions')
            )
        else:
            t = str(value).strip()
            n = ""
            presses = []
    except Exception:
        return None

    if not t:
        return None
    if not n:
        n = t

    out: Dict[str, Any] = {"time": t, "name": n}
    if presses:
        out['button_presses'] = presses
    return out  # type: ignore[return-value]


def _normalize_timer_presets_list(presets: list[Any] | None) -> list[Dict[str, Any]]:
    normalized: list[Dict[str, Any]] = []
    for v in presets or []:
        p = _coerce_timer_preset(v)
        if p is not None:
            normalized.append(p)
    return normalized


def _atomic_write_json(path: str, data: Any) -> None:
    write_json(path, data)


def load_timer_presets(path: str = TIMER_PRESETS_FILE) -> list[Dict[str, Any]]:
    """Load timer presets from a dedicated JSON file.

    File format:
    - preferred: JSON array of objects {"time": "HH:MM", "name": "..."}
    - legacy: JSON array of strings "HH:MM"
    """
    with _timer_presets_lock:
        defaults = [
            {"time": "08:15", "name": "Timer 1"},
            {"time": "08:30", "name": "Timer 2"},
            {"time": "09:10", "name": "Timer 3"},
            {"time": "09:30", "name": "Timer 4"},
        ]

        def _transform(raw: Any) -> tuple[list[Dict[str, Any]], bool]:
            if not isinstance(raw, list):
                return [], True
            normalized = _normalize_timer_presets_list(raw)
            return normalized, normalized != raw

        try:
            presets, changed = read_json(
                path,
                default_factory=lambda: list(defaults),
                create_if_missing=True,
                transform=_transform,
            )
        except Exception:
            presets = []
            changed = False

        if changed:
            try:
                write_json(path, presets)
            except Exception:
                pass
        return list(presets)


def save_timer_presets(presets: list[Any], path: str = TIMER_PRESETS_FILE) -> None:
    with _timer_presets_lock:
        normalized = _normalize_timer_presets_list(presets)
        _atomic_write_json(path, normalized)
        try:
            remember_json(path, normalized)
        except Exception:
            pass


def update_timer_preset(
    preset_number: int,
    *,
    time_str: str,
    name: Any = _TIMER_NAME_UNSET,
    path: str = TIMER_PRESETS_FILE,
) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
    with _timer_presets_lock:
        presets = load_timer_presets(path)
        preset_index = int(preset_number) - 1
        if preset_index < 0 or preset_index >= len(presets):
            raise IndexError(f"preset out of range (1..{len(presets)})")

        current = presets[preset_index]
        updated = dict(current) if isinstance(current, dict) else {"time": str(current).strip(), "name": str(current).strip()}
        updated["time"] = str(time_str).strip()
        if name is not _TIMER_NAME_UNSET:
            updated["name"] = str(name or "").strip() or updated.get("name", "")

        presets[preset_index] = updated
        save_timer_presets(presets, path)
        return presets, updated


def load_config(path: str = CONFIG_FILE) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
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

    # Ensure we have a stable secret key for Flask sessions.
    # Generate one once and persist it.
    if not str(data.get('flask_secret_key') or '').strip():
        try:
            data['flask_secret_key'] = secrets.token_hex(32)
            changed = True
        except Exception:
            pass

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
    for legacy_key in ("transcription_bind_host", "transcription_sender_input_device", "transcription_color_scheme"):
        if legacy_key in data:
            data.pop(legacy_key, None)
            changed = True
    if changed:
        save_config(data, path)

    return data


def save_config(cfg: Dict[str, Any], path: str = CONFIG_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


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
    _companion_client = _create_companion_client(_CONFIG)

    _config_mtime = mtime
    return True


# Companion client singleton
_companion_client = None


def _create_companion_client(cfg: Dict[str, Any]) -> Companion | None:
    try:
        from companion import Companion as _Companion
    except ModuleNotFoundError as e:
        # Allows the app to run even if optional dependencies (like `requests`) are missing.
        logging.getLogger("calendar").warning("Companion client unavailable (%s).", e)
        return None

    try:
        client = _Companion(cfg.get("companion_ip", "127.0.0.1"), int(cfg.get("companion_port", 8000)))
        client.debug = bool(cfg.get("debug", False))
        return client
    except Exception:
        return None


_companion_client = _create_companion_client(_CONFIG)


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
