from __future__ import annotations

import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from digico import DigicoConfig, DigicoMixerClient
from test_digico import _DeskSimulator, _free_udp_port
import webui


class _AuthenticatedUser:
    is_authenticated = True

    @staticmethod
    def get_id():
        return "42"


class DigicoWebApiTests(unittest.TestCase):
    def setUp(self):
        self.desk = _DeskSimulator(_free_udp_port())
        self.desk.start()
        self.mixer = DigicoMixerClient(
            DigicoConfig(
                enabled=True,
                host="127.0.0.1",
                port=self.desk.port,
                listen_address="127.0.0.1",
                listen_port=_free_udp_port(),
                request_interval=0.025,
                retry_interval=0.1,
                stale_after=2.0,
                auxes=(
                    {"enabled": True, "label": "Vocals", "icon": "vocals"},
                    {"enabled": True, "label": "Band", "icon": "keyboard"},
                ),
                channels=(
                    {"enabled": True, "label": "Lead Vocal", "icon": "vocals"},
                    {"enabled": True, "label": "Keys", "icon": "keyboard"},
                ),
            )
        )
        self.mixer.start()
        deadline = time.time() + 3
        while time.time() < deadline and not self.mixer.status()["ready"]:
            time.sleep(0.025)
        self.assertTrue(self.mixer.status()["ready"])

    def tearDown(self):
        self.mixer.close()
        self.desk.close()

    def test_pages_and_mixer_api(self):
        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui, "_get_digico_client_from_config", return_value=self.mixer),
        ):
            client = webui.app.test_client()
            mixer_page = client.get("/personal-mixes")
            self.assertEqual(mixer_page.status_code, 200)
            self.assertIn(b"digico_mixer.js", mixer_page.data)
            self.assertIn(b"digico_icons.js", mixer_page.data)
            setup_page = client.get("/config/digico")
            self.assertEqual(setup_page.status_code, 200)
            self.assertIn(b"iPad / OSC Relay", setup_page.data)
            self.assertIn(b"Section heading", setup_page.data)
            self.assertIn(b"digico_icons.js", setup_page.data)
            setup_script = client.get("/static/digico_setup.js")
            self.assertEqual(setup_script.status_code, 200)
            self.assertIn(b"digico-move-up", setup_script.data)
            self.assertIn(b"iconControl", setup_script.data)
            self.assertNotIn(b"Icon / emoji", setup_script.data)
            self.assertNotIn(b"digico-item-order", setup_script.data)
            setup_script.close()
            icon_script = client.get("/static/digico_icons.js")
            self.assertEqual(icon_script.status_code, 200)
            for label in (
                b"Vocals", b"Drums", b"Keyboard", b"Acoustic", b"Electric",
                b"Bass", b"Speaker", b"Headset mic", b"Tracks", b"FX",
            ):
                self.assertIn(label, icon_script.data)
            icon_script.close()
            mixer_script = client.get("/static/digico_mixer.js")
            self.assertEqual(mixer_script.status_code, 200)
            self.assertIn(b"headingText", mixer_script.data)
            self.assertIn(b"Unmuted", mixer_script.data)
            self.assertIn(b"Muted", mixer_script.data)
            self.assertNotIn(b"Send On", mixer_script.data)
            self.assertNotIn(b"Send Off", mixer_script.data)
            self.assertNotIn(b"const grouped = new Map", mixer_script.data)
            mixer_script.close()
            with tempfile.TemporaryDirectory() as tmp, patch.object(
                webui, "_AUTH_DB_PATH", Path(tmp) / "auth.db"
            ):
                permissions_page = client.get("/admin/permissions?tab=groups")
                self.assertEqual(permissions_page.status_code, 200)
                self.assertIn(b"Personal Mix AUX access", permissions_page.data)

            config = client.get("/api/digico/mixer/config")
            self.assertEqual(config.status_code, 200)
            payload = config.get_json()
            self.assertEqual([item["label"] for item in payload["auxes"]], ["Vocals", "Band"])
            self.assertEqual([item["icon"] for item in payload["auxes"]], ["vocals", "keyboard"])
            self.assertEqual([item["label"] for item in payload["channels"]], ["Lead Vocal", "Keys"])
            self.assertEqual([item["icon"] for item in payload["channels"]], ["vocals", "keyboard"])

            state = client.get("/api/digico/aux/2/state")
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.get_json()["aux"]["label"], "Band")
            state_payload = state.get_json()
            deadline = time.time() + 2
            while time.time() < deadline and any(
                item.get("sendOn") is None or item.get("level") is None or item.get("pan") is None
                for item in state_payload.get("channels", [])
            ):
                time.sleep(0.05)
                state_payload = client.get("/api/digico/aux/2/state").get_json()
            unchanged = client.get(
                f"/api/digico/aux/2/state?revision={state_payload['revision']}"
            )
            self.assertEqual(unchanged.status_code, 200)
            self.assertTrue(unchanged.get_json()["unchanged"])
            self.assertNotIn("channels", unchanged.get_json())

            changed = client.post(
                "/api/digico/aux/2/channel/1/level",
                json={"value": -12.5, "final": False},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.get_json()["value"], -12.5)

            toggled = client.post(
                "/api/digico/aux/2/channel/1/on",
                json={"value": False, "final": True},
            )
            self.assertEqual(toggled.status_code, 200)
            self.assertIs(toggled.get_json()["value"], False)

            invalid_toggle = client.post(
                "/api/digico/aux/2/channel/1/on",
                json={"value": "maybe"},
            )
            self.assertEqual(invalid_toggle.status_code, 400)

    def test_aux_scope_is_enforced_by_server(self):
        with (
            patch.object(webui, "_auth_enabled", return_value=True),
            patch.object(webui, "current_user", _AuthenticatedUser()),
            patch.object(webui, "can_access", return_value=True),
            patch.object(webui, "_effective_digico_aux_ids_for_user", return_value=["2"]),
            patch.object(webui, "_get_digico_client_from_config", return_value=self.mixer),
        ):
            client = webui.app.test_client()
            forbidden = client.get("/api/digico/aux/1/state")
            self.assertEqual(forbidden.status_code, 403)
            allowed = client.get("/api/digico/aux/2/state")
            self.assertEqual(allowed.status_code, 200)

    def test_config_cleaner_only_keeps_supported_picker_icons(self):
        channels = webui._digico_clean_indexed_items(
            [{"channel": 1, "label": "Vocal", "icon": "vocals"}],
            kind="channel",
        )
        self.assertEqual(channels[0]["icon"], "vocals")

        auxes = webui._digico_clean_indexed_items(
            [
                {"channel": 1, "icon": "fx"},
                {"channel": 2, "icon": "https://example.test/icon.svg"},
            ],
            kind="aux",
        )
        self.assertEqual(auxes[0]["icon"], "fx")
        self.assertEqual(auxes[1]["icon"], "")


if __name__ == "__main__":
    unittest.main()
