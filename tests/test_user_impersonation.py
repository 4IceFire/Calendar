from __future__ import annotations

import io
import json
import socket
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

with patch.object(threading.Thread, 'start'), patch.object(socket.socket, 'connect', side_effect=AssertionError('No hardware in tests')):
    import webui
from media_library import MediaLibrary


class ViewAsUserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.temp.name)
        self.cfg = {'auth_enabled': True, 'auth_idle_timeout_enabled': True,
                    'auth_idle_timeout_minutes': 10, 'api_legacy_anonymous_enabled': False,
                    'atem_media_enabled': False, 'atem_media_destinations': []}
        self.library = MediaLibrary(root / 'media')
        self.vh = Mock()
        self.vh.verify_video_output_route.return_value = True
        self.routing = Mock()
        self.routing.display.return_value = {'id': 'job', 'mediaId': 'image', 'output': 1,
                                            'status': 'queued', 'message': 'Preparing'}
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', root / 'auth.db'),
            patch.object(webui, '_auth_cfg', side_effect=lambda: self.cfg),
            patch.object(webui.utils, 'get_config', side_effect=lambda: dict(self.cfg)),
            patch.object(webui, '_bootstrap_default_users_roles'),
            patch.object(webui, '_get_media_library', return_value=self.library),
            patch.object(webui, '_get_media_routing_manager', return_value=self.routing),
            patch.object(webui, '_active_media_job', return_value={}),
            patch.object(webui, '_get_videohub_client_from_config', return_value=self.vh),
            patch.object(webui, '_home_set_last_videohub_route'),
            patch.object(webui, '_invalidate_videohub_state_snapshot'),
            patch.object(webui, '_get_videohub_state_snapshot', return_value={
                'outputs': [{'number': 1, 'label': 'Foyer'}, {'number': 2, 'label': 'Kids'}]}),
            patch.object(socket.socket, 'connect', side_effect=AssertionError('No hardware in tests')),
            patch.object(socket.socket, 'sendto', side_effect=AssertionError('No hardware in tests')),
        ):
            self.stack.enter_context(context)
        webui._init_auth_db()
        conn = webui._db()
        try:
            conn.executemany('INSERT INTO users(id,username,password_hash) VALUES (?,?,?)',
                             [(1, 'administrator', 'not-a-password'), (2, 'operator', 'not-a-password'),
                              (3, 'permissions-editor', 'not-a-password')])
            conn.execute("INSERT INTO groups(id,name,is_admin,is_system) VALUES(1,'Admin',1,1)")
            conn.execute("INSERT INTO groups(id,name,videohub_allowed_outputs,videohub_allowed_inputs) VALUES(2,'Operators','[1]','[1,2]')")
            conn.execute("INSERT INTO groups(id,name) VALUES(3,'Permissions editors')")
            conn.executemany('INSERT INTO user_groups(user_id,group_id) VALUES(?,?)', [(1, 1), (2, 2), (3, 3)])
            conn.executemany('INSERT INTO group_pages(group_id,page_key) VALUES(?,?)',
                             [(2, 'page:routing'), (2, 'page:media'), (2, 'page:media_upload'), (3, 'page:admin')])
            for uid in (1, 2, 3):
                conn.execute('INSERT INTO user_sessions(id,user_id,created_at,last_seen_at,session_version) VALUES(?,?,?,?,0)',
                             (f'login-{uid}', uid, '2026-09-13 10:00:00', '2026-09-13 10:00:00'))
            conn.commit()
        finally:
            conn.close()
        self.client = webui.app.test_client()
        self.sign_in(1)
        with webui._api_rate_lock:
            webui._api_rate_events.clear()

    def execute(self, sql, args=()):
        conn = webui._db()
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()

    def rows(self, sql, args=()):
        conn = webui._db()
        try:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def sign_in(self, uid, client=None):
        with (client or self.client).session_transaction() as state:
            state.clear()
            state.update(_user_id=str(uid), _auth_session_id=f'login-{uid}', _fresh=True,
                         _last_activity=int(time.time()), _csrf='initial-csrf')

    def headers(self, client=None):
        with (client or self.client).session_transaction() as state:
            return {'X-CSRF-Token': state['_csrf'], 'Origin': 'http://localhost'}

    def start(self, uid=2, **kwargs):
        kwargs.setdefault('headers', self.headers())
        return self.client.post(f'/admin/users/{uid}/view-as', **kwargs)

    def stop(self, **kwargs):
        kwargs.setdefault('headers', self.headers())
        return self.client.post('/auth/view-as/stop', **kwargs)

    @staticmethod
    def image():
        stream = io.BytesIO()
        Image.new('RGB', (16, 9), 'blue').save(stream, format='PNG')
        stream.seek(0)
        return stream

    def test_start_uses_target_pages_and_retains_original_login(self):
        users_before = self.rows('SELECT * FROM users')
        target_sessions_before = self.rows('SELECT * FROM user_sessions WHERE user_id=2')
        response = self.start()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, '/routing')
        with self.client.session_transaction() as state:
            self.assertEqual(state['_user_id'], '1')
            self.assertEqual(state['_auth_session_id'], 'login-1')
            self.assertNotEqual(state['_csrf'], 'initial-csrf')
        page = self.client.get('/media?output=1')
        self.assertEqual(page.status_code, 200)
        markup = page.get_data(as_text=True)
        self.assertIn('Viewing as operator', markup)
        self.assertIn('Actions are live', markup)
        self.assertIn('Return to admin', markup)
        self.assertNotIn('href="/config"', markup)
        self.assertEqual(self.client.get('/config/atem-media').status_code, 403)
        self.assertEqual(self.client.get('/api/atem/media/state').status_code, 403)
        self.assertEqual(self.rows('SELECT * FROM users'), users_before)
        self.assertEqual(self.rows('SELECT * FROM user_sessions WHERE user_id=2'), target_sessions_before)

    def test_page_admin_grant_cannot_impersonate(self):
        self.sign_in(3)
        self.assertEqual(self.start().status_code, 403)

    def test_anonymous_disabled_token_and_internal_callers_are_denied(self):
        with self.client.session_transaction() as state:
            state.pop('_user_id')
        self.assertEqual(self.start().status_code, 403)
        self.sign_in(1)
        self.cfg['auth_enabled'] = False
        self.assertEqual(self.start().status_code, 403)
        self.cfg['auth_enabled'] = True
        self.assertEqual(self.start(headers={**self.headers(), 'Authorization': 'Bearer any-token'}).status_code, 403)
        with webui.app.test_request_context('/admin/users/2/view-as', method='POST'):
            webui.g._tdeck_scheduler_principal = {'type': 'scheduler'}
            self.assertEqual(webui.app.full_dispatch_request().status_code, 403)

    def test_transitions_require_csrf_and_origin_and_are_post_only(self):
        for headers in ({}, {'Origin': 'http://localhost'},
                        {**self.headers(), 'Origin': 'https://attacker.invalid'}):
            self.assertEqual(self.start(headers=headers).status_code, 403)
        self.assertNotEqual(self.client.get('/admin/users/2/view-as').status_code, 200)
        self.assertEqual(self.start().status_code, 303)
        for headers in ({}, {**self.headers(), 'Origin': 'https://attacker.invalid'}):
            self.assertEqual(self.stop(headers=headers).status_code, 403)
        with self.client.session_transaction() as state:
            self.assertIn('_view_as', state)
        self.assertNotEqual(self.client.get('/auth/view-as/stop').status_code, 200)

    def test_inactive_locked_and_password_change_targets_are_rejected(self):
        for field, value in (('is_active', 0), ('is_locked', 1), ('force_password_change', 1)):
            with self.subTest(field=field):
                self.execute(f'UPDATE users SET {field}=? WHERE id=2', (value,))
                self.assertEqual(self.start().status_code, 409)
                self.execute(f'UPDATE users SET {field}=? WHERE id=2', (1 if field == 'is_active' else 0,))
        self.assertEqual(self.start(99).status_code, 404)

    def test_self_and_nested_tests_are_rejected(self):
        self.assertEqual(self.start(1).status_code, 400)
        self.assertEqual(self.start().status_code, 303)
        self.assertEqual(self.start(3).status_code, 409)

    def test_actions_apply_target_permissions_and_resource_limits(self):
        self.start()
        allowed = self.client.post('/api/media/upload', data={'file': (self.image(), 'test.png')}, headers=self.headers())
        self.assertEqual(allowed.status_code, 201)
        forbidden = self.client.post('/api/videohub/route', json={'output': 2, 'input': 1}, headers=self.headers())
        self.assertEqual(forbidden.status_code, 403)
        self.vh.route_video_output.assert_not_called()
        allowed = self.client.post('/api/videohub/route', json={'output': 1, 'input': 2}, headers=self.headers())
        self.assertEqual(allowed.status_code, 200)
        self.vh.route_video_output.assert_called_once_with(output=0, input_=1, monitoring=False)
        self.execute("DELETE FROM group_pages WHERE group_id=2 AND page_key='page:media_upload'")
        denied = self.client.post('/api/media/upload', data={'file': (self.image(), 'test.png')}, headers=self.headers())
        self.assertEqual(denied.status_code, 403)

    def test_stop_restores_admin_and_stale_target_csrf_cannot_write(self):
        self.start()
        old_headers = self.headers()
        response = self.stop()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, '/admin/users/2')
        with self.client.session_transaction() as state:
            self.assertNotIn('_view_as', state)
            self.assertEqual(state['_auth_session_id'], 'login-1')
        self.assertEqual(self.client.get('/api/atem/media/state').status_code, 200)
        response = self.client.post('/api/videohub/route', json={'output': 2, 'input': 1}, headers=old_headers)
        self.assertEqual(response.status_code, 403)
        self.vh.route_video_output.assert_not_called()

    def test_password_and_login_mutations_are_blocked(self):
        self.start()
        for path in ('/account/password', '/login'):
            self.assertEqual(self.client.post(path, headers=self.headers()).status_code, 403)
        markup = self.client.get('/account/password').get_data(as_text=True)
        self.assertIn('Password changes are unavailable', markup)
        self.assertNotIn('name="new_password"', markup)

    def test_target_revocation_blocks_pending_action_without_admin_fallback(self):
        for update in ('is_locked=1', 'is_active=0', 'force_password_change=1', 'session_version=1'):
            with self.subTest(update=update):
                self.execute('UPDATE users SET is_locked=0,is_active=1,force_password_change=0,session_version=0 WHERE id=2')
                self.start()
                self.execute(f'UPDATE users SET {update} WHERE id=2')
                response = self.client.post('/api/videohub/route', json={'output': 2, 'input': 1}, headers=self.headers())
                self.assertEqual(response.status_code, 409)
                self.vh.route_video_output.assert_not_called()
                with self.client.session_transaction() as state:
                    self.assertNotIn('_view_as', state)

    def test_return_still_works_when_target_is_deleted_or_locked(self):
        self.start()
        self.execute('UPDATE users SET is_locked=1 WHERE id=2')
        self.assertEqual(self.stop().status_code, 303)
        self.execute('UPDATE users SET is_locked=0 WHERE id=2')
        self.start()
        self.execute('DELETE FROM users WHERE id=2')
        self.assertEqual(self.stop().status_code, 303)

    def test_original_admin_revocation_or_authority_loss_ends_session(self):
        changes = ("UPDATE user_sessions SET revoked_at='revoked' WHERE id='login-1'",
                   'UPDATE users SET session_version=1 WHERE id=1',
                   'UPDATE users SET is_locked=1 WHERE id=1',
                   'UPDATE users SET force_password_change=1 WHERE id=1',
                   'DELETE FROM user_groups WHERE user_id=1')
        for update in changes:
            with self.subTest(update=update):
                self.execute('UPDATE users SET is_locked=0,force_password_change=0,session_version=0 WHERE id=1')
                self.execute("UPDATE user_sessions SET revoked_at=NULL WHERE id='login-1'")
                self.execute('INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES(1,1)')
                self.sign_in(1)
                self.start()
                self.execute(update)
                response = self.client.post('/api/videohub/route', json={'output': 1, 'input': 1}, headers=self.headers())
                self.assertEqual(response.status_code, 403)
                self.vh.route_video_output.assert_not_called()
                with self.client.session_transaction() as state:
                    self.assertNotIn('_user_id', state)

    def test_original_admin_idle_timeout_is_honored_even_if_target_disables_it(self):
        self.execute('UPDATE groups SET auth_idle_timeout_minutes_override=0 WHERE id=2')
        self.start()
        with self.client.session_transaction() as state:
            state['_last_activity'] = int(time.time()) - 1000
        self.assertEqual(self.client.get('/media').status_code, 403)
        with self.client.session_transaction() as state:
            self.assertNotIn('_user_id', state)

    def test_cookie_overlay_cannot_be_combined_with_bearer_authority(self):
        self.start()
        self.assertEqual(self.client.post('/api/videohub/route', json={'output': 2, 'input': 1},
                                         headers={**self.headers(), 'Authorization': 'Bearer anything'}).status_code, 403)
        self.vh.route_video_output.assert_not_called()
        self.sign_in(3)
        with self.client.session_transaction() as state:
            state['_view_as'] = {'admin_id': 1, 'target_id': 2, 'target_version': 0}
        self.assertEqual(self.client.get('/api/media').status_code, 403)

    def test_disabling_auth_ends_test_without_executing_pending_action_or_trapping_browser(self):
        self.start()
        self.cfg['auth_enabled'] = False
        response = self.client.post('/api/videohub/route', json={'output': 2, 'input': 1}, headers=self.headers())
        self.assertEqual(response.status_code, 403)
        self.vh.route_video_output.assert_not_called()
        with self.client.session_transaction() as state:
            self.assertNotIn('_user_id', state)
            self.assertNotIn('_view_as', state)
        self.assertEqual(self.client.get('/media').status_code, 200)
        self.assertEqual(self.client.get('/logout').status_code, 302)

    def test_other_browser_and_actual_target_session_are_unchanged(self):
        other_admin = webui.app.test_client()
        target_client = webui.app.test_client()
        self.sign_in(1, other_admin)
        self.sign_in(2, target_client)
        self.start()
        self.assertEqual(other_admin.get('/config/atem-media').status_code, 200)
        self.assertEqual(target_client.get('/media').status_code, 200)
        self.assertNotIn('Viewing as operator', target_client.get('/media').get_data(as_text=True))
        self.assertEqual(self.stop().status_code, 303)
        self.assertEqual(target_client.get('/media').status_code, 200)

    def test_audit_attributes_sync_and_background_actions_to_both_people(self):
        self.start()
        uploaded = self.client.post('/api/media/upload', data={'file': (self.image(), 'test.png')}, headers=self.headers())
        image_id = uploaded.get_json()['item']['id']
        response = self.client.post('/api/media/display', json={'output': 1, 'media_id': image_id}, headers=self.headers())
        self.assertEqual(response.status_code, 202)
        callback = self.routing.display.call_args.kwargs['on_complete']
        self.stop()
        # Complete after the browser has returned to Admin, outside any request.
        callback({'id': 'job', 'output': 1, 'status': 'succeeded'})
        for action in ('user.view_as.start', 'media.image.upload', 'media.display.queued', 'user.view_as.stop', 'media.display'):
            with self.subTest(action=action):
                rows = self.rows('SELECT * FROM activity_log WHERE action=?', (action,))
                self.assertTrue(rows)
                entry = rows[-1]
                self.assertEqual(entry['actor_user_id'], 1)
                self.assertEqual(entry['actor_display'], 'administrator as operator')
                self.assertEqual(json.loads(entry['details_json'])['view_as']['target_id'], 2)
        terminal = self.rows("SELECT * FROM activity_log WHERE action='media.display'")[-1]
        self.assertEqual(terminal['request_path'], '/api/media/display')
        self.assertEqual(terminal['ip'], '127.0.0.1')
        self.assertEqual(terminal['source'], 'web')


if __name__ == '__main__':
    unittest.main()
