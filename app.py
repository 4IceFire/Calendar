"""Event scheduling and trigger runner.

This module defines event data structures, utilities to load/save events
from JSON, and a minimal clock scheduler that watches the events file for
changes and executes triggers at the right time.
"""

import heapq
import json
import os
import threading
import time as t  # For sleep functionality
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import List, Optional


# Defaults and configuration
DEFAULT_EVENTS_FILE = "events.json"
DEFAULT_COMPANION_IP = "127.0.0.1"
DEFAULT_COMPANION_PORT = 8000
DEFAULT_POLL_INTERVAL = 1.0
CONFIG_FILE = "config.json"

def _dbg(msg: str) -> None:
    if get_debug():
        print(f"[DEBUG {datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_config(path: str = CONFIG_FILE) -> dict:
    """Load JSON config. If missing, create with defaults and return it.

    Known keys:
      - EVENTS_FILE (str)
      - companion_ip (str)
      - companion_port (int)
      - poll_interval (float)
    """
    defaults = {
        "EVENTS_FILE": DEFAULT_EVENTS_FILE,
        "companion_ip": DEFAULT_COMPANION_IP,
        "companion_port": DEFAULT_COMPANION_PORT,
        "poll_interval": DEFAULT_POLL_INTERVAL,
        "debug": False,
    }

    try:
        with open(path, "r") as f:
            data = json.load(f) or {}
    except FileNotFoundError:
        _dbg(f"Config not found; creating default at {path}")
        save_config(defaults, path)
        return defaults.copy()
    except json.JSONDecodeError:
        _dbg(f"Config invalid JSON; recreating defaults at {path}")
        save_config(defaults, path)
        return defaults.copy()

    # Merge any missing defaults and persist
    changed = False
    for k, v in defaults.items():
        if k not in data:
            data[k] = v
            changed = True
    if changed:
        _dbg("Config missing keys; writing merged defaults")
        save_config(data, path)

    return data


def save_config(cfg: dict, path: str = CONFIG_FILE) -> None:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


# Load configuration and materialize runtime variables
_CONFIG = load_config(CONFIG_FILE)
EVENTS_FILE = _CONFIG.get("EVENTS_FILE", DEFAULT_EVENTS_FILE)
companion_ip = _CONFIG.get("companion_ip", DEFAULT_COMPANION_IP)
companion_port = int(_CONFIG.get("companion_port", DEFAULT_COMPANION_PORT))
POLL_INTERVAL = float(_CONFIG.get("poll_interval", DEFAULT_POLL_INTERVAL))
_RUNTIME_DEBUG = bool(_CONFIG.get("debug", False))
_debug_lock = threading.Lock()


def get_debug() -> bool:
    with _debug_lock:
        return _RUNTIME_DEBUG


def set_debug(value: bool, persist: bool = True) -> None:
    """Set runtime debug flag and optionally persist to config.json.

    For future UI control. Immediately affects logging and scheduler/companion.
    """
    global _RUNTIME_DEBUG
    with _debug_lock:
        _RUNTIME_DEBUG = bool(value)
    state = "ON" if _RUNTIME_DEBUG else "OFF"
    print(f"[DEBUG {datetime.now().strftime('%H:%M:%S')}] Debug toggled {state}")

    if persist:
        _CONFIG["debug"] = _RUNTIME_DEBUG
        save_config(_CONFIG, CONFIG_FILE)

    # Propagate to Companion client if available
    try:
        if 'c' in globals():
            c.debug = _RUNTIME_DEBUG
    except Exception:
        pass
_dbg(
    f"Configuration: EVENTS_FILE='{EVENTS_FILE}', companion={companion_ip}:{companion_port}, poll={POLL_INTERVAL}s"
)


class TypeofTime(Enum):
    BEFORE = 0
    AT = 1
    AFTER = 2


class WeekDay(Enum):
    Monday = 1
    Tuesday = 2
    Wednesday = 3
    Thursday = 4
    Friday = 5
    Saturday = 6
    Sunday = 7


class Event:
    """An event with optional weekly repetition and one or more triggers."""

    def __init__(
        self,
        name: str,
        day: WeekDay,
        event_date: date,
        event_time: time,
        repeating: bool,
        times: List["TimeOfTrigger"],
    ) -> None:
        self.name = name
        self.day = day
        # Start date of the series (for repeating) or actual date (non-repeating)
        self.date = event_date
        # Time of day the event starts
        self.time = event_time
        # True => repeats weekly on `self.day` at `self.time`
        self.repeating = repeating
        self.times = times

        # Ensure triggers are in chronological order relative to event start
        # (BEFORE negative first, AFTER positive last)
        self.times.sort()

    def __str__(self) -> str:
        return f"   {self.name}   \n" + (len(self.name) + 6) * "-" + "\n"


class TimeOfTrigger:
    """A trigger offset relative to an event start time."""

    def __init__(self, minutes: int, typeOfTrigger: TypeofTime, buttonURL: str) -> None:
        self.minutes = minutes
        self.typeOfTrigger = typeOfTrigger
        self.buttonURL = buttonURL

        if typeOfTrigger == TypeofTime.BEFORE:
            self.timer = -minutes
        elif typeOfTrigger == TypeofTime.AT:
            self.timer = 0
        elif typeOfTrigger == TypeofTime.AFTER:
            self.timer = minutes
        else:
            raise ValueError("Impossible Selection")

    def __lt__(self, other: "TimeOfTrigger") -> bool:
        return self.timer < other.timer

    def __str__(self) -> str:
        return str(self.timer)


# ----------------------------
# Load / Save (patched)
# ----------------------------
events: List[Event] = []


def load_events_safe(path: str = EVENTS_FILE, retries: int = 10, delay: float = 0.05) -> List[Event]:
    """
    Reads events.json safely even if your GUI is writing the file at the same time.
    Retries on JSONDecodeError (partial writes).
    """
    last_err = None
    for _ in range(retries):
        try:
            with open(path, "r") as f:
                events_data = json.load(f)

            loaded_events: List[Event] = []
            for ev in events_data:
                times = [
                    TimeOfTrigger(
                        trig["minutes"],
                        TypeofTime[trig["typeOfTrigger"]],
                        trig.get("buttonURL", "")
                    )
                    for trig in ev["times"]
                ]

                loaded_events.append(
                    Event(
                        ev["name"],
                        WeekDay[ev["day"]],
                        datetime.strptime(ev["date"], "%Y-%m-%d").date(),
                        datetime.strptime(ev["time"], "%H:%M:%S").time(),
                        ev["repeating"],
                        times
                    )
                )

            return loaded_events

        except json.JSONDecodeError as e:
            last_err = e
            t.sleep(delay)

        except FileNotFoundError:
            # If the events file doesn't exist, create an empty one and use it
            try:
                with open(path, "w") as nf:
                    json.dump([], nf, indent=2)
                if get_debug():
                    print(f"[DEBUG {datetime.now().strftime('%H:%M:%S')}] Created missing events file: {path}")
            except Exception as e:
                # If we fail to create, propagate the error
                raise e
            return []

    raise last_err


def load_events() -> List[Event]:
    """Load events from the default events file."""
    return load_events_safe(EVENTS_FILE)


def save_events() -> None:
    """Persist in-memory events to disk, preserving trigger button URLs."""
    with open(EVENTS_FILE, "w") as file:
        events_data = []
        for event in events:
            event_dict = {
                "name": event.name,
                "day": event.day.name,
                "date": event.date.strftime("%Y-%m-%d"),
                "time": event.time.strftime("%H:%M:%S"),
                "repeating": event.repeating,
                "times": [
                    {
                        "minutes": trig.minutes,
                        "typeOfTrigger": trig.typeOfTrigger.name,
                        "buttonURL": trig.buttonURL,  # <-- PATCH: keep this
                    }
                    for trig in event.times
                ],
            }
            events_data.append(event_dict)

        json.dump(events_data, file, indent=2)


# Load initial in-memory list (optional, but keeps your previous pattern)
events = load_events()


# ----------------------------
# Scheduler helpers (weekly repeating + trigger heap)
# ----------------------------
def next_weekly_occurrence(event: Event, now: datetime) -> Optional[datetime]:
    """
    Returns the next start datetime for an event.
    - Non-repeating: event.date + event.time (if in the future)
    - Repeating: weekly on event.day at event.time, not before event.date
    """
    base = datetime.combine(event.date, event.time)

    # Non-repeating: one-shot
    if not event.repeating:
        return base if base > now else None

    # Repeating weekly:
    # WeekDay enum: Monday=1..Sunday=7 ; Python weekday: Monday=0..Sunday=6
    target_weekday = event.day.value - 1

    # Can't repeat before series start date
    start_date = max(event.date, now.date())

    days_ahead = (target_weekday - start_date.weekday()) % 7
    candidate_date = start_date + timedelta(days=days_ahead)
    candidate = datetime.combine(candidate_date, event.time)

    # If candidate is today but already passed, move to next week
    if candidate <= now:
        candidate += timedelta(days=7)

    return candidate


@dataclass(order=True)
class TriggerJob:
    due: datetime
    event: Event = field(compare=False)
    # Event start datetime for this occurrence
    occurrence: datetime = field(compare=False)
    trigger_index: int = field(compare=False)
    trigger: TimeOfTrigger = field(compare=False)


def push_triggers_for_occurrence(
    heap: List["TriggerJob"],
    event: Event,
    occurrence: datetime,
    now: datetime,
) -> None:
    """Expand one event occurrence into multiple TriggerJobs (one per trigger)."""
    for idx, trig in enumerate(event.times):
        due = (occurrence + timedelta(minutes=trig.timer)).replace(microsecond=0)

        # Only schedule triggers in the future
        if due > now:
            heapq.heappush(heap, TriggerJob(due, event, occurrence, idx, trig))


# ----------------------------
# Clock scheduler (watches file + executes triggers)
# ----------------------------
class ClockScheduler:
    """Watches the events file and executes due triggers."""

    def __init__(self, events_file: str = EVENTS_FILE, poll_interval: float = 1.0, *, debug: bool = False) -> None:
        self.events_file = events_file
        self.poll_interval = poll_interval
        self.debug = debug

        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._reload_needed = True

        self._heap: List[TriggerJob] = []
        self._last_mtime: Optional[float] = None

    def _dbg(self, msg: str) -> None:
        if self.debug:
            print(f"[DEBUG {datetime.now().strftime('%H:%M:%S')}] {msg}")

    def _refresh_debug_dynamic(self) -> None:
        """Adopt latest debug flag from runtime setting (for future UI changes)."""
        new_flag = get_debug()
        if new_flag != self.debug:
            self.debug = new_flag
            state = "ON" if self.debug else "OFF"
            print(f"[DEBUG {datetime.now().strftime('%H:%M:%S')}] Debug toggled {state}")
            try:
                # Propagate to Companion client if available
                if 'c' in globals():
                    c.debug = self.debug
            except Exception:
                pass

    def start(self) -> None:
        self._dbg(f"Scheduler starting (file={self.events_file}, poll={self.poll_interval}s)")
        threading.Thread(target=self._watch_file, daemon=True).start()
        self._run_forever()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        self._dbg("Scheduler stopped")

    def _watch_file(self) -> None:
        """
        Polls the modification time. When events.json changes, signal a rebuild.
        (Cross-platform, no extra deps.)
        """
        while not self._stop.is_set():
            try:
                mtime = os.path.getmtime(self.events_file)
            except FileNotFoundError:
                mtime = None

            if mtime != self._last_mtime:
                self._last_mtime = mtime
                with self._cv:
                    self._reload_needed = True
                    self._cv.notify()
                self._dbg("Detected change in events file; scheduling reload")

            t.sleep(self.poll_interval)

    def _rebuild_schedule(self) -> None:
        now = datetime.now()
        loaded = load_events_safe(self.events_file)
        self._dbg(f"Reloaded events file: {len(loaded)} event(s)")

        heap: list[TriggerJob] = []
        for ev in loaded:
            occ = next_weekly_occurrence(ev, now)
            if occ is not None:
                push_triggers_for_occurrence(heap, ev, occ, now)

        heapq.heapify(heap)
        self._heap = heap
        if self.debug:
            upcoming = sorted(self._heap)
            self._dbg(f"Scheduled {len(upcoming)} trigger(s)")
            for i, job in enumerate(upcoming[:20]):
                self._dbg(
                    f"#{i+1:02d} due={job.due.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"event='{job.event.name}' | offset={job.trigger.timer}min | url='{job.trigger.buttonURL}'"
                )

    def _handle_trigger(self, job: TriggerJob) -> None:
        """
        Central place all triggers funnel through.
        """
        print(
            f"[TRIGGER] {job.due} | Event='{job.event.name}' | "
            f"offset={job.trigger.timer}min | url='{job.trigger.buttonURL}'"
        )

        # TODO: Call your real function here
        # e.g. trigger_action(event=job.event, trigger=job.trigger, due_time=job.due, event_start=job.occurrence)
        #global c
        if c.connected:
            ok = c.post_command(job.trigger.buttonURL)
            self._dbg(f"Companion POST '{job.trigger.buttonURL}' -> {'OK' if ok else 'FAIL'}")
        else:
            self._dbg("Companion not connected; skipping POST")


    def _run_forever(self) -> None:
        while not self._stop.is_set():
            # Refresh debug mode dynamically each loop
            self._refresh_debug_dynamic()
            # Rebuild schedule if needed
            with self._cv:
                if self._reload_needed:
                    try:
                        self._rebuild_schedule()
                        self._reload_needed = False
                    except Exception as e:
                        print(f"[CLOCK] Failed to reload events: {e}")
                        self._cv.wait(timeout=1.0)
                        continue

                if not self._heap:
                    # No upcoming triggers; wait a bit or until file changes
                    self._cv.wait(timeout=1.0)
                    continue

                next_job = self._heap[0]
                now = datetime.now()
                seconds = (next_job.due - now).total_seconds()

                # Wait in short chunks to stay responsive to file edits,
                # while still being accurate near the due time.
                timeout = max(0.0, min(seconds, 1.0))
                if self.debug and seconds > 0:
                    self._dbg(
                        f"Next trigger in {seconds:.1f}s at {next_job.due.strftime('%H:%M:%S')} for '{next_job.event.name}'"
                    )
                self._cv.wait(timeout=timeout)

                # If file changed during wait, rebuild next loop
                if self._reload_needed:
                    continue

            # Fire all due jobs (outside the lock)
            while True:
                with self._cv:
                    if self._reload_needed or not self._heap:
                        break

                    job = self._heap[0]
                    if job.due > datetime.now():
                        break

                    heapq.heappop(self._heap)

                # Execute trigger
                try:
                    self._handle_trigger(job)
                except Exception as e:
                    print(f"[CLOCK] Trigger handler error: {e}")

                # If repeating, schedule next week's occurrence AFTER the last trigger for this occurrence fires
                if job.event.repeating and job.trigger_index == (len(job.event.times) - 1):
                    next_occ = next_weekly_occurrence(job.event, job.occurrence + timedelta(seconds=1))
                    if next_occ is not None:
                        with self._cv:
                            push_triggers_for_occurrence(self._heap, job.event, next_occ, datetime.now())
                            heapq.heapify(self._heap)
                        self._dbg(
                            f"Rescheduled weekly event '{job.event.name}' for {next_occ.strftime('%Y-%m-%d %H:%M:%S')}"
                        )



# ----------------------------
# Companion
# ----------------------------
from companion import Companion

companion_ip = "127.0.0.1"
companion_port = 8000

c = Companion(companion_ip, companion_port)



# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    scheduler = ClockScheduler(EVENTS_FILE, poll_interval=POLL_INTERVAL, debug=get_debug())
    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\n[CLOCK] Shutting down...")
        scheduler.stop()