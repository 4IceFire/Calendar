"""Legacy ATEM audio metering helper.

This intentionally does not use PyATEMMax for audio levels. Some older ATEM
models send valid legacy AMLv meter packets that the current PyATEMMax parser
cannot handle, so this small client opens its own UDP session and only asks for
legacy level packets.
"""

from __future__ import annotations

import math
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any


FLAG_RELIABLE = 1
FLAG_SYN = 2
FLAG_RETRANSMISSION = 4
FLAG_REQUEST_RETRANSMISSION = 8
FLAG_ACK = 16

STATE_SYN_SENT = 1
STATE_ESTABLISHED = 3

PACKET_HEADER = struct.Struct(">HHH 2x HH")


@dataclass
class _Packet:
    flags: int = 0
    session: int = 0
    sequence_number: int = 0
    acknowledgement_number: int = 0
    remote_sequence_number: int = 0
    data: bytes = b""

    @classmethod
    def from_bytes(cls, raw: bytes) -> "_Packet":
        if len(raw) < PACKET_HEADER.size:
            raise ValueError("ATEM packet was too short")
        fields = PACKET_HEADER.unpack_from(raw)
        length = fields[0] & ~(0x1F << 11)
        if length != len(raw):
            raise ValueError(f"ATEM packet length mismatch: header={length} actual={len(raw)}")
        return cls(
            flags=(fields[0] & (0x1F << 11)) >> 11,
            session=fields[1],
            acknowledgement_number=fields[2],
            remote_sequence_number=fields[3],
            sequence_number=fields[4],
            data=raw[PACKET_HEADER.size:],
        )

    def to_bytes(self) -> bytes:
        data = self.data or b""
        header = PACKET_HEADER.pack(
            PACKET_HEADER.size + len(data) + (self.flags << 11),
            self.session,
            self.acknowledgement_number,
            self.remote_sequence_number,
            self.sequence_number,
        )
        return header + data


def _atem_command(name: str, payload: bytes) -> bytes:
    return struct.pack(">H 2x 4s", len(payload) + 8, name.encode("ascii")) + payload


def _level_db(raw_value: int) -> float:
    if raw_value <= 0:
        return -60.0
    return float(math.log10(raw_value / (128 * 65536)) * 20)


def _level_payload(values: tuple[int, int, int, int] | None) -> dict[str, Any]:
    left_raw, right_raw, peak_left_raw, peak_right_raw = values or (0, 0, 0, 0)
    left_db = _level_db(int(left_raw or 0))
    right_db = _level_db(int(right_raw or 0))
    peak_left_db = _level_db(int(peak_left_raw or 0))
    peak_right_db = _level_db(int(peak_right_raw or 0))
    return {
        "left": left_db,
        "right": right_db,
        "peakLeft": peak_left_db,
        "peakRight": peak_right_db,
        "max": max(left_db, right_db),
        "peakMax": max(peak_left_db, peak_right_db),
        "raw": {
            "left": int(left_raw or 0),
            "right": int(right_raw or 0),
            "peakLeft": int(peak_left_raw or 0),
            "peakRight": int(peak_right_raw or 0),
        },
    }


def empty_level_payload() -> dict[str, Any]:
    return _level_payload(None)


def _parse_amlv(raw: bytes) -> dict[str, Any]:
    header_size = struct.calcsize(">H2x 4I 4I")
    if len(raw) < header_size:
        raise ValueError("AMLv packet was too short")
    fields = struct.unpack_from(">H2x 4I 4I", raw, 0)
    count = int(fields[0])
    source_offset = header_size
    source_end = source_offset + (2 * count)
    if len(raw) < source_end:
        raise ValueError("AMLv packet was missing source ids")
    sources = struct.unpack_from(f">{count}H", raw, source_offset) if count else ()
    level_offset = int(math.ceil(source_end / 4.0) * 4)
    level_end = level_offset + (count * 4 * 4)
    if len(raw) < level_end:
        raise ValueError("AMLv packet was missing source levels")
    level_words = struct.unpack_from(f">{count * 4}I", raw, level_offset) if count else ()

    input_levels: dict[str, dict[str, Any]] = {}
    for index, source_id in enumerate(sources):
        offset = index * 4
        input_levels[str(int(source_id))] = _level_payload(
            (
                int(level_words[offset]),
                int(level_words[offset + 1]),
                int(level_words[offset + 2]),
                int(level_words[offset + 3]),
            )
        )

    return {
        "count": count,
        "master": _level_payload((int(fields[1]), int(fields[2]), int(fields[3]), int(fields[4]))),
        "monitor": _level_payload((int(fields[5]), int(fields[6]), int(fields[7]), int(fields[8]))),
        "sources": input_levels,
        "updatedAt": time.time(),
    }


class AtemMeterClient:
    """Small UDP client that receives legacy ATEM AMLv audio meter packets."""

    def __init__(self, host: str, port: int = 9910, *, timeout: float = 3.0) -> None:
        self.host = str(host or "").strip()
        self.port = int(port or 9910)
        self.timeout = max(0.5, float(timeout or 3.0))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._levels: dict[str, Any] | None = None
        self._last_error = ""
        self._running = False
        self._connected = False
        self._packets_received = 0
        self._commands_received = 0
        self._amlv_packets_received = 0
        self._saln_packets_sent = 0
        self._last_packet_at: float | None = None
        self._last_command_at: float | None = None
        self._last_saln_at: float | None = None
        self._command_names: list[str] = []

    def start(self) -> None:
        if not self.host:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="atem-meter", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        with self._lock:
            self._thread = None
            self._running = False
            self._connected = False

    def get_levels(self) -> dict[str, Any] | None:
        with self._lock:
            if self._levels is None:
                return None
            return {
                "count": self._levels.get("count", 0),
                "master": dict(self._levels.get("master") or empty_level_payload()),
                "monitor": dict(self._levels.get("monitor") or empty_level_payload()),
                "sources": {str(k): dict(v) for k, v in (self._levels.get("sources") or {}).items()},
                "updatedAt": self._levels.get("updatedAt"),
            }

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self._levels is not None and (time.time() - float(self._levels.get("updatedAt") or 0)) < 3.0
            return {
                "enabled": bool(self._running),
                "connected": bool(self._connected),
                "active": bool(active),
                "unavailableReason": self._last_error,
                "updatedAt": self._levels.get("updatedAt") if self._levels else None,
                "sourceCount": int(self._levels.get("count") or 0) if self._levels else 0,
                "packetsReceived": int(self._packets_received),
                "commandsReceived": int(self._commands_received),
                "amlvPacketsReceived": int(self._amlv_packets_received),
                "salnPacketsSent": int(self._saln_packets_sent),
                "lastPacketAt": self._last_packet_at,
                "lastCommandAt": self._last_command_at,
                "lastSalnAt": self._last_saln_at,
                "recentCommandNames": list(self._command_names[-20:]),
            }

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message or "")

    def _set_levels(self, levels: dict[str, Any]) -> None:
        with self._lock:
            self._levels = levels
            self._last_error = ""
            self._connected = True

    def _send_packet(
        self,
        sock: socket.socket,
        packet: _Packet,
        *,
        session_id: int | None,
        local_sequence: int,
    ) -> int:
        packet.session = int(session_id or 0)
        if not packet.flags & FLAG_ACK:
            packet.sequence_number = (local_sequence + 1) % (2**16)
        sock.sendto(packet.to_bytes(), (self.host, self.port))
        if packet.flags & (FLAG_SYN | FLAG_ACK) == 0:
            local_sequence = (local_sequence + 1) % (2**16)
        return local_sequence

    def _send_ack(self, sock: socket.socket, session_id: int | None, sequence_number: int) -> None:
        ack = _Packet(
            flags=FLAG_ACK,
            session=int(session_id or 0),
            acknowledgement_number=int(sequence_number or 0),
            remote_sequence_number=0x61,
        )
        sock.sendto(ack.to_bytes(), (self.host, self.port))

    def _send_audio_levels_enable(self, sock: socket.socket, session_id: int | None, local_sequence: int, enabled: bool) -> int:
        data = _atem_command("SALN", struct.pack(">? 3x", bool(enabled)))
        packet = _Packet(flags=FLAG_RELIABLE, data=data)
        local_sequence = self._send_packet(sock, packet, session_id=session_id, local_sequence=local_sequence)
        with self._lock:
            self._saln_packets_sent += 1
            self._last_saln_at = time.time()
        return local_sequence

    def _handle_commands(self, data: bytes) -> None:
        offset = 0
        while offset + 8 <= len(data):
            command_len = struct.unpack_from(">H", data, offset)[0]
            if command_len < 8 or offset + command_len > len(data):
                break
            name = data[offset + 4:offset + 8].decode("ascii", errors="replace")
            payload = data[offset + 8:offset + command_len]
            with self._lock:
                self._commands_received += 1
                self._last_command_at = time.time()
                self._command_names.append(name)
                if len(self._command_names) > 60:
                    self._command_names = self._command_names[-60:]
            if name == "AMLv":
                with self._lock:
                    self._amlv_packets_received += 1
                self._set_levels(_parse_amlv(payload))
            offset += command_len

    def _run(self) -> None:
        backoff = 0.5
        with self._lock:
            self._running = True
        while not self._stop.is_set():
            try:
                self._run_once()
                backoff = 0.5
            except Exception as e:
                with self._lock:
                    self._connected = False
                self._set_error(str(e))
                self._stop.wait(backoff)
                backoff = min(backoff * 1.5, 5.0)
        with self._lock:
            self._running = False
            self._connected = False

    def _run_once(self) -> None:
        local_sequence = -1
        session_id: int | None = 0x1337
        state = STATE_SYN_SENT
        enable_ack = False
        sent_enable = False
        last_enable_sent = 0.0

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.25)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)

            syn = _Packet(flags=FLAG_SYN, data=bytes([1, 0, 0, 0, 0, 0, 0, 0]))
            local_sequence = self._send_packet(
                sock,
                syn,
                session_id=session_id,
                local_sequence=local_sequence,
            )
            started_at = time.time()

            while not self._stop.is_set():
                if state == STATE_SYN_SENT and time.time() - started_at > self.timeout:
                    raise TimeoutError("ATEM meter connection timed out")
                if state == STATE_ESTABLISHED and session_id is not None:
                    now = time.time()
                    if not sent_enable or now - last_enable_sent > 2.0:
                        local_sequence = self._send_audio_levels_enable(sock, session_id, local_sequence, True)
                        sent_enable = True
                        last_enable_sent = now

                try:
                    raw, _addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue

                packet = _Packet.from_bytes(raw)
                with self._lock:
                    self._packets_received += 1
                    self._last_packet_at = time.time()
                remote_sequence = packet.sequence_number
                if session_id is None:
                    session_id = packet.session

                if packet.flags & FLAG_REQUEST_RETRANSMISSION:
                    continue
                if packet.flags & FLAG_RETRANSMISSION and len(packet.data) == 0:
                    continue

                if state == STATE_SYN_SENT:
                    if packet.flags & FLAG_SYN and packet.data and packet.data[0] == 0x02:
                        state = STATE_ESTABLISHED
                        self._send_ack(sock, session_id, remote_sequence)
                        session_id = None
                    continue

                if state != STATE_ESTABLISHED:
                    continue

                with self._lock:
                    self._connected = True

                if packet.flags & FLAG_RELIABLE:
                    enable_ack = True
                    self._send_ack(sock, session_id, remote_sequence)

                if len(packet.data) == 0:
                    if not enable_ack:
                        enable_ack = True
                        self._send_ack(sock, session_id, remote_sequence)
                    continue

                self._handle_commands(packet.data)
