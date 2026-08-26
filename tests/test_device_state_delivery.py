from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from atem import AtemAudioClient
from device_snapshot import SharedSnapshotCache
from package.apps.videohub.app import VideohubApp
import webui


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class SharedSnapshotCacheTests(unittest.TestCase):
    def test_many_callers_start_only_one_background_refresh(self):
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def loader():
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(2.0)
            return {"ok": True, "value": 42}

        cache = SharedSnapshotCache(
            loader,
            lambda: {"ok": False, "value": 0},
            fresh_for=5.0,
        )
        first = cache.get()
        self.assertTrue(first["stale"])
        self.assertTrue(first["refreshing"])
        self.assertTrue(entered.wait(0.5))
        for _ in range(20):
            self.assertEqual(cache.get()["value"], 0)
        self.assertEqual(calls, 1)

        release.set()
        self.assertTrue(_wait_until(lambda: not cache.diagnostics()["refreshing"]))
        current = cache.get()
        self.assertEqual(current["value"], 42)
        self.assertFalse(current["stale"])
        self.assertEqual(calls, 1)

    def test_failure_preserves_last_good_and_backoff_allows_recovery(self):
        now = [100.0]
        should_fail = [False]
        calls = 0

        def loader():
            nonlocal calls
            calls += 1
            if should_fail[0]:
                raise TimeoutError("hardware timed out")
            return {"ok": True, "value": calls}

        cache = SharedSnapshotCache(
            loader,
            lambda: {"ok": False, "value": 0},
            fresh_for=1.0,
            retry_base=2.0,
            retry_max=8.0,
            clock=lambda: now[0],
            wall_clock=lambda: 1_700_000_000.0 + now[0],
        )
        self.assertTrue(cache.refresh_now())
        self.assertEqual(cache.get()["value"], 1)

        now[0] += 2.0
        should_fail[0] = True
        stale = cache.get()
        self.assertEqual(stale["value"], 1)
        self.assertTrue(stale["stale"])
        self.assertTrue(_wait_until(lambda: not cache.diagnostics()["refreshing"]))
        failed = cache.get()
        self.assertEqual(failed["value"], 1)
        self.assertEqual(failed["lastError"], "hardware timed out")
        self.assertEqual(failed["consecutiveFailures"], 1)
        self.assertEqual(calls, 2)

        # Calls during the retry window reuse the last-known state.
        for _ in range(5):
            cache.get(force_refresh=True)
        self.assertEqual(calls, 2)

        now[0] += 2.1
        should_fail[0] = False
        cache.get()
        self.assertTrue(_wait_until(lambda: not cache.diagnostics()["refreshing"]))
        recovered = cache.get()
        self.assertEqual(recovered["value"], 3)
        self.assertIsNone(recovered["lastError"])
        self.assertEqual(recovered["consecutiveFailures"], 0)


class AtemSnapshotTests(unittest.TestCase):
    def test_cached_control_state_overlays_latest_meter_memory(self):
        entered = threading.Event()
        release = threading.Event()

        class _FakeAtem(AtemAudioClient):
            def __init__(self):
                self.reads = 0
                super().__init__("atem.test", timeout=0.5)

            def get_audio_state(self):
                self.reads += 1
                entered.set()
                release.wait(2.0)
                return {
                    "connected": True,
                    "sources": [
                        {"id": "master", "label": "Master", "level": {}},
                        {"id": "1", "label": "Camera", "level": {}},
                    ],
                    "monitor": {},
                    "metering": {},
                }

            def get_meter_snapshot(self):
                return {
                    "ok": True,
                    "connected": True,
                    "master": {"max": -8.0},
                    "sources": {"1": {"max": -12.0}},
                    "metering": {"enabled": True, "connected": True, "active": True},
                    "sampledAt": time.time(),
                    "stale": False,
                }

        client = _FakeAtem()
        first = client.get_audio_state_snapshot()
        self.assertTrue(first["refreshing"])
        self.assertTrue(entered.wait(0.5))
        for _ in range(20):
            client.get_audio_state_snapshot()
        self.assertEqual(client.reads, 1)
        release.set()
        self.assertTrue(_wait_until(lambda: not client._audio_snapshot.diagnostics()["refreshing"]))
        current = client.get_audio_state_snapshot()
        self.assertTrue(current["connected"])
        self.assertEqual(current["sources"][0]["level"]["max"], -8.0)
        self.assertEqual(current["sources"][1]["level"]["max"], -12.0)
        self.assertEqual(client.reads, 1)

    def test_compact_meter_endpoint_does_not_read_full_control_state(self):
        class _FakeAtem:
            def get_meter_snapshot(self):
                return {
                    "ok": True,
                    "connected": True,
                    "master": {"max": -3.0},
                    "sources": {"1": {"max": -9.0}},
                    "metering": {"active": True},
                    "sampledAt": 123.0,
                    "ageMs": 5,
                    "stale": False,
                }

            def get_audio_state(self):
                raise AssertionError("compact meters must not read ATEM control state")

        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui, "_get_atem_client_from_config", return_value=_FakeAtem()),
        ):
            response = webui.app.test_client().get("/api/atem/audio/meters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sources"]["1"]["max"], -9.0)


class VideoHubSnapshotTests(unittest.TestCase):
    def setUp(self):
        with webui._status_cache_lock:
            self.old_cache = dict(webui._videohub_state_cache)
            webui._videohub_state_cache.clear()
            webui._videohub_state_cache.update({
                "ts": 0.0,
                "payload": None,
                "last_error": None,
                "failures": 0,
                "retry_after": 0.0,
            })
        with webui._videohub_state_refresh_lock:
            self.old_refreshing = webui._videohub_state_refreshing
            webui._videohub_state_refreshing = False

    def tearDown(self):
        _wait_until(lambda: not webui._videohub_state_refreshing, timeout=2.5)
        with webui._status_cache_lock:
            webui._videohub_state_cache.clear()
            webui._videohub_state_cache.update(self.old_cache)
        with webui._videohub_state_refresh_lock:
            webui._videohub_state_refreshing = self.old_refreshing

    def test_labels_and_routing_share_one_non_blocking_refresh(self):
        entered = threading.Event()
        release = threading.Event()

        class _SlowVideoHub:
            calls = 0

            def get_state(self, *, fallback_count=40):
                self.calls += 1
                entered.set()
                release.wait(2.0)
                return {
                    "inputs": [{"number": 1, "label": "Stage"}],
                    "outputs": [{"number": 1, "label": "Screen"}],
                    "routing": [1],
                }

        device = _SlowVideoHub()
        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui, "_get_videohub_client_from_config", return_value=device),
        ):
            started = time.monotonic()
            labels = webui.app.test_client().get("/api/videohub/labels").get_json()
            elapsed = time.monotonic() - started
            state = webui.app.test_client().get("/api/videohub/state").get_json()
            self.assertLess(elapsed, 0.25)
            self.assertTrue(labels["refreshing"])
            self.assertTrue(state["refreshing"])
            self.assertEqual(len(labels["inputs"]), 40)
            self.assertTrue(entered.wait(0.5))
            self.assertEqual(device.calls, 1)
            release.set()
            self.assertTrue(_wait_until(lambda: not webui._videohub_state_refreshing))
            labels = webui.app.test_client().get("/api/videohub/labels").get_json()
            state = webui.app.test_client().get("/api/videohub/state").get_json()

        self.assertEqual(labels["inputs"][0]["label"], "Stage")
        self.assertNotIn("routing", labels)
        self.assertEqual(state["routing"], [1])
        self.assertFalse(state["stale"])
        self.assertEqual(device.calls, 1)

    def test_failure_retains_last_good_state_and_recovers_after_backoff(self):
        last_good = {
            "ok": True,
            "configured": True,
            "inputs": [{"number": 1, "label": "Last Good"}],
            "outputs": [{"number": 1, "label": "Screen"}],
            "routing": [1],
        }
        with webui._status_cache_lock:
            webui._videohub_state_cache.update({"ts": time.time() - 20.0, "payload": last_good})

        class _RecoveringVideoHub:
            def __init__(self):
                self.fail = True
                self.calls = 0

            def get_state(self, *, fallback_count=40):
                self.calls += 1
                if self.fail:
                    raise TimeoutError("VideoHub timed out")
                return {
                    "inputs": [{"number": 1, "label": "Recovered"}],
                    "outputs": [{"number": 1, "label": "Screen"}],
                    "routing": [1],
                }

        device = _RecoveringVideoHub()
        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui, "_get_videohub_client_from_config", return_value=device),
        ):
            first = webui.app.test_client().get("/api/videohub/labels").get_json()
            self.assertEqual(first["inputs"][0]["label"], "Last Good")
            self.assertTrue(_wait_until(lambda: not webui._videohub_state_refreshing))
            failed = webui.app.test_client().get("/api/videohub/labels").get_json()
            self.assertEqual(failed["inputs"][0]["label"], "Last Good")
            self.assertEqual(failed["lastError"], "VideoHub timed out")
            self.assertEqual(device.calls, 1)

            device.fail = False
            with webui._status_cache_lock:
                webui._videohub_state_cache["retry_after"] = 0.0
            webui.app.test_client().get("/api/videohub/state")
            self.assertTrue(_wait_until(lambda: not webui._videohub_state_refreshing))
            recovered = webui.app.test_client().get("/api/videohub/labels").get_json()

        self.assertEqual(recovered["inputs"][0]["label"], "Recovered")
        self.assertIsNone(recovered["lastError"])
        self.assertEqual(recovered["consecutiveFailures"], 0)


class VideoHubCommandReliabilityTests(unittest.TestCase):
    def test_preset_write_is_verified_with_one_shared_state_read(self):
        preset = SimpleNamespace(
            id=7,
            name="Sunday",
            routes=[
                SimpleNamespace(output=1, input=3, monitoring=False),
                SimpleNamespace(output=2, input=4, monitoring=False),
            ],
        )

        class _FakeVideoHub:
            def __init__(self):
                self.writes = []
                self.reads = 0

            def route_video_outputs(self, *, routes, monitoring=False):
                self.writes.append((list(routes), monitoring))

            def get_state(self, *, fallback_count=40):
                self.reads += 1
                return {"routing": [3, 4]}

        device = _FakeVideoHub()
        app = VideohubApp()
        with (
            patch.object(app, "get_preset", return_value=preset),
            patch(
                "package.apps.videohub.app.get_videohub_client_from_config",
                return_value=device,
            ),
        ):
            result = app.apply_preset({}, 7)

        self.assertTrue(result["applied"])
        self.assertEqual(device.writes, [([(0, 2), (1, 3)], False)])
        self.assertEqual(device.reads, 1)

    def test_preset_write_fails_when_readback_does_not_match(self):
        preset = SimpleNamespace(
            id=7,
            name="Sunday",
            routes=[SimpleNamespace(output=1, input=3, monitoring=False)],
        )

        class _FakeVideoHub:
            def route_video_outputs(self, *, routes, monitoring=False):
                return None

            def get_state(self, *, fallback_count=40):
                return {"routing": [2]}

        app = VideohubApp()
        with (
            patch.object(app, "get_preset", return_value=preset),
            patch(
                "package.apps.videohub.app.get_videohub_client_from_config",
                return_value=_FakeVideoHub(),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                app.apply_preset({}, 7)


if __name__ == "__main__":
    unittest.main()
