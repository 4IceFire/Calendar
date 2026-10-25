"""Idle API expiry is informational, user-attributed, and still fail-closed."""
import socket
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

with patch.object(threading.Thread, 'start'):
    import webui


class SessionIdleActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.cfg = {'auth_enabled': True, 'auth_idle_timeout_enabled': True,
                    'auth_idle_timeout_minutes': 2, 'api_legacy_anonymous_enabled': False}
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', Path(self.temp.name) / 'auth.db'),
            patch.object(webui, '_auth_cfg', return_value=self.cfg),
            patch.object(webui.utils, 'get_config', return_value=self.cfg),
            patch.object(webui, '_bootstrap_default_users_roles'),
            patch.object(socket.socket, 'connect', side_effect=AssertionError('No hardware')),
            patch.object(socket.socket, 'sendto', side_effect=AssertionError('No hardware')),
        ):
            self.stack.enter_context(context)
        webui._init_auth_db()
        conn = webui._db()
        try:
            conn.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'timeout-operator','unused')")
            conn.execute("INSERT INTO groups(id,name,is_admin) VALUES(1,'Operators',0)")
            conn.execute('INSERT INTO user_groups(user_id,group_id) VALUES(1,1)')
            conn.execute("INSERT INTO group_pages(group_id,page_key) VALUES(1,'page:timers')")
            conn.execute('INSERT INTO user_sessions(id,user_id,created_at,last_seen_at,session_version) VALUES(?,?,?,?,0)',
                         ('idle-fixture', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00'))
            conn.commit()
        finally:
            conn.close()
        self.client = webui.app.test_client()
        endpoint, _ = webui.app.url_map.bind('localhost').match('/api/status/summary', method='GET')
        self.handler = Mock(side_effect=lambda: webui.jsonify(ok=True))
        self.stack.enter_context(patch.dict(webui.app.view_functions, {endpoint: self.handler}))

    def stale_login(self):
        with self.client.session_transaction() as state:
            state.clear()
            state.update(_user_id='1', _auth_session_id='idle-fixture', _fresh=True,
                         _last_activity=int(time.time()) - 180, _csrf='private-idle-csrf')

    def events(self, action):
        conn = webui._db()
        try:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM activity_log WHERE action=? ORDER BY id', (action,))]
        finally:
            conn.close()

    def test_logging_failure_does_not_bypass_timeout(self):
        self.stale_login()
        with patch.object(webui, 'log_event', side_effect=RuntimeError('log unavailable')):
            response = self.client.get('/api/status/summary')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'session_expired')
        with self.client.session_transaction() as state:
            self.assertNotIn('_user_id', state)
        self.handler.assert_not_called()

    def test_fresh_session_still_works_without_timeout_event(self):
        self.stale_login()
        with self.client.session_transaction() as state:
            state['_last_activity'] = int(time.time())
        response = self.client.get('/api/status/summary')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events('security.api.session_idle'), [])
        self.handler.assert_called_once()

    def test_security_rejection_remains_warning(self):
        self.stale_login()
        with self.client.session_transaction() as state:
            state['_last_activity'] = int(time.time())
        response = self.client.post('/api/timers/apply', json={'preset': 1})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'invalid_csrf')
        event = self.events('security.api.csrf_denied')[-1]
        self.assertEqual(event['status'], 'warning')
        self.assertEqual(event['actor_username'], 'timeout-operator')
        self.assertEqual(self.events('security.api.session_idle'), [])

    def test_followup_anonymous_request_is_not_attributed_to_timed_out_user(self):
        self.stale_login()
        self.assertEqual(self.client.get('/api/status/summary').status_code, 401)
        response = self.client.get('/api/status/summary')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'unauthorized')
        self.assertEqual(len(self.events('security.api.session_idle')), 1)
        self.handler.assert_not_called()

    def test_expiry_is_info_and_names_the_user_in_summary(self):
        self.stale_login()
        response = self.client.get('/api/status/summary')
        self.assertEqual(response.status_code, 401)
        event = self.events('security.api.session_idle')[-1]
        self.assertEqual(event['status'], 'info')
        self.assertIn('timeout-operator', event['summary'])
        self.assertIn('inactivity', event['summary'])

    def test_expiry_persists_authenticated_user_before_logout(self):
        for prefix in ('/api', '/api/v1'):
            with self.subTest(prefix=prefix):
                self.stale_login()
                response = self.client.get(prefix + '/status/summary', environ_overrides={'REMOTE_ADDR': '192.0.2.10'})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.get_json()['error'], 'session_expired')
                event = self.events('security.api.session_idle')[-1]
                self.assertEqual(event['actor_user_id'], 1)
                self.assertEqual(event['actor_username'], 'timeout-operator')
                self.assertEqual(event['actor_display'], 'timeout-operator')
                self.assertEqual(event['ip'], '192.0.2.10')
                self.assertEqual(event['request_path'], prefix + '/status/summary')
                self.assertEqual(event['source'], 'api')
                with self.client.session_transaction() as state:
                    self.assertNotIn('_user_id', state)
                    self.assertNotIn('_auth_session_id', state)
                self.assertNotIn('private-idle-csrf', str(event))
        self.handler.assert_not_called()
