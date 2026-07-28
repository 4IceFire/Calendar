from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from hisense import HisenseConfig, HisenseManager


class _FakeVidaa:
    instances = []

    def __init__(self, **kwargs):
        self.__class__.instances.append(self)
        self.kwargs = kwargs
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


class _ProfileAwareVidaa(_FakeVidaa):
    def connect(self, **_kwargs):
        if Path(self.kwargs["certfile"]).name == "old.pem":
            return False
        return super().connect(**_kwargs)


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
            "hisense_tv_groups": [{
                "id": "foyer-group",
                "name": "Foyer TVs",
                "tv_ids": ["foyer"],
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
        self.assertTrue(controller.submit("mute", wait=1)["ok"])
        self.assertTrue(controller.submit("power_off", wait=1)["ok"])

        result = controller.submit("power_on", wait=1)
        self.assertTrue(result["ok"])
        self.assertEqual(self.wakes[0], ("a0:62:fb:84:ed:28", "10.5.10"))

    def test_intentional_power_off_is_healthy_and_survives_restart(self):
        controller = self.manager.get("foyer")
        instances_before = len(_FakeVidaa.instances)

        result = controller.submit("power_off", wait=1)
        self.assertTrue(result["ok"], result)
        tv = controller.status()
        self.assertFalse(tv["connected"])
        self.assertTrue(tv["expectedOff"])
        self.assertTrue(tv["healthy"])
        self.assertEqual(tv["power"], "off")
        self.assertEqual(tv["lastError"], "")

        status = self.manager.status()
        self.assertFalse(status["connected"])
        self.assertTrue(status["healthy"])
        self.assertEqual(status["off"], 1)
        self.assertTrue(status["groups"][0]["healthy"])
        self.assertEqual(status["groups"][0]["off"], 1)
        self.assertEqual(status["groups"][0]["lastError"], "")

        # Even if the normal reconnect deadline is due, an intentionally off
        # TV must stay quiet instead of creating another client and error.
        controller._last_connect_attempt = 0.0
        controller._wake_worker.set()
        time.sleep(0.6)
        self.assertEqual(len(_FakeVidaa.instances), instances_before)

        power_state = Path(self.manager.config.statefile)
        self.assertEqual(
            json.loads(power_state.read_text(encoding="utf-8"))["expectedOff"],
            ["foyer"],
        )

        config = self.manager.config
        self.manager.close()
        self.manager = HisenseManager(
            config,
            client_factory=_FakeVidaa,
            wake_function=lambda mac, subnet: self.wakes.append((mac, subnet)) is None,
        )
        restart_instance_count = len(_FakeVidaa.instances)
        self.manager.start()
        time.sleep(0.2)
        restarted = self.manager.get("foyer").status()
        self.assertTrue(restarted["expectedOff"])
        self.assertTrue(restarted["healthy"])
        self.assertEqual(len(_FakeVidaa.instances), restart_instance_count)

        powered_on = self.manager.get("foyer").submit("power_on", wait=1)
        self.assertTrue(powered_on["ok"], powered_on)
        self.assertFalse(powered_on["tv"]["expectedOff"])

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

    def test_ordered_group_status_and_control(self):
        status = self.manager.status()
        self.assertEqual(status["groups"][0]["tvIds"], ["foyer"])
        self.assertEqual(status["groups"][0]["targetId"], "group:foyer-group")
        self.assertEqual(status["targets"][0]["targetId"], "group:foyer-group")

        result = self.manager.submit_target("group:foyer-group", "volume_set", 31)
        self.assertTrue(result["ok"], result)
        deadline = time.time() + 1
        while time.time() < deadline and self.manager.get("foyer").status()["volume"] != 31:
            time.sleep(0.01)
        self.assertEqual(self.manager.target_status("group:foyer-group")["volume"], 31)

    def test_newer_protocol_uses_detected_dynamic_auth(self):
        root = Path(self.temp.name)
        cfg = HisenseConfig.from_mapping({
            "hisense_enabled": True,
            "hisense_cert_path": "client.pem",
            "hisense_key_path": "client.key",
            "hisense_tvs": [{
                "id": "modern",
                "name": "Modern TV",
                "host": "10.5.10.175",
                "mac": "e4:8a:93:f1:da:22",
                "uuid": "56:b8:88:4e:f7:19",
                "auth_mode": "auto",
            }],
        }, base_dir=root)
        manager = HisenseManager(
            cfg,
            client_factory=_FakeVidaa,
            wake_function=lambda _mac, _subnet: True,
            protocol_detector=lambda *_args, **_kwargs: 3290,
        )
        try:
            manager.start()
            deadline = time.time() + 1
            while time.time() < deadline and not manager.status()["connected"]:
                time.sleep(0.01)
            tv = manager.status()["tvs"][0]
            self.assertEqual(tv["protocolVersion"], 3290)
            self.assertEqual(tv["authMethod"], "dynamic-modern")
            self.assertTrue(tv["uuidConfigured"])
            self.assertTrue(_FakeVidaa.instances[-1].kwargs["use_dynamic_auth"])
            self.assertEqual(_FakeVidaa.instances[-1].kwargs["mac_address"], "56:b8:88:4e:f7:19")
        finally:
            manager.close()

    def test_dynamic_auth_requires_a_separate_paired_device_uuid(self):
        root = Path(self.temp.name)
        cfg = HisenseConfig.from_mapping({
            "hisense_enabled": True,
            "hisense_cert_path": "client.pem",
            "hisense_key_path": "client.key",
            "hisense_tvs": [{
                "id": "modern",
                "host": "10.5.10.175",
                "mac": "e4:8a:93:f1:da:22",
                "auth_mode": "dynamic-modern",
            }],
        }, base_dir=root)
        manager = HisenseManager(
            cfg,
            client_factory=_FakeVidaa,
            wake_function=lambda _mac, _subnet: True,
            protocol_detector=lambda *_args, **_kwargs: 3290,
        )
        initial_client_count = len(_FakeVidaa.instances)
        try:
            manager.start()
            deadline = time.time() + 1
            while time.time() < deadline and not manager.status()["tvs"][0]["lastError"]:
                time.sleep(0.01)
            tv = manager.status()["tvs"][0]
            self.assertFalse(tv["connected"])
            self.assertFalse(tv["expectedOff"])
            self.assertFalse(tv["healthy"])
            self.assertFalse(tv["uuidConfigured"])
            self.assertIn("paired device UUID is required", tv["lastError"])
            self.assertEqual(len(_FakeVidaa.instances), initial_client_count)
        finally:
            manager.close()

    def test_automatic_certificate_profile_fallback_uses_configured_order(self):
        root = Path(self.temp.name)
        (root / "old.pem").write_text("old cert", encoding="utf-8")
        (root / "old.key").write_text("old key", encoding="utf-8")
        (root / "current.pem").write_text("current cert", encoding="utf-8")
        (root / "current.key").write_text("current key", encoding="utf-8")
        cfg = HisenseConfig.from_mapping({
            "hisense_enabled": True,
            "hisense_certificate_profiles": [
                {"id": "old", "cert_path": "old.pem", "key_path": "old.key"},
                {"id": "current", "cert_path": "current.pem", "key_path": "current.key"},
            ],
            "hisense_tvs": [{
                "id": "profile-test",
                "host": "10.5.10.175",
                "mac": "e4:8a:93:f1:da:22",
                "certificate_profile": "auto",
            }],
        }, base_dir=root)
        manager = HisenseManager(
            cfg,
            client_factory=_ProfileAwareVidaa,
            wake_function=lambda _mac, _subnet: True,
        )
        try:
            manager.start()
            deadline = time.time() + 1
            while time.time() < deadline and not manager.status()["connected"]:
                time.sleep(0.01)
            tv = manager.status()["tvs"][0]
            self.assertTrue(tv["connected"], tv)
            self.assertEqual(tv["certificateProfile"], "current")
        finally:
            manager.close()

    def test_builtin_current_certificate_is_preferred_for_newer_protocols(self):
        root = Path(self.temp.name)
        cert_dir = root / "hisense_certs"
        cert_dir.mkdir()
        (cert_dir / "vidaa_client.pem").write_text("legacy cert", encoding="utf-8")
        (cert_dir / "vidaa_client.key").write_text("legacy key", encoding="utf-8")
        (cert_dir / "vidaa_current.pem").write_text("current cert", encoding="utf-8")
        (cert_dir / "vidaa_current.key").write_text("current key", encoding="utf-8")
        cfg = HisenseConfig.from_mapping({
            "hisense_enabled": True,
            "hisense_tvs": [{
                "id": "modern",
                "host": "10.5.10.175",
                "mac": "e4:8a:93:f1:da:22",
                "uuid": "56:b8:88:4e:f7:19",
            }],
        }, base_dir=root)
        manager = HisenseManager(
            cfg,
            client_factory=_FakeVidaa,
            wake_function=lambda _mac, _subnet: True,
            protocol_detector=lambda *_args, **_kwargs: 3290,
        )
        try:
            manager.start()
            deadline = time.time() + 1
            while time.time() < deadline and not manager.status()["connected"]:
                time.sleep(0.01)
            tv = manager.status()["tvs"][0]
            self.assertTrue(tv["connected"], tv)
            self.assertEqual(tv["certificateProfile"], "current-vidaa")
            self.assertEqual(Path(_FakeVidaa.instances[-1].kwargs["certfile"]).name, "vidaa_current.pem")
        finally:
            manager.close()

    def test_builtin_legacy_certificate_is_preferred_for_older_protocols(self):
        root = Path(self.temp.name)
        cert_dir = root / "hisense_certs"
        cert_dir.mkdir()
        (cert_dir / "vidaa_client.pem").write_text("legacy cert", encoding="utf-8")
        (cert_dir / "vidaa_client.key").write_text("legacy key", encoding="utf-8")
        (cert_dir / "vidaa_current.pem").write_text("current cert", encoding="utf-8")
        (cert_dir / "vidaa_current.key").write_text("current key", encoding="utf-8")
        cfg = HisenseConfig.from_mapping({
            "hisense_enabled": True,
            "hisense_tvs": [{
                "id": "legacy",
                "host": "10.5.10.140",
                "mac": "a0:62:fb:84:ed:28",
            }],
        }, base_dir=root)
        manager = HisenseManager(
            cfg,
            client_factory=_FakeVidaa,
            wake_function=lambda _mac, _subnet: True,
            protocol_detector=lambda *_args, **_kwargs: 1000,
        )
        try:
            manager.start()
            deadline = time.time() + 1
            while time.time() < deadline and not manager.status()["connected"]:
                time.sleep(0.01)
            tv = manager.status()["tvs"][0]
            self.assertTrue(tv["connected"], tv)
            self.assertEqual(tv["certificateProfile"], "default")
            self.assertEqual(Path(_FakeVidaa.instances[-1].kwargs["certfile"]).name, "vidaa_client.pem")
        finally:
            manager.close()

    def test_a_tv_can_only_belong_to_the_first_configured_group(self):
        cfg = HisenseConfig.from_mapping({
            "hisense_tvs": [{"id": "foyer", "host": "10.5.10.175"}],
            "hisense_tv_groups": [
                {"id": "first", "tv_ids": ["foyer"]},
                {"id": "second", "tv_ids": ["foyer"]},
            ],
        }, base_dir=Path(self.temp.name))
        self.assertEqual(cfg.groups[0].tv_ids, ("foyer",))
        self.assertEqual(cfg.groups[1].tv_ids, ())


if __name__ == "__main__":
    unittest.main()
