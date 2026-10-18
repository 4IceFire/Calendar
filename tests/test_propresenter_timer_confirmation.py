"""ProPresenter may acknowledge PUT before its timer model has changed."""
import json
import unittest
from unittest.mock import Mock, patch

import requests
from propresentor import ProPresentor


class DeferredTimerTransport(requests.adapters.BaseAdapter):
    """Model the observed Mac race: reset can replace an uncommitted edit."""
    def __init__(self, reads_before_commit=3):
        self.config = {'id': {'uuid': 'synthetic-uuid', 'name': 'Service', 'index': 1},
                       'allows_overrun': True,
                       'count_down_to_time': {'time_of_day': 3600, 'period': 'am'}}
        self.pending = None
        self.reads = 0
        self.reads_before_commit = reads_before_commit
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request.method, request.path_url, kwargs.get('timeout')))
        response = requests.Response()
        response.status_code = 200
        if request.method == 'PUT':
            self.pending = json.loads(request.body)
            self.reads = 0
            result = self.pending
        elif request.path_url.endswith(('/reset', '/start')):
            # An immediate operation uses the old model and loses the edit.
            self.pending = None
            response.status_code = 204
            result = None
        else:
            if self.pending is not None:
                self.reads += 1
                if self.reads >= self.reads_before_commit:
                    self.config = self.pending
                    self.pending = None
            result = self.config
        response._content = json.dumps(result).encode() if result is not None else b''
        return response

    def close(self):
        pass


def make_client(transport):
    pp = ProPresentor('fake.invalid', 1025)
    pp.session.mount('http://', transport)
    return pp


class TimerConfirmationTests(unittest.TestCase):
    def test_apply_waits_for_committed_time_before_reset_and_start(self):
        transport = DeferredTimerTransport()
        pp = make_client(transport)
        with patch('time.sleep'):
            self.assertTrue(pp.SetCountdownToTime(1, '14:00'))
        self.assertTrue(pp.timer_operation(1, 'reset'))
        self.assertTrue(pp.timer_operation(1, 'start'))
        self.assertEqual(transport.config['count_down_to_time'],
                         {'time_of_day': 7200, 'period': 'pm'})
        self.assertEqual(sum(method == 'PUT' for method, _, _ in transport.calls), 1)
        self.assertEqual(transport.reads, 3)

    def test_flask_apply_does_not_reset_or_start_an_unconfirmed_update(self):
        import webui
        transport = DeferredTimerTransport(reads_before_commit=1000000)
        pp = make_client(transport)
        pp.timeout = 0.01
        with patch.object(webui, '_auth_enabled', return_value=False), \
             patch.object(webui.utils, 'get_config', return_value={
                 'propresenter_ip': 'fake.invalid', 'propresenter_timer_index': 2}), \
             patch.object(webui.utils, 'load_timer_presets', return_value=[{'time': '14:00'}]), \
             patch.object(webui, 'ProPresentor', return_value=pp), \
             patch.object(webui, '_home_set_last_timer_preset'), \
             patch.object(webui, '_fire_timer_button_presses_now', return_value={'fired': False, 'count': 0}):
            response = webui.app.test_client().post('/api/timers/apply', json={'preset': 1})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body['set'])
        self.assertFalse(body['reset'])
        self.assertFalse(body['started'])
        self.assertIn('not confirmed', body['detail'])
        self.assertEqual(sum(m == 'PUT' for m, _, _ in transport.calls), 1)
        self.assertFalse(any(p.endswith(('/start', '/reset', '/stop'))
                             for _, p, _ in transport.calls))

    def test_flask_direct_set_does_not_reset_an_unconfirmed_update(self):
        import webui
        pp = Mock(spec=ProPresentor)
        pp.SetCountdownToTime.return_value = False
        with patch.object(webui, '_auth_enabled', return_value=False), \
             patch.object(webui.utils, 'get_config', return_value={
                 'propresenter_ip': 'fake.invalid', 'propresenter_timer_index': 2}), \
             patch.object(webui, 'ProPresentor', return_value=pp), \
             patch.object(webui, 'log_event'):
            response = webui.app.test_client().post('/api/propresenter/timer/set',
                                                   json={'time': '14:00', 'reset': True})
        self.assertEqual(response.status_code, 200)
        pp.SetCountdownToTime.assert_called_once_with(1, '14:00')
        pp.timer_operation.assert_not_called()
        self.assertEqual(response.get_json(), {
            'ok': True, 'timer_id': 1, 'time': '14:00', 'set': False, 'reset': None,
            'propresenter_ip': 'fake.invalid', 'propresenter_port': 1025})

    def test_immediately_committed_write_does_not_sleep(self):
        transport = DeferredTimerTransport(reads_before_commit=1)
        pp = make_client(transport)
        with patch('time.sleep') as sleep:
            self.assertTrue(pp.SetCountdownToTime(1, '00:00'))
        sleep.assert_not_called()
        self.assertEqual(transport.config['count_down_to_time'],
                         {'time_of_day': 0, 'period': 'am'})

    def test_unconfirmed_write_times_out_without_replaying_put(self):
        transport = DeferredTimerTransport(reads_before_commit=1000000)
        pp = make_client(transport)
        pp.timeout = 0.01
        self.assertFalse(pp.SetCountdownToTime(1, '14:00'))
        self.assertIn('not confirmed', pp.last_timer_error)
        self.assertEqual(sum(m == 'PUT' for m, _, _ in transport.calls), 1)
        self.assertFalse(any(p.endswith(('/start', '/reset', '/stop'))
                             for _, p, _ in transport.calls))
        self.assertTrue(all(0 < t <= pp.timeout for _, _, t in transport.calls))

    def test_matching_readback_parsed_after_deadline_is_not_confirmed(self):
        transport = DeferredTimerTransport(reads_before_commit=1)
        original_send = transport.send
        now = [100.0]

        def late_matching_response(request, **kwargs):
            confirming = request.method == 'GET' and transport.pending is not None
            response = original_send(request, **kwargs)
            if confirming:
                original_json = response.json

                def late_json():
                    actual = original_json()
                    now[0] = 101.1
                    return actual

                response.json = late_json
            return response

        transport.send = late_matching_response
        pp = make_client(transport)
        pp.timeout = 1.0
        with patch('propresentor.clock.monotonic', side_effect=lambda: now[0]), \
             patch('propresentor.clock.sleep') as sleep:
            self.assertFalse(pp.SetCountdownToTime(1, '14:00'))
        self.assertEqual(pp.last_timer_error,
                         'GET /v1/timer/1: timer update not confirmed before timeout')
        self.assertEqual([(m, p) for m, p, _ in transport.calls], [
            ('GET', '/v1/timer/1'), ('PUT', '/v1/timer/1'), ('GET', '/v1/timer/1')])
        self.assertEqual(transport.config['count_down_to_time'],
                         {'time_of_day': 7200, 'period': 'pm'})
        sleep.assert_not_called()

    def test_changed_identity_aborts_confirmation(self):
        transport = DeferredTimerTransport(reads_before_commit=1)
        original_send = transport.send
        def changed_identity(request, **kwargs):
            response = original_send(request, **kwargs)
            if request.method == 'GET' and transport.reads:
                data = json.loads(response.content)
                data['id']['uuid'] = 'replacement-timer'
                response._content = json.dumps(data).encode()
            return response
        transport.send = changed_identity
        pp = make_client(transport)
        self.assertFalse(pp.SetCountdownToTime(1, '14:00'))
        self.assertIn('identity changed', pp.last_timer_error)

    def test_unreadable_confirmation_does_not_replay_write(self):
        for error in (requests.Timeout('synthetic'), 'invalid-json', 'http-error'):
            transport = DeferredTimerTransport(reads_before_commit=1)
            original_send = transport.send
            def failing_confirmation(request, **kwargs):
                if request.method == 'GET' and transport.pending:
                    if isinstance(error, Exception):
                        raise error
                    response = requests.Response()
                    response.status_code = 503 if error == 'http-error' else 200
                    response._content = b'not json'
                    return response
                return original_send(request, **kwargs)
            transport.send = failing_confirmation
            pp = make_client(transport)
            self.assertFalse(pp.SetCountdownToTime(1, '14:00'))
            self.assertIsNotNone(pp.last_timer_error)
            self.assertEqual(sum(m == 'PUT' for m, _, _ in transport.calls), 1)


if __name__ == '__main__':
    unittest.main()
