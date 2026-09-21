"""Shared display defaults use isolated authentication and fake devices."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import test_user_impersonation as auth_fixture

webui = auth_fixture.webui


class AdminDefaultViewsTests(unittest.TestCase):
    setUp = auth_fixture.ViewAsUserTests.setUp
    execute = auth_fixture.ViewAsUserTests.execute
    rows = auth_fixture.ViewAsUserTests.rows
    sign_in = auth_fixture.ViewAsUserTests.sign_in
    headers = auth_fixture.ViewAsUserTests.headers

    def save(self, page='routing', data=None, **kwargs):
        return self.client.put('/api/admin/default-views/' + page,
                               json=data if data is not None else {'inputs': [1], 'outputs': [2]},
                               headers=kwargs.pop('headers', self.headers()), **kwargs)

    def test_defaults_persist_and_are_shared_by_admins(self):
        response = self.save()
        self.assertEqual(response.status_code, 200)
        self.execute("INSERT INTO user_groups(user_id,group_id) VALUES(3,1)")
        self.sign_in(3)
        response = self.client.get('/routing')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-default-view=\'{"inputs": [1], "outputs": [2]}\'', response.data)
        with webui.app.test_request_context('/'):
            with patch.object(webui, '_can_edit_admin_default_views', return_value=True):
                self.assertEqual(webui._admin_default_view('routing'), {'inputs': [1], 'outputs': [2]})
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM auth_meta WHERE key='admin_default_view:routing'")[0]['n'], 1)

    def test_non_admin_never_uses_or_changes_admin_defaults(self):
        self.assertEqual(self.save().status_code, 200)
        self.sign_in(2)
        response = self.client.get('/routing')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-default-view=\'null\'', response.data)
        self.assertIn(b'data-allowed-outputs=\'[1]\'', response.data)
        self.assertNotIn(b'id="routing-default-view"', response.data)
        self.assertEqual(self.save().status_code, 403)
        self.sign_in(3)  # page:admin alone must not grant defaults editing.
        self.assertEqual(self.save().status_code, 403)

    def test_csrf_validation_and_no_permission_mutations(self):
        before = self.rows('SELECT * FROM group_pages')
        self.assertEqual(self.save(headers={'Origin': 'http://localhost'}).status_code, 403)
        self.assertEqual(self.save(data={'inputs': [0], 'outputs': []}).status_code, 400)
        self.assertEqual(self.save(data={'inputs': [True], 'outputs': []}).status_code, 400)
        self.assertEqual(self.save(data={'inputs': [1]}).status_code, 400)
        self.assertEqual(self.save('audio', {'sources': ['master', '1', '1']}).json['preferences'], {'sources': ['master', '1']})
        self.assertEqual(self.save('audio', {'sources': ['monitor']}).status_code, 400)
        self.assertEqual(self.rows('SELECT * FROM group_pages'), before)

    def test_automation_denied_and_audio_controls_remain_permission_scoped(self):
        self.assertFalse(webui._api_policy('/api/admin/default-views/audio', 'PUT')['service_tokens'])
        self.assertTrue(webui._api_scheduler_path_denied('/api/admin/default-views/audio'))
        self.execute("INSERT INTO group_pages(group_id,page_key) VALUES(2,'page:atem_audio')")
        self.execute("UPDATE groups SET atem_allowed_audio_sources='[\"1\"]', atem_can_monitor_audio=0 WHERE id=2")
        self.sign_in(2)
        response = self.client.get('/foyer-audio')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-allowed-source-ids=\'["1"]\'', response.data)
        self.assertIn(b'data-can-monitor=\'false\'', response.data)
        self.assertNotIn(b'id="audio-default-view"', response.data)


if __name__ == '__main__':
    unittest.main()
