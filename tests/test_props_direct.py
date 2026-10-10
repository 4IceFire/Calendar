"""Direct-only Props contract and safe legacy storage migration."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from props import PropsStore

PROP = '4CDDF2C5-7355-4347-980C-B13F12CBFA85'
ID = PROP.lower()
MACRO = '203878e3-0fd0-4e47-9dc6-fdeaf5185368'


class DirectContractTests(unittest.TestCase):
    def setUp(self):
        from test_props import PropsWebTests
        self.fixture = PropsWebTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_discovery_and_trigger_have_no_dependency_on_macro_catalog(self):
        f = self.fixture
        paths = []
        def catalog(kind):
            paths.append(kind)
            if kind != 'props':
                raise RuntimeError('Retired API unavailable')
            return [{'id': {'uuid': PROP.lower(), 'name': 'Original'}}]
        f.pp.get_json.side_effect = catalog
        self.assertTrue(f.service.catalog.refresh_now())
        self.assertEqual(paths, ['props'])
        self.assertNotIn('macros', f.service.state()['catalog'])
        data = f.store.save_library([{'id': ID, 'name': 'Welcome', 'prop_uuid': PROP}],
                                   {'folders': [], 'items': [{'id': ID}]}, 'initial')
        data = f.store.save_order([ID], data['revision'])
        f.cfg['propresenter_props_use_macros'] = True
        snapshot = f.client.get('/api/v1/props').get_json()
        self.assertIs(snapshot['use_macros'], False)
        payload = {'position': 1, 'revision': data['revision']}
        for extra in ({}, {'use_macros': False}):
            result = f.client.post('/api/v1/props/trigger', json={**payload, **extra})
            self.assertEqual(result.status_code, 200, result.data)
            self.assertEqual(result.get_json()['last_triggered']['mode'], 'prop')
        for invalid in (True, None, 0, 'false', [], {}):
            result = f.client.post('/api/props/trigger', json={**payload, 'use_macros': invalid})
            self.assertEqual(result.status_code, 400, result.data)
        for invalid in ({}, {'position': 1}, {'revision': data['revision']}, {**payload, 'mode': 'prop'}):
            self.assertEqual(f.client.post('/api/props/trigger', json=invalid).status_code, 400)
        self.assertEqual(f.client.post('/api/props/trigger', json={**payload, 'revision': 'stale'}).status_code, 409)
        self.assertEqual(f.pp.get_command.call_count, 2)
        self.assertEqual([call.args[0] for call in f.pp.get_command.call_args_list], ['prop/' + PROP + '/trigger'] * 2)
        import inspect
        self.assertNotIn('use_macros', inspect.signature(f.service.trigger).parameters)
        self.assertNotIn('validate_mode', inspect.signature(f.service.trigger).parameters)

    def test_retired_settings_are_absent_and_legacy_config_is_stripped_without_timer_changes(self):
        f = self.fixture
        from package.apps.calendar import utils
        from unittest.mock import patch
        self.assertNotIn('propresenter_props_use_macros', utils._defaults)
        for rule in f.webui.app.url_map.iter_rules():
            self.assertNotIn('/props/settings', rule.rule)
        before = f.store.read()
        for path in ('/api/props/settings', '/api/v1/props/settings'):
            result = f.client.put(path, json={'revision': 'initial', 'previous_use_macros': False, 'use_macros': True})
            self.assertEqual(result.status_code, 404)
        self.assertEqual(f.store.read(), before)
        f.cfg.update(propresenter_props_use_macros=True, propresenter_is_latest=False)
        captured = []
        with patch.object(f.webui.utils, 'save_config', side_effect=lambda cfg: captured.append(copy.deepcopy(cfg))), patch.object(f.webui.utils, 'reload_config'):
            result = f.client.post('/api/config', json={'propresenter_props_use_macros': True, 'debug': True})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertNotIn('propresenter_props_use_macros', captured[0])
        self.assertIs(captured[0]['propresenter_is_latest'], False)
        self.assertTrue(captured[0]['debug'])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.json'
            source = {'propresenter_props_use_macros': True, 'propresenter_is_latest': False}
            utils.save_config(source, str(path))
            saved = json.loads(path.read_text())
            self.assertNotIn('propresenter_props_use_macros', saved)
            self.assertIs(saved['propresenter_is_latest'], False)
            self.assertIn('propresenter_props_use_macros', source)
        f.pp.get_command.assert_not_called()

    def test_legacy_read_is_nondestructive_and_deliberate_save_migrates_only_targets(self):
        with tempfile.TemporaryDirectory() as root:
            store = PropsStore(Path(root) / 'props.json')
            legacy = {'version': 1, 'revision': 'initial',
                      'library': [{'id': ID, 'name': ' Welcome ', 'prop_uuid': PROP, 'macro_uuid': MACRO}],
                      'order': [ID, ID], 'organization': {'folders': [{'id': 'service', 'name': 'Service', 'order': 0}],
                          'items': [{'id': ID, 'folderId': 'service', 'order': 0}]}}
            raw = json.dumps(legacy).encode()
            store.path.write_bytes(raw)
            before = copy.deepcopy(legacy)
            read = store.read()
            self.assertEqual(read['version'], 2)
            self.assertEqual(read['library'], [{key: value for key, value in legacy['library'][0].items() if key != 'macro_uuid'}])
            self.assertEqual(store.path.read_bytes(), raw)
            saved = store.save_order(read['order'], read['revision'])
            persisted = json.loads(store.path.read_text())
            self.assertEqual(saved, persisted)
            self.assertNotEqual(saved['revision'], legacy['revision'])
            for key in ('order', 'organization'):
                self.assertEqual(saved[key], legacy[key])
            self.assertEqual(saved['library'], read['library'])
            self.assertEqual(legacy, before)
            # Actual HTTP reads and explicit commands must not rewrite legacy storage.
            f = self.fixture
            f.store.path.write_bytes(raw)
            response = f.client.get('/api/v1/props')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.get_json()['version'], 2)
            self.assertEqual(response.get_json()['order'], legacy['order'])
            self.assertEqual(f.store.path.read_bytes(), raw)
            response = f.client.post('/api/props/trigger', json={'position': 2, 'revision': 'initial'})
            self.assertEqual(response.status_code, 200, response.data)
            f.pp.get_command.assert_called_once_with('prop/' + PROP + '/trigger')
            self.assertEqual(f.store.path.read_bytes(), raw)
            f.pp.get_command.reset_mock()
            response = f.client.put('/api/props/library', json={'revision': 'initial',
                'library': legacy['library'], 'organization': legacy['organization']})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(f.store.path.read_bytes(), raw)
            response = f.client.put('/api/props/library', json={'revision': 'initial',
                'library': read['library'], 'organization': read['organization']})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(f.store.read()['library'], read['library'])
            self.assertEqual(f.store.read()['order'], legacy['order'])
            self.assertEqual(f.store.read()['organization'], legacy['organization'])
            self.assertEqual(f.store.read()['version'], 2)
            f.pp.get_command.assert_not_called()
            imported = store.import_document(legacy)
            self.assertEqual(imported['version'], 2)
            self.assertEqual(imported['library'], saved['library'])
            self.assertEqual(imported['order'], saved['order'])
            self.assertEqual(imported['organization'], saved['organization'])
            library_saved = store.save_library(read['library'], read['organization'], imported['revision'])
            self.assertEqual(library_saved['library'], read['library'])
            invalid = copy.deepcopy(legacy)
            invalid['library'][0]['macro_uuid'] = 'broken'
            last = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.import_document(invalid)
            self.assertEqual(store.path.read_bytes(), last)
            store.path.write_text(json.dumps(invalid))
            corrupt = store.path.read_bytes()
            with self.assertRaises(OSError):
                store.read()
            self.assertEqual(store.path.read_bytes(), corrupt)
