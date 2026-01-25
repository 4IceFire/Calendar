import json
import os
import time as t
from datetime import datetime
from typing import List

from package.apps.calendar.models import Event, TimeOfTrigger


DEFAULT_EVENTS_FILE = "events.json"


events: List[Event] = []


def _dbg(msg: str) -> None:
    # lightweight debug helper; controlled by CALENDAR_DEBUG env var
    if os.getenv("CALENDAR_DEBUG", "0") in ("1", "true", "True"):
        print(f"[DEBUG {datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_events_safe(path: str = DEFAULT_EVENTS_FILE, retries: int = 10, delay: float = 0.05) -> List[Event]:
    last_err = None
    for _ in range(retries):
        try:
            with open(path, "r") as f:
                events_data = json.load(f)

            loaded_events: List[Event] = []
            # We must construct TimeOfTrigger properly using strings -> TypeofTime
            # Do the conversion without importing TypeofTime here to avoid circulars; import locally
            from package.apps.calendar.models import TypeofTime, WeekDay
            loaded_events = []
            # determine next id to assign when missing
            max_id = 0
            for ev in events_data:
                if isinstance(ev.get("id", None), int) and ev.get("id", 0) > max_id:
                    max_id = ev.get("id")

            for ev in events_data:
                times: list[TimeOfTrigger] = []
                for trig in ev.get("times", []):
                    action_type = str(trig.get("actionType") or trig.get("action_type") or "").strip().lower()
                    api = trig.get("api") if isinstance(trig, dict) else None

                    # Backward compatible inference: if api exists, treat as API action.
                    if not action_type:
                        action_type = "api" if isinstance(api, dict) else "companion"

                    if action_type == "api":
                        times.append(
                            TimeOfTrigger(
                                trig.get("minutes", 0),
                                TypeofTime[trig.get("typeOfTrigger", "AT")],
                                "",
                                actionType="api",
                                api=api if isinstance(api, dict) else {},
                            )
                        )
                    else:
                        times.append(
                            TimeOfTrigger(
                                trig.get("minutes", 0),
                                TypeofTime[trig.get("typeOfTrigger", "AT")],
                                trig.get("buttonURL", ""),
                                actionType="companion",
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
                if "active" not in ev:
                    ev["active"] = True
                    changed = True
                # ensure trigger action schema exists
                for trig in ev.get("times", []):
                    if not isinstance(trig, dict):
                        continue

                    action_type = str(trig.get("actionType") or trig.get("action_type") or "").strip().lower()
                    if not action_type:
                        action_type = "api" if isinstance(trig.get("api"), dict) else "companion"
                        trig["actionType"] = action_type
                        changed = True

                    if action_type == "api":
                        if "api" not in trig or not isinstance(trig.get("api"), dict):
                            trig["api"] = {}
                            changed = True
                        # keep legacy buttonURL if present; not used by executor
                    else:
                        if "buttonURL" not in trig:
                            trig["buttonURL"] = ""
                            changed = True
                # ensure id exists
                if "id" not in ev or not isinstance(ev.get("id"), int):
                    max_id += 1
                    ev["id"] = max_id
                    changed = True

            if changed:
                try:
                    with open(path, "w") as wf:
                        json.dump(events_data, wf, indent=2)
                    _dbg(f"Updated events file with defaults: {path}")
                except Exception:
                    pass

            return loaded_events

        except json.JSONDecodeError as e:
            last_err = e
            t.sleep(delay)
        except FileNotFoundError:
            try:
                with open(path, "w") as nf:
                    json.dump([], nf, indent=2)
                _dbg(f"Created missing events file: {path}")
            except Exception as e:
                raise e
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
                    "buttonURL": getattr(trig, "buttonURL", ""),
                }
                for trig in event.times
            ],
        }
        events_data.append(event_dict)

    with open(path, "w") as f:
        json.dump(events_data, f, indent=2)


# Convenience: load into module-level `events`
try:
    events = load_events_safe()
except Exception:
    events = []
