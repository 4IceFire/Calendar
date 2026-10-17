"""ProPresenter HTTP API client.

This module provides a lightweight requests-based wrapper around ProPresenter's
HTTP API described at https://openapi.propresenter.com/.

Primary goal for this repo: timer control (get/set/start/stop/reset).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Optional, Any, Dict, Literal
import json
import time as clock

import requests


TimerOperation = Literal["start", "stop", "reset"]


@dataclass(frozen=True)
class ProPresenterConfig:
    host: str
    port: int
    timeout: float = 2.0


class ProPresentor:
    """Lightweight HTTP client for ProPresenter.

    Notes:
    - Base API paths:
      - GET /version (no /v1) for general instance info
      - /v1/* for most endpoints
    - Timer endpoints (from OpenAPI):
      - GET /v1/timers
      - GET /v1/timers/current
      - GET /v1/timers/{operation} (start/stop/reset)
      - POST /v1/timers
      - GET/PUT/DELETE /v1/timer/{id}
      - GET /v1/timer/{id}/{operation} (start/stop/reset)
      - PUT /v1/timer/{id}/{operation} (set + operation)
    """

    def __init__(
        self,
        ip: str,
        port: int,
        *,
        timeout: float = 2.0,
        verify_on_init: bool = False,
        debug: bool = False,
    ):
        self.ip = ip
        self.port = int(port)
        self.timeout = float(timeout)
        self.base_url = f"http://{ip}:{self.port}"
        self.session = requests.Session()
        self.debug = bool(debug)
        self._connected = False
        self.last_stage_message_error: str | None = None
        self.last_timer_error: str | None = None

        if verify_on_init:
            self._connected = self.check_connection()

    def _dbg(self, msg: str) -> None:
        if self.debug:
            print(f"[PP {datetime.now().strftime('%H:%M:%S')}] {msg}")

    @property
    def connected(self) -> bool:
        return self._connected

    def check_connection(self) -> bool:
        """Check if ProPresenter is reachable by calling GET /version."""
        try:
            url = f"{self.base_url}/version"
            self._dbg(f"GET {url}")
            resp = self.session.get(url, timeout=self.timeout)
            ok = 200 <= resp.status_code < 300
            self._connected = ok
            self._dbg(f"-> {resp.status_code} {'OK' if ok else 'FAIL'}")
            return ok
        except Exception:
            self._connected = False
            self._dbg("-> connection error")
            return False

    def _build_api_url(self, url: str) -> str:
        return f"{self.base_url}/v1/{url.lstrip('/')}"

    def get_command(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        full_url = self._build_api_url(url)
        try:
            self._dbg(f"GET {full_url}")
            resp = self.session.get(full_url, params=params, timeout=timeout or self.timeout)
            self._dbg(f"-> {resp.status_code}")
            if 200 <= resp.status_code < 300:
                return resp.text
            return None
        except requests.RequestException:
            return None

    def get_json(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[Any]:
        full_url = self._build_api_url(url)
        try:
            self._dbg(f"GET {full_url}")
            resp = self.session.get(full_url, params=params, timeout=timeout or self.timeout)
            self._dbg(f"-> {resp.status_code}")
            if 200 <= resp.status_code < 300:
                return resp.json()
            return None
        except Exception:
            return None

    def post_command(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> bool:
        """POST to the ProPresenter API. Use `json=` to send a JSON body."""
        full_url = self._build_api_url(url)
        try:
            self._dbg(f"POST {full_url}")
            resp = self.session.post(full_url, params=params, json=json, timeout=timeout or self.timeout)
            self._dbg(f"-> {resp.status_code}")
            return 200 <= resp.status_code < 300
        except requests.RequestException:
            return False

    def put_command(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> bool:
        """PUT to the ProPresenter API. Use `json=` to send a JSON body."""
        full_url = self._build_api_url(url)
        try:
            self._dbg(f"PUT {full_url}")
            resp = self.session.put(full_url, params=params, json=json, timeout=timeout or self.timeout)
            self._dbg(f"-> {resp.status_code}")
            return 200 <= resp.status_code < 300
        except requests.RequestException:
            return False

    def delete_command(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> bool:
        full_url = self._build_api_url(url)
        try:
            self._dbg(f"DELETE {full_url}")
            resp = self.session.delete(full_url, params=params, timeout=timeout or self.timeout)
            self._dbg(f"-> {resp.status_code}")
            return 200 <= resp.status_code < 300
        except requests.RequestException:
            return False

    # --- Timer endpoints (OpenAPI) ---

    def get_timers(self) -> Optional[Any]:
        return self.get_json("timers")

    def get_timers_current(self) -> Optional[Any]:
        return self.get_json("timers/current")

    def timers_operation(self, operation: TimerOperation) -> bool:
        # Some ProPresenter operations return 204 No Content (empty body).
        # Treat any 2xx as success.
        try:
            url = self._build_api_url(f"timers/{operation}")
            self._dbg(f"GET {url}")
            resp = self.session.get(url, timeout=self.timeout)
            self._dbg(f"-> {resp.status_code}")
            return 200 <= resp.status_code < 300
        except Exception:
            return False

    def create_timer(self, payload: dict) -> bool:
        return self.post_command("timers", json=payload)

    def get_timer(self, timer_id: int | str) -> Optional[Any]:
        return self.get_json(f"timer/{timer_id}")

    def set_timer(self, timer_id: int | str, payload: dict) -> bool:
        return self.put_command(f"timer/{timer_id}", json=payload)

    def delete_timer(self, timer_id: int | str) -> bool:
        return self.delete_command(f"timer/{timer_id}")

    def _timer_request(self, method: str, path: str, **kwargs):
        """One bounded request; never retry an uncertain device command."""
        self.last_timer_error = None
        try:
            timeout = kwargs.pop('timeout', self.timeout)
            response = self.session.request(method, self._build_api_url(path), timeout=timeout, **kwargs)
            if 200 <= response.status_code < 300:
                return response
            self.last_timer_error = f"{method} /v1/{path}: HTTP {response.status_code}"
        except requests.RequestException as exc:
            self.last_timer_error = f"{method} /v1/{path}: {type(exc).__name__}"
        return None

    def timer_operation(self, timer_id: int | str, operation: TimerOperation) -> bool:
        return self._timer_request('GET', f'timer/{timer_id}/{operation}') is not None

    def set_timer_and_operation(self, timer_id: int | str, operation: TimerOperation, payload: dict) -> bool:
        return self.put_command(f"timer/{timer_id}/{operation}", json=payload)

    # --- Stage display message endpoints (OpenAPI) ---

    def set_stage_message(self, message: str) -> bool:
        """Display a stage message (text overlay) on ProPresenter stage display."""
        self.last_stage_message_error = None
        try:
            msg = str(message or '').strip()
        except Exception:
            msg = ''
        if not msg:
            self.last_stage_message_error = 'empty message'
            return False
        url = self._build_api_url("stage/message")
        # ProPresenter expects a JSON string body for v1/stage/message.
        try:
            payload = json.dumps(msg)
            self._dbg(f"PUT {url} (json string)")
            resp = self.session.put(
                url,
                data=payload.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            self._dbg(f"-> {resp.status_code}")
            if 200 <= resp.status_code < 300:
                return True
            try:
                detail = resp.text.strip()
            except Exception:
                detail = ''
            self.last_stage_message_error = f"PUT json string -> {resp.status_code} {detail}".strip()
            return False
        except Exception:
            self.last_stage_message_error = "PUT json string -> request error"
            return False

    def clear_stage_message(self) -> bool:
        """Clear the current stage message."""
        return self.delete_command("stage/message")
        
    def SetCountdownToTime(self, index, timeStr) -> bool:
        time = datetime.strptime(timeStr, "%H:%M")

        response = self._timer_request('GET', f'timer/{index}')
        if response is None:
            return False
        try:
            identity = (response.json() or {}).get('id', {})
            if not isinstance(identity, dict) or not all(key in identity for key in ('uuid', 'name', 'index')):
                self.last_timer_error = f"GET /v1/timer/{index}: missing timer identity"
                return False
        except (ValueError, TypeError, AttributeError):
            self.last_timer_error = f"GET /v1/timer/{index}: invalid JSON timer response"
            return False

        time_of_day = ""
        seconds = 0
        
        hour = time.hour
        minute = time.minute 

        if time.hour < 12 and time.hour >= 0:
            time_of_day = "am"
            seconds = ((hour * 60) + minute)  * 60
        elif(time.hour >= 12 and time.hour <= 24):
            time_of_day = "pm"
            seconds = (((hour - 12) * 60) + minute)  * 60
        else:
            return False

        data = {
        "id": dict(identity),
        "allows_overrun": True,
        "count_down_to_time": {
            "time_of_day": seconds,
            "period": time_of_day
        }
        }

        if self._timer_request("PUT", f"timer/{index}", json=data) is None:
            return False

        # macOS can acknowledge an edit before publishing the new timer model.
        # Resetting immediately can restore the old model and lose that edit.
        # Confirm the write with bounded reads, never another PUT or a replay.
        deadline = clock.monotonic() + self.timeout
        while True:
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                self.last_timer_error = f"GET /v1/timer/{index}: timer update not confirmed before timeout"
                return False
            response = self._timer_request('GET', f'timer/{index}', timeout=remaining)
            if response is None:
                return False
            try:
                actual = response.json()
                if clock.monotonic() >= deadline:
                    self.last_timer_error = f"GET /v1/timer/{index}: timer update not confirmed before timeout"
                    return False
                if not isinstance(actual, dict) or not isinstance(actual.get('id'), dict):
                    raise ValueError('invalid timer model')
                if actual['id'].get('uuid') != identity['uuid']:
                    self.last_timer_error = f"GET /v1/timer/{index}: timer identity changed during update"
                    return False
                if actual.get('count_down_to_time') == data['count_down_to_time']:
                    return True
            except (ValueError, TypeError, AttributeError):
                self.last_timer_error = f"GET /v1/timer/{index}: invalid JSON timer response"
                return False
            clock.sleep(min(0.05, max(0.0, deadline - clock.monotonic())))

    def SetTimer(self, index: int, t: dt_time) -> bool:
        """Compatibility helper used by the web UI.

        Sets timer {index} to count down to the specified time-of-day.
        """
        try:
            return self.SetCountdownToTime(index, t.strftime("%H:%M"))
        except Exception:
            return False


# Optional alias with correct spelling for future code.
ProPresenter = ProPresentor

