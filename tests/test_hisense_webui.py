from __future__ import annotations

import unittest
from unittest.mock import patch

import webui


class _Controller:
    def __init__(self):
        self.commands = []

    def status(self):
        return {"id": "test-tv", "name": "Test TV", "connected": True, "power": "on", "volume": 20, "source": "HDMI1", "sources": []}

    def submit(self, action, value=None, wait=0):
        self.commands.append((action, value, wait))
        return {"ok": True, "accepted": True, "tv": self.status()}


class _Manager:
    def __init__(self):
        self.controller = _Controller()

    def status(self):
        return {
            "ok": True,
            "enabled": True,
            "connected": True,
            "online": 1,
            "total": 1,
            "tvs": [self.controller.status()],
            "groups": [{
                "id": "all",
                "targetId": "group:all",
                "name": "All TVs",
                "enabled": True,
                "tvIds": ["test-tv"],
                "connected": True,
                "online": 1,
                "total": 1,
                "power": "on",
                "volume": 20,
                "source": "HDMI1",
            }],
        }

    def get(self, tv_id):
        if tv_id != "test-tv":
            raise KeyError(tv_id)
        return self.controller

    def target_status(self, target_id):
        if target_id == "group:all":
            return self.status()["groups"][0]
        return self.get(target_id.removeprefix("tv:")).status()

    def submit_target(self, target_id, action, value=None, wait=0):
        if target_id not in {"group:all", "tv:test-tv", "test-tv"}:
            raise KeyError(target_id)
        return self.controller.submit(action, value, wait)


class HisenseWebApiTests(unittest.TestCase):
    def setUp(self):
        self.manager = _Manager()
        self.auth = patch.object(webui, "_auth_enabled", return_value=False)
        self.factory = patch.object(webui, "_get_hisense_manager_from_config", return_value=self.manager)
        self.log = patch.object(webui, "log_event")
        self.auth.start()
        self.factory.start()
        self.log.start()
        self.addCleanup(self.auth.stop)
        self.addCleanup(self.factory.stop)
        self.addCleanup(self.log.stop)
        self.client = webui.app.test_client()

    def test_page_status_and_controls(self):
        page = self.client.get("/config/tvs")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Hisense / VIDAA", page.data)
        self.assertIn(b"hisense_setup.js", page.data)
        self.assertIn(b"hisense-profiles", page.data)
        self.assertIn(b"hisense-tree", page.data)
        self.assertIn(b"hisense-expand-all", page.data)
        self.assertIn(b"hisense-collapse-all", page.data)
        self.assertIn(b"hisense-compatible-models", page.data)
        script = self.client.get("/static/hisense_setup.js")
        try:
            self.assertEqual(script.status_code, 200)
            self.assertIn(b"tv-group-select", script.data)
            self.assertIn(b"Advanced identity", script.data)
            self.assertIn(b"Required for Wake-on-LAN power-on", script.data)
        finally:
            script.close()

        listing = self.client.get("/api/tvs")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()["online"], 1)

        self.assertEqual(self.client.post("/api/tvs/test-tv/power", json={"state": "off"}).status_code, 200)
        self.assertEqual(self.client.post("/api/tvs/test-tv/volume", json={"level": 35}).status_code, 200)
        self.assertEqual(self.client.post("/api/tvs/test-tv/source", json={"source": "HDMI2"}).status_code, 200)
        self.assertEqual(self.client.post("/api/tvs/test-tv/reconnect", json={}).status_code, 200)
        self.assertEqual(self.client.post("/api/tv-targets/group%3Aall/power", json={"state": "off"}).status_code, 200)
        self.assertEqual(
            [(action, value) for action, value, _wait in self.manager.controller.commands],
            [("power_off", None), ("volume_set", 35), ("source", "HDMI2"), ("reconnect", None), ("power_off", None)],
        )

    def test_invalid_control_payloads(self):
        self.assertEqual(self.client.post("/api/tvs/test-tv/power", json={"state": "maybe"}).status_code, 400)
        self.assertEqual(self.client.post("/api/tvs/test-tv/volume", json={"level": 101}).status_code, 400)
        self.assertEqual(self.client.post("/api/tvs/test-tv/source", json={}).status_code, 400)
        self.assertEqual(self.client.get("/api/tvs/missing/state").status_code, 404)

    def test_config_normalizes_ordered_profiles_tvs_and_groups(self):
        stored = {}
        payload = {
            "hisense_enabled": True,
            "hisense_poll_interval": 10,
            "hisense_reconnect_interval": 15,
            "hisense_compatible_models": "Confirmed: 55A7G; testing: VIDAA 9 / Q0109",
            "hisense_certificate_profiles": [{
                "id": "current",
                "name": "Current VIDAA",
                "cert_path": "hisense_certs/current.pem",
                "key_path": "hisense_certs/current.key",
                "compatible_models": "VIDAA 9 / Q0109",
                "enabled": True,
            }],
            "hisense_tvs": [{
                "id": "foyer",
                "name": "Foyer TV",
                "host": "10.5.10.175",
                "mac": "e4:8a:93:f1:da:22",
                "auth_mode": "auto",
                "certificate_profile": "current",
                "enabled": True,
            }],
            "hisense_tv_groups": [{
                "id": "public-spaces",
                "name": "Public Spaces",
                "tv_ids": ["foyer"],
                "enabled": True,
            }, {
                "id": "duplicate-membership",
                "name": "Duplicate Membership",
                "tv_ids": ["foyer"],
                "enabled": True,
            }],
        }
        with (
            patch.object(webui.utils, "get_config", return_value=stored),
            patch.object(webui.utils, "save_config") as save_config,
            patch.object(webui.utils, "reload_config"),
            patch.object(webui, "_close_hisense_manager"),
        ):
            response = self.client.put("/api/hisense/config", json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = save_config.call_args.args[0]
        self.assertEqual(saved["hisense_tv_groups"][0]["tv_ids"], ["foyer"])
        self.assertEqual(saved["hisense_tv_groups"][1]["tv_ids"], [])
        self.assertEqual(saved["hisense_tvs"][0]["certificate_profile"], "current")
        self.assertEqual(saved["hisense_cert_path"], "hisense_certs/current.pem")


if __name__ == "__main__":
    unittest.main()
