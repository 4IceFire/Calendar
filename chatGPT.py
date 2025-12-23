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


EVENTS_FILE = "chat_events.json"


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

    def __init__(self, events_file: str = EVENTS_FILE, poll_interval: float = 1.0) -> None:
        self.events_file = events_file
        self.poll_interval = poll_interval

        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._reload_needed = True

        self._heap: List[TriggerJob] = []
        self._last_mtime: Optional[float] = None

    def start(self) -> None:
        threading.Thread(target=self._watch_file, daemon=True).start()
        self._run_forever()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

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

            t.sleep(self.poll_interval)

    def _rebuild_schedule(self) -> None:
        now = datetime.now()
        loaded = load_events_safe(self.events_file)

        heap: list[TriggerJob] = []
        for ev in loaded:
            occ = next_weekly_occurrence(ev, now)
            if occ is not None:
                push_triggers_for_occurrence(heap, ev, occ, now)

        heapq.heapify(heap)
        self._heap = heap

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

    def _run_forever(self) -> None:
        while not self._stop.is_set():
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



# ----------------------------
# Companion
# ----------------------------
from companion import Companion

c = Companion("127.0.0.1", 8000)



# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    scheduler = ClockScheduler(EVENTS_FILE, poll_interval=1.0)
    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\n[CLOCK] Shutting down...")
        scheduler.stop()
