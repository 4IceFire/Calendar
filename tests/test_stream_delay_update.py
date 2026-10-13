import copy
import unittest
from unittest.mock import patch
import webui

original_fire = webui._fire_timer_button_presses_now


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
        with webui._stream_delay_operation_lock:
            webui._stream_delay_operations.clear()
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
                import uuid
                statuses.append(client.post('/api/propresenter/stage/stream_delay', json=dict(body, operation_id=uuid.uuid4().hex)).status_code)
        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.presses.call_count, 1)
        self.assertEqual(webui.utils.load_timer_presets()[1]['time'], '00:01')

    def test_operation_cache_expiry_is_bounded_without_evicting_live_entries(self):
        body = self.body(delay_minutes=0)
        with patch.object(webui.time, 'monotonic', return_value=100):
            self.assertEqual(self.client.post('/api/propresenter/stage/stream_delay', json=body).status_code, 200)
        with patch.object(webui.time, 'monotonic', return_value=100 + webui._STREAM_DELAY_OPERATION_TTL - 1):
            self.assertEqual(self.client.post('/api/propresenter/stage/stream_delay', json=body).status_code, 200)
        self.assertEqual(self.presses.call_count, 1)
        # A new explicit ID is admitted after expired entries are pruned, even
        # with a one-entry limit. Never evict an unexpired consumed outcome.
        with patch.object(webui.time, 'monotonic', return_value=100 + webui._STREAM_DELAY_OPERATION_TTL), \
             patch.object(webui, '_STREAM_DELAY_OPERATION_LIMIT', 1):
            self.assertEqual(self.client.post('/api/propresenter/stage/stream_delay', json=self.body(delay_minutes=0)).status_code, 200)
        self.assertEqual(len(webui._stream_delay_operations), 1)
        self.assertEqual(self.presses.call_count, 2)

    def test_operation_validation_payload_binding_uncertainty_and_capacity(self):
        body = self.body(delay_minutes=0)
        original = copy.deepcopy(webui.utils.load_timer_presets())
        for invalid in ('short', 'x' * 129, 'x' * 16 + '/', 123, None):
            response = self.client.post('/api/propresenter/stage/stream_delay', json=dict(body, operation_id=invalid))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(FakePP.calls, [])
        self.assertEqual(webui.utils.load_timer_presets(), original)
        with patch.object(webui, '_execute_stream_delay', side_effect=RuntimeError('synthetic uncertain')) as execute:
            first = self.client.post('/api/propresenter/stage/stream_delay', json=body)
            again = self.client.post('/api/propresenter/stage/stream_delay', json=body)
        self.assertEqual(first.status_code, 500)
        self.assertEqual(again.get_json(), first.get_json())
        self.assertEqual(execute.call_count, 1)
        changed = self.client.post('/api/propresenter/stage/stream_delay', json=dict(body, delay_minutes=1))
        self.assertEqual(changed.status_code, 409)
        with patch.object(webui, '_STREAM_DELAY_OPERATION_LIMIT', 1):
            full = self.client.post('/api/propresenter/stage/stream_delay', json=self.body())
        self.assertEqual(full.status_code, 429)
        self.assertEqual(FakePP.calls, [])

    def test_inflight_duplicate_zero_delta_is_nonblocking_and_terminal_failure_not_replayed(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        body = self.body(delay_minutes=0)
        outcomes = []
        def blocked_message(pp, message):
            FakePP.calls.append(('message', message))
            entered.set()
            self.assertTrue(release.wait(5))
            return False
        def send():
            with webui.app.test_client() as client:
                outcomes.append(client.post('/api/propresenter/stage/stream_delay', json=body))
        with patch.object(FakePP, 'set_stage_message', blocked_message):
            worker = threading.Thread(target=send); worker.start()
            self.assertTrue(entered.wait(5))
            try:
                duplicate = self.client.post('/api/propresenter/stage/stream_delay', json=body)
                self.assertEqual(duplicate.status_code, 409)
            finally:
                release.set(); worker.join(5)
        self.assertFalse(worker.is_alive())
        terminal = self.client.post('/api/propresenter/stage/stream_delay', json=body)
        self.assertEqual(terminal.get_json(), outcomes[0].get_json())
        self.assertFalse(terminal.get_json()['sent'])
        self.assertEqual(self.presses.call_count, 1)
        self.assertEqual(sum(c[0] == 'set' for c in FakePP.calls), 1)

    def test_same_operation_id_is_isolated_by_principal_and_browser_session(self):
        body = self.body(delay_minutes=0)
        with webui.app.test_request_context('/api/propresenter/stage/stream_delay', method='POST', json=body):
            for principal in ({'type': 'auth_disabled', 'key': 'A'}, {'type': 'auth_disabled', 'key': 'B'},
                              {'type': 'browser_session', 'key': 'user:42'}):
                webui.g.api_principal = principal
                for sid in (('first', 'second') if principal['type'] == 'browser_session' else ('',)):
                    webui.session['_auth_session_id'] = sid
                    self.assertEqual(webui.api_prop_stage_stream_delay().status_code, 200)
        self.assertEqual(self.presses.call_count, 4)

    def test_message_only_io_allows_generic_config_and_transport_import(self):
        import json
        import threading
        import zipfile
        from pathlib import Path
        from contextlib import ExitStack
        for writer in ('generic', 'import'):
            entered, release, written = threading.Event(), threading.Event(), threading.Event()
            outcomes, errors = [], []
            self.cfg['propresenter_ip'] = 'fake.invalid'
            body = self.body(update_timer=False)
            def blocked_message(pp, message):
                entered.set()
                self.assertTrue(release.wait(5))
                return True
            def send():
                with webui.app.test_client() as client:
                    outcomes.append(client.post('/api/propresenter/stage/stream_delay', json=body))
            def write():
                try:
                    if writer == 'generic':
                        with webui.app.test_client() as client:
                            self.assertEqual(client.post('/api/config', json={'propresenter_ip': 'new.fake.invalid'}).status_code, 200)
                    else:
                        imported, _ = webui._apply_config_transport_import(Path('synthetic-import.zip'), ['config'])
                        self.assertEqual(imported[0]['id'], 'config')
                    self.assertEqual(self.cfg['propresenter_ip'], 'new.fake.invalid')
                    written.set()
                except Exception as exc:
                    errors.append(exc)
            with ExitStack() as stack:
                stack.enter_context(patch.object(FakePP, 'set_stage_message', blocked_message))
                init = stack.enter_context(patch.object(FakePP, '__init__', return_value=None))
                stack.enter_context(patch.object(webui.utils, 'save_config', side_effect=lambda cfg: self.cfg.update(cfg)))
                stack.enter_context(patch.object(webui.utils, 'reload_config'))
                stack.enter_context(patch.object(webui, '_apply_logging_config'))
                if writer == 'import':
                    with zipfile.ZipFile('synthetic-import.zip', 'w') as archive:
                        archive.writestr('payload/config.json', json.dumps(dict(self.cfg, propresenter_ip='new.fake.invalid')))
                    item = {'id': 'config', 'label': 'Config', 'path': 'config.json', 'kind': 'file'}
                    stack.enter_context(patch.object(webui, '_inspect_config_transport_zip', return_value={'items': [item]}))
                    stack.enter_context(patch.object(webui, '_create_pre_import_backup', return_value=None))
                    stack.enter_context(patch.object(webui, '_config_transport_item_map', return_value={'config': dict(item, _actual_path='synthetic-config.json')}))
                    stack.enter_context(patch.object(webui, '_clear_imported_config_caches', side_effect=lambda: self.cfg.update(json.loads(Path('synthetic-config.json').read_text()))))
                worker = threading.Thread(target=send); worker.start()
                self.assertTrue(entered.wait(5))
                writing = threading.Thread(target=write); writing.start()
                try:
                    self.assertTrue(written.wait(1), 'config/import blocked behind message IO: ' + str(errors))
                finally:
                    release.set(); worker.join(5); writing.join(5)
                self.assertEqual(errors, [])
                self.assertTrue(outcomes[0].get_json()['sent'])
                self.assertEqual(init.call_args.args, ('fake.invalid', 1025))
        self.presses.assert_not_called()

    def test_audit_failure_does_not_erase_known_execution_outcome(self):
        body = self.body()
        with patch.object(webui, 'log_event', side_effect=RuntimeError('synthetic audit unavailable')):
            response = self.client.post('/api/propresenter/stage/stream_delay', json=body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['saved'])
        self.assertTrue(response.get_json()['applied'])
        self.assertTrue(response.get_json()['sent'])
        again = self.client.post('/api/propresenter/stage/stream_delay', json=body)
        self.assertEqual(again.get_json(), response.get_json())
        self.assertEqual(self.presses.call_count, 1)

    def test_companion_instance_is_captured_before_config_reload(self):
        from unittest.mock import Mock
        old, new = Mock(connected=True), Mock(connected=True)
        old.post_command.return_value = new.post_command.return_value = True
        body = self.body()
        def reload_after_capture(**kwargs):
            self.cfg['companion_ip'] = 'new.fake.invalid'
            current[0] = new
        current = [old]
        with patch.object(webui.utils, 'get_companion', side_effect=lambda: current[0]), \
             patch.object(webui, '_queue_companion_timer_variable_sync', side_effect=reload_after_capture), \
             patch.object(webui, '_fire_timer_button_presses_now', wraps=original_fire):
            response = self.client.post('/api/propresenter/stage/stream_delay', json=body)
        self.assertTrue(response.get_json()['applied'])
        old.post_command.assert_called_once_with('location/1/0/1/press')
        new.post_command.assert_not_called()

    def test_identical_no_time_change_operation_does_not_replay(self):
        for delta in (0, 1440, -1440):
            FakePP.calls = []
            self.presses.reset_mock()
            body = self.body(delay_minutes=delta, operation_id='dedup-test-operation-' + str(delta))
            first = self.client.post('/api/propresenter/stage/stream_delay', json=body)
            self.assertEqual(first.status_code, 200)
            again = self.client.post('/api/propresenter/stage/stream_delay', json=body)
            self.assertEqual(again.status_code, 200)
            self.assertEqual(again.get_json(), first.get_json())
            self.assertEqual(self.presses.call_count, 1, 'duplicate operation replayed Companion')
            self.assertEqual(sum(c[0] == 'set' for c in FakePP.calls), 1)
            fresh = self.client.post('/api/propresenter/stage/stream_delay', json=self.body(delay_minutes=delta))
            self.assertEqual(fresh.status_code, 200)
            self.assertEqual(self.presses.call_count, 2)

    def test_device_io_releases_storage_locks_and_uses_captured_snapshot(self):
        import threading
        entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
        outcomes = []
        body = self.body()
        original_set = FakePP.SetCountdownToTime
        def blocked_set(pp, timer, value):
            entered.set()
            self.assertTrue(release.wait(5))
            return original_set(pp, timer, value)
        def send():
            with webui.app.test_client() as client:
                outcomes.append(client.post('/api/propresenter/stage/stream_delay', json=body))
        def read_and_reconfigure():
            with webui._media_operation_lock, webui._timer_mutation_lock, webui.utils._timer_presets_lock:
                current = webui.utils.load_timer_presets()
                webui.utils.save_timer_presets(list(reversed(current)))
                self.cfg['propresenter_ip'] = 'new.fake.invalid'
                self.cfg['propresenter_timer_index'] = 9
            with webui.app.test_client() as client:
                self.assertEqual(client.get('/api/timers').status_code, 200)
            read_done.set()
        with patch.object(FakePP, 'SetCountdownToTime', blocked_set), patch.object(FakePP, '__init__', return_value=None) as init:
            worker = threading.Thread(target=send)
            worker.start()
            self.assertTrue(entered.wait(5))
            reader = threading.Thread(target=read_and_reconfigure)
            reader.start()
            try:
                self.assertTrue(read_done.wait(1), 'storage/config/read blocked behind device IO')
                competing = self.client.post('/api/propresenter/stage/stream_delay', json=self.body(update_timer=False))
                self.assertEqual(competing.status_code, 409)
            finally:
                release.set()
                worker.join(5); reader.join(5)
            self.assertFalse(worker.is_alive())
            self.assertTrue(outcomes[0].get_json()['applied'])
            self.assertEqual(FakePP.calls[:3], [('set', 1, '00:01'), ('reset', 1), ('start', 1)])
            self.assertEqual(init.call_args_list[-1].args, ('fake.invalid', 1025))
            self.assertEqual(outcomes[0].get_json()['timer_presets'][1]['name'], 'Second')
            self.assertEqual(webui.utils.load_timer_presets()[0]['name'], 'Second')

    def test_checked_requires_timer_scope_and_apply_constraint(self):
        record = {'id': 981, 'name': 'synthetic', 'scopes': ['propresenter'], 'constraints': {}}
        body = self.body()
        with patch.object(webui, '_auth_enabled', return_value=True), \
             patch.object(webui, 'authenticate_service_token', return_value=(record, None)):
            headers = {'Authorization': 'Bearer synthetic-test-only'}
            message = self.client.post('/api/propresenter/stage/stream_delay', json=dict(body, update_timer=False, operation_id='message-only-operation'), headers=headers)
            self.assertEqual(message.status_code, 200)
            FakePP.calls = []
            denied = self.client.post('/api/propresenter/stage/stream_delay', json=body, headers=headers)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(FakePP.calls, [])
            record['scopes'].append('timers')
            record['constraints'] = {'allowed_paths': ['POST /api/propresenter/stage/*']}
            denied = self.client.post('/api/propresenter/stage/stream_delay', json=body, headers=headers)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(FakePP.calls, [])
            record['constraints'] = {}
            allowed = self.client.post('/api/propresenter/stage/stream_delay', json=body, headers=headers)
            self.assertEqual(allowed.status_code, 200)
            self.assertTrue(allowed.get_json()['applied'])

    def body(self, **extra):
        import uuid
        body = {'delay_minutes': 2, 'preset': 2, 'update_timer': True,
                'operation_id': uuid.uuid4().hex,
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
