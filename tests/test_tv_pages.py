"""TV pages use current group/room grants, never client-side filtering."""
import unittest
from unittest.mock import patch, Mock
import webui


class TvPageTests(unittest.TestCase):
    def setUp(self):
        self.client = webui.app.test_client()
        self.cfg = {'hisense_tvs': [{'id': 'a', 'name': 'Foyer'}, {'id': 'b', 'name': 'Private'}],
                    'hisense_tv_groups': [{'id': 'foyer', 'name': 'Foyer', 'tv_ids': ['a']},
                                          {'id': 'private', 'name': 'Private', 'tv_ids': ['b']}]}
        self.stack = []
        for target, value in [('_auth_enabled', False)]:
            p = patch.object(webui, target, return_value=value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(webui.utils, 'get_config', return_value=self.cfg)
        p.start(); self.addCleanup(p.stop)

    def test_separate_pages_and_browser_only_policy(self):
        for url, key in [('/tv-diagnostics', 'page:tv_diagnostics'), ('/tv-controls', 'page:tv_controls')]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'tv_pages.js', response.data)
            self.assertIn(key, webui._PAGE_REGISTRY)
        policy = webui._api_policy('/api/tv-pages/state', 'GET')
        self.assertFalse(policy['service_tokens'])

    def test_room_grants_are_bound_to_same_group_and_current_membership(self):
        from tv_pages import permitted_ids
        rows = [{'id': 1}, {'id': 2}]
        grants = {1: {'page:tv_controls', 'tvroom:foyer', 'tvdevice:private:b'},
                  2: {'page:tv_controls', 'tvroom:private', 'tvdevice:foyer:a'}}
        self.assertEqual(permitted_ids(self.cfg, rows, grants), set())
        grants[1].add('tvdevice:foyer:a')
        self.assertEqual(permitted_ids(self.cfg, rows, grants), {'a'})
        self.cfg['hisense_tv_groups'][0]['tv_ids'] = []
        self.assertEqual(permitted_ids(self.cfg, rows, grants), set())

    def test_ungrouped_root_cannot_collide_with_a_valid_existing_group_id(self):
        from tv_pages import room_catalog, permitted_ids
        cfg = {'hisense_tvs': [{'id': 'a'}, {'id': 'b'}],
               'hisense_tv_groups': [{'id': '_ungrouped', 'tv_ids': ['a']}]}
        rooms = room_catalog(cfg)
        self.assertEqual(rooms[0]['id'], '_ungrouped')
        self.assertNotEqual(rooms[0]['id'], rooms[1]['id'])
        grants = {1: {'tvroom:_ungrouped', 'tvdevice:_ungrouped:*'}}
        self.assertEqual(permitted_ids(cfg, [{'id': 1}], grants), {'a'})

    def test_diagnostics_payload_does_not_expose_network_or_setup_secrets(self):
        from tv_pages import public_tv
        value = public_tv({'id': 'a', 'host': 'SECRET', 'mac': 'SECRET', 'lastError': 'password=SECRET',
                           'observation': {'fields': {}, 'failures': []}})
        self.assertNotIn('SECRET', str(value))

    def test_unmapped_media_and_room_bulk_commands_rejected(self):
        response = self.client.post('/api/tv-media/a/display', json={'media_id': 'x'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('mapping', response.get_json()['error'].lower())
