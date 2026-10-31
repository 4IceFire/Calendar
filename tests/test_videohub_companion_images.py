"""Companion's existing apply endpoint: image-only references, real auth/coordinator."""
import copy
import io
import time
from unittest.mock import patch

from PIL import Image
import api_security
from media_library import MediaLibrary
from media_player_usage import PlayerUsage
from routing_presets import RoutingPresetStore
from test_media_routing import FakeMedia, configuration
import test_videohub_ignore as fixture


class CompanionImageTests(fixture.VideoHubIgnoreApiTests):
    def setUp(self):
        super().setUp()
        self.cfg.update(configuration())
        self.hub.routing = [1, 2, 3, 4]
        self.hub.get_routing_state_strict = lambda: {'input_count': 8, 'output_count': 4, 'routing': self.hub.routing[:]}
        self.library = MediaLibrary(self.root / 'media')
        stream = io.BytesIO()
        Image.new('RGB', (8, 8), 'red').save(stream, format='PNG')
        stream.seek(0)
        self.image = self.library.upload(stream, 'fixture.png')
        self.store = RoutingPresetStore(self.library.root)
        self.source = self.store.save({'name': 'Welcome', 'media_id': self.image['id'], 'output': 9,
            'actions': [{'label': 'Must not run', 'method': 'POST', 'path': '/api/timers/apply', 'body': {'preset': 1}}]})
        self.preset = self.create([{'output': 1, 'routing_preset': self.source['id']},
                                  {'output': 2, 'input': 7}, {'output': 3, 'input': None}])
        self.path = f"/api/videohub/presets/{self.preset['id']}/apply"
        self.media = FakeMedia()
        for context in (patch.object(self.webui, '_get_media_library', return_value=self.library),
                        patch.object(self.webui, '_get_routing_preset_store', return_value=self.store),
                        patch.object(self.webui, '_media_routing_instance', None),
                        patch.object(self.webui, 'get_videohub_client_from_config', return_value=self.hub),
                        patch('atem_media.get_atem_media_manager', return_value=self.media),
                        patch('media_routing.PLAYER_USAGE', PlayerUsage()),
                        patch.object(self.webui, '_videohub_image_jobs', {})):
            self.stack.enter_context(context)
        self.dispatch_action = self.webui._execute_routing_preset_action
        self.actions = self.stack.enter_context(patch.object(self.webui, '_execute_routing_preset_action'))
        self.hub.route_video_output = lambda *, output, input_: self.hub.routing.__setitem__(output, input_ + 1)
        self.hub.verify_video_output_route = lambda *, output, input_: self.hub.routing[output] == input_ + 1
        self.other = self.webui.app.test_client()

    def token(self, constraints=None, scopes=('videohub',)):
        value = api_security.create_service_token(self.webui._AUTH_DB_PATH, name='Companion fixture',
                                                 scopes=scopes, constraints=constraints)
        return value, {'Authorization': 'Bearer ' + value['token']}

    def wait_job(self, path, headers):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.other.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.get_json())
            job = response.get_json()['job']
            if job['status'] in ('succeeded', 'failed'):
                # Completion logging runs just after terminal job publication.
                manager = self.webui._videohub_image_jobs[job['id']][2]
                while manager.active_job() is not None and time.monotonic() < deadline:
                    time.sleep(.005)
                return job
            time.sleep(.005)
        self.fail('Coordinator did not terminate within three seconds')

    def test_config_and_resource_snapshot_survive_request_and_catalog_changes(self):
        _, headers = self.token()
        captured = []
        original = self.backend._apply_routes
        def numeric(cfg, preset):
            self.cfg['videohub_ip'] = '192.0.2.99'
            captured.append(copy.deepcopy(cfg))
            return original(cfg, preset)
        with patch.object(self.backend, '_apply_routes', side_effect=numeric):
            response = self.other.post(self.path, headers=headers)
            self.assertEqual(response.status_code, 202)
            path = f"/api/videohub/presets/{self.preset['id']}/jobs/{response.get_json()['job']['id']}"
            self.assertEqual(self.wait_job(path, headers)['status'], 'succeeded')
        self.assertEqual(captured[0]['videohub_ip'], '192.0.2.2')
        self.assertEqual(self.media.calls[0]['media_id'], self.image['id'])
        self.assertEqual(self.hub.routing, [5, 7, 3, 4])
        # Changing the catalog cannot rewrite a retained job or grant new reads.
        self.store.save({'name': self.source['name'], 'media_id': self.image['id'],
                         'enabled': False}, self.source['id'], self.source['revision'])
        self.assertEqual(self.other.get(path, headers=headers).status_code, 403)

    def test_browser_image_apply_still_requires_csrf_and_trusted_origin(self):
        self.assertEqual(self.client.post(self.path).status_code, 403)
        self.assertEqual(self.client.post(self.path, headers={**self.headers, 'Origin': 'https://untrusted.invalid'}).status_code, 403)
        self.assertEqual(self.hub.writes, [])
        self.assertEqual(self.media.calls, [])

    def test_token_constraints_preflight_all_routes_and_references_without_hardware(self):
        for constraints in ({'videohub_presets': [99]}, {'videohub_outputs': [1, 3]},
                            {'videohub_outputs': [2, 3]}, {'videohub_inputs': [5]},
                            {'videohub_inputs': [7]}, {'routing_presets': ['a' * 32]},
                            {'routing_outputs': [9]},
                            {'allowed_paths': ['GET /api/videohub/*']}):
            with self.subTest(constraints=constraints):
                _, headers = self.token(constraints)
                self.assertEqual(self.other.post(self.path, headers=headers).status_code, 403)
        for scopes in (['read'], ['routing_presets'], ['atem']):
            _, headers = self.token(scopes=scopes)
            self.assertEqual(self.other.post(self.path, headers=headers).status_code, 403)
        self.assertEqual(self.hub.writes, [])
        self.assertEqual(self.hub.reads, 0)
        self.assertEqual(self.media.calls, [])
        self.assertIsNone(self.webui._media_routing_instance)

    def test_invalid_last_reference_preflight_prevents_all_hardware(self):
        _, headers = self.token()
        for kind in ('deleted', 'disabled', 'temporary', 'missing_metadata'):
            with self.subTest(kind=kind):
                source = self.store.save({'name': 'Last', 'media_id': self.image['id'],
                                          'enabled': kind != 'disabled'})
                self.backend.upsert_preset(self.cfg, {'id': self.preset['id'], 'name': 'Two', 'routes': [
                    {'output': 1, 'routing_preset': self.source['id']},
                    {'output': 2, 'input': 7}, {'output': 3, 'routing_preset': source['id']}]})
                if kind == 'deleted':
                    self.store.delete(source['id'], source['revision'])
                if kind == 'temporary':
                    image = {**self.image, 'temporary': True}
                    context = patch.object(self.library, 'get', side_effect=lambda identity: image)
                elif kind == 'missing_metadata':
                    context = patch.object(self.library, 'get', side_effect=KeyError('Image not found'))
                else:
                    from contextlib import nullcontext
                    context = nullcontext()
                with context:
                    self.assertIn(self.other.post(self.path, headers=headers).status_code, (400, 404))
        self.assertEqual(self.hub.writes, [])
        self.assertEqual(self.media.calls, [])
        self.assertIsNone(self.webui._media_routing_instance)

    def test_token_jobs_are_exact_owner_and_current_resource_scoped(self):
        token, headers = self.token()
        response = self.other.post(self.path, headers=headers)
        self.assertEqual(response.status_code, 202, response.get_json())
        path = f"/api/videohub/presets/{self.preset['id']}/jobs/{response.get_json()['job']['id']}"
        self.assertEqual(self.wait_job(path, headers)['status'], 'succeeded')
        _, second = self.token()
        self.assertEqual(self.other.get(path, headers=second).status_code, 403)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.other.get(path.replace('/presets/1/', '/presets/99/'), headers=headers).status_code, 403)
        for constraints in ({'videohub_presets': [99]}, {'videohub_outputs': [1]},
                            {'videohub_inputs': [5]}, {'videohub_inputs': [7]},
                            {'routing_presets': ['a' * 32]}, {'routing_outputs': [9]},
                            {'allowed_paths': ['POST /api/videohub/*']}):
            with self.subTest(constraints=constraints):
                conn = self.webui._db()
                conn.execute('UPDATE service_tokens SET constraints_json=? WHERE id=?',
                             (__import__('json').dumps(constraints), token['id']))
                conn.commit(); conn.close()
                self.assertEqual(self.other.get(path, headers=headers).status_code, 403)
        conn = self.webui._db()
        conn.execute('UPDATE service_tokens SET constraints_json=?,scopes_json=? WHERE id=?',
                     ('{}', '["read"]', token['id']))
        conn.commit(); conn.close()
        self.assertEqual(self.other.get(path, headers=headers).status_code, 403)
        api_security.revoke_service_token(self.webui._AUTH_DB_PATH, token['id'])
        self.assertEqual(self.other.get(path, headers=headers).status_code, 401)
        self.actions.assert_not_called()

    def test_token_expiry_and_browser_job_isolation_no_generic_media_access(self):
        response = self.client.post(self.path, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.get_json())
        path = f"/api/videohub/presets/{self.preset['id']}/jobs/{response.get_json()['job']['id']}"
        deadline = time.monotonic() + 3
        manager = self.webui._videohub_image_jobs[response.get_json()['job']['id']][2]
        while manager.active_job() is not None and time.monotonic() < deadline:
            time.sleep(.005)
        token, headers = self.token(scopes=['*'])
        self.assertEqual(self.other.get(path, headers=headers).status_code, 403)
        for target in ('/api/media', '/api/media/' + self.image['id'], '/api/config/atem-media',
                       '/api/videohub/routing-presets', '/api/routing/presets'):
            self.assertEqual(self.other.get(target, headers=headers).status_code, 403, target)
        conn = self.webui._db()
        from datetime import datetime, timezone, timedelta
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn.execute('UPDATE service_tokens SET expires_at=? WHERE id=?', (expired, token['id']))
        conn.commit(); conn.close()
        self.assertEqual(self.other.post(self.path, headers=headers).status_code, 401)
        self.assertEqual(self.other.get(path, headers=headers).status_code, 401)
        self.assertFalse(self.webui._execute_scheduler_internal_action({'method': 'POST', 'path': self.path}))
        self.assertFalse(self.dispatch_action({'method': 'POST', 'path': self.path, 'body': None,
                                              'label': 'Nested apply'}, self.source, {'source': 'system'}))

    def test_busy_failure_and_shared_refusal_never_execute_saved_actions(self):
        _, headers = self.token()
        self.media.immediate = False
        response = self.other.post(self.path, headers=headers)
        self.assertEqual(response.status_code, 202, response.get_json())
        path = f"/api/videohub/presets/{self.preset['id']}/jobs/{response.get_json()['job']['id']}"
        deadline = time.monotonic() + 3
        while self.media.callback is None and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertIsNotNone(self.media.callback)
        self.assertEqual(self.other.post(self.path, headers=headers).status_code, 409)
        self.media.failure = 'simulated load failure'
        self.media.complete()
        self.assertEqual(self.wait_job(path, headers)['status'], 'failed')
        self.assertEqual(self.hub.routing, [1, 7, 3, 4])
        before = len(self.media.calls)
        self.media.immediate = True
        self.hub.routing = [1, 5, 6, 4]
        # Keep both existing receivers: no free mapped image player.
        self.backend.upsert_preset(self.cfg, {'id': self.preset['id'], 'name': 'Shared', 'routes': [
            {'output': 1, 'routing_preset': self.source['id']}, {'output': 2, 'input': None},
            {'output': 3, 'input': None}]})
        response = self.other.post(self.path, headers=headers)
        self.assertEqual(response.status_code, 202)
        path = f"/api/videohub/presets/{self.preset['id']}/jobs/{response.get_json()['job']['id']}"
        self.assertEqual(self.wait_job(path, headers)['status'], 'failed')
        self.assertEqual(len(self.media.calls), before)
        self.assertEqual(self.hub.routing, [1, 5, 6, 4])
        self.actions.assert_not_called()
        failures = [c for c in self.log.call_args_list if c.args[0] == 'videohub.preset.apply']
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(c.kwargs['status'] == 'failure' for c in failures))

    def test_completion_actor_is_exact_token_even_with_browser_cookie(self):
        token, headers = self.token()
        self.media.immediate = False
        response = self.client.post(self.path, headers=headers)
        self.assertEqual(response.status_code, 202, response.get_json())
        path = f"/api/videohub/presets/{self.preset['id']}/jobs/{response.get_json()['job']['id']}"
        deadline = time.monotonic() + 3
        while self.media.callback is None and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertIsNotNone(self.media.callback)
        self.login(3)
        self.media.complete()
        self.assertEqual(self.wait_job(path, headers)['status'], 'succeeded')
        records = [c for c in self.log.call_args_list if c.args[0] == 'videohub.preset.apply']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].kwargs['actor_username'], f"token:{token['id']}:Companion fixture")
        self.assertIsNone(records[0].kwargs['actor_user_id'])
        self.assertEqual(records[0].kwargs['source'], 'companion')

    def test_missing_source_asset_preflight_prevents_even_numeric_writes(self):
        _, headers = self.token()
        (self.library.root / 'images' / (self.image['id'] + '.png')).unlink()
        response = self.other.post(self.path, headers=headers)
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(self.hub.writes, [])
        self.assertEqual(self.media.calls, [])

    def test_existing_apply_accepts_companion_image_only_references(self):
        import http.client
        import json
        import threading
        from werkzeug.serving import make_server
        server = make_server('127.0.0.1', 0, self.webui.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, headers):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request(method, path, headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read()), response.getheader('X-TDeck-API-Version')
            finally:
                connection.close()
        _, headers = self.token({'videohub_presets': [self.preset['id']],
                                 'videohub_outputs': [1, 2, 3], 'videohub_inputs': [5, 7],
                                 'routing_presets': [self.source['id']], 'routing_outputs': [1]})
        for prefix in ('/api', '/api/v1'):
            with self.subTest(prefix=prefix):
                status, payload, version = request('POST', self.path.replace('/api', prefix, 1), headers)
                self.assertEqual(status, 202, payload)
                if prefix == '/api/v1':
                    self.assertEqual(version, '1')
                path = f"{prefix}/videohub/presets/{self.preset['id']}/jobs/{payload['job']['id']}"
                job = self.wait_job(path, headers)
                self.assertEqual(job['status'], 'succeeded', job)
                status, payload, _ = request('GET', path, headers)
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload['job']['imagesCompleted'], 1)
        self.assertEqual(self.hub.routing, [5, 7, 3, 4])
        self.assertEqual(len(self.media.calls), 2)
        self.assertEqual(self.store.get(self.source['id']), self.source)
        self.actions.assert_not_called()
        self.assertEqual(self.backend.get_preset(self.cfg, self.preset['id']).to_dict()['routes'], self.preset['routes'])


# Fixture inheritance must not duplicate browser tests.
for _name in vars(fixture.VideoHubIgnoreApiTests):
    if _name.startswith('test_'):
        setattr(CompanionImageTests, _name, None)
