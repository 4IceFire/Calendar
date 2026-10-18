"""Replacement/repair regressions: local storage only, never hardware."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_library
from media_library import MediaLibrary
import test_media_library as library_fixtures
import test_media_webui as web_fixtures
import webui


class MediaReplaceWebTests(unittest.TestCase):
    setUp = web_fixtures.MediaWebTests.setUp

    def test_asset_flags_are_derived_and_delete_still_returns_200(self):
        self.grants = {'page:media_library'}
        for thumbnail in (False, True):
            self.library.path(self.item['id'], thumbnail).unlink()
            item = self.client.get('/api/media').get_json()['items'][0]
            self.assertTrue(item['thumbnail_missing'] if thumbnail else item['file_missing'])
            self.assertNotIn(str(self.library.root), str(item))
        response = self.client.delete('/api/media/' + self.item['id'], headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['item']['file_missing'])
        self.assertTrue(response.get_json()['item']['thumbnail_missing'])

    def test_catalog_payloads_use_bounded_index_reads_and_correct_flags(self):
        self.grants = {'page:media_library', 'page:config'}
        items = [self.item]
        for count in (1, 8, 32):
            while len(items) < count:
                items.append(self.library.upload(web_fixtures._image(), 'extra.png'))
            self.library._asset_path(items[0]['id']).unlink(missing_ok=True)
            self.library._asset_path(items[-1]['id'], True).unlink(missing_ok=True)
            for prefix in ('/api', '/api/v1'):
                for endpoint, field in (('/media', 'items'), ('/config/routing-presets', 'images')):
                    with self.subTest(count=count, prefix=prefix, endpoint=endpoint):
                        with patch.object(self.library, '_read', wraps=self.library._read) as read:
                            response = self.client.get(prefix + endpoint)
                        self.assertEqual(response.status_code, 200, response.get_json())
                        payload = response.get_json()[field]
                        self.assertEqual(len(payload), count)
                        # Cleanup may read routing references plus expiry; the
                        # catalogue itself must not add one read per image.
                        self.assertLessEqual(read.call_count, 3)
                        for item in payload:
                            self.assertEqual(item['file_missing'], not self.library._asset_path(item['id']).is_file())
                            self.assertEqual(item['thumbnail_missing'], not self.library._asset_path(item['id'], True).is_file())

    def test_replace_committed_cleanup_failures_return_success_and_keep_backups(self):
        self.grants = {'page:media_library'}
        unlink = Path.unlink
        for target in ('index', 'images', 'thumbnails'):
            with self.subTest(target=target):
                retained = []

                def fail_cleanup(path, *args, **kwargs):
                    if '.replace-' in path.name and (path.stem.startswith('index.') if target == 'index'
                                                     else path.parent.name == target):
                        retained.append(path)
                        raise OSError('/private/path secret=bytes')
                    return unlink(path, *args, **kwargs)

                with patch.object(Path, 'unlink', fail_cleanup), self.assertLogs('media_library', level='WARNING') as logs:
                    response = self.client.post('/api/media/' + self.item['id'] + '/replace',
                                                data={'file': (io.BytesIO(library_fixtures.image_bytes(size=(33, 22))), 'new.png')},
                                                headers=self.headers)
                    self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(self.library.get(self.item['id'])['width'], 33)
                self.assertFalse(response.get_json()['item']['file_missing'])
                self.assertFalse(response.get_json()['item']['thumbnail_missing'])
                self.assertTrue(retained)
                self.assertTrue(all(path.is_file() for path in retained))
                self.assertNotIn('/private/path', str(logs.output))
                self.assertNotIn('secret=bytes', str(logs.output))
                self.assertNotIn(str(self.library.root), str(logs.output))
                for path in retained:
                    path.unlink()

    def test_replacement_blocks_any_active_job_without_hardware_factories(self):
        import atem_media
        self.grants = {'page:media_library'}
        with (patch.object(webui, '_active_media_job', side_effect=self.real_active_media_job),
              patch.object(webui, '_get_atem_media_manager', side_effect=AssertionError('No hardware factory')),
              patch.object(webui, '_get_media_routing_manager', side_effect=AssertionError('No routing factory')),
              patch.object(webui, '_media_routing_instance', None),
              patch.object(webui._routing_preset_runner, 'active_job', return_value=None),
              patch.object(atem_media, 'peek_atem_media_job') as peek):
            for stage in sorted(webui._MEDIA_ACTIVE_JOBS):
                peek.return_value = {'status': stage, 'mediaId': 'f' * 32}
                response = self.client.post('/api/media/' + self.item['id'] + '/replace',
                                            data={'file': (web_fixtures._image(), 'new.png')}, headers=self.headers)
                self.assertEqual(response.status_code, 409, stage)
            peek.return_value = None
            response = self.client.post('/api/media/' + self.item['id'] + '/replace',
                                        data={'file': (web_fixtures._image(), 'new.png')}, headers=self.headers)
            self.assertEqual(response.status_code, 200)

    def test_replacement_shares_upload_admission_and_rate_limits(self):
        self.grants = {'page:media_library'}
        path = '/api/media/' + self.item['id'] + '/replace'
        with patch.object(webui, '_MEDIA_UPLOADS_PER_MINUTE', 2):
            self.assertEqual(self.client.post('/api/media/upload', data={'file': (web_fixtures._image(), 'new.png')},
                                              headers=self.headers).status_code, 201)
            self.assertEqual(self.client.post(path, data={'file': (web_fixtures._image(), 'new.png')},
                                              headers=self.headers).status_code, 200)
            with patch.object(webui.app.request_class, '_load_form_data') as parse:
                self.assertEqual(self.client.post(path.replace('/api/', '/api/v1/'),
                                                  data={'file': (web_fixtures._image(), 'new.png')},
                                                  headers=self.headers).status_code, 429)
                parse.assert_not_called()
        with webui._media_upload_rate_lock:
            webui._media_upload_rate_events.clear()
        self.assertTrue(webui._media_upload_slot.acquire(blocking=False))
        try:
            with patch.object(webui.app.request_class, '_load_form_data') as parse:
                for headers, extra in ((self.headers, {}), ({'Origin': 'http://localhost'}, {'_csrf': 'media-csrf'})):
                    response = self.client.post(path, data={'file': (web_fixtures._image(), 'new.png'), **extra}, headers=headers)
                    self.assertEqual(response.status_code, 429)
                parse.assert_not_called()
        finally:
            webui._media_upload_slot.release()
        self.assertEqual(self.client.post(path, data={'file': (web_fixtures._image(), 'new.png')}, headers=self.headers).status_code, 200)

    def test_replacement_stream_limits_and_partial_stream_cleanup(self):
        self.grants = {'page:media_library'}
        original_factory = webui.Request._get_file_stream
        streams = []

        def track(request_object, *args, **kwargs):
            stream = original_factory(request_object, *args, **kwargs)
            streams.append(stream)
            return stream

        for prefix in ('/api', '/api/v1'):
            path = prefix + '/media/' + self.item['id'] + '/replace'
            self.cfg['api_upload_max_request_bytes'] = 1024
            body = (b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\n'
                    b'Content-Type: image/png\r\n\r\n' + b'x' * 2048 + b'\r\n--boundary--\r\n')
            for length in (True, False):
                streams.clear()
                with patch.object(webui.Request, '_get_file_stream', autospec=True, side_effect=track):
                    response = self.client.post(path, data=body, headers=self.headers,
                                                content_type='multipart/form-data; boundary=boundary',
                                                environ_overrides={} if length else {'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
                self.assertEqual(response.status_code, 413)
                self.assertTrue(all(stream.closed for stream in streams))
                if not length:
                    self.assertTrue(streams)
            # A third multipart part is rejected after opening the first file.
            self.cfg['api_upload_max_request_bytes'] = 4 * 1024 * 1024
            body = (b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\n\r\n' +
                    web_fixtures._image().getvalue() + b'\r\n--boundary\r\nContent-Disposition: form-data; name="_csrf"\r\n\r\nmedia-csrf\r\n'
                    b'--boundary\r\nContent-Disposition: form-data; name="extra"\r\n\r\nx\r\n--boundary--\r\n')
            streams.clear()
            with patch.object(webui.Request, '_get_file_stream', autospec=True, side_effect=track):
                response = self.client.post(path, data=body, headers=self.headers, content_type='multipart/form-data; boundary=boundary')
            self.assertEqual(response.status_code, 413)
            self.assertTrue(streams)
            self.assertTrue(all(stream.closed for stream in streams))

    def test_replace_failure_activity_never_exposes_paths_or_payload(self):
        self.grants = {'page:media_library'}
        with patch.object(self.library, 'replace', side_effect=OSError('/private/path secret=bytes')):
            response = self.client.post('/api/media/' + self.item['id'] + '/replace',
                                        data={'file': (web_fixtures._image(), 'secret_filename.png')}, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        failures = [call for call in self.events.call_args_list if call.args[0] == 'media.image.replace']
        self.assertTrue(failures)
        self.assertEqual(failures[-1].kwargs['status'], 'failure')
        self.assertNotIn('/private/path', str(failures))
        self.assertNotIn('secret_filename', str(failures))
        self.assertNotIn('secret=bytes', str(failures))

    def test_replace_accepts_only_one_file_and_optional_form_csrf(self):
        from werkzeug.datastructures import MultiDict
        self.grants = {'page:media_library'}
        path = '/api/media/' + self.item['id'] + '/replace'
        before = self.library.get(self.item['id'])
        for extra in ([('name', 'spoof')], [('temporary', 'false')], [('expires_at', 'spoof')],
                      [('uploaded_by', 'spoof')], [('preset', 'true')], [('other', 'x')],
                      [('file', (web_fixtures._image(), 'second.png'))],
                      [('_csrf', 'media-csrf'), ('_csrf', 'media-csrf')]):
            with self.subTest(extra=extra):
                response = self.client.post(path, data=MultiDict([('file', (web_fixtures._image(), 'new.png')), *extra]),
                                            headers=self.headers)
                self.assertIn(response.status_code, (400, 413))
                self.assertEqual(self.library.get(self.item['id']), before)
        response = self.client.post(path, data={'file': (web_fixtures._image(), 'new.png'), '_csrf': 'media-csrf'},
                                    headers={'Origin': 'http://localhost'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post(path, json={'file': 'fake'}, headers=self.headers).status_code, 400)

    def test_replace_rejects_readonly_origin_and_bad_header_csrf_before_body_parse(self):
        for prefix in ('/api', '/api/v1'):
            path = prefix + '/media/' + self.item['id'] + '/replace'
            for grants, headers in (
                ({'page:config'}, self.headers),
                ({'page:routing', 'page:media', 'page:media_upload', 'page:media_save'}, self.headers),
                ({'page:media_library'}, {**self.headers, 'Origin': 'https://evil.invalid'}),
                ({'page:media_library'}, {**self.headers, 'X-CSRF-Token': 'wrong'}),
            ):
                self.grants = grants
                with self.subTest(prefix=prefix, grants=grants, headers=headers):
                    with patch.object(webui.app.request_class, '_load_form_data',
                                      side_effect=AssertionError('Denied before parsing')) as parse:
                        response = self.client.post(path, data={'file': (web_fixtures._image(), 'new.png')},
                                                    headers=headers)
                    parse.assert_not_called()
                    self.assertEqual(response.status_code, 403)

    def test_replace_endpoint_repairs_missing_assets_and_get_returns_new_pixels(self):
        self.grants = {'page:media_library'}
        original = self.library.get(self.item['id'])
        for thumbnail in (False, True):
            self.library.path(self.item['id'], thumbnail).unlink()
        for prefix in ('/api', '/api/v1'):
            response = self.client.post(prefix + '/media/' + self.item['id'] + '/replace',
                                        data={'file': (io.BytesIO(library_fixtures.image_bytes(size=(33, 22))), 'new.png')},
                                        headers=self.headers)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            payload = response.get_json()
            self.assertTrue(payload['ok'])
            item = payload['item']
            self.assertEqual(item['id'], self.item['id'])
            self.assertEqual(item['name'], original['name'])
            self.assertEqual(item['created_at'], original['created_at'])
            self.assertFalse(item['file_missing'])
            self.assertFalse(item['thumbnail_missing'])
            self.assertEqual(len(self.library.list()), 1)
            if prefix == '/api/v1':
                self.assertEqual(response.headers['X-TDeck-API-Version'], '1')
            with self.client.get(item['url']) as image:
                from PIL import Image
                with Image.open(io.BytesIO(image.data)) as decoded:
                    self.assertEqual(decoded.size, (33, 22))
        self.assertTrue(any(call.args[0] == 'media.image.replace' and call.kwargs['status'] == 'success'
                            for call in self.events.call_args_list))
        self.manager.load.assert_not_called()
        self.routing.display.assert_not_called()



class MediaReplaceAuthTests(unittest.TestCase):
    """Real Flask-Login user loading, DB grants and session validation."""

    def setUp(self):
        from contextlib import ExitStack
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'auth.db'
        self.library = MediaLibrary(self.root / 'library')
        self.item = self.library.upload(web_fixtures._image(), 'original.png')
        self.cfg = {'auth_enabled': True, 'auth_idle_timeout_enabled': False,
                    'api_legacy_anonymous_enabled': False, 'api_write_rate_limit_per_minute': 600}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for context in (patch.object(webui, '_AUTH_DB_PATH', self.db),
                        patch.object(webui, '_auth_cfg', return_value=self.cfg),
                        patch.object(webui.utils, 'get_config', return_value=self.cfg),
                        patch.object(webui, '_bootstrap_default_users_roles'),
                        patch.object(webui, '_get_media_library', return_value=self.library),
                        patch.object(webui, '_active_media_job', return_value={}),
                        patch.object(webui, 'log_event')):
            self.stack.enter_context(context)
        webui._init_auth_db()
        conn = webui._db()
        try:
            conn.execute("INSERT INTO groups(id,name,is_admin) VALUES (21,'Media fixture',0)")
            conn.execute("INSERT INTO users(id,username,password_hash) VALUES (21,'replace-fixture','unused')")
            conn.execute('INSERT INTO user_groups(user_id,group_id) VALUES (21,21)')
            conn.commit()
        finally:
            conn.close()
        self.client = webui.app.test_client()
        self.headers = {'X-CSRF-Token': 'real-csrf', 'Origin': 'http://localhost'}
        self.path = '/api/media/' + self.item['id'] + '/replace'
        self.login()
        with webui._media_upload_rate_lock:
            webui._media_upload_rate_events.clear()
        with webui._api_rate_lock:
            webui._api_rate_events.clear()

    def login(self):
        with self.client.session_transaction() as state:
            state.clear()
            state.update(_user_id='21', _fresh=True, _csrf='real-csrf')

    def test_database_permissions_and_csrf_origin_enforce_before_parsing(self):
        for pages in (['page:config'], ['page:routing', 'page:media', 'page:media_upload'], ['page:media_library']):
            webui._set_group_pages(21, pages)
            self.login()
            for headers in ({}, {**self.headers, 'Origin': 'https://evil.invalid'},
                            {**self.headers, 'X-CSRF-Token': 'wrong'}):
                with patch.object(webui.app.request_class, '_load_form_data') as parse:
                    response = self.client.post(self.path, data={'file': (web_fixtures._image(), 'new.png')}, headers=headers)
                    # Missing header/form CSRF needs bounded parsing only for an
                    # otherwise authorized manager with a trusted origin.
                    parse.assert_not_called()
                self.assertEqual(response.status_code, 403)
            response = self.client.post(self.path, data={'file': (web_fixtures._image(), 'new.png')}, headers=self.headers)
            self.assertEqual(response.status_code, 200 if pages == ['page:media_library'] else 403)
        with self.client.session_transaction() as state:
            sid = state['_auth_session_id']
        conn = webui._db()
        conn.execute("UPDATE user_sessions SET revoked_at='2026-01-01' WHERE id=?", (sid,))
        conn.commit(); conn.close()
        with patch.object(webui.app.request_class, '_load_form_data') as parse:
            response = self.client.post(self.path, data={'file': (web_fixtures._image(), 'new.png')}, headers=self.headers)
            parse.assert_not_called()
        self.assertEqual(response.status_code, 401)

    def test_tokens_scheduler_and_signed_out_are_denied_before_parsing(self):
        import api_security
        token = api_security.create_service_token(self.db, name='Fixture', scopes=['*'])['token']
        for prefix in ('/api', '/api/v1'):
            path = prefix + '/media/' + self.item['id'] + '/replace'
            with patch.object(webui.app.request_class, '_load_form_data') as parse:
                response = self.client.post(path, headers={'Authorization': 'Bearer ' + token},
                                            data={'file': (web_fixtures._image(), 'new.png')})
                self.assertEqual(response.status_code, 403)
                parse.assert_not_called()
            with webui.app.test_request_context(path, method='POST'):
                webui.g._tdeck_scheduler_principal = {'type': 'scheduler'}
                with patch.object(webui.app.request_class, '_load_form_data') as parse:
                    response = webui.app.full_dispatch_request()
                    self.assertEqual(response.status_code, 403)
                    parse.assert_not_called()
        with self.client.session_transaction() as state:
            state.clear()
        with patch.object(webui.app.request_class, '_load_form_data') as parse:
            response = self.client.post(self.path, data={'file': (web_fixtures._image(), 'new.png')}, headers=self.headers)
            self.assertEqual(response.status_code, 401)
            parse.assert_not_called()


class MediaReplaceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = MediaLibrary(Path(self.temp.name) / 'library')

    def _snapshot(self):
        return {str(path.relative_to(self.library.root)): path.read_bytes()
                for path in self.library.root.rglob('*') if path.is_file()}

    def test_replacement_reuses_image_validation_and_leaves_originals_unchanged(self):
        item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'old.png')
        before = self._snapshot()
        for content in (b'', b'not an image', library_fixtures.image_bytes()[:30]):
            with self.subTest(content=content[:8]), self.assertRaises(ValueError):
                self.library.replace(item['id'], io.BytesIO(content))
        for constant, limit, message in (('MAX_UPLOAD_BYTES', 20, 'upload limit'),
                                         ('MAX_IMAGE_PIXELS', 100, 'megapixel'),
                                         ('MAX_DECODED_BYTES', 20, 'memory limit'),
                                         ('MAX_STORED_IMAGE_BYTES', 20, 'Prepared image')):
            with self.subTest(constant=constant), patch.object(media_library, constant, limit):
                with self.assertRaisesRegex(ValueError, message):
                    self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes(size=(30, 20))))
        for image_id in ('../index', 'A' * 32, 'f' * 32, None):
            with self.subTest(image_id=image_id):
                with self.assertRaises(KeyError):
                    self.library.replace(image_id, io.BytesIO(library_fixtures.image_bytes()))
                with self.assertRaises(KeyError):
                    self.library.asset_status(image_id)
        self.assertEqual(self._snapshot(), before)

    def test_replacement_leaves_saved_routing_preset_bytes_and_legacy_provenance_unchanged(self):
        from routing_presets import RoutingPresetStore
        item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'old.png')
        raw = json.loads((self.library.root / 'index.json').read_text())
        for field in ('uploaded_by', 'uploaded_by_id', 'temporary', 'expires_at'):
            raw['images'][0].pop(field)
        (self.library.root / 'index.json').write_text(json.dumps(raw))
        store = RoutingPresetStore(self.library.root)
        preset = store.save({'name': 'Saved reference', 'media_id': item['id'], 'output': 2})
        preset_bytes = (self.library.root / 'routing_presets.json').read_bytes()
        result = self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes(size=(21, 15))))
        self.assertIsNone(result['uploaded_by'])
        self.assertIsNone(result['uploaded_by_id'])
        self.assertFalse(result['temporary'])
        self.assertEqual(store.get(preset['id']), preset)
        self.assertEqual((self.library.root / 'routing_presets.json').read_bytes(), preset_bytes)

    def test_rollback_attempts_independent_restores_and_retains_recovery_backups(self):
        for failed_target in ('images', 'thumbnails', 'index.json'):
            with self.subTest(failed_target=failed_target):
                item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'old.png')
                before = self._snapshot()
                attempted = []

                def fail_restore(operation):
                    def restore(source, destination, *args, **kwargs):
                        if '.replace-' in Path(source).name:
                            attempted.append(Path(destination))
                            if Path(destination).name == failed_target or Path(destination).parent.name == failed_target:
                                raise OSError('/private/restore secret=bytes')
                        return operation(source, destination, *args, **kwargs)
                    return restore

                with (patch.object(self.library, '_save', side_effect=OSError('save failed')),
                      patch.object(media_library.shutil, 'copyfile', side_effect=fail_restore(media_library.shutil.copyfile)),
                      patch.object(media_library.os, 'replace', side_effect=fail_restore(media_library.os.replace))):
                    with self.assertRaisesRegex(OSError, 'recovery backups retained'):
                        self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes(size=(31, 20))))
                self.assertEqual(set(attempted), {self.library._index,
                    self.library._asset_path(item['id']), self.library._asset_path(item['id'], True)})
                backups = list(self.library.root.rglob('*.replace-*.tmp'))
                self.assertEqual(len(backups), 3)
                for backup in backups:
                    original = backup.with_name(backup.name.split('.replace-')[0] +
                                                ('.json' if backup.parent == self.library.root else '.png'))
                    self.assertEqual(backup.read_bytes(), before[str(original.relative_to(self.library.root))])
                    if original.name != failed_target and original.parent.name != failed_target:
                        self.assertEqual(original.read_bytes(), backup.read_bytes())
                # Isolate the next fault case without using a broken catalogue.
                for backup in backups:
                    backup.unlink()

    def test_failure_after_committed_write_rolls_back_index_and_created_assets(self):
        item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'old.png')
        self.library.path(item['id']).unlink()
        before = self._snapshot()
        save = self.library._save

        def late_failure(entries):
            save(entries)
            raise OSError('Failure after index commit')

        with patch.object(self.library, '_save', side_effect=late_failure):
            with self.assertRaises(OSError):
                self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes(size=(31, 20))))
        self.assertEqual(self._snapshot(), before)

    def test_failed_replacement_restores_files_metadata_and_missing_state(self):
        for missing in ((), (False,), (True,), (False, True)):
            for failed_target in ('images', 'thumbnails', 'index.json'):
                with self.subTest(missing=missing, failed_target=failed_target):
                    item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'old.png')
                    for thumbnail in missing:
                        self.library.path(item['id'], thumbnail).unlink()
                    before = self._snapshot()
                    original_write = media_library._atomic_write

                    def fail(path, data):
                        if path.name == failed_target or path.parent.name == failed_target:
                            raise OSError('Write failed')
                        original_write(path, data)

                    with patch.object(media_library, '_atomic_write', side_effect=fail):
                        with self.assertRaises(OSError):
                            self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes(size=(25, 17))))
                    self.assertEqual(self._snapshot(), before)

    def test_replacement_accounts_for_reclaimed_capacity_and_temporary_disk_space(self):
        item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'old.png')
        used = sum(self.library.path(item['id'], thumb).stat().st_size for thumb in (False, True))
        with patch.object(media_library, 'MAX_LIBRARY_BYTES', used):
            self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes()))
        (self.library.root / 'images' / 'orphan').write_bytes(b'xx')
        before = self._snapshot()
        with patch.object(media_library, 'MAX_LIBRARY_BYTES', used + 1):
            with self.assertRaisesRegex(ValueError, 'storage limit'):
                self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes()))
        self.assertEqual(self._snapshot(), before)
        with patch.object(media_library.shutil, 'disk_usage') as usage:
            usage.return_value.free = media_library.MIN_FREE_DISK_BYTES
            with self.assertRaisesRegex(ValueError, 'low on free storage'):
                self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes()))
        self.assertEqual(self._snapshot(), before)

    def test_batch_status_reads_once_under_root_lock_and_standalone_validates_membership(self):
        item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'old.png')
        original_status = self.library._asset_status

        def checked_status(image_id):
            self.assertTrue(self.library._lock._is_owned())
            return original_status(image_id)

        with (patch.object(self.library, '_read', wraps=self.library._read) as read,
              patch.object(self.library, '_asset_status', side_effect=checked_status)):
            items = self.library.list(include_asset_status=True)
        self.assertEqual(read.call_count, 1)
        self.assertFalse(items[0]['file_missing'])
        self.assertFalse(items[0]['thumbnail_missing'])
        self.assertNotIn('file_missing', self.library.get(item['id']))
        # Even existing orphan assets are not a catalogue member.
        self.library._save({})
        self.assertTrue(self.library._asset_path(item['id']).is_file())
        with self.assertRaises(KeyError):
            self.library.asset_status(item['id'])

    def test_replacement_rejects_directory_symlinks_inside_and_outside_root(self):
        item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'old.png')
        for thumbnail in (False, True):
            folder = self.library.root / ('thumbnails' if thumbnail else 'images')
            for inside in (True, False):
                with self.subTest(thumbnail=thumbnail, inside=inside):
                    target = (self.library.root if inside else Path(self.temp.name)) / ('target-' + folder.name)
                    folder.rename(target)
                    before = {path.name: path.read_bytes() for path in target.iterdir()}
                    folder.symlink_to(target, target_is_directory=True)
                    try:
                        for action in (lambda: self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes())),
                                       lambda: self.library.asset_status(item['id']),
                                       lambda: self.library.list(include_asset_status=True),
                                       lambda: self.library.path(item['id'], thumbnail)):
                            with self.assertRaises(OSError):
                                action()
                        self.assertTrue(folder.is_symlink())
                        self.assertEqual({path.name: path.read_bytes() for path in target.iterdir()}, before)
                    finally:
                        folder.unlink()
                        target.rename(folder)

    def test_replacement_rejects_asset_symlinks_even_within_root(self):
        item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'old.png')
        target = self.library.root / 'private'
        target.write_bytes(b'untouched')
        for thumbnail in (False, True):
            path = self.library._asset_path(item['id'], thumbnail)
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(OSError):
                self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes()))
            self.assertEqual(target.read_bytes(), b'untouched')
            self.assertTrue(path.is_symlink())
            path.unlink()

    def test_replace_repairs_assets_at_count_limit_preserving_identity_and_provenance(self):
        for count, missing in enumerate(((), (False,), (True,), (False, True)), 1):
            with self.subTest(missing=missing):
                item = self.library.upload(io.BytesIO(library_fixtures.image_bytes()), 'original.png',
                                           uploaded_by='Original', uploaded_by_id='42', temporary=True)
                item = self.library.update(item['id'], 'Stable name', preset=True)
                for thumbnail in missing:
                    self.library.path(item['id'], thumbnail).unlink()
                with patch.object(media_library, 'MAX_LIBRARY_IMAGES', len(self.library.list())):
                    result = self.library.replace(item['id'], io.BytesIO(library_fixtures.image_bytes(size=(30, 20))))
                self.assertEqual({k: v for k, v in result.items() if k not in ('width', 'height', 'size_bytes')},
                                 {k: v for k, v in item.items() if k not in ('width', 'height', 'size_bytes')})
                self.assertEqual((result['width'], result['height']), (30, 20))
                self.assertEqual(self.library.asset_status(item['id']), {'file_missing': False, 'thumbnail_missing': False})
                self.assertEqual(self.library.get(item['id']), result)
                self.assertEqual(len(self.library.list()), count)
                self.assertNotIn('file_missing', json.loads((self.library.root / 'index.json').read_text())['images'][0])
