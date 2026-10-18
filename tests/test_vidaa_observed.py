import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from vidaa_observed import ObservedVidaaTV
from pyvidaa import VidaaTV


class StrictReceiptTests(unittest.TestCase):
    def setUp(self):
        self.client = ObservedVidaaTV.__new__(ObservedVidaaTV)
        self.client.client_id = 'fixture'
        self.client._state = {'statetype': 'livetv'}
        self.client._cached_volume = 90
        self.client._receipt = Mock()
        self.client._receipts = {'state': None, 'volume': None}
        self.client._receipt_events = {key: threading.Event() for key in self.client._receipts}
        self.client._publish = Mock(return_value=True)

    def receipt(self, topic, payload):
        with patch.object(VidaaTV, '_on_message'):
            self.client._on_message(None, None, SimpleNamespace(topic=topic, payload=json.dumps(payload).encode()))

    def test_cached_getter_fallback_is_never_returned_or_re_timestamped(self):
        self.assertIsNone(self.client.get_state(timeout=.001))
        self.assertIsNone(self.client.get_volume(timeout=.001))
        self.client._receipt.assert_not_called()

    def test_unchanged_device_receipt_is_fresh_and_external_reports_reach_observer(self):
        self.client._publish.side_effect = lambda *_: (self.receipt('/broadcast/state', {'statetype': 'livetv'}) or True)
        self.assertEqual(self.client.get_state(timeout=.001), {'statetype': 'livetv'})
        self.client._publish.side_effect = lambda *_: (self.receipt('/broadcast/volumechange', {'volume_type': 1, 'volume_value': 45}) or True)
        self.assertIsNone(self.client.get_volume(timeout=.001))
        self.assertTrue(self.client._receipt.call_args.args[0]['_tdeck_volume'])

    def test_non_object_and_auth_payloads_are_not_physical_observations(self):
        self.receipt('/authentication', {'statetype': 'authentication'})
        self.receipt('/broadcast/state', 'invalid')
        self.client._receipt.assert_not_called()
