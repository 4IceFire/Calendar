import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from vidaa_observed import ObservedVidaaTV
from pyvidaa import VidaaTV
from pyvidaa.topics import TOPIC_GET_STATE, TOPIC_STATE_RESPONSE, get_topic


class PowerSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.broker = Mock()
        self.broker.subscribe.return_value = (0, 1)
        self.broker.publish.return_value = SimpleNamespace(rc=0)
        with patch('pyvidaa.client.mqtt.Client', return_value=self.broker):
            self.tv = ObservedVidaaTV('fixture.invalid', client_id='fixture',
                                      use_ssl=False, enable_persistence=False,
                                      on_state_change=Mock())
        self.tv._connected = True

    def receipt(self, payload, *, retained=False, topic=TOPIC_STATE_RESPONSE, client=None):
        self.tv._on_message(self.broker if client is None else client, None,
                            SimpleNamespace(topic=topic, payload=json.dumps(payload).encode(), retain=retained))

    def test_guarded_wake_revalidates_newer_source_only_under_publication_lock(self):
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'fake_sleep_0'}, retained=True) or (0, 1))
        with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
            self.tv.get_power_state(timeout=.001)
            clock.return_value = 103
            getter = self.tv.get_power_state
            def changed_after_getter(*args, **kwargs):
                captured = getter(*args, **kwargs)
                self.receipt({'sourceid': 'HDMI1'}, retained=True)
                return captured
            with patch.object(self.tv, 'get_power_state', side_effect=changed_after_getter):
                self.assertEqual(self.tv.wake_from_standby(), 'unknown')
        self.assertNotIn('KEY_POWER', [c.args[1] for c in self.broker.publish.call_args_list])

    def test_guarded_wake_dwell_uses_receipt_time_not_parser_completion(self):
        with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
            parser = VidaaTV._on_message
            def slow_parse(*args):
                parser(self.tv, *args)
                clock.return_value = 103
            with patch.object(VidaaTV, '_on_message', side_effect=slow_parse):
                self.receipt({'statetype': 'fake_sleep_0'}, retained=True)
            self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'fake_sleep_0'}, retained=True) or (0, 1))
            with patch.object(self.tv._wake_changed, 'wait', return_value=False) as settle:
                self.assertEqual(self.tv.wake_from_standby(), 'sent')
            settle.assert_not_called()

    def test_guarded_wake_latest_receipt_revokes_standby_before_publish(self):
        for payload, topic in (
            ({'statetype': 'livetv'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 'unknown'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 'authentication'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 'livetv', 'error': 'denied'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 'livetv'}, '/broadcast/state'),
            ({'statetype': 'error'}, '/broadcast/state'),
            ({'sourceid': 'HDMI1'}, '/broadcast/state'),
        ):
            with self.subTest(payload=payload, topic=topic):
                self.tv._invalidate_transport()
                self.broker.publish.reset_mock()
                self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'fake_sleep_0'}, retained=True) or (0, 1))
                with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
                    self.tv.get_power_state(timeout=.001)
                    clock.return_value = 103
                    getter = self.tv.get_power_state
                    def changed_after_getter(*args, **kwargs):
                        captured = getter(*args, **kwargs)
                        self.receipt(payload, topic=topic)
                        return captured
                    with patch.object(self.tv, 'get_power_state', side_effect=changed_after_getter):
                        self.assertIn(self.tv.wake_from_standby(), ('unknown', 'already_on'))
                self.assertNotIn('KEY_POWER', [c.args[1] for c in self.broker.publish.call_args_list])

    def test_guarded_wake_does_not_publish_past_newer_inflight_power_parser(self):
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'fake_sleep_0'}, retained=True) or (0, 1))
        parsing, release = threading.Event(), threading.Event()
        parser = VidaaTV._on_message
        workers = []
        def paused_parse(client, userdata, msg):
            parser(self.tv, client, userdata, msg)
            if json.loads(msg.payload).get('statetype') == 'livetv':
                parsing.set()
                release.wait(1)
        with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
            self.tv.get_power_state(timeout=.001)
            clock.return_value = 103
            getter = self.tv.get_power_state
            def changed_after_getter(*args, **kwargs):
                captured = getter(*args, **kwargs)
                worker = threading.Thread(target=lambda: self.receipt({'statetype': 'livetv'}))
                workers.append(worker)
                worker.start()
                self.assertTrue(parsing.wait(.2))
                return captured
            try:
                with patch.object(VidaaTV, '_on_message', side_effect=paused_parse), \
                     patch.object(self.tv, 'get_power_state', side_effect=changed_after_getter):
                    self.assertEqual(self.tv.wake_from_standby(), 'unknown')
            finally:
                release.set()
                for worker in workers:
                    worker.join(.5)
        self.assertNotIn('KEY_POWER', [c.args[1] for c in self.broker.publish.call_args_list])

    def test_guarded_wake_settles_without_lock_then_requeries_before_one_key(self):
        def publish(_, payload):
            if payload == 'KEY_POWER':
                self.assertTrue(self.tv._transport_lock._is_owned())
            return SimpleNamespace(rc=0)
        self.broker.publish.side_effect = publish
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'fake_sleep_0'}, retained=True) or (0, 1))
        with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
            def settle(seconds):
                self.assertFalse(self.tv._transport_lock._is_owned())
                self.assertEqual(seconds, 2.0)
                self.assertNotIn('KEY_POWER', [c.args[1] for c in self.broker.publish.call_args_list])
                clock.return_value += seconds
                return False
            with patch.object(self.tv._wake_changed, 'wait', side_effect=settle) as wait:
                self.assertEqual(self.tv.wake_from_standby(), 'sent')
            wait.assert_called_once_with(2.0)
        self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list], ['', '', 'KEY_POWER'])
        self.assertEqual(self.broker.subscribe.call_count, 2)

    def test_guarded_wake_dwell_cancellation_invalidation_and_failed_requery_send_no_key(self):
        for change in ('cancel', 'on', 'error', 'standby_again', 'disconnect', 'requery_silent'):
            with self.subTest(change=change):
                self.tv._connected = True
                self.tv._invalidate_transport()
                self.broker.publish.reset_mock()
                self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'fake_sleep_0'}, retained=True) or (0, 1))
                cancelled = [False]
                with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
                    def settle(seconds):
                        self.assertFalse(self.tv._transport_lock._is_owned())
                        clock.return_value += seconds
                        if change == 'cancel':
                            cancelled[0] = True
                        elif change == 'disconnect':
                            self.tv._on_disconnect(self.broker, None, 1)
                        elif change == 'requery_silent':
                            self.broker.subscribe.side_effect = None
                        else:
                            self.receipt({'statetype': 'livetv' if change == 'on' else 'error'})
                            if change == 'standby_again':
                                self.receipt({'statetype': 'fake_sleep_0'})
                        return self.tv._wake_changed.is_set()
                    with patch.object(self.tv._wake_changed, 'wait', side_effect=settle), \
                         patch.object(threading.Event, 'wait', autospec=True, side_effect=lambda event, *_: event.is_set()):
                        self.assertEqual(self.tv.wake_from_standby(cancelled=lambda: cancelled[0]), 'unknown')
                self.assertNotIn('KEY_POWER', [c.args[1] for c in self.broker.publish.call_args_list])

    def test_guarded_wake_disconnect_or_same_client_epoch_change_after_getter_aborts(self):
        for reconnect in (False, True):
            with self.subTest(reconnect=reconnect):
                self.tv._connected = True
                self.tv._invalidate_transport()
                self.broker.publish.reset_mock()
                self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'fake_sleep_0'}, retained=True) or (0, 1))
                with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
                    self.tv.get_power_state(timeout=.001)
                    clock.return_value = 103
                    getter = self.tv.get_power_state
                    def changed_after_getter(*args, **kwargs):
                        captured = getter(*args, **kwargs)
                        self.tv._on_disconnect(self.broker, None, 1)
                        if reconnect:
                            self.tv._on_connect(self.broker, None, {}, 0)
                        return captured
                    with patch.object(self.tv, 'get_power_state', side_effect=changed_after_getter):
                        self.assertEqual(self.tv.wake_from_standby(), 'unknown')
                self.assertNotIn('KEY_POWER', [c.args[1] for c in self.broker.publish.call_args_list])

    def test_guarded_wake_explicit_outcomes_for_on_disconnected_and_failed_publish(self):
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'livetv'}, retained=True) or (0, 1))
        self.assertEqual(self.tv.wake_from_standby(), 'already_on')
        self.tv._connected = False
        self.assertEqual(self.tv.wake_from_standby(), 'unknown')
        self.tv._connected = True
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'fake_sleep_0'}, retained=True) or (0, 1))
        with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
            self.tv.get_power_state(timeout=.001)
            clock.return_value = 103
            with patch.object(self.tv, 'send_key', return_value=False):
                self.assertEqual(self.tv.wake_from_standby(), 'attempted_failed')
            with patch.object(self.tv, 'send_key', side_effect=RuntimeError('uncertain')):
                self.assertEqual(self.tv.wake_from_standby(), 'attempted_failed')

    def test_power_snapshot_requests_broker_replay_not_process_cache(self):
        self.tv._state = {'statetype': 'livetv'}
        state = {'statetype': 'fake_sleep_0'}
        self.broker.subscribe.side_effect = lambda topic: (self.receipt(state, retained=True) or (0, 1))
        self.assertEqual(self.tv.get_power_state(timeout=.01), dict(state, _tdeck_retained=True))
        self.broker.publish.assert_called_once_with(get_topic(TOPIC_GET_STATE, 'fixture'), '')
        self.broker.subscribe.assert_called_once_with(TOPIC_STATE_RESPONSE)
        self.broker.subscribe.side_effect = None
        self.assertIsNone(self.tv.get_power_state(timeout=.001))

    def test_retained_replays_seed_observer_but_never_finish_strict_reads(self):
        for getter, topic, payload in (
            ('get_state', TOPIC_STATE_RESPONSE, {'statetype': 'livetv'}),
            ('get_volume', '/remoteapp/mobile/broadcast/ui_service/volume', {'volume_type': 0, 'volume_value': 35}),
        ):
            with self.subTest(getter=getter):
                self.broker.publish.side_effect = lambda *_: (self.receipt(payload, retained=True, topic=topic) or SimpleNamespace(rc=0))
                self.assertIsNone(getattr(self.tv, getter)(timeout=.001))
                self.assertTrue(self.tv._receipt.call_args.args[0]['_tdeck_retained'])
        self.broker.publish.side_effect = None
        self.assertIsNone(self.tv.get_state(timeout=.001))
        self.assertIsNone(self.tv.get_volume(timeout=.001))

    def test_power_off_standby_snapshot_sends_no_key(self):
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'fake_sleep_0'}, retained=True) or (0, 1))
        self.assertTrue(self.tv.power_off())
        self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list], [''])

    def test_power_off_revalidates_live_standby_after_snapshot_getter_returns(self):
        from tv_observation import Observation
        observation = Observation()
        self.tv._receipt = lambda payload: observation.report(
            'power', 'off' if payload['statetype'] == 'fake_sleep_0' else 'on',
            retained=payload['_tdeck_retained'])
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'livetv'}, retained=True) or (0, 1))
        getter = self.tv.get_power_state
        def standby_after_getter(*args, **kwargs):
            captured = getter(*args, **kwargs)
            self.receipt({'statetype': 'fake_sleep_0'})
            self.assertEqual(self.tv._snapshot['statetype'], 'fake_sleep_0')
            self.assertEqual(observation.snapshot()['fields']['power']['value'], 'off')
            return captured
        with patch.object(self.tv, 'get_power_state', side_effect=standby_after_getter):
            self.assertTrue(self.tv.power_off())
        self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list], [''])

    def test_power_off_new_invalid_power_receipt_revokes_captured_on(self):
        getter = self.tv.get_power_state
        for payload in ({'statetype': 'unknown'}, {'statetype': 'authentication'},
                        {'statetype': 'error'}, {'statetype': 'livetv', 'error': 'denied'}):
            with self.subTest(payload=payload):
                self.broker.publish.reset_mock()
                self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'livetv'}, retained=True) or (0, 1))
                def invalid_after_getter(*args, **kwargs):
                    captured = getter(*args, **kwargs)
                    self.receipt(payload)
                    return captured
                with patch.object(self.tv, 'get_power_state', side_effect=invalid_after_getter), \
                     patch.object(self.tv._standby_event, 'wait', return_value=False):
                    self.assertFalse(self.tv.power_off())
                self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list], [''])

    def test_power_off_rejects_newer_inflight_receipt_before_parser_completion(self):
        for payload in ({'statetype': 'fake_sleep_0'}, {'statetype': 'authentication'},
                        {'statetype': 'error'}):
            with self.subTest(payload=payload):
                self.broker.publish.reset_mock()
                self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'livetv'}, retained=True) or (0, 1))
                parsing, release = threading.Event(), threading.Event()
                parser = VidaaTV._on_message
                workers = []
                def paused_parse(client, userdata, msg):
                    parser(self.tv, client, userdata, msg)
                    if json.loads(msg.payload) == payload:
                        parsing.set()
                        if not release.wait(2):
                            raise AssertionError('parser release timed out')
                getter = self.tv.get_power_state
                def inflight_after_getter(*args, **kwargs):
                    captured = getter(*args, **kwargs)
                    worker = threading.Thread(target=lambda: self.receipt(payload))
                    workers.append(worker)
                    worker.start()
                    self.assertTrue(parsing.wait(1))
                    return captured
                try:
                    with patch.object(VidaaTV, '_on_message', side_effect=paused_parse), \
                         patch.object(self.tv, 'get_power_state', side_effect=inflight_after_getter), \
                         patch.object(self.tv._standby_event, 'wait', return_value=False):
                        self.assertFalse(self.tv.power_off())
                finally:
                    release.set()
                    for worker in workers:
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                self.assertNotIn('KEY_POWER', [c.args[1] for c in self.broker.publish.call_args_list])

    def test_power_off_older_parser_cannot_replace_newer_standby_snapshot(self):
        getter = self.tv.get_power_state
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'livetv'}, retained=True) or (0, 1))
        parser = VidaaTV._on_message
        def newer_receipt_during_parse(client, userdata, msg):
            parser(self.tv, client, userdata, msg)
            if json.loads(msg.payload)['statetype'] == 'livetv':
                self.receipt({'statetype': 'fake_sleep_0'})
        def interleave_after_getter(*args, **kwargs):
            captured = getter(*args, **kwargs)
            with patch.object(VidaaTV, '_on_message', side_effect=newer_receipt_during_parse):
                self.receipt({'statetype': 'livetv'}, retained=True)
            return captured
        with patch.object(self.tv, 'get_power_state', side_effect=interleave_after_getter), \
             patch.object(self.tv._standby_event, 'wait', return_value=False):
            self.assertTrue(self.tv.power_off())
        self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list], [''])

    def test_power_off_on_snapshot_sends_one_key_and_waits_for_physical_standby(self):
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'livetv'}, retained=True) or (0, 1))
        confirmed = threading.Event()
        def publish(topic, payload):
            if payload == 'KEY_POWER':
                self.receipt({'statetype': 'fake_sleep_0'}, retained=True)
                self.assertFalse(self.tv._standby_event.is_set())
                self.receipt({'statetype': 'fake_sleep_0'})
                confirmed.set()
            return SimpleNamespace(rc=0)
        self.broker.publish.side_effect = publish
        self.assertTrue(self.tv.power_off())
        self.assertTrue(confirmed.is_set())
        self.assertTrue(self.tv._standby_event.is_set())
        self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list], ['', 'KEY_POWER'])
        self.assertFalse(self.tv._receipt.call_args.args[0]['_tdeck_retained'])

    def test_power_off_waits_past_retained_standby_until_physical_receipt(self):
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'livetv'}, retained=True) or (0, 1))
        key_sent = threading.Event()
        finished = threading.Event()
        result = []
        def publish(_, payload):
            if payload == 'KEY_POWER':
                key_sent.set()
            return SimpleNamespace(rc=0)
        self.broker.publish.side_effect = publish
        def turn_off():
            result.append(self.tv.power_off())
            finished.set()
        worker = threading.Thread(target=turn_off)
        worker.start()
        try:
            self.assertTrue(key_sent.wait(.2))
            self.receipt({'statetype': 'fake_sleep_0'}, retained=True)
            self.assertFalse(finished.wait(.02))
            self.receipt({'statetype': 'fake_sleep_0'})
            self.assertTrue(finished.wait(.2))
            self.assertEqual(result, [True])
            self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list], ['', 'KEY_POWER'])
        finally:
            worker.join(2)

    def test_power_off_missing_confirmation_is_bounded_accepted_not_retried(self):
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'livetv'}, retained=True) or (0, 1))
        with patch.object(self.tv._standby_event, 'wait', return_value=False) as wait:
            self.assertTrue(self.tv.power_off())
        wait.assert_called_once()
        self.assertLessEqual(wait.call_args.args[0], 1.5)
        self.assertEqual([c.args[1] for c in self.broker.publish.call_args_list], ['', 'KEY_POWER'])

    def test_power_off_unknown_disconnected_or_rejected_never_toggles(self):
        self.tv._connected = False
        self.assertFalse(self.tv.power_off())
        self.broker.publish.assert_not_called()
        self.tv._connected = True
        for state in (None, {}, {'statetype': 'authentication'}, {'statetype': 'error'}, {'statetype': 'unknown'}):
            with self.subTest(state=state), patch.object(self.tv, 'get_power_state', return_value=state):
                self.assertFalse(self.tv.power_off())
        self.broker.publish.assert_not_called()

    def test_disconnect_and_same_object_reconnect_abort_all_outstanding_reads(self):
        for getter in ('get_state', 'get_volume', 'get_sources', 'get_device_info', 'get_tv_info', 'get_power_state'):
            with self.subTest(getter=getter):
                self.tv._on_connect(self.broker, None, {}, 0)
                started = threading.Event()
                self.broker.publish.side_effect = lambda *_: (started.set() or SimpleNamespace(rc=0))
                result = []
                worker = threading.Thread(target=lambda: result.append(getattr(self.tv, getter)(timeout=1)))
                worker.start()
                self.assertTrue(started.wait(.2))
                self.tv._on_disconnect(self.broker, None, 1)
                self.tv._on_connect(self.broker, None, {}, 0)
                worker.join(.2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result, [None])
                self.assertTrue(all(value is None for value in self.tv._receipts.values()))
                self.assertIsNone(self.tv._snapshot)

    def test_prior_epoch_inflight_parser_and_old_client_cannot_seed_receipts(self):
        original = VidaaTV._on_message
        def reconnect_during_parse(*args):
            original(self.tv, *args)
            self.tv._on_disconnect(self.broker, None, 1)
            self.tv._on_connect(self.broker, None, {}, 0)
        with patch.object(VidaaTV, '_on_message', side_effect=reconnect_during_parse):
            self.receipt({'statetype': 'livetv'})
        self.assertIsNone(self.tv._snapshot)
        self.tv._receipt.assert_not_called()
        self.receipt({'statetype': 'livetv'}, client=Mock())
        self.tv._receipt.assert_not_called()
        self.tv._on_disconnect(self.broker, None, 1)
        self.receipt({'statetype': 'livetv'})
        self.tv._receipt.assert_not_called()

    def test_pre_request_inflight_receipt_cannot_complete_new_read(self):
        parsing = threading.Event()
        release = threading.Event()
        requested = threading.Event()
        original = VidaaTV._on_message
        def paused_parser(*args):
            original(self.tv, *args)
            parsing.set()
            release.wait(.5)
        with patch.object(VidaaTV, '_on_message', side_effect=paused_parser):
            delivery = threading.Thread(target=lambda: self.receipt({'statetype': 'livetv'}))
            delivery.start()
            self.assertTrue(parsing.wait(.2))
            result = []
            self.broker.publish.side_effect = lambda *_: (requested.set() or SimpleNamespace(rc=0))
            reader = threading.Thread(target=lambda: result.append(self.tv.get_state(timeout=.03)))
            reader.start()
            self.assertTrue(requested.wait(.2))
            release.set()
            delivery.join(.2)
            reader.join(.2)
        self.assertEqual(result, [None])

    def test_snapshot_rejects_invalid_error_auth_or_unrelated_metadata(self):
        for payload, topic in (
            ({'statetype': 'authentication'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 'authenticationcode'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 'error'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 'unknown'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 'livetv', 'error': 'denied'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 1}, TOPIC_STATE_RESPONSE),
            ({'sourceid': 'HDMI1'}, TOPIC_STATE_RESPONSE),
            ({'statetype': 'livetv'}, '/remoteapp/mobile/other/ui_service/data/state'),
            ({'statetype': 'livetv'}, '/remoteapp/mobile/broadcast/platform_service/data/gettvinfo'),
            ([], TOPIC_STATE_RESPONSE),
        ):
            with self.subTest(payload=payload, topic=topic):
                self.broker.subscribe.side_effect = lambda _: (self.receipt(payload, topic=topic, retained=True) or (0, 1))
                self.assertIsNone(self.tv.get_power_state(timeout=.001))

    def test_snapshot_transport_errors_and_key_rejection_are_safe(self):
        for operation in ('publish', 'subscribe'):
            with self.subTest(operation=operation):
                target = getattr(self.broker, operation)
                target.side_effect = OSError('mock transport error')
                try:
                    self.assertIsNone(self.tv.get_power_state(timeout=.001))
                finally:
                    target.side_effect = None
        self.broker.subscribe.return_value = (4, None)
        self.broker.publish.side_effect = lambda *_: (self.receipt({'statetype': 'livetv'}) or SimpleNamespace(rc=0))
        self.assertIsNone(self.tv.get_power_state(timeout=.001))
        self.broker.publish.side_effect = None
        self.broker.subscribe.return_value = (0, 1)
        self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': 'livetv'}, retained=True) or (0, 1))
        self.broker.publish.side_effect = lambda _, payload: SimpleNamespace(rc=4 if payload == 'KEY_POWER' else 0)
        self.assertFalse(self.tv.power_off())
        self.broker.publish.side_effect = lambda _, payload: (_ for _ in ()).throw(OSError('key rejected')) if payload == 'KEY_POWER' else SimpleNamespace(rc=0)
        self.assertFalse(self.tv.power_off())

    def test_real_auth_required_topic_revokes_on_and_off_authorization(self):
        auth_topic = '/remoteapp/mobile/fixture/ui_service/data/authenticationcode'
        getter = self.tv.get_power_state
        for operation, state_type, rejected in (
                ('wake_from_standby', 'fake_sleep_0', 'unknown'),
                ('power_off', 'livetv', False)):
            with self.subTest(operation=operation):
                self.receipt({'result': 1}, topic=auth_topic)
                self.tv._invalidate_transport()
                self.broker.publish.reset_mock()
                self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': state_type}, retained=True) or (0, 1))
                with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
                    getter(timeout=.001)
                    clock.return_value = 103
                    def auth_after_getter(*args, **kwargs):
                        captured = getter(*args, **kwargs)
                        self.tv._receipt.reset_mock()
                        self.receipt({'statetype': 'authenticationcode'}, topic=auth_topic)
                        self.assertTrue(self.tv.needs_authentication())
                        self.tv._receipt.assert_not_called()
                        return captured
                    with patch.object(self.tv, 'get_power_state', side_effect=auth_after_getter), \
                         patch.object(self.tv._standby_event, 'wait', return_value=False):
                        self.assertEqual(getattr(self.tv, operation)(), rejected)
                    # A later power snapshot alone cannot clear authoritative auth.
                    clock.return_value = 106
                    with patch.object(self.tv._wake_changed, 'wait', return_value=False), \
                         patch.object(self.tv._standby_event, 'wait', return_value=False):
                        self.assertEqual(getattr(self.tv, operation)(), rejected)
                self.assertNotIn('KEY_POWER', [c.args[1] for c in self.broker.publish.call_args_list])

    def test_inflight_real_auth_parser_blocks_newer_power_snapshot_authorization(self):
        auth_topic = '/remoteapp/mobile/fixture/ui_service/data/authenticationcode'
        getter = self.tv.get_power_state
        parser = VidaaTV._on_message
        for operation, state_type, rejected in (
                ('wake_from_standby', 'fake_sleep_0', 'unknown'),
                ('power_off', 'livetv', False)):
            with self.subTest(operation=operation):
                self.receipt({'result': 1}, topic=auth_topic)
                self.tv._invalidate_transport()
                self.broker.publish.reset_mock()
                self.broker.subscribe.side_effect = lambda _: (self.receipt({'statetype': state_type}, retained=True) or (0, 1))
                parsing, release = threading.Event(), threading.Event()
                workers = []
                def paused_parse(client, userdata, msg):
                    if msg.topic == auth_topic:
                        parsing.set()
                        if not release.wait(2):
                            raise AssertionError('auth parser release timed out')
                    parser(self.tv, client, userdata, msg)
                with patch('vidaa_observed.time.monotonic', return_value=100) as clock:
                    def auth_during_getter(*args, **kwargs):
                        getter(*args, **kwargs)
                        worker = threading.Thread(target=lambda: self.receipt(
                            {'statetype': 'authenticationcode'}, topic=auth_topic))
                        workers.append(worker)
                        worker.start()
                        self.assertTrue(parsing.wait(1))
                        self.assertFalse(self.tv.needs_authentication())
                        # New state parsing must not mask an unfinished auth parser.
                        getter(timeout=.001)
                        clock.return_value += 3
                        return getter(timeout=.001)
                    try:
                        with patch.object(VidaaTV, '_on_message', side_effect=paused_parse), \
                             patch.object(self.tv, 'get_power_state', side_effect=auth_during_getter), \
                             patch.object(self.tv._standby_event, 'wait', return_value=False):
                            self.assertEqual(getattr(self.tv, operation)(), rejected)
                    finally:
                        release.set()
                        for worker in workers:
                            worker.join(1)
                            self.assertFalse(worker.is_alive())
                self.assertTrue(self.tv.needs_authentication())
                self.assertNotIn('KEY_POWER', [c.args[1] for c in self.broker.publish.call_args_list])

    def test_authentication_and_token_refresh_still_use_inherited_parser(self):
        self.receipt({'statetype': 'authentication'}, topic=TOPIC_STATE_RESPONSE)
        self.assertTrue(self.tv.needs_authentication())
        self.tv._receipt.assert_not_called()
        self.receipt({'result': 1}, topic='/remoteapp/mobile/fixture/ui_service/data/authenticationcode')
        self.assertTrue(self.tv.is_authenticated())
        self.assertFalse(self.tv.needs_authentication())
        self.tv._refresh_token = 'mock-refresh'
        self.tv._pending_token_refresh = True
        self.broker.publish.side_effect = lambda *_: (self.receipt(
            {'accesstoken': 'mock-access', 'refreshtoken': 'mock-next'},
            topic='/remoteapp/mobile/fixture/platform_service/data/tokenissuance') or SimpleNamespace(rc=0))
        self.assertTrue(self.tv.refresh_token(timeout=.001))
        self.assertEqual(self.tv._password, 'mock-access')
        self.assertFalse(self.tv._pending_token_refresh)
        self.tv._receipt.assert_not_called()

    def test_power_snapshot_accepts_exact_direct_reply_with_metadata(self):
        self.broker.publish.side_effect = lambda *_: (self.receipt(
            {'statetype': 'livetv'}, topic='/remoteapp/mobile/fixture/ui_service/data/state') or SimpleNamespace(rc=0))
        self.assertEqual(self.tv.get_power_state(timeout=.01), {'statetype': 'livetv', '_tdeck_retained': False})


class StrictReceiptTests(unittest.TestCase):
    def setUp(self):
        with patch('pyvidaa.client.mqtt.Client'):
            self.client = ObservedVidaaTV('fixture.invalid', client_id='fixture',
                                          use_ssl=False, enable_persistence=False)
        self.client._client = None
        self.client._connected = True
        self.client._state = {'statetype': 'livetv'}
        self.client._cached_volume = 90
        self.client._receipt = Mock()
        self.client._receipts = {key: None for key in ('state', 'volume', 'sources', 'device_info', 'tv_info')}
        self.client._receipt_events = {key: threading.Event() for key in self.client._receipts}
        self.client._publish = Mock(return_value=True)

    def receipt(self, topic, payload):
        with patch.object(VidaaTV, '_on_message'):
            self.client._on_message(None, None, SimpleNamespace(topic=topic, payload=json.dumps(payload).encode()))

    def test_connection_subscribes_to_client_specific_state_response(self):
        self.client._client = Mock()
        with patch.object(VidaaTV, '_on_connect'):
            self.client._on_connect(self.client._client, None, {}, 0)
        self.client._client.subscribe.assert_called_once_with('/remoteapp/mobile/fixture/ui_service/data/state')

    def test_fresh_client_specific_state_reply_completes_read(self):
        state = {'statetype': 'sourceswitch', 'sourceid': 'HDMI1'}
        self.client._publish.side_effect = lambda *_: (self.receipt('/remoteapp/mobile/fixture/ui_service/data/state', state) or True)
        self.assertEqual(self.client.get_state(timeout=.001), state)

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
