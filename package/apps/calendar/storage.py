import copy
import json
import os
import threading
import time as t
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from package.apps.calendar.models import Event, TimeOfTrigger


DEFAULT_EVENTS_FILE = "events.json"


# Legacy compatibility alias.
# Use load_events_safe()/load_events() for fresh reads instead of relying on
# an import-time cache.
events: List[Event] = []
_events_cache_lock = threading.RLock()
_events_cache: dict[str, dict[str, Any]] = {}


def _dbg(msg: str) -> None:
    # lightweight debug helper; controlled by CALENDAR_DEBUG env var
    if os.getenv("CALENDAR_DEBUG", "0") in ("1", "true", "True"):
        print(f"[DEBUG {datetime.now().strftime('%H:%M:%S')}] {msg}")


def _cache_key(path: str | Path) -> str:
    try:
        return str(Path(path).resolve())
    except Exception:
        return str(path)


def _file_mtime_ns(path: str | Path) -> int | None:
    try:
        return Path(path).stat().st_mtime_ns
    except Exception:
        return None


def _copy_events_list(value: list[Event]) -> list[Event]:
    try:
        return copy.deepcopy(value)
    except Exception:
        return list(value or [])


def load_events_safe(path: str = DEFAULT_EVENTS_FILE, retries: int = 10, delay: float = 0.05) -> List[Event]:
    cache_key = _cache_key(path)
    current_mtime = _file_mtime_ns(path)
    with _events_cache_lock:
        cached = _events_cache.get(cache_key)
        if cached is not None and cached.get("mtime_ns") == current_mtime:
            cached_events = cached.get("events")
            if isinstance(cached_events, list):
                return _copy_events_list(cached_events)

    last_err = None
    for _ in range(retries):
        try:
            with open(path, "r", encoding="utf-8") as f:
                events_data = json.load(f)
            if not isinstance(events_data, list):
                events_data = []

            # We must construct TimeOfTrigger properly using strings -> TypeofTime
            # Do the conversion without importing TypeofTime here to avoid circulars; import locally
            from package.apps.calendar.models import TypeofTime, WeekDay

            loaded_events: List[Event] = []

            # determine next id to assign when missing
            max_id = 0
            for ev in events_data:
                if isinstance(ev, dict) and isinstance(ev.get("id", None), int) and ev.get("id", 0) > max_id:
                    max_id = ev.get("id")

            for ev in events_data:
                if not isinstance(ev, dict):
                    continue
                times: list[TimeOfTrigger] = []
                for trig in ev.get("times", []):
                    if not isinstance(trig, dict):
                        continue
                    action_type = str(trig.get("actionType") or trig.get("action_type") or "").strip().lower()
                    api = trig.get("api")
                    timer = trig.get("timer")
                    enabled = True
                    trig_name = str(trig.get("name") or "").strip()
                    trig_uid = str(trig.get("uid") or "").strip()
                    trig_uid = trig_uid or None
                    try:
                        if "enabled" in trig:
                            enabled = bool(trig.get("enabled"))
                        elif "active" in trig:
                            enabled = bool(trig.get("active"))
                    except Exception:
                        enabled = True

                    # Backward compatible inference: if api exists, treat as API action.
                    if not action_type:
                        if isinstance(api, dict):
                            action_type = "api"
                        elif isinstance(timer, dict):
                            action_type = "timer"
                        else:
                            action_type = "companion"

                    if action_type == "api":
                        times.append(
                            TimeOfTrigger(
                                trig.get("minutes", 0),
                                TypeofTime[trig.get("typeOfTrigger", "AT")],
                                "",
                                name=trig_name,
                                uid=trig_uid,
                                actionType="api",
                                api=api if isinstance(api, dict) else {},
                                timer=None,
                                enabled=enabled,
                            )
                        )
                    elif action_type == "timer":
                        times.append(
                            TimeOfTrigger(
                                trig.get("minutes", 0),
                                TypeofTime[trig.get("typeOfTrigger", "AT")],
                                "",
                                name=trig_name,
                                uid=trig_uid,
                                actionType="timer",
                                api=None,
                                timer=timer if isinstance(timer, dict) else {},
                                enabled=enabled,
                            )
                        )
                    else:
                        times.append(
                            TimeOfTrigger(
                                trig.get("minutes", 0),
                                TypeofTime[trig.get("typeOfTrigger", "AT")],
                                trig.get("buttonURL", ""),
                                name=trig_name,
                                uid=trig_uid,
                                actionType="companion",
                                timer=None,
                                enabled=enabled,
                            )
                        )

                # assign id if missing
                ev_id = ev.get("id")
                if not isinstance(ev_id, int):
                    max_id += 1
                    ev_id = max_id
                    ev["id"] = ev_id

                loaded_events.append(
                    Event(
                        ev.get("name", ""),
                        ev_id,
                        WeekDay[ev.get("day", "Monday")],
                        datetime.strptime(ev.get("date", "1970-01-01"), "%Y-%m-%d").date(),
                        datetime.strptime(ev.get("time", "00:00:00"), "%H:%M:%S").time(),
                        ev.get("repeating", False),
                        times,
                        ev.get("active", True),
                    )
                )

            # Merge defaults back into the file if necessary
            changed = False
            for ev in events_data:
                if not isinstance(ev, dict):
                    continue
                if "active" not in ev:
                    ev["active"] = True
                    changed = True
                # ensure trigger action schema exists
                for trig in ev.get("times", []):
                    if not isinstance(trig, dict):
                        continue

                    if "enabled" not in trig:
                        try:
                            trig["enabled"] = bool(trig.get("active", True))
                        except Exception:
                            trig["enabled"] = True
                        changed = True

                    action_type = str(trig.get("actionType") or trig.get("action_type") or "").strip().lower()
                    if not action_type:
                        if isinstance(trig.get("api"), dict):
                            action_type = "api"
                        elif isinstance(trig.get("timer"), dict):
                            action_type = "timer"
                        else:
                            action_type = "companion"
                        trig["actionType"] = action_type
                        changed = True

                    if action_type == "api":
                        if "api" not in trig or not isinstance(trig.get("api"), dict):
                            trig["api"] = {}
                            changed = True
                        # keep legacy buttonURL if present; not used by executor
                    elif action_type == "timer":
                        if "timer" not in trig or not isinstance(trig.get("timer"), dict):
                            trig["timer"] = {}
                            changed = True
                    else:
                        if "buttonURL" not in trig:
                            trig["buttonURL"] = ""
                            changed = True

                    if "uid" not in trig or not str(trig.get("uid") or "").strip():
                        trig["uid"] = str(uuid.uuid4())
                        changed = True
                # ensure id exists
                if "id" not in ev or not isinstance(ev.get("id"), int):
                    max_id += 1
                    ev["id"] = max_id
                    changed = True

            wrote_back = False
            if changed:
                try:
                    with open(path, "w", encoding="utf-8") as wf:
                        json.dump(events_data, wf, indent=2)
                    _dbg(f"Updated events file with defaults: {path}")
                    wrote_back = True
                except Exception:
                    pass

            if not changed or wrote_back:
                with _events_cache_lock:
                    _events_cache[cache_key] = {
                        "mtime_ns": _file_mtime_ns(path),
                        "events": _copy_events_list(loaded_events),
                    }

            return _copy_events_list(loaded_events)

        except json.JSONDecodeError as e:
            last_err = e
            t.sleep(delay)
        except FileNotFoundError:
            try:
                with open(path, "w", encoding="utf-8") as nf:
                    json.dump([], nf, indent=2)
                _dbg(f"Created missing events file: {path}")
            except Exception as e:
                raise e
            with _events_cache_lock:
                _events_cache[cache_key] = {"mtime_ns": _file_mtime_ns(path), "events": []}
            return []

    raise last_err


def load_events(path: str = DEFAULT_EVENTS_FILE) -> List[Event]:
    return load_events_safe(path)


def save_events(events_list: List[Event], path: str = DEFAULT_EVENTS_FILE) -> None:
    events_data = []
    for event in events_list:
        event_dict = {
            "id": getattr(event, "id", None),
            "name": event.name,
            "day": event.day.name,
            "date": event.date.strftime("%Y-%m-%d"),
            "time": event.time.strftime("%H:%M:%S"),
            "repeating": event.repeating,
            "active": getattr(event, "active", True),
            "times": [
                trig.to_dict() if hasattr(trig, "to_dict") else {
                    "minutes": trig.minutes,
                    "typeOfTrigger": trig.typeOfTrigger.name,
                    "enabled": bool(getattr(trig, "enabled", True)),
                    "buttonURL": getattr(trig, "buttonURL", ""),
                }
                for trig in event.times
            ],
        }
        events_data.append(event_dict)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(events_data, f, indent=2)
    with _events_cache_lock:
        _events_cache[_cache_key(path)] = {
            "mtime_ns": _file_mtime_ns(path),
            "events": _copy_events_list(events_list),
        }


