"""Mapping validation through setup, generic saves and config imports."""
import copy
import json
import unittest
import zipfile
from unittest.mock import patch
from tv_fixture import TVFixture


class TvConfigTests(unittest.TestCase):
    def setUp(self):
        self.fx = TVFixture().__enter__()
        self.addCleanup(self.fx.__exit__, None, None, None)
        self.w = self.fx.w

    def test_generic_config_cannot_revert_dedicated_mapping(self):
        admin = self.fx.client('admin-tv')
        response = admin.post('/api/config', json={'hisense_media_mappings': {'foyer': {'output': 99, 'source': 'HDMI2'}}}, headers=self.fx.headers(admin))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fx.cfg['hisense_media_mappings'], {'foyer': {'output': 1, 'source': 'HDMI1'}})

    def import_config(self, cfg):
        target = self.fx.root / 'imported.json'
        target.write_text('{"unchanged":true}')
        archive = self.fx.root / 'import.zip'
        item = {'id': 'config', 'path': 'config.json', 'kind': 'file', 'label': 'Config'}
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('payload/config.json', json.dumps(cfg))
        with patch.object(self.w, '_inspect_config_transport_zip', return_value={'items': [item]}), patch.object(self.w, '_config_transport_item_map', return_value={'config': {**item, '_actual_path': str(target)}}), patch.object(self.w, '_create_pre_import_backup'), patch.object(self.w, '_clear_imported_config_caches'):
            self.w._apply_config_transport_import(archive, ['config'])
        return json.loads(target.read_text())

    def test_import_validates_and_persists_canonical_mappings(self):
        cfg = copy.deepcopy(self.fx.cfg)
        cfg['hisense_media_mappings']['foyer']['source'] = ' HDMI1 '
        self.assertEqual(self.import_config(cfg)['hisense_media_mappings']['foyer']['source'], 'HDMI1')
        cfg.pop('hisense_media_mappings')
        self.assertEqual(self.import_config(cfg)['hisense_media_mappings'], {})

    def test_import_preserves_tv_mapping_validation_and_retires_old_timer_settings(self):
        cfg = copy.deepcopy(self.fx.cfg)
        cfg['hisense_media_mappings']['foyer']['source'] = ' HDMI1 '
        for key in self.w.utils.RETIRED_TIMER_CONFIG_KEYS:
            cfg[key] = True
        restored = self.import_config(cfg)
        self.assertEqual(restored['hisense_media_mappings']['foyer'], {'output': 1, 'source': 'HDMI1'})
        for key in self.w.utils.RETIRED_TIMER_CONFIG_KEYS:
            self.assertNotIn(key, restored)

    def test_invalid_mapping_import_fails_before_config_replacement(self):
        for invalid in (None, [], {'foyer': None}, {'foyer': {'output': True, 'source': 'HDMI1'}}, {'private-absent': {'output': 1, 'source': 'HDMI1'}}, {'foyer': {'output': 1, 'source': '   '}}):
            with self.subTest(mapping=invalid):
                cfg = copy.deepcopy(self.fx.cfg); cfg['hisense_media_mappings'] = invalid
                with self.assertRaises(ValueError):
                    self.import_config(cfg)
                self.assertEqual(json.loads((self.fx.root / 'imported.json').read_text()), {'unchanged': True})

    def test_corrupt_stored_mapping_is_unmapped_not_server_error(self):
        self.fx.cfg['hisense_media_mappings'] = None
        response = self.fx.client().get('/api/tv-pages/state')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json['tvs'][0]['mediaMapping'])

    def test_deleted_tv_mapping_does_not_disable_another_tvs_valid_mapping(self):
        self.fx.cfg['hisense_media_mappings']['removed-tv'] = {'output': 2, 'source': 'HDMI1'}
        response = self.fx.client().get('/api/tv-pages/state')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['tvs'][0]['mediaMapping'], {'output': 1, 'source': 'HDMI1'})

    def test_corrupt_mapping_remains_repairable_in_setup(self):
        self.fx.cfg['hisense_media_mappings'] = None
        response = self.fx.client('admin-tv').get('/api/config/tv-media-mappings')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['mappings'], {})
        self.assertTrue(response.json['warning'])
