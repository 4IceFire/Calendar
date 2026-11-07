"""Real-auth APIs backed by actual dLive TCP simulator, no hardware."""
import copy
import threading
from unittest.mock import patch
from test_dlive import Desk, config, wait_for
import unittest
import test_videohub_ignore


class ProviderAuthorizationRaceTests(unittest.TestCase):
    """Real Flask authentication/setup, fake cached clients, no device sockets."""

    def setUp(self):
        fixture = test_videohub_ignore.VideoHubIgnoreApiTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.webui, self.cfg = fixture.webui, fixture.cfg
        self.client, self.headers = fixture.client, fixture.headers
        self.stack = fixture.stack
        self.cfg.update(config(1))  # Valid mapping only; the fake factory never opens TCP.
        self.cfg.update(digico_enabled=True,
                        digico_auxes=[dict(enabled=True), dict(enabled=True)],
                        digico_channels=[dict(enabled=True)])
        self.webui._set_group_pages(2, ['page:digico_mixer'])
        self.webui._set_group_digico_allowed_auxes(2, ['1', '__all_dlive__'])
        self.admin = self.webui.app.test_client()
        with self.client.session_transaction() as session:
            admin_session = dict(session)
        with self.admin.session_transaction() as session:
            session.update(admin_session)
        fixture.login(2)
        self.calls = []
        self.checkpoint = lambda: None
        owner = self

        class FakeMixer:
            def __init__(self, provider):
                self.provider = provider

            def mixer_config(self):
                payload = dict(provider=self.provider,
                               auxes=[dict(channel=n, enabled=True) for n in
                                      ([1, 2] if self.provider == 'digico' else [2002])],
                               channels=[dict(channel=1, enabled=True)])
                if threading.current_thread() is threading.main_thread():
                    owner.checkpoint()
                return payload

            def status(self):
                return dict(provider=self.provider, revision=1)

            def route_enabled(self, aux, channel=None):
                return aux in ([1, 2] if self.provider == 'digico' else [2002])

            def request_aux_state(self, aux):
                owner.calls.append((self.provider, 'request_aux_state', aux))

            def aux_state(self, aux):
                owner.calls.append((self.provider, 'aux_state', aux))
                return dict(aux=aux, channels=[])

            def set_level(self, aux, channel, value):
                owner.calls.append((self.provider, 'set_level', aux))
                return value

            def set_master_level(self, aux, value):
                owner.calls.append((self.provider, 'set_master_level', aux))
                return value

            def input_meter_state(self):
                return dict(provider=self.provider, revision=1, channels=[])

        clients = {provider: FakeMixer(provider) for provider in ('dlive', 'digico')}
        self.stack.enter_context(patch.object(self.webui, '_digico_client_factory',
            side_effect=lambda cfg: clients[cfg['personal_mix_provider']]))
        self.stack.enter_context(patch.object(self.webui, '_close_digico_client'))
        self.stack.enter_context(patch.object(self.webui.utils, 'save_config',
            side_effect=lambda cfg: self.cfg.update(copy.deepcopy(cfg))))
        self.stack.enter_context(patch.object(self.webui.utils, 'reload_config'))

    def interleave_switch(self, request, *, target='digico', after_guard=True):
        reached = threading.Event()
        attempted = threading.Event()
        completed = threading.Event()
        results, errors = [], []
        lock = self.webui._media_operation_lock

        class ObservedLock:
            def __enter__(inner):
                if threading.current_thread() is switch:
                    attempted.set()
                return lock.__enter__()

            def __exit__(inner, *args):
                return lock.__exit__(*args)

        def change_provider():
            try:
                if not reached.wait(3):
                    raise AssertionError('Operational request did not reach race seam')
                results.append(self.admin.post('/api/digico/setup', headers=self.headers,
                                               json={'personal_mix_provider': target}))
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        def checkpoint():
            reached.set()
            self.assertTrue(attempted.wait(3), 'Admin did not attempt configuration lock')
            # Broken code permits the switch here. Correct code blocks the admin;
            # never synchronously wait for that mutation before releasing the route.
            completed.wait(0.25)

        guard = self.webui._digico_api_guard

        def guarded(**kwargs):
            denied = guard(**kwargs)
            if threading.current_thread() is threading.main_thread():
                checkpoint()
            return denied

        switch = threading.Thread(target=change_provider, daemon=True)
        with patch.object(self.webui, '_media_operation_lock', ObservedLock()):
            if after_guard:
                seam = patch.object(self.webui, '_digico_api_guard', side_effect=guarded)
            else:
                seam = patch.object(self, 'checkpoint', side_effect=checkpoint)
            with seam:
                switch.start()
                try:
                    response = request()
                finally:
                    reached.set()
                    switch.join(3)
            self.assertFalse(switch.is_alive(), 'Provider switch deadlocked')
        self.assertFalse(errors, errors)
        self.assertTrue(attempted.is_set())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status_code, 200, results[0].get_json())
        self.assertEqual(self.cfg['personal_mix_provider'], target)
        return response

    def assert_aux2_is_not_authorized_by_dlive_all(self, method, suffix):
        path = '/api/digico/aux/2/' + suffix
        request = lambda: self.client.open(path, method=method, headers=self.headers,
                                           json={'value': -12} if method == 'POST' else None)
        response = self.interleave_switch(request)
        self.assertEqual(self.calls, [], 'dLive All authorized a command/read on DiGiCo AUX2')
        self.assertEqual(response.status_code, 404, response.get_json())
        self.assertEqual(request().status_code, 403)  # New-provider scope is AUX1 only.

    def test_channel_write_cannot_cross_provider_authorization(self):
        self.assert_aux2_is_not_authorized_by_dlive_all('POST', 'channel/1/level')

    def test_aux_read_cannot_cross_provider_authorization(self):
        self.assert_aux2_is_not_authorized_by_dlive_all('GET', 'state')

    def test_master_control_cannot_cross_provider_authorization(self):
        self.assert_aux2_is_not_authorized_by_dlive_all('POST', 'master/level')

    def test_mixer_config_cannot_filter_old_client_with_new_provider_all(self):
        self.cfg['personal_mix_provider'] = 'digico'
        response = self.interleave_switch(lambda: self.client.get('/api/digico/mixer/config'),
                                          target='dlive', after_guard=False)
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload['provider'], 'digico')
        self.assertEqual([aux['channel'] for aux in payload['auxes']], [1])
        current = self.client.get('/api/digico/mixer/config').get_json()
        self.assertEqual(current['provider'], 'dlive')
        self.assertEqual([aux['channel'] for aux in current['auxes']], [2002])

    def test_input_meter_read_keeps_authorized_provider_snapshot(self):
        response = self.interleave_switch(lambda: self.client.get('/api/digico/input-meters'))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['provider'], 'dlive')


class DLiveApiTests(unittest.TestCase):
    def setUp(self):
        fixture = test_videohub_ignore.VideoHubIgnoreApiTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.cfg = fixture.cfg
        self.webui = fixture.webui
        self.stack = fixture.stack
        self.client = fixture.client
        self.headers = fixture.headers
        self.login = fixture.login
        self.desk = Desk()
        self.addCleanup(self.desk.close)
        import personal_mixes
        self.addCleanup(personal_mixes.close_client)
        self.cfg.update(config(self.desk.port))
        self.webui._set_group_pages(2, ['page:digico_mixer'])
        self.webui._set_group_digico_allowed_auxes(2, ['2002'])
        self.stack.enter_context(patch.object(self.webui.utils, 'save_config', side_effect=lambda cfg: self.cfg.update(copy.deepcopy(cfg))))
        self.stack.enter_context(patch.object(self.webui.utils, 'reload_config'))

    def test_active_provider_reaches_real_socket_through_legacy_and_v1_routes(self):
        self.login(2)
        payload = self.client.get('/api/digico/mixer/config').get_json()
        self.assertEqual(payload['provider'], 'dlive')
        self.assertEqual([a['channel'] for a in payload['auxes']], [2002])
        response = self.client.get('/api/v1/digico/aux/2002/state')
        self.assertEqual(response.status_code, 200)
        wait_for(lambda: self.client.get('/api/digico/aux/2002/state').get_json()['channels'][0]['level'] is not None)
        response = self.client.post('/api/digico/aux/2002/channel/1/level', headers=self.headers,
                                    json=dict(value=-12, final=True))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(response.get_json()['confirmed'])
        wait_for(lambda: self.desk.level == 83)
        self.assertFalse(payload['capabilities']['sendOn'])
        for field in ['on', 'pan']:
            response = self.client.post(f'/api/digico/aux/2002/channel/1/{field}', headers=self.headers, json=dict(value=1))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post('/api/digico/aux/2002/channel/1/level', json=dict(value=-5)).status_code, 403)
        self.assertEqual(self.client.get('/api/digico/aux/1001/state').status_code, 403)
        self.assertEqual(self.client.post('/api/digico/setup', headers=self.headers, json={}).status_code, 403)

    def test_master_controls_share_aux_scope_and_unsupported_provider_fails_closed(self):
        self.login(2)
        response = self.client.post('/api/digico/aux/2002/master/level', headers=self.headers, json=dict(value=-12))
        # Establish connection through a read, then issue the explicit command.
        wait_for(lambda: self.client.get('/api/digico/aux/2002/state').get_json()['status']['connected'])
        response = self.client.post('/api/digico/aux/2002/master/level', headers=self.headers, json=dict(value=-12))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.client.post('/api/digico/aux/1001/master/mute', headers=self.headers, json=dict(value=True)).status_code, 403)
        self.assertEqual(self.client.post('/api/digico/aux/2002/master/mute', headers=self.headers, json=dict(value='false')).status_code, 400)

    def test_setup_page_exposes_provider_and_manual_mapping_without_operator_redesign(self):
        response = self.client.get('/config/digico')
        self.assertIn(b'personal-mix-provider', response.data)
        self.assertIn(b'dlive-aux-mappings', response.data)
        self.assertIn(b'dlive_setup.js', response.data)

    def test_import_validates_mappings_preserves_provider_and_disables_dlive(self):
        import json
        import zipfile
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'config.json'
            target.write_text(json.dumps(self.cfg))
            archive = root / 'import.zip'
            with patch.object(self.webui, '_APP_ROOT', root), patch.object(self.webui.utils, 'CONFIG_FILE', str(target)), patch.object(self.webui, '_clear_imported_config_caches'):
                payload, _ = self.webui._create_config_transport_zip(['config'], reason='test')
                archive.write_bytes(payload)
                imported, _ = self.webui._apply_config_transport_import(archive, ['config'])
                restored = json.loads(target.read_text())
                self.assertFalse(restored['dlive_enabled'])
                self.assertEqual(restored['personal_mix_provider'], 'dlive')
                self.assertEqual(restored['dlive_auxes'][0]['channel'], 2002)
                self.assertEqual(imported[0]['id'], 'config')

    def test_automation_setup_denied_and_aux_scope_lookup_failure_is_not_allow_all(self):
        import api_security
        record = api_security.create_service_token(self.webui._AUTH_DB_PATH, name='Mixer', scopes=['digico'])
        headers = {'Authorization': 'Bearer ' + record['token']}
        self.client = self.webui.app.test_client()
        self.assertEqual(self.client.post('/api/digico/setup', headers=headers, json={}).status_code, 403)
        self.assertEqual(self.client.get('/api/digico/mixer/config', headers=headers).status_code, 200)
        restricted = api_security.create_service_token(self.webui._AUTH_DB_PATH, name='Read mixer only', scopes=['digico'],
            constraints={'allowed_paths': ['GET /api/digico/mixer/config']})
        narrow = {'Authorization': 'Bearer ' + restricted['token']}
        self.assertEqual(self.client.get('/api/digico/mixer/config', headers=narrow).status_code, 200)
        self.assertEqual(self.client.post('/api/digico/aux/2002/channel/1/level', headers=narrow, json={'value': -12}).status_code, 403)
        self.assertEqual(self.client.post('/api/digico/aux/2002/master/mute', headers=narrow, json={'value': True}).status_code, 403)
        with self.client.session_transaction() as session:
            import time
            session.update(_user_id='2', _fresh=True, _auth_session_id='fixture-2', _last_activity=int(time.time()), _csrf='ignore-csrf')
        with patch.object(self.webui, '_effective_digico_aux_ids_for_user', side_effect=RuntimeError('DB failed')):
            self.assertEqual(self.client.get('/api/digico/mixer/config').get_json()['auxes'], [])
        self.assertFalse(self.webui._execute_scheduler_internal_action({'path': '/api/digico/setup', 'method': 'POST', 'json': {}}))

    def test_failed_control_is_audited_without_success_or_replay(self):
        self.login(2)
        self.cfg['dlive_enabled'] = False
        self.webui.log_event.reset_mock()
        response = self.client.post('/api/digico/aux/2002/channel/1/level', headers=self.headers, json=dict(value=-12, final=True))
        self.assertEqual(response.status_code, 503)
        self.assertTrue(any(call.kwargs.get('status') == 'failure' for call in self.webui.log_event.call_args_list))
        self.assertFalse(any(call.kwargs.get('status') == 'success' for call in self.webui.log_event.call_args_list))
        self.assertFalse(self.desk.messages)

    def test_level_payload_validation_never_reaches_wire_for_non_numbers(self):
        self.login(2)
        for value in (True, None, {}, [], 'NaN', 'Infinity'):
            response = self.client.post('/api/digico/aux/2002/channel/1/level', headers=self.headers, json=dict(value=value))
            self.assertEqual(response.status_code, 400, (value, response.get_json()))
        self.assertFalse(any(m.startswith(bytes.fromhex('f0 00 00 1a 50 10 01 00 03 0d')) for m in self.desk.messages))

    def test_permission_catalog_only_lists_active_provider_not_saved_grants(self):
        self.cfg['digico_auxes'] = [dict(label='Old SD9 IEM', enabled=True)]
        self.webui._set_group_digico_allowed_auxes(2, ['1', '2009'])
        self.assertEqual({a['channel'] for a in self.webui._digico_aux_options()}, {2002})
        self.cfg['personal_mix_provider'] = 'digico'
        # Synthetic provider; no OSC transport is constructed for the catalog.
        with patch.object(self.webui, '_get_digico_client_from_config', return_value=None):
            self.assertEqual({a['channel'] for a in self.webui._digico_aux_options()}, {1})

    def test_group_autosave_preserves_inactive_grants_without_broadening_access(self):
        import json
        self.cfg['digico_auxes'] = [dict(label='Old SD9 IEM')]
        self.webui._set_group_digico_allowed_auxes(2, ['1'])
        response = self.client.post('/api/admin/groups/2', headers=self.headers,
            json={'page_keys': ['page:digico_mixer'], 'digico_allowed_auxes_role': []})
        self.assertEqual(response.status_code, 200, response.get_json())
        conn = self.webui._db()
        try:
            saved = json.loads(conn.execute('SELECT digico_allowed_auxes FROM groups WHERE id=2').fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(saved, ['1'])
        self.login(2)
        self.assertEqual(self.client.get('/api/digico/mixer/config').get_json()['auxes'], [])
        self.assertEqual(self.client.get('/api/digico/aux/2002/state').status_code, 403)

    def test_clearing_last_active_grant_is_none_until_explicit_all(self):
        self.webui._set_group_digico_allowed_auxes(2, ['2002'])
        for payload, expected in [
            ({'digico_allowed_auxes_role': []}, []),
            ({'digico_allowed_auxes_role': [], 'digico_aux_access_mode': 'all'}, [2002]),
        ]:
            self.login(1)
            response = self.client.post('/api/admin/groups/2', headers=self.headers,
                json={'page_keys': ['page:digico_mixer'], **payload})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.login(2)
            self.assertEqual([a['channel'] for a in self.client.get('/api/digico/mixer/config').get_json()['auxes']], expected)

    def test_form_save_preserves_hidden_scope_and_explicit_all_does_not_erase_old_desk(self):
        self.cfg['digico_auxes'] = [dict(label='SD9')]
        self.webui._set_group_digico_allowed_auxes(2, ['1'])
        response = self.client.post('/admin/permissions', headers=self.headers, data={
            '_csrf': 'ignore-csrf', 'action': 'save_group', 'group_id': '2',
            'page_keys': ['page:digico_mixer'], 'digico_aux_access_mode': 'selected'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.webui._group_settings_snapshot(2)['digico_allowed_auxes'], ['1'])
        self.login(2)
        for prefix in ['/api/digico', '/api/v1/digico']:
            self.assertEqual(self.client.get(prefix + '/aux/2002/state').status_code, 403)
            for suffix in ['/channel/1/level', '/master/level', '/master/mute']:
                response = self.client.post(prefix + '/aux/2002' + suffix, headers=self.headers, json={'value': -12})
                self.assertEqual(response.status_code, 403)
        self.login(1)
        response = self.client.post('/api/admin/groups/2', headers=self.headers, json={
            'page_keys': ['page:digico_mixer'], 'digico_allowed_auxes_role': [], 'digico_aux_access_mode': 'all'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('1', self.webui._group_settings_snapshot(2)['digico_allowed_auxes'])
        self.login(2)
        self.assertEqual([a['channel'] for a in self.client.get('/api/digico/mixer/config').get_json()['auxes']], [2002])
        self.cfg['personal_mix_provider'] = 'digico'
        self.assertEqual(self.webui._effective_digico_aux_ids_for_user(2), ['1', '__all_dlive__'])

    def test_provider_switch_filters_admin_and_union_scopes_and_denies_inactive_routes(self):
        self.cfg.update(digico_enabled=False, digico_auxes=[dict(label='SD9 IEM', enabled=True)], digico_channels=[dict(enabled=True)])
        self.webui._set_group_digico_allowed_auxes(2, ['1', '2002'])
        for uid in [1, 2]:
            self.login(uid)
            self.assertEqual([a['channel'] for a in self.client.get('/api/digico/mixer/config').get_json()['auxes']], [2002])
            self.assertEqual(self.client.get('/api/digico/aux/1/state').status_code, 404)
            self.assertEqual(self.client.post('/api/digico/aux/1/channel/1/level', headers=self.headers, json={'value': -12}).status_code, 404)
        self.cfg['personal_mix_provider'] = 'digico'
        # A synthetic discovered DiGiCo desk; disabled transport has no inventory.
        from types import SimpleNamespace
        sd9 = SimpleNamespace(mixer_config=lambda: {'auxes': [dict(channel=1, enabled=True)], 'channels': []},
                              status=lambda: {}, route_enabled=lambda aux, channel=None: aux == 1)
        with patch.object(self.webui, '_get_digico_client_from_config', return_value=sd9):
            for uid in [1, 2]:
                self.login(uid)
                self.assertEqual([a['channel'] for a in self.client.get('/api/digico/mixer/config').get_json()['auxes']], [1])
                self.assertEqual(self.client.get('/api/v1/digico/aux/2002/state').status_code, 404)
                self.assertEqual(self.client.post('/api/v1/digico/aux/2002/channel/1/level', headers=self.headers, json={'value': -12}).status_code, 404)
        self.cfg['personal_mix_provider'] = 'dlive'
        self.cfg['dlive_auxes'].append(dict(channel=1001, bus=1, stereo=False, enabled=True))
        self.webui._set_group_pages(3, ['page:digico_mixer'])
        self.webui._set_group_digico_allowed_auxes(2, ['1'])
        self.webui._set_group_digico_allowed_auxes(3, ['2002'])
        conn = self.webui._db()
        conn.execute('INSERT INTO user_groups(user_id,group_id) VALUES(2,3)')
        conn.commit(); conn.close()
        self.login(2)
        self.assertEqual([a['channel'] for a in self.client.get('/api/digico/mixer/config').get_json()['auxes']], [2002])
        self.webui._set_group_digico_allowed_auxes(3, [])
        self.assertEqual({a['channel'] for a in self.client.get('/api/digico/mixer/config').get_json()['auxes']}, {1001, 2002})

    def test_legacy_global_all_and_omitted_grants_survive_unrelated_autosave(self):
        self.webui._set_group_digico_allowed_auxes(2, [])
        response = self.client.post('/api/admin/groups/2', headers=self.headers, json={
            'page_keys': ['page:digico_mixer'], 'digico_allowed_auxes_role': [],
            'digico_aux_access_mode': 'all', 'digico_aux_catalog_provider': 'dlive'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.webui._group_settings_snapshot(2)['digico_allowed_auxes'], [])
        self.webui._set_group_digico_allowed_auxes(2, ['1', '2002', '2009'])
        response = self.client.post('/api/admin/groups/2', headers=self.headers,
            json={'page_keys': ['page:digico_mixer'], 'auth_idle_timeout_minutes_override_role': 30})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.webui._group_settings_snapshot(2)['digico_allowed_auxes'], ['1', '2002', '2009'])

    def test_setup_metadata_readback_retains_namespaced_ids_and_manual_ordinals(self):
        auxes = [dict(channel=1001, bus=1, stereo=False, enabled=False, order=2, label='Mono', group='Band', colour='#123456', icon='drums'),
                 dict(channel=2002, bus=2, stereo=True, enabled=True, order=1, label='Stereo', group='Vocals', colour='#cc4422', icon='headset')]
        channels = [dict(channel=3, order=1, label='Guitar', group='Band', icon='electric'), dict(channel=1, order=2, label='Lead', group='Vocals')]
        response = self.client.post('/api/digico/setup', headers=self.headers, json={'dlive_auxes': auxes, 'dlive_channels': channels})
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = self.client.get('/api/digico/setup').get_json()['config']
        self.assertEqual(saved['dlive_auxes'], auxes)
        self.assertEqual([c['channel'] for c in saved['dlive_channels']], [3, 1])
        visible = self.client.get('/api/digico/mixer/config').get_json()
        self.assertEqual([a['channel'] for a in visible['auxes']], [2002])
        self.assertEqual(visible['auxes'][0]['colour'], '#cc4422')
        self.assertEqual([c['channel'] for c in visible['channels']], [3, 1])
        self.assertEqual(self.client.get('/api/digico/aux/1001/state').status_code, 404)
        self.assertEqual(self.client.post('/api/digico/aux/1001/master/level', headers=self.headers, json={'value': -12}).status_code, 404)

    def test_stale_provider_permission_save_is_rejected_before_any_group_changes(self):
        self.webui._set_group_digico_allowed_auxes(2, ['1'])
        response = self.client.post('/api/admin/groups/2', headers=self.headers, json={
            'page_keys': ['page:home', 'page:digico_mixer'], 'digico_allowed_auxes_role': [],
            'digico_aux_access_mode': 'all', 'digico_aux_catalog_provider': 'digico'})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(self.webui._group_settings_snapshot(2)['digico_allowed_auxes'], ['1'])
        self.assertNotIn('page:home', self.webui._group_settings_snapshot(2)['page_keys'])
        response = self.client.post('/admin/permissions', headers=self.headers, data={
            '_csrf': 'ignore-csrf', 'action': 'save_group', 'group_id': '2',
            'page_keys': ['page:home', 'page:digico_mixer'], 'digico_aux_access_mode': 'all',
            'digico_aux_catalog_provider': 'digico'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.webui._group_settings_snapshot(2)['digico_allowed_auxes'], ['1'])
        self.assertNotIn('page:home', self.webui._group_settings_snapshot(2)['page_keys'])

    def test_generic_editor_hides_protected_provider_fields(self):
        from pathlib import Path
        script = (Path(__file__).resolve().parents[1] / 'static' / 'app.js').read_text()
        hidden = script.split('const hiddenKeys = new Set([', 1)[1].split(']);', 1)[0]
        self.assertIn("'personal_mix_provider'", hidden)
        self.assertIn("'dlive_auxes'", hidden)

    def test_validated_setup_and_generic_config_cannot_silently_retarget(self):
        response = self.client.post('/api/digico/setup', headers=self.headers,
                                    json=dict(personal_mix_provider='dlive', dlive_midi_channel=13))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.cfg['dlive_midi_channel'], 4)
        response = self.client.post('/api/digico/setup', headers=self.headers, json=dict(dlive_tls=True))
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/config', headers=self.headers,
                                    json=dict(personal_mix_provider='digico', dlive_ip='192.0.2.4'))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.cfg['personal_mix_provider'], 'dlive')
        self.assertEqual(self.cfg['dlive_ip'], '127.0.0.1')
        response = self.client.get('/api/digico/setup')
        self.assertEqual(response.get_json()['config']['personal_mix_provider'], 'dlive')
