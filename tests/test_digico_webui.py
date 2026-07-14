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
                    {"enabled": True, "label": "Vocals"},
                    {"enabled": True, "label": "Band"},
                ),
                channels=(
                    {"enabled": True, "label": "Lead Vocal"},
                    {"enabled": True, "label": "Keys"},
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
            setup_page = client.get("/config/digico")
            self.assertEqual(setup_page.status_code, 200)
            self.assertIn(b"iPad / OSC Relay", setup_page.data)
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
            self.assertEqual([item["label"] for item in payload["channels"]], ["Lead Vocal", "Keys"])

            state = client.get("/api/digico/aux/2/state")
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.get_json()["aux"]["label"], "Band")

            changed = client.post(
                "/api/digico/aux/2/channel/1/level",
                json={"value": -12.5, "final": False},
            )
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.get_json()["value"], -12.5)

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


if __name__ == "__main__":
    unittest.main()
