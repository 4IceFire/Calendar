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
        self.routing = Mock()
        self.routing.active_job.return_value = None
        self.display_job = {
            'id': 'a' * 32, 'mediaId': self.item['id'], 'output': 1, 'status': 'queued',
            'message': 'Preparing your image…', 'error': '', 'player': 2, 'aux': 1,
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
            patch.object(webui, 'can_access', side_effect=lambda key: key in self.grants),
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
        self.grants.update({'page:config', 'page:media_load'})
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
        self.grants.update({'page:config', 'page:media_load'})
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
            {'player': 2, 'label': 'Foyer', 'slots': [41, 42], 'aux': 1, 'videohub_input': 5},
            {'player': 4, 'label': 'Kids', 'slots': [43, 44], 'aux': 2, 'videohub_input': 6},
        ]}
        response = self.client.put('/api/config/atem-media', json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = json.loads(self.config_file.read_text())
        self.assertEqual(saved['atem_ip'], '192.0.2.1')
        self.assertEqual(saved['atem_media_destinations'][1]['player'], 4)
        self.assertEqual(saved['atem_media_destinations'][1]['aux'], 2)
        self.assertEqual(saved['atem_media_destinations'][1]['videohub_input'], 6)
        payload['atem_media_destinations'][1]['slots'] = [42, 43]
        self.assertEqual(self.client.put('/api/config/atem-media', json=payload, headers=self.headers).status_code, 400)

    def test_service_tokens_and_scheduler_cannot_use_new_media_apis(self):
        token = api_security.create_service_token(self.db, name='ATEM', scopes=['atem', 'config'])['token']
        for method, path in (('get', '/api/media'), ('get', '/api/atem/media/state'),
                             ('post', '/api/atem/media/load'), ('put', '/api/config/atem-media'),
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
        self.grants.update({'page:config', 'page:media_manage'})
        self.cfg['atem_media_destinations'] = 'invalid imported value'
        with patch.object(webui, '_get_atem_media_manager', side_effect=AssertionError('Must not construct a manager')):
            response = self.client.put('/api/config/atem-media', json={'atem_media_destinations': []}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.delete('/api/media/' + self.item['id'], headers=self.headers).status_code, 200)

    def test_config_only_user_can_manage_library_without_gaining_hardware_load(self):
        self.grants = {'page:config'}
        response = self.client.get('/config/atem-media')
        self.assertEqual(response.status_code, 200)
        self.assertIn('media-setup', response.get_data(as_text=True))
        self.assertIn('id="media-grid"', response.get_data(as_text=True))
        self.assertNotIn('id="media-load-button"', response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/atem/media/state').status_code, 200)
        listing = self.client.get('/api/media')
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()['permissions'], {'upload': True, 'manage': True, 'load': False})
        for key in ('url', 'thumbnail_url'):
            with self.client.get(listing.get_json()['items'][0][key]) as image:
                self.assertEqual(image.status_code, 200)
        upload = self.client.post('/api/media/upload', data={'file': (_image(), 'config-image.png')}, headers=self.headers)
        self.assertEqual(upload.status_code, 201)
        item = upload.get_json()['item']
        self.assertEqual(self.client.patch('/api/media/' + item['id'], json={'name': 'Team Night', 'preset': True},
                                           headers=self.headers).status_code, 200)
        self.assertEqual(self.client.delete('/api/media/' + item['id'], headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post('/api/atem/media/load', json={'media_id': self.item['id'], 'player': 2},
                                          headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post('/api/media/display', json={'media_id': self.item['id'], 'output': 1},
                                          headers=self.headers).status_code, 403)
        self.manager.load.assert_not_called()
        self.routing.display.assert_not_called()

    def test_nonadmin_cannot_change_server_executable(self):
        self.grants.add('page:config')
        with patch.object(webui, '_can_manage_service_tokens_for_current_user', return_value=False):
            response = self.client.put('/api/config/atem-media', json={'atem_media_node_path': 'C:/untrusted.exe'}, headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.config_file.exists())

    def _allow_display(self):
        self.grants.update({'page:routing', 'page:media', 'page:media_load'})
        self.cfg.update(atem_media_enabled=True, atem_media_destinations=[
            {'player': 2, 'label': 'A', 'slots': [41, 42], 'aux': 1, 'videohub_input': 5},
            {'player': 4, 'label': 'B', 'slots': [43, 44], 'aux': 2, 'videohub_input': 6},
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

    def test_display_requires_all_three_grants_for_writes_and_job_reads(self):
        self._allow_display()
        required = {'page:routing', 'page:media', 'page:media_load'}
        for missing in required:
            with self.subTest(missing=missing):
                self.grants = required - {missing}
                self.assertEqual(self._post_display().status_code, 403)
                self.assertEqual(self.client.get('/api/media/display/' + self.display_job['id']).status_code, 403)
        self.routing.display.assert_not_called()
        self.routing.get_job.assert_not_called()

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

    def test_display_accepts_only_saved_image_and_target_not_player_overrides(self):
        self._allow_display()
        for output in (True, 0, -1, 1.5, '01', '1.0', [], None):
            with self.subTest(output=output):
                self.assertEqual(self._post_display(output=output).status_code, 400)
        self.assertEqual(self._post_display(media_id='f' * 32).status_code, 404)
        for extra in ({'player': 4}, {'input': 6}, {'aux': 2}):
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
        self.grants.add('page:config')
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
        self.grants = {'page:config'}
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
