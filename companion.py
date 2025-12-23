"""Bitfocus Companion HTTP client (tidy version).

Small utility to check connectivity and send simple HTTP commands to Companion.
Keeps the interface minimal while adding type hints, timeouts, and basic
error handling.
"""

import os
from datetime import datetime
from typing import Optional, Any, Dict
import requests


"""
Press Button (POST): 'location/<page>/<row>/<column>/press'
Get Custom Variable (GET): 'custom-variable/<name>/value'
Change Custom Variable (POST): 'custom-variable/<name>/value?value=<value>'
"""

# Load environment variables from .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DEBUG = str(os.getenv("CALENDAR_DEBUG", "0")).lower() in {"1", "true", "yes", "on"}


class Companion:
    """Lightweight HTTP-only client for Bitfocus Companion.

    On initialization, optionally verifies a connection to the Companion HTTP
    server by performing an HTTP GET request to '/'.
    """

    def __init__(
        self,
        host: str,
        port: int = 8000,
        timeout: float = 2.0,
        verify_on_init: bool = True,
        debug: bool = DEBUG,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{host}:{port}"
        self._connected = False
        self.session = requests.Session()
        self.debug = debug

        if verify_on_init:
            self._connected = self.check_connection()

    def _dbg(self, msg: str) -> None:
        if self.debug:
            print(f"[DEBUG {datetime.now().strftime('%H:%M:%S')}] {msg}")

    @property
    def connected(self) -> bool:
        return self._connected

    def check_connection(self) -> bool:
        """Check connectivity to the Companion HTTP server (HTTP only).

        Returns True if an HTTP GET '/' returns a non-error status (< 400).
        """
        try:
            url = f"{self.base_url}/"
            self._dbg(f"GET {url}")
            resp = self.session.get(url, timeout=self.timeout)
            ok = resp.status_code < 400
            self._connected = ok
            self._dbg(f"-> {resp.status_code} {'OK' if ok else 'FAIL'}")
            return ok
        except Exception:
            self._connected = False
            self._dbg("-> connection error")
            return False
        
    def _build_api_url(self, url: str) -> str:
        """Build a full API URL from a relative path.

        Always prefixes '/api/' to the provided path, trimming leading '/'.
        """
        return f"{self.base_url}/api/{url.lstrip('/')}"

    def post_command(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> bool:
        """POST to Companion's API. Returns True if request succeeds (2xx)."""
        full_url = self._build_api_url(url)
        try:
            self._dbg(f"POST {full_url}")
            resp = self.session.post(full_url, params=params, json=json, timeout=timeout or self.timeout)
            self._dbg(f"-> {resp.status_code}")
            return 200 <= resp.status_code < 300
        except requests.RequestException:
            self._dbg("-> request error")
            return False

    def get_command(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        """GET from Companion's API. Returns response text or None on error."""
        full_url = self._build_api_url(url)
        try:
            self._dbg(f"GET {full_url}")
            resp = self.session.get(full_url, params=params, timeout=timeout or self.timeout)
            self._dbg(f"-> {resp.status_code}")
            if 200 <= resp.status_code < 300:
                return resp.text
            return None
        except requests.RequestException:
            self._dbg("-> request error")
            return None

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass