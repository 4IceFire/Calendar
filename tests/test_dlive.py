"""dLive acceptance tests; literal wire fixtures from A&H V2.0 pp1-2."""
import socket
import threading
import time
import unittest

HEADER = bytes.fromhex('f0 00 00 1a 50 10 01 00')


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for dLive observation')


class Desk:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.messages = []
        self.connections = []
        self.level = 107
        self.accept_writes = True
        self.respond = True
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections.append(conn)
            conn.settimeout(.1)
            pending = b''
            try:
                while not self.stop.is_set():
                    try:
                        data = conn.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    pending += data
                    while pending and pending[0] in (0xb5, 0x95):
                        size = 9 if pending[0] == 0xb5 else 6
                        if len(pending) < size:
                            break
                        self.messages.append(pending[:size])
                        pending = pending[size:]
                    while b'\xf7' in pending:
                        msg, pending = pending.split(b'\xf7', 1)
                        msg += b'\xf7'
                        self.messages.append(msg)
                        body = msg[len(HEADER):-1]
                        if not self.respond:
                            continue
                        if body[:4] == bytes.fromhex('03 05 0f 0d'):
                            # base channel 4, input 1 -> stereo Aux 2 on N+2.
                            reply = HEADER + bytes([body[0], 13, body[4], body[5], body[6], self.level, 247])
                            conn.sendall(reply[:4])
                            conn.sendall(reply[4:])
                        elif body[:2] == bytes.fromhex('03 0d'):
                            if self.accept_writes:
                                self.level = body[-1]
                        elif len(body) == 3 and body[1] == 1:
                            conn.sendall(HEADER + bytes([body[0], 2, body[2]]) + b'Vocals\xf7')
                        elif len(body) == 5 and body[:4] == bytes.fromhex('05 05 0b 17'):
                            conn.sendall(bytes([0xb5, 99, body[4], 98, 23, 6, 107]))
                        elif len(body) == 4 and body[:3] == bytes.fromhex('05 05 09'):
                            conn.sendall(bytes([0x95, body[3], 63, body[3], 0]))
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.stop.set()
        for conn in self.connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.listener.close()
        self.thread.join(2)


def config(port):
    return dict(personal_mix_provider='dlive', dlive_enabled=True, dlive_ip='127.0.0.1',
                dlive_port=port, dlive_midi_channel=4, dlive_retry_interval=.1,
                dlive_request_interval=.01, dlive_stale_after=2,
                dlive_auxes=[dict(channel=2002, bus=2, stereo=True, label='IEM', enabled=True)],
                dlive_channels=[dict(channel=1, label='Vocal', group='Vocals', enabled=True)])


class DLiveCodecTests(unittest.TestCase):
    def test_reference_curve_preserves_every_observed_midi_step(self):
        from dlive import encode_level, decode_level
        for value in range(128):
            self.assertEqual(encode_level(decode_level(value)), value)
        for value in (True, False, float('nan'), float('inf'), -151, 11):
            with self.assertRaises(ValueError):
                encode_level(value)

    def test_mono_stereo_boundary_notes_and_base_channels(self):
        import dlive
        class Recorder:
            def __init__(self):
                self.messages = []
            def sendall(self, data):
                self.messages.append(data)
        for base, aux, stereo, bus, db, wire in [
            (1, 1062, False, 62, 10, '00 0d 7f 02 3d 7f f7'),
            (12, 2031, True, 31, -150, '0b 0d 7f 0d 5e 00 f7')]:
            cfg = dict(config(1), dlive_enabled=False, dlive_midi_channel=base,
                       dlive_auxes=[dict(channel=aux, bus=bus, stereo=stereo)],
                       dlive_channels=[dict(channel=128)])
            mixer = dlive.DLiveMixerClient(cfg)
            recorder = Recorder()
            mixer.sock = recorder
            mixer.set_level(aux, 128, db)
            self.assertEqual(recorder.messages, [HEADER + bytes.fromhex(wire)])


class DLiveProviderTests(unittest.TestCase):
    def tearDown(self):
        import personal_mixes
        personal_mixes.close_client()

    def test_factory_endpoint_replacement_never_retargets_or_replays_old_client(self):
        import personal_mixes
        first_desk, second_desk = Desk(), Desk()
        try:
            first = personal_mixes.get_client(config(first_desk.port))
            wait_for(lambda: first.status()['connected'])
            second = personal_mixes.get_client(config(second_desk.port))
            wait_for(lambda: second.status()['connected'])
            with self.assertRaises(ConnectionError):
                first.set_level(2002, 1, -20)
            third = personal_mixes.get_client(config(first_desk.port))
            wait_for(lambda: third.status()['connected'])
            self.assertIsNot(first, third)
            third.set_level(2002, 1, -12)
            wait_for(lambda: first_desk.level == 83)
            self.assertEqual(second_desk.level, 107)
            self.assertFalse(any(m.startswith(HEADER + bytes.fromhex('03 0d')) for m in second_desk.messages))
        finally:
            personal_mixes.close_client()
            first_desk.close()
            second_desk.close()

    def test_factory_selects_dlive_and_closes_old_transport(self):
        import personal_mixes
        import dlive
        cfg = config(1)
        cfg['dlive_enabled'] = False
        first = personal_mixes.get_client(cfg)
        self.assertIsInstance(first, dlive.DLiveMixerClient)
        self.assertIs(first, personal_mixes.get_client(cfg))
        second = personal_mixes.get_client(dict(cfg, dlive_ip='127.0.0.2'))
        self.assertIsNot(first, second)
        self.assertTrue(first.stop.is_set())
        with self.assertRaises(ConnectionError):
            first.set_level(2002, 1, -20)
        with self.assertRaises(ValueError):
            personal_mixes.get_client(dict(cfg, personal_mix_provider='unknown'))

    def test_validation_preserves_explicit_bus_identity_and_rejects_unsafe_inputs(self):
        import personal_mixes
        cfg = personal_mixes.validate_config(config(51325))
        self.assertEqual(cfg['dlive_auxes'][0]['channel'], 2002)
        for change in [dict(dlive_midi_channel=13), dict(dlive_port=51327), dict(dlive_tls=True),
                       dict(dlive_auxes=[dict(channel=2, bus=2, stereo=True)]),
                       dict(dlive_auxes=[dict(channel=2002, bus=32, stereo=True)]),
                       dict(dlive_request_interval=float('nan')),
                       dict(dlive_channels=[dict(channel=129)]),
                       dict(dlive_channels=[dict(channel=1), dict(channel=1)])]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                personal_mixes.validate_config(dict(config(51325), **change))


class DLiveWireTests(unittest.TestCase):
    def test_accepted_but_ignored_send_never_becomes_observed_success(self):
        import dlive
        desk = Desk()
        mixer = dlive.DLiveMixerClient(config(desk.port))
        try:
            mixer.start()
            wait_for(lambda: mixer.aux_state(2002)['channels'][0]['level'] is not None)
            before = mixer.aux_state(2002)['channels'][0]
            desk.accept_writes = False
            submitted = mixer.set_level(2002, 1, -12)
            wait_for(lambda: mixer.aux_state(2002)['channels'][0]['observedAt'] > before['observedAt'])
            actual = mixer.aux_state(2002)['channels'][0]['level']
            self.assertEqual(actual, before['level'])
            self.assertNotEqual(actual, submitted)
        finally:
            mixer.close()
            desk.close()

    def test_idle_connection_uses_documented_read_only_heartbeat_without_reconnecting(self):
        import dlive
        desk = Desk()
        mixer = dlive.DLiveMixerClient(dict(config(desk.port), dlive_stale_after=1))
        query = HEADER + bytes.fromhex('03 01 00 f7')
        try:
            mixer.start()
            wait_for(lambda: desk.messages.count(query) >= 2, timeout=3)
            self.assertEqual(len(desk.connections), 1, 'An idle healthy desk must not reconnect just because no fader changed')
            self.assertTrue(mixer.status()['ready'])
            self.assertFalse(any(m.startswith(HEADER + bytes.fromhex('03 0d')) for m in desk.messages))
        finally:
            mixer.close()
            desk.close()

    def test_writable_but_silent_peer_loses_readiness_and_recovers(self):
        import dlive
        desk = Desk()
        mixer = dlive.DLiveMixerClient(dict(config(desk.port), dlive_stale_after=1))
        try:
            mixer.start()
            wait_for(lambda: mixer.status()['connected'])
            desk.respond = False
            wait_for(lambda: not mixer.status()['connected'])
            wait_for(lambda: len(desk.connections) >= 2)
            self.assertFalse(mixer.status()['ready'])
            desk.respond = True
            wait_for(lambda: mixer.aux_state(2002)['channels'][0]['level'] is not None, timeout=4)
            self.assertFalse(any(m.startswith(HEADER + bytes.fromhex('03 0d')) for m in desk.messages))
        finally:
            mixer.close()
            desk.close()

    def test_aux_master_nrpn_mute_running_status_is_not_input_mute(self):
        import dlive
        desk = Desk()
        mixer = dlive.DLiveMixerClient(config(desk.port))
        try:
            mixer.start()
            wait_for(lambda: mixer.status()['connected'])
            mixer.request_aux_state(2002)
            wait_for(lambda: mixer.aux_state(2002).get('master', {}).get('mute') is False)
            self.assertAlmostEqual(mixer.aux_state(2002)['master']['level'], -.078740, places=4)
            mixer.set_master_level(2002, -12)
            mixer.set_master_mute(2002, True)
            wait_for(lambda: bytes.fromhex('95 41 7f 95 41 00') in desk.messages)
            self.assertIn(bytes.fromhex('b5 63 41 b5 62 17 b5 06 53'), desk.messages)
        finally:
            mixer.close()
            desk.close()

    def test_disconnect_recovery_rejects_without_replay_and_observations_expire(self):
        import dlive
        desk = Desk()
        mixer = dlive.DLiveMixerClient(config(desk.port))
        try:
            mixer.start()
            mixer.request_aux_state(2002)
            wait_for(lambda: mixer.aux_state(2002)['channels'][0]['level'] is not None)
            desk.connections[-1].shutdown(socket.SHUT_RDWR)
            wait_for(lambda: not mixer.status()['connected'])
            self.assertIsNone(mixer.aux_state(2002)['channels'][0]['level'])
            with self.assertRaises(ConnectionError):
                mixer.set_level(2002, 1, -30)
            wait_for(lambda: len(desk.connections) >= 2)
            wait_for(lambda: mixer.aux_state(2002)['channels'][0]['level'] is not None)
            self.assertFalse(any(m[len(HEADER)+1] == 13 for m in desk.messages))
            # A socket being open must not keep an old field fresh.
            with mixer.lock:
                mixer.observed[(2002, 1)] = (0, time.time() - 10)
            before = mixer.revision
            self.assertTrue(mixer.aux_state(2002)['channels'][0]['stale'])
            self.assertGreater(mixer.status()['revision'], before)
        finally:
            mixer.close()
            desk.close()

    def test_stream_handles_running_status_realtime_coalescing_and_bounded_garbage(self):
        import dlive
        stream = dlive.MidiStream()
        self.assertEqual(stream.feed(bytes.fromhex('b5 63')), [])
        self.assertEqual(stream.feed(bytes.fromhex('41 f8 62 17 06 6b')), [
            bytes.fromhex('b5 63 41'), bytes.fromhex('b5 62 17'), bytes.fromhex('b5 06 6b')])
        self.assertEqual(stream.feed(HEADER[:3]), [])
        self.assertEqual(stream.feed(HEADER[3:] + bytes.fromhex('03 0d 00 05 41 6b f7') * 1),
                         [HEADER + bytes.fromhex('03 0d 00 05 41 6b f7')])
        stream.feed(b'\xf0' + b'\x01' * 10000)
        self.assertIsNone(stream.sysex)
        self.assertEqual(stream.feed(bytes.fromhex('95 41 7f 41 00')), [bytes.fromhex('95 41 7f'), bytes.fromhex('95 41 00')])

    def test_personal_send_level_real_socket_readback_not_optimistic(self):
        import dlive
        desk = Desk()
        mixer = dlive.DLiveMixerClient(config(desk.port))
        try:
            mixer.start()
            mixer.request_aux_state(2002)
            wait_for(lambda: mixer.aux_state(2002)['channels'][0]['level'] is not None)
            self.assertIn(HEADER + bytes.fromhex('03 05 0f 0d 00 05 41 f7'), desk.messages)
            self.assertFalse(any(m[len(HEADER)+1] == 0x0d for m in desk.messages))
            value = mixer.set_level(2002, 1, -12)
            wait_for(lambda: abs(mixer.aux_state(2002)['channels'][0]['level'] - value) < .01)
            self.assertIn(HEADER + bytes.fromhex('03 0d 00 05 41 53 f7'), desk.messages)
            self.assertAlmostEqual(value, -12.173228, places=4)
            self.assertIsNone(mixer.aux_state(2002)['channels'][0]['sendOn'])
        finally:
            mixer.close()
            desk.close()
