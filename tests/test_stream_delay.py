from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import webui


class _FakeProPresenter:
    instances: list["_FakeProPresenter"] = []

    def __init__(self, ip: str, port: int):
        self.ip = ip
        self.port = port
        self.messages: list[str] = []
        self.last_stage_message_error = None
        self.instances.append(self)

    def set_stage_message(self, message: str) -> bool:
        self.messages.append(message)
        return True


class StreamDelayTests(unittest.TestCase):
    def setUp(self):
        _FakeProPresenter.instances = []
        self.client = webui.app.test_client()

    def test_build_stream_delay_message_adds_minutes_and_wraps_midnight(self):
        self.assertEqual(
            webui._build_stream_delay_message({"time": "23:59"}, 2),
            "STREAM DELAY - 12:01AM",
        )

    def test_timers_stage_panel_includes_stream_delay_controls(self):
        root = Path(webui.__file__).resolve().parent
        markup = (root / "templates" / "timers.html").read_text(encoding="utf-8")
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="timers-stage-delay-minutes"', markup)
        self.assertIn('id="timers-stage-delay-decrease"', markup)
        self.assertIn('id="timers-stage-delay-increase"', markup)
        self.assertIn('id="timers-stage-delay-send"', markup)
        self.assertIn('/api/propresenter/stage/stream_delay', app_js)

    def test_stream_delay_endpoint_sends_adjusted_selected_preset_message(self):
        config = {
            "stream_start_preset": 1,
            "propresenter_ip": "10.0.0.15",
            "propresenter_port": 1025,
        }
        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui.utils, "get_config", return_value=config),
            patch.object(webui.utils, "load_timer_presets", return_value=[{"name": "Service", "time": "09:30"}]),
            patch.object(webui, "ProPresentor", _FakeProPresenter),
            patch.object(webui, "log_event"),
        ):
            response = self.client.post("/api/propresenter/stage/stream_delay", json={"delay_minutes": 5})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "STREAM DELAY - 9:35AM")
        self.assertEqual(_FakeProPresenter.instances[-1].messages, ["STREAM DELAY - 9:35AM"])

    def test_stream_delay_endpoint_uses_explicitly_selected_preset(self):
        config = {
            "stream_start_preset": 1,
            "propresenter_ip": "10.0.0.15",
            "propresenter_port": 1025,
        }
        presets = [
            {"name": "First", "time": "09:30"},
            {"name": "Second", "time": "10:00"},
        ]
        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui.utils, "get_config", return_value=config),
            patch.object(webui.utils, "load_timer_presets", return_value=presets),
            patch.object(webui, "ProPresentor", _FakeProPresenter),
            patch.object(webui, "log_event"),
        ):
            response = self.client.post(
                "/api/propresenter/stage/stream_delay",
                json={"preset": 2, "delay_minutes": 5},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["preset_number"], 2)
        self.assertEqual(response.get_json()["message"], "STREAM DELAY - 10:05AM")
        self.assertEqual(_FakeProPresenter.instances[-1].messages, ["STREAM DELAY - 10:05AM"])

    def test_stream_delay_endpoint_rejects_non_integer_delay(self):
        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui, "log_event"),
        ):
            response = self.client.post("/api/propresenter/stage/stream_delay", json={"delay_minutes": "five"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "delay_minutes must be an integer")

    def test_stream_delay_endpoint_rejects_fractional_values_and_non_object_json(self):
        config = {"stream_start_preset": 1, "propresenter_ip": "10.0.0.15", "propresenter_port": 1025}
        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui.utils, "get_config", return_value=config),
            patch.object(webui.utils, "load_timer_presets", return_value=[{"time": "09:30"}]),
            patch.object(webui, "ProPresentor", _FakeProPresenter),
            patch.object(webui, "log_event"),
        ):
            fractional_delay = self.client.post(
                "/api/propresenter/stage/stream_delay",
                json={"delay_minutes": 1.5},
            )
            fractional_preset = self.client.post(
                "/api/propresenter/stage/stream_delay",
                json={"preset": 1.5, "delay_minutes": 1},
            )
            scalar_body = self.client.post("/api/propresenter/stage/stream_delay", json="hello")

        self.assertEqual(fractional_delay.status_code, 400)
        self.assertEqual(fractional_delay.get_json()["error"], "delay_minutes must be an integer")
        self.assertEqual(fractional_preset.status_code, 400)
        self.assertEqual(fractional_preset.get_json()["error"], "preset must be an integer")
        self.assertEqual(scalar_body.status_code, 400)
        self.assertEqual(scalar_body.get_json()["error"], "request body must be a JSON object")

    def test_stream_delay_endpoint_requires_delay_minutes(self):
        with (
            patch.object(webui, "_auth_enabled", return_value=False),
            patch.object(webui, "log_event"),
        ):
            response = self.client.post("/api/propresenter/stage/stream_delay", json={})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "delay_minutes is required")


if __name__ == "__main__":
    unittest.main()
