"""The complete display coordinator, with no hardware or filesystem writes."""

from __future__ import annotations

import copy
import threading
import time
import unittest
from unittest.mock import patch

from media_routing import BusyError, MediaRoutingManager, VIDEO_ROUTING_LOCK, video_routing_guard


def configuration():
    return {"atem_media_enabled": True, "atem_ip": "192.0.2.1", "videohub_ip": "192.0.2.2",
            "atem_media_destinations": [
                {"player": 2, "label": "A", "slots": [41, 42], "aux": 1, "videohub_input": 5},
                {"player": 4, "label": "B", "slots": [43, 44], "aux": 2, "videohub_input": 6},
            ]}


class FakeMedia:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.immediate = True
        self.failure = None
        self.before_complete = None
        self.closed = False
        self.ready = True
        self.generation = 1
        self.aux_source = 3020
        self.second_aux_source = 3040
        self.aux_count = 2

    def snapshot(self):
        return {"enabled": True, "connected": self.ready, "ready": self.ready,
                "generation": self.generation, "capabilities": {"auxes": self.aux_count},
                "players": [{"player": 2, "type": "still", "slot": 42, "fillSource": 3020},
                            {"player": 4, "type": "still", "slot": 44, "fillSource": 3040}],
                "auxes": [{"aux": 1, "source": self.aux_source}, {"aux": 2, "source": self.second_aux_source}]}

    def load(self, media_id, player, *, aux=None, on_complete=None):
        self.calls.append({"media_id": media_id, "player": player, "aux": aux})
        self.callback = on_complete
        if self.before_complete:
            self.before_complete()
        if self.immediate:
            self.complete()
        return {"id": "atem-job", "status": "queued"}

    def complete(self, status=None):
        if self.calls[-1]['player'] == 4:
            self.second_aux_source = 3040
        self.callback({"id": "atem-job", "status": status or ("failed" if self.failure else "succeeded"),
                       "error": self.failure, "mediaName": "Welcome", "generation": 1,
                       "slot": 42 if self.calls[-1]["player"] == 2 else 44})

    def close(self):
        self.closed = True
        if self.callback:
            self.complete("failed")


class MediaRoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = configuration()
        self.state = {"input_count": 8, "output_count": 4, "routing": [1, 2, 3, 4]}
        self.media = FakeMedia()
        self.routes = []
        self.read_count = 0
        self.before_read = None
        self.after_route = None
        self.done = threading.Event()
        self.outcomes = []
        self.manager = self.make_manager()

    def make_manager(self, **options):
        return MediaRoutingManager(get_config=lambda: copy.deepcopy(self.cfg),
                                   get_media_manager=lambda: self.media,
                                   read_videohub=self.read, route_videohub=self.route, **options)

    def read(self):
        self.read_count += 1
        if self.before_read:
            self.before_read(self.read_count)
        return copy.deepcopy(self.state)

    def route(self, output, source):
        self.routes.append((output, source))
        self.state["routing"][output - 1] = source
        if self.after_route:
            self.after_route()

    def completed(self, job):
        self.outcomes.append(job)
        self.done.set()

    def wait(self):
        self.assertTrue(self.done.wait(3), "Display job did not complete")
        self.assertIsNone(self.manager.active_job())
        with video_routing_guard():
            pass
        return self.outcomes[-1]

    def display(self, **options):
        return self.manager.display("image-1", 1, on_complete=self.completed, **options)

    def test_success_requires_media_aux_and_full_videohub_readback(self):
        queued = self.display()
        self.assertEqual(queued["status"], "queued")
        result = self.wait()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.media.calls, [{"media_id": "image-1", "player": 2, "aux": 1}])
        self.assertEqual(self.routes, [(1, 5)])
        self.assertEqual(self.read_count, 4)
        self.assertEqual(result["videohubInput"], 5)
        self.assertEqual(result["mediaName"], "Welcome")
        self.assertEqual(len(result["id"]), 32)

    def test_existing_exclusive_target_player_is_preferred(self):
        self.state["routing"][0] = 6
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.media.calls[0]["player"], 4)
        self.assertEqual(self.routes, [(1, 6)])

    def test_player_feeding_another_output_is_never_overwritten(self):
        self.state["routing"][2] = 5
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.media.calls[0]["player"], 4)
        self.assertEqual(self.state["routing"][2], 5)

    def test_all_used_players_fail_without_loading_or_routing(self):
        self.state["routing"] = [1, 5, 6, 4]
        self.display()
        self.assertIn("in use", self.wait()["error"])
        self.assertEqual(self.media.calls, [])
        self.assertEqual(self.routes, [])

    def test_player_selected_by_another_aux_is_skipped(self):
        self.media.second_aux_source = 3020
        self.state['routing'][2] = 6
        self.display()
        self.assertEqual(self.wait()['status'], 'failed')
        self.assertEqual(self.media.calls, [])
        self.assertEqual(self.routes, [])
        self.assertEqual(self.state['routing'][2], 6)

    def test_another_isolated_player_is_selected_when_first_is_aliased(self):
        self.media.second_aux_source = 3020
        self.display()
        self.assertEqual(self.wait()['status'], 'succeeded')
        self.assertEqual(self.media.calls[0]['player'], 4)

    def test_incomplete_aux_state_cannot_allocate_a_player(self):
        self.media.aux_count = 3
        self.display()
        self.assertEqual(self.wait()['status'], 'failed')
        self.assertEqual(self.media.calls, [])

    def test_shared_current_player_is_not_reused(self):
        self.state["routing"] = [5, 5, 6, 4]
        self.display()
        self.assertEqual(self.wait()["status"], "failed")
        self.assertEqual(self.media.calls, [])

    def test_allowed_input_list_filters_candidates_before_any_load(self):
        self.display(allowed_inputs=[6])
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.media.calls[0]["player"], 4)

    def test_no_allowed_mapping_fails_without_hardware_action(self):
        self.display(allowed_inputs=[1, 2])
        self.assertIn("your access", self.wait()["error"])
        self.assertEqual(self.media.calls, [])
        self.assertEqual(self.routes, [])

    def test_incomplete_fallback_and_phantom_states_fail_closed(self):
        states = [
            {"routing": [1, 2, 3, 4]},
            {"input_count": 8, "output_count": 4, "routing": [1, 2, 3]},
            {"input_count": 8, "output_count": 4, "routing": [1, None, 3, 4]},
            {"input_count": 8, "output_count": 4, "routing": [1, 2, 3, 9]},
            {**self.state, "stale": True},
            {**self.state, "refreshing": True},
            {**self.state, "configured": False},
            {**self.state, "output_count": True},
            {"input_count": 8, "output_count": 1, "routing": [1]},
        ]
        for state in states:
            with self.subTest(state=state):
                self.state = state
                self.done.clear()
                self.manager.display("image-1", 2, on_complete=self.completed)
                self.assertEqual(self.wait()["status"], "failed")
                self.assertEqual(self.media.calls, [])
                self.assertEqual(self.routes, [])

    def test_mapping_outside_actual_device_is_rejected(self):
        self.cfg["atem_media_destinations"][1]["videohub_input"] = 9
        self.display()
        self.assertEqual(self.wait()["status"], "failed")
        self.assertEqual(self.media.calls, [])

    def test_external_target_change_before_load_is_detected(self):
        def change(count):
            if count == 2:
                self.state["routing"][0] = 3
        self.before_read = change
        self.display()
        self.assertIn("Routing changed", self.wait()["error"])
        self.assertEqual(self.media.calls, [])

    def test_external_use_or_target_change_during_load_prevents_final_route(self):
        for index, source in ((1, 5), (0, 2)):
            with self.subTest(index=index):
                self.done.clear()
                self.state["routing"] = [1, 2, 3, 4]
                self.media.before_complete = lambda: self.state["routing"].__setitem__(index, source)
                self.display()
                self.assertIn("Routing changed", self.wait()["error"])
                self.assertEqual(self.routes, [])

    def test_changed_config_during_load_prevents_final_route(self):
        self.media.before_complete = lambda: self.cfg.__setitem__("videohub_ip", "192.0.2.3")
        self.display()
        self.assertIn("Routing changed", self.wait()["error"])
        self.assertEqual(self.routes, [])

    def test_failed_media_or_aux_never_routes_and_hides_internal_error(self):
        self.media.failure = "Debug detail from 192.0.2.1 internal device path"
        self.display()
        result = self.wait()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("192.0.2.1", result["error"])
        self.assertIn("192.0.2.1", result["internalError"])
        self.assertEqual(self.routes, [])

    def test_route_readback_mismatch_never_claims_success(self):
        self.after_route = lambda: self.state["routing"].__setitem__(0, 2)
        self.display()
        self.assertEqual(self.wait()["status"], "failed")
        self.assertEqual(self.routes, [(1, 5)])

    def test_observed_aux_change_after_load_prevents_videohub_write(self):
        self.media.before_complete = lambda: setattr(self.media, "aux_source", 1)
        self.display()
        self.assertIn("source changed", self.wait()["error"])
        self.assertEqual(self.routes, [])

    def test_observed_atem_reconnect_during_route_never_claims_success(self):
        self.after_route = lambda: setattr(self.media, "generation", 2)
        self.display()
        self.assertIn("source changed", self.wait()["error"])
        self.assertEqual(self.routes, [(1, 5)])

    def test_global_reservation_rejects_other_writes_without_waiting(self):
        self.media.immediate = False
        self.display()
        deadline = time.monotonic() + 2
        while not self.media.callback and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertIsNotNone(self.manager.active_job())
        try:
            with self.assertRaises(BusyError):
                with video_routing_guard():
                    self.fail("Guard must not enter")
            with self.assertRaises(BusyError):
                self.make_manager().display("image-2", 2)
        finally:
            self.media.complete()
        self.assertEqual(self.wait()["status"], "succeeded")

    def test_route_guard_also_blocks_new_display_and_releases_after_error(self):
        with self.assertRaisesRegex(RuntimeError, "test"):
            with video_routing_guard():
                with self.assertRaises(BusyError):
                    self.display()
                raise RuntimeError("test")
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")

    def test_timeout_closes_media_before_releasing_and_ignores_late_callback(self):
        self.manager = self.make_manager(job_timeout=.03)
        self.media.immediate = False
        original_close = self.media.close
        def close():
            self.assertTrue(VIDEO_ROUTING_LOCK.locked())
            original_close()
        self.media.close = close
        self.display()
        self.assertEqual(self.wait()["status"], "failed")
        self.assertTrue(self.media.closed)
        self.media.complete("succeeded")
        self.assertEqual(len(self.outcomes), 1)
        self.assertEqual(self.routes, [])

    def test_duplicate_terminal_callbacks_cannot_change_outcome(self):
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")
        self.media.complete("failed")
        self.assertEqual(len(self.outcomes), 1)
        self.assertEqual(self.manager.snapshot()["job"]["status"], "succeeded")

    def test_recent_job_ids_are_bounded_and_return_independent_copies(self):
        ids = []
        for _ in range(34):
            self.done.clear()
            ids.append(self.display()["id"])
            self.wait()
        self.assertEqual(len(set(ids)), 34)
        with self.assertRaises(KeyError):
            self.manager.get_job(ids[0])
        with self.assertRaises(KeyError):
            self.manager.get_job(ids[1])
        result = self.manager.get_job(ids[-1])
        result["status"] = "forged"
        self.assertEqual(self.manager.get_job(ids[-1])["status"], "succeeded")

    def test_invalid_config_and_thread_start_failure_release_reservation(self):
        self.cfg["atem_media_enabled"] = False
        with self.assertRaises(ValueError):
            self.display()
        self.cfg["atem_media_enabled"] = True
        with patch("media_routing.threading.Thread.start", side_effect=RuntimeError("test thread failure")):
            with self.assertRaisesRegex(RuntimeError, "test thread failure"):
                self.display()
        self.assertIsNone(self.manager.active_job())
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
