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
                {"player": 2, "label": "A", "slots": [41, 42], "videohub_input": 5},
                {"player": 4, "label": "B", "slots": [43, 44], "videohub_input": 6},
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
        self.selected_slot = 42
        self.selected_type = "still"

    def snapshot(self):
        return {"enabled": True, "connected": self.ready, "ready": self.ready,
                "generation": self.generation, "capabilities": {"auxes": self.aux_count},
                "players": [{"player": 2, "type": self.selected_type, "slot": self.selected_slot, "fillSource": 3020},
                            {"player": 4, "type": "still", "slot": 44, "fillSource": 3040}],
                "auxes": [{"aux": 1, "source": self.aux_source}, {"aux": 2, "source": self.second_aux_source}]}

    def load(self, media_id, player, *, on_complete=None):
        self.calls.append({"media_id": media_id, "player": player})
        self.callback = on_complete
        if self.before_complete:
            self.before_complete()
        if self.immediate:
            self.complete()
        return {"id": "atem-job", "status": "queued"}

    def complete(self, status=None):
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

    def test_input_only_mapping_requires_media_and_full_videohub_readback(self):
        queued = self.display()
        self.assertEqual(queued["status"], "queued")
        result = self.wait()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.media.calls, [{"media_id": "image-1", "player": 2}])
        self.assertEqual(self.routes, [(1, 5)])
        self.assertEqual(self.read_count, 4)
        self.assertEqual(result["videohubInput"], 5)
        self.assertEqual(result["mediaName"], "Welcome")
        self.assertEqual(len(result["id"]), 32)
        self.assertNotIn("aux", result)

    def test_saved_legacy_aux_mapping_keeps_its_videohub_input(self):
        for destination in self.cfg["atem_media_destinations"]:
            destination["aux"] = 1  # Old values are inert, even when duplicated.
        self.display()
        result = self.wait()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.media.calls, [{"media_id": "image-1", "player": 2}])
        self.assertEqual(self.routes, [(1, 5)])
        self.assertNotIn("aux", result)

    def test_no_videohub_mapping_rejects_display_without_loading(self):
        for destination in self.cfg["atem_media_destinations"]:
            destination.pop("videohub_input")
        with self.assertRaisesRegex(ValueError, "not configured"):
            self.display()
        self.assertEqual(self.media.calls, [])
        self.assertEqual(self.routes, [])
        with video_routing_guard():
            pass

    def test_test_only_player_is_skipped_for_display(self):
        self.cfg["atem_media_destinations"][0].pop("videohub_input")
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.media.calls[0]["player"], 4)
        self.assertEqual(self.routes, [(1, 6)])

    def test_existing_exclusive_target_player_is_preferred(self):
        self.state["routing"][0] = 6
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.media.calls[0]["player"], 4)
        self.assertEqual(self.routes, [])

    def test_replacing_image_on_an_existing_route_loads_new_image_without_rerouting(self):
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")
        self.done.clear()
        self.manager.display("image-2", 1, on_complete=self.completed)
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.media.calls, [
            {"media_id": "image-1", "player": 2}, {"media_id": "image-2", "player": 2},
        ])
        self.assertEqual(self.routes, [(1, 5)])
        self.assertEqual(self.state["routing"], [5, 2, 3, 4])

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

    def test_player_shared_by_multiple_auxes_remains_eligible(self):
        self.media.second_aux_source = 3020
        self.state['routing'][2] = 6
        self.display()
        self.assertEqual(self.wait()['status'], 'succeeded')
        self.assertEqual(self.media.calls[0]['player'], 2)
        self.assertEqual(self.routes, [(1, 5)])
        self.assertEqual(self.state['routing'][2], 6)
        self.assertEqual(self.media.second_aux_source, 3020)

    def test_incomplete_aux_state_does_not_block_player_allocation(self):
        self.media.aux_count = 3
        self.display()
        self.assertEqual(self.wait()['status'], 'succeeded')
        self.assertEqual(self.media.calls[0]['player'], 2)

    def test_absent_aux_and_fill_source_state_does_not_block_display(self):
        original_snapshot = self.media.snapshot

        def snapshot():
            state = original_snapshot()
            state.pop("auxes")
            state.pop("capabilities")
            for player in state["players"]:
                player.pop("fillSource")
            return state

        self.media.snapshot = snapshot
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.routes, [(1, 5)])

    def test_shared_current_player_is_not_reused(self):
        self.state["routing"] = [5, 5, 6, 4]
        self.display()
        self.assertEqual(self.wait()["status"], "failed")
        self.assertEqual(self.media.calls, [])

    def test_preset_reuses_shared_current_player_even_with_another_player_free(self):
        self.state["routing"] = [6, 6, 3, 4]
        self.display(allow_shared_player=True)
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.media.calls, [{"media_id": "image-1", "player": 4}])
        self.assertEqual(self.routes, [])
        self.assertEqual(self.state["routing"], [6, 6, 3, 4])

    def test_preset_loads_busy_player_then_adds_a_different_target_output(self):
        self.cfg["atem_media_destinations"] = self.cfg["atem_media_destinations"][:1]
        for routes in ([1, 5, 3, 4], [1, 5, 5, 4]):
            with self.subTest(routes=routes):
                self.state["routing"] = list(routes)
                self.media.calls.clear()
                self.routes.clear()
                self.done.clear()
                self.display(allow_shared_player=True)
                result = self.wait()
                self.assertEqual(result["status"], "succeeded", result["error"])
                self.assertEqual(self.media.calls, [{"media_id": "image-1", "player": 2}])
                self.assertEqual(self.routes, [(1, 5)])
                self.assertEqual(self.state["routing"], [5, *routes[1:]])

    def test_preset_shared_player_selection_requires_an_allowed_input(self):
        for routes, allowed in (([1, 5, 6, 4], [1, 2]), ([5, 5, 6, 4], [1, 2])):
            with self.subTest(routes=routes, allowed=allowed):
                self.state["routing"] = routes
                self.done.clear()
                self.display(allow_shared_player=True, allowed_inputs=allowed)
                self.assertEqual(self.wait()["status"], "failed")
                self.assertEqual(self.media.calls, [])
                self.assertEqual(self.routes, [])

    def test_preset_prefers_an_existing_feed_over_an_unused_player(self):
        self.state["routing"] = [1, 6, 3, 4]
        self.display(allow_shared_player=True)
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.media.calls[0]["player"], 4)
        self.assertEqual(self.routes, [(1, 6)])
        self.assertEqual(self.state["routing"], [6, 6, 3, 4])

    def test_preset_chooses_busy_players_in_config_order_after_input_permissions(self):
        for allowed, player, source in (([], 2, 5), ([6], 4, 6)):
            with self.subTest(allowed=allowed):
                self.state["routing"] = [1, 6, 5, 4]
                self.routes.clear()
                self.media.calls.clear()
                self.done.clear()
                self.display(allow_shared_player=True, allowed_inputs=allowed)
                self.assertEqual(self.wait()["status"], "succeeded")
                self.assertEqual(self.media.calls[0]["player"], player)
                self.assertEqual(self.routes, [(1, source)])
                self.assertEqual(self.state["routing"], [source, 6, 5, 4])

    def test_shared_receivers_must_stay_unchanged_before_and_during_image_load(self):
        for phase, output, source in ((2, 2, 5), (3, 2, 5), (3, 1, 2), (3, 0, 2), (4, 2, 5)):
            with self.subTest(phase=phase, output=output, source=source):
                self.state["routing"] = [5, 5, 6, 4]
                self.read_count = 0
                self.media.calls.clear()
                self.done.clear()
                def change(count):
                    if count == phase:
                        self.state["routing"][output] = source
                self.before_read = change
                self.display(allow_shared_player=True)
                self.assertEqual(self.wait()["status"], "failed")
                if phase == 2:
                    self.assertEqual(self.media.calls, [])
                self.assertEqual(self.routes, [])

    def test_two_presets_replace_shared_image_and_run_actions_after_each_verified_load(self):
        from routing_presets import RoutingPresetRunner
        runner = RoutingPresetRunner()
        self.state["routing"] = [1, 5, 6, 4]
        actions = []
        for identity in ("first", "second"):
            self.done.clear()
            loaded = threading.Event()
            self.media.before_complete = loaded.set
            preset = {"id": identity, "name": identity, "media_id": identity,
                      "actions": [{"label": "Start timer"}]}
            self.media.immediate = False
            runner.start(identity, preset, 1, "operator",
                display=lambda callback: self.manager.display(identity, 1, on_complete=callback, allow_shared_player=True),
                execute=lambda action: actions.append(self.media.calls[-1]["media_id"]) or True,
                completed=self.completed)
            self.assertTrue(loaded.wait(2), "Preset did not start its image load")
            self.assertEqual(self.media.calls[-1]["media_id"], identity)
            self.assertNotIn(identity, actions)
            if identity == "first":
                self.assertEqual(self.state["routing"][0], 1)
            self.media.complete()
            result = self.wait()
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["sharedOutputs"], [2])
        self.assertEqual(actions, ["first", "second"])
        self.assertEqual([call["player"] for call in self.media.calls], [2, 2])
        self.assertEqual(self.routes, [(1, 5)])
        self.assertEqual(self.state["routing"], [5, 5, 6, 4])

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

    def test_failed_media_never_routes_and_hides_internal_error(self):
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

    def test_manual_aux_change_after_load_does_not_block_videohub_write(self):
        self.media.before_complete = lambda: setattr(self.media, "aux_source", 1)
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.routes, [(1, 5)])
        self.assertEqual(self.media.aux_source, 1)

    def test_changed_legacy_aux_configuration_does_not_interrupt_display(self):
        self.cfg["atem_media_destinations"][0]["aux"] = 1
        self.media.before_complete = lambda: self.cfg["atem_media_destinations"][0].__setitem__("aux", 2)
        self.display()
        self.assertEqual(self.wait()["status"], "succeeded")
        self.assertEqual(self.routes, [(1, 5)])

    def test_observed_still_or_player_type_change_after_load_blocks_route(self):
        for field, value in (("selected_slot", 41), ("selected_type", "clip")):
            with self.subTest(field=field):
                self.done.clear()
                self.media.selected_slot = 42
                self.media.selected_type = "still"
                self.media.before_complete = lambda: setattr(self.media, field, value)
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
