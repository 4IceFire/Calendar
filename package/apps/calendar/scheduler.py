import heapq
import threading
import time as t
from datetime import datetime, timedelta
from typing import List, Optional
import json
from pathlib import Path

from package.apps.calendar.models import Event, TimeOfTrigger, TriggerJob
from package.apps.calendar import storage, utils
logger = utils.get_logger()


class ClockScheduler:
    def __init__(self, events_file: str = storage.DEFAULT_EVENTS_FILE, poll_interval: float = 1.0, *, debug: bool = False) -> None:
        self.events_file = events_file
        self.poll_interval = poll_interval
        self.debug = debug

        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._reload_needed = True

        self._heap: List[TriggerJob] = []
        self._last_mtime: Optional[float] = None
        # track config.json mtime so we can react to config changes at runtime
        self._last_config_mtime: Optional[float] = None

        # Companion client from shared utils
        self.c = utils.get_companion()
        # track last-known companion connectivity to avoid noisy prints
        self._companion_down = False
        # Track next-job alerts to avoid repeating threshold notices
        self._next_due = None
        self._announced_thresholds = set()

    def _dbg(self, msg: str) -> None:
        if self.debug:
            print(f"[DEBUG {datetime.now().strftime('%H:%M:%S')}] {msg}")

    def _refresh_debug_dynamic(self) -> None:
        # Reload config from disk if it changed so edits to config.json take
        # effect without restarting the scheduler. Then refresh the dynamic
        # debug setting from shared utils.
        try:
            utils.reload_config()
        except Exception:
            pass

        try:
            new_debug = bool(utils.get_debug())
        except Exception:
            new_debug = False

        if new_debug != self.debug:
            # Print a single-line notice about the change (useful to operators)
            print(f"[DEBUG] Dynamic debug set to {new_debug}")
            self.debug = new_debug

        # Also refresh the companion client reference and propagate debug
        try:
            self.c = utils.get_companion()
            if self.c is not None:
                try:
                    self.c.debug = self.debug
                except Exception:
                    pass
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
        while not self._stop.is_set():
            try:
                mtime = __import__("os").path.getmtime(self.events_file)
            except FileNotFoundError:
                mtime = None

            # detect changes to the events file
            if mtime != self._last_mtime:
                self._last_mtime = mtime
                with self._cv:
                    self._reload_needed = True
                    self._cv.notify()
                self._dbg("Detected change in events file; scheduling reload")

            # detect changes to the config file and reload runtime config
            try:
                cfg_mtime = __import__("os").path.getmtime(utils.CONFIG_FILE)
            except FileNotFoundError:
                cfg_mtime = None

            if cfg_mtime != self._last_config_mtime:
                # update stored mtime first to avoid repeated reloads
                self._last_config_mtime = cfg_mtime
                try:
                    reloaded = utils.reload_config()
                except Exception:
                    reloaded = False

                if reloaded:
                    # Pull new companion client and dynamic debug immediately
                    try:
                        self.c = utils.get_companion()
                        new_debug = bool(utils.get_debug())
                        if new_debug != self.debug:
                            print(f"[DEBUG] Dynamic debug set to {new_debug}")
                            self.debug = new_debug
                            try:
                                if self.c is not None:
                                    self.c.debug = self.debug
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # If events filename changed in config, adopt it and force reload
                    try:
                        cfg = utils.get_config()
                        new_events = cfg.get("EVENTS_FILE", self.events_file)
                        if new_events != self.events_file:
                            self._dbg(f"Config changed EVENTS_FILE: '{self.events_file}' -> '{new_events}'")
                            self.events_file = new_events
                            # force events-file mtime refresh so loader picks up the file
                            self._last_mtime = None

                    except Exception:
                        pass

                    with self._cv:
                        self._reload_needed = True
                        self._cv.notify()
                    self._dbg("Detected change in config file; scheduling reload")

            t.sleep(self.poll_interval)

    def _rebuild_schedule(self) -> None:
        now = datetime.now()
        loaded = storage.load_events_safe(self.events_file)
        # Always show a short feedback message when the events file is reloaded
        # so operators get confirmation even when debug is disabled.
        print(f"[CLOCK] Reloaded events file: {len(loaded)} event(s)")
        self._dbg(f"(debug) detailed reload: {len(loaded)} event(s) loaded from {self.events_file}")

        heap: list[TriggerJob] = []
        for ev in loaded:
            if not getattr(ev, "active", True):
                if self.debug:
                    self._dbg(f"Skipping inactive event '{ev.name}'")
                continue

            occ = next_weekly_occurrence(ev, now)
            if occ is not None:
                push_triggers_for_occurrence(heap, ev, occ, now)

        heapq.heapify(heap)
        self._heap = heap
        # Persist a concise snapshot of upcoming triggers so external CLI
        # processes can inspect the scheduled jobs even when running in a
        # different process (background scheduler). Write atomically.
        try:
            out = []

            now = datetime.now()
            for job in sorted(self._heap):
                out.append(
                    {
                        "due": job.due.strftime("%Y-%m-%d %H:%M:%S"),
                        "seconds_until": int((job.due - now).total_seconds()),
                        "event": job.event.name,
                        "event_id": getattr(job.event, "id", None),
                        "trigger_index": job.trigger_index,
                        "offset_min": job.trigger.timer,
                        "url": job.trigger.buttonURL,
                    }
                )

            path = Path.cwd() / "calendar_triggers.json"
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
            tmp.replace(path)
        except Exception:
            pass
        if self.debug:
            upcoming = sorted(self._heap)
            self._dbg(f"Scheduled {len(upcoming)} trigger(s)")
            for i, job in enumerate(upcoming[:20]):
                self._dbg(
                    f"#{i+1:02d} due={job.due.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"event=#{getattr(job.event,'id',None)} '{job.event.name}' | offset={job.trigger.timer}min | url='{job.trigger.buttonURL}'"
                )

    def _handle_trigger(self, job: TriggerJob) -> None:
        print(
            f"[TRIGGER] {job.due} | Event=#{getattr(job.event,'id',None)} '{job.event.name}' | "
            f"offset={job.trigger.timer}min | url='{job.trigger.buttonURL}'"
        )

        if self.c and getattr(self.c, "connected", False):
            ok = self.c.post_command(job.trigger.buttonURL)
            if ok:
                # Companion is reachable; if it was previously down, notify recovery
                if self._companion_down:
                    print(f"[COMPANION] Companion reconnected at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    logger.info("Companion reconnected")
                    self._companion_down = False

                logger.info(f"POST {job.trigger.buttonURL} OK | event=#{getattr(job.event,'id',None)} '{job.event.name}' | due={job.due}")
                self._dbg(f"Companion POST '{job.trigger.buttonURL}' -> OK")
            else:
                # POST failed: mark as down and print a short summary to stdout
                if not self._companion_down:
                    print(f"[COMPANION] Companion appears unreachable (POST failed) at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}; see calendar.log")
                    logger.warning("Companion POST failed; marking as down")
                    self._companion_down = True

                logger.error(f"POST {job.trigger.buttonURL} FAIL | event=#{getattr(job.event,'id',None)} '{job.event.name}' | due={job.due}")
                self._dbg(f"Companion POST '{job.trigger.buttonURL}' -> FAIL")
        else:
            # Companion not connected: print a short summary once and log it
            if not self._companion_down:
                print(f"[COMPANION] Companion not connected at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}; scheduled POST skipped; see calendar.log")
                logger.warning(f"Companion not connected; would POST {job.trigger.buttonURL} | event=#{getattr(job.event,'id',None)} '{job.event.name}' | due={job.due}")
                self._companion_down = True

            self._dbg("Companion not connected; skipping POST")

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            # Refresh dynamic debug/companion state each loop so runtime changes apply quickly
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
                    self._cv.wait(timeout=1.0)
                    continue

                next_job = self._heap[0]
                now = datetime.now()
                seconds = (next_job.due - now).total_seconds()

                timeout = max(0.0, min(seconds, 1.0))
                # In debug mode, emit sparse alerts for upcoming trigger times
                if self.debug and seconds > 0:
                    # If we've switched to a new next job, reset announced thresholds
                    if self._next_due is None or self._next_due != getattr(next_job, 'due', None):
                        self._next_due = getattr(next_job, 'due', None)
                        self._announced_thresholds.clear()

                    # Alert thresholds in seconds (announce once each)
                    for thr in (30, 15, 5):
                        if seconds <= thr and thr not in self._announced_thresholds:
                            print(f"[ALERT] {int(seconds)}s until next trigger at {next_job.due.strftime('%Y-%m-%d %H:%M:%S')} for #{getattr(next_job.event,'id',None)} '{next_job.event.name}'")
                            self._announced_thresholds.add(thr)
                self._cv.wait(timeout=timeout)

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

                try:
                    self._handle_trigger(job)
                except Exception as e:
                    print(f"[CLOCK] Trigger handler error: {e}")

                # After firing due jobs, if debug is enabled, announce the time until next job (single concise message)
                if self.debug:
                    with self._cv:
                        if self._heap:
                            nxt = self._heap[0]
                            secs = (nxt.due - datetime.now()).total_seconds()
                            if secs > 0:
                                print(f"[NEXT] Next trigger in {int(secs)}s at {nxt.due.strftime('%Y-%m-%d %H:%M:%S')} for '{nxt.event.name}'")
                                # Reset thresholds tracking for the newly reported next job
                                self._next_due = getattr(nxt, 'due', None)
                                self._announced_thresholds.clear()
                        else:
                            # no upcoming jobs
                            self._next_due = None
                            self._announced_thresholds.clear()
                if job.event.repeating and job.trigger_index == (len(job.event.times) - 1):
                    next_occ = next_weekly_occurrence(job.event, job.occurrence + timedelta(seconds=1))
                    if next_occ is not None:
                        with self._cv:
                            push_triggers_for_occurrence(self._heap, job.event, next_occ, datetime.now())
                            heapq.heapify(self._heap)
                            self._dbg(
                                f"Rescheduled weekly event #{getattr(job.event,'id',None)} '{job.event.name}' for {next_occ.strftime('%Y-%m-%d %H:%M:%S')}"
                            )


# Helper scheduling functions
from package.apps.calendar.models import WeekDay


def next_weekly_occurrence(event: Event, now: datetime) -> Optional[datetime]:
    base = datetime.combine(event.date, event.time)

    if not event.repeating:
        return base if base > now else None

    target_weekday = event.day.value - 1
    start_date = max(event.date, now.date())

    days_ahead = (target_weekday - start_date.weekday()) % 7
    candidate_date = start_date + timedelta(days=days_ahead)
    candidate = datetime.combine(candidate_date, event.time)

    if candidate <= now:
        candidate += timedelta(days=7)

    return candidate


def push_triggers_for_occurrence(
    heap: List[TriggerJob],
    event: Event,
    occurrence: datetime,
    now: datetime,
) -> None:
    for idx, trig in enumerate(event.times):
        due = (occurrence + timedelta(minutes=trig.timer)).replace(microsecond=0)
        if due > now:
            heapq.heappush(heap, TriggerJob(due, event, occurrence, idx, trig))
