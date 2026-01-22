"""Blackmagic Design VideoHub TCP client.

This module mirrors the style of `propresentor.py` and `companion.py` so the rest
of the repo can import a single, top-level client:

- VideoHub default port: 9990
- Protocol: line-oriented command blocks terminated by a blank line
- Indexing: VideoHub uses 0-based input/output indexes

For this repo's use-cases, we keep the client intentionally lightweight:
- open a TCP connection
- send a single command block
- close

That is sufficient for routing triggers initiated by the CLI or Web UI.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Any


DEFAULT_PORT = 9990


@dataclass(frozen=True)
class VideohubConfig:
    host: str
    port: int = DEFAULT_PORT
    timeout: float = 2.0


def get_videohub_client_from_config(
    cfg: dict,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    timeout: Optional[float] = None,
    debug: bool = False,
) -> Optional["VideohubClient"]:
    """Build a `VideohubClient` from a loaded config dict.

    Reads keys:
    - videohub_ip or videohub_host
    - videohub_port (optional)
    - videohub_timeout (optional)

    Optional overrides can be supplied via args.
    """

    if cfg is None:
        cfg = {}

    host_value = (host or cfg.get("videohub_ip") or cfg.get("videohub_host") or "").strip()
    if not host_value:
        return None

    try:
        port_value = int(port or cfg.get("videohub_port") or DEFAULT_PORT)
    except Exception:
        port_value = DEFAULT_PORT

    try:
        timeout_value = float(timeout if timeout is not None else cfg.get("videohub_timeout", 2.0))
    except Exception:
        timeout_value = 2.0

    return VideohubClient(host_value, port_value, timeout=timeout_value, debug=debug)


class VideohubClient:
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        timeout: float = 2.0,
        verify_on_init: bool = False,
        debug: bool = False,
    ) -> None:
        self.host = str(host).strip()
        self.port = int(port)
        self.timeout = float(timeout)
        self.debug = bool(debug)
        self._connected = False

        if verify_on_init:
            self._connected = self.check_connection()

    def _dbg(self, msg: str) -> None:
        if self.debug:
            print(f"[VH {datetime.now().strftime('%H:%M:%S')}] {msg}")

    @property
    def connected(self) -> bool:
        return self._connected

    def check_connection(self) -> bool:
        """Best-effort reachability check using PING."""
        ok = self.ping()
        self._connected = ok
        return ok

    def _send(self, payload: str, *, read_response: bool = False) -> Optional[str]:
        if not self.host:
            raise ValueError("VideoHub host is required")

        data = payload.encode("utf-8", errors="replace")

        self._dbg(f"TCP {self.host}:{self.port} send {len(data)} bytes")
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(data)

            if not read_response:
                return None

            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                return ""
            return chunk.decode("utf-8", errors="replace")

    def _recv_initial_state(self, *, max_bytes: int = 512 * 1024) -> str:
        """Read the initial state dump that VideoHub sends upon connection.

        Many VideoHub devices push their full state/labels immediately after a TCP
        client connects. We read until a socket timeout occurs.
        """

        if not self.host:
            raise ValueError("VideoHub host is required")

        buf = bytearray()
        self._dbg(f"TCP {self.host}:{self.port} recv initial state")
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            while len(buf) < max_bytes:
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
        try:
            return bytes(buf).decode("utf-8", errors="replace")
        except Exception:
            return ""

    @staticmethod
    def _parse_label_block(text: str, header: str) -> dict[int, str]:
        """Parse a label block like INPUT LABELS / OUTPUT LABELS.

        Returns a map of 0-based index -> label.
        """

        if not text:
            return {}

        # Blocks look like:
        # INPUT LABELS:
        # 0 Camera 1
        # 1 Camera 2
        #
        # (blank line ends the block)
        try:
            token = f"{header}:"
            start = text.find(token)
            if start < 0:
                return {}
            block = text[start + len(token):]
            # Trim leading newlines
            block = block.lstrip("\r\n")
            # Take until first blank line
            m = None
            import re
            m = re.search(r"\r?\n\r?\n", block)
            if m:
                block = block[:m.start()]
            lines = [ln.strip() for ln in block.splitlines()]
        except Exception:
            return {}

        out: dict[int, str] = {}
        for ln in lines:
            if not ln:
                continue
            # Split at first whitespace: "0 Name..."
            try:
                idx_str, label = ln.split(None, 1)
            except ValueError:
                # Could be just an index with no label
                idx_str, label = ln, ""
            try:
                idx = int(idx_str)
            except Exception:
                continue
            out[idx] = str(label or "").strip()
        return out

    @staticmethod
    def _parse_routing_block(text: str, header: str) -> dict[int, int]:
        """Parse a routing block like VIDEO OUTPUT ROUTING.

        Returns a map of 0-based output index -> 0-based input index.
        """

        if not text:
            return {}

        try:
            token = f"{header}:"
            start = text.find(token)
            if start < 0:
                return {}
            block = text[start + len(token):]
            block = block.lstrip("\r\n")
            import re
            m = re.search(r"\r?\n\r?\n", block)
            if m:
                block = block[:m.start()]
            lines = [ln.strip() for ln in block.splitlines()]
        except Exception:
            return {}

        out: dict[int, int] = {}
        for ln in lines:
            if not ln:
                continue
            # Lines look like: "0 4" => output 0 routes to input 4
            try:
                out_str, in_str = ln.split(None, 1)
            except ValueError:
                continue
            try:
                out_idx = int(out_str)
                in_idx = int(in_str)
            except Exception:
                continue
            if out_idx < 0 or in_idx < 0:
                continue
            out[out_idx] = in_idx
        return out

    def get_labels(self, *, fallback_count: int = 40) -> dict[str, list[dict[str, Any]]]:
        """Fetch input/output labels from the device.

        Returns 1-based `number` entries for UI convenience.
        If device labels aren't available, falls back to a numeric-only list.
        """

        state = ""
        try:
            state = self._recv_initial_state()
        except Exception:
            state = ""

        inputs0 = self._parse_label_block(state, "INPUT LABELS")
        outputs0 = self._parse_label_block(state, "OUTPUT LABELS")

        def _to_list(m: dict[int, str]) -> list[dict[str, Any]]:
            if not m:
                return [{"number": i, "label": ""} for i in range(1, int(fallback_count) + 1)]
            max_idx = max(m.keys()) if m else -1
            n = max(max_idx + 1, int(fallback_count))
            out_list: list[dict[str, Any]] = []
            for i0 in range(0, n):
                out_list.append({"number": i0 + 1, "label": m.get(i0, "")})
            return out_list

        return {
            "inputs": _to_list(inputs0),
            "outputs": _to_list(outputs0),
        }

    def get_state(self, *, fallback_count: int = 40) -> dict[str, Any]:
        """Fetch labels and current routing snapshot from the device.

        Returns:
          - inputs: [{number,label}] (1-based)
          - outputs: [{number,label}] (1-based)
          - routing: [input_number,...] where index 0 corresponds to output #1

        If routing isn't available, defaults to an identity-style mapping.
        """

        state = ""
        try:
            state = self._recv_initial_state()
        except Exception:
            state = ""

        inputs0 = self._parse_label_block(state, "INPUT LABELS")
        outputs0 = self._parse_label_block(state, "OUTPUT LABELS")
        routing0 = self._parse_routing_block(state, "VIDEO OUTPUT ROUTING")

        def _to_list(m: dict[int, str]) -> list[dict[str, Any]]:
            if not m:
                return [{"number": i, "label": ""} for i in range(1, int(fallback_count) + 1)]
            max_idx = max(m.keys()) if m else -1
            n = max(max_idx + 1, int(fallback_count))
            out_list: list[dict[str, Any]] = []
            for i0 in range(0, n):
                out_list.append({"number": i0 + 1, "label": m.get(i0, "")})
            return out_list

        inputs = _to_list(inputs0)
        outputs = _to_list(outputs0)
        n = max(
            int(fallback_count),
            len(inputs),
            len(outputs),
            (max(routing0.keys()) + 1) if routing0 else 0,
        )

        # Default to identity mapping where possible.
        routing: list[int] = []
        for out0 in range(0, n):
            in0 = routing0.get(out0, out0)
            # Clamp into [0, n-1] as a best-effort fallback.
            if in0 < 0:
                in0 = 0
            if in0 >= n:
                in0 = 0
            routing.append(in0 + 1)

        return {
            "inputs": inputs,
            "outputs": outputs,
            "routing": routing,
        }

    def ping(self) -> bool:
        try:
            # Protocol uses a blank line to terminate command blocks.
            self._send("PING:\n\n", read_response=False)
            self._connected = True
            return True
        except OSError as e:
            self._dbg(f"ping failed: {e}")
            self._connected = False
            return False

    def route_video_output(self, *, output: int, input_: int, monitoring: bool = False) -> None:
        """Route `input_` to `output`.

        Parameters are 0-based indexes (matching the VideoHub protocol).
        """

        if output < 0 or input_ < 0:
            raise ValueError("output and input_ must be >= 0")

        header = "VIDEO MONITORING OUTPUT ROUTING" if monitoring else "VIDEO OUTPUT ROUTING"
        cmd = f"{header}:\n{output} {input_}\n\n"
        self._send(cmd, read_response=False)

    def route_video_outputs(self, *, routes: list[tuple[int, int]], monitoring: bool = False) -> None:
        """Route many outputs in a single request.

        `routes` is a list of (output_idx, input_idx) pairs using 0-based indexes.
        """

        if not routes:
            return

        lines: list[str] = []
        for output, input_ in routes:
            if output < 0 or input_ < 0:
                raise ValueError("output and input_ must be >= 0")
            lines.append(f"{int(output)} {int(input_)}")

        header = "VIDEO MONITORING OUTPUT ROUTING" if monitoring else "VIDEO OUTPUT ROUTING"
        cmd = f"{header}:\n" + "\n".join(lines) + "\n\n"
        self._send(cmd, read_response=False)

    # Minor convenience for future expansion (labels/state parsing)
    def send_raw(self, payload: str, *, read_response: bool = False) -> Optional[str]:
        """Send a raw protocol block. Caller must include final blank line."""
        return self._send(payload, read_response=read_response)
