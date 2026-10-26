"""Real-auth APIs backed by actual dLive TCP simulator, no hardware."""
import copy
from unittest.mock import patch
from test_dlive import Desk, config, wait_for
import unittest
import test_videohub_ignore


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

    def test_permission_catalog_keeps_inactive_provider_grants_visible(self):
        self.cfg['digico_auxes'] = [dict(label='Old SD9 IEM', enabled=True)]
        options = self.webui._digico_aux_options()
        self.assertEqual({a['channel'] for a in options}, {1, 2002})
        self.assertTrue(any('SD9' in a['label'] for a in options if a['channel'] == 1))
        self.webui._set_group_digico_allowed_auxes(2, ['2009'])
        options = self.webui._digico_aux_options()
        missing = next((a for a in options if str(a['channel']) == '2009'), None)
        self.assertIsNotNone(missing, 'Lost grants must not become an empty/all selection on unrelated group save')
        self.assertIn('Unavailable', missing['label'])

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
