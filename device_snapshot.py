"""Thread-safe, stale-while-refresh snapshot cache for hardware integrations.

HTTP request handlers should never wait for slow or disconnected production
hardware.  ``SharedSnapshotCache`` keeps one last-known snapshot per process,
starts at most one background refresh, and applies bounded exponential backoff
after failures so a group of browsers cannot create a reconnect storm.
"""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from typing import Any


class SharedSnapshotCache:
    """Share a hardware snapshot across all request threads in one process."""

    def __init__(
        self,
        loader: Callable[[], dict[str, Any]],
        fallback: Callable[[], dict[str, Any]],
        *,
        fresh_for: float,
        retry_base: float = 0.5,
        retry_max: float = 10.0,
        thread_name: str = "device-snapshot-refresh",
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._loader = loader
        self._fallback = fallback
        self._fresh_for = max(0.0, float(fresh_for))
        self._retry_base = max(0.01, float(retry_base))
        self._retry_max = max(self._retry_base, float(retry_max))
        self._thread_name = str(thread_name or "device-snapshot-refresh")
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._updated_at = 0.0
        self._updated_wall_at: float | None = None
        self._refreshing = False
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._retry_after = 0.0

    def get(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return immediately and refresh stale data in one background thread."""
        now = self._clock()
        start_refresh = False
        with self._lock:
            has_payload = isinstance(self._payload, dict)
            age = max(0.0, now - self._updated_at) if has_payload else None
            stale = not has_payload or age is None or age > self._fresh_for
            wants_refresh = stale or bool(force_refresh)
            if wants_refresh and not self._refreshing and now >= self._retry_after:
                self._refreshing = True
                start_refresh = True
            payload = copy.deepcopy(self._payload) if has_payload else copy.deepcopy(self._fallback())
            metadata = self._metadata_locked(now=now, stale=stale)

        if start_refresh:
            threading.Thread(
                target=self.refresh_now,
                name=self._thread_name,
                daemon=True,
            ).start()
        payload.update(metadata)
        return payload

    def refresh_now(self) -> bool:
        """Refresh synchronously; intended for the cache worker and tests.

        The method still participates in the same success/failure bookkeeping.
        Calling it directly does not wait for or start a second refresh.
        """
        with self._lock:
            if not self._refreshing:
                self._refreshing = True
        try:
            payload = self._loader()
            if not isinstance(payload, dict):
                raise TypeError("snapshot loader must return a dictionary")
        except Exception as exc:
            now = self._clock()
            with self._lock:
                self._consecutive_failures += 1
                delay = min(
                    self._retry_max,
                    self._retry_base * (2 ** max(0, self._consecutive_failures - 1)),
                )
                self._retry_after = now + delay
                self._last_error = str(exc) or exc.__class__.__name__
                self._refreshing = False
            return False

        now = self._clock()
        with self._lock:
            self._payload = copy.deepcopy(payload)
            self._updated_at = now
            self._updated_wall_at = self._wall_clock()
            self._last_error = None
            self._consecutive_failures = 0
            self._retry_after = 0.0
            self._refreshing = False
        return True

    def invalidate(self, *, refresh: bool = False) -> None:
        """Mark the current payload stale, optionally scheduling one refresh."""
        with self._lock:
            if self._payload is not None:
                self._updated_at = 0.0
        if refresh:
            self.get(force_refresh=True)

    def reset(self) -> None:
        """Clear runtime state.  Used when an integration is closed or replaced."""
        with self._lock:
            self._payload = None
            self._updated_at = 0.0
            self._updated_wall_at = None
            self._refreshing = False
            self._last_error = None
            self._consecutive_failures = 0
            self._retry_after = 0.0

    def diagnostics(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            has_payload = isinstance(self._payload, dict)
            age = max(0.0, now - self._updated_at) if has_payload else None
            return self._metadata_locked(
                now=now,
                stale=not has_payload or age is None or age > self._fresh_for,
            )

    def _metadata_locked(self, *, now: float, stale: bool) -> dict[str, Any]:
        has_payload = isinstance(self._payload, dict)
        age = max(0.0, now - self._updated_at) if has_payload else None
        return {
            "stale": bool(stale),
            "refreshing": bool(self._refreshing),
            "sampledAt": self._updated_wall_at,
            "ageMs": int(round(age * 1000.0)) if age is not None else None,
            "lastError": self._last_error,
            "consecutiveFailures": int(self._consecutive_failures),
        }
