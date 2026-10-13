"""Real authentication and saved-action dispatch; hardware replaced at boundary."""
import uuid
import unittest
from unittest.mock import patch
import api_security
import test_routing_presets as fixture
webui = fixture.webui
REAL_ROUTING_FACTORY = webui._get_media_routing_manager


class AutomationTests(fixture.RoutingPresetWebTests):
    # Reuse isolated storage/auth fixture, not inherited browser assertions.
    def token(self, scopes=('routing_presets',), constraints=None):
        value = api_security.create_service_token(webui._AUTH_DB_PATH, name='Companion fixture',
            scopes=scopes, constraints=constraints)
        return {'Authorization': 'Bearer ' + value['token']}

    def test_lifecycle_constraints_stale_revision_and_uncertain_no_replay(self):
        import time
        import http.client
        import json
        import threading
        from werkzeug.serving import make_server
        headers = self.token(constraints={'routing_presets': [self.preset['id']], 'routing_outputs': [26]})
        server = make_server('127.0.0.1', 0, webui.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def request(method, path, body=None, credentials=headers):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                               headers={**credentials, 'Content-Type': 'application/json'})
            response = connection.getresponse(); result = response.status, json.loads(response.read())
            connection.close(); return result
        path = '/api/v1/automation/routing-presets'
        status, listing = request('GET', path)
        self.assertEqual(status, 200, listing)
        body = {'revision': self.preset['revision'], 'execution_id': uuid.uuid4().hex,
                'issued_at': int(time.time()), 'execution_epoch': listing['execution_epoch'], 'output': 26}
        trigger = path + '/' + self.preset['id'] + '/trigger'
        self.assertEqual(request('POST', trigger, {**body, 'output': 25})[0], 403)
        self.assertEqual(request('POST', trigger, {**body, 'actions': []})[0], 400)
        self.assertEqual(request('POST', trigger, {**body, 'issued_at': int(time.time()) - 301})[0], 409)
        with patch.object(webui, '_ROUTING_PRESET_CONFIRMATION_EPOCH', 'different-process'):
            self.assertEqual(request('POST', trigger, body)[0], 409)
        self.assertEqual(self.callbacks, [])
        # One POST followed only by polling after the caller loses its reply.
        status, response = request('POST', trigger, body)
        self.assertEqual(status, 202, response)
        jobpath = path + '/jobs/' + body['execution_id']
        self.assertEqual(request('GET', jobpath)[1]['job']['status'], 'loading')
        self.callbacks[0]({'status': 'failed', 'message': 'No exclusive player is available.'})
        self.assertEqual(request('GET', jobpath)[1]['job']['status'], 'failed')
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.callbacks), 1)
        changed = self.store.save({'name': 'Changed', 'media_id': self.image['id']}, self.preset['id'], self.preset['revision'])
        self.assertEqual(request('POST', trigger, body)[0], 409)
        self.assertEqual(request('POST', trigger, {**body, 'revision': changed['revision']})[0], 409)
        self.store.save({'name': 'Disabled', 'media_id': self.image['id'], 'enabled': False}, changed['id'], changed['revision'])
        self.assertEqual(request('POST', trigger, body)[0], 404)

    def real_devices(self, routes=None):
        import copy
        from test_media_routing import FakeMedia, configuration
        from unittest.mock import Mock
        media = FakeMedia()
        state = {'input_count': 8, 'output_count': 4, 'routing': routes or [1, 2, 3, 4]}
        hub = Mock()
        hub.get_routing_state_strict.side_effect = lambda: copy.deepcopy(state)
        hub.route_video_output.side_effect = lambda *, output, input_: state['routing'].__setitem__(output, input_ + 1)
        hub.verify_video_output_route.side_effect = lambda *, output, input_: state['routing'][output] == input_ + 1
        self.cfg.update(configuration())
        from media_player_usage import PlayerUsage
        for context in (patch('media_routing.PLAYER_USAGE', PlayerUsage()),
                        patch.object(webui, '_get_media_routing_manager', REAL_ROUTING_FACTORY),
                        patch.object(webui, '_media_routing_instance', None),
                        patch.object(webui, 'get_videohub_client_from_config', return_value=hub),
                        patch('atem_media.get_atem_media_manager', return_value=media),
                        patch.object(webui, '_invalidate_videohub_state_snapshot')):
            self.stack.enter_context(context)
        return media, hub, state

    def trigger_real(self, preset=None, output=1, headers=None):
        import time
        preset = preset or self.preset
        headers = headers or self.token()
        listing = self.client.get('/api/automation/routing-presets', headers=headers).get_json()
        body = {'revision': preset['revision'], 'execution_id': uuid.uuid4().hex,
                'issued_at': int(time.time()), 'execution_epoch': listing['execution_epoch']}
        if output is not None:
            body['output'] = output
        response = self.client.post('/api/automation/routing-presets/' + preset['id'] + '/trigger', headers=headers, json=body)
        return response, body, headers

    def wait_real(self, body, headers):
        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.client.get('/api/automation/routing-presets/jobs/' + body['execution_id'], headers=headers)
            self.assertEqual(response.status_code, 200, response.get_json())
            job = response.get_json()['job']
            if job['status'] in ('failed', 'succeeded'):
                return job
            time.sleep(.01)
        self.fail('Real coordinator failed to terminate')

    def test_real_factory_exclusive_image_then_actions_and_shared_refusal(self):
        media, hub, state = self.real_devices()
        response, body, headers = self.trigger_real()
        self.assertEqual(response.status_code, 202, response.get_json())
        job = self.wait_real(body, headers)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertTrue(job['imageDisplayed'])
        self.assertEqual(job['actionsCompleted'], 1)
        self.assertEqual(state['routing'], [5, 2, 3, 4])
        self.assertEqual(len(self.calls), 1)
        # Both alternatives already feed other receivers: no upload or route may occur.
        state['routing'][:] = [1, 5, 6, 4]
        before = len(media.calls)
        response, body, headers = self.trigger_real()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.wait_real(body, headers)['status'], 'failed')
        self.assertEqual(len(media.calls), before)
        self.assertEqual(state['routing'], [1, 5, 6, 4])
        self.assertEqual(len(self.calls), 1)

    def test_real_fixed_output_partial_actions_and_revoked_token(self):
        media, hub, state = self.real_devices()
        preset = self.store.save({'name': 'Fixed', 'media_id': self.image['id'], 'output': 2,
                                 'actions': [fixture.action('First'), fixture.action('Second'), fixture.action('Third')]})
        calls = []
        def operation():
            self.assertIn(state['routing'][1], (5, 6))
            calls.append(webui.request.get_json())
            return webui.jsonify(ok=len(calls) != 2), (400 if len(calls) == 2 else 200)
        with patch.dict(webui.app.view_functions, {'api_apply_timer_preset': operation}):
            response, body, headers = self.trigger_real(preset, output=3)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(media.calls, [])
            response, body, headers = self.trigger_real(preset, output=None)
            self.assertEqual(response.status_code, 202)
            job = self.wait_real(body, headers)
        self.assertEqual(job['status'], 'failed', job)
        self.assertTrue(job['imageDisplayed'])
        self.assertEqual(job['actionsCompleted'], 1)
        self.assertEqual(len(calls), 2)
        for token in api_security.list_service_tokens(webui._AUTH_DB_PATH):
            api_security.revoke_service_token(webui._AUTH_DB_PATH, token['id'])
        self.assertEqual(self.client.get('/api/automation/routing-presets', headers=headers).status_code, 401)

    def test_real_factory_busy_and_config_replacement_retains_pending_job(self):
        import time
        media, hub, state = self.real_devices()
        media.immediate = False
        response, body, headers = self.trigger_real()
        self.assertEqual(response.status_code, 202)
        deadline = time.monotonic() + 3
        while media.callback is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNotNone(media.callback)
        original = REAL_ROUTING_FACTORY(refresh=True)
        self.cfg['atem_ip'] = '192.0.2.9'
        self.assertIs(REAL_ROUTING_FACTORY(refresh=True), original)
        self.assertEqual(self.trigger_real()[0].status_code, 409)
        media.close()  # Bounded cancellation/failed display must not execute actions.
        self.assertEqual(self.wait_real(body, headers)['status'], 'failed')
        self.assertEqual(self.calls, [])
        self.assertIsNot(REAL_ROUTING_FACTORY(refresh=True), original)
        self.cfg['atem_ip'] = '192.0.2.1'
        self.assertIsNot(REAL_ROUTING_FACTORY(refresh=True), original)

    def test_cli_supports_explicit_routing_scope_and_constraints(self):
        import cli
        import io
        with patch.object(cli, '_service_token_db_path', return_value=webui._AUTH_DB_PATH), \
             patch.object(cli, '_activity_log_cli_event'), patch('sys.stdout', new=io.StringIO()):
            result = cli.main(['service-tokens', 'create', 'Scoped CLI', '--scope', 'routing_presets',
                               '--routing-preset', self.preset['id'], '--routing-output', '26'])
        self.assertEqual(result, 0)
        saved = api_security.list_service_tokens(webui._AUTH_DB_PATH)[0]
        self.assertEqual(saved['constraints'], {'routing_presets': [self.preset['id']], 'routing_outputs': [26]})

    def test_new_constraints_never_parse_empty_or_all_as_unrestricted(self):
        for key in ('routing_presets', 'routing_outputs'):
            for value in ([], '', 'all', ['all'], [0], [True], None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.token(constraints={key: value})
        import sqlite3
        token = api_security.create_service_token(webui._AUTH_DB_PATH, name='Corrupt', scopes=['routing_presets'])
        from contextlib import closing
        with closing(sqlite3.connect(webui._AUTH_DB_PATH)) as conn:
            conn.execute("UPDATE service_tokens SET constraints_json=? WHERE id=?", ('{"routing_presets":[]}', token['id']))
            conn.commit()
        self.assertEqual(self.client.get('/api/automation/routing-presets', headers={'Authorization':'Bearer '+token['token']}).status_code, 401)

    def test_explicit_scope_catalog_trigger_and_owner_polling(self):
        headers = self.token(constraints={'routing_presets': [self.preset['id']], 'routing_outputs': [26]})
        path = '/api/automation/routing-presets'
        listing = self.client.get(path, headers=headers)
        self.assertEqual(listing.status_code, 200, listing.get_json())
        entry = listing.get_json()['presets'][0]
        self.assertEqual(entry['id'], self.preset['id'])
        self.assertIsNone(entry['output'])
        self.assertIsNone(entry['unavailable_reason'])
        self.assertNotIn('actions', entry)
        execution = uuid.uuid4().hex
        body = {'revision': self.preset['revision'], 'execution_id': execution, 'issued_at': int(__import__('time').time()), 'execution_epoch': listing.get_json()['execution_epoch'], 'output': 26}
        response = self.client.post(path + '/' + self.preset['id'] + '/trigger', headers=headers, json=body)
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(self.calls, [])
        self.assertTrue(self.manager.display.call_args.kwargs['refuse_shared'])
        self.callbacks[0]({'status': 'succeeded'})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['actor']['source'], 'api')
        self.assertRegex(self.calls[0]['actor']['actor_username'], r'^token:\d+:Companion fixture$')
        jobpath = path + '/jobs/' + execution
        self.assertEqual(self.client.get(jobpath, headers=headers).get_json()['job']['status'], 'succeeded')
        self.assertEqual(self.client.get(jobpath, headers=self.token()).status_code, 403)
        self.assertEqual(self.client.post(path + '/' + self.preset['id'] + '/trigger', headers=headers, json=body).status_code, 202)
        self.assertEqual(len(self.callbacks), 1)
        for scopes in (['read'], ['videohub']):
            self.assertEqual(self.client.get(path, headers=self.token(scopes)).status_code, 403)
        with self.user(1):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get('/api/routing/presets', headers=headers).status_code, 403)


# Avoid duplicate inherited browser tests while sharing fixture helpers.
for name in list(vars(fixture.RoutingPresetWebTests)):
    if name.startswith('test_'):
        setattr(AutomationTests, name, None)

if __name__ == '__main__':
    unittest.main()
