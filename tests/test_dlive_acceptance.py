"""Desired invariants retained from parent-independent-invariants.py (R1/R2).

The config barrier releases the first request from a third thread: serialization
must be allowed to wait, not be forced to deadlock by the reproduction harness.
"""
import copy
import threading
import time
import unittest
from unittest.mock import patch

from test_dlive import Desk, config
import test_dlive_webui as _api_fixture


class ConfigConcurrencyInvariant(_api_fixture.DLiveApiTests):
    def test_generic_config_save_must_not_restore_pre_setup_endpoint(self):
        for first_path in ('/api/config', '/api/digico/setup'):
            with self.subTest(first_path=first_path):
                from test_dlive import wait_for
                self.cfg.update(dlive_enabled=True, dlive_ip='127.0.0.1', debug=True)
                old_client = self.webui._get_digico_client_from_config()
                wait_for(lambda: old_client.status()['ready'])
                at_save = threading.Event()
                release = threading.Event()
                second_started = threading.Event()
                errors, responses = [], {}

                def save(cfg):
                    if threading.current_thread().name == 'first-save':
                        at_save.set()
                        if not release.wait(5):
                            raise RuntimeError('Config barrier timed out')
                    self.cfg.update(copy.deepcopy(cfg))

                def post(path, name):
                    try:
                        client = self.webui.app.test_client()
                        with client.session_transaction() as session:
                            session.update(_user_id='1', _fresh=True, _auth_session_id='fixture-1',
                                           _last_activity=int(time.time()), _csrf='ignore-csrf')
                        if name == 'second':
                            second_started.set()
                        body = ({'debug': False} if path == '/api/config' else
                                {'dlive_ip': '127.0.0.2', 'dlive_enabled': False})
                        response = client.post(path, headers=self.headers, json=body)
                        responses[path] = (response.status_code, response.get_json())
                    except BaseException as exc:
                        errors.append(repr(exc))

                other = '/api/digico/setup' if first_path == '/api/config' else '/api/config'
                with patch.object(self.webui.utils, 'save_config', side_effect=save):
                    first = threading.Thread(target=post, args=(first_path, 'first'), name='first-save')
                    second = threading.Thread(target=post, args=(other, 'second'))
                    first.start()
                    try:
                        self.assertTrue(at_save.wait(5))
                        second.start()
                        self.assertTrue(second_started.wait(5))
                        # Give the second request an opportunity to take a stale snapshot.
                        time.sleep(.15)
                    finally:
                        release.set()
                        first.join(8)
                        if second.ident is not None:
                            second.join(8)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertFalse(errors, errors)
                self.assertEqual({k: v[0] for k, v in responses.items()},
                                 {'/api/config': 200, '/api/digico/setup': 200}, responses)
                self.assertEqual(self.cfg['dlive_ip'], '127.0.0.2',
                                 'Generic Config restored an old protected dLive endpoint after setup succeeded')
                self.assertFalse(self.cfg['debug'], 'Dedicated setup lost an unrelated concurrent Config edit')
                client = self.webui._get_digico_client_from_config()
                self.assertEqual(client.config['dlive_ip'], '127.0.0.2')
                self.assertFalse(client.config['dlive_enabled'])
                self.assertTrue(old_client.stop.is_set())
                with self.assertRaises(ConnectionError):
                    old_client.set_level(2002, 1, -12)
                self.assertFalse(any(m.startswith(bytes.fromhex('f0 00 00 1a 50 10 01 00 03 0d'))
                                     for m in self.desk.messages), 'Config mutations must not execute audio writes')

    def test_import_and_setup_share_complete_reload_lifecycle_boundary(self):
        import json
        import tempfile
        from pathlib import Path
        self.cfg['dlive_enabled'] = False
        entered, release, setup_started = threading.Event(), threading.Event(), threading.Event()
        errors, results = [], {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'config.json'
            target.write_text(json.dumps(self.cfg))
            archive = root / 'import.zip'
            original_import = self.webui._apply_config_transport_import_locked

            def paused_import(*args):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Import barrier timed out')
                return original_import(*args)

            def reload_config(**kwargs):
                self.cfg.clear()
                self.cfg.update(json.loads(target.read_text()))

            def save(cfg):
                target.write_text(json.dumps(cfg))

            def run_import():
                try:
                    results['import'] = self.webui._apply_config_transport_import(archive, ['config'])
                except BaseException as exc:
                    errors.append(repr(exc))

            def run_setup():
                try:
                    client = self.webui.app.test_client()
                    with client.session_transaction() as session:
                        session.update(_user_id='1', _fresh=True, _auth_session_id='fixture-1',
                                       _last_activity=int(time.time()), _csrf='ignore-csrf')
                    setup_started.set()
                    result = client.post('/api/digico/setup', headers=self.headers,
                                         json={'dlive_ip': '127.0.0.2', 'dlive_enabled': False})
                    results['setup'] = result.status_code
                except BaseException as exc:
                    errors.append(repr(exc))

            with (patch.object(self.webui, '_APP_ROOT', root),
                  patch.object(self.webui.utils, 'CONFIG_FILE', str(target)),
                  patch.object(self.webui.utils, 'reload_config', side_effect=reload_config),
                  patch.object(self.webui.utils, 'save_config', side_effect=save),
                  patch.object(self.webui, '_apply_config_transport_import_locked', side_effect=paused_import)):
                payload, _ = self.webui._create_config_transport_zip(['config'], reason='test')
                archive.write_bytes(payload)
                first = threading.Thread(target=run_import)
                second = threading.Thread(target=run_setup)
                first.start()
                try:
                    self.assertTrue(entered.wait(5))
                    second.start()
                    self.assertTrue(setup_started.wait(5))
                    time.sleep(.15)
                finally:
                    release.set()
                    first.join(8)
                    if second.ident is not None:
                        second.join(8)
            self.assertFalse(errors, errors)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(results['setup'], 200)
            self.assertEqual(results['import'][0][0]['id'], 'config')
            self.assertEqual(self.cfg['dlive_ip'], '127.0.0.2')
            self.assertFalse(self.cfg['dlive_enabled'])
            self.assertEqual(self.cfg['personal_mix_provider'], 'dlive')
            self.assertEqual(json.loads(target.read_text())['dlive_ip'], '127.0.0.2')
            client = self.webui._get_digico_client_from_config()
            self.assertEqual(client.config['dlive_ip'], '127.0.0.2')


# Reuse real-auth setup without duplicating the base acceptance cases.
for _name in list(_api_fixture.DLiveApiTests.__dict__):
    if _name.startswith('test_'):
        setattr(ConfigConcurrencyInvariant, _name, None)


class NetworkCapacityInvariant(unittest.TestCase):
    def test_live_and_hidden_demand_share_fair_background_refresh_and_bounded_leases(self):
        import dlive
        from test_dlive import wait_for
        desk = Desk()
        cfg = dict(config(desk.port), dlive_request_interval=.025, dlive_stale_after=10,
                   dlive_auxes=[dict(channel=2000+i, bus=i, stereo=True) for i in range(1, 5)],
                   dlive_channels=[dict(channel=i) for i in range(1, 129)])
        mixer = dlive.DLiveMixerClient(cfg)
        try:
            mixer.start()
            for number in range(2001, 2005):
                mixer.request_aux_state(number)
            wait_for(lambda: len(mixer.observed) == 512 and len(mixer.master) == 8, timeout=5)
            with mixer.lock:
                first = dict(mixer.observed)
                initial_master = dict(mixer.master)
            end = time.monotonic() + 12
            while time.monotonic() < end:
                # Many live phones on AUX1; AUX2-4 are hidden (no HTTP polls).
                for _ in range(20):
                    mixer.request_aux_state(2001)
                time.sleep(.1)
            with mixer.lock:
                for key, (_, stamp) in mixer.observed.items():
                    self.assertGreater(stamp, first[key][1] + 10, key)
                    self.assertLess(time.time() - stamp, 10, key)
                for key, (_, stamp) in mixer.master.items():
                    self.assertGreater(stamp, initial_master[key][1] + 10, key)
                    self.assertLess(time.time() - stamp, 10, key)
                self.assertLessEqual(len(mixer.pending), 132)
                self.assertEqual(len(mixer.demand_order), 4)
                self.assertFalse(mixer.status()['pollingWarning'])
                # Expire only hidden leases without making the test wait 65s.
                for number in (2002, 2003, 2004):
                    mixer.last_request[number] -= 66
                batch = mixer._read_batch(time.monotonic())
                self.assertLessEqual(len(batch), 16)
                self.assertEqual(list(mixer.demand_order), [2001])
            # Returning hidden phone gets a fair new lease, not an audio replay.
            mixer.request_aux_state(2002)
            wait_for(lambda: mixer.master[(2002, 'level')][1] > time.time() - 1)
            self.assertEqual(len(desk.connections), 1)
            self.assertFalse(any(m.startswith(bytes.fromhex('f0 00 00 1a 50 10 01 00 03 0d')) for m in desk.messages))
        finally:
            mixer.close()
            desk.close()

    def test_overcapacity_is_explained_without_inventing_observations(self):
        import dlive
        cfg = dict(config(1), dlive_enabled=False, dlive_request_interval=.5, dlive_stale_after=10,
                   dlive_auxes=[dict(channel=2000+i, bus=i, stereo=True) for i in range(1, 5)],
                   dlive_channels=[dict(channel=i) for i in range(1, 129)])
        mixer = dlive.DLiveMixerClient(cfg)
        for number in range(2001, 2005):
            for _ in range(20):
                mixer.request_aux_state(number)  # many phones share one lease
        status = mixer.status()
        self.assertIn('refresh budget', status['pollingWarning'])
        self.assertEqual(status['polling']['activeAuxes'], 4)
        self.assertEqual(status['polling']['fields'], 520)
        self.assertEqual(status['polling']['batchLimit'], 16)
        self.assertFalse(status['ready'])
        self.assertFalse(mixer.observed)
        self.assertFalse(mixer.master)
        self.assertEqual(len(mixer.pending), 0)

    def test_four_128_input_mixes_must_not_expire_on_healthy_default_transport(self):
        import dlive
        desk = Desk()
        cfg = dict(config(desk.port), dlive_request_interval=.025, dlive_stale_after=10,
                   dlive_auxes=[dict(channel=2000+i, bus=i, stereo=True) for i in range(1, 5)],
                   dlive_channels=[dict(channel=i) for i in range(1, 129)])
        mixer = dlive.DLiveMixerClient(cfg)
        ever_fresh = False
        first_stamps = {}
        max_response = 0
        max_pending = 0
        end = time.monotonic() + 27
        try:
            mixer.start()
            while time.monotonic() < end:
                started = time.monotonic()
                states = [mixer.aux_state(2000+i) for i in range(1, 5)]
                max_response = max(max_response, time.monotonic() - started)
                max_pending = max(max_pending, len(mixer.pending))
                value = states[0]['channels'][0]
                if value['level'] is not None:
                    ever_fresh = True
                elif ever_fresh:
                    self.fail('A healthy desk loses previously read send controls with only four active 128-input AUXes')
                for state in states:
                    for field in state['channels']:
                        if field['observedAt']:
                            first_stamps.setdefault((state['aux']['channel'], field['channel']), field['observedAt'])
                time.sleep(.1)
            self.assertTrue(ever_fresh, 'Fixture never received a first send')
            self.assertEqual(len(first_stamps), 512, 'Every active AUX/input must get a fair initial read')
            for state in states:
                for field in state['channels']:
                    self.assertFalse(field['stale'])
                    self.assertGreater(field['observedAt'], first_stamps[(state['aux']['channel'], field['channel'])] + 10)
            self.assertLess(max_response, .25, 'Foreground reads must not wait for the desk refresh')
            self.assertLessEqual(max_pending, 132, 'Full AUX snapshots must not accumulate in the shared queue')
            self.assertFalse(mixer.status()['pollingWarning'])
            self.assertEqual(len(desk.connections), 1)
            self.assertFalse(any(m.startswith(bytes.fromhex('f0 00 00 1a 50 10 01 00 03 0d')) for m in desk.messages))
            print('CAPACITY four AUX / 512 sends: all refreshed beyond 10s; max foreground', max_response,
                  'max queue', max_pending, 'queries', len(desk.messages))
        finally:
            mixer.close()
            desk.close()
