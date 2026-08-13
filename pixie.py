"""Direct SAL Pixie Plus Gateway integration for TDeck.

The local Pixie protocol is unofficial and reverse-engineered.  TDeck only
sends commands to validated physical-device IDs or inventory scene IDs.  It
never sends the Gateway's native group-address packet: TDeck auditoriums are
logical groups that fan out as individual device commands.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
except Exception:  # pragma: no cover - exercised through the unavailable status
    AES = None  # type: ignore[assignment]
    pad = None  # type: ignore[assignment]
    unpad = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)

PIXIE_DISCOVERY_PORT = 41580
PIXIE_CONTROL_PORT = 41578
PIXIE_INVENTORY_PORT = 53216
PIXIE_REACHABILITY_INTERVAL = 1.0
PIXIE_REACHABILITY_STALE_AFTER = 30.0
PIXIE_COMMAND_FEEDBACK_GRACE = 4.0
PIXIE_FLAG_DUAL_DATA = 0
PIXIE_FLAG_SINGLE_DATA = 1
PIXIE_FLAG_EACK = 2
PIXIE_FLAG_HEARTBEAT = 5
PIXIE_IV = b"0" * 16

PIXIE_CLOUD_BASE_URL = "https://www.pixie.app/p0/pixieCloud"
PIXIE_APPLICATION_ID = "6426f04c206c108275ede71b9fd09ac8"
PIXIE_CLIENT_KEY = "35779bd411c751ff87577cd762118dad"


class PixieError(RuntimeError):
    """Raised when Pixie discovery, authentication, inventory or control fails."""


def _require_crypto() -> None:
    if AES is None or pad is None or unpad is None:
        raise PixieError("Pixie support requires the pycryptodome Python package")


def _padded_aes_key(value: str) -> bytes:
    source = str(value).encode("utf-8")
    length = len(source) if len(source) % 16 == 0 else ((len(source) // 16) + 1) * 16
    if length not in (16, 24, 32):
        raise PixieError(f"Pixie AES key expands to unsupported length {length}")
    return source.ljust(length, b"\x00")


def encrypt_pixie_payload(plaintext: str, key: str) -> bytes:
    _require_crypto()
    cipher = AES.new(_padded_aes_key(key), AES.MODE_CBC, iv=PIXIE_IV)
    return cipher.encrypt(pad(str(plaintext).encode("utf-8"), AES.block_size))


def decrypt_pixie_payload(ciphertext: bytes, key: str) -> str:
    _require_crypto()
    cipher = AES.new(_padded_aes_key(key), AES.MODE_CBC, iv=PIXIE_IV)
    try:
        return unpad(cipher.decrypt(bytes(ciphertext)), AES.block_size).decode("utf-8")
    except Exception as exc:
        raise PixieError("Pixie encrypted payload could not be authenticated") from exc


def encode_single_envelope(payload: Any, key: str, flag: int = PIXIE_FLAG_SINGLE_DATA) -> bytes:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return bytes([int(flag) & 0xFF]) + encrypt_pixie_payload(raw, key)


def decrypt_dual_handshake(envelope: bytes, net_id: str) -> tuple[str, str]:
    if len(envelope) != 34 or envelope[0] != PIXIE_FLAG_DUAL_DATA:
        raise PixieError("Pixie handshake is not a 34-byte dual envelope")
    session_key = decrypt_pixie_payload(envelope[1:17], net_id)
    mesh_value = decrypt_pixie_payload(envelope[18:34], session_key)
    return session_key, mesh_value


def derive_inventory_key(unix_seconds: int, net_id: str | int) -> bytes:
    xor_value = (int(net_id) ^ int(unix_seconds)) & 0xFFFFFFFFFFFFFFFF
    return f"Pixie{xor_value:x}".encode("ascii")[:16].ljust(16, b"\x00")


def derive_inventory_nonce(unix_seconds: int, mesh_net_2: str | int) -> int:
    return ((int(unix_seconds) & 0xFFFFFFFF) ^ (int(mesh_net_2) & 0xFFFFFFFF)) & 0xFFFFFFFF


def build_inventory_request(unix_seconds: int, net_id: str | int, mesh_net_2: str | int) -> bytes:
    _require_crypto()
    key = derive_inventory_key(unix_seconds, net_id)
    cipher = AES.new(key, AES.MODE_CBC, iv=PIXIE_IV)
    encrypted = cipher.encrypt(pad(b'{"get":{"selected":127}}', AES.block_size))
    encoded = base64.b64encode(bytes([1]) + encrypted).decode("ascii")
    nonce = derive_inventory_nonce(unix_seconds, mesh_net_2)
    return f"ea{len(encoded):08x}{nonce:08x}{encoded}".encode("ascii")


def parse_inventory_response_frames(raw: bytes) -> list[str]:
    text = bytes(raw).decode("ascii", errors="ignore")
    payloads: list[str] = []
    offset = 0
    while offset < len(text):
        marker = text.find("eb", offset)
        if marker < 0 or marker + 10 > len(text):
            break
        length_text = text[marker + 2:marker + 10]
        if not re.fullmatch(r"[0-9a-fA-F]{8}", length_text):
            offset = marker + 2
            continue
        length = int(length_text, 16)
        start = marker + 10
        end = start + length
        if end > len(text):
            break
        payloads.append(text[start:end])
        offset = end
    return payloads


def _decode_inventory_json(plaintext: bytes) -> Any:
    direct = plaintext.decode("utf-8", errors="replace")
    if direct.lstrip().startswith("{"):
        return json.loads(direct)
    for candidate in (plaintext, plaintext[1:]):
        try:
            return json.loads(base64.b64decode(candidate.decode("ascii")).decode("utf-8"))
        except Exception:
            continue
    raise PixieError("Pixie inventory response did not contain recognizable JSON")


def decrypt_inventory_response(payload_base64: str, unix_seconds: int, net_id: str | int) -> Any:
    _require_crypto()
    try:
        ciphertext = base64.b64decode(re.sub(r"\s+", "", str(payload_base64)), validate=True)
    except Exception as exc:
        raise PixieError("Pixie inventory response is not valid base64") from exc
    if not ciphertext or len(ciphertext) % 16:
        raise PixieError("Pixie inventory ciphertext has invalid length")
    cipher = AES.new(derive_inventory_key(unix_seconds, net_id), AES.MODE_CBC, iv=PIXIE_IV)
    try:
        plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)
    except Exception as exc:
        raise PixieError("Pixie inventory response could not be decrypted") from exc
    return _decode_inventory_json(plaintext)


def _decode_inventory_frames(
    raw: bytes,
    requested_at: int,
    net_id: str | int,
) -> dict[str, Any]:
    frames = parse_inventory_response_frames(raw)
    attempts = ["".join(frames), *frames] if len(frames) > 1 else frames
    last_error: Exception | None = None
    for frame in attempts:
        try:
            value = decrypt_inventory_response(frame, requested_at, net_id)
            if not isinstance(value, dict):
                raise PixieError("Decrypted Pixie response is not an inventory")
            inventory = value.get("data") if isinstance(value.get("data"), dict) else value
            if not isinstance(inventory.get("deviceList"), list):
                raise PixieError("Decrypted Pixie response is not an inventory")
            return {"requestedAt": requested_at, "raw": value, "inventory": inventory}
        except Exception as exc:
            last_error = exc
    raise PixieError(str(last_error or "Pixie Gateway returned no complete inventory frames"))


def fetch_local_inventory(
    host: str,
    net_id: str | int,
    mesh_net_2: str | int,
    *,
    timeout: float = 5.0,
    port: int = PIXIE_INVENTORY_PORT,
) -> dict[str, Any]:
    requested_at = int(time.time())
    request_payload = build_inventory_request(requested_at, net_id, mesh_net_2)
    chunks: list[bytes] = []
    with socket.create_connection((str(host), int(port)), timeout=float(timeout)) as sock:
        sock.settimeout(float(timeout))
        sock.sendall(request_payload)
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            # The Gateway commonly keeps this short-lived socket open after it
            # has sent a complete response. Decode as data arrives so a valid
            # inventory is not delayed until the socket timeout expires.
            try:
                return _decode_inventory_frames(b"".join(chunks), requested_at, net_id)
            except PixieError:
                pass
    if not chunks:
        raise PixieError("Timed out waiting for Pixie local inventory")
    return _decode_inventory_frames(b"".join(chunks), requested_at, net_id)


def parse_gateway_advert(payload: bytes, host: str) -> dict[str, str] | None:
    try:
        value = json.loads(bytes(payload).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(value, dict) or value.get("type") != "GW":
        return None
    mesh_net = str(value.get("meshNet") or "").strip()
    mesh_net_2 = str(value.get("meshNet2") or "").strip()
    if not mesh_net and not mesh_net_2:
        return None
    return {
        "host": str(host),
        "type": "GW",
        "meshNet": mesh_net,
        "meshNet2": mesh_net_2,
        "from": str(value.get("from") or ""),
    }


def discover_gateways(timeout: float = 5.0) -> list[dict[str, str]]:
    """Passively listen for Gateway advertisements without transmitting."""

    found: dict[str, dict[str, str]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", PIXIE_DISCOVERY_PORT))
        sock.settimeout(max(0.1, float(timeout)))
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            try:
                payload, address = sock.recvfrom(65535)
            except socket.timeout:
                break
            advert = parse_gateway_advert(payload, address[0])
            if advert:
                found[advert["host"]] = advert
    finally:
        sock.close()
    return list(found.values())


def _cloud_headers(session_token: str = "") -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "x-parse-application-id": PIXIE_APPLICATION_ID,
        "x-parse-client-key": PIXIE_CLIENT_KEY,
    }
    if session_token:
        headers["x-parse-session-token"] = session_token
    return headers


def _checked_cloud_json(response: requests.Response, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if not response.ok:
        code = payload.get("code") if isinstance(payload, dict) else None
        if response.status_code == 403 or code == 101:
            raise PixieError("Pixie rejected the username or password")
        message = str(payload.get("error") or "") if isinstance(payload, dict) else ""
        raise PixieError(f"{context} failed with HTTP {response.status_code}{': ' + message if message else ''}")
    if not isinstance(payload, dict):
        raise PixieError(f"{context} returned an invalid JSON response")
    return payload


def _parse_cloud_home(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value.get("objectId"):
        return None
    gateway = value.get("gateway") if isinstance(value.get("gateway"), dict) else value.get("Gateway")
    gateway = gateway if isinstance(gateway, dict) else {}
    object_id = str(value["objectId"])
    return {
        "objectId": object_id,
        "name": str(value.get("name") or object_id),
        "netId": str(value.get("netID") or value.get("netId") or ""),
        "meshNet": str(value.get("meshNet") or ""),
        "meshNet2": str(value.get("meshNet2") or ""),
        "gatewayId": str(value.get("gatewayID") or value.get("gatewayId") or gateway.get("objectId") or gateway.get("id") or ""),
        "devices": value.get("deviceList") if isinstance(value.get("deviceList"), list) else [],
        "groups": value.get("groupList") if isinstance(value.get("groupList"), list) else [],
        "scenes": value.get("sceneList") if isinstance(value.get("sceneList"), list) else [],
    }


def _login_pixie_cloud(username: str, password: str, *, timeout: float = 10.0) -> dict[str, Any]:
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        raise PixieError("Enter the Pixie account email and password")
    response = requests.post(
        f"{PIXIE_CLOUD_BASE_URL}/login",
        headers={
            **_cloud_headers(),
            "x-parse-installation-id": "tdeck-pixie-controls",
            "x-parse-revocable-session": "1",
        },
        json={"username": username, "password": password},
        timeout=float(timeout),
    )
    login = _checked_cloud_json(response, "Pixie login")
    if not login.get("sessionToken") or not login.get("objectId"):
        raise PixieError("Pixie login response omitted account identity or session token")
    return login


def _cloud_online_entry_is_online(value: Any) -> bool:
    marker = value.get("online") if isinstance(value, dict) else value
    if marker is None:
        return isinstance(value, dict)
    if isinstance(marker, bool):
        return marker
    if isinstance(marker, (int, float)):
        return marker > 0
    if isinstance(marker, str):
        text = marker.strip().lower()
        if text in ("", "0", "false", "off", "offline"):
            return False
        try:
            return float(text) > 0
        except Exception:
            return text in ("true", "on", "online")
    return False


def parse_cloud_online_ids(payload: Any) -> set[str]:
    """Return online physical IDs from Pixie's live Home status maps."""
    return {
        ident for ident, state in parse_cloud_device_states(payload).items()
        if state["online"]
    }


def parse_cloud_device_states(payload: Any) -> dict[str, dict[str, Any]]:
    """Normalize live availability and level feedback from one Pixie Home."""
    if not isinstance(payload, dict):
        raise PixieError("Pixie Home status returned invalid JSON")
    # `onlineList` is the current map. `onlineList2` is retained as a
    # compatibility fallback and may contain an older level for the same node.
    primary = payload.get("onlineList") if isinstance(payload.get("onlineList"), dict) else None
    secondary = payload.get("onlineList2") if isinstance(payload.get("onlineList2"), dict) else None
    status_maps = [primary] if primary is not None else ([secondary] if secondary is not None else [])
    if not status_maps:
        raise PixieError("Pixie Home status omitted its live online lists")
    states: dict[str, dict[str, Any]] = {}
    for status_map in status_maps:
        for raw_id, value in status_map.items():
            ident = str(raw_id or "").strip()
            if not ident:
                continue
            record = value if isinstance(value, dict) else {}
            brightness = _first_number(record, ("br", "brightness"))
            if brightness is not None and not 0 <= brightness <= 100:
                brightness = None
            relay = _first_boolean(record, ("r", "relay", "on"))
            on_value = brightness > 0 if brightness is not None else relay
            states[ident] = {
                "online": _cloud_online_entry_is_online(value),
                "brightness": int(round(brightness)) if brightness is not None else None,
                "on": on_value,
            }
    return states


def fetch_pixie_cloud_reachability(
    username: str,
    password: str,
    home_id: str,
    *,
    session_token: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Fetch the small, live per-device status maps for one Pixie Home."""
    home_id = str(home_id or "").strip()
    if not home_id:
        raise PixieError("Select a Pixie Home before checking device availability")

    token = str(session_token or "")
    if not token:
        token = str(_login_pixie_cloud(username, password, timeout=timeout)["sessionToken"])

    def _read(current_token: str) -> requests.Response:
        return requests.get(
            f"{PIXIE_CLOUD_BASE_URL}/classes/Home/{home_id}",
            headers=_cloud_headers(current_token),
            params={"keys": "onlineList,onlineList2,updatedAt"},
            timeout=float(timeout),
        )

    response = _read(token)
    if response.status_code in (401, 403) and session_token:
        token = str(_login_pixie_cloud(username, password, timeout=timeout)["sessionToken"])
        response = _read(token)
    payload = _checked_cloud_json(response, "Pixie device availability")
    device_states = parse_cloud_device_states(payload)
    return {
        "sessionToken": token,
        "onlineIds": {ident for ident, state in device_states.items() if state["online"]},
        "deviceStates": device_states,
        "updatedAt": str(payload.get("updatedAt") or ""),
    }


def provision_pixie_cloud(username: str, password: str, *, timeout: float = 10.0) -> dict[str, Any]:
    login = _login_pixie_cloud(username, password, timeout=timeout)
    session_token = str(login.get("sessionToken") or "")
    user_id = str(login.get("objectId") or "")
    current_home = login.get("curHome") if isinstance(login.get("curHome"), dict) else {}

    homes: list[dict[str, Any]] = []
    limit = 100
    for skip in range(0, 10000, limit):
        response = requests.get(
            f"{PIXIE_CLOUD_BASE_URL}/classes/Home",
            headers=_cloud_headers(session_token),
            params={"where": "{}", "skip": skip, "limit": limit},
            timeout=float(timeout),
        )
        payload = _checked_cloud_json(response, "Pixie Home listing")
        batch = payload.get("results") if isinstance(payload.get("results"), list) else []
        homes.extend(home for raw in batch if (home := _parse_cloud_home(raw)) is not None)
        if len(batch) < limit:
            break
    return {
        "login": {
            "userId": user_id,
            "currentHomeId": str(current_home.get("objectId") or ""),
        },
        "homes": homes,
    }


def _first_string(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _first_number(record: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    value = _first_string(record, keys)
    if not value:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _first_boolean(record: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool):
            return value
        if value in (1, "1", "on", "ON"):
            return True
        if value in (0, "0", "off", "OFF"):
            return False
    return None


def _normalize_target(value: Any, target_type: str, index: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    ident = _first_string(value, (
        "objectId", "deviceID", "deviceId", "groupID", "groupId", "sceneID", "sceneId",
        "uuid", "address", "id", "ID",
    )) or f"{target_type}-{index + 1}"
    name = _first_string(value, ("name", "Name", "sName", "deviceName", "groupName", "sceneName", "label")) or ident
    return {"id": ident, "name": name, "type": target_type, "raw": value}


def normalize_targets(values: Any, target_type: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, value in enumerate(values if isinstance(values, list) else []):
        target = _normalize_target(value, target_type, index)
        if target:
            output.append(target)
    return output


def normalize_devices(values: Any) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for target in normalize_targets(values, "device"):
        raw = target["raw"]
        nested = raw.get("state") if isinstance(raw.get("state"), dict) else {}
        model = _first_string(raw, ("model", "modelNo", "modelNumber", "productModel", "productID"))
        type_text = _first_string(raw, ("deviceType", "type", "category", "kind")).lower()
        kind_text = f"{model} {type_text}".lower()
        if re.search(r"switch|plug|socket|relay|amp|^swl|^ess|^sp023|^pc206dr", kind_text):
            kind = "switch"
        elif re.search(r"dimmer|light|rgb|strip|^sdd|^flp|^lt8915", kind_text) or _first_number(nested, ("br",)) is not None:
            kind = "dimmer"
        else:
            kind = "unknown"
        raw_brightness = _first_number(nested, ("br",))
        brightness = _first_number(raw, ("brightness", "level", "dimLevel", "value"))
        if brightness is None and raw_brightness is not None:
            brightness = round((raw_brightness * 100) / 255)
        on_value = _first_boolean(raw, ("on", "isOn", "power", "state"))
        if on_value is None and raw_brightness is not None:
            on_value = raw_brightness > 0
        # Pixie calls an internal numeric mesh value `online` (for example 76),
        # even for devices that have been physically disconnected for years.
        # Only explicit boolean reachability fields are safe to present as a
        # live connection status. Otherwise status is unknown, not online.
        online: bool | None = None
        for key in ("isOnline", "reachable", "online"):
            if isinstance(raw.get(key), bool):
                online = raw[key]
                break
        devices.append({
            **target,
            "model": model,
            "kind": kind,
            "online": online,
            "on": on_value,
            "brightness": max(0, min(100, int(round(brightness)))) if brightness is not None else None,
        })
    return devices


def numeric_physical_device_id(device_id: str) -> int:
    text = str(device_id or "")
    if not re.fullmatch(r"\d+", text):
        raise PixieError(f"Unsafe Pixie physical device ID {text}")
    value = int(text)
    if value <= 0 or value >= 0x8000:
        raise PixieError(f"Unsafe Pixie physical device ID {text}")
    return value


def build_brightness_command_hex(device_id: str, brightness_percent: int, counter: int) -> str:
    device_number = numeric_physical_device_id(device_id)
    brightness_percent = int(brightness_percent)
    if brightness_percent < 0 or brightness_percent > 100:
        raise PixieError("Brightness must be a whole number from 0 to 100")
    sequence_value = int(counter) & 0xFF
    sequence = bytes([sequence_value, sequence_value >> 1, sequence_value >> 2])
    source = struct.pack("<H", 1027)
    destination = struct.pack("<H", device_number)
    brightness = min(255, max(0, round((brightness_percent * 256) / 100)))
    packet = sequence + source + bytes.fromhex("ffffe76969320010") + bytes([brightness, 0, 0]) + destination
    return packet.hex()


class PixieControlSession:
    """Authenticated persistent control socket with serialized writes."""

    def __init__(self, host: str, net_id: str, mesh_net: str, mesh_net_2: str, *, port: int = PIXIE_CONTROL_PORT):
        self.host = str(host)
        self.net_id = str(net_id)
        self.mesh_net = str(mesh_net or "")
        self.mesh_net_2 = str(mesh_net_2)
        self.port = int(port)
        self._socket: socket.socket | None = None
        self._session_key = ""
        self._ready = False
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        self._receiver_thread: threading.Thread | None = None

    @property
    def is_ready(self) -> bool:
        return bool(self._ready and self._socket is not None and self._session_key)

    def _read_handshake(self, sock: socket.socket, timeout: float) -> bytes:
        sock.settimeout(float(timeout))
        buffer = ""
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                raise PixieError("Pixie Gateway closed before authentication")
            buffer += re.sub(r"\s+", "", chunk.decode("ascii", errors="ignore"))
            if len(buffer) < 48:
                continue
            try:
                decoded = base64.b64decode(buffer[:48], validate=True)
            except Exception as exc:
                raise PixieError("Pixie Gateway sent an invalid handshake") from exc
            if len(decoded) == 34:
                return decoded
        raise PixieError("Timed out waiting for Pixie Gateway handshake")

    def start(self, timeout: float = 4.0) -> None:
        self.stop()
        sock = socket.create_connection((self.host, self.port), timeout=float(timeout))
        self._socket = sock
        try:
            envelope = self._read_handshake(sock, timeout)
            handshake: tuple[str, str] | None = None
            for candidate in dict.fromkeys((self.net_id, self.net_id.lstrip("0") or "0")):
                try:
                    handshake = decrypt_dual_handshake(envelope, candidate)
                    break
                except Exception:
                    continue
            if not handshake:
                raise PixieError("Pixie Gateway authentication failed for the configured Net ID")
            session_key, mesh_value = handshake
            expected = {value for value in (self.mesh_net, self.mesh_net_2) if value}
            if expected and mesh_value not in expected:
                raise PixieError("Pixie Gateway mesh authentication did not match this Home")
            self._session_key = session_key
            self._write_payload({"data": {"type": "GwData", "data": "fffe01010100000400003400d568"}})
            time.sleep(0.035)
            self._write_payload({"op": "ack", "code": 0}, PIXIE_FLAG_EACK)
            time.sleep(0.1)
            self._write_payload({"op": "ack", "code": 0}, PIXIE_FLAG_HEARTBEAT)
            sock.settimeout(1.8)
            try:
                confirmation = sock.recv(4096)
            except socket.timeout as exc:
                raise PixieError("Pixie Gateway did not confirm the local control session") from exc
            if not confirmation:
                raise PixieError("Pixie Gateway did not confirm the local control session")
            sock.settimeout(0.5)
            self._stop.clear()
            self._ready = True
            self._receiver_thread = threading.Thread(target=self._receiver_loop, daemon=True, name="pixie-receiver")
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name="pixie-heartbeat")
            self._receiver_thread.start()
            self._heartbeat_thread.start()
        except Exception:
            self.stop()
            raise

    def _receiver_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._socket
            if sock is None:
                return
            try:
                data = sock.recv(4096)
                if not data:
                    self._ready = False
                    return
            except socket.timeout:
                continue
            except OSError:
                self._ready = False
                return

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(10.0):
            if not self.is_ready:
                continue
            try:
                self._write_payload({"op": "ack", "code": 0}, PIXIE_FLAG_HEARTBEAT)
            except Exception:
                self._ready = False
                return

    def _write_payload(self, payload: Any, flag: int = PIXIE_FLAG_SINGLE_DATA) -> None:
        sock = self._socket
        if sock is None or not self._session_key:
            raise PixieError("Pixie local control socket is unavailable")
        envelope = base64.b64encode(encode_single_envelope(payload, self._session_key, flag))
        with self._write_lock:
            sock.sendall(envelope)

    def send(self, payload: Any) -> None:
        if not self.is_ready:
            raise PixieError("Pixie local control session is not authenticated and ready")
        self._write_payload(payload)

    def stop(self) -> None:
        self._ready = False
        self._stop.set()
        sock = self._socket
        self._socket = None
        self._session_key = ""
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass


@dataclass
class PixieSecrets:
    username: str = ""
    password: str = ""


def pixie_secrets_path(base_dir: Path) -> Path:
    return Path(base_dir) / "pixie_secrets.json"


def load_pixie_secrets(base_dir: Path) -> PixieSecrets:
    try:
        value = json.loads(pixie_secrets_path(base_dir).read_text(encoding="utf-8"))
    except Exception:
        value = {}
    return PixieSecrets(
        username=str(value.get("username") or "") if isinstance(value, dict) else "",
        password=str(value.get("password") or "") if isinstance(value, dict) else "",
    )


def save_pixie_secrets(base_dir: Path, username: str, password: str) -> None:
    path = pixie_secrets_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"username": str(username or "").strip(), "password": str(password or "")}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass


class PixieManager:
    """One process-wide Pixie inventory cache and authenticated control session."""

    def __init__(self, config: dict[str, Any], *, base_dir: Path):
        self.config = dict(config or {})
        self.base_dir = Path(base_dir)
        self.mode = str(self.config.get("pixie_network_mode") or "disabled").strip().lower()
        if self.mode not in ("disabled", "observe", "control"):
            self.mode = "disabled"
        self._lock = threading.RLock()
        self._inventory_lock = threading.Lock()
        self._control_lock = threading.Lock()
        self._cloud_status_lock = threading.Lock()
        self._control: PixieControlSession | None = None
        self._counter = 0x10
        self._last_inventory_at = 0.0
        self._resolved_host = ""
        self._resolved_net_id = ""
        self._resolved_mesh_net_2 = ""
        self._resolved_mesh_net = ""
        self._last_control_attempt = 0.0
        self._last_cloud_status_attempt = 0.0
        self._last_cloud_status_at = 0.0
        self._cloud_session_token = ""
        self._cloud_online_ids: set[str] = set()
        self._cloud_device_states: dict[str, dict[str, Any]] = {}
        self._cloud_status_available = False
        self._recent_commands: dict[str, tuple[int, float]] = {}
        self._manager_stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        self._started_at = time.time()
        self._snapshot: dict[str, Any] = {
            "available": AES is not None,
            "mode": self.mode,
            "connected": False,
            "controlReady": False,
            "gatewayHost": str(self.config.get("pixie_gateway_host") or ""),
            "homeName": str(self.config.get("pixie_home_name") or ""),
            "devices": [],
            "groups": [],
            "scenes": [],
            "lastError": "",
            "lastInventoryAt": None,
            "reachabilityAvailable": False,
            "reachabilityStale": False,
            "reachabilityError": "",
            "lastReachabilityAt": None,
            "reachabilityUpdatedAt": "",
        }
        self.start()
        if self.mode != "disabled":
            self._supervisor_thread = threading.Thread(
                target=self._supervisor_loop,
                daemon=True,
                name="pixie-supervisor",
            )
            self._supervisor_thread.start()

    def start(self) -> None:
        if self.mode == "disabled":
            self._snapshot["lastError"] = "Network access is disabled"
            return
        try:
            _require_crypto()
            host = str(self.config.get("pixie_gateway_host") or "").strip()
            if not host:
                adverts = discover_gateways(5.0)
                if len(adverts) != 1:
                    raise PixieError(
                        "Multiple Pixie Gateways discovered; configure an IP address"
                        if adverts else "No Pixie Gateway advertisement received"
                    )
                host = adverts[0]["host"]
            net_id = str(self.config.get("pixie_net_id") or "").strip()
            mesh_net = str(self.config.get("pixie_mesh_net") or "").strip()
            mesh_net_2 = str(self.config.get("pixie_mesh_net_2") or "").strip()
            if not net_id or not mesh_net_2:
                secrets_value = load_pixie_secrets(self.base_dir)
                provisioning = provision_pixie_cloud(secrets_value.username, secrets_value.password)
                selected_id = str(self.config.get("pixie_home_id") or provisioning["login"].get("currentHomeId") or "")
                homes = provisioning.get("homes") or []
                selected = next((home for home in homes if home.get("objectId") == selected_id), None)
                if selected is None and len(homes) == 1:
                    selected = homes[0]
                if selected is None:
                    choices = ", ".join(f"{home.get('name')} ({home.get('objectId')})" for home in homes)
                    raise PixieError(f"Select a Pixie Home ID; available Homes: {choices or 'none'}")
                net_id = str(selected.get("netId") or "")
                mesh_net = str(selected.get("meshNet") or "")
                mesh_net_2 = str(selected.get("meshNet2") or "")
            if not net_id or not mesh_net_2:
                raise PixieError("The selected Pixie Home did not provide Net ID and Mesh Net 2")
            self._resolved_host = host
            self._resolved_net_id = net_id
            self._resolved_mesh_net = mesh_net
            self._resolved_mesh_net_2 = mesh_net_2
            with self._lock:
                self._snapshot["gatewayHost"] = host
            self._refresh_inventory_values(host, net_id, mesh_net_2)
            self._refresh_cloud_reachability(force=True)
            if self.mode == "control":
                self._connect_control(force=True)
        except Exception as exc:
            with self._lock:
                self._snapshot["connected"] = False
                self._snapshot["controlReady"] = False
                self._snapshot["lastError"] = str(exc)

    def _connect_control(self, *, force: bool = False) -> bool:
        if self.mode != "control":
            return False
        current = self._control
        if current is not None and current.is_ready:
            return True
        now = time.monotonic()
        if not force and (now - self._last_control_attempt) < 5.0:
            return False
        with self._control_lock:
            current = self._control
            if current is not None and current.is_ready:
                return True
            now = time.monotonic()
            if not force and (now - self._last_control_attempt) < 5.0:
                return False
            self._last_control_attempt = now
            if current is not None:
                current.stop()
            control = PixieControlSession(
                self._resolved_host,
                self._resolved_net_id,
                self._resolved_mesh_net,
                self._resolved_mesh_net_2,
            )
            try:
                control.start()
            except Exception:
                control.stop()
                self._control = None
                raise
            self._control = control
            with self._lock:
                self._snapshot["controlReady"] = True
                self._snapshot["connected"] = True
                self._snapshot["lastError"] = ""
            return True

    def _supervisor_loop(self) -> None:
        while not self._manager_stop.wait(PIXIE_REACHABILITY_INTERVAL):
            if self.mode != "disabled":
                self._refresh_cloud_reachability()
            if self.mode != "control":
                continue
            if not self._resolved_host or not self._resolved_net_id or not self._resolved_mesh_net_2:
                continue
            try:
                self._connect_control()
            except Exception as exc:
                with self._lock:
                    self._snapshot["controlReady"] = False
                    self._snapshot["connected"] = False
                    self._snapshot["lastError"] = str(exc)

    def _connection_values(self) -> tuple[str, str, str]:
        host = str(self._resolved_host or self._snapshot.get("gatewayHost") or self.config.get("pixie_gateway_host") or "").strip()
        net_id = str(self._resolved_net_id or self.config.get("pixie_net_id") or "").strip()
        mesh_net_2 = str(self._resolved_mesh_net_2 or self.config.get("pixie_mesh_net_2") or "").strip()
        if not host or not net_id or not mesh_net_2:
            raise PixieError("Pixie Gateway connection identifiers are incomplete")
        return host, net_id, mesh_net_2

    def _set_cloud_reachability_error(self, message: str) -> None:
        now = time.time()
        keep_last_known = bool(
            self._last_cloud_status_at
            and (now - self._last_cloud_status_at) <= PIXIE_REACHABILITY_STALE_AFTER
        )
        with self._lock:
            self._snapshot["reachabilityStale"] = True
            self._snapshot["reachabilityError"] = str(message or "Pixie device availability is unavailable")
            if not keep_last_known:
                self._cloud_status_available = False
                self._snapshot["reachabilityAvailable"] = False
                self._snapshot["devices"] = [
                    {**item, "online": None} for item in self._snapshot.get("devices", [])
                ]

    def _refresh_cloud_reachability(self, *, force: bool = False) -> None:
        if self.mode == "disabled":
            return
        now = time.monotonic()
        if not force and (now - self._last_cloud_status_attempt) < PIXIE_REACHABILITY_INTERVAL:
            return
        with self._cloud_status_lock:
            now = time.monotonic()
            if not force and (now - self._last_cloud_status_attempt) < PIXIE_REACHABILITY_INTERVAL:
                return
            self._last_cloud_status_attempt = now
            home_id = str(self.config.get("pixie_home_id") or "").strip()
            secrets_value = load_pixie_secrets(self.base_dir)
            if not home_id or not secrets_value.username or not secrets_value.password:
                self._set_cloud_reachability_error(
                    "Pixie account credentials and a Home are required for live device availability"
                )
                return
            try:
                result = fetch_pixie_cloud_reachability(
                    secrets_value.username,
                    secrets_value.password,
                    home_id,
                    session_token=self._cloud_session_token,
                    timeout=5.0,
                )
                self._cloud_session_token = str(result.get("sessionToken") or "")
                online_ids = {str(value) for value in result.get("onlineIds") or []}
                device_states = {
                    str(ident): dict(state)
                    for ident, state in (result.get("deviceStates") or {}).items()
                    if isinstance(state, dict)
                }
                success_at = time.time()
                self._cloud_online_ids = online_ids
                self._cloud_device_states = device_states
                self._cloud_status_available = True
                self._last_cloud_status_at = success_at
                with self._lock:
                    devices = []
                    now_monotonic = time.monotonic()
                    for item in self._snapshot.get("devices", []):
                        ident = str(item.get("id"))
                        state = device_states.get(ident, {})
                        updated = {**item, "online": ident in online_ids}
                        pending = self._recent_commands.get(ident)
                        if pending and (now_monotonic - pending[1]) < PIXIE_COMMAND_FEEDBACK_GRACE:
                            updated["brightness"] = pending[0]
                            updated["on"] = pending[0] > 0
                        else:
                            self._recent_commands.pop(ident, None)
                            if state.get("brightness") is not None:
                                updated["brightness"] = state["brightness"]
                            if state.get("on") is not None:
                                updated["on"] = state["on"]
                        devices.append(updated)
                    self._snapshot["devices"] = devices
                    self._snapshot["reachabilityAvailable"] = True
                    self._snapshot["reachabilityStale"] = False
                    self._snapshot["reachabilityError"] = ""
                    self._snapshot["lastReachabilityAt"] = success_at
                    self._snapshot["reachabilityUpdatedAt"] = str(result.get("updatedAt") or "")
            except Exception as exc:
                self._set_cloud_reachability_error(str(exc))

    def _refresh_inventory_values(self, host: str, net_id: str, mesh_net_2: str) -> None:
        result = fetch_local_inventory(host, net_id, mesh_net_2)
        inventory = result["inventory"]
        devices = normalize_devices(inventory.get("deviceList"))
        with self._lock:
            previous_devices = {
                str(item.get("id")): item for item in self._snapshot.get("devices", [])
            }
        if self._cloud_status_available:
            online_ids = self._cloud_online_ids
            cloud_states = self._cloud_device_states
            merged_devices = []
            for item in devices:
                ident = str(item.get("id"))
                previous = previous_devices.get(ident)
                state = cloud_states.get(ident, {})
                updated = {**item, "online": ident in online_ids}
                # Local inventory contains cached setup-era brightness. Preserve
                # the process-wide live/optimistic state instead of bouncing a
                # slider back to that stale value on every one-second refresh.
                if previous is not None:
                    updated["brightness"] = previous.get("brightness")
                    updated["on"] = previous.get("on")
                else:
                    if state.get("brightness") is not None:
                        updated["brightness"] = state["brightness"]
                    if state.get("on") is not None:
                        updated["on"] = state["on"]
                merged_devices.append(updated)
            devices = merged_devices
        groups = normalize_targets(inventory.get("groupList"), "group")
        scenes = normalize_targets(inventory.get("sceneList"), "scene")
        now = time.time()
        with self._lock:
            self._last_inventory_at = now
            self._snapshot.update({
                "connected": True,
                "devices": devices,
                "groups": groups,
                "scenes": scenes,
                "lastError": "",
                "lastInventoryAt": now,
            })

    def refresh_inventory(self, *, force: bool = False, max_age: float = 0.75) -> dict[str, Any]:
        if self.mode == "disabled":
            return self.status()
        if not force and (time.time() - self._last_inventory_at) < float(max_age):
            return self.status()
        with self._inventory_lock:
            if not force and (time.time() - self._last_inventory_at) < float(max_age):
                return self.status()
            try:
                self._refresh_inventory_values(*self._connection_values())
            except Exception as exc:
                with self._lock:
                    self._snapshot["lastError"] = str(exc)
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = {
                key: ([dict(item) for item in value] if isinstance(value, list) else value)
                for key, value in self._snapshot.items()
            }
        if self.mode == "control" and (self._control is None or not self._control.is_ready):
            snapshot["controlReady"] = False
            snapshot["connected"] = False
            snapshot["lastError"] = snapshot.get("lastError") or "Pixie control session disconnected"
        snapshot["deviceCount"] = len(snapshot.get("devices") or [])
        snapshot["sceneCount"] = len(snapshot.get("scenes") or [])
        snapshot["nativeGroupCount"] = len(snapshot.get("groups") or [])
        return snapshot

    def _device(self, device_id: str) -> dict[str, Any]:
        matches = [item for item in self._snapshot.get("devices", []) if str(item.get("id")) == str(device_id)]
        if len(matches) != 1:
            raise PixieError(f"Pixie device ID {device_id} is missing or duplicated")
        numeric_physical_device_id(str(matches[0]["id"]))
        return matches[0]

    def _scene(self, scene_id: str) -> dict[str, Any]:
        matches = [item for item in self._snapshot.get("scenes", []) if str(item.get("id")) == str(scene_id)]
        if len(matches) != 1 or not re.fullmatch(r"\d+", str(scene_id)):
            raise PixieError(f"Pixie scene ID {scene_id} is missing, duplicated, or unsafe")
        return matches[0]

    def _next_counter(self) -> int:
        with self._lock:
            if self._counter < 0x10:
                self._counter = 0x10
            value = self._counter
            self._counter = (self._counter + 1) & 0xFF
            if self._counter < 0x10:
                self._counter = 0x10
            return value

    def set_brightness(self, device_ids: list[str], level: int) -> dict[str, Any]:
        if self.mode != "control" or self._control is None or not self._control.is_ready:
            raise PixieError("Pixie control is not enabled or the authenticated session is not ready")
        level = int(level)
        if level < 0 or level > 100:
            raise PixieError("Brightness must be from 0 to 100")
        succeeded: list[str] = []
        failed: list[dict[str, str]] = []
        for device_id in [str(value) for value in device_ids]:
            try:
                device = self._device(device_id)
                command_hex = build_brightness_command_hex(device_id, level, self._next_counter())
                payload = {
                    "data": {"type": "bleData", "data": command_hex, "repeat": 0},
                    "from": load_pixie_secrets(self.base_dir).username.strip() or "tdeck-local",
                }
                self._control.send(payload)
                with self._lock:
                    device["brightness"] = level
                    device["on"] = level > 0
                    self._recent_commands[device_id] = (level, time.monotonic())
                succeeded.append(device_id)
            except Exception as exc:
                failed.append({"id": device_id, "error": str(exc)})
        return {"ok": bool(succeeded) and not failed, "succeeded": succeeded, "failed": failed, "level": level}

    def activate_scene(self, scene_id: str) -> dict[str, Any]:
        if self.mode != "control" or self._control is None or not self._control.is_ready:
            raise PixieError("Pixie control is not enabled or the authenticated session is not ready")
        scene = self._scene(str(scene_id))
        payload = {
            "data": {"type": "wifiGw", "func": "sceneOp", "data": {"id": str(scene["id"])}},
            "from": load_pixie_secrets(self.base_dir).username.strip() or "tdeck-local",
        }
        self._control.send(payload)
        return {"ok": True, "sceneId": str(scene["id"])}

    def close(self) -> None:
        self._manager_stop.set()
        control = self._control
        self._control = None
        if control is not None:
            control.stop()
        with self._lock:
            self._snapshot["connected"] = False
            self._snapshot["controlReady"] = False


_MANAGER_LOCK = threading.RLock()
_MANAGER: PixieManager | None = None
_MANAGER_FINGERPRINT = ""


def _manager_fingerprint(config: dict[str, Any], base_dir: Path) -> str:
    keys = (
        "pixie_network_mode", "pixie_gateway_host", "pixie_home_id", "pixie_home_name",
        "pixie_net_id", "pixie_mesh_net", "pixie_mesh_net_2",
    )
    values = {key: config.get(key) for key in keys}
    try:
        values["secrets_mtime"] = pixie_secrets_path(base_dir).stat().st_mtime_ns
    except Exception:
        values["secrets_mtime"] = 0
    return json.dumps(values, sort_keys=True, default=str)


def get_pixie_manager_from_config(config: dict[str, Any] | None, *, base_dir: Path | None = None) -> PixieManager:
    global _MANAGER, _MANAGER_FINGERPRINT
    cfg = dict(config or {})
    root = Path(base_dir or Path.cwd())
    fingerprint = _manager_fingerprint(cfg, root)
    with _MANAGER_LOCK:
        if _MANAGER is not None and _MANAGER_FINGERPRINT == fingerprint:
            return _MANAGER
        if _MANAGER is not None:
            _MANAGER.close()
        _MANAGER = PixieManager(cfg, base_dir=root)
        _MANAGER_FINGERPRINT = fingerprint
        return _MANAGER


def close_pixie_manager() -> None:
    global _MANAGER, _MANAGER_FINGERPRINT
    with _MANAGER_LOCK:
        manager = _MANAGER
        _MANAGER = None
        _MANAGER_FINGERPRINT = ""
    if manager is not None:
        manager.close()
