"""Desired regression: unrelated cloud setup must not block live cached mixer reads."""
import threading
import time
from unittest.mock import Mock, patch
from test_dlive import wait_for
from test_dlive_webui import DLiveApiTests

class ReviewerLiveReadResponsiveness(DLiveApiTests):
    def test_pixie_cloud_provisioning_must_not_block_cached_dlive_state(self):
        mixer = self.webui._get_digico_client_from_config()
        wait_for(lambda: mixer.aux_state(2002)['channels'][0]['level'] is not None)
        entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
        replies, errors = {}, []
        def client():
            c = self.webui.app.test_client()
            with c.session_transaction() as session:
                session.update(_user_id='1', _fresh=True, _auth_session_id='fixture-1',
                    _last_activity=int(time.time()), _csrf='ignore-csrf')
            return c
        def provisioning(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('review cloud timeout')
            return {'login':{'currentHomeId':'synthetic-home'}, 'homes':[{'objectId':'synthetic-home',
                'netId':'synthetic-net', 'meshNet2':'synthetic-mesh'}]}
        def setup():
            try:
                result = client().put('/api/pixie/config', headers=self.headers,
                    json={'pixie_network_mode':'observe', 'pixie_home_id':'synthetic-home',
                          'pixie_auditoriums':[], 'pixie_devices':[], 'pixie_scenes':[]})
                replies['setup'] = result.status_code
                replies['setup_body'] = result.get_json()
            except BaseException as exc:
                errors.append(repr(exc))
        def read():
            try:
                start = time.monotonic()
                replies['read'] = client().get('/api/digico/aux/2002/state').status_code
                replies['read_seconds'] = time.monotonic() - start
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                read_done.set()
        manager = Mock()
        manager.status.return_value = {'devices':[], 'scenes':[]}
        with (patch.object(self.webui, '_get_pixie_manager_from_config', return_value=manager),
              patch.object(self.webui, '_load_pixie_secrets', return_value=None),
              patch.object(self.webui, '_save_pixie_secrets'),
              patch.object(self.webui, '_close_pixie_manager'),
              patch.object(self.webui, '_provision_pixie_cloud', side_effect=provisioning)):
            a, b = threading.Thread(target=setup), threading.Thread(target=read)
            a.start()
            try:
                self.assertTrue(entered.wait(5), ('setup did not enter real provisioning path', replies, errors))
                b.start()
                responsive = read_done.wait(1.0)
            finally:
                release.set()
                a.join(8)
                if b.ident is not None:
                    b.join(8)
        print('REVIEW PIXIE/DLIVE cached-read responsiveness:', replies, 'completed before cloud release=', responsive)
        self.assertFalse(errors, errors)
        self.assertEqual(replies['setup'], 200)
        self.assertEqual(replies['read'], 200)
        self.assertTrue(responsive, 'Cached live dLive state blocked for entire unrelated Pixie cloud operation')

for name in DLiveApiTests.__dict__:
    if name.startswith('test_'):
        setattr(ReviewerLiveReadResponsiveness, name, None)
del DLiveApiTests


# The independent desired-invariant regression above is retained verbatim.
import copy
import json
import tempfile
from pathlib import Path
import pixie


class PixieConfigurationTransactions(ReviewerLiveReadResponsiveness):
    test_pixie_cloud_provisioning_must_not_block_cached_dlive_state = None

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = self.root / 'config.json'
        self.cfg.update(debug=True, pixie_network_mode='disabled',
                        pixie_gateway_host='original-gateway', pixie_home_id='original-home',
                        pixie_home_name='Original', pixie_net_id='original-net',
                        pixie_mesh_net='', pixie_mesh_net_2='original-mesh',
                        pixie_auditoriums=[], pixie_devices=[], pixie_scenes=[])
        self.target.write_text(json.dumps(self.cfg))
        pixie.save_pixie_secrets(self.root, 'fixture-account', 'fixture-password')
        self.manager = Mock()
        self.manager.status.return_value = {'devices': [], 'scenes': []}
        self.stack.enter_context(patch.object(self.webui, '_APP_ROOT', self.root))
        self.stack.enter_context(patch.object(self.webui.utils, 'CONFIG_FILE', str(self.target)))
        self.stack.enter_context(patch.object(self.webui, '_get_pixie_manager_from_config', return_value=self.manager))
        self.close_manager = self.stack.enter_context(patch.object(self.webui, '_close_pixie_manager'))
        self.save_config = self.stack.enter_context(patch.object(self.webui.utils, 'save_config', side_effect=self.persist))
        self.stack.enter_context(patch.object(self.webui.utils, 'reload_config', side_effect=self.reload))
        self.slow_body = dict(pixie_network_mode='observe', pixie_gateway_host='slow-gateway',
                              pixie_home_id='slow-home', pixie_auditoriums=[],
                              pixie_devices=[], pixie_scenes=[])
        self.disabled_body = dict(self.slow_body, pixie_network_mode='disabled',
                                  pixie_gateway_host='original-gateway', pixie_home_id='original-home')

    def persist(self, cfg):
        self.target.write_text(json.dumps(cfg))
        self.cfg.clear()
        self.cfg.update(copy.deepcopy(cfg))

    def reload(self, **kwargs):
        self.cfg.clear()
        self.cfg.update(json.loads(self.target.read_text()))

    def request_client(self):
        client = self.webui.app.test_client()
        with client.session_transaction() as session:
            session.update(_user_id='1', _fresh=True, _auth_session_id='fixture-1',
                           _last_activity=int(time.time()), _csrf='ignore-csrf')
        return client

    def during_cloud(self, action):
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def provision(*args):
            entered.set()
            if not release.wait(6):
                raise RuntimeError('cloud barrier timed out')
            return {'homes': [{'objectId': 'slow-home', 'netId': 'slow-net', 'meshNet2': 'slow-mesh'}]}

        def worker():
            try:
                response = self.request_client().put('/api/pixie/config', headers=self.headers, json=self.slow_body)
                results.append((response.status_code, response.get_json()))
            except BaseException as exc:
                errors.append(repr(exc))

        with patch.object(self.webui, '_provision_pixie_cloud', side_effect=provision):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                action()
            finally:
                release.set()
                thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 1)
        return results[0]

    def assert_stale(self, result):
        self.assertEqual(result[0], 409, result)
        self.assertEqual(result[1].get('code'), 'configuration_changed', result)
        self.assertEqual(json.loads(self.target.read_text()), self.cfg)
        self.assertFalse(any(call.kwargs.get('status') == 'success'
                             for call in self.webui.log_event.call_args_list
                             if call.args[0] == 'pixie.config.update' and
                             call.kwargs.get('details', {}).get('gateway_host') == 'slow-gateway'))

    def test_delayed_provisioning_composes_with_successful_config_and_mixer_setup(self):
        from test_dlive import wait_for
        mixer = self.webui._get_digico_client_from_config()
        wait_for(lambda: mixer.aux_state(2002)['channels'][0]['level'] is not None)

        def concurrent():
            client = self.request_client()
            # Real auth, real factory/socket fake desk: reads and explicit controls
            # must complete while the unrelated cloud operation is still paused.
            self.assertEqual(client.get('/api/digico/aux/2002/state').status_code, 200)
            self.assertEqual(client.post('/api/digico/aux/2002/channel/1/level',
                             headers=self.headers, json={'value': -12, 'final': True}).status_code, 200)
            wait_for(lambda: self.desk.level == 83)
            self.assertEqual(client.post('/api/config', headers=self.headers, json={'debug': False}).status_code, 200)
            response = client.post('/api/digico/setup', headers=self.headers,
                                   json={'dlive_ip': '127.0.0.2', 'dlive_enabled': False})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertTrue(mixer.stop.is_set())

        result = self.during_cloud(concurrent)
        self.assertEqual(result[0], 200, result)
        self.assertFalse(self.cfg['debug'])
        self.assertFalse(self.cfg['dlive_enabled'])
        self.assertEqual(self.cfg['dlive_ip'], '127.0.0.2')
        self.assertEqual(self.cfg['pixie_gateway_host'], 'slow-gateway')
        current = self.webui._get_digico_client_from_config()
        self.assertFalse(current.config['dlive_enabled'])
        self.assertEqual(current.config['dlive_ip'], '127.0.0.2')
        with self.assertRaises(ConnectionError):
            mixer.set_level(2002, 1, -10)
        self.assertEqual(json.loads(self.target.read_text()), self.cfg)

    def test_later_pixie_save_even_same_values_supersedes_pending_provisioning(self):
        before = copy.deepcopy(self.cfg)
        def concurrent():
            response = self.request_client().put('/api/pixie/config', headers=self.headers, json=self.disabled_body)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(self.cfg, before)  # ABA: value-only checks cannot detect this save.
        self.assert_stale(self.during_cloud(concurrent))
        self.assertEqual(self.cfg, before)
        self.close_manager.assert_called_once()
        self.save_config.assert_called_once()

    def test_changed_pixie_endpoint_rejects_pending_provisioning(self):
        def concurrent():
            response = self.request_client().post('/api/config', headers=self.headers,
                                                  json={'pixie_gateway_host': 'new-gateway'})
            self.assertEqual(response.status_code, 200, response.get_json())
        self.assert_stale(self.during_cloud(concurrent))
        self.assertEqual(self.cfg['pixie_gateway_host'], 'new-gateway')
        self.close_manager.assert_not_called()
        self.save_config.assert_called_once()

    def test_changed_credentials_reject_pending_provisioning_without_secret_overwrite(self):
        before = copy.deepcopy(self.cfg)
        def concurrent():
            pixie.save_pixie_secrets(self.root, 'replacement-account', 'replacement-password')
        self.assert_stale(self.during_cloud(concurrent))
        self.assertEqual(self.cfg, before)
        self.assertEqual(pixie.load_pixie_secrets(self.root).username, 'replacement-account')
        self.close_manager.assert_not_called()
        self.save_config.assert_not_called()

    def test_import_even_identical_pixie_values_supersedes_pending_provisioning(self):
        payload, _ = self.webui._create_config_transport_zip(['config'], reason='test')
        archive = self.root / 'import.zip'
        archive.write_bytes(payload)
        def concurrent():
            # Exercise the real import replacement and guarded lifecycle wrapper.
            with patch.object(self.webui, '_clear_imported_config_caches', side_effect=self.reload):
                imported, _ = self.webui._apply_config_transport_import(archive, ['config'])
            self.assertEqual(imported[0]['id'], 'config')
            self.assertEqual(self.cfg['pixie_gateway_host'], 'original-gateway')
            self.assertFalse(self.cfg['dlive_enabled'])
        self.assert_stale(self.during_cloud(concurrent))
        self.assertFalse(self.cfg['dlive_enabled'])
        self.assertEqual(self.cfg['pixie_network_mode'], 'disabled')
        self.close_manager.assert_not_called()
        self.save_config.assert_not_called()


    def test_provisioning_and_validation_errors_do_not_write_or_replace_manager(self):
        before_cfg = copy.deepcopy(self.cfg)
        before_disk = self.target.read_bytes()
        before_secrets = (self.root / 'pixie_secrets.json').read_bytes()
        for response, body, expected in (
                (RuntimeError('fixture cloud unavailable'), self.slow_body, 502),
                ({'homes': []}, self.slow_body, 400),
                ({'homes': [{'objectId': 'slow-home', 'netId': 'missing-mesh'}]}, self.slow_body, 400),
                ({}, dict(self.slow_body, pixie_network_mode='invalid'), 400),
                ({}, dict(self.slow_body, pixie_devices='invalid'), 400)):
            with self.subTest(expected=expected, response=type(response).__name__):
                kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch.object(self.webui, '_provision_pixie_cloud', **kwargs):
                    result = self.request_client().put('/api/pixie/config', headers=self.headers, json=body)
                self.assertEqual(result.status_code, expected, result.get_json())
                self.assertEqual(self.cfg, before_cfg)
                self.assertEqual(self.target.read_bytes(), before_disk)
                self.assertEqual((self.root / 'pixie_secrets.json').read_bytes(), before_secrets)
        self.save_config.assert_not_called()
        self.close_manager.assert_not_called()

    def test_failed_local_persistence_restores_config_and_secret_bytes(self):
        before_cfg = copy.deepcopy(self.cfg)
        before_disk = self.target.read_bytes()
        secret_path = self.root / 'pixie_secrets.json'
        before_secrets = secret_path.read_bytes()
        body = dict(self.disabled_body, pixie_username='new-account', pixie_password='new-password')

        def partial_secrets(*args):
            secret_path.write_text('partial secret write')
            raise OSError('fixture secret storage failure')

        def partial_config(cfg):
            self.target.write_text('partial config write')
            raise OSError('fixture config storage failure')

        def unchanged_config(cfg):
            raise OSError('fixture config storage failure before writing')

        for failing_target, callback, request_body in (
                ('_save_pixie_secrets', partial_secrets, body),
                ('save_config', partial_config, body),
                ('save_config', unchanged_config, self.disabled_body)):
            with self.subTest(failing_target=failing_target, callback=callback.__name__):
                self.target.write_bytes(before_disk)
                secret_path.write_bytes(before_secrets)
                secret_path.chmod(0o640)
                owner = self.webui if failing_target.startswith('_') else self.webui.utils
                with patch.object(owner, failing_target, side_effect=callback):
                    result = self.request_client().put('/api/pixie/config', headers=self.headers, json=request_body)
                self.assertEqual(result.status_code, 502, result.get_json())
                self.assertEqual(self.cfg, before_cfg, 'A failed persist must not mutate the live config snapshot')
                self.assertEqual(self.target.read_bytes(), before_disk)
                self.assertEqual(secret_path.read_bytes(), before_secrets)
                self.assertEqual(secret_path.stat().st_mode & 0o777, 0o640)
        self.close_manager.assert_not_called()


    def test_newer_provisioned_home_and_mode_win_over_older_cloud_result(self):
        def concurrent():
            newer = dict(self.slow_body, pixie_network_mode='control', pixie_home_id='new-home',
                         pixie_gateway_host='new-gateway', pixie_username='new-account',
                         pixie_password='new-password')
            homes = {'homes': [{'objectId': 'new-home', 'netId': 'new-net', 'meshNet2': 'new-mesh'}]}
            with patch.object(self.webui, '_provision_pixie_cloud', return_value=homes):
                response = self.request_client().put('/api/pixie/config', headers=self.headers, json=newer)
            self.assertEqual(response.status_code, 200, response.get_json())
        self.assert_stale(self.during_cloud(concurrent))
        self.assertEqual(self.cfg['pixie_network_mode'], 'control')
        self.assertEqual(self.cfg['pixie_home_id'], 'new-home')
        self.assertEqual(self.cfg['pixie_gateway_host'], 'new-gateway')
        self.assertEqual(pixie.load_pixie_secrets(self.root).username, 'new-account')
        self.close_manager.assert_called_once()
        self.save_config.assert_called_once()

    def test_local_commit_holds_boundary_through_reload_and_manager_replacement(self):
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        results, errors = {}, []
        def reload(**kwargs):
            if threading.current_thread().name == 'pixie-local-commit':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('reload barrier timed out')
            self.reload(**kwargs)
        def request(name, path, body):
            try:
                if name == 'generic':
                    second_started.set()
                client = self.request_client()
                response = (client.put(path, headers=self.headers, json=body) if name == 'pixie' else
                            client.post(path, headers=self.headers, json=body))
                results[name] = response.status_code
            except BaseException as exc:
                errors.append(repr(exc))
        with patch.object(self.webui.utils, 'reload_config', side_effect=reload):
            first = threading.Thread(name='pixie-local-commit', target=request,
                                     args=('pixie', '/api/pixie/config', self.disabled_body))
            second = threading.Thread(target=request, args=('generic', '/api/config', {'debug': False}))
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                second.start()
                self.assertTrue(second_started.wait(5))
                time.sleep(.15)
                self.assertEqual(self.save_config.call_count, 1,
                                 'A concurrent writer crossed the local reload/lifecycle boundary')
                self.close_manager.assert_not_called()
            finally:
                release.set()
                first.join(8)
                if second.ident is not None:
                    second.join(8)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(results, {'pixie': 200, 'generic': 200})
        self.assertFalse(self.cfg['debug'])
        self.close_manager.assert_called_once()
        self.assertEqual(json.loads(self.target.read_text()), self.cfg)

    # Independent R4 desired-invariant methods retained verbatim.
    def test_generic_noop_pixie_disable_supersedes_pending_cloud(self):
        before = copy.deepcopy(self.cfg)
        def concurrent():
            response = self.request_client().post('/api/config', headers=self.headers,
                                                   json={'pixie_network_mode':'disabled'})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(self.cfg, before)
        result = self.during_cloud(concurrent)
        print('REVIEW generic no-op disabled:', result, 'final mode=', self.cfg['pixie_network_mode'])
        self.assert_stale(result)
        self.assertEqual(self.cfg['pixie_network_mode'], 'disabled')

    def test_generic_endpoint_ABA_supersedes_pending_cloud(self):
        before = copy.deepcopy(self.cfg)
        def concurrent():
            for host in ['temporary-replacement', 'original-gateway']:
                response = self.request_client().post('/api/config', headers=self.headers,
                                                       json={'pixie_gateway_host':host})
                self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(self.cfg, before)
        result = self.during_cloud(concurrent)
        print('REVIEW generic endpoint ABA:', result, 'final gateway=', self.cfg['pixie_gateway_host'])
        self.assert_stale(result)
        self.assertEqual(self.cfg['pixie_gateway_host'], 'original-gateway')
