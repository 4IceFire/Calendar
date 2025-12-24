"""Calendar app adapter exposing AppBase-compatible class.

Wraps the refactored scheduler in package.apps.calendar.scheduler so the
app can be registered without keeping code in the repository root.
"""
from threading import Thread
from typing import Dict

from package.core import AppBase, register_app
from package.apps.calendar.scheduler import ClockScheduler
from package.apps.calendar import storage, utils
import signal
import sys


class CalendarApp(AppBase):
    def __init__(self) -> None:
        self._scheduler: ClockScheduler | None = None
        self._thread: Thread | None = None

    def start(self, blocking: bool = True) -> None:
        if self._scheduler is not None:
            return
        cfg = utils.get_config()
        events_file = cfg.get("EVENTS_FILE", storage.DEFAULT_EVENTS_FILE)
        poll = float(cfg.get("poll_interval", 1.0))
        self._scheduler = ClockScheduler(events_file, poll_interval=poll, debug=utils.get_debug())

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
            self._scheduler.start()
        else:
            def run():
                try:
                    self._scheduler.start()
                except Exception:
                    pass

            self._thread = Thread(target=run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._scheduler is None:
            return
        self._scheduler.stop()
        self._scheduler = None

    def status(self) -> Dict:
        return {"running": self._thread is not None}


def _factory() -> CalendarApp:
    return CalendarApp()


# Register under the name 'calendar'
register_app("calendar", _factory)
