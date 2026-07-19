from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from hisense import HisenseConfig, HisenseManager


class _FakeVidaa:
    instances = []

    def __init__(self, **kwargs):
        self.__class__.instances.append(self)
        self.client_id = kwargs["client_id"]
        self.connected = False
        self.volume = 18
        self.muted = False
        self.state = {"statetype": "sourceswitch", "sourceid": "HDMI1"}
        self.published = []
        self._authenticated = False
        self._auth_event = None

    def connect(self, **_kwargs):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    @property
    def is_connected(self):
        return self.connected

    def get_state(self, timeout=0):
        return dict(self.state)

    def get_volume(self, timeout=0):
        return self.volume

    @property
    def is_muted(self):
        return self.muted

    def get_sources(self, timeout=0):
        return [
            {"sourceid": "TV", "displayname": "TV"},
            {"sourceid": "HDMI1", "displayname": "HDMI 1"},
            {"sourceid": "HDMI2", "displayname": "HDMI 2"},
        ]

    def get_tv_info(self, timeout=0):
        return {"modelName": "55A7G"}

    def get_device_info(self, timeout=0):
        return {"model_name": "55A7G"}

    def set_volume(self, level):
        self.volume = level
        return True

    def volume_up(self):
        self.volume += 1
        return True

    def volume_down(self):
        self.volume -= 1
        return True

    def mute(self):
        self.muted = not self.muted
        return True

    def power_off(self):
        self.state = {"statetype": "fake_sleep_0"}
        return True

    def power_on(self):
        self.state = {"statetype": "sourceswitch", "sourceid": "HDMI1"}
        return True

    def start_pairing(self):
        return True

    def authenticate(self, pin, wait_for_response=False):
        self._authenticated = pin == "1234"
        return True

    def is_authenticated(self):
        return self._authenticated

    def _request_token(self):
        return None

    def _publish(self, topic, payload):
        self.published.append((topic, payload))
        if isinstance(payload, dict) and payload.get("sourceid"):
            self.state["sourceid"] = payload["sourceid"]
        return True


class HisenseManagerTests(unittest.TestCase):
    def setUp(self):
        _FakeVidaa.instances.clear()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "client.pem").write_text("cert", encoding="utf-8")
        (root / "client.key").write_text("key", encoding="utf-8")
        cfg = HisenseConfig.from_mapping({
            "hisense_enabled": True,
            "hisense_cert_path": "client.pem",
            "hisense_key_path": "client.key",
            "hisense_poll_interval": 2,
            "hisense_reconnect_interval": 2,
            "hisense_tvs": [{
                "id": "foyer",
                "name": "Foyer TV",
                "host": "10.5.10.140",
                "mac": "a0:62:fb:84:ed:28",
            }],
        }, base_dir=root)
        self.wakes = []
        self.manager = HisenseManager(
            cfg,
            client_factory=_FakeVidaa,
            wake_function=lambda mac, subnet: self.wakes.append((mac, subnet)) is None,
        )
        self.manager.start()
        deadline = time.time() + 2
        while time.time() < deadline and not self.manager.status()["connected"]:
            time.sleep(0.02)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_status_and_all_control_types(self):
        status = self.manager.status()
        self.assertTrue(status["connected"], status)
        self.assertEqual(status["tvs"][0]["model"], "55A7G")
        controller = self.manager.get("foyer")

        self.assertTrue(controller.submit("volume_set", 27, wait=1)["ok"])
        self.assertEqual(controller.status()["volume"], 27)
        self.assertTrue(controller.submit("source", "HDMI 2", wait=1)["ok"])
        self.assertEqual(controller.status()["source"], "HDMI2")
        self.assertTrue(controller.submit("power_off", wait=1)["ok"])
        self.assertTrue(controller.submit("mute", wait=1)["ok"])

        result = controller.submit("power_on", wait=1)
        self.assertTrue(result["ok"])
        self.assertEqual(self.wakes[0], ("a0:62:fb:84:ed:28", "10.5.10"))

    def test_pair_and_validation(self):
        controller = self.manager.get("foyer")
        self.assertTrue(controller.submit("pair_request", wait=1)["ok"])
        self.assertTrue(controller.submit("pair_submit", "1234", wait=1)["ok"])
        failed = controller.submit("pair_submit", "12", wait=1)
        self.assertFalse(failed["ok"])
        self.assertIn("four digits", failed["error"])

    def test_command_reconnects_after_transport_drop(self):
        first = _FakeVidaa.instances[-1]
        first.connected = False
        result = self.manager.get("foyer").submit("volume_up", wait=1)
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(len(_FakeVidaa.instances), 2)


if __name__ == "__main__":
    unittest.main()
