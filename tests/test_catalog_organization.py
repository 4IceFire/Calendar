import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import webui
from catalog_organization import validate_organization
from package.apps.videohub.app import VideohubApp
import test_group_media_permissions


class CatalogOrganizationTests(unittest.TestCase):
    def test_legacy_presets_survive_organization_and_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'presets.json'
            old = [{'id': 9, 'name': 'Nine', 'routes': [{'output': 2, 'input': 8}], 'locked': True},
                   {'id': 21, 'name': 'Twenty one', 'routes': [], 'locked': False}]
            path.write_text(json.dumps(old), encoding='utf-8')
            cfg = {'videohub_presets_file': str(path)}
            app = VideohubApp()
            self.assertEqual(app.list_presets(cfg), old)
            tree = {'folders': [{'id': 'services', 'name': 'Services'}],
                    'items': [{'id': '21', 'folderId': 'services'}, {'id': '9', 'folderId': None}]}
            expected = app.save_organization(cfg, tree)
            app.upsert_preset(cfg, {'id': 21, 'name': 'Renamed preset', 'routes': []})
            app.set_preset_locked(cfg, 21, True)
            self.assertEqual(VideohubApp().get_organization(cfg), expected)
            self.assertEqual(app.get_preset(cfg, 9).to_dict(), old[0])
            self.assertTrue(app.get_preset(cfg, 21).locked)
            self.assertEqual([item['id'] for item in app.get_organization(cfg)['items']], ['21', '9'])
            # Removing a folder moves entries to root without touching their routes or IDs.
            tree['folders'] = []
            tree['items'][0]['folderId'] = None
            app.save_organization(cfg, tree)
            self.assertIsNone(app.get_organization(cfg)['items'][0]['folderId'])
            app.set_preset_locked(cfg, 21, False)
            app.delete_preset(cfg, 21)
            created = app.upsert_preset(cfg, {'name': 'New', 'routes': []})
            self.assertGreater(created.id, 21)
            self.assertEqual(app.get_preset(cfg, 9).to_dict(), old[0])

    def test_invalid_tree_rejected_without_losing_items(self):
        for value in ({'folders': [], 'items': []},
                      {'folders': [], 'items': [{'id': 2, 'folderId': 'missing'}]},
                      {'folders': [], 'items': [{'id': 2}, {'id': 2}]}):
            with self.assertRaises(ValueError):
                validate_organization(value, [2])


class GroupOrganizationTests(unittest.TestCase):
    setUp = test_group_media_permissions.GroupMediaPermissionTests.setUp

    def test_rename_preserves_membership_grants_and_protected_admin(self):
        webui._set_group_pages(2, ['page:routing', 'page:videohub'])
        before = webui._group_settings_snapshot(2)
        result = self.client.post('/api/admin/groups/2/rename', headers=self.headers, json={'name': 'Production'})
        self.assertEqual(result.status_code, 200)
        after = webui._group_settings_snapshot(2)
        self.assertEqual(after['name'], 'Production')
        before['name'] = 'Production'
        self.assertEqual(before, after)
        self.assertEqual([g['id'] for g in webui._get_user_groups(2)], [2])
        for gid, name in ((1, 'Renamed Admin'), (2, 'admin')):
            result = self.client.post(f'/api/admin/groups/{gid}/rename', headers=self.headers, json={'name': name})
            self.assertEqual(result.status_code, 403)
        self.assertEqual(self.client.post('/api/admin/groups/999/rename', headers=self.headers, json={'name': 'Missing'}).status_code, 404)
        for malformed in ([], None, {}, {'name': {}}, {'name': []}, {'name': 12}, {'name': ' '}, {'name': 'Other', 'is_admin': True}):
            result = self.client.post('/api/admin/groups/2/rename', headers=self.headers, json=malformed)
            self.assertEqual(result.status_code, 400)
            self.assertEqual(webui._group_settings_snapshot(2), after)
        webui._ensure_group('Another')
        self.assertEqual(self.client.post('/api/admin/groups/2/rename', headers=self.headers, json={'name': 'ANOTHER'}).status_code, 400)

    def test_group_folder_persistence_and_resource_permissions(self):
        tree = {'folders': [{'id': 'team', 'name': 'Team'}],
                'items': [{'id': '2', 'folderId': 'team'}, {'id': '1', 'folderId': None}]}
        before = webui._group_settings_snapshot(2)
        result = self.client.post('/api/admin/groups/organization', headers=self.headers, json=tree)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get('/api/admin/groups/organization').get_json()['organization'], result.get_json()['organization'])
        self.assertEqual(before, webui._group_settings_snapshot(2))
        self.assertEqual(self.client.post('/api/admin/groups/organization', json=tree).status_code, 403)
        with patch.object(webui, 'current_user', webui._User(webui._user_record(2))):
            self.assertEqual(self.client.post('/api/admin/groups/organization', headers=self.headers, json=tree).status_code, 403)
            self.assertEqual(self.client.post('/api/admin/groups/2/rename', headers=self.headers, json={'name': 'Denied'}).status_code, 403)

    def test_videohub_edit_grant_and_hidden_presets_are_preserved(self):
        self.cfg['videohub_presets_file'] = str(Path(self.temp.name) / 'presets.json')
        app = VideohubApp()
        first = app.upsert_preset(self.cfg, {'name': 'Visible', 'routes': []})
        second = app.upsert_preset(self.cfg, {'name': 'Hidden', 'routes': [{'output': 2, 'input': 5}]})
        webui._set_group_pages(2, ['page:videohub'])
        webui._set_group_videohub_allowed_preset_ids(2, [first.id])
        webui._set_group_videohub_can_edit_presets(2, False)
        value = {'folders': [{'id': 'team', 'name': 'Team'}], 'items': [{'id': str(first.id), 'folderId': 'team'}]}
        with patch.object(webui, '_get_videohub_app', return_value=app), patch.object(webui, 'current_user', webui._User(webui._user_record(2))):
            self.assertEqual(self.client.post('/api/videohub/presets/organization', headers=self.headers, json=value).status_code, 403)
            webui._set_group_videohub_can_edit_presets(2, True)
            response = self.client.post('/api/videohub/presets/organization', headers=self.headers, json=value)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([item['id'] for item in response.get_json()['organization']['items']], [str(first.id)])
        self.assertEqual(app.get_preset(self.cfg, second.id).to_dict(), second.to_dict())
        self.assertEqual({item['id'] for item in app.get_organization(self.cfg)['items']}, {str(first.id), str(second.id)})
