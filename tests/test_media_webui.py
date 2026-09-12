from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import api_security
import webui
from media_library import MediaLibrary


class _User:
    is_authenticated = True
    username = 'media-operator'

    def get_id(self):
        return '42'


def _image():
    buffer = io.BytesIO()
    Image.new('RGB', (16, 9), 'blue').save(buffer, format='PNG')
    buffer.seek(0)
    return buffer


class MediaWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = MediaLibrary(self.root / 'library')
        self.item = self.library.upload(_image(), 'welcome.png', 'Welcome')
        self.db = self.root / 'auth.db'
        self.config_file = self.root / 'config.json'
        self.cfg = {
            'auth_enabled': True, 'api_legacy_anonymous_enabled': False,
            'api_write_rate_limit_per_minute': 600, 'api_max_request_bytes': 2048,
            'api_upload_max_request_bytes': 4 * 1024 * 1024,
            'atem_ip': '192.0.2.1', 'atem_media_enabled': False,
            'atem_media_destinations': [], 'atem_media_node_path': '',
        }
        self.grants = {'page:media'}
        self.manager = Mock()
        self.manager.snapshot.return_value = {'enabled': False, 'connected': False, 'job': None, 'destinations': []}
        self.manager.load.return_value = {'id': 'job-1', 'mediaId': self.item['id'], 'mediaName': 'Welcome',
                                         'player': 2, 'status': 'queued'}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', self.db),
            patch.object(webui, '_auth_enabled', return_value=True),
            patch.object(webui, '_auth_cfg', side_effect=lambda: self.cfg),
            patch.object(webui, 'current_user', _User()),
            patch.object(webui, '_touch_current_user_session', return_value=True),
            patch.object(webui, 'can_access', side_effect=lambda key: key in self.grants),
            patch.object(webui, '_get_media_library', return_value=self.library),
            patch.object(webui, '_get_atem_media_manager', return_value=self.manager),
            patch.object(webui, '_active_media_job', side_effect=lambda: self.manager.snapshot.return_value.get('job') or {}),
            patch.object(webui.utils, 'get_config', side_effect=lambda: dict(self.cfg)),
            patch.object(webui.utils, 'CONFIG_FILE', str(self.config_file)),
            patch.object(webui.utils, 'reload_config', return_value=True),
        ):
            self.stack.enter_context(context)
        self.events = self.stack.enter_context(patch.object(webui, 'log_event'))
        webui._init_auth_db()
        with webui._api_rate_lock:
            webui._api_rate_events.clear()
        self.client = webui.app.test_client()
        with self.client.session_transaction() as state:
            state['_csrf'] = 'media-csrf'
        self.headers = {'X-CSRF-Token': 'media-csrf', 'Origin': 'http://localhost'}

    def test_library_view_and_protected_image_urls_work_offline(self):
        result = self.client.get('/api/media')
        self.assertEqual(result.status_code, 200)
        payload = result.get_json()
        self.assertEqual(payload['items'][0]['name'], 'Welcome')
        self.assertFalse(any(payload['permissions'].values()))
        for url_key in ('url', 'thumbnail_url'):
            with self.client.get(payload['items'][0][url_key]) as image:
                self.assertEqual(image.status_code, 200)
                self.assertEqual(image.mimetype, 'image/png')
                self.assertIn('no-store', image.headers['Cache-Control'])
        self.manager.load.assert_not_called()

    def test_media_page_renders_without_loading_hardware(self):
        response = self.client.get('/media')
        self.assertEqual(response.status_code, 200)
        self.assertIn('media.js', response.get_data(as_text=True))
        self.manager.snapshot.assert_not_called()

    def test_browse_permission_does_not_grant_writes(self):
        for method, path, kwargs in (
            ('post', '/api/media/upload', {'data': {'file': (_image(), 'test.png')}}),
            ('patch', '/api/media/' + self.item['id'], {'json': {'name': 'Renamed'}}),
            ('delete', '/api/media/' + self.item['id'], {}),
            ('post', '/api/atem/media/load', {'json': {'media_id': self.item['id'], 'player': 2}}),
        ):
            with self.subTest(path=path):
                result = getattr(self.client, method)(path, headers=self.headers, **kwargs)
                self.assertEqual(result.status_code, 403)
        self.assertEqual(len(self.library.list()), 1)
        self.manager.load.assert_not_called()

    def test_action_permission_alone_does_not_grant_library_or_images(self):
        self.grants = {'page:media_upload', 'page:media_manage', 'page:media_load'}
        for path in ('/api/media', '/api/atem/media/state', '/api/v1/media',
                     '/media/images/' + self.item['id'] + '.png'):
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_upload_requires_csrf_and_same_origin(self):
        self.grants.add('page:media_upload')
        for headers in ({}, {**self.headers, 'Origin': 'https://untrusted.invalid'}):
            result = self.client.post('/api/media/upload', data={'file': (_image(), 'test.png')}, headers=headers)
            self.assertEqual(result.status_code, 403)
        self.assertEqual(len(self.library.list()), 1)

    def test_upload_names_and_presets_can_be_managed_separately(self):
        self.grants.add('page:media_upload')
        result = self.client.post('/api/media/upload', data={'file': (_image(), 'event.png'), 'name': "Mother's Day"},
                                  headers=self.headers)
        self.assertEqual(result.status_code, 201)
        item = result.get_json()['item']
        self.assertEqual(item['name'], "Mother's Day")
        self.assertFalse(item['preset'])
        self.grants.add('page:media_manage')
        edited = self.client.patch('/api/media/' + item['id'], json={'preset': True}, headers=self.headers)
        self.assertEqual(edited.status_code, 200)
        self.assertTrue(edited.get_json()['item']['preset'])
        self.assertEqual(self.client.delete('/api/media/' + item['id'], headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(item['thumbnail_url']).status_code, 404)

    def test_invalid_upload_and_edit_are_rejected(self):
        self.grants.update({'page:media_upload', 'page:media_manage'})
        result = self.client.post('/api/media/upload', data={'file': (io.BytesIO(b'not an image'), 'fake.png')}, headers=self.headers)
        self.assertEqual(result.status_code, 400)
        for payload in ([], {'preset': 'false'}, {'unknown': True}, {'name': ''}, {'name': 17}):
            result = self.client.patch('/api/media/' + self.item['id'], json=payload, headers=self.headers)
            self.assertEqual(result.status_code, 400)

    def test_upload_uses_upload_size_limit_and_alias(self):
        self.grants.add('page:media_upload')
        with webui.app.test_request_context('/api/media/upload', method='POST', data=b'x' * 4096):
            allowed, limit = webui._api_request_within_size_limit('/api/media/upload')
        self.assertTrue(allowed)
        self.assertEqual(limit, self.cfg['api_upload_max_request_bytes'])
        result = self.client.post('/api/v1/media/upload', data={'file': (_image(), 'test.png')}, headers=self.headers)
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.headers['X-TDeck-API-Version'], '1')
        self.cfg['api_upload_max_request_bytes'] = 1024
        result = self.client.post('/api/media/upload', data=b'x' * 2048, content_type='application/octet-stream', headers=self.headers)
        self.assertEqual(result.status_code, 413)

    def test_load_returns_job_and_preserves_actor_for_terminal_log(self):
        self.grants.add('page:media_load')
        result = self.client.post('/api/atem/media/load', json={'media_id': self.item['id'], 'player': 2}, headers=self.headers)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.get_json()['job']['status'], 'queued')
        callback = self.manager.load.call_args.kwargs['on_complete']
        callback({**self.manager.load.return_value, 'status': 'succeeded', 'slot': 5})
        event = self.events.call_args
        self.assertEqual(event.args[0], 'atem.media.load')
        self.assertEqual(event.kwargs['actor_user_id'], 42)
        self.assertEqual(event.kwargs['actor_username'], 'media-operator')
        self.assertEqual(event.kwargs['status'], 'success')

    def test_unknown_image_never_reaches_hardware(self):
        self.grants.add('page:media_load')
        result = self.client.post('/api/atem/media/load', json={'media_id': 'f' * 32, 'player': 2}, headers=self.headers)
        self.assertEqual(result.status_code, 404)
        self.manager.load.assert_not_called()

    def test_busy_job_cannot_be_deleted_or_reconfigured(self):
        self.grants.update({'page:media_manage', 'page:config'})
        self.manager.snapshot.return_value['job'] = {'mediaId': self.item['id'], 'status': 'uploading'}
        self.assertEqual(self.client.delete('/api/media/' + self.item['id'], headers=self.headers).status_code, 409)
        self.assertEqual(self.client.put('/api/config/atem-media', json={'atem_media_enabled': True}, headers=self.headers).status_code, 409)
        self.assertFalse(self.config_file.exists())

    def test_media_setup_requires_config_grant_and_validates_reservations(self):
        self.assertEqual(self.client.get('/api/config/atem-media').status_code, 403)
        self.grants.add('page:config')
        payload = {'atem_media_enabled': True, 'atem_media_destinations': [
            {'player': 2, 'label': 'Foyer', 'slots': [41, 42]},
            {'player': 4, 'label': 'Kids', 'slots': [43, 44]},
        ]}
        response = self.client.put('/api/config/atem-media', json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = json.loads(self.config_file.read_text())
        self.assertEqual(saved['atem_ip'], '192.0.2.1')
        self.assertEqual(saved['atem_media_destinations'][1]['player'], 4)
        payload['atem_media_destinations'][1]['slots'] = [42, 43]
        self.assertEqual(self.client.put('/api/config/atem-media', json=payload, headers=self.headers).status_code, 400)

    def test_service_tokens_and_scheduler_cannot_use_new_media_apis(self):
        token = api_security.create_service_token(self.db, name='ATEM', scopes=['atem', 'config'])['token']
        for method, path in (('get', '/api/media'), ('get', '/api/atem/media/state'),
                             ('post', '/api/atem/media/load'), ('put', '/api/config/atem-media')):
            result = getattr(self.client, method)(path, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(result.status_code, 403)
        with webui.app.test_request_context('/api/atem/media/load', method='POST'):
            webui.g._tdeck_scheduler_principal = {'type': 'scheduler'}
            result = webui._api_scheduler_internal_gate()
            self.assertEqual(result.status_code, 403)

    def test_streamed_upload_without_content_length_is_bounded(self):
        self.grants.add('page:media_upload')
        self.cfg['api_upload_max_request_bytes'] = 1024
        body = (b'--test-boundary\r\nContent-Disposition: form-data; name="file"; filename="test.png"\r\n'
                b'Content-Type: image/png\r\n\r\n' + b'x' * 2048 + b'\r\n--test-boundary--\r\n')
        response = self.client.post('/api/media/upload', data=body, headers=self.headers,
                                    content_type='multipart/form-data; boundary=test-boundary',
                                    environ_overrides={'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()['error'], 'request_too_large')
        self.assertEqual(len(self.library.list()), 1)

    def test_invalid_old_setup_can_be_repaired_without_constructing_manager(self):
        self.grants.update({'page:config', 'page:media_manage'})
        self.cfg['atem_media_destinations'] = 'invalid imported value'
        with patch.object(webui, '_get_atem_media_manager', side_effect=AssertionError('Must not construct a manager')):
            response = self.client.put('/api/config/atem-media', json={'atem_media_destinations': []}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.delete('/api/media/' + self.item['id'], headers=self.headers).status_code, 200)

    def test_config_only_user_can_open_setup_and_state_but_not_library(self):
        self.grants = {'page:config'}
        response = self.client.get('/config/atem-media')
        self.assertEqual(response.status_code, 200)
        self.assertIn('media-setup', response.get_data(as_text=True))
        self.assertNotIn('id="media-grid"', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/atem/media/state').status_code, 200)
        self.assertEqual(self.client.get('/api/media').status_code, 403)

    def test_nonadmin_cannot_change_server_executable(self):
        self.grants.add('page:config')
        with patch.object(webui, '_can_manage_service_tokens_for_current_user', return_value=False):
            response = self.client.put('/api/config/atem-media', json={'atem_media_node_path': 'C:/untrusted.exe'}, headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.config_file.exists())

    def test_general_and_media_config_saves_do_not_revert_reservations(self):
        self.grants.add('page:config')
        entered, release, media_done = threading.Event(), threading.Event(), threading.Event()
        responses = []

        def save_general(cfg):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test timed out')
            self.config_file.write_text(json.dumps(cfg), encoding='utf-8')

        def reload_config(force=False):
            self.cfg.update(json.loads(self.config_file.read_text(encoding='utf-8')))

        def general():
            with webui.app.test_client() as client:
                with client.session_transaction() as session:
                    session['_csrf'] = 'media-csrf'
                responses.append(client.post('/api/config', json={'atem_timeout': 5, 'atem_media_destinations': []}, headers=self.headers).status_code)

        def media():
            with webui.app.test_client() as client:
                with client.session_transaction() as session:
                    session['_csrf'] = 'media-csrf'
                responses.append(client.put('/api/config/atem-media', json={'atem_media_destinations': [
                    {'player': 2, 'label': 'Foyer', 'slots': [5, 6]}]}, headers=self.headers).status_code)
            media_done.set()

        with (patch.object(webui.utils, 'save_config', side_effect=save_general),
              patch.object(webui.utils, 'reload_config', side_effect=reload_config),
              patch.object(webui, '_apply_logging_config')):
            first = threading.Thread(target=general)
            second = threading.Thread(target=media)
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second.start()
                self.assertFalse(media_done.wait(0.05))
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertEqual(responses, [200, 200])
        saved = json.loads(self.config_file.read_text())
        self.assertEqual(saved['atem_timeout'], 5)
        self.assertEqual(saved['atem_media_destinations'][0]['slots'], [5, 6])


if __name__ == '__main__':
    unittest.main()
