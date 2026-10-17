import unittest
from unittest.mock import patch, Mock
import test_videohub_ignore as fixture


class AutosaveApiTests(fixture.VideoHubIgnoreApiTests):
    def test_new_duplicate_and_compare_before_write(self):
        response = self.client.post('/api/videohub/presets/new', headers=self.headers,
                                    json={'name': 'Blank', 'mode': 'keep'})
        self.assertEqual(response.status_code, 201, response.get_json())
        original = response.get_json()['preset']
        self.assertEqual(len(original['routes']), 40)
        self.assertTrue(all(r['input'] is None for r in original['routes']))
        path = '/api/videohub/presets/' + str(original['id'])
        changed = self.client.put(path, headers=self.headers, json={'name': 'Renamed', 'routes': original['routes'], 'expected': original})
        self.assertEqual(changed.status_code, 200, changed.get_json())
        stale = self.client.put(path, headers=self.headers, json={'name': 'Lost', 'routes': [], 'expected': original})
        self.assertEqual(stale.status_code, 409, stale.get_json())
        copy = self.client.post(path + '/duplicate', headers=self.headers).get_json()['preset']
        self.assertEqual(copy['name'], 'Renamed (Copy)')
        self.assertNotEqual(copy['id'], original['id'])
        self.assertEqual(copy['routes'], original['routes'])
        self.assertFalse(copy['locked'])
        self.assertEqual(self.hub.writes, [])

    def test_actual_hex_catalog_reference_and_fixed_output_do_not_reject_save(self):
        # RoutingPresetStore creates hex32 IDs, not hyphenated UUIDs.
        identity = 'a' * 32
        store = Mock()
        for destination in (1, 9):
            store.get.return_value = {'id': identity, 'name': 'Welcome', 'enabled': True,
                                      'media_id': 'b' * 32, 'output': destination, 'actions': []}
            with patch.object(self.webui, '_get_routing_preset_store', return_value=store):
                saved = self.create([{'output': 1, 'routing_preset': identity}])
                self.assertEqual(saved['routes'], [{'output': 1, 'routing_preset': identity}])
        self.assertEqual(self.hub.writes, [])

    def test_new_modes_use_real_smaller_counts_and_preserve_snapshot(self):
        self.hub.get_routing_state_strict = Mock(return_value={'input_count': 8, 'output_count': 4, 'routing': [8, 7, 6, 5]})
        for mode, expected in (('keep', [None]*4), ('identity', [1, 2, 3, 4]), ('device', [8, 7, 6, 5])):
            response = self.client.post('/api/videohub/presets/new', headers=self.headers, json={'name': mode, 'mode': mode})
            self.assertEqual(response.status_code, 201, response.get_json())
            self.assertEqual([r['input'] for r in response.get_json()['preset']['routes']], expected)
        self.assertEqual(self.hub.writes, [])

    def test_restricted_editor_creation_denied_before_storage_changes(self):
        original = self.create([{'output': 1, 'input': None}])
        self.webui._set_group_videohub_can_edit_presets(2, True)
        self.webui._set_group_videohub_allowed_preset_ids(2, str(original['id']))
        self.login(2)
        before = (self.root / 'presets.json').read_bytes()
        for path, body in (('/api/videohub/presets/new', {'name': 'Hidden', 'mode': 'keep'}),
                           ('/api/videohub/presets/' + str(original['id']) + '/duplicate', {})):
            response = self.client.post(path, headers=self.headers, json=body)
            self.assertEqual(response.status_code, 403, response.get_json())
            self.assertEqual((self.root / 'presets.json').read_bytes(), before)

    def test_snapshot_failure_never_saves_identity(self):
        self.hub.get_routing_state_strict = Mock(side_effect=OSError('device unavailable'))
        response = self.client.post('/api/videohub/presets/new', headers=self.headers,
                                    json={'name': 'Snapshot', 'mode': 'device'})
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(self.backend.list_presets(self.cfg), [])


for name in vars(fixture.VideoHubIgnoreApiTests):
    if name.startswith('test_'):
        setattr(AutosaveApiTests, name, None)
