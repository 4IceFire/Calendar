"""Calendar app adapter exposing AppBase-compatible class.

Wraps the refactored scheduler in package.apps.calendar.scheduler so the
app can be registered without keeping code in the repository root.
"""
from threading import Thread, current_thread
from typing import Callable, Dict
import logging

from package.core import AppBase, register_app
from package.apps.calendar.scheduler import ClockScheduler
from package.apps.calendar import storage, utils
import signal
import sys


class CalendarApp(AppBase):
    def __init__(self) -> None:
        self._scheduler: ClockScheduler | None = None
        self._thread: Thread | None = None
        self._last_error: str | None = None
        self._internal_action_executor: Callable | None = None

    def set_internal_action_executor(self, executor: Callable | None) -> None:
        self._internal_action_executor = executor
        if self._scheduler is not None:
            self._scheduler.set_internal_action_executor(executor)

    def start(self, blocking: bool = True) -> None:
        if self._scheduler is not None:
            if self._thread is not None and self._thread.is_alive():
                return
            try:
                if bool(self._scheduler.status().get("running", False)):
                    return
            except Exception:
                pass
        cfg = utils.get_config()
        self._last_error = None
        events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
        poll = float(cfg.get("poll_interval", 1.0))
        self._scheduler = ClockScheduler(
            events_file,
            poll_interval=poll,
            debug=utils.get_debug(),
            internal_action_executor=self._internal_action_executor,
        )

        # Register signal handlers so the process can be stopped gracefully
        def _handle_term(signum, frame):
            try:
                self.stop()
            except Exception:
                pass
            # ensure process exits
            try:
                sys.exit(0)
            except Exception:
                pass

        try:
            signal.signal(signal.SIGINT, _handle_term)
        except Exception:
            pass
        try:
            signal.signal(signal.SIGTERM, _handle_term)
        except Exception:
            pass

        if blocking:
            try:
                self._scheduler.start()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise
        else:
            scheduler = self._scheduler

            def run():
                try:
                    scheduler.start()
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logging.getLogger("calendar").exception("Calendar scheduler background worker exited")

            self._thread = Thread(target=run, name="tdeck-calendar-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._scheduler is None:
            return
        scheduler = self._scheduler
        scheduler.stop()
        thread = self._thread
        if thread is not None and thread is not current_thread():
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass
        self._scheduler = None
        self._thread = None

    def status(self) -> Dict:
        scheduler_status: dict = {}
        if self._scheduler is not None:
            try:
                scheduler_status = dict(self._scheduler.status() or {})
            except Exception as exc:
                self._last_error = f"Status {type(exc).__name__}: {exc}"

        thread_alive = bool(self._thread and self._thread.is_alive())
        scheduler_running = bool(scheduler_status.get("running", False))
        # Background mode requires both the scheduler state and its owning
        # thread to be alive. In blocking CLI mode there is no wrapper thread.
        running = scheduler_running and (thread_alive if self._thread is not None else True)
        scheduler_status.update(
            {
                "running": running,
                "thread_alive": thread_alive,
                "last_error": self._last_error or scheduler_status.get("last_error"),
            }
        )
        return scheduler_status


def _factory() -> CalendarApp:
    return CalendarApp()


# Register under the name 'calendar'
register_app("calendar", _factory)
