import copy
import unittest
from unittest.mock import patch
import webui


class FakePP:
    calls = []
    fail = None
    last_timer_error = 'synthetic timer error'
    last_stage_message_error = 'synthetic message error'
    def __init__(self, *args):
        pass
    def SetCountdownToTime(self, timer, value):
        self.calls.append(('set', timer, value))
        return self.fail != 'set'
    def timer_operation(self, timer, operation):
        self.calls.append((operation, timer))
        return self.fail != operation
    def set_stage_message(self, message):
        self.calls.append(('message', message))
        return self.fail != 'message'


class StreamDelayUpdateTests(unittest.TestCase):
    def setUp(self):
        FakePP.calls = []
        FakePP.fail = None
        self.cfg = {'propresenter_ip': 'fake.invalid', 'propresenter_port': 1025,
                    'propresenter_timer_index': 2, 'stream_start_preset': 1, 'stream_delay_preset': 2}
        webui.utils.save_timer_presets([
            {'name': 'First', 'time': '09:30'},
            {'name': 'Second', 'time': '23:59', 'button_presses': [{'buttonURL': 'location/1/0/1/press'}]}])
        for p in [patch.object(webui, '_auth_enabled', return_value=False),
                  patch.object(webui.utils, 'get_config', return_value=self.cfg),
                  patch.object(webui, 'ProPresentor', FakePP),
                  patch.object(webui, '_home_set_last_timer_preset'),
                  patch.object(webui, '_queue_companion_timer_variable_sync'),
                  patch.object(webui, 'log_event')]:
            p.start(); self.addCleanup(p.stop)
        press_patch = patch.object(webui, '_fire_timer_button_presses_now', return_value={'fired': True, 'count': 1})
        self.presses = press_patch.start()
        self.addCleanup(press_patch.stop)
        self.client = webui.app.test_client()

    def test_unchecked_is_message_only_including_no_companion_variable_sync(self):
        original = webui.utils.load_timer_presets()
        with patch.object(webui, '_queue_companion_timer_variable_sync') as sync:
            response = self.client.post('/api/propresenter/stage/stream_delay', json=self.body(update_timer=False, delay_minutes=-2))
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['saved'])
        self.assertFalse(data['applied'])
        self.assertTrue(data['sent'])
        self.assertEqual(webui.utils.load_timer_presets(), original)
        self.assertEqual(FakePP.calls, [('message', 'STREAM DELAY - 11:57PM')])
        self.presses.assert_not_called()
        sync.assert_not_called()

    def test_partial_failures_retain_saved_time_and_never_replay(self):
        for failure in ['set', 'reset', 'start', 'message']:
            FakePP.calls = []
            FakePP.fail = failure
            data = self.client.post('/api/propresenter/stage/stream_delay', json=self.body()).get_json()
            self.assertTrue(data['saved'])
            self.assertEqual(data['applied'], failure == 'message')
            self.assertEqual(data['sent'], failure != 'message')
            self.assertFalse(data['ok'])
            self.assertEqual(sum(c[0] == 'set' for c in FakePP.calls), 1)
        self.assertEqual(webui.utils.load_timer_presets()[1]['time'], '00:07')

    def test_stale_revision_and_invalid_connection_fail_before_writes_or_commands(self):
        body = self.body()
        webui.utils.update_timer_preset(2, time_str='10:00')
        response = self.client.post('/api/propresenter/stage/stream_delay', json=body)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()['saved'])
        self.assertEqual(FakePP.calls, [])
        self.cfg['propresenter_port'] = 'bad'
        response = self.client.post('/api/propresenter/stage/stream_delay', json=self.body())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(webui.utils.load_timer_presets()[1]['time'], '10:00')
        self.presses.assert_not_called()

    def test_delay_selection_persists_independently_and_follows_moves_and_deletes(self):
        with patch.object(webui.utils, 'save_config', side_effect=lambda cfg: self.cfg.update(cfg)), \
             patch.object(webui.utils, 'reload_config'):
            response = self.client.post('/api/timers/mutate', json={'action': 'set_stream_delay_preset', 'preset': 2})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['stream_delay_preset'], 2)
            self.client.post('/api/timers/mutate', json={'action': 'move_preset', 'preset': 2, 'to': 1})
            state = self.client.get('/api/timers').get_json()
            self.assertEqual(state['stream_delay_preset'], 1)
            self.assertEqual(state['stream_start_preset'], 2)
            self.client.post('/api/timers/mutate', json={'action': 'delete_preset', 'preset': 1})
            self.assertEqual(self.client.get('/api/timers').get_json()['stream_delay_preset'], 0)
        self.assertEqual(FakePP.calls, [])

    def test_storage_preserves_unknown_preset_fields(self):
        presets = webui.utils.load_timer_presets()
        presets[1]['operator_note'] = {'color': 'blue'}
        webui.utils.save_timer_presets(presets)
        response = self.client.post('/api/propresenter/stage/stream_delay', json=self.body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(webui.utils.load_timer_presets()[1].get('operator_note'), {'color': 'blue'})

    def test_companion_failure_is_not_reported_as_applied(self):
        self.presses.return_value = {'fired': True, 'count': 1, 'ok': 0, 'fail': 1}
        data = self.client.post('/api/propresenter/stage/stream_delay', json=self.body()).get_json()
        self.assertTrue(data['saved'])
        self.assertTrue(data['apply']['started'])
        self.assertFalse(data['applied'])
        self.assertTrue(data['sent'])
        self.assertFalse(data['ok'])

    def test_companion_variable_sync_failure_does_not_hide_saved_apply_outcome(self):
        with patch.object(webui, '_queue_companion_timer_variable_sync', side_effect=RuntimeError('synthetic sync')):
            response = self.client.post('/api/propresenter/stage/stream_delay', json=self.body())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['saved'])
        self.assertTrue(response.get_json()['applied'])
        self.assertTrue(response.get_json()['sent'])

    def test_replace_all_accepts_independent_delay_setting(self):
        with patch.object(webui.utils, 'save_config', side_effect=lambda cfg: self.cfg.update(cfg)), patch.object(webui.utils, 'reload_config'):
            response = self.client.post('/api/timers', json={'timer_presets': webui.utils.load_timer_presets(),
                                       'stream_start_preset': 2, 'stream_delay_preset': 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['stream_delay_preset'], 1)
        self.assertEqual(response.get_json()['stream_start_preset'], 2)

    def test_message_only_does_not_require_a_valid_timer_target(self):
        self.cfg['propresenter_timer_index'] = 'broken timer target'
        response = self.client.post('/api/propresenter/stage/stream_delay', json={'preset': 2, 'delay_minutes': 1, 'update_timer': False})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['sent'])
        self.presses.assert_not_called()

    def test_concurrent_checked_requests_with_one_revision_only_apply_once(self):
        import threading
        body = self.body()
        barrier = threading.Barrier(2)
        statuses = []
        def send():
            with webui.app.test_client() as client:
                barrier.wait(timeout=5)
                statuses.append(client.post('/api/propresenter/stage/stream_delay', json=body).status_code)
        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.presses.call_count, 1)
        self.assertEqual(webui.utils.load_timer_presets()[1]['time'], '00:01')

    def body(self, **extra):
        body = {'delay_minutes': 2, 'preset': 2, 'update_timer': True,
                'revision': self.client.get('/api/timers').get_json().get('revision')}
        body.update(extra)
        return body

    def test_checked_saves_then_applies_selected_preset_and_repeated_click_uses_persisted_time(self):
        for expected in ['00:01', '00:03']:
            response = self.client.post('/api/propresenter/stage/stream_delay', json=self.body())
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data.get('saved'))
            self.assertTrue(data['applied'])
            self.assertTrue(data['sent'])
            presets = webui.utils.load_timer_presets()
            self.assertEqual(presets[1]['time'], expected)
            self.assertEqual(presets[0]['time'], '09:30')
            self.assertEqual(presets[1]['button_presses'], [{'buttonURL': 'location/1/0/1/press'}])
            self.assertEqual(FakePP.calls[-4:-1], [('set', 1, expected), ('reset', 1), ('start', 1)])
        self.assertEqual(self.presses.call_count, 2)


if __name__ == '__main__':
    unittest.main()
