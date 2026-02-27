import heapq
import threading
import time as t
from datetime import datetime, timedelta
from typing import List, Optional
import json
from pathlib import Path
import requests

from package.apps.calendar.models import Event, TriggerJob
from package.apps.calendar import storage, utils
logger = utils.get_logger()

_button_templates_cache: dict = {"mtime": None, "labels_by_url": {}}


def _read_button_templates_any() -> list[dict]:
    """Read button_templates.json in either legacy list or tree format."""
    path = Path.cwd() / "button_templates.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "[]")
    except Exception:
        return []

    if isinstance(raw, dict):
        templates = raw.get("templates")
        return templates if isinstance(templates, list) else []
    if isinstance(raw, list):
        return raw
    return []


def _button_template_effective_url(tpl: dict) -> str:
    if not isinstance(tpl, dict):
        return ""
    url = str(tpl.get("buttonURL") or "").strip()
    if url:
        return url
    pattern = str(tpl.get("pattern") or "").strip()
    if pattern:
        return f"location/{pattern}/press"
    return ""


def _get_button_template_labels_by_url() -> dict[str, str]:
    path = Path.cwd() / "button_templates.json"
    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = None

    if mtime is not None and _button_templates_cache.get("mtime") == mtime:
        try:
            return dict(_button_templates_cache.get("labels_by_url") or {})
        except Exception:
            return {}

    labels: dict[str, str] = {}
    try:
        for tpl in _read_button_templates_any():
            if not isinstance(tpl, dict):
                continue
            url = _button_template_effective_url(tpl)
            if not url:
                continue
            label = str(tpl.get("label") or "").strip()
            if label:
                labels[url] = label
    except Exception:
        labels = {}

    _button_templates_cache["mtime"] = mtime
    _button_templates_cache["labels_by_url"] = labels
    return dict(labels)


def _resolve_trigger_display_name(trigger) -> str:
    try:
        n = str(getattr(trigger, "name", "") or "").strip()
    except Exception:
        n = ""
    if n:
        return n

    action_type = str(getattr(trigger, "actionType", "companion") or "companion").lower()
    if action_type == "api":
        api = getattr(trigger, "api", None)
        if isinstance(api, dict):
            return str(api.get("path") or "").strip()
        return "API"

    url = str(getattr(trigger, "buttonURL", "") or "").strip()
    labels = _get_button_template_labels_by_url()
    return str(labels.get(url) or url or "").strip()


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

        # Startup efficiency: the scheduler does one intentional initial load
        # (via `_reload_needed = True`). Seed the mtimes BEFORE the watcher
        # starts so the first watcher iteration doesn't also flag both files
        # as "changed" and cause extra reloads.
        try:
            self._last_mtime = __import__("os").path.getmtime(self.events_file)
        except FileNotFoundError:
            self._last_mtime = None
        except Exception:
            # Best-effort; leave as-is
            pass

        try:
            self._last_config_mtime = __import__("os").path.getmtime(utils.CONFIG_FILE)
        except FileNotFoundError:
            self._last_config_mtime = None
        except Exception:
            pass

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
                # Best-effort reload. Note: in this process, config.json may
                # have already been reloaded elsewhere (e.g., web UI save), in
                # which case reload_config() can return False even though the
                # file changed. We still must react to the new config values.
                try:
                    utils.reload_config()
                except Exception:
                    pass

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
                action_type = str(getattr(job.trigger, "actionType", "companion") or "companion").lower()
                api = getattr(job.trigger, "api", None)
                name = _resolve_trigger_display_name(job.trigger)
                out.append(
                    {
                        "due": job.due.strftime("%Y-%m-%d %H:%M:%S"),
                        "seconds_until": int((job.due - now).total_seconds()),
                        "event": job.event.name,
                        "event_id": getattr(job.event, "id", None),
                        "trigger_index": job.trigger_index,
                        "offset_min": job.trigger.timer,
                        "name": name,
                        "actionType": action_type,
                        "url": job.trigger.buttonURL,
                        "api": api if isinstance(api, dict) else None,
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
                action_type = str(getattr(job.trigger, "actionType", "companion") or "companion").lower()
                api = getattr(job.trigger, "api", None) if action_type == "api" else None
                api_desc = ""
                if isinstance(api, dict):
                    api_desc = f" | api={str(api.get('method') or '').upper()} {api.get('path') or ''}"
                self._dbg(
                    f"#{i+1:02d} due={job.due.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"event=#{getattr(job.event,'id',None)} '{job.event.name}' | offset={job.trigger.timer}min | url='{job.trigger.buttonURL}'"
                    + (api_desc if action_type == "api" else "")
                )

    def _execute_internal_api_action(self, api: dict, job: TriggerJob | None = None) -> bool:
        try:
            cfg = utils.get_config() or {}
        except Exception:
            cfg = {}

        try:
            port = int(cfg.get("webserver_port", 5000))
        except Exception:
            port = 5000

        # Internal API calls may legitimately take a few seconds (e.g. ProPresenter
        # legacy timer sequences include waits). Allow a configurable timeout.
        try:
            timeout_s = float(
                cfg.get(
                    "internal_api_timeout_seconds",
                    cfg.get("scheduler_internal_api_timeout_seconds", 10.0),
                )
            )
        except Exception:
            timeout_s = 10.0
        timeout_s = max(1.0, min(timeout_s, 60.0))

        method = str(api.get("method") or "POST").strip().upper()
        path = str(api.get("path") or "").strip()
        body = api.get("body")

        # If the request body is an object, inject event context so endpoints
        # can resolve values relative to the event start time.
        if isinstance(body, dict) and job is not None:
            try:
                injected = dict(body)
                injected.setdefault('event_start', getattr(job, 'occurrence', None).isoformat() if getattr(job, 'occurrence', None) else None)
                injected.setdefault('event_due', getattr(job, 'due', None).isoformat() if getattr(job, 'due', None) else None)
                injected.setdefault('event_id', getattr(getattr(job, 'event', None), 'id', None))
                injected.setdefault('event_name', getattr(getattr(job, 'event', None), 'name', None))
                body = injected
            except Exception:
                pass

        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return False

        # Guardrails: only allow internal API calls to our own /api/* routes.
        if '://' in path:
            return False

        if path.startswith('/api/'):
            path_norm = path
        elif path.startswith('/'):
            path_norm = '/api' + path
        else:
            path_norm = '/api/' + path.lstrip('/')

        if not path_norm.startswith('/api/'):
            return False

        url = f"http://127.0.0.1:{port}{path_norm}"

        try:
            if method == "GET":
                resp = requests.request(method, url, timeout=timeout_s)
            else:
                resp = requests.request(method, url, json=body if body is not None else None, timeout=timeout_s)

            ok = 200 <= int(resp.status_code) < 300
            if not ok and self.debug:
                try:
                    snippet = (resp.text or '').strip().replace('\n', ' ')
                    if len(snippet) > 300:
                        snippet = snippet[:300] + '...'
                    self._dbg(f"Internal API non-2xx: {resp.status_code} {method} {path_norm} resp='{snippet}'")
                except Exception:
                    pass
            return ok
        except Exception as e:
            if self.debug:
                try:
                    self._dbg(f"Internal API exception: {method} {path_norm} err={e}")
                except Exception:
                    pass
            return False

    def _handle_trigger(self, job: TriggerJob) -> None:
        action_type = str(getattr(job.trigger, "actionType", "companion") or "companion").lower()
        name = _resolve_trigger_display_name(job.trigger)
        if action_type == "api":
            api = getattr(job.trigger, "api", None)
            m = str((api or {}).get("method") or "POST").upper() if isinstance(api, dict) else "POST"
            p = str((api or {}).get("path") or "") if isinstance(api, dict) else ""
            print(
                f"[TRIGGER] {job.due} | Event=#{getattr(job.event,'id',None)} '{job.event.name}' | "
                f"name='{name}' | offset={job.trigger.timer}min | api={m} {p}"
            )
        else:
            print(
                f"[TRIGGER] {job.due} | Event=#{getattr(job.event,'id',None)} '{job.event.name}' | "
                f"name='{name}' | offset={job.trigger.timer}min | url='{job.trigger.buttonURL}'"
            )

        if action_type == "api":
            api = getattr(job.trigger, "api", None)
            ok = self._execute_internal_api_action(api if isinstance(api, dict) else {}, job)
            if ok:
                logger.info(
                    f"API {str((api or {}).get('method') or 'POST').upper()} {str((api or {}).get('path') or '')} OK | "
                    f"event=#{getattr(job.event,'id',None)} '{job.event.name}' | due={job.due}"
                )
                self._dbg("Internal API action -> OK")
            else:
                print(f"[ACTION] Internal API action failed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}; see calendar.log")
                logger.error(
                    f"API {str((api or {}).get('method') or 'POST').upper()} {str((api or {}).get('path') or '')} FAIL | "
                    f"event=#{getattr(job.event,'id',None)} '{job.event.name}' | due={job.due}"
                )
                self._dbg("Internal API action -> FAIL")
            return

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
                if job.event.repeating:
                    # Reschedule only after all triggers for the current occurrence have fired.
                    still_pending = False
                    with self._cv:
                        for j in self._heap:
                            try:
                                if j.event is job.event and j.occurrence == job.occurrence:
                                    still_pending = True
                                    break
                            except Exception:
                                continue
                    if not still_pending:
                        next_occ = next_weekly_occurrence(job.event, job.occurrence + timedelta(seconds=1))
                        if next_occ is not None:
                            with self._cv:
                                push_triggers_for_occurrence(self._heap, job.event, next_occ, datetime.now())
                                heapq.heapify(self._heap)
                                self._dbg(
                                    f"Rescheduled weekly event #{getattr(job.event,'id',None)} '{job.event.name}' for {next_occ.strftime('%Y-%m-%d %H:%M:%S')}"
                                )


# Helper scheduling functions


def next_weekly_occurrence(event: Event, now: datetime) -> Optional[datetime]:
    def _has_future_trigger(occurrence: datetime) -> bool:
        # A trigger is still pending if its computed due time is in the future.
        for trig in getattr(event, "times", []):
            try:
                if not bool(getattr(trig, "enabled", True)):
                    continue
            except Exception:
                pass
            due = (occurrence + timedelta(minutes=trig.timer)).replace(microsecond=0)
            if due > now:
                return True
        return False

    base = datetime.combine(event.date, event.time)

    if not event.repeating:
        # Non-repeating events can still have AFTER triggers pending even if the
        # base event time is already in the past.
        if base > now:
            return base
        return base if _has_future_trigger(base) else None

    # Repeating weekly: prefer the most recent occurrence (including today)
    # if it still has future trigger(s) pending; otherwise schedule the next.
    target_weekday = event.day.value - 1

    # First possible occurrence on/after the configured start date.
    first_date = event.date + timedelta(days=(target_weekday - event.date.weekday()) % 7)
    first_occ = datetime.combine(first_date, event.time)

    # Most recent occurrence on/before today.
    today = now.date()
    days_back = (today.weekday() - target_weekday) % 7
    last_date = today - timedelta(days=days_back)
    if last_date < first_date:
        last_date = first_date
    last_occ = datetime.combine(last_date, event.time)

    if last_occ > now:
        return last_occ

    if _has_future_trigger(last_occ):
        return last_occ

    return last_occ + timedelta(days=7)


def push_triggers_for_occurrence(
    heap: List[TriggerJob],
    event: Event,
    occurrence: datetime,
    now: datetime,
) -> None:
    for idx, trig in enumerate(event.times):
        try:
            if not bool(getattr(trig, "enabled", True)):
                continue
        except Exception:
            pass
        due = (occurrence + timedelta(minutes=trig.timer)).replace(microsecond=0)
        if due > now:
            heapq.heappush(heap, TriggerJob(due, event, occurrence, idx, trig))


