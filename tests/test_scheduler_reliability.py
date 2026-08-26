import heapq
import json
import os
import tempfile
import threading
import time as wall_time
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

import package.apps.calendar as calendar_app_module
from package.apps.calendar import utils
from package.apps.calendar import storage
from package.apps.calendar.models import Event, TimeOfTrigger, TriggerJob, TypeofTime, WeekDay
from package.apps.calendar.scheduler import (
    ClockScheduler,
    next_weekly_occurrence,
    push_triggers_for_occurrence,
)


class _FakeCompanion:
    def __init__(self, *, connected: bool, post_result: bool = True) -> None:
        self.connected = connected
        self.post_result = post_result
        self.posts: list[str] = []

    def post_command(self, url: str) -> bool:
        self.posts.append(url)
        if self.post_result:
            self.connected = True
        return self.post_result


def _event(event_id: int, name: str, hour: int) -> Event:
    trigger = TimeOfTrigger(
        0,
        TypeofTime.AT,
        f"location/1/0/{event_id}/press",
        uid=f"trigger-{event_id}",
    )
    return Event(
        name,
        event_id,
        WeekDay.Sunday,
        date(2026, 8, 9),
        time(hour, 0),
        True,
        [trigger],
        True,
    )


class SchedulerReliabilityTests(unittest.TestCase):
    def test_unchanged_config_does_not_recreate_companion_client(self):
        """Regression: a missing os import caused a network probe every tick."""

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"debug": False}), encoding="utf-8")
            current_mtime = os.path.getmtime(config_path)

            sentinel_client = object()
            with (
                patch.object(utils, "CONFIG_FILE", str(config_path)),
                patch.object(utils, "_config_mtime", current_mtime),
                patch.object(utils, "_companion_client", sentinel_client),
                patch.object(utils, "_create_companion_client") as create_client,
            ):
                self.assertFalse(utils.reload_config())
                self.assertFalse(utils.reload_config())

            create_client.assert_not_called()

    def test_companion_press_is_attempted_even_if_old_health_probe_failed(self):
        scheduler = ClockScheduler(debug=False)
        companion = _FakeCompanion(connected=False, post_result=True)
        scheduler.c = companion
        event = _event(1, "10AM Service", 10)
        occurrence = datetime(2026, 8, 9, 10, 0)
        job = TriggerJob(occurrence, event, occurrence, 0, event.times[0])

        with (
            patch("package.apps.calendar.scheduler._activity_log_scheduler_event"),
            patch("builtins.print"),
        ):
            scheduler._handle_trigger(job)

        self.assertEqual(["location/1/0/1/press"], companion.posts)

    def test_failed_companion_post_is_attempted_and_reported_failed(self):
        scheduler = ClockScheduler(debug=False)
        companion = _FakeCompanion(connected=False, post_result=False)
        scheduler.c = companion
        event = _event(2, "10AM Service", 10)
        occurrence = datetime(2026, 8, 9, 10, 0)
        job = TriggerJob(occurrence, event, occurrence, 0, event.times[0])

        with (
            patch("package.apps.calendar.scheduler._activity_log_scheduler_event"),
            patch("builtins.print"),
        ):
            result = scheduler._handle_trigger(job)

        self.assertFalse(result)
        self.assertEqual(["location/1/0/2/press"], companion.posts)

    def test_scheduler_prefers_private_in_process_dispatch_without_a_token(self):
        captured: list[tuple[dict, TriggerJob | None]] = []

        def execute(action, job):
            captured.append((action, job))
            return True

        scheduler = ClockScheduler(debug=False, internal_action_executor=execute)
        event = _event(3, "10AM Service", 10)
        occurrence = datetime(2026, 8, 9, 10, 0)
        job = TriggerJob(occurrence, event, occurrence, 0, event.times[0])

        with (
            patch.object(utils, "get_config", return_value={}),
            patch.dict(os.environ, {"TDECK_INTERNAL_API_TOKEN": ""}, clear=False),
            patch("package.apps.calendar.scheduler.requests.request") as request_call,
        ):
            result = scheduler._execute_internal_api_action(
                {"method": "POST", "path": "timers/mutate", "body": {"action": "noop"}},
                job,
            )

        self.assertTrue(result)
        request_call.assert_not_called()
        self.assertEqual(1, len(captured))
        action, passed_job = captured[0]
        self.assertIs(passed_job, job)
        self.assertEqual("/api/timers/mutate", action["path"])
        self.assertEqual(3, action["body"]["event_id"])
        self.assertEqual("10AM Service", action["body"]["event_name"])

    def test_webui_scheduler_dispatch_is_internal_and_cannot_manage_configuration(self):
        import webui
        from flask import g, jsonify

        captured = {}

        def route_stub():
            captured["principal"] = dict(g.api_principal)
            captured["body"] = webui.request.get_json(silent=True)
            return jsonify({"ok": True})

        endpoint = "api_apply_timer_preset"
        original = webui.app.view_functions[endpoint]
        webui.app.view_functions[endpoint] = route_stub
        self.addCleanup(webui.app.view_functions.__setitem__, endpoint, original)
        config = {
            "auth_enabled": True,
            "api_legacy_anonymous_enabled": False,
            "api_max_request_bytes": 2 * 1024 * 1024,
            "api_write_rate_limit_per_minute": 600,
        }

        with (
            patch.object(webui, "_auth_cfg", return_value=config),
            patch.object(webui, "log_event"),
        ):
            internal = webui._execute_scheduler_internal_action(
                {"method": "POST", "path": "/api/timers/apply", "body": {"preset": 1}}
            )
            denied = webui._execute_scheduler_internal_action(
                {"method": "GET", "path": "/api/config/service-tokens"}
            )
            external = webui.app.test_client().post(
                "/api/timers/apply", json={"preset": 1}
            )

        self.assertTrue(internal)
        self.assertEqual("scheduler", captured["principal"]["type"])
        self.assertEqual({"preset": 1}, captured["body"])
        self.assertFalse(denied)
        self.assertEqual(401, external.status_code)
        self.assertEqual("unauthorized", external.get_json()["error"])

    def test_webui_injects_private_dispatcher_into_calendar_app(self):
        import webui

        class CalendarStub:
            executor = None

            def set_internal_action_executor(self, executor):
                self.executor = executor

            @staticmethod
            def status():
                return {"running": True}

        calendar = CalendarStub()
        with (
            patch.object(webui, "list_apps", return_value={"calendar": object()}),
            patch.object(webui, "get_app", return_value=calendar),
            patch.object(webui, "_running_apps", {}),
        ):
            webui._start_all_apps()

        self.assertIs(calendar.executor, webui._execute_scheduler_internal_action)

    def test_two_services_remain_queued_and_fire_independently(self):
        scheduler = ClockScheduler(debug=False)
        scheduler._reload_needed = False
        sunday = datetime(2026, 8, 9, 0, 0)
        eight = _event(1, "8AM Service", 8)
        ten = _event(2, "10AM Service", 10)
        push_triggers_for_occurrence(scheduler._heap, eight, sunday.replace(hour=8), sunday)
        push_triggers_for_occurrence(scheduler._heap, ten, sunday.replace(hour=10), sunday)
        heapq.heapify(scheduler._heap)

        fired: list[str] = []

        def record_trigger(job):
            fired.append(job.event.name)
            return True

        with patch.object(scheduler, "_handle_trigger", side_effect=record_trigger):
            self.assertEqual(1, scheduler._fire_due_jobs(now=sunday.replace(hour=8)))
            self.assertEqual(["8AM Service"], fired)
            self.assertEqual("10AM Service", scheduler._heap[0].event.name)

            self.assertEqual(1, scheduler._fire_due_jobs(now=sunday.replace(hour=10)))

        self.assertEqual(["8AM Service", "10AM Service"], fired)
        self.assertEqual("10AM Service", scheduler.status()["last_trigger_event"])
        self.assertTrue(scheduler.status()["last_trigger_success"])
        next_occurrences = {
            job.event.name: job.due
            for job in scheduler._heap
            if job.event.name in {"8AM Service", "10AM Service"}
        }
        self.assertEqual(sunday.replace(hour=8) + timedelta(days=7), next_occurrences["8AM Service"])
        self.assertEqual(sunday.replace(hour=10) + timedelta(days=7), next_occurrences["10AM Service"])

    def test_restart_between_services_rebuilds_todays_10am_job(self):
        sunday = datetime(2026, 8, 9, 9, 0)
        eight = _event(1, "8AM Service", 8)
        ten = _event(2, "10AM Service", 10)
        heap: list[TriggerJob] = []

        for event in (eight, ten):
            occurrence = next_weekly_occurrence(event, sunday)
            self.assertIsNotNone(occurrence)
            push_triggers_for_occurrence(heap, event, occurrence, sunday)
        heapq.heapify(heap)

        self.assertEqual("10AM Service", heap[0].event.name)
        self.assertEqual(datetime(2026, 8, 9, 10, 0), heap[0].due)

    def test_after_trigger_rolls_repeating_service_to_next_week(self):
        """Regression for the production 8 AM -> missing 10 AM failure.

        The old rollover asked for the next occurrence relative to event start
        plus one second. An AFTER cue then made that helper choose the same
        occurrence, whose cues were all filtered as past, silently removing
        the repeating event from the queue.
        """

        sunday = datetime(2026, 8, 9, 0, 0)
        eight_occurrence = sunday.replace(hour=8)
        ten_occurrence = sunday.replace(hour=10)
        eight = Event(
            "8AM Service",
            1,
            WeekDay.Sunday,
            sunday.date(),
            eight_occurrence.time(),
            True,
            [
                TimeOfTrigger(0, TypeofTime.AT, "location/1/0/1/press", uid="eight-at"),
                TimeOfTrigger(15, TypeofTime.AFTER, "location/1/0/2/press", uid="eight-after"),
            ],
            True,
        )
        ten = Event(
            "10AM Service",
            2,
            WeekDay.Sunday,
            sunday.date(),
            ten_occurrence.time(),
            True,
            [TimeOfTrigger(45, TypeofTime.BEFORE, "location/1/0/3/press", uid="ten-before")],
            True,
        )
        scheduler = ClockScheduler(debug=False)
        scheduler._reload_needed = False
        push_triggers_for_occurrence(scheduler._heap, eight, eight_occurrence, sunday)
        push_triggers_for_occurrence(scheduler._heap, ten, ten_occurrence, sunday)
        heapq.heapify(scheduler._heap)

        with patch.object(scheduler, "_handle_trigger", return_value=True):
            self.assertEqual(2, scheduler._fire_due_jobs(now=sunday.replace(hour=8, minute=15)))

        queued = sorted((job.due, job.event.name) for job in scheduler._heap)
        self.assertIn((sunday.replace(hour=9, minute=15), "10AM Service"), queued)
        self.assertIn((eight_occurrence + timedelta(days=7), "8AM Service"), queued)
        self.assertIn((eight_occurrence + timedelta(days=7, minutes=15), "8AM Service"), queued)

    def test_scheduler_thread_fires_two_separate_events_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_path = Path(tmp)
            events_path = temp_path / "events.json"
            config_path = temp_path / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            first_due = (datetime.now() + timedelta(seconds=2)).replace(microsecond=0)
            second_due = first_due + timedelta(seconds=1)

            def one_time_event(event_id: int, name: str, due: datetime) -> Event:
                weekday = WeekDay[due.strftime("%A")]
                trigger = TimeOfTrigger(
                    0,
                    TypeofTime.AT,
                    f"location/1/0/{event_id}/press",
                    uid=f"e2e-trigger-{event_id}",
                )
                return Event(
                    name,
                    event_id,
                    weekday,
                    due.date(),
                    due.time(),
                    False,
                    [trigger],
                    True,
                )

            storage.save_events(
                [
                    one_time_event(1, "First Service", first_due),
                    one_time_event(2, "Second Service", second_due),
                ],
                str(events_path),
            )
            companion = _FakeCompanion(connected=False, post_result=True)

            with (
                patch.object(utils, "CONFIG_FILE", str(config_path)),
                patch.object(utils, "reload_config", return_value=False),
                patch.object(utils, "get_companion", return_value=companion),
                patch("package.apps.calendar.scheduler._activity_log_scheduler_event"),
                patch("builtins.print"),
            ):
                scheduler = ClockScheduler(str(events_path), poll_interval=0.02, debug=False)
                worker = threading.Thread(target=scheduler.start, daemon=True)
                worker.start()
                deadline = wall_time.monotonic() + 8.0
                while len(companion.posts) < 2 and wall_time.monotonic() < deadline:
                    wall_time.sleep(0.02)
                running_status = scheduler.status()
                scheduler.stop()
                worker.join(2.0)

            self.assertEqual(
                ["location/1/0/1/press", "location/1/0/2/press"],
                companion.posts,
            )
            self.assertTrue(running_status["running"])
            self.assertTrue(running_status["watcher_alive"])
            self.assertFalse(worker.is_alive())

    def test_file_watcher_survives_transient_windows_filesystem_error(self):
        scheduler = ClockScheduler(poll_interval=0.01, debug=False)
        scheduler._last_mtime = 1.0
        scheduler._last_config_mtime = 1.0
        calls = 0
        recovered = threading.Event()

        def flaky_getmtime(_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("temporary OneDrive lock")
            if calls >= 3:
                recovered.set()
            return 1.0

        with (
            patch("package.apps.calendar.scheduler.os.path.getmtime", side_effect=flaky_getmtime),
            patch("package.apps.calendar.scheduler.logger.exception"),
        ):
            watcher = threading.Thread(target=scheduler._watch_file, daemon=True)
            watcher.start()
            self.assertTrue(recovered.wait(2.0))
            scheduler.stop()
            watcher.join(2.0)

        self.assertFalse(watcher.is_alive())
        self.assertGreaterEqual(calls, 3)
        self.assertIsNone(scheduler._last_watch_error)

    def test_calendar_app_reports_worker_crash_instead_of_running(self):
        started = threading.Event()

        class CrashingScheduler:
            def __init__(self, *args, **kwargs):
                self.stopped = False

            def start(self):
                started.set()
                raise RuntimeError("scheduler boom")

            def stop(self):
                self.stopped = True

            def status(self):
                return {"running": False}

        with patch.object(calendar_app_module, "ClockScheduler", CrashingScheduler):
            app = calendar_app_module.CalendarApp()
            app.start(blocking=False)
            self.assertTrue(started.wait(2.0))
            if app._thread is not None:
                app._thread.join(2.0)
            status = app.status()

        self.assertFalse(status["running"])
        self.assertIn("scheduler boom", status["last_error"])

    def test_scheduler_health_endpoint_exposes_real_worker_state(self):
        import webui

        class HealthyCalendarApp:
            @staticmethod
            def status():
                return {
                    "running": True,
                    "watcher_alive": True,
                    "last_tick_at": datetime.now().isoformat(timespec="seconds"),
                    "queued_triggers": 2,
                    "next_trigger_event": "10AM Service",
                }

        with patch.object(webui, "get_app", return_value=HealthyCalendarApp()):
            status = webui._probe_scheduler_status({"poll_interval": 1})

        self.assertTrue(status["healthy"])
        self.assertEqual(2, status["queued_triggers"])
        self.assertEqual("10AM Service", status["next_trigger_event"])

        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui, "_probe_scheduler_status", return_value=status),
        ):
            response = webui.app.test_client().get("/api/scheduler_status")
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["healthy"])


if __name__ == "__main__":
    unittest.main()
