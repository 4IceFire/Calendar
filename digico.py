"""DiGiCo SD/Quantum OSC transport and personal-mixer state service.

The DiGiCo remote-control connection is OSC over UDP, with one important
compatibility wrinkle: the console and iPad app can emit OSC messages without
the type-tag string required by the OSC 1.0 specification.  The codec in this
module deliberately accepts those packets, matching the behaviour used by
OSCWebMixer2.

Only this backend talks to the console.  Browser clients use TDeck's HTTP API,
so any number of personal-mix pages can share the console's single iPad-style
connection.
"""

from __future__ import annotations

import colorsys
import json
import logging
import math
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable


LOGGER = logging.getLogger(__name__)

DEFAULT_DESK_PORT = 9000
DEFAULT_LISTEN_PORT = 8000


class OscCodecError(ValueError):
    """Raised when an OSC packet cannot be decoded safely."""


def _osc_pad(raw: bytes) -> bytes:
    return raw + (b"\x00" * ((4 - (len(raw) % 4)) % 4))


def _osc_string(value: str) -> bytes:
    return _osc_pad(str(value).encode("utf-8") + b"\x00")


def _read_osc_string(packet: bytes, offset: int) -> tuple[str, int]:
    if offset < 0 or offset >= len(packet):
        raise OscCodecError("OSC string offset is outside the packet")
    end = packet.find(b"\x00", offset)
    if end < 0:
        raise OscCodecError("OSC string is not null terminated")
    try:
        value = packet[offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OscCodecError("OSC string is not valid UTF-8") from exc
    next_offset = int(math.ceil((end + 1) / 4.0) * 4)
    if next_offset > len(packet):
        raise OscCodecError("OSC string padding exceeds packet length")
    return value, next_offset


def encode_osc_message(address: str, args: Iterable[Any] | None = None) -> bytes:
    """Encode a small, standards-compliant OSC message.

    DiGiCo control values used by TDeck are strings, integers, floats and
    booleans.  Empty query messages include an empty type-tag string (",").
    """

    address = str(address or "").strip()
    if not address.startswith("/"):
        raise OscCodecError("OSC address must begin with '/'")

    tags: list[str] = []
    payload: list[bytes] = []
    for arg in list(args or []):
        if isinstance(arg, bool):
            tags.append("T" if arg else "F")
        elif arg is None:
            tags.append("N")
        elif isinstance(arg, int):
            if -(2**31) <= arg <= (2**31 - 1):
                tags.append("i")
                payload.append(struct.pack(">i", int(arg)))
            else:
                tags.append("h")
                payload.append(struct.pack(">q", int(arg)))
        elif isinstance(arg, float):
            tags.append("f")
            payload.append(struct.pack(">f", float(arg)))
        elif isinstance(arg, (bytes, bytearray, memoryview)):
            blob = bytes(arg)
            tags.append("b")
            payload.append(struct.pack(">i", len(blob)) + _osc_pad(blob))
        else:
            tags.append("s")
            payload.append(_osc_string(str(arg)))

    return _osc_string(address) + _osc_string("," + "".join(tags)) + b"".join(payload)


def _decode_osc_message(packet: bytes) -> tuple[str, list[Any]]:
    address, offset = _read_osc_string(packet, 0)
    if not address.startswith("/"):
        raise OscCodecError("OSC message address must begin with '/'")
    if offset >= len(packet):
        # DiGiCo sends valid address-only packets without a type-tag string.
        return address, []

    type_tags, offset = _read_osc_string(packet, offset)
    if not type_tags.startswith(","):
        # Match the DiGiCo-tolerant osc.js fork: unknown/missing type tags mean
        # arguments cannot be unpacked, but the message itself remains useful.
        return address, []

    args: list[Any] = []
    for tag in type_tags[1:]:
        if tag == "i":
            if offset + 4 > len(packet):
                raise OscCodecError("OSC int32 exceeds packet length")
            args.append(struct.unpack_from(">i", packet, offset)[0])
            offset += 4
        elif tag == "f":
            if offset + 4 > len(packet):
                raise OscCodecError("OSC float32 exceeds packet length")
            args.append(struct.unpack_from(">f", packet, offset)[0])
            offset += 4
        elif tag == "h":
            if offset + 8 > len(packet):
                raise OscCodecError("OSC int64 exceeds packet length")
            args.append(struct.unpack_from(">q", packet, offset)[0])
            offset += 8
        elif tag == "d":
            if offset + 8 > len(packet):
                raise OscCodecError("OSC float64 exceeds packet length")
            args.append(struct.unpack_from(">d", packet, offset)[0])
            offset += 8
        elif tag in ("s", "S"):
            value, offset = _read_osc_string(packet, offset)
            args.append(value)
        elif tag == "b":
            if offset + 4 > len(packet):
                raise OscCodecError("OSC blob length exceeds packet length")
            length = struct.unpack_from(">i", packet, offset)[0]
            offset += 4
            if length < 0 or offset + length > len(packet):
                raise OscCodecError("OSC blob exceeds packet length")
            args.append(packet[offset : offset + length])
            offset += int(math.ceil(length / 4.0) * 4)
        elif tag == "T":
            args.append(True)
        elif tag == "F":
            args.append(False)
        elif tag in ("N", "I"):
            args.append(None)
        else:
            raise OscCodecError(f"Unsupported OSC type tag: {tag}")
    return address, args


def decode_osc_packet(packet: bytes) -> list[tuple[str, list[Any]]]:
    """Decode an OSC message or bundle into ``(address, args)`` tuples."""

    raw = bytes(packet or b"")
    if not raw:
        raise OscCodecError("OSC packet is empty")
    if raw.startswith(b"#bundle\x00"):
        if len(raw) < 16:
            raise OscCodecError("OSC bundle is too short")
        offset = 16  # '#bundle\0' plus the 64-bit time tag
        messages: list[tuple[str, list[Any]]] = []
        while offset < len(raw):
            if offset + 4 > len(raw):
                raise OscCodecError("OSC bundle element length is missing")
            length = struct.unpack_from(">i", raw, offset)[0]
            offset += 4
            if length <= 0 or offset + length > len(raw):
                raise OscCodecError("OSC bundle element exceeds packet length")
            messages.extend(decode_osc_packet(raw[offset : offset + length]))
            offset += length
        return messages
    return [_decode_osc_message(raw)]


@dataclass(frozen=True)
class DigicoConfig:
    enabled: bool = False
    host: str = ""
    port: int = DEFAULT_DESK_PORT
    listen_address: str = "0.0.0.0"
    listen_port: int = DEFAULT_LISTEN_PORT
    request_interval: float = 0.1
    retry_interval: float = 1.0
    stale_after: float = 10.0
    auxes: tuple[dict[str, Any], ...] = ()
    channels: tuple[dict[str, Any], ...] = ()
    external_devices: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, cfg: dict[str, Any] | None) -> "DigicoConfig":
        raw = cfg or {}

        def _int(key: str, default: int, low: int, high: int) -> int:
            try:
                return max(low, min(high, int(raw.get(key, default))))
            except Exception:
                return default

        def _float(key: str, default: float, low: float, high: float) -> float:
            try:
                return max(low, min(high, float(raw.get(key, default))))
            except Exception:
                return default

        def _dict_tuple(key: str) -> tuple[dict[str, Any], ...]:
            value = raw.get(key, [])
            if not isinstance(value, list):
                return ()
            return tuple(dict(item) for item in value if isinstance(item, dict))

        return cls(
            enabled=bool(raw.get("digico_enabled", False)),
            host=str(raw.get("digico_ip") or "").strip(),
            port=_int("digico_port", DEFAULT_DESK_PORT, 1, 65535),
            listen_address=str(raw.get("digico_listen_address") or "0.0.0.0").strip() or "0.0.0.0",
            listen_port=_int("digico_listen_port", DEFAULT_LISTEN_PORT, 1, 65535),
            request_interval=_float("digico_request_interval", 0.1, 0.025, 5.0),
            retry_interval=_float("digico_retry_interval", 1.0, 0.1, 30.0),
            stale_after=_float("digico_stale_after", 10.0, 1.0, 300.0),
            auxes=_dict_tuple("digico_auxes"),
            channels=_dict_tuple("digico_channels"),
            external_devices=_dict_tuple("digico_external_devices"),
        )


class DigicoMixerClient:
    """One shared UDP connection, discovery worker and state cache."""

    CACHE_PREFIXES = (
        "/Console/",
        "/Aux_Outputs/",
        "/Input_Channels/",
        "/Snapshots/",
    )

    def __init__(self, config: DigicoConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._receiver_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._cache: dict[str, dict[str, Any]] = {}
        self._pending: deque[str] = deque()
        self._pending_set: set[str] = set()
        self._request_sent_at: dict[str, float] = {}
        self._last_query_address = ""
        self._last_query_at = 0.0
        self._last_heartbeat_at = 0.0
        self._last_packet_at = 0.0
        self._last_desk_packet_at = 0.0
        self._last_error = ""
        self._started_at = 0.0
        self._revision = 0
        self._packets_received = 0
        self._packets_sent = 0
        self._ignored_packets = 0
        self._parse_errors = 0
        self._relay_packets = 0
        self._desk_ip = ""
        self._external_device_cache: tuple[dict[str, Any], ...] | None = None
        self._current_snapshot = -1
        self._current_snapshot_name = ""

    def start(self) -> None:
        with self._lock:
            if self._receiver_thread and self._receiver_thread.is_alive():
                return
            if not self.config.enabled or not self.config.host:
                return
            try:
                self._desk_ip = socket.gethostbyname(self.config.host)
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.settimeout(0.25)
                sock.bind((self.config.listen_address, self.config.listen_port))
                self._socket = sock
                self._stop.clear()
                self._started_at = time.time()
                self._last_error = ""
            except Exception as exc:
                self._last_error = str(exc)
                if self._socket is not None:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                self._socket = None
                raise

            self._receiver_thread = threading.Thread(
                target=self._receive_loop,
                name="tdeck-digico-receiver",
                daemon=True,
            )
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="tdeck-digico-discovery",
                daemon=True,
            )
            self._receiver_thread.start()
            self._worker_thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            sock = self._socket
            self._socket = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        for thread in (self._receiver_thread, self._worker_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        self._receiver_thread = None
        self._worker_thread = None

    def restart_discovery(self) -> None:
        with self._lock:
            self._cache.clear()
            self._pending.clear()
            self._pending_set.clear()
            self._request_sent_at.clear()
            self._last_query_address = ""
            self._last_query_at = 0.0
            self._last_heartbeat_at = 0.0
            self._current_snapshot = -1
            self._current_snapshot_name = ""
            self._revision += 1

    def _external_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._external_device_cache is not None:
                return [dict(item) for item in self._external_device_cache]
        out: list[dict[str, Any]] = []
        for raw in self.config.external_devices:
            if not bool(raw.get("enabled", True)):
                continue
            ip = str(raw.get("ip") or "").strip()
            if not ip:
                continue
            try:
                resolved = socket.gethostbyname(ip)
                port = max(1, min(65535, int(raw.get("port", self.config.listen_port))))
            except Exception:
                continue
            out.append(
                {
                    "name": str(raw.get("name") or ip).strip(),
                    "ip": resolved,
                    "port": port,
                    "broadcast": bool(raw.get("broadcast", True)),
                    "loopback": bool(raw.get("loopback", False)),
                }
            )
        with self._lock:
            self._external_device_cache = tuple(dict(item) for item in out)
        return [dict(item) for item in out]

    def _relay_raw(self, raw: bytes, source: tuple[str, int]) -> None:
        source_ip = str(source[0])
        devices = self._external_devices()
        if source_ip == self._desk_ip:
            for device in devices:
                if not device["broadcast"]:
                    continue
                if not device["loopback"] and device["ip"] == source_ip:
                    continue
                self._send_raw(raw, device["ip"], int(device["port"]), relay=True)
            return

        # Only explicitly configured external devices can proxy to the desk.
        if not any(device["ip"] == source_ip for device in devices):
            return
        self._send_raw(raw, self._desk_ip, self.config.port, relay=True)
        for device in devices:
            if not device["broadcast"]:
                continue
            if not device["loopback"] and device["ip"] == source_ip:
                continue
            self._send_raw(raw, device["ip"], int(device["port"]), relay=True)

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                sock = self._socket
            if sock is None:
                return
            try:
                raw, source = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    with self._lock:
                        self._last_error = "DiGiCo UDP socket closed unexpectedly"
                return
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                continue

            now = time.time()
            source_ip = str(source[0])
            known_external = any(device["ip"] == source_ip for device in self._external_devices())
            if source_ip != self._desk_ip and not known_external:
                with self._lock:
                    self._ignored_packets += 1
                continue
            with self._lock:
                self._packets_received += 1
                self._last_packet_at = now
                if source_ip == self._desk_ip:
                    self._last_desk_packet_at = now

            try:
                self._relay_raw(raw, source)
                for address, args in decode_osc_packet(raw):
                    self._handle_message(address, args, source, now)
            except Exception as exc:
                with self._lock:
                    self._parse_errors += 1
                    self._last_error = f"OSC parse error: {exc}"

    def _handle_message(
        self,
        address: str,
        args: list[Any],
        source: tuple[str, int],
        now: float,
    ) -> None:
        with self._lock:
            prior = self._cache.get(address)
            changed = prior is None or prior.get("args") != args
            if address.startswith(self.CACHE_PREFIXES):
                self._cache[address] = {"address": address, "args": list(args), "ts": now}
            self._request_sent_at.pop(address + "/?", None)
            if changed:
                self._revision += 1

            if address == "/Console/Session/!":
                self._cache.clear()
                self._pending.clear()
                self._pending_set.clear()
                self._request_sent_at.clear()
                self._last_query_address = ""
                self._current_snapshot = -1
                self._current_snapshot_name = ""
                self._revision += 1
                return

            if address == "/Snapshots/Current_Snapshot" and args:
                try:
                    self._current_snapshot = int(args[0])
                except Exception:
                    self._current_snapshot = -1
                if self._current_snapshot < 0:
                    self._current_snapshot_name = ""
                else:
                    self._enqueue_locked("/Snapshots/names/?")
            elif address == "/Snapshots/name" and args:
                try:
                    if int(args[0]) == self._current_snapshot:
                        self._current_snapshot_name = str(args[-1])
                except Exception:
                    pass
            else:
                rename = address.rsplit("/", 1)
                if len(rename) == 2 and rename[0] == "/Snapshots/Rename_Snapshot" and args:
                    try:
                        if int(rename[1]) == self._current_snapshot:
                            self._current_snapshot_name = str(args[0])
                    except Exception:
                        pass

    def _cache_args_locked(self, address: str) -> list[Any] | None:
        entry = self._cache.get(address)
        if not entry:
            return None
        args = entry.get("args")
        return list(args) if isinstance(args, list) else None

    def _channel_count_locked(self) -> int:
        args = self._cache_args_locked("/Console/Input_Channels")
        try:
            return max(0, int(args[0])) if args else 0
        except Exception:
            return 0

    def _aux_modes_locked(self) -> list[Any]:
        return self._cache_args_locked("/Console/Aux_Outputs/modes") or []

    def _next_discovery_query_locked(self) -> str:
        if self._channel_count_locked() <= 0:
            return "/Console/Channels/?"
        modes = self._aux_modes_locked()
        if not modes:
            return "/Console/Aux_Outputs/modes/?"
        for number in range(1, len(modes) + 1):
            address = f"/Aux_Outputs/{number}/Buss_Trim/name"
            if address not in self._cache:
                return address + "/?"
        for number in range(1, self._channel_count_locked() + 1):
            address = f"/Input_Channels/{number}/Channel_Input/name"
            if address not in self._cache:
                return address + "/?"
        if "/Snapshots/Current_Snapshot" not in self._cache:
            return "/Snapshots/Current_Snapshot/?"
        return ""

    def _worker_loop(self) -> None:
        while not self._stop.wait(self.config.request_interval):
            query = ""
            now = time.time()
            with self._lock:
                discovery = self._next_discovery_query_locked()
                if discovery:
                    if (
                        discovery != self._last_query_address
                        or (now - self._last_query_at) >= self.config.retry_interval
                    ):
                        query = discovery
                elif self._pending:
                    query = self._pending.popleft()
                    self._pending_set.discard(query)
                else:
                    # Discovery and browser polling can both be idle. A small
                    # snapshot query keeps connectivity status truthful and
                    # notices a disconnected desk without flooding the wire.
                    heartbeat_interval = max(0.5, min(5.0, self.config.stale_after / 3.0))
                    if (now - self._last_heartbeat_at) >= heartbeat_interval:
                        query = "/Snapshots/Current_Snapshot/?"
                        self._last_heartbeat_at = now

                if query:
                    self._last_query_address = query
                    self._last_query_at = now
                    self._request_sent_at[query] = now
            if query:
                try:
                    self.send(query)
                except Exception:
                    pass

    def _send_raw(self, raw: bytes, host: str, port: int, *, relay: bool = False) -> None:
        with self._lock:
            sock = self._socket
        if sock is None:
            raise RuntimeError("DiGiCo UDP socket is not running")
        with self._send_lock:
            sock.sendto(raw, (host, int(port)))
        with self._lock:
            self._packets_sent += 1
            if relay:
                self._relay_packets += 1

    def send(self, address: str, args: Iterable[Any] | None = None) -> None:
        if not self._desk_ip:
            raise RuntimeError("DiGiCo desk host is not configured")
        self._send_raw(encode_osc_message(address, args), self._desk_ip, self.config.port)

    def _enqueue_locked(self, address: str) -> None:
        if address in self._pending_set:
            return
        sent_at = self._request_sent_at.get(address, 0.0)
        if sent_at and (time.time() - sent_at) < self.config.retry_interval:
            return
        self._pending.append(address)
        self._pending_set.add(address)

    def request_aux_state(self, aux_number: int) -> None:
        with self._lock:
            channel_count = self._channel_count_locked()
            modes = self._aux_modes_locked()
            stereo = 0 < int(aux_number) <= len(modes) and self._mode_is_stereo(modes[int(aux_number) - 1])
            level_queries: list[str] = []
            on_queries: list[str] = []
            pan_queries: list[str] = []
            for channel in range(1, channel_count + 1):
                custom = self._config_item(self.config.channels, channel)
                if custom and not bool(custom.get("enabled", True)):
                    continue
                on_address = f"/Input_Channels/{channel}/Aux_Send/{aux_number}/send_on"
                if on_address not in self._cache:
                    on_queries.append(on_address + "/?")
                level_address = f"/Input_Channels/{channel}/Aux_Send/{aux_number}/send_level"
                if level_address not in self._cache:
                    level_queries.append(level_address + "/?")
                if stereo:
                    pan_address = f"/Input_Channels/{channel}/Aux_Send/{aux_number}/send_pan"
                    if pan_address not in self._cache:
                        pan_queries.append(pan_address + "/?")
            # Preserve fast fader loading: levels populate first, followed by
            # send states, then stereo pans. Toggles remain disabled until
            # their own desk values arrive, so an unknown state cannot be sent.
            for address in level_queries + on_queries + pan_queries:
                self._enqueue_locked(address)

    @staticmethod
    def _colour(number: int, total: int) -> str:
        hue = ((max(1, number) - 1) / max(1, total)) % 1.0
        red, green, blue = colorsys.hls_to_rgb(hue, 0.40, 0.50)
        return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"

    @staticmethod
    def _config_item(items: tuple[dict[str, Any], ...], number: int) -> dict[str, Any]:
        index = number - 1
        return dict(items[index]) if 0 <= index < len(items) else {}

    @staticmethod
    def _mode_is_stereo(mode: Any) -> bool:
        if isinstance(mode, (int, float)):
            return int(mode) == 2
        return str(mode or "").strip().lower() in {"2", "stereo", "st"}

    @staticmethod
    def _value_is_on(value: Any) -> bool:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "on", "yes"}:
                return True
            if normalized in {"false", "off", "no", ""}:
                return False
            try:
                return float(normalized) != 0.0
            except Exception:
                return False
        try:
            return float(value) != 0.0
        except Exception:
            return bool(value)

    def mixer_config(self) -> dict[str, Any]:
        with self._lock:
            modes = self._aux_modes_locked()
            channel_count = self._channel_count_locked()
            auxes: list[dict[str, Any]] = []
            for number, mode in enumerate(modes, start=1):
                custom = self._config_item(self.config.auxes, number)
                name_args = self._cache_args_locked(f"/Aux_Outputs/{number}/Buss_Trim/name") or []
                desk_label = str(name_args[0]) if name_args else f"Aux {number}"
                auxes.append(
                    {
                        "channel": number,
                        "deskLabel": desk_label,
                        "label": str(custom.get("label") or desk_label),
                        "enabled": bool(custom.get("enabled", True)),
                        "stereo": self._mode_is_stereo(mode),
                        "mode": mode,
                        "colour": str(custom.get("colour") or self._colour(number, len(modes))),
                        "icon": str(custom.get("icon") or ""),
                        "order": int(custom.get("order", number)),
                    }
                )

            channels: list[dict[str, Any]] = []
            for number in range(1, channel_count + 1):
                custom = self._config_item(self.config.channels, number)
                name_args = self._cache_args_locked(
                    f"/Input_Channels/{number}/Channel_Input/name"
                ) or []
                desk_label = str(name_args[0]) if name_args else f"Channel {number}"
                channels.append(
                    {
                        "channel": number,
                        "deskLabel": desk_label,
                        "label": str(custom.get("label") or desk_label),
                        "enabled": bool(custom.get("enabled", True)),
                        "order": int(custom.get("order", number)),
                        "group": str(custom.get("group") or custom.get("title") or ""),
                        "icon": str(custom.get("icon") or ""),
                    }
                )

            return {
                "auxes": sorted(auxes, key=lambda item: (item["order"], item["channel"])),
                "channels": sorted(channels, key=lambda item: (item["order"], item["channel"])),
                "snapshot": self._current_snapshot_name,
                "revision": self._revision,
            }

    def aux_state(self, aux_number: int) -> dict[str, Any]:
        cfg = self.mixer_config()
        aux = next((item for item in cfg["auxes"] if int(item["channel"]) == int(aux_number)), None)
        if aux is None:
            raise KeyError("AUX not found")
        self.request_aux_state(int(aux_number))
        with self._lock:
            values: list[dict[str, Any]] = []
            for channel in cfg["channels"]:
                number = int(channel["channel"])
                level_args = self._cache_args_locked(
                    f"/Input_Channels/{number}/Aux_Send/{aux_number}/send_level"
                ) or []
                on_args = self._cache_args_locked(
                    f"/Input_Channels/{number}/Aux_Send/{aux_number}/send_on"
                ) or []
                pan_args = self._cache_args_locked(
                    f"/Input_Channels/{number}/Aux_Send/{aux_number}/send_pan"
                ) or []
                values.append(
                    {
                        **channel,
                        "sendOn": self._value_is_on(on_args[0]) if on_args else None,
                        "level": float(level_args[0]) if level_args else None,
                        "pan": float(pan_args[0]) if pan_args else None,
                    }
                )
            return {
                "aux": aux,
                "channels": values,
                "snapshot": self._current_snapshot_name,
                "revision": self._revision,
            }

    def _validate_route(self, aux_number: int, channel_number: int) -> None:
        with self._lock:
            aux_count = len(self._aux_modes_locked())
            channel_count = self._channel_count_locked()
        if aux_number < 1 or aux_number > aux_count:
            raise ValueError("AUX number is outside the discovered desk range")
        if channel_number < 1 or channel_number > channel_count:
            raise ValueError("Channel number is outside the discovered desk range")

    def route_enabled(self, aux_number: int, channel_number: int | None = None) -> bool:
        """Check a configured route without rebuilding the browser layout."""
        with self._lock:
            aux_number = int(aux_number)
            modes = self._aux_modes_locked()
            if aux_number < 1 or aux_number > len(modes):
                return False
            aux_config = self._config_item(self.config.auxes, aux_number)
            if aux_config and not bool(aux_config.get("enabled", True)):
                return False
            if channel_number is None:
                return True
            channel_number = int(channel_number)
            if channel_number < 1 or channel_number > self._channel_count_locked():
                return False
            channel_config = self._config_item(self.config.channels, channel_number)
            return not channel_config or bool(channel_config.get("enabled", True))

    def set_level(self, aux_number: int, channel_number: int, db: float) -> float:
        self._validate_route(aux_number, channel_number)
        value = max(-150.0, min(10.0, float(db)))
        address = f"/Input_Channels/{channel_number}/Aux_Send/{aux_number}/send_level"
        self.send(address, [value])
        self._optimistic_cache(address, [value])
        return value

    def set_pan(self, aux_number: int, channel_number: int, pan: float) -> float:
        self._validate_route(aux_number, channel_number)
        value = max(0.0, min(1.0, float(pan)))
        address = f"/Input_Channels/{channel_number}/Aux_Send/{aux_number}/send_pan"
        self.send(address, [value])
        self._optimistic_cache(address, [value])
        return value

    def set_send_on(self, aux_number: int, channel_number: int, enabled: bool) -> bool:
        self._validate_route(aux_number, channel_number)
        value = bool(enabled)
        address = f"/Input_Channels/{channel_number}/Aux_Send/{aux_number}/send_on"
        self.send(address, [1 if value else 0])
        self._optimistic_cache(address, [1 if value else 0])
        return value

    def _optimistic_cache(self, address: str, args: list[Any]) -> None:
        with self._lock:
            self._cache[address] = {"address": address, "args": list(args), "ts": time.time()}
            self._revision += 1

    def status(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            running = bool(self._receiver_thread and self._receiver_thread.is_alive() and self._socket)
            ready = not bool(self._next_discovery_query_locked())
            last_age = (now - self._last_desk_packet_at) if self._last_desk_packet_at else None
            return {
                "configured": bool(self.config.enabled and self.config.host),
                "enabled": bool(self.config.enabled),
                "running": running,
                "connected": bool(running and last_age is not None and last_age <= self.config.stale_after),
                "ready": bool(ready and running),
                "desk": f"{self.config.host}:{self.config.port}" if self.config.host else "",
                "listen": f"{self.config.listen_address}:{self.config.listen_port}",
                "lastDeskPacketAge": last_age,
                "lastPacketAt": self._last_packet_at or None,
                "startedAt": self._started_at or None,
                "packetsReceived": self._packets_received,
                "packetsSent": self._packets_sent,
                "ignoredPackets": self._ignored_packets,
                "relayPackets": self._relay_packets,
                "parseErrors": self._parse_errors,
                "lastError": self._last_error,
                "cacheEntries": len(self._cache),
                "pendingRequests": len(self._pending),
                "channels": self._channel_count_locked(),
                "auxes": len(self._aux_modes_locked()),
                "snapshot": self._current_snapshot_name,
                "revision": self._revision,
                "missingDiscoveryRequest": self._next_discovery_query_locked(),
                "externalDevices": len(self._external_devices()),
            }


_CLIENT_LOCK = threading.Lock()
_CLIENT: DigicoMixerClient | None = None
_CLIENT_SIGNATURE = ""


def _config_signature(config: DigicoConfig) -> str:
    return json.dumps(
        {
            "enabled": config.enabled,
            "host": config.host,
            "port": config.port,
            "listen_address": config.listen_address,
            "listen_port": config.listen_port,
            "request_interval": config.request_interval,
            "retry_interval": config.retry_interval,
            "stale_after": config.stale_after,
            "auxes": config.auxes,
            "channels": config.channels,
            "external_devices": config.external_devices,
        },
        sort_keys=True,
        default=str,
    )


def get_digico_client_from_config(cfg: dict[str, Any] | None) -> DigicoMixerClient | None:
    """Return the process-wide DiGiCo client for the supplied TDeck config."""

    global _CLIENT, _CLIENT_SIGNATURE
    config = DigicoConfig.from_mapping(cfg)
    signature = _config_signature(config)
    with _CLIENT_LOCK:
        if _CLIENT is not None and signature == _CLIENT_SIGNATURE:
            try:
                _CLIENT.start()
            except Exception:
                pass
            return _CLIENT

        previous = _CLIENT
        _CLIENT = DigicoMixerClient(config)
        _CLIENT_SIGNATURE = signature
        if previous is not None:
            previous.close()
        try:
            _CLIENT.start()
        except Exception:
            # Keep the client so its detailed status/lastError remains visible.
            pass
        return _CLIENT


def close_digico_client() -> None:
    global _CLIENT, _CLIENT_SIGNATURE
    with _CLIENT_LOCK:
        client = _CLIENT
        _CLIENT = None
        _CLIENT_SIGNATURE = ""
    if client is not None:
        client.close()
