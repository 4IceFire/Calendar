import unittest
import time
from unittest.mock import Mock, patch
import hisense


class FeedbackTests(unittest.TestCase):
    def test_offline_expected_off_probe_requires_visible_page_not_command_lease(self):
        self.tv._mark_powered_off()
        self.tv.fast_until = time.monotonic() + 20
        self.tv._last_poll = self.tv._last_connect_attempt = 0
        with patch.object(self.tv, '_connect') as connect, patch.object(self.tv._stop, 'is_set', side_effect=[False, True]), patch.object(self.tv._wake_worker, 'wait'):
            self.tv._run()
        connect.assert_not_called()
        self.tv.visible_until = time.monotonic() + 5
        with patch.object(self.tv, '_connect') as connect, patch.object(self.tv._stop, 'is_set', side_effect=[False, True]), patch.object(self.tv._wake_worker, 'wait'):
            self.tv._run()
        connect.assert_called_once_with(allow_expected_off=True)

    def setUp(self):
        cfg = hisense.HisenseConfig.from_mapping({'hisense_tvs': [{'id': 'a', 'host': '192.0.2.1'}]})
        self.tv = hisense.HisenseTvController(cfg.tvs[0], cfg, client_factory=Mock())
        self.client = Mock(is_connected=True)
        self.tv._client = self.client
        self.tv._state['connected'] = True
        self.client.get_state.return_value = {'statetype': 'livetv', 'sourceid': 'HDMI1'}
        self.client.get_volume.return_value = 20

    def test_unreachable_control_port_is_not_misdiagnosed_as_pairing(self):
        factory = Mock()
        detector = Mock(return_value=2000)
        tv = hisense.HisenseTvController(self.tv.config, self.tv.service_config,
                                        client_factory=factory, protocol_detector=detector,
                                        reachability_probe=lambda *_: (False, 'timed out'))
        self.assertFalse(tv._connect())
        self.assertEqual(tv.status().get('readFailureReason'), 'control_unreachable')
        self.assertIn('unreachable', tv.status()['lastError'].lower())
        factory.assert_not_called()
        detector.assert_not_called()

    def test_poll_preserves_only_current_connection_attempt_diagnosis(self):
        factory = Mock()
        detector = Mock(return_value=2000)
        probe = Mock(return_value=(False, 'timed out'))
        tv = hisense.HisenseTvController(self.tv.config, self.tv.service_config,
                                        client_factory=factory, protocol_detector=detector,
                                        reachability_probe=probe)
        tv.outcome_callback = Mock()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                tv._poll()
                status = tv.status()
                self.assertEqual(status['readFailureReason'], 'control_unreachable')
                self.assertEqual(status['lastError'],
                                 'TV control port is unreachable. Check power, network and configured IP before pairing.')
                self.assertEqual(tv.outcome_callback.call_args.args[2]['reason'], 'control_unreachable')
                self.assertFalse(status['connected'])
        self.assertEqual(probe.call_count, 2)
        factory.assert_not_called()
        detector.assert_not_called()

        # A later attempt with no fresh diagnosis must not reuse unreachable.
        with patch.object(tv, '_ensure_connected', side_effect=RuntimeError('password=PRIVATE')):
            tv._poll()
        status = tv.status()
        self.assertEqual(status['readFailureReason'], 'connection_failed')
        self.assertEqual(status['lastError'], 'TV feedback request failed; check connection or setup')
        self.assertEqual(tv.outcome_callback.call_args.args[2]['reason'], 'connection_failed')
        self.assertNotIn('PRIVATE', str(status))

    def test_feedback_errors_classify_stage_without_logging_exception_text(self):
        for stage in ('connection_failed', 'feedback_error'):
            with self.subTest(stage=stage):
                self.tv._read_failure = False
                self.tv._read_failure_reason = ''
                self.tv._client = self.client
                self.tv._state['connected'] = True
                self.tv.outcome_callback = Mock()
                if stage == 'connection_failed':
                    with patch.object(self.tv, '_ensure_connected', side_effect=RuntimeError('password=PRIVATE')):
                        self.tv._poll()
                else:
                    self.client.get_state.side_effect = RuntimeError('token=PRIVATE')
                    self.tv._poll()
                result = self.tv.outcome_callback.call_args.args[2]
                self.assertEqual(result.get('reason'), stage)
                self.assertNotIn('PRIVATE', str(result))
                self.assertNotIn('PRIVATE', str(self.tv.status()))

    def test_feedback_failure_callback_explains_timeout_without_secrets(self):
        self.tv.outcome_callback = Mock()
        self.client.get_state.return_value = None
        self.client.get_volume.return_value = None
        self.tv._poll()
        result = self.tv.outcome_callback.call_args.args[2]
        self.assertEqual(result.get('reason'), 'feedback_timeout')
        self.assertEqual(self.tv.status().get('readFailureReason'), 'feedback_timeout')

    def test_retained_power_snapshot_is_last_known_not_fresh_or_confirmation(self):
        self.tv.observation.request('power_on', None)
        self.tv._state['lastSeen'] = 17
        self.tv._on_state_change({'statetype': 'sourceswitch', 'sourceid': 'HDMI1',
                                  '_tdeck_retained': True})
        status = self.tv.status()
        power = status['observation']['fields']['power']
        self.assertIsNone(power['sampledAt'])
        self.assertEqual(power['value'], 'on')
        self.assertTrue(power['stale'])
        self.assertEqual(power['provenance'], 'broker_retained')
        self.assertEqual(status['observation']['command']['status'], 'pending')
        self.assertEqual(status['lastSeen'], 17)
        self.assertEqual(status['power'], 'on')

    @patch('hisense.time.time', return_value=100.0)
    def test_retained_on_after_live_off_preserves_intent_and_legacy_projection(self, _clock):
        persisted = Mock()
        self.tv._expected_off_callback = persisted
        self.tv._mark_powered_off()
        self.tv._on_state_change({'statetype': 'fake_sleep_0', 'sourceid': 'HDMI2'})
        before = self.tv.status()
        persisted.reset_mock()
        self.tv._on_state_change({'statetype': 'livetv', 'sourceid': 'HDMI1',
                                  '_tdeck_retained': True})
        status = self.tv.status()
        self.assertEqual(status['observation']['fields']['power']['value'], 'off')
        self.assertTrue(status['expectedOff'])
        self.assertEqual(status['power'], 'off')
        self.assertEqual(status['source'], 'HDMI2')
        self.assertEqual(status['observation']['fields'], before['observation']['fields'])
        self.assertEqual(status['lastSeen'], before['lastSeen'])
        persisted.assert_not_called()
        self.tv._on_state_change({'statetype': 'livetv', 'sourceid': 'HDMI1'})
        self.assertFalse(self.tv.status()['expectedOff'])
        persisted.assert_called_once_with('a', False)

    def test_retained_volume_does_not_confirm_or_refresh_main_speaker_feedback(self):
        self.tv.observation.request('volume_set', 23)
        self.tv._on_state_change({'_tdeck_volume': True, '_tdeck_retained': True,
                                  'volume_type': 0, 'volume_value': 23})
        observation = self.tv.status()['observation']
        self.assertIsNone(observation['fields']['volume']['sampledAt'])
        self.assertEqual(observation['fields']['volume']['value'], 23)
        self.assertTrue(observation['fields']['volume']['stale'])
        self.assertEqual(observation['command']['status'], 'pending')

    def test_power_on_uses_explicit_broker_snapshot_without_claiming_confirmation(self):
        class SnapshotClient:
            strict_observations = True
            wake_uses_wol = True
            is_connected = True
            get_power_state = Mock(return_value={'statetype': 'sourceswitch',
                                                  '_tdeck_retained': True})
            get_state = Mock(return_value=None)
            send_key = Mock()
        client = SnapshotClient()
        self.tv._client = client
        self.tv._state['lastSeen'] = 17
        self.tv._wake = Mock()
        command = hisense._Command('power_on')
        self.tv._execute_command(command)
        self.assertTrue(command.result['ok'])
        self.assertFalse(command.result['confirmed'])
        self.assertFalse(command.result['pending'])
        self.assertEqual(self.tv.status()['lastSeen'], 17)
        client.get_power_state.assert_called_once()
        client.get_state.assert_not_called()
        client.send_key.assert_not_called()
        self.tv._wake.assert_not_called()

    def native_fixture(self, payload):
        import json
        from types import SimpleNamespace
        from dataclasses import replace
        from vidaa_observed import ObservedVidaaTV
        from pyvidaa.topics import TOPIC_STATE_RESPONSE
        broker = Mock()
        broker.publish.return_value = SimpleNamespace(rc=0)
        with patch('pyvidaa.client.mqtt.Client', return_value=broker):
            client = ObservedVidaaTV('fixture.invalid', client_id='fixture',
                                     use_ssl=False, enable_persistence=False,
                                     on_state_change=self.tv._on_state_change)
        client._connected = True
        broker.subscribe.side_effect = lambda _: (client._on_message(broker, None,
            SimpleNamespace(topic=TOPIC_STATE_RESPONSE,
                            payload=json.dumps(payload).encode(), retain=True)) or (0, 1))
        self.tv.config = replace(self.tv.config, mac='02:00:00:00:00:01')
        self.tv._client = client
        self.tv._wake = Mock(return_value=True)
        self.tv._state['lastSeen'] = 17
        return client, broker

    def test_real_adapter_silent_getter_retained_standby_wakes_once_without_confirmation(self):
        import threading
        client, broker = self.native_fixture({'statetype': 'fake_sleep_0'})
        clock = [100.0]
        def wait(event, duration=None):
            self.assertFalse(self.tv._lock._is_owned())
            self.assertFalse(client._transport_lock._is_owned())
            if event.is_set():
                return True
            clock[0] += duration or 0
            return False
        with patch('time.monotonic', side_effect=lambda: clock[0]), \
             patch.object(threading.Event, 'wait', autospec=True, side_effect=wait):
            self.assertIsNone(client.get_state(timeout=.001))
            command = hisense._Command('power_on')
            self.tv._execute_command(command)
            self.tv._begin_power_on()
            self.tv._poll()
        self.assertTrue(command.result['ok'])
        self.assertTrue(command.result['pending'])
        self.assertFalse(command.result['confirmed'])
        self.assertEqual([c.args[1] for c in broker.publish.call_args_list].count('KEY_POWER'), 1)
        self.tv._wake.assert_not_called()
        status = self.tv.status()
        self.assertTrue(status['powerOnPending'])
        self.assertEqual(status['lastSeen'], 17)
        self.assertIsNone(status['observation']['fields']['power']['sampledAt'])
        self.assertEqual(status['observation']['command']['status'], 'pending')

    def test_live_on_before_guarded_return_rearms_the_next_native_wake(self):
        import json
        import threading
        from types import SimpleNamespace
        from pyvidaa.topics import TOPIC_STATE_RESPONSE
        client, broker = self.native_fixture({'statetype': 'fake_sleep_0'})
        guarded = client.wake_from_standby
        observed = []
        def receipt(payload):
            client._on_message(broker, None, SimpleNamespace(
                topic=TOPIC_STATE_RESPONSE, payload=json.dumps(payload).encode(), retain=False))
        def wake_then_live_on(**kwargs):
            outcome = guarded(**kwargs)
            if outcome == 'sent':
                worker = threading.Thread(target=lambda: receipt({'statetype': 'livetv'}))
                worker.start()
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive(), 'Callback must not wait on a controller lock held through I/O')
                observed.append((self.tv._power_key_sent, self.tv._pending_power_on))
            return outcome
        with patch('time.monotonic', return_value=100.0) as clock, \
             patch.object(client, 'wake_from_standby', side_effect=wake_then_live_on):
            client.get_power_state(timeout=.01)
            clock.return_value = 103.0
            first = hisense._Command('power_on')
            self.tv._execute_command(first)
            self.assertTrue(first.result['ok'])
            self.assertEqual(observed, [(False, False)])
            self.assertFalse(self.tv._power_key_sent, 'The older send result must not overwrite live-On rearming')
            receipt({'statetype': 'fake_sleep_0'})
            clock.return_value = 106.0
            second = hisense._Command('power_on')
            self.tv._execute_command(second)
            self.assertTrue(second.result['ok'])
        self.assertEqual(observed, [(False, False), (False, False)])
        self.assertEqual([c.args[1] for c in broker.publish.call_args_list].count('KEY_POWER'), 2)
        self.tv._wake.assert_not_called()

    def test_pending_wol_reconnect_standby_uses_one_guarded_key(self):
        import threading
        from dataclasses import replace
        self.tv.config = replace(self.tv.config, mac='02:00:00:00:00:01')
        self.tv._client = None
        self.tv._wake = Mock(return_value=True)
        self.tv.observation.request('power_on', None)
        self.assertTrue(self.tv._begin_power_on())
        self.tv._wake.assert_called_once()
        client, broker = self.native_fixture({'statetype': 'fake_sleep_0'})
        self.tv._state['connected'] = True
        with patch('time.monotonic', return_value=100) as clock, \
             patch.object(threading.Event, 'wait', autospec=True, side_effect=lambda event, *_: event.is_set()):
            client.get_power_state(timeout=.001)
            clock.return_value = 103
            self.tv._poll()
            self.tv._begin_power_on()
            self.tv._poll()
        self.assertEqual([c.args[1] for c in broker.publish.call_args_list].count('KEY_POWER'), 1)
        self.assertTrue(self.tv._power_key_sent)
        self.assertTrue(self.tv.status()['powerOnPending'])
        self.assertIsNone(self.tv.status()['observation']['fields']['power']['sampledAt'])
        self.assertEqual(self.tv.status()['observation']['command']['status'], 'pending')

    def test_native_failed_key_attempt_is_consumed_across_polls_and_repeated_on(self):
        import threading
        from types import SimpleNamespace
        for failure in ('rejected', 'raised'):
            with self.subTest(failure=failure):
                self.setUp()  # Independent failure modes, not a second wake cycle.
                client, broker = self.native_fixture({'statetype': 'fake_sleep_0'})
                attempted = []
                def publish(_, payload):
                    if payload == 'KEY_POWER':
                        attempted.append(payload)
                        if failure == 'raised':
                            raise OSError('uncertain key publish')
                        return SimpleNamespace(rc=4)
                    return SimpleNamespace(rc=0)
                broker.publish.side_effect = publish
                clock = [100.0]
                def wait(event, duration=None):
                    if event.is_set():
                        return True
                    clock[0] += duration or 0
                    return False
                with patch('time.monotonic', side_effect=lambda: clock[0]), \
                     patch.object(threading.Event, 'wait', autospec=True, side_effect=wait):
                    self.assertTrue(self.tv._begin_power_on())
                    self.assertTrue(self.tv._power_key_sent)
                    self.tv._begin_power_on()
                    self.tv._poll()
                    self.tv._poll()
                self.assertEqual(attempted, ['KEY_POWER'])
                self.tv._wake.assert_called_once()
                self.assertTrue(self.tv.status()['powerOnPending'])

    def test_native_uncertain_key_failed_wol_cannot_retoggle_after_new_on_or_reconnect(self):
        import threading
        from types import SimpleNamespace
        for failure in ('rejected', 'raised'):
            with self.subTest(failure=failure):
                self.setUp()
                client, broker = self.native_fixture({'statetype': 'fake_sleep_0'})
                self.tv._wake.return_value = False
                attempted = []
                def publish(_, payload):
                    if payload == 'KEY_POWER':
                        attempted.append(payload)
                        if failure == 'raised':
                            raise OSError('uncertain key publish')
                        return SimpleNamespace(rc=4)
                    return SimpleNamespace(rc=0)
                broker.publish.side_effect = publish
                clock = [100.0]
                def wait(event, duration=None):
                    if event.is_set():
                        return True
                    clock[0] += duration or 0
                    return False
                with patch('time.monotonic', side_effect=lambda: clock[0]), \
                     patch.object(threading.Event, 'wait', autospec=True, side_effect=wait):
                    first = hisense._Command('power_on')
                    self.tv._execute_command(first)
                    self.assertFalse(first.result['ok'])
                    self.assertFalse(self.tv._pending_power_on)
                    self.assertTrue(self.tv._power_key_sent, 'failed WoL must not unconsume the uncertain key')
                    for transition in ('new_on', 'expired', 'reconnect'):
                        if transition == 'expired':
                            self.tv._wake.return_value = True
                            self.assertTrue(self.tv._begin_power_on())
                            self.assertTrue(self.tv._pending_power_on)
                            self.tv._wake.return_value = False
                            clock[0] += 60
                            with patch.object(self.tv._stop, 'is_set', side_effect=[False, True]), \
                                 patch.object(self.tv, '_poll'), patch.object(self.tv._wake_worker, 'wait'):
                                self.tv._run()
                            self.assertFalse(self.tv._pending_power_on)
                            self.assertTrue(self.tv._power_key_sent)
                        elif transition == 'reconnect':
                            client._on_disconnect(broker, None, 1)
                            client._on_connect(broker, None, {}, 0)
                        command = hisense._Command('power_on')
                        self.tv._execute_command(command)
                        self.assertFalse(command.result['ok'])
                        self.assertEqual(attempted, ['KEY_POWER'], transition)
                self.assertIsNone(self.tv.status()['observation']['fields']['power']['sampledAt'])

    def test_consumed_key_rearms_only_after_live_on_or_successful_off_intent(self):
        import json
        import threading
        from types import SimpleNamespace
        from pyvidaa.topics import TOPIC_STATE_RESPONSE
        for new_cycle in ('live_on', 'off_intent'):
            with self.subTest(new_cycle=new_cycle):
                self.setUp()
                client, broker = self.native_fixture({'statetype': 'fake_sleep_0'})
                clock = [100.0]
                def wait(event, duration=None):
                    if event.is_set():
                        return True
                    clock[0] += duration or 0
                    return False
                def receipt(payload, retained=False):
                    client._on_message(broker, None, SimpleNamespace(topic=TOPIC_STATE_RESPONSE,
                        payload=json.dumps(payload).encode(), retain=retained))
                with patch('time.monotonic', side_effect=lambda: clock[0]), \
                     patch.object(threading.Event, 'wait', autospec=True, side_effect=wait):
                    self.assertTrue(self.tv._begin_power_on())
                    self.tv._clear_pending_power_on()
                    receipt({'statetype': 'livetv'}, retained=True)
                    self.assertTrue(self.tv._power_key_sent, 'retained ON cannot rearm a consumed toggle')
                    if new_cycle == 'live_on':
                        receipt({'statetype': 'livetv'})
                    else:
                        command = hisense._Command('power_off')
                        self.tv._execute_command(command)
                        self.assertTrue(command.result['ok'])
                        self.assertTrue(self.tv.status()['expectedOff'])
                        self.tv._client = client
                        client._connected = True
                        self.tv._state['connected'] = True
                    self.assertFalse(self.tv._power_key_sent)
                    receipt({'statetype': 'fake_sleep_0'})
                    self.tv._state['connected'] = True
                    self.assertTrue(self.tv._begin_power_on())
                self.assertFalse(self.tv.status()['expectedOff'])
                self.assertEqual([c.args[1] for c in broker.publish.call_args_list].count('KEY_POWER'), 2)

    def test_real_native_known_on_never_sends_key_or_wol(self):
        client, broker = self.native_fixture({'statetype': 'livetv'})
        command = hisense._Command('power_on')
        self.tv._execute_command(command)
        self.assertTrue(command.result['ok'])
        self.assertFalse(command.result['confirmed'])
        self.assertNotIn('KEY_POWER', [c.args[1] for c in broker.publish.call_args_list])
        self.tv._wake.assert_not_called()
        self.assertIsNone(self.tv.status()['observation']['fields']['power']['sampledAt'])
        self.assertEqual(self.tv.status()['lastSeen'], 17)

    @patch('hisense.time.time', return_value=100.0)
    def test_native_retained_on_control_precondition_does_not_override_fresh_off(self, _clock):
        client, broker = self.native_fixture({'statetype': 'livetv'})
        self.tv._on_state_change({'statetype': 'fake_sleep_0'})
        before = self.tv.status()
        command = hisense._Command('power_on')
        self.tv._execute_command(command)
        self.assertTrue(command.result['ok'])
        self.assertFalse(command.result['confirmed'])
        self.assertEqual(self.tv.status()['power'], 'off')
        self.assertEqual(self.tv.status()['observation']['fields']['power'],
                         before['observation']['fields']['power'])
        self.assertEqual(self.tv.status()['lastSeen'], before['lastSeen'])
        self.tv._wake.assert_not_called()
        self.assertNotIn('KEY_POWER', [c.args[1] for c in broker.publish.call_args_list])

    def test_real_native_unknown_auth_error_source_only_or_silent_uses_wol_only(self):
        import threading
        for payload in ({'statetype': 'unknown'}, {'statetype': 'authentication'},
                        {'statetype': 'error'}, {'statetype': 'livetv', 'error': 'denied'},
                        {'sourceid': 'HDMI1'}, None):
            with self.subTest(payload=payload):
                self.tv._clear_pending_power_on()
                client, broker = self.native_fixture(payload)
                if payload is None:
                    broker.subscribe.side_effect = None
                    broker.subscribe.return_value = (0, 1)
                with patch.object(threading.Event, 'wait', autospec=True, side_effect=lambda event, *_: event.is_set()):
                    self.assertTrue(self.tv._begin_power_on())
                self.assertNotIn('KEY_POWER', [c.args[1] for c in broker.publish.call_args_list])
                self.tv._wake.assert_called_once()
                self.assertTrue(self.tv.status()['powerOnPending'])
                self.assertIsNone(self.tv.status()['observation']['fields']['power']['sampledAt'])

    def test_native_without_guarded_helper_never_uses_raw_toggle_even_after_standby(self):
        from dataclasses import replace
        class SnapshotClient:
            strict_observations = True
            wake_uses_wol = True
            is_connected = True
            is_muted = False
            get_power_state = Mock(return_value={'statetype': 'fake_sleep_0',
                                                  '_tdeck_retained': False})
            get_state = Mock(return_value={'statetype': 'fake_sleep_0'})
            get_volume = Mock(return_value=None)
            send_key = Mock(return_value=True)
        client = SnapshotClient()
        self.tv.config = replace(self.tv.config, mac='02:00:00:00:00:01')
        self.tv._client = client
        self.tv._wake = Mock(return_value=True)
        command = hisense._Command('power_on')
        self.tv._execute_command(command)
        self.assertTrue(command.result['ok'])
        self.assertTrue(command.result['pending'])
        client.send_key.assert_not_called()
        self.tv._wake.assert_called_once()
        self.tv._poll()
        client.send_key.assert_not_called()

    def test_pending_native_wake_reads_broker_snapshot_without_faking_live_confirmation(self):
        class SnapshotClient:
            strict_observations = True
            wake_uses_wol = True
            is_connected = True
            is_muted = False
            get_power_state = Mock(return_value={'statetype': 'sourceswitch',
                                                  '_tdeck_retained': True})
            get_state = Mock(return_value=None)
            get_volume = Mock(return_value=None)
            send_key = Mock()
        client = SnapshotClient()
        self.tv._client = client
        self.tv.observation.request('power_on', None)
        self.tv._pending_power_on = True
        self.tv._state['lastSeen'] = 17
        self.tv._poll()
        self.assertFalse(self.tv.status()['powerOnPending'])
        self.assertEqual(self.tv.status()['observation']['command']['status'], 'pending')
        self.assertIsNone(self.tv.status()['observation']['fields']['power']['sampledAt'])
        self.assertEqual(self.tv.status()['lastSeen'], 17)
        client.get_power_state.assert_called_once()
        client.send_key.assert_not_called()

    def test_authentication_metadata_is_not_physical_power_feedback(self):
        self.tv._on_state_change({'statetype': 'authenticationcode'})
        state = self.tv.status()
        self.assertEqual(state['power'], 'unknown')
        self.assertIsNone(state['observation']['fields']['power']['value'])

    def test_silent_poll_does_not_advance_last_device_receipt(self):
        self.tv._on_state_change({'statetype': 'livetv', 'sourceid': 'HDMI1'})
        stamp = self.tv.status()['lastSeen']
        self.client.get_state.return_value = None
        self.client.get_volume.return_value = None
        self.tv._poll()
        self.assertEqual(self.tv.status()['lastSeen'], stamp)

    def test_full_poll_preserves_newer_callback_last_seen_after_snapshot(self):
        self.client.get_sources.return_value = []
        with patch('hisense.time.time', return_value=100.0) as clock:
            def device_info(**_):
                # Full-info requests happen after the poll's observation snapshot.
                self.assertEqual(self.tv.observation.snapshot()['fields']['power']['sampledAt'], 100.0)
                clock.return_value = 200.0
                self.tv._on_state_change({'statetype': 'livetv', 'sourceid': 'HDMI2'})
                self.assertEqual(self.tv.status()['lastSeen'], 200.0)
                return {'model_name': 'Test TV'}

            self.client.get_device_info.side_effect = device_info
            self.tv._poll(full=True)
            self.assertEqual(self.tv.status()['lastSeen'], 200.0)
            self.assertEqual(self.tv.status()['model'], 'Test TV')

    def test_fresh_mute_or_source_feedback_is_not_reported_as_total_read_failure(self):
        class StrictClient:
            strict_observations = True
            is_connected = True
            is_muted = False
            get_state = Mock(return_value=None)
            get_volume = Mock()
        client = StrictClient()
        self.tv._client = client
        for payload in ({'_tdeck_volume': True, 'volume_type': 2, 'volume_value': 0}, {'sourceid': 'HDMI1'}):
            with self.subTest(payload=payload):
                client.get_volume.side_effect = lambda **_: self.tv._on_state_change(payload)
                self.tv._poll()
                self.assertFalse(self.tv.status()['readFailure'])
                self.assertIsNone(self.tv.status()['observation']['fields']['volume']['value'])

    def test_idle_poll_does_not_erase_device_reported_power_or_refresh_its_age(self):
        class StrictClient:
            strict_observations = True
            is_connected = True
            is_muted = False
            get_state = Mock(return_value=None)
            get_volume = Mock(return_value=0)
        self.tv._client = StrictClient()
        self.tv._on_state_change({'statetype': 'sourceswitch', 'sourceid': 'HDMI1'})
        stamp = self.tv.status()['observation']['fields']['power']['sampledAt']
        self.tv._poll()
        state = self.tv.status()
        self.assertEqual(state['power'], 'on')
        self.assertEqual(state['observation']['fields']['power']['sampledAt'], stamp)
        self.assertTrue(self.tv.observation.snapshot(now=stamp + 20)['fields']['power']['stale'])
        self.assertFalse(state['readFailure'])

    def test_accepted_ignored_volume_is_not_observed(self):
        self.tv._poll()
        self.tv._execute_command(hisense._Command('volume_set', 40))
        state = self.tv.status()
        self.assertEqual(state['volume'], 20)
        self.assertEqual(state['observation']['fields']['volume']['value'], 20)
        self.assertEqual(state['observation']['command']['requested'], 40)
        self.assertEqual(state['observation']['command']['status'], 'pending')

    def test_failure_is_sanitized_and_not_cleared_by_poll(self):
        self.client.set_volume.side_effect = RuntimeError('password=PRIVATE')
        command = hisense._Command('volume_set', 40)
        self.tv._execute_command(command)
        self.assertNotIn('PRIVATE', str(command.result))
        self.tv._poll()
        result = self.tv.status()['observation']
        self.assertEqual(result['failures'][0]['operation'], 'volume_set')
        self.assertNotIn('PRIVATE', str(result))

    def test_power_off_bookkeeping_is_not_observation(self):
        self.tv._mark_powered_off()
        self.assertIsNone(self.tv.status()['observation']['fields']['power']['value'])

    def test_accepted_ignored_source_keeps_reported_input_and_mismatches(self):
        self.tv._poll()
        self.tv._execute_command(hisense._Command('source', 'HDMI2'))
        state = self.tv.status()['observation']
        self.assertEqual(state['fields']['source']['value'], 'HDMI1')
        self.assertEqual(state['command']['status'], 'pending')
        later = self.tv.observation.snapshot(now=state['command']['requestedAt'] + 16)
        self.assertEqual(later['command']['status'], 'mismatch')
        self.tv._on_state_change({'statetype': 'livetv', 'sourceid': 'HDMI2'})
        self.assertEqual(self.tv.status()['observation']['command']['status'], 'confirmed')

    def test_read_failure_history_is_bounded_and_recovery_does_not_erase_it(self):
        self.client.get_state.return_value = None
        self.client.get_volume.return_value = None
        self.tv._poll(); self.tv._poll()
        failed = self.tv.status()
        self.assertTrue(failed['readFailure'])
        self.assertEqual(len(failed['observation']['failures']), 1)
        self.client.get_state.return_value = {'statetype': 'livetv', 'sourceid': 'HDMI1'}
        self.client.get_volume.return_value = 20
        self.tv._poll()
        recovered = self.tv.status()
        self.assertFalse(recovered['readFailure'])
        self.assertEqual(recovered['observation']['failures'][0]['operation'], 'status_read')

    def test_only_fresh_standby_report_confirms_power_off(self):
        self.tv._poll()
        self.tv._execute_command(hisense._Command('power_off'))
        self.assertEqual(self.tv.status()['observation']['command']['status'], 'pending')
        self.tv._on_state_change({'statetype': 'fake_sleep_0'})
        self.assertEqual(self.tv.status()['observation']['command']['status'], 'confirmed')

    def test_disconnected_client_cannot_confirm_later_commands(self):
        profile = hisense.HisenseCertificateProfile('fake', 'Fake', __file__, __file__)
        self.tv._make_client(profile, use_dynamic_auth=False, auth_method=None)
        old_callback = self.tv._client_factory.call_args.kwargs['on_state_change']
        self.tv._disconnect()
        self.tv.observation.request('source', 'HDMI2')
        old_callback({'statetype': 'livetv', 'sourceid': 'HDMI2'})
        self.assertIsNone(self.tv.status()['observation']['fields']['source']['value'])
        self.assertEqual(self.tv.status()['observation']['command']['status'], 'pending')

    def test_physical_remote_on_report_clears_intentional_off_without_wake(self):
        self.tv._mark_powered_off()
        self.tv._on_state_change({'statetype': 'livetv', 'sourceid': 'HDMI1'})
        self.assertFalse(self.tv.status()['expectedOff'])
        self.assertEqual(self.tv.status()['observation']['fields']['power']['value'], 'on')
