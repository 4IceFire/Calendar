from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

import webui


class _GroupControls(HTMLParser):
    """Read the actual rendered form, including controls in hidden tabs."""

    def __init__(self, html, group_id):
        super().__init__()
        self.group_id = str(group_id)
        self.parents = []
        self.controls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        in_group = any(parent_tag == 'form' and parent.get('data-role-id') == self.group_id
                       for parent_tag, parent in self.parents)
        if in_group and tag in ('input', 'button'):
            self.controls.append((attrs, [parent for _, parent in self.parents]))
        if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
            self.parents.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.parents) - 1, -1, -1):
            if self.parents[index][0] == tag:
                del self.parents[index:]
                break

    def checked_pages(self):
        return [attrs['value'] for attrs, _ in self.controls
                if attrs.get('name') == 'page_keys' and 'checked' in attrs]


class GroupMediaPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'auth.db'
        self.cfg = {'auth_enabled': True, 'api_write_rate_limit_per_minute': 600,
                    'api_legacy_anonymous_enabled': False}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.library = Mock()
        self.library.list.return_value = []
        self.library.root = Path(self.temp.name) / "library"
        self.library.purge_expired.return_value = []
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', self.db),
            patch.object(webui, '_auth_enabled', return_value=True),
            patch.object(webui, '_auth_cfg', return_value=self.cfg),
            patch.object(webui.utils, 'get_config', return_value=self.cfg),
            patch.object(webui, '_bootstrap_default_users_roles'),
            patch.object(webui, '_touch_current_user_session', return_value=True),
            patch.object(webui, '_digico_aux_options', return_value=[]),
            patch.object(webui, '_pixie_permission_catalog', return_value={'auditoriums': [], 'scenes': []}),
            patch.object(webui, '_get_atem_audio_sources_for_permissions', return_value=[]),
            patch.object(webui, '_load_companion_surfaces', return_value=[]),
            patch.object(webui, '_get_media_library', return_value=self.library),
            patch.object(webui, 'log_event'),
        ):
            self.stack.enter_context(context)
        webui._init_auth_db()
        conn = webui._db()
        try:
            conn.executemany('INSERT INTO groups(id,name,is_admin) VALUES (?,?,?)',
                             [(1, 'Admin', 1), (2, 'Media team', 0)])
            conn.executemany('INSERT INTO users(id,username,password_hash) VALUES (?,?,?)',
                             [(1, 'administrator', 'unused-test-hash'), (2, 'operator', 'unused-test-hash')])
            conn.executemany('INSERT INTO user_groups(user_id,group_id) VALUES (?,?)', [(1, 1), (2, 2)])
            conn.commit()
        finally:
            conn.close()
        self.stack.enter_context(patch.object(webui, 'current_user', webui._User(webui._user_record(1))))
        self.client = webui.app.test_client()
        with self.client.session_transaction() as state:
            state['_csrf'] = 'group-media-csrf'
        self.headers = {'Origin': 'http://localhost', 'X-CSRF-Token': 'group-media-csrf'}
        with webui._api_rate_lock:
            webui._api_rate_events.clear()

    def _editor(self):
        response = self.client.get('/admin/permissions?tab=groups')
        self.assertEqual(response.status_code, 200)
        return _GroupControls(response.get_data(as_text=True), 2)

    def _save(self, keys):
        response = self.client.post('/api/admin/groups/2', headers=self.headers, json={
            'page_keys': keys, 'videohub_allowed_outputs_role': '[2]', 'videohub_allowed_inputs_role': '[5]',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        return set(webui._group_settings_snapshot(2)['page_keys'])

    def test_media_and_upload_are_only_in_routing_detail_tab(self):
        webui._set_group_pages(2, ['page:media', 'page:media_upload', 'page:media_load', 'page:media_manage'])
        controls = self._editor().controls
        top_media = [attrs['value'] for attrs, parents in controls
                     if attrs.get('name') == 'page_keys' and attrs.get('value', '').startswith('page:media')
                     and any('group-page-access' in parent.get('class', '').split() for parent in parents)]
        self.assertEqual(top_media, ['page:media_library'])
        media = [(attrs, parents) for attrs, parents in controls if attrs.get('value') == 'page:media']
        self.assertEqual(len(media), 1)
        self.assertTrue(any(parent.get('data-permission-panel') == 'routing' for parent in media[0][1]))
        uploads = [(attrs, parents) for attrs, parents in controls if attrs.get('value') == 'page:media_upload']
        self.assertEqual(len(uploads), 1)
        self.assertIn('checked', uploads[0][0])
        self.assertTrue(any(parent.get('data-permission-panel') == 'routing' for parent in uploads[0][1]))
        self.assertFalse(any(attrs.get('value') in ('page:media_load', 'page:media_manage') for attrs, _ in controls))

    def test_library_access_and_permanent_upload_grants_are_independent(self):
        webui._set_group_pages(2, ['page:media_library', 'page:media_save'])
        user = webui._User(webui._user_record(2))
        for check in (user.allows_page, lambda key: webui._user_allows_page(2, key)):
            self.assertTrue(check('page:media_library'))
            self.assertFalse(check('page:routing'))
            self.assertFalse(check('page:media_save'))
        keys = ['page:routing', 'page:media', 'page:media_upload', 'page:media_save']
        self._save(keys)
        self.assertTrue(webui._User(webui._user_record(2)).allows_page('page:media_save'))
        self.assertFalse(webui._user_allows_page(2, 'page:media_library'))
        controls = self._editor().controls
        save = [(attrs, parents) for attrs, parents in controls if attrs.get('value') == 'page:media_save']
        self.assertEqual(len(save), 1)
        self.assertTrue(any(parent.get('data-permission-panel') == 'routing' for parent in save[0][1]))
        self._save([key for key in keys if key != 'page:media_upload'])
        self.assertFalse(webui._user_allows_page(2, 'page:media_save'))
        self.assertIn('page:media_save', webui._group_settings_snapshot(2)['page_keys'])

    def test_hidden_upload_survives_page_changes_and_can_be_explicitly_revoked(self):
        webui._set_group_pages(2, ['page:media', 'page:media_upload', 'page:routing'])
        keys = set(self._editor().checked_pages())
        keys.remove('page:routing')
        keys.add('page:home')
        self.assertIn('page:media_upload', self._save(sorted(keys)))
        editor = self._editor()
        media_tab = next(attrs for attrs, _ in editor.controls if attrs.get('data-permission-tab') == 'routing')
        upload = next(attrs for attrs, _ in editor.controls if attrs.get('value') == 'page:media_upload')
        self.assertIn('hidden', media_tab)
        self.assertIn('checked', upload)
        self.assertNotIn('disabled', upload)
        self.assertIn('page:media_upload', self._save(editor.checked_pages() + ['page:account']))
        saved = self._save(self._editor().checked_pages() + ['page:routing'])
        self.assertTrue({'page:media', 'page:media_upload'} <= saved)
        saved = self._save([key for key in saved if key != 'page:media_upload'])
        self.assertNotIn('page:media_upload', saved)
        self.assertIn('page:media', saved)
        snapshot = webui._group_settings_snapshot(2)
        self.assertEqual(snapshot['videohub_allowed_outputs'], [2])
        self.assertEqual(snapshot['videohub_allowed_inputs'], [5])

    def test_form_save_preserves_upload_while_routing_tab_is_hidden(self):
        webui._set_group_pages(2, ['page:media_upload', 'page:account'])
        response = self.client.post('/admin/permissions', headers=self.headers, data={
            '_csrf': 'group-media-csrf', 'action': 'save_group', 'group_id': '2',
            'page_keys': self._editor().checked_pages() + ['page:home'],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(webui._group_settings_snapshot(2)['page_keys']),
                         {'page:media_upload', 'page:account', 'page:home'})

    def test_media_grants_alone_do_not_authorize_pages_images_or_apis(self):
        for keys in (['page:media'], ['page:media', 'page:media_upload'],
                     ['page:routing', 'page:media_upload']):
            with self.subTest(keys=keys):
                webui._set_group_pages(2, keys)
                self.assertFalse(webui._user_allows_page(2, 'page:media'))
                self.assertFalse(webui._user_allows_page(2, 'page:media_upload'))
                with patch.object(webui, 'current_user', webui._User(webui._user_record(2))):
                    for path in ('/media', '/media?output=2', '/media/upload', '/api/media',
                                 '/api/v1/media', '/media/images/' + 'a' * 32 + '.png',
                                 '/media/thumbnails/' + 'a' * 32 + '.png'):
                        self.assertEqual(self.client.get(path).status_code, 403, path)
                    for path in ('/api/media/upload', '/api/v1/media/upload', '/api/media/display'):
                        self.assertEqual(self.client.post(path, headers=self.headers).status_code, 403, path)
        self.library.list.assert_not_called()
        self.library.upload.assert_not_called()

    def test_single_input_output_save_render_and_server_enforcement(self):
        keys = ['page:routing', 'page:media']
        for raw_output, raw_input in (('2', '5'), ('[2]', '[5]'), (2, 5), ([2], [5])):
            with self.subTest(output=raw_output, input=raw_input):
                response = self.client.post('/api/admin/groups/2', headers=self.headers, json={
                    'page_keys': keys, 'videohub_allowed_outputs_role': raw_output,
                    'videohub_allowed_inputs_role': raw_input,
                })
                self.assertEqual(response.status_code, 200)
                user = webui._User(webui._user_record(2))
                self.assertEqual(user.videohub_allowed_outputs, [2])
                self.assertEqual(user.videohub_allowed_inputs, [5])
                with patch.object(webui, 'current_user', user):
                    page = self.client.get('/routing')
                    self.assertEqual(page.status_code, 200)
                    self.assertIn('data-allowed-outputs=\'[2]\'', page.get_data(as_text=True))
                    self.assertIn('data-allowed-inputs=\'[5]\'', page.get_data(as_text=True))
                    with patch.dict(webui.app.view_functions, {key: lambda: webui.jsonify(ok=True) for key in ('api_videohub_route', 'api_v1__api_videohub_route')}):
                        for path in ('/api/videohub/route', '/api/v1/videohub/route'):
                            for output, input_, status in ((2, 5, 200), (1, 5, 403), (2, 1, 403)):
                                result = self.client.post(path, headers=self.headers, json={'output': output, 'input': input_})
                                self.assertEqual(result.status_code, status)
                    self.assertEqual(self.client.get('/media?output=1').status_code, 403)
        # A partial settings update must preserve the other restriction.
        response = self.client.post('/api/admin/groups/2', headers=self.headers, json={
            'page_keys': keys, 'videohub_allowed_inputs_role': '7',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(webui._group_settings_snapshot(2)['videohub_allowed_outputs'], [2])
        self.assertEqual(webui._group_settings_snapshot(2)['videohub_allowed_inputs'], [7])

    def test_invalid_routing_restriction_does_not_save_allow_all_or_other_changes(self):
        keys = ['page:routing']
        self._save(keys)
        before = webui._group_settings_snapshot(2)
        for raw in ('input 2', '2-4', '0', '-1', '2.5', '[2, false]', '2,bad', 'null', {}, True):
            with self.subTest(raw=raw):
                response = self.client.post('/api/admin/groups/2', headers=self.headers, json={
                    'page_keys': keys + ['page:media'], 'videohub_allowed_outputs_role': raw,
                    'videohub_allowed_inputs_role': '7',
                })
                self.assertEqual(response.status_code, 400)
                self.assertIn('Allowed Outputs', response.get_json()['error'])
                self.assertEqual(webui._group_settings_snapshot(2), before)
        response = self.client.post('/admin/permissions', headers=self.headers, data={
            '_csrf': 'group-media-csrf', 'action': 'save_group', 'group_id': '2',
            'page_keys': keys + ['page:media'], 'videohub_allowed_inputs_role': 'bad',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(webui._group_settings_snapshot(2), before)

    def test_routing_lookup_failure_never_renders_unrestricted_controls(self):
        self._save(['page:routing'])
        with patch.object(webui, 'current_user', webui._User(webui._user_record(2))), \
             patch.object(webui, '_effective_videohub_allowlists_for_user', side_effect=RuntimeError('Permission lookup failed')), \
             patch.dict(webui.app.config, {'TESTING': True}):
            with self.assertRaisesRegex(RuntimeError, 'Permission lookup failed'):
                self.client.get('/routing')

    def test_only_routing_groups_expand_port_access_and_admin_is_unrestricted(self):
        self._save(['page:routing'])
        conn = webui._db()
        try:
            conn.execute("INSERT INTO groups(id,name) VALUES (3,'Unrelated group')")
            conn.execute('INSERT INTO user_groups(user_id,group_id) VALUES (2,3)')
            conn.commit()
        finally:
            conn.close()
        webui._set_group_pages(3, ['page:media'])
        self.assertEqual(webui._User(webui._user_record(2)).videohub_allowed_outputs, [2])
        webui._set_group_pages(3, ['page:routing'])
        self.assertEqual(webui._User(webui._user_record(2)).videohub_allowed_outputs, [])
        webui._set_group_videohub_allowlists(3, '3', '6')
        self.assertEqual(webui._User(webui._user_record(2)).videohub_allowed_outputs, [2, 3])
        self.assertEqual(webui._User(webui._user_record(2)).videohub_allowed_inputs, [5, 6])
        self.assertEqual(webui._User(webui._user_record(1)).videohub_allowed_outputs, [])

    def test_stored_legacy_grants_do_not_authorize_media_or_management(self):
        for keys in (['page:routing', 'page:media_load', 'page:media_manage', 'page:media_upload'],
                     ['page:media', 'page:routing', 'page:media_load', 'page:media_manage']):
            with self.subTest(keys=keys):
                webui._set_group_pages(2, keys)
                with patch.object(webui, 'current_user', webui._User(webui._user_record(2))):
                    result = self.client.get('/api/media')
                    if 'page:media' in keys:
                        self.assertEqual(result.status_code, 200)
                        self.assertEqual(result.get_json()['permissions'], {'load': False, 'upload': False, 'manage': False, 'save': False})
                    else:
                        self.assertEqual(result.status_code, 403)
                    self.assertEqual(self.client.patch('/api/media/' + 'a' * 32, json={'preset': True},
                                                       headers=self.headers).status_code, 403)
                    self.assertEqual(self.client.delete('/api/media/' + 'a' * 32, headers=self.headers).status_code, 403)
                    self.assertEqual(self.client.post('/api/media/upload', headers=self.headers).status_code, 403)
        self.library.upload.assert_not_called()
        self.library.update.assert_not_called()
        self.library.delete.assert_not_called()

    def test_media_and_upload_grants_combine_across_groups_without_config_management(self):
        webui._set_group_pages(2, ['page:media'])
        conn = webui._db()
        try:
            conn.execute("INSERT INTO groups(id,name) VALUES (3,'Upload and routing')")
            conn.execute('INSERT INTO user_groups(user_id,group_id) VALUES (2,3)')
            conn.commit()
        finally:
            conn.close()
        webui._set_group_pages(3, ['page:routing', 'page:media_upload'])
        with patch.object(webui, 'current_user', webui._User(webui._user_record(2))):
            result = self.client.get('/api/media')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()['permissions'], {'load': False, 'upload': True, 'manage': False, 'save': False})
            self.assertTrue(webui._media_display_allowed())
            self.assertFalse(webui.can_access('page:config'))


if __name__ == '__main__':
    unittest.main()
