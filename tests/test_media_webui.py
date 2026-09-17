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
from werkzeug.datastructures import MultiDict

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
        self.grants = {'page:routing', 'page:media'}
        self.manager = Mock()
        self.manager.snapshot.return_value = {'enabled': False, 'connected': False, 'job': None, 'destinations': []}
        self.manager.load.return_value = {'id': 'job-1', 'mediaId': self.item['id'], 'mediaName': 'Welcome',
                                         'player': 2, 'status': 'queued'}
        self.routing = Mock()
        self.routing.active_job.return_value = None
        self.display_job = {
            'id': 'a' * 32, 'mediaId': self.item['id'], 'output': 1, 'status': 'queued',
            'message': 'Preparing your image…', 'error': '', 'player': 2,
            'videohubInput': 5, 'internalError': 'Private device diagnostic',
        }
        self.routing.display.return_value = dict(self.display_job)
        self.routing.get_job.return_value = dict(self.display_job)
        self.allowed_outputs, self.allowed_inputs = [], []
        self.real_active_media_job = webui._active_media_job
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', self.db),
            patch.object(webui, '_auth_enabled', return_value=True),
            patch.object(webui, '_auth_cfg', side_effect=lambda: self.cfg),
            patch.object(webui, 'current_user', _User()),
            patch.object(webui, '_touch_current_user_session', return_value=True),
            patch.object(webui, 'can_access', side_effect=lambda key: {key, *webui._PAGE_PREREQUISITES.get(key, ())} <= self.grants),
            patch.object(webui, '_get_media_library', return_value=self.library),
            patch.object(webui, '_get_atem_media_manager', return_value=self.manager),
            patch.object(webui, '_get_media_routing_manager', return_value=self.routing),
            patch.object(webui, '_active_media_job', side_effect=lambda: self.routing.active_job.return_value
                         or self.manager.snapshot.return_value.get('job') or {}),
            patch.object(webui, '_effective_videohub_allowlists_for_user',
                         side_effect=lambda _uid: (self.allowed_outputs, self.allowed_inputs)),
            patch.object(webui, '_get_videohub_state_snapshot', return_value={
                'outputs': [{'number': 1, 'label': 'Foyer'}, {'number': 2, 'label': 'Kids'}]}),
            patch.object(webui.utils, 'get_config', side_effect=lambda: dict(self.cfg)),
            patch.object(webui.utils, 'CONFIG_FILE', str(self.config_file)),
            patch.object(webui.utils, 'reload_config', return_value=True),
        ):
            self.stack.enter_context(context)
        self.events = self.stack.enter_context(patch.object(webui, 'log_event'))
        webui._init_auth_db()
        with webui._api_rate_lock:
            webui._api_rate_events.clear()
        with webui._media_upload_rate_lock:
            webui._media_upload_rate_events.clear()
        self.client = webui.app.test_client()
        with self.client.session_transaction() as state:
            state['_csrf'] = 'media-csrf'
        self.headers = {'X-CSRF-Token': 'media-csrf', 'Origin': 'http://localhost'}

    def test_routing_uploads_default_to_private_temporary_images(self):
        self.grants.add('page:media_upload')
        response = self.client.post('/api/media/upload', data={'file': (_image(), 'photo.png')}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        item = response.get_json()['item']
        self.assertTrue(item['temporary'])
        self.assertNotIn('uploaded_by_id', item)
        stored = self.library.get(item['id'])
        self.assertEqual(stored['uploaded_by'], 'media-operator')
        self.assertEqual(stored['uploaded_by_id'], '42')
        from datetime import datetime
        self.assertEqual((datetime.fromisoformat(stored['expires_at']) - datetime.fromisoformat(stored['created_at'])).days, 7)
        self.assertEqual([entry['id'] for entry in self.client.get('/api/media').get_json()['items']], [self.item['id']])
        self.assertEqual(self.client.get('/api/media?collection=temporary').status_code, 403)
        with self.client.get(item['thumbnail_url']) as image:
            self.assertEqual(image.status_code, 200)
        self._allow_display()
        self.assertEqual(self.client.post('/api/media/display', json={'media_id': item['id'], 'output': 1}, headers=self.headers).status_code, 202)
        with patch.object(_User, 'get_id', return_value='99'):
            self.assertEqual(self.client.get(item['thumbnail_url']).status_code, 403)
            self.assertEqual(self.client.post('/api/media/display', json={'media_id': item['id'], 'output': 1}, headers=self.headers).status_code, 403)
        self.grants = {'page:config'}
        self.assertEqual(self.client.get(item['url']).status_code, 403)
        self.grants = {'page:media_library'}
        listing = self.client.get('/api/media?collection=temporary').get_json()['items']
        self.assertEqual(listing[0]['uploaded_by'], 'media-operator')
        with self.client.get(item['url']) as image:
            self.assertEqual(image.status_code, 200)

    def test_permanent_uploads_require_additional_grant_and_ignore_spoofed_metadata(self):
        self.grants.add('page:media_upload')
        for prefix in ('/api', '/api/v1'):
            response = self.client.post(prefix + '/media/upload', data={'file': (_image(), 'photo.png'), 'temporary': 'false'}, headers=self.headers)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.library.list()), 1)
        self.grants.add('page:media_save')
        response = self.client.post('/api/media/upload', data={'file': (_image(), 'photo.png'), 'temporary': 'false'}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.get_json()['item']['temporary'])
        self.assertEqual(len(self.client.get('/api/media').get_json()['items']), 2)
        for field in ('uploaded_by', 'uploaded_by_id', 'expires_at'):
            response = self.client.post('/api/media/upload', data={'file': (_image(), 'photo.png'), field: 'fake'}, headers=self.headers)
            self.assertEqual(response.status_code, 400)
        self.grants.remove('page:media_upload')
        self.assertFalse(webui.can_access('page:media_save'))

    def test_promoting_temporary_image_requires_library_and_preserves_attribution(self):
        item = self.library.upload(_image(), 'temp.png', uploaded_by='Original uploader', uploaded_by_id='99', temporary=True)
        self.grants.add('page:media_save')
        path = '/api/media/' + item['id']
        self.assertEqual(self.client.patch(path, json={'keep': True}, headers=self.headers).status_code, 403)
        self.grants = {'page:media_library'}
        self.assertEqual(self.client.patch(path, json={'keep': True}).status_code, 403)
        response = self.client.patch(path, json={'keep': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        promoted = response.get_json()['item']
        self.assertFalse(promoted['temporary'])
        self.assertIsNone(promoted['expires_at'])
        self.assertEqual(promoted['created_at'], item['created_at'])
        self.assertEqual(promoted['uploaded_by'], 'Original uploader')
        self.assertEqual(self.client.get('/api/media?collection=temporary').get_json()['items'], [])
        self.assertEqual(len(self.client.get('/api/media').get_json()['items']), 2)
        self.assertTrue(any(call.args[0] == 'media.image.keep' for call in self.events.call_args_list))

    def test_retention_waits_for_active_transfer_then_deletes_local_files_only(self):
        item = self.library.upload(_image(), 'temp.png', temporary=True)
        path = self.library.path(item['id'])
        raw = json.loads((self.library.root / 'index.json').read_text())
        next(entry for entry in raw['images'] if entry['id'] == item['id'])['expires_at'] = '2000-01-01T00:00:00+00:00'
        (self.library.root / 'index.json').write_text(json.dumps(raw))
        self.routing.active_job.return_value = {'status': 'uploading', 'mediaId': item['id']}
        webui._cleanup_temporary_media()
        self.assertTrue(path.exists())
        self.routing.active_job.return_value = None
        webui._cleanup_temporary_media()
        self.assertFalse(path.exists())
        self.assertEqual(len(self.library.list()), 1)
        self.manager.load.assert_not_called()
        self.routing.display.assert_not_called()
        self.assertTrue(any(call.args[0] == 'media.image.expire' for call in self.events.call_args_list))

    def test_config_validates_retention_and_uses_it_for_new_uploads(self):
        self.grants = {'page:config', 'page:media_library'}
        for invalid in (0, -1, 366, True, '7', 1.5):
            self.assertEqual(self.client.put('/api/config/atem-media', json={'media_temporary_retention_days': invalid}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.put('/api/config/atem-media', json={'media_temporary_retention_days': 30}, headers=self.headers).status_code, 200)
        self.cfg['media_temporary_retention_days'] = 30
        item = self.client.post('/api/media/upload', data={'file': (_image(), 'photo.png')}, headers=self.headers).get_json()['item']
        from datetime import datetime
        self.assertEqual((datetime.fromisoformat(item['expires_at']) - datetime.fromisoformat(item['created_at'])).days, 30)

    def test_library_view_and_protected_image_urls_work_offline(self):
        result = self.client.get('/api/media')
        self.assertEqual(result.status_code, 200)
        payload = result.get_json()
        self.assertEqual(payload['items'][0]['name'], 'Welcome')
        self.assertFalse(payload['permissions']['upload'])
        self.assertFalse(payload['permissions']['manage'])
        for url_key in ('url', 'thumbnail_url'):
            with self.client.get(payload['items'][0][url_key]) as image:
                self.assertEqual(image.status_code, 200)
                self.assertEqual(image.mimetype, 'image/png')
                self.assertIn('no-store', image.headers['Cache-Control'])
                self.assertEqual(image.headers['X-Content-Type-Options'], 'nosniff')
        self.manager.load.assert_not_called()

    def test_media_page_renders_without_loading_hardware(self):
        response = self.client.get('/media')
        self.assertEqual(response.status_code, 200)
        self.assertIn('media.js', response.get_data(as_text=True))
        self.manager.snapshot.assert_not_called()

    def test_media_permission_does_not_grant_upload_management_or_raw_load(self):
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

    def test_upload_or_legacy_action_permissions_cannot_replace_media_access(self):
        for grants in ({'page:media_upload'}, {'page:media_manage', 'page:media_load'},
                       {'page:routing', 'page:media_upload', 'page:media_manage', 'page:media_load'}):
            with self.subTest(grants=grants):
                self.grants = grants
                for path in ('/media', '/media/upload', '/api/media', '/api/atem/media/state',
                             '/api/v1/media', '/media/images/' + self.item['id'] + '.png',
                             '/media/thumbnails/' + self.item['id'] + '.png'):
                    self.assertEqual(self.client.get(path).status_code, 403)
                upload = self.client.post('/api/media/upload', data={'file': (_image(), 'test.png')},
                                          headers=self.headers)
                self.assertEqual(upload.status_code, 403)
                self.assertEqual(self._post_display().status_code, 403)
        self.assertEqual(len(self.library.list()), 1)
        self.routing.display.assert_not_called()
        self.manager.load.assert_not_called()

    def test_upload_requires_csrf_and_same_origin(self):
        self.grants.add('page:media_upload')
        for headers in ({}, {**self.headers, 'Origin': 'https://untrusted.invalid'}):
            result = self.client.post('/api/media/upload', data={'file': (_image(), 'test.png')}, headers=headers)
            self.assertEqual(result.status_code, 403)
        self.assertEqual(len(self.library.list()), 1)

    def test_upload_denies_permission_origin_and_header_csrf_before_parsing(self):
        for path in ('/api/media/upload', '/api/v1/media/upload'):
            for grants, headers in (
                ({'page:media'}, self.headers),
                ({'page:routing', 'page:media', 'page:media_upload'}, {**self.headers, 'Origin': 'https://untrusted.invalid'}),
                ({'page:routing', 'page:media', 'page:media_upload'}, {**self.headers, 'X-CSRF-Token': 'wrong'}),
            ):
                with self.subTest(path=path, grants=grants, headers=headers):
                    self.grants = grants
                    with patch.object(webui.app.request_class, '_load_form_data',
                                      side_effect=AssertionError('Rejected requests must not parse uploads')):
                        response = self.client.post(path, data={'file': (_image(), 'test.png')}, headers=headers)
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.library.list()), 1)

    def test_upload_accepts_form_csrf_and_large_valid_file_with_bounded_parser(self):
        self.grants.add('page:media_upload')
        # Random pixels produce a PNG larger than the multipart parser's read
        # buffer, checking that form-memory limits still allow normal files.
        import random
        image = io.BytesIO()
        Image.frombytes('RGB', (300, 300), random.Random(0).randbytes(300 * 300 * 3)).save(image, format='PNG')
        self.assertGreater(image.tell(), 128 * 1024)
        image.seek(0)
        response = self.client.post('/api/media/upload',
                                    data={'file': (image, 'photo.png'), 'name': 'Photo', '_csrf': 'media-csrf'},
                                    headers={'Origin': 'http://localhost'})
        self.assertEqual(response.status_code, 201)

    def test_upload_rejects_duplicate_files_fields_and_unexpected_parts(self):
        self.grants.add('page:media_upload')
        for path in ('/api/media/upload', '/api/v1/media/upload'):
            for extra in (
                [('file', (_image(), 'second.png'))],
                [('other_file', (_image(), 'second.png'))],
                [('name', 'First'), ('name', 'Second')],
                [('preset', 'true')],
                [('name', ' ' * 1025)],
            ):
                with self.subTest(path=path, fields=[entry[0] for entry in extra]):
                    # Flask closes each submitted stream; create a fresh
                    # second file for each request using its original bytes.
                    fields = [('file', (_image(), 'photo.png'))]
                    for key, value in extra:
                        fields.append((key, (_image(), value[1]) if isinstance(value, tuple) else value))
                    response = self.client.post(path, data=MultiDict(fields), headers=self.headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.get_json()['error'], 'invalid_upload')
        self.assertEqual(len(self.library.list()), 1)

    def test_upload_limits_multipart_parts_and_text_before_decode(self):
        self.grants.add('page:media_upload')
        for path in ('/api/media/upload', '/api/v1/media/upload'):
            for extra in (
                [('padding' + str(index), 'x') for index in range(4)],
                [('name', 'x' * (128 * 1024 + 1))],
            ):
                with self.subTest(path=path, extra_count=len(extra)):
                    with patch.object(self.library, 'upload', side_effect=AssertionError('Must reject before image decode')):
                        response = self.client.post(path,
                                                    data=MultiDict([('file', (_image(), 'test.png')), *extra]),
                                                    headers=self.headers)
                    self.assertEqual(response.status_code, 413)
                    self.assertEqual(response.get_json()['error'], 'request_too_large')
        self.assertEqual(len(self.library.list()), 1)

    def test_upload_does_not_trust_filename_or_declared_content_type(self):
        self.grants.add('page:media_upload')
        response = self.client.post('/api/media/upload',
                                    data={'file': (io.BytesIO(b'<html>Not an image</html>'), 'photo.png', 'image/png')},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.library.list()), 1)
        response = self.client.post('/api/media/upload',
                                    data={'file': (_image(), '../../outside.html', 'text/html'), 'name': 'Safe pixels'},
                                    headers=self.headers)
        self.assertEqual(response.status_code, 201)
        item = response.get_json()['item']
        self.assertEqual(self.library.path(item['id']).parent, self.library.path(self.item['id']).parent)
        self.assertEqual(self.library.path(item['id']).name, item['id'] + '.png')
        self.assertFalse((self.root / 'outside.html').exists())
        with self.client.get(item['url']) as image:
            self.assertEqual(image.mimetype, 'image/png')
            self.assertTrue(image.data.startswith(b'\x89PNG\r\n\x1a\n'))

    def test_upload_rate_limit_is_shared_by_aliases_and_checked_before_parsing(self):
        self.grants.add('page:media_upload')
        with patch.object(webui, '_MEDIA_UPLOADS_PER_MINUTE', 2):
            for path in ('/api/media/upload', '/api/v1/media/upload'):
                response = self.client.post(path, data={'file': (_image(), 'test.png')}, headers=self.headers)
                self.assertEqual(response.status_code, 201)
            with patch.object(webui.app.request_class, '_load_form_data',
                              side_effect=AssertionError('Rate-limited uploads must not be parsed')):
                response = self.client.post('/api/media/upload', data={'file': (_image(), 'test.png')},
                                            headers=self.headers)
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.get_json()['error'], 'rate_limited')
            self.assertGreater(int(response.headers['Retry-After']), 0)
            # Filling the dedicated upload allowance does not block a normal
            # media read or an unrelated API write.
            self.assertEqual(self.client.get('/api/media').status_code, 200)
            self.grants.update({'page:config', 'page:media_library'})
            self.assertEqual(self.client.patch('/api/media/' + self.item['id'], json={'name': 'Updated'},
                                               headers=self.headers).status_code, 200)
            with webui._media_upload_rate_lock:
                webui._media_upload_rate_events['user:42'] = webui.deque([webui.time.monotonic() - 61] * 2)
            response = self.client.post('/api/media/upload', data={'file': (_image(), 'recovered.png')},
                                        headers=self.headers)
            self.assertEqual(response.status_code, 201)

    def test_only_one_upload_can_parse_or_decode_at_a_time(self):
        self.grants.add('page:media_upload')
        entered, release = threading.Event(), threading.Event()
        results = []
        original_upload = self.library.upload

        def hold_upload(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test timed out')
            return original_upload(*args, **kwargs)

        def first_upload():
            with webui.app.test_client() as client:
                with client.session_transaction() as state:
                    state['_csrf'] = 'media-csrf'
                results.append(client.post('/api/media/upload', data={'file': (_image(), 'first.png')},
                                            headers=self.headers).status_code)

        with patch.object(self.library, 'upload', side_effect=hold_upload):
            worker = threading.Thread(target=first_upload)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                for headers, form in (
                    (self.headers, {}),
                    ({'Origin': 'http://localhost'}, {'_csrf': 'media-csrf'}),
                ):
                    with patch.object(webui.app.request_class, '_load_form_data',
                                      side_effect=AssertionError('Busy uploads must not be parsed')):
                        response = self.client.post('/api/v1/media/upload',
                                                    data={'file': (_image(), 'next.png'), **form}, headers=headers)
                    self.assertEqual(response.status_code, 429)
                    self.assertEqual(response.get_json()['error'], 'upload_busy')
                    self.assertEqual(response.headers['Retry-After'], '2')
            finally:
                release.set()
                worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [201])
        response = self.client.post('/api/media/upload', data={'file': (_image(), 'after.png')}, headers=self.headers)
        self.assertEqual(response.status_code, 201)

    def test_failed_upload_releases_admission_for_next_request(self):
        self.grants.add('page:media_upload')
        for form, headers, expected in (
            ({'file': (_image(), 'test.png'), '_csrf': 'wrong'}, {'Origin': 'http://localhost'}, 403),
            ({'file': (io.BytesIO(b'Not an image'), 'test.png')}, self.headers, 400),
            ({'file': (_image(), 'test.png'), 'name': 'x' * (128 * 1024 + 1)}, self.headers, 413),
        ):
            response = self.client.post('/api/media/upload', data=form, headers=headers)
            self.assertEqual(response.status_code, expected)
            response = self.client.post('/api/media/upload', data={'file': (_image(), 'next.png')}, headers=self.headers)
            self.assertEqual(response.status_code, 201)

    def test_rejected_multipart_upload_closes_partial_temporary_files(self):
        self.grants.add('page:media_upload')
        original_factory = webui.Request._get_file_stream
        streams = []

        def track_stream(request_object, *args, **kwargs):
            stream = original_factory(request_object, *args, **kwargs)
            streams.append(stream)
            return stream

        for path in ('/api/media/upload', '/api/v1/media/upload'):
            for streamed in (False, True):
                with self.subTest(path=path, streamed=streamed):
                    streams.clear()
                    with patch.object(webui.Request, '_get_file_stream', autospec=True, side_effect=track_stream):
                        if streamed:
                            self.cfg['api_upload_max_request_bytes'] = 1024
                            body = (b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="test.png"\r\n'
                                    b'Content-Type: image/png\r\n\r\n' + b'x' * 2048 + b'\r\n--boundary--\r\n')
                            response = self.client.post(path, data=body, headers=self.headers,
                                                        content_type='multipart/form-data; boundary=boundary',
                                                        environ_overrides={'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
                        else:
                            self.cfg['api_upload_max_request_bytes'] = 4 * 1024 * 1024
                            # Send the file first: Werkzeug's test builder
                            # normally places text parts before all files.
                            body = (b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="test.png"\r\n'
                                    b'Content-Type: image/png\r\n\r\n' + _image().getvalue() + b'\r\n')
                            for index in range(4):
                                body += (f'--boundary\r\nContent-Disposition: form-data; name="padding{index}"\r\n\r\nx\r\n').encode()
                            body += b'--boundary--\r\n'
                            response = self.client.post(path, data=body, headers=self.headers,
                                                        content_type='multipart/form-data; boundary=boundary')
                    self.assertEqual(response.status_code, 413)
                    self.assertTrue(streams, 'The parser must have created a partial file before rejecting the body')
                    self.assertTrue(all(stream.closed for stream in streams))

    def test_upload_grant_creates_temporary_images_but_only_library_managers_can_edit_or_delete(self):
        self.grants.add('page:media_upload')
        result = self.client.post('/api/media/upload', data={'file': (_image(), 'event.png'), 'name': "Mother's Day"},
                                  headers=self.headers)
        self.assertEqual(result.status_code, 201)
        item = result.get_json()['item']
        self.assertEqual(item['name'], "Mother's Day")
        self.assertFalse(item['preset'])
        self.assertEqual(self.client.patch('/api/media/' + item['id'], json={'preset': True},
                                           headers=self.headers).status_code, 403)
        self.assertEqual(self.client.delete('/api/media/' + item['id'], headers=self.headers).status_code, 403)
        # Old saved management grants must not restore operator-side management.
        self.grants.add('page:media_manage')
        for changes in ({'name': 'Renamed'}, {'preset': True}):
            self.assertEqual(self.client.patch('/api/media/' + item['id'], json=changes,
                                               headers=self.headers).status_code, 403)
        self.assertEqual(self.client.delete('/api/media/' + item['id'], headers=self.headers).status_code, 403)
        self.assertEqual(self.library.get(item['id'])['name'], "Mother's Day")
        self.assertFalse(self.library.get(item['id'])['preset'])
        self.grants = {'page:media_library'}
        edited = self.client.patch('/api/media/' + item['id'], json={'preset': True}, headers=self.headers)
        self.assertEqual(edited.status_code, 200)
        self.assertTrue(edited.get_json()['item']['preset'])
        self.assertEqual(self.client.delete('/api/media/' + item['id'], headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(item['thumbnail_url']).status_code, 404)

    def test_invalid_upload_and_edit_are_rejected(self):
        self.grants.update({'page:config', 'page:media_library'})
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
        self.grants.update({'page:config', 'page:media_library'})
        self.cfg.update(atem_media_enabled=True, atem_media_destinations=[
            {'player': 2, 'label': 'Test only', 'slots': [41, 42]},
        ])
        page = self.client.get('/media-library')
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="media-load-button"', page.get_data(as_text=True))
        result = self.client.post('/api/atem/media/load', json={'media_id': self.item['id'], 'player': 2}, headers=self.headers)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.get_json()['job']['status'], 'queued')
        self.assertEqual(self.manager.load.call_args.args, (self.item['id'], 2))
        self.assertEqual(set(self.manager.load.call_args.kwargs), {'on_complete'})
        callback = self.manager.load.call_args.kwargs['on_complete']
        callback({**self.manager.load.return_value, 'status': 'succeeded', 'slot': 5})
        event = self.events.call_args
        self.assertEqual(event.args[0], 'atem.media.load')
        self.assertEqual(event.kwargs['actor_user_id'], 42)
        self.assertEqual(event.kwargs['actor_username'], 'media-operator')
        self.assertEqual(event.kwargs['status'], 'success')

    def test_unknown_image_never_reaches_hardware(self):
        self.grants.update({'page:config', 'page:media_library'})
        result = self.client.post('/api/atem/media/load', json={'media_id': 'f' * 32, 'player': 2}, headers=self.headers)
        self.assertEqual(result.status_code, 404)
        self.manager.load.assert_not_called()

    def test_busy_job_cannot_be_deleted_or_reconfigured(self):
        self.grants.update({'page:config', 'page:media_library'})
        self.manager.snapshot.return_value['job'] = {'mediaId': self.item['id'], 'status': 'uploading'}
        self.assertEqual(self.client.delete('/api/media/' + self.item['id'], headers=self.headers).status_code, 409)
        self.assertEqual(self.client.put('/api/config/atem-media', json={'atem_media_enabled': True}, headers=self.headers).status_code, 409)
        self.assertFalse(self.config_file.exists())

    def test_media_setup_requires_config_grant_and_validates_reservations(self):
        self.assertEqual(self.client.get('/api/config/atem-media').status_code, 403)
        self.grants.add('page:config')
        payload = {'atem_media_enabled': True, 'atem_media_destinations': [
            {'player': 2, 'label': 'Foyer', 'slots': [41, 42], 'videohub_input': 5},
            {'player': 4, 'label': 'Kids', 'slots': [43, 44], 'videohub_input': 6},
        ]}
        response = self.client.put('/api/config/atem-media', json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = json.loads(self.config_file.read_text())
        self.assertEqual(saved['atem_ip'], '192.0.2.1')
        self.assertEqual(saved['atem_media_destinations'][1]['player'], 4)
        self.assertNotIn('aux', saved['atem_media_destinations'][1])
        self.assertEqual(saved['atem_media_destinations'][1]['videohub_input'], 6)
        payload['atem_media_destinations'][1]['slots'] = [42, 43]
        self.assertEqual(self.client.put('/api/config/atem-media', json=payload, headers=self.headers).status_code, 400)

    def test_saving_legacy_setup_preserves_inputs_and_discards_auxes(self):
        self.grants.add('page:config')
        self.cfg.update(atem_media_enabled=True, atem_media_destinations=[
            {'player': 2, 'label': 'Foyer', 'slots': [41, 42], 'aux': 1, 'videohub_input': 5},
            {'player': 4, 'label': 'Kids', 'slots': [43, 44], 'aux': 1, 'videohub_input': 6},
        ])
        current = self.client.get('/api/config/atem-media')
        self.assertEqual(current.status_code, 200)
        destinations = current.get_json()['config']['atem_media_destinations']
        self.assertEqual([item['videohub_input'] for item in destinations], [5, 6])
        self.assertTrue(all('aux' not in item for item in destinations))
        self.assertEqual(self.cfg['atem_media_destinations'][0]['aux'], 1)
        self.assertFalse(self.config_file.exists())
        response = self.client.put('/api/config/atem-media', json={'atem_media_enabled': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = json.loads(self.config_file.read_text())['atem_media_destinations']
        self.assertEqual([item['videohub_input'] for item in saved], [5, 6])
        self.assertTrue(all('aux' not in item for item in saved))

    def test_service_tokens_and_scheduler_cannot_use_new_media_apis(self):
        token = api_security.create_service_token(self.db, name='ATEM', scopes=['atem', 'config'])['token']
        for method, path in (('get', '/api/media'), ('get', '/api/atem/media/state'),
                             ('post', '/api/atem/media/load'), ('put', '/api/config/atem-media'),
                             ('post', '/api/media/upload'), ('post', '/api/v1/media/upload'),
                             ('post', '/api/media/display'), ('post', '/api/v1/media/display'),
                             ('get', '/api/media/display/' + self.display_job['id'])):
            result = getattr(self.client, method)(path, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(result.status_code, 403)
        for path in ('/api/atem/media/load', '/api/media/display', '/api/v1/media/display'):
            with webui.app.test_request_context(path, method='POST'):
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
        self.grants.update({'page:config', 'page:media_library'})
        self.cfg['atem_media_destinations'] = 'invalid imported value'
        with patch.object(webui, '_get_atem_media_manager', side_effect=AssertionError('Must not construct a manager')):
            response = self.client.put('/api/config/atem-media', json={'atem_media_destinations': []}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.delete('/api/media/' + self.item['id'], headers=self.headers).status_code, 200)

    def test_config_access_is_separate_from_library_management(self):
        self.grants = {'page:config'}
        page = self.client.get('/config/atem-media')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('id="media-grid"', page.get_data(as_text=True))
        self.assertNotIn('id="media-load-button"', page.get_data(as_text=True))
        self.assertEqual(self.client.get('/media-library').status_code, 403)
        self.assertEqual(self.client.get('/api/media').status_code, 200)  # preset image selector
        self.assertEqual(self.client.post('/api/media/upload', data={'file': (_image(), 'photo.png')}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.delete('/api/media/' + self.item['id'], headers=self.headers).status_code, 403)
        self.grants = {'page:media_library'}
        page = self.client.get('/media-library')
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="media-load-button"', page.get_data(as_text=True))
        self.assertEqual(self.client.get('/config/atem-media').status_code, 403)
        self.assertEqual(self.client.get('/api/config/atem-media').status_code, 403)
        self.assertEqual(self.client.get('/api/atem/media/state').status_code, 200)
        upload = self.client.post('/api/media/upload', data={'file': (_image(), 'photo.png'), 'temporary': 'false'}, headers=self.headers)
        self.assertEqual(upload.status_code, 201)
        self.assertFalse(upload.get_json()['item']['temporary'])
        self.assertEqual(self.client.post('/api/atem/media/load', json={'media_id': self.item['id'], 'player': 2}, headers=self.headers).status_code, 202)


    def test_nonadmin_cannot_change_server_executable(self):
        self.grants.add('page:config')
        with patch.object(webui, '_can_manage_service_tokens_for_current_user', return_value=False):
            response = self.client.put('/api/config/atem-media', json={'atem_media_node_path': 'C:/untrusted.exe'}, headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.config_file.exists())

    def _allow_display(self):
        self.grants.update({'page:routing', 'page:media'})
        self.cfg.update(atem_media_enabled=True, atem_media_destinations=[
            {'player': 2, 'label': 'A', 'slots': [41, 42], 'videohub_input': 5},
            {'player': 4, 'label': 'B', 'slots': [43, 44], 'videohub_input': 6},
        ])

    def _post_display(self, **overrides):
        return self.client.post('/api/media/display', json={'media_id': self.item['id'], 'output': 1, **overrides},
                                headers=self.headers)

    def test_operator_cannot_use_raw_player_load_or_detailed_status(self):
        self._allow_display()
        self.assertEqual(self.client.get('/api/atem/media/state').status_code, 403)
        self.assertEqual(self.client.post('/api/atem/media/load', json={'media_id': self.item['id'], 'player': 2},
                                          headers=self.headers).status_code, 403)
        self.manager.load.assert_not_called()
        self.assertEqual(self._post_display().status_code, 202)

    def test_display_requires_routing_and_media_for_writes_and_job_reads(self):
        self._allow_display()
        required = {'page:routing', 'page:media'}
        for missing in required:
            with self.subTest(missing=missing):
                self.grants = (required - {missing}) | {'page:media_load', 'page:media_manage'}
                self.assertEqual(self._post_display().status_code, 403)
                self.assertEqual(self.client.get('/api/media/display/' + self.display_job['id']).status_code, 403)
        self.routing.display.assert_not_called()
        self.routing.get_job.assert_not_called()

    def test_media_and_routing_select_from_pool_without_upload_or_legacy_load_grants(self):
        self._allow_display()
        self.assertEqual(self.grants, {'page:media', 'page:routing'})
        response = self.client.get('/media?output=1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-can-display="true"', response.get_data(as_text=True))
        self.assertNotIn('href="/media/upload', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/media').status_code, 200)
        self.assertEqual(self._post_display().status_code, 202)
        self.assertEqual(self.client.get('/api/media/display/' + self.display_job['id']).status_code, 200)
        self.assertEqual(self.client.get('/media/upload?output=1').status_code, 403)
        upload = self.client.post('/api/media/upload', data={'file': (_image(), 'test.png')}, headers=self.headers)
        self.assertEqual(upload.status_code, 403)
        self.assertEqual(self.client.delete('/api/media/' + self.item['id'], headers=self.headers).status_code, 403)
        self.assertEqual(len(self.library.list()), 1)

    def test_config_management_does_not_add_upload_controls_to_operator_page(self):
        self._allow_display()
        self.grants.add('page:config')
        page = self.client.get('/media?output=1')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('id="media-upload-link"', page.get_data(as_text=True))
        self.assertEqual(self.client.get('/media/upload?output=1').status_code, 403)
        self.grants.add('page:media_upload')
        page = self.client.get('/media?output=1')
        self.assertIn('id="media-upload-link"', page.get_data(as_text=True))
        self.assertEqual(self.client.get('/media/upload?output=1').status_code, 200)

    def test_config_with_legacy_load_cannot_test_players_without_media_permission(self):
        self.grants = {'page:config', 'page:media_load', 'page:media_manage'}
        self.assertEqual(self.client.post('/api/atem/media/load', json={'media_id': self.item['id'], 'player': 2},
                                          headers=self.headers).status_code, 403)
        self.manager.load.assert_not_called()

    def test_display_checks_output_and_mapped_input_before_enqueueing(self):
        self._allow_display()
        self.allowed_outputs = [2]
        self.assertEqual(self._post_display().status_code, 403)
        self.routing.display.assert_not_called()
        self.allowed_outputs = [1]
        self.allowed_inputs = [1, 2]
        self.assertEqual(self._post_display().status_code, 403)
        self.routing.display.assert_not_called()
        self.allowed_inputs = [6]
        self.assertEqual(self._post_display().status_code, 202)
        self.assertEqual(self.routing.display.call_args.args, (self.item['id'], 1))
        self.assertEqual(self.routing.display.call_args.kwargs['allowed_inputs'], [6])
        self.assertNotIn('allow_shared_player', self.routing.display.call_args.kwargs)

    def test_display_accepts_only_saved_image_and_target_not_player_overrides(self):
        self._allow_display()
        for output in (True, 0, -1, 1.5, '01', '1.0', [], None):
            with self.subTest(output=output):
                self.assertEqual(self._post_display(output=output).status_code, 400)
        self.assertEqual(self._post_display(media_id='f' * 32).status_code, 404)
        for extra in ({'player': 4}, {'input': 6}, {'aux': 2}, {'allow_shared_player': True}):
            self.assertEqual(self._post_display(**extra).status_code, 400)
        self.routing.display.assert_not_called()

    def test_display_requires_csrf_and_same_origin(self):
        self._allow_display()
        for headers in ({}, {**self.headers, 'Origin': 'https://untrusted.invalid'}):
            result = self.client.post('/api/media/display', json={'media_id': self.item['id'], 'output': 1},
                                      headers=headers)
            self.assertEqual(result.status_code, 403)
        self.routing.display.assert_not_called()

    def test_display_returns_compact_job_and_preserves_initiator_for_terminal_log(self):
        self._allow_display()
        result = self._post_display()
        self.assertEqual(result.status_code, 202)
        job = result.get_json()['job']
        self.assertEqual(set(job), {'id', 'mediaId', 'output', 'status', 'message', 'error'})
        self.assertEqual(job['status'], 'queued')
        self.assertNotIn('Private device diagnostic', result.get_data(as_text=True))
        callback = self.routing.display.call_args.kwargs['on_complete']
        callback({**self.display_job, 'status': 'succeeded'})
        event = self.events.call_args
        self.assertEqual(event.args[0], 'media.display')
        self.assertEqual(event.kwargs['actor_user_id'], 42)
        self.assertEqual(event.kwargs['actor_username'], 'media-operator')
        self.assertEqual(event.kwargs['target_id'], 1)
        self.assertEqual(event.kwargs['status'], 'success')
        self.assertEqual(event.kwargs['details']['player'], 2)

    def test_display_alias_has_identical_permissions_and_compact_contract(self):
        self._allow_display()
        result = self.client.post('/api/v1/media/display', json={'media_id': self.item['id'], 'output': 1},
                                  headers=self.headers)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.headers['X-TDeck-API-Version'], '1')
        read = self.client.get('/api/v1/media/display/' + self.display_job['id'])
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.get_json()['job'], result.get_json()['job'])

    def test_job_read_hides_diagnostics_and_rechecks_current_output_access(self):
        self._allow_display()
        self.routing.get_job.return_value.update(status='failed', error='The TV did not confirm the image.')
        result = self.client.get('/api/media/display/' + self.display_job['id'])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['job']['status'], 'failed')
        self.assertEqual(set(result.get_json()['job']), {'id', 'mediaId', 'output', 'status', 'message', 'error'})
        self.assertNotIn('Private device diagnostic', result.get_data(as_text=True))
        self.allowed_outputs = [2]
        self.assertEqual(self.client.get('/api/media/display/' + self.display_job['id']).status_code, 403)
        self.routing.get_job.side_effect = KeyError('expired')
        self.assertEqual(self.client.get('/api/media/display/' + 'b' * 32).status_code, 404)

    def test_all_active_display_stages_block_reconfiguration_and_image_deletion(self):
        self._allow_display()
        self.grants.update({'page:config', 'page:media_library'})
        for stage in ('queued', 'preparing', 'loading', 'routing'):
            with self.subTest(stage=stage):
                self.routing.active_job.return_value = {**self.display_job, 'status': stage}
                self.assertEqual(self.client.delete('/api/media/' + self.item['id'], headers=self.headers).status_code, 409)
                self.assertEqual(self.client.put('/api/config/atem-media', json={'atem_media_enabled': False},
                                                 headers=self.headers).status_code, 409)
                self.assertEqual(self.client.post('/api/config', json={'atem_ip': '192.0.2.3'},
                                                  headers=self.headers).status_code, 409)
                self.assertEqual(self._post_display().status_code, 409)
                self.assertEqual(self.client.post('/api/atem/media/load', json={'media_id': self.item['id'], 'player': 2},
                                                  headers=self.headers).status_code, 409)
        self.assertFalse(self.config_file.exists())
        self.assertEqual(len(self.library.list()), 1)
        self.manager.load.assert_not_called()
        self.routing.display.assert_not_called()

    def test_active_job_helper_prioritizes_display_after_atem_upload_finished(self):
        import atem_media
        self.routing.active_job.return_value = {**self.display_job, 'status': 'routing'}
        with (patch.object(webui, '_media_routing_instance', self.routing),
              patch.object(atem_media, 'peek_atem_media_job', return_value={'status': 'succeeded'}) as peek):
            self.assertEqual(self.real_active_media_job()['status'], 'routing')
            peek.assert_not_called()
            self.routing.active_job.return_value = None
            self.assertEqual(self.real_active_media_job()['status'], 'succeeded')
            peek.assert_called_once_with()

    def test_enqueue_conflict_returns_busy_without_claiming_success(self):
        from atem_media import BusyError
        self._allow_display()
        self.routing.display.side_effect = BusyError('Another image is being displayed.')
        response = self._post_display()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()['ok'])
        self.assertNotIn('job', response.get_json())
        self.assertFalse(any(call.args[0] == 'media.display.queued' for call in self.events.call_args_list))

    def test_active_display_does_not_block_unrelated_library_work(self):
        self.grants = {'page:media_library'}
        self.routing.active_job.return_value = {**self.display_job, 'status': 'routing'}
        upload = self.client.post('/api/media/upload', data={'file': (_image(), 'next-event.png')}, headers=self.headers)
        self.assertEqual(upload.status_code, 201)
        next_id = upload.get_json()['item']['id']
        self.assertEqual(self.client.delete('/api/media/' + next_id, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get('/api/media').status_code, 200)

    def test_global_display_reservation_blocks_direct_routes_presets_and_raw_load(self):
        from media_routing import video_routing_guard
        self._allow_display()
        self.grants.update({'page:config', 'page:videohub'})
        with (patch.object(webui, '_get_videohub_client_from_config') as hardware,
              patch.object(webui, '_get_videohub_app') as backend,
              video_routing_guard()):
            for path, body in (
                ('/api/videohub/route', {'output': 1, 'input': 5}),
                ('/api/videohub/presets/1/apply', {}),
                ('/api/atem/media/load', {'media_id': self.item['id'], 'player': 2}),
            ):
                with self.subTest(path=path):
                    result = self.client.post(path, json=body, headers=self.headers)
                    self.assertEqual(result.status_code, 409, result.get_data(as_text=True))
                    self.assertEqual(result.get_json()['error'], 'busy')
            hardware.assert_not_called()
            backend.assert_not_called()
            self.manager.load.assert_not_called()

    def test_picker_and_upload_require_valid_output_access_without_hardware_calls(self):
        self._allow_display()
        self.grants.add('page:media_upload')
        self.allowed_outputs = [1]
        for path in ('/media', '/media/upload'):
            self.assertEqual(self.client.get(path + '?output=2').status_code, 403)
            self.assertEqual(self.client.get(path + '?output=bad').status_code, 400)
            self.assertEqual(self.client.get(path + '?output=1').status_code, 200)
        self.grants.remove('page:routing')
        self.assertEqual(self.client.get('/media?output=1').status_code, 403)
        self.grants.remove('page:media_upload')
        self.assertEqual(self.client.get('/media/upload').status_code, 403)
        self.manager.load.assert_not_called()
        self.routing.display.assert_not_called()

    def test_upload_only_saves_library_item_until_display_is_requested(self):
        self._allow_display()
        self.grants.add('page:media_upload')
        response = self.client.post('/api/media/upload', data={'file': (_image(), 'new-photo.png')}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        self.routing.display.assert_not_called()
        self.manager.load.assert_not_called()

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
