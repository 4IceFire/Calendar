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
        return {"ok": True, "enabled": True, "connected": True, "online": 1, "total": 1, "tvs": [self.controller.status()]}

    def get(self, tv_id):
        if tv_id != "test-tv":
            raise KeyError(tv_id)
        return self.controller


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

        listing = self.client.get("/api/tvs")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()["online"], 1)

        self.assertEqual(self.client.post("/api/tvs/test-tv/power", json={"state": "off"}).status_code, 200)
        self.assertEqual(self.client.post("/api/tvs/test-tv/volume", json={"level": 35}).status_code, 200)
        self.assertEqual(self.client.post("/api/tvs/test-tv/source", json={"source": "HDMI2"}).status_code, 200)
        self.assertEqual(self.client.post("/api/tvs/test-tv/reconnect", json={}).status_code, 200)
        self.assertEqual(
            [(action, value) for action, value, _wait in self.manager.controller.commands],
            [("power_off", None), ("volume_set", 35), ("source", "HDMI2"), ("reconnect", None)],
        )

    def test_invalid_control_payloads(self):
        self.assertEqual(self.client.post("/api/tvs/test-tv/power", json={"state": "maybe"}).status_code, 400)
        self.assertEqual(self.client.post("/api/tvs/test-tv/volume", json={"level": 101}).status_code, 400)
        self.assertEqual(self.client.post("/api/tvs/test-tv/source", json={}).status_code, 400)
        self.assertEqual(self.client.get("/api/tvs/missing/state").status_code, 404)


if __name__ == "__main__":
    unittest.main()
