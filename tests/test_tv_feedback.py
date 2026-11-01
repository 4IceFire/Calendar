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
