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
        self.client._receipts = {key: None for key in ('state', 'volume', 'sources', 'device_info', 'tv_info')}
        self.client._receipt_events = {key: threading.Event() for key in self.client._receipts}
        self.client._publish = Mock(return_value=True)

    def receipt(self, topic, payload):
        with patch.object(VidaaTV, '_on_message'):
            self.client._on_message(None, None, SimpleNamespace(topic=topic, payload=json.dumps(payload).encode()))

    def test_mute_response_does_not_finish_main_volume_read(self):
        # Production A60G sends mute first, then the actual speaker level.
        main = threading.Timer(.01, lambda: self.receipt('/broadcast/volumechange', {'volume_type': 0, 'volume_value': 0}))
        def publish(*_):
            self.receipt('/broadcast/volumechange', {'volume_type': 2, 'volume_value': 0})
            main.start()
            return True
        self.client._publish.side_effect = publish
        try:
            self.assertEqual(self.client.get_volume(timeout=.2), 0)
        finally:
            main.join()

    def test_volume_broadcast_cannot_complete_sources_or_info_requests(self):
        for getter, suffix, payload in (
            ('get_sources', 'ui_service/data/sourcelist', [{'sourceid': 'HDMI1'}]),
            ('get_device_info', 'platform_service/data/getdeviceinfo', {'model_name': '55A60GAVT'}),
            ('get_tv_info', 'platform_service/data/gettvinfo', {'brand': 'Hisense'}),
        ):
            with self.subTest(getter=getter):
                self.client._response_event = threading.Event()
                self.client._last_response = None
                self.client.on_state_change = None
                def deliver(topic, data):
                    self.client._on_message(None, None, SimpleNamespace(topic=topic, payload=json.dumps(data).encode()))
                reply = threading.Timer(.01, lambda: deliver('/remoteapp/mobile/fixture/' + suffix, payload))
                def publish(*_):
                    deliver('/broadcast/volumechange', {'volume_type': 2, 'volume_value': 0})
                    reply.start()
                    return True
                self.client._publish.side_effect = publish
                try:
                    self.assertEqual(getattr(self.client, getter)(timeout=.2), payload)
                finally:
                    reply.join()

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

    def test_broadcast_authentication_metadata_cannot_satisfy_state_read(self):
        self.client._publish.side_effect = lambda *_: (self.receipt('/broadcast/state', {'statetype': 'authenticationcode'}) or True)
        self.assertIsNone(self.client.get_state(timeout=.001))
        self.client._receipt.assert_not_called()

    def test_non_object_and_auth_payloads_are_not_physical_observations(self):
        self.receipt('/authentication', {'statetype': 'authentication'})
        self.receipt('/broadcast/state', 'invalid')
        self.client._receipt.assert_not_called()
