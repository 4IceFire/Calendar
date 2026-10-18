import unittest
from tv_observation import Observation


class ObservationTests(unittest.TestCase):
    def test_old_audio_report_cannot_change_newer_audio_classification(self):
        state = Observation()
        state.volume({'volume_type': 1, 'volume_value': 44}, at=20)
        state.volume({'volume_type': 0, 'volume_value': 22}, at=10)
        result = state.snapshot(now=21)
        self.assertEqual(result['audio'], 'external-unverified')
        self.assertIsNone(result['fields']['volume']['value'])

    def test_main_speaker_numeric_string_is_valid_but_bool_or_scale_is_not(self):
        state = Observation()
        state.volume({'volume_type': 0, 'volume_value': '23'}, at=1)
        self.assertEqual(state.snapshot(now=2)['fields']['volume']['value'], 23)
        state.volume({'volume_type': 0, 'volume_value': True}, at=3)
        state.volume({'volume_type': 0, 'volume_value': 255}, at=4)
        self.assertEqual(state.snapshot(now=5)['fields']['volume']['sampledAt'], 1)

    def test_request_is_not_observation_and_old_reports_cannot_confirm(self):
        state = Observation()
        state.report('volume', 20, at=10)
        state.request('volume_set', 40, at=11)
        self.assertEqual(state.snapshot(now=12)['fields']['volume']['value'], 20)
        self.assertEqual(state.snapshot(now=12)['command']['status'], 'pending')
        state.report('volume', 40, at=9)
        self.assertEqual(state.snapshot(now=12)['command']['status'], 'pending')
        state.report('volume', 40, at=13)
        self.assertEqual(state.snapshot(now=14)['command']['status'], 'confirmed')

    def test_mismatch_timeout_and_historical_failure_survive_recovery(self):
        state = Observation()
        state.request('volume_set', 40, at=10)
        state.report('volume', 20, at=12)
        self.assertEqual(state.snapshot(now=30)['command']['status'], 'mismatch')
        state.failure('volume_set', at=31)
        state.report('volume', 40, at=32)
        self.assertEqual(state.snapshot(now=33)['failures'][0]['operation'], 'volume_set')
        self.assertEqual(state.snapshot(now=33)['command']['status'], 'failed')

    def test_external_and_mute_reports_do_not_invent_numeric_volume(self):
        state = Observation()
        state.volume({'volume_type': 0, 'volume_value': 22}, at=10)
        state.volume({'volume_type': 2, 'volume_value': 1}, at=11)
        self.assertEqual(state.snapshot(now=12)['fields']['volume']['sampledAt'], 10)
        state.volume({'volume_type': 1, 'volume_value': 44}, at=13)
        self.assertIsNone(state.snapshot(now=14)['fields']['volume']['value'])
        self.assertEqual(state.snapshot(now=14)['audio'], 'external-unverified')

    def test_missing_feedback_is_unconfirmed_and_stale_not_zero(self):
        state = Observation()
        state.request('power_off', None, at=10)
        result = state.snapshot(now=40)
        self.assertEqual(result['command']['status'], 'unconfirmed')
        self.assertIsNone(result['fields']['volume']['value'])
        self.assertTrue(result['fields']['volume']['stale'])
