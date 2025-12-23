"""Bitfocus Companion HTTP client (tidy version).

Small utility to check connectivity and send simple HTTP commands to Companion.
Keeps the interface minimal while adding type hints, timeouts, and basic
error handling.
"""

from typing import Optional, Any, Dict

import requests


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
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{host}:{port}"
        self._connected = False
        self.session = requests.Session()

        if verify_on_init:
            self._connected = self.check_connection()

    @property
    def connected(self) -> bool:
        return self._connected

    def check_connection(self) -> bool:
        """Check connectivity to the Companion HTTP server (HTTP only).

        Returns True if an HTTP GET '/' returns a non-error status (< 400).
        """
        try:
            resp = self.session.get(f"{self.base_url}/", timeout=self.timeout)
            ok = resp.status_code < 400
            self._connected = ok
            return ok
        except Exception:
            self._connected = False
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
            resp = self.session.post(full_url, params=params, json=json, timeout=timeout or self.timeout)
            return 200 <= resp.status_code < 300
        except requests.RequestException:
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
            resp = self.session.get(full_url, params=params, timeout=timeout or self.timeout)
            if 200 <= resp.status_code < 300:
                return resp.text
            return None
        except requests.RequestException:
            return None

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass