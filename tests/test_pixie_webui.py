from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import patch

import webui


class _AuthenticatedUser:
    is_authenticated = True

    @staticmethod
    def get_id():
        return "42"


class _FakePixieManager:
    def __init__(self):
        self.brightness_calls = []
        self.scene_calls = []
        self.snapshot = {
            "available": True,
            "mode": "control",
            "connected": True,
            "controlReady": True,
            "lastError": "",
            "gatewayHost": "10.0.0.50",
            "lastInventoryAt": 123,
            "devices": [
                {"id": "120", "name": "Pixie House", "kind": "dimmer", "model": "SDD350", "online": True, "on": True, "brightness": 50},
                {"id": "233", "name": "Pixie Amp", "kind": "switch", "model": "Relay", "online": None, "on": False, "brightness": 0},
                {"id": "300", "name": "Restricted", "kind": "dimmer", "online": True, "on": True, "brightness": 25},
            ],
            "groups": [{"id": "8", "name": "Native group"}],
            "scenes": [{"id": "17578", "name": "Pixie Scene"}],
            "deviceCount": 3,
            "sceneCount": 1,
            "nativeGroupCount": 1,
        }

    def status(self):
        return dict(self.snapshot)

    def refresh_inventory(self, **_kwargs):
        return self.status()

    def set_brightness(self, device_ids, level):
        self.brightness_calls.append((list(device_ids), level))
        return {"ok": True, "succeeded": list(device_ids), "failed": [], "level": level}

    def activate_scene(self, scene_id):
        self.scene_calls.append(scene_id)
        return {"ok": True, "sceneId": scene_id}


def _config():
    return {
        "pixie_network_mode": "control",
        "pixie_gateway_host": "10.0.0.50",
        "pixie_home_id": "home-1",
        "pixie_home_name": "Church",
        "pixie_net_id": "12345",
        "pixie_mesh_net": "45678",
        "pixie_mesh_net_2": "67890",
        "pixie_auditoriums": [
            {"id": "main", "name": "Main Auditorium", "device_ids": ["120", "233", "300"]},
        ],
        "pixie_devices": [
            {"id": "120", "name": "House Lights", "original_name": "Pixie House", "detected_kind": "dimmer", "control_type": "automatic"},
            {"id": "233", "name": "Amplifier", "original_name": "Pixie Amp", "detected_kind": "switch", "control_type": "automatic"},
            {"id": "300", "name": "Restricted", "original_name": "Restricted", "detected_kind": "dimmer", "control_type": "automatic"},
        ],
        "pixie_scenes": [
            {"id": "17578", "name": "Welcome", "original_name": "Pixie Scene", "enabled": True},
        ],
    }


class PixieWebUiTests(unittest.TestCase):
    def setUp(self):
        self.manager = _FakePixieManager()
        self.client = webui.app.test_client()

    def test_pages_and_assets_are_registered(self):
        with patch.object(webui, "_auth_enabled", return_value=False):
            page = self.client.get("/pixie")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"pixie_controls.js", page.data)
            self.assertIn(b"pixie-tile-grid", page.data)
            self.assertIn(b"pixie-auditorium-grid", page.data)
            setup = self.client.get("/config/pixie")
            self.assertEqual(setup.status_code, 200)
            self.assertIn(b"Save and restart Pixie service", setup.data)
            self.assertIn(b"pixie_setup.js", setup.data)
            self.assertIn(b'href="/config/pixie"', setup.data)
            style = self.client.get("/static/pixie.css")
            self.assertEqual(style.status_code, 200)
            style.close()

    def test_operator_payload_hides_inaccessible_devices_and_original_names(self):
        permissions = {"all": False, "auditoriums": {"main": ["120", "233"]}, "scenes": ["17578"]}
        with (
            patch.object(webui, "_auth_enabled", return_value=True),
            patch.object(webui, "current_user", _AuthenticatedUser()),
            patch.object(webui, "can_access", return_value=True),
            patch.object(webui, "_effective_pixie_permissions_for_user", return_value=permissions),
            patch.object(webui, "_get_pixie_manager_from_config", return_value=self.manager),
            patch.object(webui.utils, "get_config", return_value=_config()),
        ):
            response = self.client.get("/api/pixie/state?auditorium_id=main&refresh=1")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item["id"] for item in payload["devices"]], ["120", "233"])
        self.assertEqual([item["name"] for item in payload["devices"]], ["House Lights", "Amplifier"])
        self.assertIsNone(payload["devices"][1]["online"])
        self.assertFalse(payload["devices"][1]["disabled"])
        self.assertNotIn("original_name", payload["devices"][0])
        self.assertEqual(payload["scenes"][0]["name"], "Welcome")

    def test_server_enforces_auditorium_device_scope_and_switch_thresholds(self):
        permissions = {"all": False, "auditoriums": {"main": ["120", "233"]}, "scenes": []}
        common = (
            patch.object(webui, "_auth_enabled", return_value=True),
            patch.object(webui, "current_user", _AuthenticatedUser()),
            patch.object(webui, "can_access", return_value=True),
            patch.object(webui, "_validate_csrf", return_value=True),
            patch.object(webui, "_effective_pixie_permissions_for_user", return_value=permissions),
            patch.object(webui, "_get_pixie_manager_from_config", return_value=self.manager),
            patch.object(webui.utils, "get_config", return_value=_config()),
            patch.object(webui, "log_event"),
        )
        with ExitStack() as stack:
            for context in common:
                stack.enter_context(context)
            forbidden = self.client.post(
                "/api/pixie/devices/brightness",
                json={"auditorium_id": "main", "device_ids": ["300"], "level": 50, "final": True},
            )
            self.assertEqual(forbidden.status_code, 403)
            mixed = self.client.post(
                "/api/pixie/devices/brightness",
                json={"auditorium_id": "main", "device_ids": ["120", "233"], "level": 50, "final": False},
            )
            self.assertEqual(mixed.status_code, 200)
            self.assertEqual(self.manager.brightness_calls[-1], (["120"], 50))
            endpoint = self.client.post(
                "/api/pixie/devices/brightness",
                json={"auditorium_id": "main", "device_ids": ["120", "233"], "level": 100, "final": True},
            )
            self.assertEqual(endpoint.status_code, 200)
            self.assertEqual(self.manager.brightness_calls[-1], (["120", "233"], 100))
            next(item for item in self.manager.snapshot["devices"] if item["id"] == "233")["online"] = False
            call_count = len(self.manager.brightness_calls)
            offline = self.client.post(
                "/api/pixie/devices/brightness",
                json={"auditorium_id": "main", "device_ids": ["233"], "level": 100, "final": True},
            )
            self.assertEqual(offline.status_code, 400)
            self.assertIn("offline", offline.get_json()["error"].lower())
            self.assertEqual(len(self.manager.brightness_calls), call_count)

    def test_scene_permission_is_enforced_server_side(self):
        with (
            patch.object(webui, "_auth_enabled", return_value=True),
            patch.object(webui, "current_user", _AuthenticatedUser()),
            patch.object(webui, "can_access", return_value=True),
            patch.object(webui, "_validate_csrf", return_value=True),
            patch.object(webui, "_effective_pixie_permissions_for_user", return_value={"all": False, "auditoriums": {}, "scenes": []}),
            patch.object(webui, "_get_pixie_manager_from_config", return_value=self.manager),
            patch.object(webui.utils, "get_config", return_value=_config()),
            patch.object(webui, "log_event"),
        ):
            denied = self.client.post("/api/pixie/scenes/17578/activate", json={})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.manager.scene_calls, [])

    def test_permissions_union_keeps_device_grant_tied_to_auditorium(self):
        rows = [
            {
                "pixie_allowed_auditoriums": '["main"]',
                "pixie_allowed_devices": '{"main":["120"]}',
                "pixie_allowed_scenes": '["scene-a"]',
            },
            {
                "pixie_allowed_auditoriums": '["chapel"]',
                "pixie_allowed_devices": '{"main":["300"],"chapel":"*"}',
                "pixie_allowed_scenes": '["scene-b"]',
            },
        ]
        with (
            patch.object(webui, "_auth_enabled", return_value=True),
            patch.object(webui, "_user_is_admin", return_value=False),
            patch.object(webui, "_get_user_groups_for_page", return_value=rows),
        ):
            effective = webui._effective_pixie_permissions_for_user(42)
        self.assertEqual(effective["auditoriums"]["main"], ["120"])
        self.assertIsNone(effective["auditoriums"]["chapel"])
        self.assertEqual(effective["scenes"], ["scene-a", "scene-b"])


if __name__ == "__main__":
    unittest.main()
