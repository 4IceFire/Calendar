"""Native Hisense/VIDAA TV control for TDeck.

Each configured TV owns a small worker thread.  HTTP callers only enqueue
commands, while the worker serializes MQTT operations and reconnects when the
TV wakes or drops off the network.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    from pyvidaa import VidaaTV
    from pyvidaa.protocol import AuthMethod, detect_protocol, get_auth_method
    from pyvidaa.topics import TOPIC_SET_SOURCE, get_topic
    from pyvidaa.wol import wake_tv
except Exception:  # pragma: no cover - reported through manager status
    VidaaTV = None  # type: ignore[assignment]
    AuthMethod = None  # type: ignore[assignment]
    detect_protocol = None  # type: ignore[assignment]
    get_auth_method = None  # type: ignore[assignment]
    TOPIC_SET_SOURCE = ""
    get_topic = None  # type: ignore[assignment]
    wake_tv = None  # type: ignore[assignment]


DEFAULT_SOURCES = [
    {"id": "TV", "name": "TV"},
    {"id": "HDMI1", "name": "HDMI 1"},
    {"id": "HDMI2", "name": "HDMI 2"},
    {"id": "HDMI3", "name": "HDMI 3"},
    {"id": "AVS", "name": "AV"},
]


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_id(value: Any, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return text or fallback


def _resolve_path(value: Any, base_dir: Path) -> str:
    path = Path(str(value or "").strip())
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


@dataclass(frozen=True)
class HisenseTvConfig:
    id: str
    name: str
    host: str
    mac: str
    enabled: bool = True
    port: int = 36669
    auth_mode: str = "auto"
    certificate_profile: str = "auto"

    @classmethod
    def from_mapping(cls, value: dict[str, Any], index: int) -> "HisenseTvConfig":
        fallback = f"tv-{index + 1}"
        ident = _safe_id(value.get("id") or value.get("name"), fallback)
        return cls(
            id=ident,
            name=str(value.get("name") or ident).strip(),
            host=str(value.get("host") or value.get("ip") or "").strip(),
            mac=str(value.get("mac") or "").strip().lower().replace("-", ":"),
            enabled=_bool(value.get("enabled"), True),
            port=max(1, min(65535, int(value.get("port") or 36669))),
            auth_mode=str(value.get("auth_mode") or "auto").strip().lower(),
            certificate_profile=_safe_id(value.get("certificate_profile"), "auto")
            if str(value.get("certificate_profile") or "auto").strip().lower() != "auto"
            else "auto",
        )


@dataclass(frozen=True)
class HisenseCertificateProfile:
    id: str
    name: str
    certfile: str
    keyfile: str
    compatible_models: str = ""
    enabled: bool = True

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any],
        index: int,
        base_dir: Path,
    ) -> "HisenseCertificateProfile":
        fallback = f"profile-{index + 1}"
        ident = _safe_id(value.get("id") or value.get("name"), fallback)
        return cls(
            id=ident,
            name=str(value.get("name") or ident).strip(),
            certfile=_resolve_path(value.get("cert_path") or value.get("certfile"), base_dir),
            keyfile=_resolve_path(value.get("key_path") or value.get("keyfile"), base_dir),
            compatible_models=str(value.get("compatible_models") or "").strip(),
            enabled=_bool(value.get("enabled"), True),
        )


@dataclass(frozen=True)
class HisenseTvGroup:
    id: str
    name: str
    tv_ids: tuple[str, ...]
    enabled: bool = True

    @classmethod
    def from_mapping(cls, value: dict[str, Any], index: int) -> "HisenseTvGroup":
        fallback = f"group-{index + 1}"
        ident = _safe_id(value.get("id") or value.get("name"), fallback)
        raw_ids = value.get("tv_ids") if isinstance(value.get("tv_ids"), list) else []
        seen: set[str] = set()
        tv_ids: list[str] = []
        for raw_id in raw_ids:
            tv_id = _safe_id(raw_id, "")
            if tv_id and tv_id not in seen:
                seen.add(tv_id)
                tv_ids.append(tv_id)
        return cls(
            id=ident,
            name=str(value.get("name") or ident).strip(),
            tv_ids=tuple(tv_ids),
            enabled=_bool(value.get("enabled"), True),
        )


@dataclass(frozen=True)
class HisenseConfig:
    enabled: bool
    certfile: str
    keyfile: str
    poll_interval: float
    reconnect_interval: float
    tvs: tuple[HisenseTvConfig, ...]
    certificate_profiles: tuple[HisenseCertificateProfile, ...]
    groups: tuple[HisenseTvGroup, ...]
    compatible_models: str

    @classmethod
    def from_mapping(cls, cfg: dict[str, Any] | None, base_dir: Path | None = None) -> "HisenseConfig":
        cfg = cfg or {}
        root = (base_dir or Path.cwd()).resolve()
        raw_tvs = cfg.get("hisense_tvs") if isinstance(cfg.get("hisense_tvs"), list) else []
        tvs = tuple(HisenseTvConfig.from_mapping(v, i) for i, v in enumerate(raw_tvs) if isinstance(v, dict))
        raw_profiles = cfg.get("hisense_certificate_profiles")
        if not isinstance(raw_profiles, list) or not raw_profiles:
            raw_profiles = [{
                "id": "default",
                "name": "Default / legacy",
                "cert_path": cfg.get("hisense_cert_path") or "hisense_certs/vidaa_client.pem",
                "key_path": cfg.get("hisense_key_path") or "hisense_certs/vidaa_client.key",
                "compatible_models": cfg.get("hisense_compatible_models") or "55A7G, 65A7G",
                "enabled": True,
            }]
        profiles = tuple(
            HisenseCertificateProfile.from_mapping(v, i, root)
            for i, v in enumerate(raw_profiles)
            if isinstance(v, dict)
        )
        raw_groups = cfg.get("hisense_tv_groups") if isinstance(cfg.get("hisense_tv_groups"), list) else []
        groups = tuple(HisenseTvGroup.from_mapping(v, i) for i, v in enumerate(raw_groups) if isinstance(v, dict))
        return cls(
            enabled=_bool(cfg.get("hisense_enabled"), False),
            certfile=_resolve_path(cfg.get("hisense_cert_path") or "hisense_certs/vidaa_client.pem", root),
            keyfile=_resolve_path(cfg.get("hisense_key_path") or "hisense_certs/vidaa_client.key", root),
            poll_interval=max(2.0, float(cfg.get("hisense_poll_interval") or 10.0)),
            reconnect_interval=max(2.0, float(cfg.get("hisense_reconnect_interval") or 15.0)),
            tvs=tvs,
            certificate_profiles=profiles,
            groups=groups,
            compatible_models=str(cfg.get("hisense_compatible_models") or "Confirmed: 55A7G, 65A7G").strip(),
        )


@dataclass
class _Command:
    action: str
    value: Any = None
    done: threading.Event | None = None
    result: dict[str, Any] | None = None


class HisenseTvController:
    def __init__(
        self,
        config: HisenseTvConfig,
        service_config: HisenseConfig,
        *,
        client_factory: Callable[..., Any] | None = None,
        wake_function: Callable[[str, str | None], bool] | None = None,
        protocol_detector: Callable[..., int | None] | None = None,
    ) -> None:
        self.config = config
        self.service_config = service_config
        self._client_factory = client_factory or VidaaTV
        self._wake = wake_function or wake_tv
        self._protocol_detector = (
            protocol_detector
            if protocol_detector is not None
            else (detect_protocol if client_factory is None else None)
        )
        self._client: Any = None
        self._queue: queue.Queue[_Command] = queue.Queue()
        self._stop = threading.Event()
        self._wake_worker = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._last_connect_attempt = 0.0
        self._last_poll = 0.0
        self._pending_power_on = False
        self._protocol_checked = False
        self._selected_dynamic_auth = False
        self._state: dict[str, Any] = {
            "connected": False,
            "power": "unknown",
            "volume": None,
            "muted": False,
            "source": "",
            "sources": list(DEFAULT_SOURCES),
            "model": "",
            "lastError": "",
            "lastSeen": None,
            "pairing": False,
            "protocolVersion": None,
            "authMethod": "",
            "certificateProfile": "",
            "compatibilityStatus": "Not connected",
        }

    def start(self) -> None:
        if not self.config.enabled or not self.config.host:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=f"hisense-{self.config.id}", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake_worker.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._disconnect()

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["sources"] = [dict(source) for source in self._state.get("sources", [])]
        return {
            "id": self.config.id,
            "name": self.config.name,
            "host": self.config.host,
            "mac": self.config.mac,
            "enabled": self.config.enabled,
            "authMode": self.config.auth_mode,
            "configuredCertificateProfile": self.config.certificate_profile,
            **state,
        }

    def submit(self, action: str, value: Any = None, *, wait: float = 0.0) -> dict[str, Any]:
        if not self.config.enabled:
            raise ValueError("TV is disabled")
        command = _Command(action=action, value=value, done=threading.Event() if wait > 0 else None)
        self._queue.put(command)
        self._wake_worker.set()
        if command.done is None:
            return {"ok": True, "accepted": True}
        if not command.done.wait(wait):
            return {"ok": True, "accepted": True, "pending": True}
        return command.result or {"ok": False, "error": "TV command failed"}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                command = self._queue.get_nowait()
            except queue.Empty:
                command = None

            if command is not None:
                self._execute_command(command)
                continue

            now = time.monotonic()
            if not self._is_connected():
                if now - self._last_connect_attempt >= self.service_config.reconnect_interval:
                    self._connect()
            elif now - self._last_poll >= self.service_config.poll_interval:
                self._poll()
            self._wake_worker.wait(0.5)
            self._wake_worker.clear()

    def _detect_protocol_version(self) -> int | None:
        if self._protocol_checked:
            value = self._state.get("protocolVersion")
            return int(value) if isinstance(value, int) else None
        self._protocol_checked = True
        version = None
        if self._protocol_detector is not None:
            try:
                version = self._protocol_detector(self.config.host, timeout=1.5, retries=0)
            except TypeError:
                version = self._protocol_detector(self.config.host)
            except Exception:
                version = None
        with self._lock:
            self._state["protocolVersion"] = version
        return version

    def _certificate_candidates(self) -> list[HisenseCertificateProfile]:
        profiles = [profile for profile in self.service_config.certificate_profiles if profile.enabled]
        if self.config.certificate_profile == "auto":
            return profiles
        return [profile for profile in profiles if profile.id == self.config.certificate_profile]

    def _auth_candidates(self, protocol_version: int | None) -> list[tuple[bool, Any, str]]:
        mode = self.config.auth_mode
        if mode == "static-legacy":
            return [(False, None, "static-legacy")]
        named = {
            "dynamic-legacy": getattr(AuthMethod, "LEGACY", None),
            "dynamic-middle": getattr(AuthMethod, "MIDDLE", None),
            "dynamic-modern": getattr(AuthMethod, "MODERN", None),
        }
        if mode in named:
            return [(True, named[mode], mode)]
        if protocol_version is not None and get_auth_method is not None:
            method = get_auth_method(protocol_version)
            label = f"dynamic-{getattr(method, 'value', 'auto')}"
            if protocol_version < 3000:
                return [(False, None, "static-legacy"), (True, method, label)]
            return [(True, method, label), (False, None, "static-legacy")]
        result: list[tuple[bool, Any, str]] = [(False, None, "static-legacy")]
        if AuthMethod is not None:
            result.extend([
                (True, AuthMethod.MODERN, "dynamic-modern"),
                (True, AuthMethod.MIDDLE, "dynamic-middle"),
                (True, AuthMethod.LEGACY, "dynamic-legacy"),
            ])
        return result

    def _make_client(
        self,
        profile: HisenseCertificateProfile,
        *,
        use_dynamic_auth: bool,
        auth_method: Any,
    ) -> Any:
        if self._client_factory is None:
            raise RuntimeError("pyvidaa is not installed")
        if not Path(profile.certfile).is_file() or not Path(profile.keyfile).is_file():
            raise RuntimeError(f'certificate profile "{profile.name}" files were not found')
        # Keep the protocol client name stable even if an administrator later
        # renames the TV in TDeck.  This also preserves approvals created by
        # the earlier bridge, which used companion_<ip-with-underscores>.
        protocol_client_id = f"companion_{re.sub(r'[^a-zA-Z0-9]+', '_', self.config.host).strip('_')}"
        return self._client_factory(
            host=self.config.host,
            port=self.config.port,
            client_id=protocol_client_id,
            use_ssl=True,
            verify_ssl=False,
            certfile=profile.certfile,
            keyfile=profile.keyfile,
            # Modern VIDAA generations issue access/refresh tokens. Persist
            # them through pyvidaa so a TDeck restart does not require repair.
            enable_persistence=use_dynamic_auth,
            use_dynamic_auth=use_dynamic_auth,
            auth_method=auth_method,
            auto_detect_protocol=False,
            mac_address=self.config.mac or None,
            on_state_change=self._on_state_change,
        )

    def _connect(self) -> bool:
        self._last_connect_attempt = time.monotonic()
        self._disconnect(clear_error=False)
        protocol_version = self._detect_protocol_version()
        profiles = self._certificate_candidates()
        if not profiles:
            self._set_error("No enabled certificate profile is available for this TV")
            return False
        errors: list[str] = []
        for profile in profiles:
            for dynamic, method, label in self._auth_candidates(protocol_version):
                client = None
                try:
                    client = self._make_client(profile, use_dynamic_auth=dynamic, auth_method=method)
                    if not client.connect(timeout=3.0, auto_auth=False, try_fallback=False):
                        raise RuntimeError("connection timed out or authentication was rejected")
                    self._client = client
                    self._selected_dynamic_auth = dynamic
                    with self._lock:
                        self._state.update({
                            "connected": True,
                            "lastError": "",
                            "lastSeen": time.time(),
                            "authMethod": label,
                            "certificateProfile": profile.id,
                            "compatibilityStatus": "Connected",
                        })
                    if self._pending_power_on and self._client.power_on():
                        self._pending_power_on = False
                    self._poll(full=True)
                    return True
                except Exception as exc:
                    errors.append(f"{profile.name} / {label}: {exc}")
                    if client is not None:
                        try:
                            client.disconnect()
                        except Exception:
                            pass
        message = "; ".join(errors[-4:]) or "connection failed"
        self._set_error(message)
        with self._lock:
            self._state["compatibilityStatus"] = "No compatible connection profile found"
        self._disconnect(clear_error=False)
        return False

    def _disconnect(self, *, clear_error: bool = False) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        with self._lock:
            self._state["connected"] = False
            if clear_error:
                self._state["lastError"] = ""

    def _is_connected(self) -> bool:
        with self._lock:
            client = self._client
            transport_connected = getattr(client, "is_connected", True) if client is not None else False
            return bool(self._state.get("connected") and client is not None and transport_connected)

    def _ensure_connected(self) -> Any:
        if not self._is_connected() and not self._connect():
            raise RuntimeError(self.status().get("lastError") or "TV is offline")
        return self._client

    def _poll(self, *, full: bool = False) -> None:
        self._last_poll = time.monotonic()
        try:
            client = self._ensure_connected()
            if self._pending_power_on and client.power_on():
                self._pending_power_on = False
            state = client.get_state(timeout=2.0) or {}
            volume = client.get_volume(timeout=2.0)
            updates: dict[str, Any] = {
                "power": "off" if state.get("statetype") == "fake_sleep_0" else ("on" if state else "unknown"),
                "volume": volume,
                "muted": bool(getattr(client, "is_muted", False)),
                "lastSeen": time.time(),
                "lastError": "",
                "connected": True,
            }
            source = state.get("sourceid") or state.get("sourcename")
            if source:
                updates["source"] = str(source)
            if full:
                sources = client.get_sources(timeout=2.0)
                if isinstance(sources, list) and sources:
                    updates["sources"] = self._normalize_sources(sources)
                info = client.get_device_info(timeout=2.0) or client.get_tv_info(timeout=2.0) or {}
                updates["model"] = str(info.get("model_name") or info.get("modelName") or info.get("model") or "")
            with self._lock:
                self._state.update(updates)
        except Exception as exc:
            self._set_error(str(exc))
            self._disconnect(clear_error=False)

    @staticmethod
    def _normalize_sources(sources: list[Any]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in sources:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("sourceid") or item.get("id") or item.get("sourcename") or "").strip()
            if not ident:
                continue
            name = str(item.get("displayname") or item.get("sourcename") or item.get("name") or ident).strip()
            result.append({"id": ident, "name": name})
        return result or list(DEFAULT_SOURCES)

    def _on_state_change(self, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            return
        updates: dict[str, Any] = {"lastSeen": time.time(), "connected": True}
        if state.get("statetype"):
            updates["power"] = "off" if state.get("statetype") == "fake_sleep_0" else "on"
        if state.get("sourceid") or state.get("sourcename"):
            updates["source"] = str(state.get("sourceid") or state.get("sourcename"))
        with self._lock:
            self._state.update(updates)

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._state["lastError"] = str(message or "Unknown TV error")

    def _execute_command(self, command: _Command) -> None:
        try:
            action, value = command.action, command.value
            if action == "reconnect":
                self._disconnect(clear_error=True)
                self._protocol_checked = False
                ok = self._connect()
            elif action == "power_on":
                if not self.config.mac:
                    raise ValueError("A MAC address is required for power on")
                subnet = self.config.host.rsplit(".", 1)[0] if self.config.host.count(".") == 3 else None
                if self._wake is None or not self._wake(self.config.mac, subnet):
                    raise RuntimeError("Wake-on-LAN packet could not be sent")
                self._pending_power_on = True
                self._disconnect(clear_error=True)
                self._last_connect_attempt = 0.0
                ok = True
            else:
                client = self._ensure_connected()
                if action == "power_off":
                    ok = bool(client.power_off())
                elif action == "power_toggle":
                    ok = bool(client.power_off()) if self.status().get("power") == "on" else self._power_on_connected(client)
                elif action == "volume_set":
                    level = max(0, min(100, int(value)))
                    ok = bool(client.set_volume(level))
                    if ok:
                        with self._lock:
                            self._state["volume"] = level
                elif action == "volume_up":
                    ok = bool(client.volume_up())
                elif action == "volume_down":
                    ok = bool(client.volume_down())
                elif action == "mute":
                    ok = bool(client.mute())
                elif action == "source":
                    ok = self._set_source(client, str(value or ""))
                elif action == "pair_request":
                    ok = bool(client.start_pairing())
                    with self._lock:
                        self._state["pairing"] = ok
                elif action == "pair_submit":
                    ok = self._authenticate_pin(client, str(value or ""))
                    with self._lock:
                        self._state["pairing"] = not ok
                else:
                    raise ValueError(f"Unknown TV command: {action}")
            if not ok:
                raise RuntimeError("TV rejected the command")
            command.result = {"ok": True, "accepted": True, "tv": self.status()}
        except Exception as exc:
            self._set_error(str(exc))
            command.result = {"ok": False, "error": str(exc), "tv": self.status()}
        finally:
            if command.done:
                command.done.set()

    def _power_on_connected(self, client: Any) -> bool:
        if self.config.mac and self._wake is not None:
            subnet = self.config.host.rsplit(".", 1)[0] if self.config.host.count(".") == 3 else None
            self._wake(self.config.mac, subnet)
        return bool(client.power_on())

    def _set_source(self, client: Any, requested: str) -> bool:
        needle = requested.strip().lower().replace(" ", "")
        if not needle:
            raise ValueError("Source is required")
        source_id = requested.strip()
        for source in self.status().get("sources", []):
            candidates = {
                str(source.get("id") or "").lower().replace(" ", ""),
                str(source.get("name") or "").lower().replace(" ", ""),
            }
            if needle in candidates:
                source_id = str(source.get("id"))
                break
        if get_topic is None:
            return bool(client.set_source(source_id))
        topic = get_topic(TOPIC_SET_SOURCE, client.client_id)
        ok = bool(client._publish(topic, {"sourceid": source_id}))
        if ok:
            with self._lock:
                self._state["source"] = source_id
        return ok

    @staticmethod
    def _authenticate_legacy(client: Any, pin: str) -> bool:
        if not re.fullmatch(r"\d{4}", pin):
            raise ValueError("PIN must be exactly four digits")
        # A7G firmware accepts the PIN but does not issue an access token.  The
        # library exposes the accepted state immediately; waiting for a token
        # would incorrectly report failure after ten seconds.
        if not client.authenticate(pin, wait_for_response=False):
            return False
        event = getattr(client, "_auth_event", None)
        if event is not None:
            event.wait(5.0)
        authenticated = bool(client.is_authenticated())
        if authenticated and hasattr(client, "_request_token"):
            client._request_token()
        return authenticated

    def _authenticate_pin(self, client: Any, pin: str) -> bool:
        if not self._selected_dynamic_auth:
            return self._authenticate_legacy(client, pin)
        if not re.fullmatch(r"\d{4}", pin):
            raise ValueError("PIN must be exactly four digits")
        return bool(client.authenticate(pin, wait_for_response=True, timeout=8.0))


class HisenseManager:
    def __init__(
        self,
        config: HisenseConfig,
        *,
        client_factory: Callable[..., Any] | None = None,
        wake_function: Callable[[str, str | None], bool] | None = None,
        protocol_detector: Callable[..., int | None] | None = None,
    ) -> None:
        self.config = config
        self.controllers = {
            tv.id: HisenseTvController(
                tv,
                config,
                client_factory=client_factory,
                wake_function=wake_function,
                protocol_detector=protocol_detector,
            )
            for tv in config.tvs
        }

    def start(self) -> None:
        if self.config.enabled:
            for controller in self.controllers.values():
                controller.start()

    def close(self) -> None:
        for controller in self.controllers.values():
            controller.close()

    def get(self, tv_id: str) -> HisenseTvController:
        controller = self.controllers.get(str(tv_id or "").strip().lower())
        if controller is None:
            raise KeyError(f"Unknown TV: {tv_id}")
        return controller

    @staticmethod
    def _common(values: list[Any], *, empty: Any = "") -> Any:
        if not values or all(value in (None, "") for value in values):
            return empty
        return values[0] if all(value == values[0] for value in values) else "mixed"

    def group_status(self, group: HisenseTvGroup) -> dict[str, Any]:
        members = [
            self.controllers[tv_id].status()
            for tv_id in group.tv_ids
            if tv_id in self.controllers
        ]
        enabled = [tv for tv in members if tv.get("enabled")]
        online = [tv for tv in enabled if tv.get("connected")]
        source_lists: list[dict[str, str]] = []
        seen_sources: set[str] = set()
        for tv in members:
            for source in tv.get("sources") or []:
                source_id = str(source.get("id") or "").strip()
                if source_id and source_id not in seen_sources:
                    seen_sources.add(source_id)
                    source_lists.append(dict(source))
        volume = self._common([tv.get("volume") for tv in enabled], empty=None)
        errors = [
            f"{tv.get('name') or tv.get('id')}: {tv.get('lastError')}"
            for tv in enabled
            if tv.get("lastError")
        ]
        return {
            "id": group.id,
            "targetId": f"group:{group.id}",
            "type": "group",
            "name": group.name,
            "enabled": group.enabled,
            "tvIds": list(group.tv_ids),
            "tvs": members,
            "connected": bool(enabled) and len(online) == len(enabled),
            "online": len(online),
            "total": len(enabled),
            "power": self._common([tv.get("power") for tv in enabled], empty="unknown"),
            "volume": volume if volume != "mixed" else None,
            "volumeState": volume,
            "muted": bool(enabled) and all(bool(tv.get("muted")) for tv in enabled),
            "source": self._common([tv.get("source") for tv in enabled], empty=""),
            "sources": source_lists or list(DEFAULT_SOURCES),
            "lastError": "; ".join(errors),
        }

    def get_group(self, group_id: str) -> HisenseTvGroup:
        needle = str(group_id or "").strip().lower()
        for group in self.config.groups:
            if group.id == needle:
                return group
        raise KeyError(f"Unknown TV group: {group_id}")

    def target_status(self, target_id: str) -> dict[str, Any]:
        raw = str(target_id or "").strip()
        kind, separator, ident = raw.partition(":")
        if separator and kind == "group":
            return self.group_status(self.get_group(ident))
        if separator and kind == "tv":
            status = self.get(ident).status()
        else:
            status = self.get(raw).status()
        return {"targetId": f"tv:{status['id']}", "type": "tv", **status}

    def submit_target(
        self,
        target_id: str,
        action: str,
        value: Any = None,
        *,
        wait: float = 0.0,
    ) -> dict[str, Any]:
        raw = str(target_id or "").strip()
        kind, separator, ident = raw.partition(":")
        if not (separator and kind == "group"):
            tv_id = ident if separator and kind == "tv" else raw
            return self.get(tv_id).submit(action, value, wait=wait)
        group = self.get_group(ident)
        if not group.enabled:
            raise ValueError("TV group is disabled")
        controllers = [
            self.controllers[tv_id]
            for tv_id in group.tv_ids
            if tv_id in self.controllers and self.controllers[tv_id].config.enabled
        ]
        if not controllers:
            raise ValueError("TV group has no enabled TVs")
        results = []
        for controller in controllers:
            try:
                result = controller.submit(action, value, wait=0.0)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            results.append({"tvId": controller.config.id, **result})
        ok = all(result.get("ok") for result in results)
        return {
            "ok": ok,
            "accepted": ok,
            "target": self.group_status(group),
            "results": results,
            **({"error": "One or more group commands were rejected"} if not ok else {}),
        }

    def status(self) -> dict[str, Any]:
        tvs = [controller.status() for controller in self.controllers.values()]
        enabled = [tv for tv in tvs if tv.get("enabled")]
        online = [tv for tv in enabled if tv.get("connected")]
        groups = [self.group_status(group) for group in self.config.groups]
        targets = [
            {
                "targetId": f"group:{group['id']}",
                "type": "group",
                "id": group["id"],
                "name": group["name"],
            }
            for group in groups
            if group.get("enabled")
        ]
        targets.extend(
            {
                "targetId": f"tv:{tv['id']}",
                "type": "tv",
                "id": tv["id"],
                "name": tv["name"],
            }
            for tv in tvs
            if tv.get("enabled")
        )
        return {
            "ok": True,
            "enabled": self.config.enabled,
            "available": VidaaTV is not None or any(c._client_factory is not None for c in self.controllers.values()),
            "configured": bool(enabled),
            "connected": bool(enabled) and len(online) == len(enabled),
            "online": len(online),
            "total": len(enabled),
            "tvs": tvs,
            "groups": groups,
            "targets": targets,
            "compatibleModels": self.config.compatible_models,
        }


_MANAGER_LOCK = threading.Lock()
_MANAGER: HisenseManager | None = None
_MANAGER_SIGNATURE = ""


def _signature(config: HisenseConfig) -> str:
    return json.dumps(
        {
            "enabled": config.enabled,
            "certfile": config.certfile,
            "keyfile": config.keyfile,
            "poll": config.poll_interval,
            "reconnect": config.reconnect_interval,
            "tvs": [tv.__dict__ for tv in config.tvs],
            "profiles": [profile.__dict__ for profile in config.certificate_profiles],
            "groups": [group.__dict__ for group in config.groups],
            "compatible_models": config.compatible_models,
        },
        sort_keys=True,
    )


def get_hisense_manager_from_config(cfg: dict[str, Any] | None, *, base_dir: Path | None = None) -> HisenseManager:
    global _MANAGER, _MANAGER_SIGNATURE
    config = HisenseConfig.from_mapping(cfg, base_dir=base_dir)
    signature = _signature(config)
    with _MANAGER_LOCK:
        if _MANAGER is not None and signature == _MANAGER_SIGNATURE:
            _MANAGER.start()
            return _MANAGER
        previous = _MANAGER
        _MANAGER = HisenseManager(config)
        _MANAGER_SIGNATURE = signature
    if previous is not None:
        previous.close()
    _MANAGER.start()
    return _MANAGER


def close_hisense_manager() -> None:
    global _MANAGER, _MANAGER_SIGNATURE
    with _MANAGER_LOCK:
        manager = _MANAGER
        _MANAGER = None
        _MANAGER_SIGNATURE = ""
    if manager is not None:
        manager.close()
