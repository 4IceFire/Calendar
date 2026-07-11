"""Blackmagic ATEM audio control client.

This module wraps PyATEMMax in the same lightweight style as the repo's
`companion.py`, `propresentor.py`, and `videohub.py` integrations.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Any, Optional

try:
    import PyATEMMax
except Exception:  # pragma: no cover - optional dependency until installed
    PyATEMMax = None  # type: ignore

try:
    from atem_meter import AtemMeterClient, empty_level_payload
except Exception:  # pragma: no cover - optional at import time
    AtemMeterClient = None  # type: ignore
    empty_level_payload = None  # type: ignore


DEFAULT_PORT = 9910
DEFAULT_TIMEOUT = 3.0
MASTER_SOURCE_ID = "master"

_FALLBACK_AUDIO_SOURCES = [
    {"id": "1", "source": 1, "label": "Input 1", "kind": "input"},
    {"id": "2", "source": 2, "label": "Input 2", "kind": "input"},
    {"id": "3", "source": 3, "label": "Input 3", "kind": "input"},
    {"id": "4", "source": 4, "label": "Input 4", "kind": "input"},
    {"id": "5", "source": 5, "label": "Input 5", "kind": "input"},
    {"id": "6", "source": 6, "label": "Input 6", "kind": "input"},
    {"id": "7", "source": 7, "label": "Input 7", "kind": "input"},
    {"id": "8", "source": 8, "label": "Input 8", "kind": "input"},
    {"id": "9", "source": 9, "label": "Input 9", "kind": "input"},
    {"id": "10", "source": 10, "label": "Input 10", "kind": "input"},
    {"id": "11", "source": 11, "label": "Input 11", "kind": "input"},
    {"id": "12", "source": 12, "label": "Input 12", "kind": "input"},
    {"id": "13", "source": 13, "label": "Input 13", "kind": "input"},
    {"id": "14", "source": 14, "label": "Input 14", "kind": "input"},
    {"id": "15", "source": 15, "label": "Input 15", "kind": "input"},
    {"id": "16", "source": 16, "label": "Input 16", "kind": "input"},
    {"id": "17", "source": 17, "label": "Input 17", "kind": "input"},
    {"id": "18", "source": 18, "label": "Input 18", "kind": "input"},
    {"id": "19", "source": 19, "label": "Input 19", "kind": "input"},
    {"id": "20", "source": 20, "label": "Input 20", "kind": "input"},
    {"id": "1001", "source": 1001, "label": "XLR", "kind": "input"},
    {"id": "1101", "source": 1101, "label": "AES/EBU", "kind": "input"},
    {"id": "1201", "source": 1201, "label": "RCA", "kind": "input"},
    {"id": "2001", "source": 2001, "label": "Media Player 1", "kind": "input"},
    {"id": "2002", "source": 2002, "label": "Media Player 2", "kind": "input"},
]

_CLIENT_CACHE_LOCK = threading.Lock()
_CLIENT_CACHE: dict[tuple[str, int, float, bool], "AtemAudioClient"] = {}


@dataclass(frozen=True)
class AtemConfig:
    host: str
    port: int = DEFAULT_PORT
    timeout: float = DEFAULT_TIMEOUT


def get_atem_client_from_config(
    cfg: dict[str, Any] | None,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    timeout: Optional[float] = None,
    debug: bool = False,
) -> Optional["AtemAudioClient"]:
    cfg = cfg or {}
    host_value = str(host or cfg.get("atem_ip") or cfg.get("atem_host") or "").strip()
    if not host_value:
        return None

    try:
        port_value = int(port or cfg.get("atem_port") or DEFAULT_PORT)
    except Exception:
        port_value = DEFAULT_PORT
    if port_value < 1 or port_value > 65535:
        port_value = DEFAULT_PORT

    try:
        timeout_value = float(timeout if timeout is not None else cfg.get("atem_timeout", DEFAULT_TIMEOUT))
    except Exception:
        timeout_value = DEFAULT_TIMEOUT
    timeout_value = max(0.5, min(timeout_value, 15.0))

    key = (host_value, port_value, timeout_value, bool(debug))
    with _CLIENT_CACHE_LOCK:
        client = _CLIENT_CACHE.get(key)
        if client is not None:
            return client
        client = AtemAudioClient(host_value, port_value, timeout=timeout_value, debug=debug)
        for old_client in _CLIENT_CACHE.values():
            try:
                old_client.close()
            except Exception:
                pass
        _CLIENT_CACHE.clear()
        _CLIENT_CACHE[key] = client
        return client


class AtemAudioClient:
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        debug: bool = False,
    ) -> None:
        self.host = str(host or "").strip()
        self.port = int(port or DEFAULT_PORT)
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.debug = bool(debug)
        self._lock = threading.RLock()
        self._switcher = None
        self._connected = False
        self._meter_client = None

    def _build_switcher(self):
        if PyATEMMax is None:
            raise RuntimeError("PyATEMMax is not installed")
        sw = PyATEMMax.ATEMMax()
        try:
            sw.atem.UDPPort = int(self.port)
        except Exception:
            pass
        return sw

    def close(self) -> None:
        with self._lock:
            sw = self._switcher
            self._switcher = None
            self._connected = False
            meter_client = self._meter_client
            self._meter_client = None
            if meter_client is not None:
                try:
                    meter_client.close()
                except Exception:
                    pass
            if sw is not None:
                try:
                    sw.disconnect()
                except Exception:
                    pass

    def _switcher_is_connected(self, sw: Any) -> bool:
        try:
            return bool(getattr(sw, "connected", False) or getattr(sw, "switcherAlive", False))
        except Exception:
            return False

    def _ensure_switcher(self):
        if not self.host:
            raise ValueError("ATEM host is required")
        if self._switcher is not None and self._switcher_is_connected(self._switcher):
            self._connected = True
            return self._switcher

        if self._switcher is not None:
            try:
                self._switcher.disconnect()
            except Exception:
                pass
            self._switcher = None
            self._connected = False

        sw = self._build_switcher()
        sw.connect(self.host, connTimeout=max(1, int(round(self.timeout))))
        connected = bool(sw.waitForConnection(infinite=False, timeout=self.timeout))
        if not connected:
            try:
                sw.disconnect()
            except Exception:
                pass
            raise TimeoutError("ATEM connection timed out")
        self._switcher = sw
        self._connected = True
        self._ensure_metering()
        return sw

    def _ensure_metering(self) -> None:
        if AtemMeterClient is None:
            return
        if self._meter_client is None:
            self._meter_client = AtemMeterClient(self.host, self.port, timeout=self.timeout)
        try:
            self._meter_client.start()
        except Exception:
            pass

    def _meter_levels(self) -> dict[str, Any] | None:
        try:
            self._ensure_metering()
            if self._meter_client is not None:
                return self._meter_client.get_levels()
        except Exception:
            pass
        return None

    def _meter_status(self) -> dict[str, Any]:
        if AtemMeterClient is None:
            return {
                "enabled": False,
                "connected": False,
                "active": False,
                "unavailableReason": "ATEM meter client is unavailable",
            }
        try:
            self._ensure_metering()
            if self._meter_client is not None:
                return self._meter_client.status()
        except Exception as e:
            return {
                "enabled": False,
                "connected": False,
                "active": False,
                "unavailableReason": str(e),
            }
        return {
            "enabled": False,
            "connected": False,
            "active": False,
            "unavailableReason": "ATEM meter client is not running",
        }

    def _with_switcher(self, callback):
        with self._lock:
            try:
                sw = self._ensure_switcher()
                result = callback(sw)
                time.sleep(0.08)
                return result
            except Exception:
                self.close()
                raise

    def ping(self) -> bool:
        try:
            with self._lock:
                sw = self._ensure_switcher()
                if not self._switcher_is_connected(sw):
                    raise TimeoutError("ATEM connection lost")
            return True
        except Exception:
            self.close()
            return False

    @staticmethod
    def fallback_sources() -> list[dict[str, Any]]:
        return [{"id": MASTER_SOURCE_ID, "label": "Master", "kind": "master"}, *_FALLBACK_AUDIO_SOURCES]

    @staticmethod
    def _constant_name(value: Any) -> str:
        try:
            return str(getattr(value, "name", "") or value or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _input_label(sw: Any, source_id: int, fallback: str) -> str:
        try:
            props = sw.inputProperties[source_id]
            label = str(getattr(props, "longName", "") or getattr(props, "shortName", "") or "").strip()
            if label:
                return label
        except Exception:
            pass
        return fallback

    @staticmethod
    def _audio_source_defs(sw: Any) -> list[dict[str, Any]]:
        values = []
        try:
            raw_values = getattr(sw.atem.audioSources, "_values", {}) or {}
            values = list(raw_values.values())
        except Exception:
            values = []

        out: list[dict[str, Any]] = []
        for item in values:
            try:
                source_id = int(getattr(item, "value"))
                name = str(getattr(item, "name", "") or "").strip()
            except Exception:
                continue
            if source_id <= 0:
                continue
            if name.startswith("input"):
                label = AtemAudioClient._input_label(sw, source_id, f"Input {source_id}")
            elif name == "xlr":
                label = "XLR"
            elif name == "aes_ebu":
                label = "AES/EBU"
            elif name == "rca":
                label = "RCA"
            elif name.startswith("mp"):
                label = "Media Player " + name[2:]
            elif name.startswith("mic"):
                label = "Mic " + name[3:]
            else:
                label = name.replace("_", " ").title()
            out.append({"id": str(source_id), "source": source_id, "label": label, "kind": "input"})
        return out or list(_FALLBACK_AUDIO_SOURCES)

    @staticmethod
    def _level_db(sw: Any, raw: Any) -> float:
        try:
            return float(sw.atem.audioWord2Db(int(raw or 0)))
        except Exception:
            return -60.0

    @staticmethod
    def _level_payload(sw: Any, level_obj: Any) -> dict[str, Any]:
        left_raw = getattr(level_obj, "left", 0) if level_obj is not None else 0
        right_raw = getattr(level_obj, "right", 0) if level_obj is not None else 0
        peak = getattr(level_obj, "peak", None) if level_obj is not None else None
        peak_left_raw = getattr(peak, "left", 0) if peak is not None else 0
        peak_right_raw = getattr(peak, "right", 0) if peak is not None else 0
        left_db = AtemAudioClient._level_db(sw, left_raw)
        right_db = AtemAudioClient._level_db(sw, right_raw)
        peak_left_db = AtemAudioClient._level_db(sw, peak_left_raw)
        peak_right_db = AtemAudioClient._level_db(sw, peak_right_raw)
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

    @staticmethod
    def _empty_level_payload() -> dict[str, Any]:
        if empty_level_payload is not None:
            return empty_level_payload()
        return {
            "left": -60.0,
            "right": -60.0,
            "peakLeft": -60.0,
            "peakRight": -60.0,
            "max": -60.0,
            "peakMax": -60.0,
            "raw": {"left": 0, "right": 0, "peakLeft": 0, "peakRight": 0},
        }

    def get_audio_state(self) -> dict[str, Any]:
        def _read(sw: Any) -> dict[str, Any]:
            meter_levels = self._meter_levels() or {}
            meter_sources = meter_levels.get("sources") if isinstance(meter_levels, dict) else {}
            sources = [{"id": MASTER_SOURCE_ID, "label": "Master", "kind": "master"}]
            for item in self._audio_source_defs(sw):
                source_id = int(item["source"])
                try:
                    inp = sw.audioMixer.input[source_id]
                    volume = float(getattr(inp, "volume", 0.0) or 0.0)
                    mix_option = self._constant_name(getattr(inp, "mixOption", ""))
                except Exception:
                    volume = 0.0
                    mix_option = "off"
                row = dict(item)
                row.update({
                    "volume": volume,
                    "muted": mix_option == "off",
                    "mixOption": mix_option or "off",
                })
                row["level"] = (meter_sources or {}).get(str(source_id)) or self._empty_level_payload()
                sources.append(row)

            try:
                master_volume = float(getattr(sw.audioMixer.master, "volume", 0.0) or 0.0)
            except Exception:
                master_volume = 0.0
            sources[0]["volume"] = master_volume
            sources[0]["muted"] = False
            sources[0]["level"] = meter_levels.get("master") if isinstance(meter_levels, dict) and meter_levels.get("master") else self._empty_level_payload()

            try:
                monitor = sw.audioMixer.monitor
                monitor_audio = bool(getattr(monitor, "monitorAudio", False))
                monitor_dim = bool(getattr(monitor, "dim", False))
                monitor_mute = bool(getattr(monitor, "mute", False))
                monitor_volume = float(getattr(monitor, "volume", 0.0) or 0.0)
                solo = bool(getattr(monitor, "solo", False))
                solo_input = getattr(monitor, "soloInput", None)
                solo_source = str(int(getattr(solo_input, "value"))) if solo_input is not None else ""
            except Exception:
                monitor_audio = False
                monitor_dim = False
                monitor_mute = False
                monitor_volume = 0.0
                solo = False
                solo_source = ""

            return {
                "connected": True,
                "host": self.host,
                "port": self.port,
                "sources": sources,
                "monitor": {
                    "enabled": monitor_audio,
                    "dim": monitor_dim,
                    "muted": monitor_mute,
                    "volume": monitor_volume,
                    "solo": solo,
                    "soloSource": solo_source,
                },
                "metering": self._meter_status(),
            }

        return self._with_switcher(_read)

    def set_volume(self, source_id: str, db: float) -> None:
        source = str(source_id or "").strip()
        db = max(-60.0, min(float(db), 6.0))

        def _set(sw: Any) -> None:
            if source == MASTER_SOURCE_ID:
                sw.setAudioMixerMasterVolume(db)
            else:
                sw.setAudioMixerInputVolume(int(source), db)

        self._with_switcher(_set)

    def set_mix_option(self, source_id: str, mix_option: str) -> None:
        source = str(source_id or "").strip()
        if source == MASTER_SOURCE_ID:
            raise ValueError("Master mix option is not supported by this ATEM audio mixer")
        option = str(mix_option or "").strip().lower()
        if option in ("afv", "audiofollowvideo", "audio_follow_video"):
            atem_option = "afv"
        elif option in ("on", "1", "true"):
            atem_option = "on"
        else:
            atem_option = "off"

        def _set(sw: Any) -> None:
            sw.setAudioMixerInputMixOption(int(source), atem_option)

        self._with_switcher(_set)

    def set_mute(self, source_id: str, muted: bool) -> None:
        self.set_mix_option(source_id, "off" if muted else "on")

    def set_solo(self, source_id: str, enabled: bool) -> None:
        source = str(source_id or "").strip()
        if source == MASTER_SOURCE_ID:
            raise ValueError("Master solo is not supported")

        def _set(sw: Any) -> None:
            if enabled:
                sw.setAudioMixerMonitorSoloInput(int(source))
                sw.setAudioMixerMonitorSolo(True)
            else:
                sw.setAudioMixerMonitorSolo(False)

        self._with_switcher(_set)

    def set_monitor(self, *, enabled: bool | None = None, dim: bool | None = None, volume: float | None = None) -> None:
        def _set(sw: Any) -> None:
            if enabled is not None:
                sw.setAudioMixerMonitorMonitorAudio(bool(enabled))
            if dim is not None:
                sw.setAudioMixerMonitorDim(bool(dim))
            if volume is not None:
                db = max(-60.0, min(float(volume), 6.0))
                sw.setAudioMixerMonitorVolume(db)

        self._with_switcher(_set)
