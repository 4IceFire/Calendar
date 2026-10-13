"""Synthetic timer regressions: no real integration traffic."""
import unittest
from unittest.mock import Mock, patch

import requests
import webui
from propresentor import ProPresentor


class DirectTimerTests(unittest.TestCase):
    def test_old_settings_cannot_stop_or_sleep_during_apply(self):
        pp = Mock()
        calls = []
        pp.SetCountdownToTime.side_effect = lambda *args: calls.append(('set', args)) or True
        pp.timer_operation.side_effect = lambda *args: calls.append((args[1], args)) or True
        cfg = {'propresenter_is_latest': False, 'propresenter_timer_wait_set_ms': 60000,
               'propresenter_ip': 'fake.invalid', 'propresenter_timer_index': 2}
        with patch.object(webui, 'ProPresentor', return_value=pp), \
             patch.object(webui, '_home_set_last_timer_preset'), \
             patch.object(webui, '_fire_timer_button_presses_now', return_value={'fired': True, 'count': 1}), \
             patch.object(webui.time, 'sleep') as sleep:
            result, status = webui._apply_timer_preset_number(preset_number=1, cfg=cfg,
                presets=[{'time': '09:30', 'name': 'Service'}])
        self.assertEqual([c[0] for c in calls], ['set', 'reset', 'start'])
        sleep.assert_not_called()
        self.assertEqual(status, 200)
        self.assertTrue(result['started'])

    def test_retired_settings_are_removed_on_config_save_and_load(self):
        import json
        import tempfile
        from pathlib import Path
        keys = ['propresenter_is_latest', 'propresenter_timer_wait_stop_ms',
                'propresenter_timer_wait_set_ms', 'propresenter_timer_wait_reset_ms']
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'config.json')
            webui.utils.save_config(dict.fromkeys(keys, 60000), path)
            saved = json.loads(Path(path).read_text())
            self.assertFalse(set(keys) & set(saved))
            Path(path).write_text(json.dumps(dict.fromkeys(keys, False)))
            loaded = webui.utils.load_config(path)
            self.assertFalse(set(keys) & set(loaded))

    def test_timer_transport_errors_expose_failed_method_path_without_replay(self):
        for failure in (Mock(status_code=400), requests.Timeout('synthetic timeout')):
            pp = ProPresentor('fake.invalid', 1025)
            pp.session = Mock()
            pp.session.request.side_effect = [Mock(status_code=200, json=lambda: {
                'id': {'uuid': 'fake-uuid', 'name': 'Service', 'index': 0}}), failure]
            self.assertFalse(pp.SetCountdownToTime(0, '09:30'))
            self.assertIn('PUT /v1/timer/0', pp.last_timer_error)
            self.assertIn('Timeout' if isinstance(failure, Exception) else 'HTTP 400', pp.last_timer_error)
            self.assertEqual(pp.session.request.call_count, 2)

    def test_real_requests_preparation_and_flask_apply_use_vendor_paths(self):
        import json
        class Transport(requests.adapters.BaseAdapter):
            calls = []
            def send(self, request, **kwargs):
                self.calls.append((request.method, request.url, request.body, kwargs.get('timeout')))
                response = requests.Response()
                response.status_code = 204
                response._content = b''
                if request.method == 'GET' and request.url.endswith('/timer/1'):
                    response.status_code = 200
                    response._content = json.dumps({'id': {'uuid': 'synthetic-uuid', 'name': 'Service', 'index': 1}}).encode()
                if request.method == 'PUT':
                    data = json.loads(request.body)
                    response.status_code = 200 if data['id']['uuid'] == 'synthetic-uuid' else 400
                    self.payload = data
                return response
            def close(self): pass
        transport = Transport()
        pp = ProPresentor('fake.invalid', 1025)
        pp.session.mount('http://', transport)
        self.assertTrue(pp.check_connection())
        self.assertEqual(transport.calls[0][:2], ('GET', 'http://fake.invalid:1025/version'))
        transport.calls.clear()
        with patch.object(webui, '_auth_enabled', return_value=False), \
             patch.object(webui.utils, 'get_config', return_value={'propresenter_ip': 'fake.invalid', 'propresenter_port': 1025, 'propresenter_timer_index': 2}), \
             patch.object(webui.utils, 'load_timer_presets', return_value=[{'time': '12:00'}]), \
             patch.object(webui, 'ProPresentor', return_value=pp), \
             patch.object(webui, '_home_set_last_timer_preset'), \
             patch.object(webui, '_fire_timer_button_presses_now', return_value={'fired': False, 'count': 0}):
            response = webui.app.test_client().post('/api/timers/apply', json={'preset': 1})
        self.assertTrue(response.get_json()['started'])
        self.assertEqual([(m, u.split('/v1/')[1]) for m, u, b, t in transport.calls],
                         [('GET', 'timer/1'), ('PUT', 'timer/1'), ('GET', 'timer/1/reset'), ('GET', 'timer/1/start')])
        self.assertTrue(all(t == 2.0 for m, u, b, t in transport.calls))
        self.assertEqual(transport.payload['count_down_to_time'], {'time_of_day': 0, 'period': 'pm'})

    def test_http_payload_preserves_required_uuid(self):
        pp = ProPresentor('fake.invalid', 1025)
        identity = {'uuid': '942C3FC3-C4B2-44F7-A55D-4CC913BB8A5D', 'name': 'Service', 'index': 1}
        pp.session = Mock()
        def strict_transport(method, url, **kwargs):
            if method == 'GET':
                return Mock(status_code=200, json=lambda: {'id': identity})
            data = kwargs['json']
            return Mock(status_code=200 if data['id'] == identity else 400)
        pp.session.request.side_effect = strict_transport
        self.assertTrue(pp.SetCountdownToTime(1, '13:30'))
        self.assertEqual(pp.session.request.call_args.kwargs['json']['count_down_to_time'],
                         {'time_of_day': 5400, 'period': 'pm'})
        self.assertEqual(pp.session.request.call_args.args[1], 'http://fake.invalid:1025/v1/timer/1')


if __name__ == '__main__':
    unittest.main()
