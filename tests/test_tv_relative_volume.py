"""Real native adapter/controller with only the MQTT transport simulated."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hisense import HisenseConfig, HisenseTvController, _Command
from pyvidaa.topics import TOPIC_GET_STATE, TOPIC_GET_VOLUME, TOPIC_SET_VOLUME, TOPIC_STATE_RESPONSE, get_topic
from vidaa_observed import ObservedVidaaTV


class NativeRelativeVolumeTests(unittest.TestCase):
    def setUp(self):
        config = HisenseConfig.from_mapping({'hisense_tvs': [
            {'id': 'fixture', 'name': 'Fixture', 'host': 'fixture.invalid'}]})
        self.controller = HisenseTvController(config.tvs[0], config)
        self.broker = Mock()
        self.broker.publish.return_value = SimpleNamespace(rc=0)
        with patch('pyvidaa.client.mqtt.Client', return_value=self.broker):
            self.tv = ObservedVidaaTV('fixture.invalid', client_id='fixture',
                                      use_ssl=False, enable_persistence=False,
                                      on_state_change=self.controller._on_state_change)
        self.tv._connected = True
        self.controller._client = self.tv
        self.controller._state['connected'] = True
        self.level = 30
        self.apply_write = False
        self.at_write = []
        self.broker.publish.side_effect = self.publish

    def receipt(self, payload, topic):
        self.tv._on_message(self.broker, None, SimpleNamespace(
            topic=topic, payload=json.dumps(payload).encode(), retain=False))

    def publish(self, topic, payload):
        if topic == get_topic(TOPIC_GET_VOLUME, 'fixture'):
            self.receipt({'volume_type': 0, 'volume_value': self.level},
                         '/remoteapp/mobile/broadcast/ui_service/volume')
        elif topic == get_topic(TOPIC_GET_STATE, 'fixture'):
            self.receipt({'statetype': 'sourceswitch', 'sourceid': 'HDMI1'}, TOPIC_STATE_RESPONSE)
        elif topic == get_topic(TOPIC_SET_VOLUME, 'fixture'):
            self.at_write.append(self.controller.observation.snapshot()['command'])
            if self.apply_write:
                self.level = int(payload)
        return SimpleNamespace(rc=0)

    def execute(self, action):
        command = _Command(action=action)
        self.controller._execute_command(command)
        return command.result

    def test_external_audio_keeps_one_key_passthrough_without_numeric_confirmation(self):
        def publish(topic, payload):
            if topic == get_topic(TOPIC_GET_VOLUME, 'fixture'):
                self.receipt({'volume_type': 1, 'volume_value': 55}, '/broadcast/volume')
            elif topic == get_topic(TOPIC_GET_STATE, 'fixture'):
                self.receipt({'statetype': 'sourceswitch'}, TOPIC_STATE_RESPONSE)
            return SimpleNamespace(rc=0)
        self.broker.publish.side_effect = publish
        self.controller.observation.volume({'volume_value': 20})
        with patch.object(threading.Event, 'wait', autospec=True,
                          side_effect=lambda event, *_: event.is_set()):
            result = self.execute('volume_up')
        self.assertTrue(result['ok'])
        self.assertTrue(result['pending'])
        self.assertFalse(result['confirmed'])
        self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list].count('KEY_VOLUMEUP'), 1)
        self.assertNotIn(get_topic(TOPIC_SET_VOLUME, 'fixture'),
                         [c.args[0] for c in self.broker.publish.call_args_list])
        self.assertEqual(result['tv']['observation']['audio'], 'external-unverified')
        self.receipt({'volume_value': 21}, '/broadcast/volume')
        self.assertEqual(self.controller.observation.snapshot()['command']['status'], 'pending')

    def test_invalid_or_unavailable_preread_never_authorizes_numeric_write(self):
        for payload, retained in (
            (None, False), ({'volume_value': 30}, True),
            ({'volume_type': 1, 'volume_value': 30}, False),
            ({'volume_type': 2, 'volume_value': 0}, False),
            ({'volume_value': True}, False), ({'volume_value': -1}, False),
            ({'volume_value': 101}, False), ({'volume_value': 'unknown'}, False),
        ):
            for action in ('volume_up', 'volume_down'):
                with self.subTest(payload=payload, retained=retained, action=action):
                    self.tv._cached_volume = 42
                    self.receipt({'volume_value': 42}, '/broadcast/volume')
                    self.controller.observation.volume({'volume_value': 42})
                    self.at_write.clear()
                    self.broker.publish.reset_mock()
                    def publish(topic, value):
                        if topic == get_topic(TOPIC_GET_VOLUME, 'fixture') and payload is not None:
                            self.tv._on_message(self.broker, None, SimpleNamespace(
                                topic='/broadcast/volume', payload=json.dumps(payload).encode(), retain=retained))
                        return SimpleNamespace(rc=0)
                    self.broker.publish.side_effect = publish
                    with patch.object(threading.Event, 'wait', autospec=True,
                                      side_effect=lambda event, *_: event.is_set()):
                        result = self.execute(action)
                    self.assertTrue(result['ok'])
                    self.assertFalse(result['confirmed'])
                    self.assertTrue(result['pending'])
                    self.assertEqual(result['tv']['observation']['command']['status'], 'pending')
                    self.assertIsNone(result['tv']['observation']['command']['baseline'])
                    calls = self.broker.publish.call_args_list
                    self.assertNotIn(get_topic(TOPIC_SET_VOLUME, 'fixture'), [c.args[0] for c in calls])
                    self.assertEqual([c.args[1] for c in calls if str(c.args[1]).startswith('KEY_')],
                                     ['KEY_VOLUMEUP' if action == 'volume_up' else 'KEY_VOLUMEDOWN'])
                    if payload and payload.get('volume_type') == 1:
                        self.assertEqual(result['tv']['observation']['audio'], 'external-unverified')

    def test_failed_numeric_publish_is_single_attempt_without_key_fallback(self):
        for failure in ('rejected', 'exception'):
            with self.subTest(failure=failure):
                self.at_write.clear()
                self.broker.publish.reset_mock()
                def publish(topic, payload):
                    result = self.publish(topic, payload)
                    if topic == get_topic(TOPIC_SET_VOLUME, 'fixture'):
                        if failure == 'exception':
                            raise OSError('uncertain publication')
                        return SimpleNamespace(rc=1)
                    return result
                self.broker.publish.side_effect = publish
                result = self.execute('volume_up')
                self.assertFalse(result['ok'])
                self.assertEqual(len(self.at_write), 1)
                self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list], ['', '31'])
                self.receipt({'volume_value': 31}, '/broadcast/volume')
                command = self.controller.observation.snapshot()['command']
                self.assertEqual(command['operation'], 'volume_up')
                self.assertEqual(command['status'], 'failed')

    def test_missing_postwrite_feedback_stays_unconfirmed_without_replay(self):
        self.apply_write = True
        reads = [0]
        def publish(topic, payload):
            if topic == get_topic(TOPIC_GET_VOLUME, 'fixture'):
                reads[0] += 1
                if reads[0] > 1:
                    return SimpleNamespace(rc=0)
            return self.publish(topic, payload)
        self.broker.publish.side_effect = publish
        with patch.object(threading.Event, 'wait', autospec=True,
                          side_effect=lambda event, *_: event.is_set()):
            result = self.execute('volume_up')
        self.assertTrue(result['ok'])
        self.assertFalse(result['confirmed'])
        self.assertTrue(result['pending'])
        self.assertEqual(len(self.at_write), 1)
        self.assertEqual(result['tv']['observation']['fields']['volume']['value'], 30)

    def test_wrong_target_in_correct_direction_does_not_confirm(self):
        def publish(topic, payload):
            result = self.publish(topic, payload)
            if topic == get_topic(TOPIC_SET_VOLUME, 'fixture'):
                self.level = 32  # Direction alone is insufficient for numeric writes.
            return result
        self.broker.publish.side_effect = publish
        result = self.execute('volume_up')
        self.assertFalse(result['confirmed'])
        self.assertTrue(result['pending'])
        self.assertFalse(result['tv']['observation']['command']['matches'])

    def test_new_authentication_requirement_after_preread_prevents_write(self):
        getter = self.tv.get_volume
        def auth_after_read(*args, **kwargs):
            level = getter(*args, **kwargs)
            self.receipt({'statetype': 'authenticationcode'},
                         '/remoteapp/mobile/fixture/ui_service/data/authenticationcode')
            self.assertTrue(self.tv.needs_authentication())
            return level
        with patch.object(self.tv, 'get_volume', side_effect=auth_after_read):
            result = self.execute('volume_up')
        self.assertFalse(result['ok'])
        self.assertEqual(self.at_write, [])

    def test_new_external_audio_after_preread_revokes_numeric_write(self):
        getter = self.tv.get_volume
        def external_after_read(*args, **kwargs):
            level = getter(*args, **kwargs)
            self.receipt({'volume_type': 1, 'volume_value': 30},
                         '/remoteapp/mobile/broadcast/ui_service/volume')
            return level
        with patch.object(self.tv, 'get_volume', side_effect=external_after_read):
            result = self.execute('volume_up')
        self.assertFalse(result['ok'])
        self.assertEqual(self.at_write, [])
        self.assertEqual(result['tv']['observation']['audio'], 'external-unverified')

    def test_transport_change_after_fresh_preread_prevents_numeric_write(self):
        getter = self.tv.get_volume
        def disconnected_after_read(*args, **kwargs):
            level = getter(*args, **kwargs)
            self.tv._on_disconnect(self.broker, None, 1)
            self.tv._connected = True  # Same client, new transport epoch.
            return level
        with patch.object(self.tv, 'get_volume', side_effect=disconnected_after_read):
            result = self.execute('volume_up')
        self.assertFalse(result['ok'])
        self.assertEqual(self.at_write, [])

    def test_bounded_target_requires_matching_postwrite_feedback(self):
        self.apply_write = True
        for action, start, target in (
            ('volume_up', 30, 31), ('volume_down', 30, 29),
            ('volume_up', 99, 100), ('volume_up', 100, 100),
            ('volume_down', 1, 0), ('volume_down', 0, 0),
        ):
            with self.subTest(action=action, start=start):
                self.level = start
                result = self.execute(action)
                self.assertTrue(result['confirmed'])
                self.assertFalse(result['pending'])
                self.assertEqual(self.level, target)
                self.assertEqual(self.at_write[-1]['status'], 'pending')
                observed = result['tv']['observation']['command']
                self.assertEqual(observed['operation'], action)
                self.assertIsNone(observed['requested'])
                self.assertEqual(observed['target'], target)

    def test_changed_preread_cannot_confirm_ignored_absolute_setter(self):
        for action, old_level in (('volume_up', 20), ('volume_down', 40)):
            with self.subTest(action=action):
                self.controller.observation.volume({'volume_value': old_level})
                result = self.execute(action)
                self.assertTrue(result['ok'])
                self.assertFalse(result['confirmed'])
                self.assertTrue(result['pending'])
                self.assertEqual(self.at_write[-1]['status'], 'pending')
                observed = result['tv']['observation']['command']
                self.assertEqual(observed['operation'], action)
                self.assertEqual(observed['baseline'], 30)
                self.assertEqual(observed['status'], 'pending')
