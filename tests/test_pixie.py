from __future__ import annotations

import base64
import json
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import pixie


class _InventorySimulator:
    def __init__(self, unix_seconds: int, net_id: str, *, linger: float = 0.0):
        self.unix_seconds = unix_seconds
        self.net_id = net_id
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.request = b""
        self.linger = float(linger)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        connection, _address = self.sock.accept()
        with connection:
            self.request = connection.recv(65536)
            inventory = {
                "deviceList": [
                    {"id": 120, "name": "House Lights", "model": "SDD350", "online": 1, "state": {"br": 128}},
                    {"id": 233, "name": "Amplifier", "deviceType": "relay", "online": 1, "state": {"br": 255}},
                ],
                "groupList": [{"id": 8, "name": "Unsafe native group"}],
                "sceneList": [{"id": 17578, "sName": "House 50%"}],
            }
            plaintext = json.dumps(inventory, separators=(",", ":")).encode("utf-8")
            cipher = AES.new(pixie.derive_inventory_key(self.unix_seconds, self.net_id), AES.MODE_CBC, iv=pixie.PIXIE_IV)
            encrypted = cipher.encrypt(pad(plaintext, AES.block_size))
            encoded = base64.b64encode(encrypted).decode("ascii")
            connection.sendall(f"eb{len(encoded):08x}{encoded}".encode("ascii"))
            if self.linger:
                time.sleep(self.linger)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass
        self.thread.join(timeout=1)


class PixieProtocolTests(unittest.TestCase):
    def test_aes_payload_and_chained_handshake_round_trip(self):
        plaintext = json.dumps({"command": "fixture", "value": 42})
        key = "1234567890123456"
        encrypted = pixie.encrypt_pixie_payload(plaintext, key)
        self.assertNotEqual(encrypted, plaintext.encode())
        self.assertEqual(pixie.decrypt_pixie_payload(encrypted, key), plaintext)

        net_id = "12345"
        session_key = "session-key-123"
        mesh_value = "mesh-value"
        envelope = (
            b"\x00"
            + pixie.encrypt_pixie_payload(session_key, net_id)
            + b"\x00"
            + pixie.encrypt_pixie_payload(mesh_value, session_key)
        )
        self.assertEqual(pixie.decrypt_dual_handshake(envelope, net_id), (session_key, mesh_value))

    def test_inventory_derivation_and_frames(self):
        self.assertEqual(pixie.derive_inventory_key(0x12345678, 0), b"Pixie12345678\0\0\0")
        self.assertEqual(pixie.derive_inventory_nonce(0x12345678, 0x01020304), 0x1336557C)
        request = pixie.build_inventory_request(0x12345678, 123, 456).decode("ascii")
        self.assertRegex(request, r"^ea[0-9a-f]{16}[A-Za-z0-9+/=]+$")
        self.assertNotIn("selected", request)
        self.assertEqual(len(request[18:]), int(request[2:10], 16))
        self.assertEqual(
            pixie.parse_inventory_response_frames(b"noiseeb00000004YWJjeb00000004ZGVm"),
            ["YWJj", "ZGVm"],
        )

    def test_simulated_gateway_inventory_and_device_classification(self):
        now = 0x66332211
        simulator = _InventorySimulator(now, "12345")
        simulator.start()
        try:
            with patch.object(pixie.time, "time", return_value=now):
                result = pixie.fetch_local_inventory(
                    "127.0.0.1", "12345", "67890", port=simulator.port, timeout=1.0
                )
            devices = pixie.normalize_devices(result["inventory"]["deviceList"])
            self.assertEqual([device["kind"] for device in devices], ["dimmer", "switch"])
            self.assertEqual(devices[0]["brightness"], 50)
            self.assertIsNone(devices[0]["online"])
            self.assertTrue(simulator.request.startswith(b"ea"))
            self.assertNotIn(b"selected", simulator.request)
        finally:
            simulator.close()

    def test_inventory_returns_before_gateway_closes_completed_response(self):
        now = 0x66332211
        simulator = _InventorySimulator(now, "12345", linger=0.6)
        simulator.start()
        try:
            started = time.perf_counter()
            with patch.object(pixie.time, "time", return_value=now):
                pixie.fetch_local_inventory(
                    "127.0.0.1", "12345", "67890", port=simulator.port, timeout=1.0
                )
            self.assertLess(time.perf_counter() - started, 0.35)
        finally:
            simulator.close()

    def test_device_online_requires_an_explicit_boolean_status(self):
        devices = pixie.normalize_devices([
            {"id": 1, "name": "Routing value", "online": 76, "state": {"br": 255}},
            {"id": 2, "name": "Explicitly offline", "reachable": False, "state": {"br": 0}},
            {"id": 3, "name": "Explicitly online", "isOnline": True, "state": {"br": 128}},
        ])
        self.assertIsNone(devices[0]["online"])
        self.assertFalse(devices[1]["online"])
        self.assertTrue(devices[2]["online"])

    def test_cloud_online_lists_distinguish_online_and_offline_devices(self):
        payload = {
            "onlineList": {
                "61": {"online": 0},
                "141": {"online": 202, "br": 37},
                "143": True,
                "233": {"online": "17", "r": 1},
            },
            "onlineList2": {
                "74": {"online": "0"},
                "141": {"online": 190, "br": 82},
                "233": {"online": "17", "r": 1},
            },
        }
        online_ids = pixie.parse_cloud_online_ids(payload)
        self.assertEqual(online_ids, {"141", "143", "233"})
        states = pixie.parse_cloud_device_states(payload)
        self.assertEqual(states["141"]["brightness"], 37)
        self.assertTrue(states["141"]["on"])
        self.assertTrue(states["233"]["on"])
        self.assertEqual(
            pixie.parse_cloud_online_ids({"onlineList2": {"233": {"online": 17}}}),
            {"233"},
        )
        with self.assertRaises(pixie.PixieError):
            pixie.parse_cloud_online_ids({"deviceList": []})

    def test_cloud_reachability_uses_a_small_home_status_request(self):
        login_response = Mock(status_code=200, ok=True)
        login_response.json.return_value = {
            "sessionToken": "session-token",
            "objectId": "user-id",
        }
        status_response = Mock(status_code=200, ok=True)
        status_response.json.return_value = {
            "onlineList": {"141": {"online": 202}},
            "onlineList2": {},
            "updatedAt": "2026-08-13T00:00:00.000Z",
        }
        with (
            patch.object(pixie.requests, "post", return_value=login_response) as post,
            patch.object(pixie.requests, "get", return_value=status_response) as get,
        ):
            result = pixie.fetch_pixie_cloud_reachability(
                "operator@example.test", "secret", "home-id", timeout=2.0
            )
        self.assertEqual(result["onlineIds"], {"141"})
        self.assertEqual(result["deviceStates"]["141"]["brightness"], None)
        self.assertEqual(result["sessionToken"], "session-token")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(get.call_args.kwargs["params"], {"keys": "onlineList,onlineList2,updatedAt"})

    def test_manager_applies_shared_cloud_reachability_to_inventory(self):
        inventory = {
            "deviceList": [
                {"id": 61, "name": "AUD 2 LH Back", "online": 76, "state": {"br": 255}},
                {"id": 141, "name": "AUD 2 LH Front", "online": 85, "state": {"br": 128}},
            ],
            "groupList": [],
            "sceneList": [],
        }
        config = {
            "pixie_network_mode": "observe",
            "pixie_gateway_host": "10.0.0.50",
            "pixie_home_id": "home-id",
            "pixie_net_id": "12345",
            "pixie_mesh_net_2": "67890",
        }
        with (
            patch.object(pixie, "fetch_local_inventory", return_value={"inventory": inventory}),
            patch.object(pixie, "load_pixie_secrets", return_value=pixie.PixieSecrets("user", "password")),
            patch.object(pixie, "fetch_pixie_cloud_reachability", return_value={
                "sessionToken": "token",
                "onlineIds": {"141"},
                "updatedAt": "2026-08-13T00:00:00.000Z",
            }),
        ):
            manager = pixie.PixieManager(config, base_dir=Path("."))
        try:
            status = manager.status()
            self.assertTrue(status["reachabilityAvailable"])
            self.assertFalse(status["devices"][0]["online"])
            self.assertTrue(status["devices"][1]["online"])
        finally:
            manager.close()

    def test_inventory_refresh_preserves_live_or_optimistic_level(self):
        inventory = {
            "deviceList": [
                {"id": 141, "name": "House Lights", "online": 85, "state": {"br": 255}},
            ],
            "groupList": [],
            "sceneList": [],
        }
        config = {
            "pixie_network_mode": "observe",
            "pixie_gateway_host": "10.0.0.50",
            "pixie_home_id": "home-id",
            "pixie_net_id": "12345",
            "pixie_mesh_net_2": "67890",
        }
        with (
            patch.object(pixie, "fetch_local_inventory", return_value={"inventory": inventory}),
            patch.object(pixie, "load_pixie_secrets", return_value=pixie.PixieSecrets("user", "password")),
            patch.object(pixie, "fetch_pixie_cloud_reachability", return_value={
                "sessionToken": "token",
                "onlineIds": {"141"},
                "deviceStates": {"141": {"online": True, "brightness": 37, "on": True}},
                "updatedAt": "2026-08-13T00:00:00.000Z",
            }),
        ):
            manager = pixie.PixieManager(config, base_dir=Path("."))
            try:
                self.assertEqual(manager.status()["devices"][0]["brightness"], 37)
                with manager._lock:
                    manager._snapshot["devices"][0]["brightness"] = 42
                    manager._snapshot["devices"][0]["on"] = True
                manager._refresh_inventory_values("10.0.0.50", "12345", "67890")
                self.assertEqual(manager.status()["devices"][0]["brightness"], 42)
            finally:
                manager.close()

    def test_stale_cloud_feedback_does_not_bounce_a_recent_command(self):
        inventory = {
            "deviceList": [
                {"id": 141, "name": "House Lights", "online": 85, "state": {"br": 255}},
            ],
            "groupList": [],
            "sceneList": [],
        }
        config = {
            "pixie_network_mode": "observe",
            "pixie_gateway_host": "10.0.0.50",
            "pixie_home_id": "home-id",
            "pixie_net_id": "12345",
            "pixie_mesh_net_2": "67890",
        }
        cloud_result = {
            "sessionToken": "token",
            "onlineIds": {"141"},
            "deviceStates": {"141": {"online": True, "brightness": 37, "on": True}},
            "updatedAt": "2026-08-13T00:00:00.000Z",
        }
        with (
            patch.object(pixie, "fetch_local_inventory", return_value={"inventory": inventory}),
            patch.object(pixie, "load_pixie_secrets", return_value=pixie.PixieSecrets("user", "password")),
            patch.object(pixie, "fetch_pixie_cloud_reachability", return_value=cloud_result),
        ):
            manager = pixie.PixieManager(config, base_dir=Path("."))
            try:
                with manager._lock:
                    manager._snapshot["devices"][0]["brightness"] = 42
                    manager._recent_commands["141"] = (42, time.monotonic())
                manager._refresh_cloud_reachability(force=True)
                self.assertEqual(manager.status()["devices"][0]["brightness"], 42)

                with manager._lock:
                    manager._recent_commands["141"] = (
                        42,
                        time.monotonic() - pixie.PIXIE_COMMAND_FEEDBACK_GRACE - 1,
                    )
                manager._refresh_cloud_reachability(force=True)
                self.assertEqual(manager.status()["devices"][0]["brightness"], 37)
            finally:
                manager.close()

    def test_brightness_packets_reject_native_group_and_broadcast_destinations(self):
        packet = bytes.fromhex(pixie.build_brightness_command_hex("120", 100, 0x10))
        self.assertEqual(list(packet[:3]), [0x10, 0x08, 0x04])
        self.assertEqual(packet[13], 0xFF)
        self.assertEqual(int.from_bytes(packet[-2:], "little"), 120)
        self.assertEqual(int.from_bytes(packet[-2:], "little") & 0x8000, 0)
        for unsafe in ("0", "32768", "65535", "not-an-id"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(pixie.PixieError):
                    pixie.build_brightness_command_hex(unsafe, 50, 0x10)

    def test_gateway_advert_parser_is_passive_and_strict(self):
        self.assertEqual(
            pixie.parse_gateway_advert(b'{"type":"GW","meshNet":"12","meshNet2":"34"}', "10.0.0.2")["host"],
            "10.0.0.2",
        )
        self.assertIsNone(pixie.parse_gateway_advert(b'{"type":"device"}', "10.0.0.3"))


if __name__ == "__main__":
    unittest.main()
