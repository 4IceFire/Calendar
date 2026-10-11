"""VideoHub Ignore routes use explicit null, never a live-input snapshot."""
import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from package.apps.videohub.app import VideohubApp


class FakeVideoHub:
    def __init__(self):
        self.routing = list(range(1, 41))
        self.writes = []
        self.reads = 0
        self.fail_readback = False

    def route_video_outputs(self, *, routes, monitoring=False):
        self.writes.append({'routes': list(routes), 'monitoring': monitoring})
        if not monitoring and not self.fail_readback:
            for output, input_ in routes:
                self.routing[output] = input_ + 1

    def get_state(self, *, fallback_count=40):
        self.reads += 1
        return self.snapshot()

    def snapshot(self):
        return {'ok': True, 'configured': True, 'refreshing': False,
                'inputs': [{'number': n, 'label': f'Input {n}'} for n in range(1, 41)],
                'outputs': [{'number': n, 'label': f'Output {n}'} for n in range(1, 41)],
                'routing': self.routing[:]}


class VideoHubIgnoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'presets.json'
        self.cfg = {'videohub_presets_file': str(self.path)}
        self.app = VideohubApp()

    def test_explicit_ignore_round_trips_without_changing_legacy_routes(self):
        routes = [{'output': 1, 'input': None}, {'output': 2, 'input': 7},
                  {'output': 3, 'input': None, 'monitoring': True}]
        created = self.app.upsert_preset(self.cfg, {'name': 'Mixed', 'routes': routes})
        self.assertEqual(created.to_dict()['routes'], routes)
        self.assertEqual(json.loads(self.path.read_text())['presets'][0]['routes'], routes)
        self.assertEqual(VideohubApp().get_preset(self.cfg, created.id).to_dict()['routes'], routes)
        # A new path forces disk deserialization instead of reusing JSON cache.
        disk_path = self.path.with_name('disk-copy.json')
        disk_path.write_bytes(self.path.read_bytes())
        self.assertEqual(VideohubApp().list_presets({'videohub_presets_file': str(disk_path)})[0]['routes'], routes)
        # Missing inputs and nonpositive values retain the existing invalid-route behavior.
        invalid = self.app.upsert_preset(self.cfg, {'name': 'Invalid', 'routes': [
            {'output': 1}, {'output': 2, 'input': 0}, {'output': 3, 'input': -1},
            {'output': 0, 'input': None}, {'output': 4, 'input': 'ignore'}]})
        self.assertEqual(invalid.routes, [])

    def test_mixed_apply_skips_ignore_in_both_tables_and_verifies_only_writes(self):
        from unittest.mock import Mock
        routes = [{'output': 1, 'input': None}, {'output': 2, 'input': 7},
                  {'output': 3, 'input': None, 'monitoring': True},
                  {'output': 4, 'input': 8, 'monitoring': True}]
        preset = self.app.upsert_preset(self.cfg, {'name': 'Mixed', 'routes': routes})
        device = Mock()
        device.get_state.return_value = {'routing': [12, 7, 13, 4]}
        with patch('package.apps.videohub.app.get_videohub_client_from_config', return_value=device):
            result = self.app.apply_preset(self.cfg, preset.id)
        self.assertTrue(result['applied'])
        self.assertEqual(result['routes'], 2)
        self.assertEqual(result['ignored'], 2)
        self.assertEqual(device.route_video_outputs.call_args_list, [
            unittest.mock.call(routes=[(1, 6)], monitoring=False),
            unittest.mock.call(routes=[(3, 7)], monitoring=True)])
        device.get_state.assert_called_once_with(fallback_count=40)
        self.assertEqual(self.app.get_preset(self.cfg, preset.id).to_dict()['routes'], routes)


    def test_all_ignore_is_successful_without_constructing_device(self):
        preset = self.app.upsert_preset(self.cfg, {'name': 'No-op', 'routes': [
            {'output': 1, 'input': None}, {'output': 2, 'input': None, 'monitoring': True}]})
        with patch('package.apps.videohub.app.get_videohub_client_from_config', return_value=None) as factory:
            result = self.app.apply_preset(self.cfg, preset.id)
        self.assertEqual(result, {'id': preset.id, 'name': 'No-op', 'routes': 0, 'ignored': 2, 'applied': True})
        factory.assert_not_called()


class VideoHubIgnoreApiTests(unittest.TestCase):
    def setUp(self):
        with patch.object(threading.Thread, 'start'):
            import webui
        self.webui = webui
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {'auth_enabled': True, 'auth_idle_timeout_enabled': False,
                    'api_legacy_anonymous_enabled': False, 'api_write_rate_limit_per_minute': 600,
                    'videohub_presets_file': str(self.root / 'presets.json')}
        self.backend = VideohubApp()
        self.hub = FakeVideoHub()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        rooms = {'rooms': [{'id': 'fixture', 'name': 'Fixture room'}], 'filtered_inputs': [7],
                 'output_layouts': {'1': {'room_id': 'fixture', 'x': 25, 'y': 50},
                                    '2': {'room_id': 'fixture', 'x': 75, 'y': 50}}}
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', self.root / 'auth.db'),
            patch.object(webui, '_auth_cfg', return_value=self.cfg),
            patch.object(webui.utils, 'get_config', return_value=self.cfg),
            patch.object(webui, '_bootstrap_default_users_roles'),
            patch.object(webui, '_touch_current_user_session', return_value=True),
            patch.object(webui, '_get_videohub_app', return_value=self.backend),
            patch('package.apps.videohub.app.get_videohub_client_from_config', return_value=self.hub),
            patch.object(webui, '_get_videohub_client_from_config', return_value=self.hub),
            patch.object(webui, '_get_videohub_state_snapshot', side_effect=lambda **kwargs: self.hub.snapshot()),
            patch.object(webui, '_load_videohub_rooms_config', return_value=rooms),
            patch.object(webui, '_invalidate_videohub_state_snapshot'),
            patch.object(webui, '_home_set_last_videohub_preset'),
        ):
            self.stack.enter_context(context)
        self.log = self.stack.enter_context(patch.object(webui, 'log_event'))
        webui._init_auth_db()
        conn = webui._db()
        conn.executemany('INSERT INTO groups(id,name,is_admin) VALUES (?,?,?)',
                         [(1, 'Admin', 1), (2, 'Operators', 0), (3, 'Denied', 0)])
        conn.executemany('INSERT INTO users(id,username,password_hash) VALUES (?,?,?)',
                         [(1, 'fixture-admin', 'unused'), (2, 'operator', 'unused'), (3, 'denied', 'unused')])
        conn.executemany('INSERT INTO user_groups(user_id,group_id) VALUES (?,?)', [(1, 1), (2, 2), (3, 3)])
        conn.commit(); conn.close()
        webui._set_group_pages(2, ['page:videohub'])
        webui._set_group_videohub_can_edit_presets(2, False)
        self.client = webui.app.test_client()
        self.login(1)
        self.headers = {'Origin': 'http://localhost', 'X-CSRF-Token': 'ignore-csrf'}
        with webui._api_rate_lock:
            webui._api_rate_events.clear()

    def login(self, uid):
        with self.client.session_transaction() as state:
            state.clear()
            state.update(_user_id=str(uid), _fresh=True, _auth_session_id=f'fixture-{uid}',
                         _last_activity=int(time.time()), _csrf='ignore-csrf')

    def create(self, routes):
        response = self.client.post('/api/videohub/presets', headers=self.headers,
                                    json={'name': 'Mixed', 'routes': routes})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['preset']

    def test_browser_token_companion_alias_and_scheduler_share_ignore_backend(self):
        import api_security
        routes = [{'output': 1, 'input': None}, {'output': 2, 'input': 7}]
        preset = self.create(routes)
        path = f"/api/videohub/presets/{preset['id']}/apply"
        response = self.client.post(path, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        token = api_security.create_service_token(self.webui._AUTH_DB_PATH, name='Companion fixture',
                                                  scopes=['videohub'], constraints={'videohub_presets': [preset['id']]})
        self.client = self.webui.app.test_client()
        response = self.client.post(path.replace('/api/', '/api/v1/'),
                                    headers={'Authorization': 'Bearer ' + token['token']})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.headers['X-TDeck-API-Version'], '1')
        self.assertTrue(self.webui._execute_scheduler_internal_action({'method': 'POST', 'path': path}))
        self.assertEqual(self.hub.writes, [{'routes': [(1, 6)], 'monitoring': False}] * 3)
        self.assertEqual(self.hub.reads, 3)
        self.assertEqual(self.hub.routing[0], 1)
        applied_logs = [c for c in self.log.call_args_list if c.args[0] == 'videohub.preset.apply']
        self.assertEqual(len(applied_logs), 3)
        self.assertTrue(all(c.kwargs['status'] == 'success' and c.kwargs['details']['result']['ignored'] == 1
                            for c in applied_logs))

    def test_ignore_preserves_edit_lock_folder_ids_and_authorization(self):
        routes = [{'output': 1, 'input': None}, {'output': 2, 'input': 7}]
        preset = self.create(routes)
        pid = preset['id']
        tree = {'folders': [{'id': 'service', 'name': 'Service'}],
                'items': [{'id': str(pid), 'folderId': 'service'}]}
        tree = self.backend.save_organization(self.cfg, tree)
        path = f'/api/videohub/presets/{pid}'
        edited = self.client.put(path, headers=self.headers, json={'name': 'Renamed', 'routes': routes})
        self.assertEqual(edited.get_json()['preset']['id'], pid)
        self.assertEqual(edited.get_json()['preset']['routes'], routes)
        self.assertEqual(self.backend.get_organization(self.cfg), tree)
        self.backend.set_preset_locked(self.cfg, pid, True)
        self.assertEqual(self.client.put(path, headers=self.headers, json={'routes': routes}).status_code, 400)
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(path + '/apply', headers=self.headers).status_code, 200)
        self.login(2)
        self.assertEqual(self.client.put(path, headers=self.headers, json={'routes': routes}).status_code, 403)
        self.assertEqual(self.client.post('/api/videohub/presets', headers=self.headers, json={'routes': routes}).status_code, 403)
        self.assertEqual(self.client.post(path + '/apply').status_code, 403)
        self.assertEqual(self.client.post(path + '/apply', headers=self.headers).status_code, 200)
        self.webui._set_group_videohub_allowed_preset_ids(2, [pid + 1])
        self.assertEqual(self.client.post(path + '/apply', headers=self.headers).status_code, 403)
        self.login(3)
        self.assertEqual(self.client.post(path + '/apply', headers=self.headers).status_code, 403)
        self.assertEqual(self.backend.get_preset(self.cfg, pid).to_dict()['routes'], routes)
        self.assertEqual(self.backend.get_organization(self.cfg), tree)

    def test_all_ignore_audited_noop_and_mixed_readback_failure(self):
        preset = self.create([{'output': 1, 'input': None}, {'output': 2, 'input': None}])
        path = f"/api/videohub/presets/{preset['id']}/apply"
        response = self.client.post(path, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['result']['routes'], 0)
        self.assertEqual(self.hub.writes, [])
        self.assertEqual(self.hub.reads, 0)
        self.assertEqual(self.log.call_args.kwargs['status'], 'success')
        self.client.put(path.removesuffix('/apply'), headers=self.headers,
                        json={'name': 'Mixed', 'routes': [{'output': 1, 'input': None}, {'output': 2, 'input': 7}]})
        self.hub.fail_readback = True
        response = self.client.post(path, headers=self.headers)
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn('did not confirm', response.get_json()['error'])
        self.assertEqual(self.log.call_args.kwargs['status'], 'failure')

    def test_ignore_presets_retain_nonblocking_write_reservation(self):
        from media_routing import video_routing_guard
        for routes in ([{'output': 1, 'input': None}],
                       [{'output': 1, 'input': None}, {'output': 2, 'input': 7}]):
            preset = self.create(routes)
            with video_routing_guard():
                response = self.client.post(f"/api/videohub/presets/{preset['id']}/apply", headers=self.headers)
            self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(self.hub.writes, [])
        self.assertEqual(self.hub.reads, 0)


if __name__ == '__main__':
    unittest.main()
