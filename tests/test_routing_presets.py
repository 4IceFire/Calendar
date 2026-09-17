import copy
import io
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
with patch.object(threading.Thread, 'start'):
    import webui
from media_library import MediaLibrary
from routing_presets import RoutingPresetStore, RoutingPresetRunner, validate_action


def action(label='Start timer', path='/api/timers/apply'):
    return {'label': label, 'path': path, 'method': 'POST', 'body': {'preset': 1}}


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RoutingPresetStore(self.temp.name)
        self.value = {'name': 'Team night', 'media_id': 'a' * 32, 'actions': [action()]}

    def test_round_trip_revision_and_referenced_image(self):
        item = self.store.save(self.value)
        self.assertIsNone(item['output'])
        other = RoutingPresetStore(self.temp.name)
        self.assertEqual(other.get(item['id']), item)
        updated = other.save({**self.value, 'output': 26}, item['id'], item['revision'])
        self.assertNotEqual(updated['revision'], item['revision'])
        with self.assertRaises(ValueError):
            self.store.save(self.value, item['id'], item['revision'])
        with self.assertRaises(ValueError):
            self.store.delete(item['id'], item['revision'])
        other.delete(updated['id'], updated['revision'])
        self.assertEqual(self.store.list(), [])

    def test_legacy_image_presets_import_once_without_actions_or_grants(self):
        root = Path(self.temp.name) / 'legacy'
        root.mkdir()
        images = [{'id': 'b' * 32, 'name': 'Mother day', 'preset': True}]
        item = RoutingPresetStore(root, images).list()[0]
        self.assertEqual(item['media_id'], images[0]['id'])
        self.assertEqual(item['actions'], [])
        self.assertEqual(RoutingPresetStore(root, images).list(), [item])

    def test_untrusted_paths_and_unbounded_actions_are_rejected(self):
        for path in ('https://example.com/api/test', '//example.com/api/test', '/api/../config',
                     '/api/%2e%2e/config', '/api//timers', '/api/timers#fragment', '/api/timers\\apply'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_action(action(path=path))
        for value in ({**self.value, 'actions': [action()] * 21}, {**self.value, 'output': True},
                      {**self.value, 'media_id': '../secret'}, {**self.value, 'output': 0},
                      {**self.value, 'name': {'bad': 'value'}}):
            with self.assertRaises(ValueError):
                self.store.save(value)
        with self.assertRaises(ValueError):
            validate_action({**action(), 'headers': {'Authorization': 'no'}})

    def test_corrupt_storage_is_not_replaced(self):
        self.store.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(OSError):
            self.store.save(self.value)
        self.assertEqual(self.store.path.read_text(), '{broken')

    def test_runner_waits_for_image_stops_on_failure_and_never_replays(self):
        runner = RoutingPresetRunner()
        preset = self.store.save({**self.value, 'actions': [action('First'), action('Second'), action('Third')]})
        callbacks, calls, finished = [], [], []
        def execute(item):
            calls.append(item['label'])
            return item['label'] != 'Second'
        kwargs = dict(display=lambda callback: callbacks.append(callback), execute=execute, completed=finished.append)
        first = runner.start('run', preset, 26, 'owner', **kwargs)
        self.assertEqual(first['status'], 'loading')
        self.assertEqual(calls, [])
        runner.start('run', preset, 26, 'owner', **kwargs)
        self.assertEqual(len(callbacks), 1)
        callbacks[0]({'status': 'succeeded'})
        self.assertEqual(calls, ['First', 'Second'])
        self.assertEqual(finished[0]['status'], 'failed')
        self.assertTrue(finished[0]['imageDisplayed'])
        runner.start('run', preset, 26, 'owner', **kwargs)
        callbacks[0]({'status': 'succeeded'})
        self.assertEqual(len(calls), 2)
        runner.start('failed-image', preset, 26, 'owner', **kwargs)
        callbacks[1]({'status': 'failed', 'message': 'All image players are in use on other outputs.', 'internalError': 'allocator diagnostic'})
        self.assertEqual(len(calls), 2)
        self.assertIn('All image players are in use', finished[-1]['message'])
        self.assertEqual(finished[-1]['displayError'], 'allocator diagnostic')
        self.assertNotIn('displayError', webui._public_routing_preset_job(finished[-1]))


class RoutingPresetWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.library = MediaLibrary(root / 'media')
        image = io.BytesIO(); Image.new('RGB', (16, 9), 'blue').save(image, format='PNG'); image.seek(0)
        self.image = self.library.upload(image, 'photo.png', 'Team night')
        self.store = RoutingPresetStore(self.library.root)
        self.preset = self.store.save({'name': 'Team night', 'media_id': self.image['id'], 'actions': [action()]})
        self.runner = RoutingPresetRunner()
        self.callbacks = []
        self.manager = Mock()
        self.manager.display.side_effect = lambda *args, **kwargs: self.callbacks.append(kwargs['on_complete'])
        self.calls = []
        def operation():
            self.calls.append({'principal': dict(webui.g.api_principal), 'actor': webui.capture_activity_actor(),
                               'body': webui.request.get_json()})
            return webui.jsonify(ok=True)
        self.cfg = {'auth_enabled': True, 'api_legacy_anonymous_enabled': False,
                    'api_write_rate_limit_per_minute': 600, 'atem_media_enabled': True, 'atem_ip': '192.0.2.1',
                    'atem_media_destinations': [{'player': 2, 'slots': [41, 42], 'videohub_input': 13}]}
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', root / 'auth.db'),
            patch.object(webui, '_auth_enabled', return_value=True),
            patch.object(webui, '_auth_cfg', return_value=self.cfg),
            patch.object(webui.utils, 'get_config', return_value=self.cfg),
            patch.object(webui, '_bootstrap_default_users_roles'),
            patch.object(webui, '_touch_current_user_session', return_value=True),
            patch.object(webui, '_get_media_library', return_value=self.library),
            patch.object(webui, '_get_routing_preset_store', return_value=self.store),
            patch.object(webui, '_routing_preset_runner', self.runner),
            patch.object(webui, '_get_media_routing_manager', return_value=self.manager),
            patch.object(webui, '_active_media_job', side_effect=lambda: self.runner.active_job() or {}),
            patch.object(webui, '_get_videohub_state_snapshot', return_value={'outputs': [{'number': 26, 'label': 'Foyer'}]}),
            patch.dict(webui.app.view_functions, {'api_apply_timer_preset': operation, 'api_v1__api_apply_timer_preset': operation}),
            patch.object(webui, 'log_event'),
        ):
            self.stack.enter_context(context)
        webui._init_auth_db()
        conn = webui._db()
        conn.executemany('INSERT INTO users(id,username,password_hash) VALUES(?,?,?)', [(1, 'admin-fixture', 'unused'), (2, 'operator', 'unused'), (3, 'config-editor', 'unused')])
        conn.execute("INSERT INTO groups(id,name,is_admin) VALUES(1,'Admin',1)")
        conn.execute("INSERT INTO groups(id,name,videohub_allowed_outputs,videohub_allowed_inputs) VALUES(2,'Operator','[26]','[13]')")
        conn.execute("INSERT INTO groups(id,name) VALUES(3,'Config')")
        conn.executemany('INSERT INTO user_groups(user_id,group_id) VALUES(?,?)', [(1,1),(2,2),(3,3)])
        conn.commit(); conn.close()
        self.grants = ['page:routing', 'page:routing_presets', 'preset:' + self.preset['id']]
        webui._set_group_pages(2, self.grants)
        webui._set_group_pages(3, ['page:config'])
        self.client = webui.app.test_client()
        with self.client.session_transaction() as state: state['_csrf'] = 'preset-csrf'
        self.headers = {'Origin': 'http://localhost', 'X-CSRF-Token': 'preset-csrf'}
        with webui._api_rate_lock: webui._api_rate_events.clear()

    def user(self, uid=2):
        return patch.object(webui, 'current_user', webui._User(webui._user_record(uid)))

    def prepare(self, output=26, preset=None):
        preset = preset or self.preset
        return self.client.post('/api/routing/presets/' + preset['id'] + '/prepare', headers=self.headers,
                                json={'revision': preset['revision'], 'output': output})

    def apply(self, prepared, extra=None):
        return self.client.post('/api/routing/presets/' + self.preset['id'] + '/apply', headers=self.headers,
                                json={'confirmation_token': prepared['confirmation_token'], **(extra or {})})

    def test_temporary_images_are_excluded_and_cannot_be_saved_into_presets(self):
        temporary = self.library.upload(io.BytesIO(self.library.path(self.image['id']).read_bytes()), 'temporary.png', temporary=True)
        with self.user(1):
            listing = self.client.get('/api/config/routing-presets').get_json()
            self.assertNotIn(temporary['id'], [image['id'] for image in listing['images']])
            response = self.client.post('/api/config/routing-presets', headers=self.headers,
                                        json={'name': 'Temporary preset', 'media_id': temporary['id'], 'output': 26})
            self.assertEqual(response.status_code, 400)
            self.assertIn('saved library', response.get_json()['message'])

    def test_only_assigned_presets_are_visible_without_granting_the_media_pool(self):
        other = self.store.save({'name': 'Hidden', 'media_id': self.image['id'], 'output': 25})
        with self.user():
            self.assertEqual(self.client.get('/routing').status_code, 200)
            listing = self.client.get('/api/routing/presets').get_json()
            self.assertEqual([item['id'] for item in listing['presets']], [self.preset['id']])
            self.assertNotIn('actions', listing['presets'][0])
            with self.client.get(listing['presets'][0]['thumbnail_url']) as thumbnail:
                self.assertEqual(thumbnail.status_code, 200)
            self.assertEqual(self.client.get('/api/media').status_code, 403)
            self.assertEqual(self.prepare(preset=other).status_code, 403)
            self.assertEqual(self.client.get('/routing?preset=' + self.preset['id']).status_code, 200)

    def test_confirmation_then_image_then_approved_action_without_operator_timer_access(self):
        with self.user():
            prepared = self.prepare().get_json()
            self.assertEqual(prepared['output_label'], 'Foyer')
            self.assertEqual(self.callbacks, [])
            self.assertEqual(self.calls, [])
            self.assertEqual(self.client.post('/api/timers/apply', json={'preset': 1}, headers=self.headers).status_code, 403)
            response = self.apply(prepared)
            self.assertEqual(response.status_code, 202, response.get_json())
            self.assertTrue(self.manager.display.call_args.kwargs['allow_shared_player'])
            self.assertEqual(self.calls, [])
            self.assertEqual(self.apply(prepared).status_code, 202)
            self.assertEqual(len(self.callbacks), 1)
            self.callbacks[0]({'status': 'succeeded'})
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.calls[0]['principal']['type'], 'routing_preset')
            self.assertEqual(self.calls[0]['actor']['actor_username'], 'operator')
            self.assertEqual(self.calls[0]['body'], {'preset': 1})
            self.assertEqual(self.apply(prepared).status_code, 202)
            self.assertEqual(len(self.calls), 1)
            job = self.client.get('/api/routing/presets/jobs/' + prepared['execution_id']).get_json()['job']
            self.assertEqual(job['status'], 'succeeded')
            self.assertNotIn('owner', job)

    def test_output_inputs_grants_csrf_and_stale_confirmation_are_enforced(self):
        with self.user():
            self.assertEqual(self.prepare(25).status_code, 403)
            prepared = self.prepare().get_json()
            self.assertEqual(self.apply(prepared, {'actions': [action()]}).status_code, 400)
            for headers in ({}, {**self.headers, 'Origin': 'https://other.invalid'}):
                self.assertEqual(self.client.post('/api/routing/presets/' + self.preset['id'] + '/apply',
                    headers=headers, json={'confirmation_token': prepared['confirmation_token']}).status_code, 403)
            self.store.save({'name': 'Changed', 'media_id': self.image['id']}, self.preset['id'], self.preset['revision'])
            self.assertEqual(self.apply(prepared).status_code, 409)
        self.assertEqual(self.callbacks, [])
        webui._set_group_pages(2, ['page:routing', 'page:routing_presets'])
        with self.user(): self.assertEqual(self.client.get('/api/routing/presets').get_json()['presets'], [])
        webui._set_group_pages(2, ['page:routing_presets', 'preset:' + self.preset['id']])
        with self.user(): self.assertEqual(self.client.get('/api/routing/presets').status_code, 403)

    def test_fixed_output_cannot_be_overridden_and_configuration_requires_admin(self):
        value = {'name': 'Fixed', 'media_id': self.image['id'], 'output': 26, 'actions': []}
        self.preset = self.store.save(value, self.preset['id'], self.preset['revision'])
        with self.user():
            self.assertEqual(self.prepare(25).status_code, 400)
            self.assertEqual(self.prepare(None).status_code, 200)
        for uid in (2, 3):
            with self.user(uid): self.assertEqual(self.client.post('/api/config/routing-presets', json=value, headers=self.headers).status_code, 403)
        with self.user(1):
            self.assertEqual(self.client.post('/api/config/routing-presets', json=value, headers=self.headers).status_code, 201)
            for path in ('/api/config', '/api/admin/users', '/api/media/upload', '/api/routing/presets',
                         '/api/config/service-tokens', '/api/unknown', '/api/v1/config/service-tokens'):
                response = self.client.post('/api/config/routing-presets', json={**value, 'actions': [action(path=path)]}, headers=self.headers)
                self.assertEqual(response.status_code, 400, path)

    def test_no_browser_header_can_create_the_private_preset_principal(self):
        with self.user():
            response = self.client.post('/api/timers/apply', json={'preset': 1}, headers={**self.headers, 'X-TDeck-Preset': self.preset['id']})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.calls, [])
        with webui.app.test_request_context('/api/routing/presets'):
            webui.g._tdeck_scheduler_principal = {'type': 'scheduler'}
            self.assertEqual(webui.app.full_dispatch_request().status_code, 403)

    def test_even_full_scope_service_tokens_cannot_use_or_edit_presets(self):
        import api_security
        token = api_security.create_service_token(webui._AUTH_DB_PATH, name='Fixture', scopes=['*'])
        headers = {'Authorization': 'Bearer ' + token['token']}
        for prefix in ('/api', '/api/v1'):
            self.assertEqual(self.client.get(prefix + '/routing/presets', headers=headers).status_code, 403)
            self.assertEqual(self.client.post(prefix + '/routing/presets/' + self.preset['id'] + '/prepare',
                json={'revision': self.preset['revision'], 'output': 26}, headers=headers).status_code, 403)
            self.assertEqual(self.client.post(prefix + '/config/routing-presets',
                json={'name': 'New', 'media_id': self.image['id']}, headers=headers).status_code, 403)

    def test_another_session_cannot_reuse_a_confirmation_or_read_its_job(self):
        with self.user():
            prepared = self.prepare().get_json()
            self.apply(prepared)
            with self.client.session_transaction() as state: state['_csrf'] = 'different-csrf'
            changed_headers = {**self.headers, 'X-CSRF-Token': 'different-csrf'}
            response = self.client.post('/api/routing/presets/' + self.preset['id'] + '/apply',
                                       headers=changed_headers, json={'confirmation_token': prepared['confirmation_token']})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.client.get('/api/routing/presets/jobs/' + prepared['execution_id']).status_code, 403)
        self.assertEqual(len(self.callbacks), 1)

    def test_expired_malformed_and_pre_restart_confirmations_cannot_run(self):
        with self.user():
            prepared = self.prepare().get_json()
            for value in (None, {}, [], 123, 'not-a-signed-confirmation'):
                response = self.apply({'confirmation_token': value})
                self.assertEqual(response.status_code, 400)
            confirmed = webui._preset_signer().loads(prepared['confirmation_token'])
            with patch('itsdangerous.timed.TimestampSigner.get_timestamp', return_value=1):
                old = {'confirmation_token': webui._preset_signer().dumps(confirmed)}
            self.assertEqual(self.apply(old).status_code, 400)
            with patch.object(webui, '_ROUTING_PRESET_CONFIRMATION_EPOCH', 'new-server-process'):
                self.assertEqual(self.apply(prepared).status_code, 400)
        self.assertEqual(self.callbacks, [])

    def test_revoked_preset_and_changed_port_permissions_block_confirmed_requests(self):
        with self.user(): prepared = self.prepare().get_json()
        webui._set_group_pages(2, ['page:routing', 'page:routing_presets'])
        with self.user(): self.assertEqual(self.apply(prepared).status_code, 403)
        webui._set_group_pages(2, self.grants)
        conn = webui._db()
        conn.execute("UPDATE groups SET videohub_allowed_inputs='[12]' WHERE id=2")
        conn.commit(); conn.close()
        with self.user(): self.assertEqual(self.apply(prepared).status_code, 403)
        self.assertEqual(self.callbacks, [])

    def test_scoped_thumbnails_and_fixed_outputs_cannot_escape_grants(self):
        stream = io.BytesIO(); Image.new('RGB', (16, 9), 'red').save(stream, format='PNG'); stream.seek(0)
        hidden_image = self.library.upload(stream, 'hidden.png', 'Private')
        hidden = self.store.save({'name': 'Private', 'media_id': hidden_image['id'], 'output': 25})
        webui._set_group_pages(2, self.grants + ['preset:' + hidden['id']])
        with self.user():
            response = self.client.get('/api/v1/routing/presets')
            self.assertEqual(response.status_code, 200)
            self.assertEqual([item['id'] for item in response.get_json()['presets']], [self.preset['id']])
            self.assertEqual(self.prepare(preset=hidden).status_code, 403)
            self.assertEqual(self.client.get('/media/thumbnails/' + hidden_image['id'] + '.png').status_code, 403)
            self.assertEqual(self.client.get('/media/images/' + hidden_image['id'] + '.png').status_code, 403)

    def test_active_preset_blocks_config_and_referenced_image_deletion(self):
        with self.user():
            prepared = self.prepare().get_json()
            self.assertEqual(self.apply(prepared).status_code, 202)
        with self.user(1):
            self.assertEqual(self.client.post('/api/config/routing-presets', headers=self.headers,
                json={'name': 'New', 'media_id': self.image['id']}).status_code, 409)
        self.callbacks[0]({'status': 'succeeded'})
        with self.user(1):
            self.assertEqual(self.client.delete('/api/media/' + self.image['id'], headers=self.headers).status_code, 409)
        self.assertEqual(self.library.get(self.image['id'])['id'], self.image['id'])

    def test_action_aliases_preserve_internal_auth_and_view_as_audit_context(self):
        actor = {'actor_user_id': 2, 'actor_username': 'operator', 'source': 'web',
                 'impersonation': {'admin_id': 1, 'admin_username': 'admin-fixture',
                                   'target_id': 2, 'target_username': 'operator'}}
        with webui.app.app_context():
            self.assertTrue(webui._execute_routing_preset_action(action(path='/api/v1/timers/apply'), self.preset, actor))
        self.assertEqual(self.calls[0]['principal']['type'], 'routing_preset')
        self.assertEqual(self.calls[0]['actor'], actor)


if __name__ == '__main__':
    unittest.main()
