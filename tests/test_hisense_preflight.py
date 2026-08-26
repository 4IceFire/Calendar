from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hisense import HisenseConfig, HisenseManager


class _ReadOnlyVidaa:
    instances = []

    def __init__(self, **kwargs):
        self.__class__.instances.append(self)
        self.kwargs = kwargs
        self.client_id = kwargs["client_id"]
        self.connected = False
        self.is_muted = False
        self.reads = []
        self.mutations = []

    @property
    def is_connected(self):
        return self.connected

    def connect(self, **_kwargs):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def get_state(self, timeout=0):
        self.reads.append(("state", timeout))
        return {"statetype": "sourceswitch", "sourceid": "HDMI1"}

    def get_volume(self, timeout=0):
        self.reads.append(("volume", timeout))
        return 18

    def get_sources(self, timeout=0):
        self.reads.append(("sources", timeout))
        return [{"sourceid": "HDMI1", "displayname": "HDMI 1"}]

    def get_device_info(self, timeout=0):
        self.reads.append(("device-info", timeout))
        return {"model_name": "Test VIDAA"}

    def get_tv_info(self, timeout=0):
        self.reads.append(("tv-info", timeout))
        return {}


class _TimeoutVidaa(_ReadOnlyVidaa):
    def connect(self, **_kwargs):
        raise TimeoutError("connection timed out")


class _AuthRejectedVidaa(_ReadOnlyVidaa):
    def connect(self, **_kwargs):
        raise PermissionError("authentication was rejected")


class HisensePreflightTests(unittest.TestCase):
    def setUp(self):
        _ReadOnlyVidaa.instances.clear()
        _TimeoutVidaa.instances.clear()
        _AuthRejectedVidaa.instances.clear()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "client.pem").write_text("test certificate", encoding="utf-8")
        (root / "client.key").write_text("test key", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _manager(
        self,
        *,
        protocol=1000,
        uuid="",
        client_factory=_ReadOnlyVidaa,
        reachable=True,
        auth_mode="auto",
    ):
        cfg = HisenseConfig.from_mapping({
            "hisense_enabled": True,
            "hisense_cert_path": "client.pem",
            "hisense_key_path": "client.key",
            "hisense_tvs": [{
                "id": "diagnostic-tv",
                "name": "Diagnostic TV",
                "host": "192.0.2.10",
                "mac": "aa:bb:cc:dd:ee:ff",
                "uuid": uuid,
                "auth_mode": auth_mode,
            }],
        }, base_dir=Path(self.temp.name))
        return HisenseManager(
            cfg,
            client_factory=client_factory,
            wake_function=lambda *_args: (_ for _ in ()).throw(AssertionError("preflight must not wake the TV")),
            protocol_detector=lambda *_args, **_kwargs: protocol,
            reachability_probe=lambda *_args, **_kwargs: reachable,
        )

    @staticmethod
    def _check(report, check_id):
        return next(check for check in report["checks"] if check["id"] == check_id)

    def test_missing_uuid_is_reported_without_attempting_dynamic_auth(self):
        manager = self._manager(protocol=3290)
        try:
            report = manager.get("diagnostic-tv")._run_preflight()
        finally:
            manager.close()
        self.assertEqual(report["state"], "needs-uuid")
        self.assertFalse(report["ready"])
        self.assertEqual(self._check(report, "paired-uuid")["status"], "fail")
        self.assertTrue(report["authentication"]["uuidRequired"])
        self.assertEqual(_ReadOnlyVidaa.instances, [])

    def test_static_legacy_protocol_does_not_require_uuid(self):
        manager = self._manager(protocol=1000)
        try:
            report = manager.get("diagnostic-tv")._run_preflight()
        finally:
            manager.close()
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["protocol"]["generation"], "static-legacy")
        self.assertEqual(report["authentication"]["method"], "static-legacy")
        self.assertFalse(report["authentication"]["uuidRequired"])

    def test_dynamic_auth_uses_separate_paired_client_uuid(self):
        paired_uuid = "11:22:33:44:55:66"
        manager = self._manager(protocol=3290, uuid=paired_uuid)
        try:
            report = manager.get("diagnostic-tv")._run_preflight()
            client = _ReadOnlyVidaa.instances[-1]
        finally:
            manager.close()
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["protocol"]["generation"], "dynamic")
        self.assertTrue(client.kwargs["use_dynamic_auth"])
        self.assertEqual(client.kwargs["mac_address"], paired_uuid)
        self.assertNotEqual(client.kwargs["mac_address"].lower(), "aa:bb:cc:dd:ee:ff".lower())

    def test_reachable_tv_connection_timeout_is_distinguished(self):
        manager = self._manager(protocol=1000, client_factory=_TimeoutVidaa)
        try:
            report = manager.get("diagnostic-tv")._run_preflight()
        finally:
            manager.close()
        self.assertEqual(report["state"], "timeout")
        self.assertEqual(self._check(report, "reachability")["status"], "pass")
        self.assertEqual(self._check(report, "authentication")["status"], "fail")

    def test_authentication_rejection_has_pairing_repair_steps(self):
        manager = self._manager(protocol=1000, client_factory=_AuthRejectedVidaa)
        try:
            report = manager.get("diagnostic-tv")._run_preflight()
        finally:
            manager.close()
        self.assertEqual(report["state"], "auth-rejected")
        self.assertTrue(any("Pair or repair" in step for step in report["repairSteps"]))

    def test_success_performs_reads_and_no_mutating_command(self):
        manager = self._manager(protocol=1000)
        try:
            report = manager.get("diagnostic-tv")._run_preflight()
            client = _ReadOnlyVidaa.instances[-1]
        finally:
            manager.close()
        self.assertEqual(report["state"], "ready")
        self.assertTrue(report["safe"])
        self.assertEqual(self._check(report, "capability-read")["status"], "pass")
        self.assertTrue(any(read[0] == "state" for read in client.reads))
        self.assertEqual(client.mutations, [])

    def test_unreachable_tv_skips_authentication(self):
        manager = self._manager(protocol=1000, reachable=False)
        try:
            report = manager.get("diagnostic-tv")._run_preflight()
        finally:
            manager.close()
        self.assertEqual(report["state"], "unreachable")
        self.assertEqual(self._check(report, "reachability")["status"], "fail")
        self.assertEqual(_ReadOnlyVidaa.instances, [])


if __name__ == "__main__":
    unittest.main()
