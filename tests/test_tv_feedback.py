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
