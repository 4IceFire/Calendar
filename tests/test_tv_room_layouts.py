"""Room pictures are display metadata, never membership or device actions."""
import unittest
import io
from unittest.mock import patch
from PIL import Image

from tv_fixture import TVFixture


class RoomLayoutTests(unittest.TestCase):
    def setUp(self):
        self.fx = TVFixture().__enter__()
        self.addCleanup(self.fx.__exit__, None, None, None)
        self.admin = self.fx.client('admin-tv')

    def test_save_positions_reload_and_scope_without_commands(self):
        layout = {'ground': {'positions': {'foyer': {'x': .2, 'y': .7}, 'hall': {'x': .8, 'y': .3}}}}
        response = self.admin.put('/api/config/tv-room-layouts', json={'layouts': layout}, headers=self.fx.headers(self.admin))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.admin.get('/api/config/tv-room-layouts').json['layouts'], layout)
        operator = self.fx.client()
        room = operator.get('/api/tv-pages/state').json['rooms'][0]
        self.assertEqual(room['layout']['positions'], {'foyer': {'x': .2, 'y': .7}})
        self.assertNotIn('hall', str(room))
        self.assertTrue(all(not fake.commands for fake in self.fx.clients.values()))
    def test_uploaded_picture_is_validated_scoped_and_explicitly_saved(self):
        import tv_pages
        with patch.object(tv_pages, 'images_dir', return_value=self.fx.root / 'rooms', create=True):
            data = io.BytesIO()
            metadata = Image.Exif(); metadata[0x010E] = 'private fixture location'
            Image.new('RGB', (160, 90), 'navy').save(data, format='JPEG', exif=metadata, icc_profile=b'private fixture profile')
            response = self.admin.post('/api/config/tv-room-layouts/background',
                data={'file': (io.BytesIO(data.getvalue()), '../../room.jpg')}, headers=self.fx.headers(self.admin))
            self.assertEqual(response.status_code, 200, response.data)
            filename = response.json['image']
            self.assertIn(filename, self.admin.get('/api/config/tv-room-layouts').json['images'])
            self.assertNotIn('hisense_room_layouts', self.fx.cfg)
            self.assertTrue((self.fx.root / 'rooms' / filename).read_bytes().startswith(b'\x89PNG'))
            with Image.open(self.fx.root / 'rooms' / filename) as normalized:
                self.assertFalse(normalized.getexif(), 'Uploaded backgrounds must not retain private EXIF metadata')
                self.assertNotIn('icc_profile', normalized.info, 'Room normalization must discard original metadata')
            layout = {'ground': {'background': filename, 'positions': {}}}
            self.assertEqual(self.admin.put('/api/config/tv-room-layouts', json={'layouts': layout}, headers=self.fx.headers(self.admin)).status_code, 200)
            url = '/tv-room-images/ground/' + filename
            operator = self.fx.client()
            response = operator.get(url)
            self.assertEqual(response.status_code, 200)
            response.close()
            self.assertEqual(self.fx.client('empty-tv').get(url).status_code, 403)
            self.assertEqual(operator.get('/tv-room-images/private-room/' + filename).status_code, 403)
            self.assertEqual(self.admin.post('/api/config/tv-room-layouts/background', data={'file': (io.BytesIO(b'<svg/>'), 'fake.png')}, headers=self.fx.headers(self.admin)).status_code, 400)
            self.assertEqual(operator.post('/api/config/tv-room-layouts/background', data={'file': (io.BytesIO(data.getvalue()), 'room.jpg')}, headers=self.fx.headers(operator)).status_code, 403)
            self.assertEqual(self.admin.post('/api/config/tv-room-layouts/background', data={'file': [(io.BytesIO(data.getvalue()), 'first.jpg'), (io.BytesIO(data.getvalue()), 'second.jpg')]}, headers=self.fx.headers(self.admin)).status_code, 400)
            self.assertTrue(all(not fake.commands for fake in self.fx.clients.values()))


    def test_invalid_membership_coordinates_and_image_paths_never_save(self):
        headers = self.fx.headers(self.admin)
        for layouts in [[], {'unknown': {}}, {'ground': {'positions': {'private': {'x': .5, 'y': .5}}}},
                        {'ground': {'positions': {'foyer': {'x': True, 'y': .5}}}},
                        {'ground': {'positions': {'foyer': {'x': 1.01, 'y': .5}}}},
                        {'ground': {'positions': {'foyer': {'x': 10 ** 500, 'y': .5}}}},
                        {'ground': {'positions': {'foyer': {'x': float('nan'), 'y': .5}}}},
                        {'ground': {'positions': {'foyer': {'x': .5}}}},
                        {'ground': {'background': '../../secret.png'}},
                        {'ground': {'background': 'https://example.com/image.png'}},
                        {'ground': {'background': 'a' * 32 + '.png'}},
                        {'ground': {'outputs': [1]}}]:
            response = self.admin.put('/api/config/tv-room-layouts', json={'layouts': layouts}, headers=headers)
            self.assertEqual(response.status_code, 400, (layouts, response.data))
        self.assertNotIn('hisense_room_layouts', self.fx.cfg)
        self.assertTrue(all(not fake.commands for fake in self.fx.clients.values()))

    def test_config_authority_csrf_origin_automation_and_versioned_routes(self):
        import api_security
        self.assertIn(b'href="/config/tv-rooms"', self.admin.get('/config/tvs').data)
        operator = self.fx.client()
        for prefix in ('/api', '/api/v1'):
            path = prefix + '/config/tv-room-layouts'
            self.assertEqual(operator.get(path).status_code, 403)
            self.assertEqual(operator.put(path, json={'layouts': {}}, headers=self.fx.headers(operator)).status_code, 403)
            self.assertEqual(self.admin.put(path, json={'layouts': {}}).status_code, 403)
            self.assertEqual(self.admin.put(path, json={'layouts': {}}, headers=self.fx.headers(self.admin, 'https://untrusted.example')).status_code, 403)
            token = api_security.create_service_token(self.fx.root / 'auth.db', name='Room test', scopes=['*'])
            machine = self.fx.w.app.test_client()
            auth = {'Authorization': 'Bearer ' + token['token']}
            self.assertEqual(machine.get(path, headers=auth).status_code, 403)
            self.assertEqual(machine.put(path, json={'layouts': {}}, headers=auth).status_code, 403)
            result = self.fx.w._execute_scheduler_internal_action({'method': 'PUT', 'path': path, 'body': {'layouts': {}}})
            self.assertFalse(result)
        self.assertEqual(operator.get('/config/tv-rooms').status_code, 403)
        # Config is sufficient; no protected Admin or new grant is required.
        conn = self.fx.w._db()
        try:
            conn.execute("INSERT INTO group_pages(group_id,page_key) VALUES(4,'page:config')"); conn.commit()
        finally:
            conn.close()
        config_user = self.fx.client('empty-tv')
        response = config_user.put('/api/config/tv-room-layouts', json={'layouts': {}}, headers=self.fx.headers(config_user))
        self.assertEqual(response.status_code, 200, response.data)

    def test_upload_admission_blocks_concurrent_decoder_work(self):
        data = io.BytesIO(); Image.new('RGB', (8, 8), 'red').save(data, format='PNG')
        slot = self.fx.w._media_upload_slot
        self.assertTrue(slot.acquire(blocking=False))
        try:
            response = self.admin.post('/api/config/tv-room-layouts/background', data={'file': (io.BytesIO(data.getvalue()), 'room.png')}, headers=self.fx.headers(self.admin))
            self.assertEqual(response.status_code, 429, response.data)
            self.assertEqual(response.json['error'], 'upload_busy')
        finally:
            slot.release()

    def test_image_stream_limits_and_missing_or_symlinked_images_fail_closed(self):
        import tv_pages
        from werkzeug.test import EnvironBuilder
        path = '/api/config/tv-room-layouts/background'
        headers = self.fx.headers(self.admin)
        # Streaming body without Content-Length must still be bounded before parsing.
        builder = EnvironBuilder(path=path, method='POST', data={'file': (io.BytesIO(b'x' * (11 * 1024 * 1024)), 'large.png')}, headers=headers)
        env = builder.get_environ(); env.pop('CONTENT_LENGTH', None); env['wsgi.input_terminated'] = True
        streams = []
        original = self.fx.w._TDeckRequest._get_file_stream
        def tracked(request, *args, **kwargs):
            stream = original(request, *args, **kwargs); streams.append(stream); return stream
        try:
            self.assertEqual(self.admin.open(env).status_code, 413)
        finally:
            env['wsgi.input'].close(); builder.close()
        builder = EnvironBuilder(path=path, method='POST', data={key: (io.BytesIO(b'x' * 1024), 'room.png') for key in ('file', 'second', 'third')}, headers=headers)
        env = builder.get_environ()
        try:
            with patch.object(self.fx.w._TDeckRequest, '_get_file_stream', tracked):
                self.assertEqual(self.admin.open(env).status_code, 413)
            self.assertTrue(streams and all(stream.closed for stream in streams), 'Rejected multipart streams must be closed')
        finally:
            env['wsgi.input'].close(); builder.close()
            for stream in streams:
                stream.close()
        # No extension-based trust and no multiframe or oversized decoded images.
        for content, filename in [(b'<svg onload="alert(1)"/>', 'room.png'), (b'not an image', 'room.jpg')]:
            self.assertEqual(self.admin.post(path, data={'file': (io.BytesIO(content), filename)}, headers=headers).status_code, 400)
        self.fx.cfg['hisense_room_layouts'] = {'ground': {'background': 'b' * 32 + '.png', 'positions': {}}}
        self.assertEqual(self.fx.client().get('/tv-room-images/ground/' + 'b' * 32 + '.png').status_code, 404)
        directory = tv_pages.images_dir(); directory.mkdir()
        secret = self.fx.root / 'secret'; secret.write_bytes(b'SECRET')
        (directory / ('b' * 32 + '.png')).symlink_to(secret)
        self.assertEqual(self.admin.get('/tv-room-images/ground/' + 'b' * 32 + '.png').status_code, 404)
        self.assertEqual(self.fx.w.app.test_client().get('/tv-room-images/ground/' + 'b' * 32 + '.png').status_code, 302)


    def test_diagnostics_navigation_only_links_current_control_scope(self):
        operator = self.fx.client()
        response = operator.get('/api/tv-pages/state?mode=diagnostics')
        self.assertTrue(response.json['tvs'][0]['canControl'])
        reader = self.fx.client('reader-tv')
        self.assertTrue(all(not tv['canControl'] for tv in reader.get('/api/tv-pages/state?mode=diagnostics').json['tvs']))
        selected = operator.get('/tv-controls?tv=foyer')
        self.assertEqual(selected.status_code, 200)
        self.assertIn(b'data-selected-tv="foyer"', selected.data)
        self.assertEqual(operator.get('/tv-controls?tv=private').status_code, 403)
        self.assertEqual(reader.get('/tv-controls?tv=foyer').status_code, 403)
        self.assertTrue(all(not fake.commands for fake in self.fx.clients.values()))


    def test_room_picture_preview_is_small_private_and_permission_checked(self):
        import hashlib
        import tv_pages
        data = io.BytesIO()
        Image.effect_noise((1800, 1200), 80).convert('RGB').save(data, format='PNG')
        response = self.admin.post('/api/config/tv-room-layouts/background', data={'file': (io.BytesIO(data.getvalue()), 'photo.png')}, headers=self.fx.headers(self.admin))
        self.assertEqual(response.status_code, 200, response.data)
        filename = response.json['image']; original = tv_pages.image_path(filename)
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        self.assertEqual(self.admin.put('/api/config/tv-room-layouts', json={'layouts': {'ground': {'background': filename, 'positions': {}}}}, headers=self.fx.headers(self.admin)).status_code, 200)
        operator = self.fx.client(); url = '/tv-room-images/ground/' + filename + '?preview=1'
        response = operator.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/jpeg')
        self.assertTrue(response.data.startswith(b'\xff\xd8'))
        self.assertLess(len(response.data), original.stat().st_size / 2)
        with Image.open(io.BytesIO(response.data)) as preview:
            self.assertLessEqual(max(preview.size), 1280)
            self.assertFalse(preview.getexif())
        etag = response.headers['ETag']; response.close()
        cached = operator.get(url, headers={'If-None-Match': etag})
        self.assertEqual(cached.status_code, 304)
        self.assertIn('private', cached.headers['Cache-Control'])
        self.assertIn('no-cache', cached.headers['Cache-Control']); cached.close()
        conn = self.fx.w._db()
        try:
            conn.execute("DELETE FROM group_pages WHERE group_id=2 AND page_key='tvdevice:ground:foyer'"); conn.commit()
        finally:
            conn.close()
        self.assertEqual(operator.get(url, headers={'If-None-Match': etag}).status_code, 403)
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), original_hash)
        self.assertTrue(all(not fake.commands for fake in self.fx.clients.values()))
