import time
import unittest
from contextlib import closing
from tv_fixture import TVFixture


class TvSecurityTests(unittest.TestCase):
    def setUp(self):
        self.fx = TVFixture().__enter__()
        self.addCleanup(self.fx.__exit__, None, None, None)
        self.w = self.fx.w
        self.client = self.fx.client()
        self.headers = self.fx.headers(self.client)

    def test_real_auth_room_filters_all_legacy_and_versioned_paths(self):
        for prefix in ('/api', '/api/v1'):
            state = self.client.get(prefix + '/tvs')
            self.assertEqual(state.status_code, 200, state.data)
            self.assertEqual([tv['id'] for tv in state.json['tvs']], ['foyer'])
            self.assertNotIn('192.0.2', str(state.json))
            for path in ('/tvs/private/state', '/tv-targets/tv:private/state', '/tv-targets/group:ground/state'):
                self.assertEqual(self.client.get(prefix + path).status_code, 403)
            for path, body in [('/tvs/private/power', {'state': 'on'}), ('/tv-targets/group:ground/power', {'state': 'off'}),
                               ('/tvs/foyer/power', {'state': 'toggle'}), ('/tvs/foyer/reconnect', {})]:
                self.assertIn(self.client.post(prefix + path, json=body, headers=self.headers).status_code, (400, 403))
        self.assertFalse(any(fake.commands for fake in self.fx.clients.values()))

    def test_diagnostics_readers_cannot_operate_and_scope_revocation_is_immediate(self):
        reader = self.fx.client('reader-tv')
        result = reader.get('/api/tv-pages/state?mode=diagnostics')
        self.assertEqual(len(result.json['tvs']), 2)
        self.assertEqual(reader.get('/tv-controls').status_code, 403)
        self.assertEqual(reader.post('/api/tvs/foyer/volume', json={'level': 40}, headers=self.fx.headers(reader)).status_code, 403)
        with closing(self.w._db()) as conn:
            conn.execute("DELETE FROM group_pages WHERE group_id=2 AND page_key='tvroom:ground'"); conn.commit()
        self.assertEqual(self.client.get('/api/tvs/foyer/state').status_code, 403)
        self.assertEqual(self.client.get('/api/tv-pages/state').json['tvs'], [])
        self.assertEqual(self.client.get('/api/tv-media/foyer/images').status_code, 403)
        self.assertEqual(self.client.get('/tv-media/foyer/' + self.fx.image['id'] + '/thumbnail').status_code, 403)

    def test_browser_csrf_origin_tokens_and_companion_failure_recovery(self):
        import api_security
        self.assertEqual(self.client.post('/api/tvs/foyer/volume', json={'level': 30}).status_code, 403)
        self.assertEqual(self.client.post('/api/tvs/foyer/volume', json={'level': 30}, headers={**self.headers, 'Origin': 'https://invalid.example'}).status_code, 403)
        token = api_security.create_service_token(self.fx.root / 'auth.db', name='Fixture Companion', scopes=['tvs'])
        machine = self.w.app.test_client()
        headers = {'Authorization': 'Bearer ' + token['token']}
        for path in ('/api/tv-pages/state', '/api/tv-media/foyer/images', '/api/config/tv-media-mappings'):
            self.assertEqual(machine.get(path, headers=headers).status_code, 403)
        self.fx.clients['foyer'].fail = True
        response = machine.post('/api/tv-targets/tv:foyer/volume', json={'level': 30}, headers=headers)
        self.assertEqual(response.status_code, 502)
        self.fx.clients['foyer'].fail = False
        self.fx.manager.get('foyer')._poll()
        result = self.client.get('/api/tv-pages/state?mode=diagnostics').json['tvs'][0]
        self.assertEqual(result['observation']['failures'][0]['operation'], 'volume_set')
        self.assertNotIn('fixture-private-secret', str(result))
        self.assertFalse(result['readError'])
        response = machine.post('/api/tv-targets/group:ground/volume', json={'level': 28}, headers=headers)
        self.assertEqual(response.status_code, 200, response.data)
        deadline = time.monotonic() + 2
        while self.fx.clients['hall'].volume != 28 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.fx.clients['hall'].volume, 28)
        self.assertTrue(any(call.args[0] == 'hisense.command.complete' for call in self.fx.log.call_args_list))
        completed = [call for call in self.fx.log.call_args_list if call.args[0] == 'hisense.command.complete']
        self.assertTrue(any(call.kwargs.get('source') == 'companion' and 'Fixture Companion' in call.kwargs.get('actor_username', '') for call in completed))

    def test_media_has_no_unrestricted_routing_authority_and_jobs_are_owner_scoped(self):
        for path in ('/api/media', '/api/media/display/not-a-job', '/api/videohub/state', '/api/atem/media/state'):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        listing = self.client.get('/api/tv-media/foyer/images')
        self.assertEqual(listing.status_code, 200, listing.data)
        item = listing.json['items'][0]
        image_response = self.client.get(item['thumbnail_url'])
        self.assertEqual(image_response.status_code, 200)
        image_response.close()
        response = self.client.post('/api/tv-media/foyer/display', headers=self.headers, json={'media_id': item['id'], 'output': 2})
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/tv-media/foyer/display', headers=self.headers, json={'media_id': item['id']})
        self.assertEqual(response.status_code, 202, response.data)
        job_id = response.json['job']['id']
        path = '/api/tv-media/foyer/display/' + job_id
        other = self.fx.client('admin-tv')
        self.assertEqual(other.get(path).status_code, 404)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(path)
            if response.json['job']['status'] not in ('queued', 'allocating', 'loading', 'routing', 'verifying', 'uploading'):
                break
            time.sleep(.05)
        self.assertEqual(response.json['job']['status'], 'succeeded', response.json)
        self.assertEqual(self.fx.hub.routing[0], 7)
        self.assertFalse(self.fx.clients['foyer'].commands)
        self.assertEqual(other.get('/api/media/display/' + job_id).status_code, 403)
        self.fx.cfg['hisense_media_mappings']['foyer']['output'] = 2
        self.assertEqual(self.client.get(path).status_code, 403)

    def test_shared_player_confirmation_and_changed_allocation_fail_closed(self):
        self.fx.cfg['atem_media_destinations'].append({'player': 4, 'label': 'Media B', 'slots': [43, 44], 'videohub_input': 8})
        self.fx.hub.routing[:] = [1, 7, 8]
        image = self.fx.image['id']
        response = self.client.post('/api/tv-media/foyer/display', headers=self.headers, json={'media_id': image})
        self.assertEqual(response.status_code, 202)
        path = '/api/tv-media/foyer/display/' + response.json['job']['id']
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.client.get(path).json['job']
            if job['status'] == 'confirmation_required':
                break
            time.sleep(.02)
        self.assertEqual(job['status'], 'confirmation_required', job)
        self.assertIn('shared_confirmation', job)
        self.assertIsNone(self.fx.atem.job)
        self.fx.hub.routing[:] = [1, 8, 7]
        response = self.client.post('/api/tv-media/foyer/display', headers=self.headers,
                    json={'media_id': image, 'shared_confirmation': job['shared_confirmation']})
        self.assertEqual(response.status_code, 202)
        path = '/api/tv-media/foyer/display/' + response.json['job']['id']
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.client.get(path).json['job']
            if job['status'] == 'confirmation_required':
                break
            time.sleep(.02)
        self.assertEqual(job['status'], 'confirmation_required')
        self.assertIsNone(self.fx.atem.job)

    def test_mapping_save_nonexecuting_and_input_feedback_required(self):
        admin = self.fx.client('admin-tv')
        mappings = {'foyer': {'output': 2, 'source': 'HDMI2'}}
        response = admin.put('/api/config/tv-media-mappings', headers=self.fx.headers(admin), json={'mappings': mappings})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(admin.get('/api/config/tv-media-mappings').json['mappings'], mappings)
        self.assertFalse(self.fx.clients['foyer'].commands)
        self.assertEqual(self.client.post('/api/tv-media/foyer/display', headers=self.headers, json={'media_id': self.fx.image['id']}).status_code, 409)
        self.assertEqual(self.client.put('/api/config/tv-media-mappings', headers=self.headers, json={'mappings': {}}).status_code, 403)

    def test_moving_tv_revokes_previous_room_grants_without_renumbering(self):
        self.fx.cfg['hisense_tv_groups'][0]['tv_ids'] = ['hall']
        self.fx.cfg['hisense_tv_groups'][1]['tv_ids'].append('foyer')
        self.assertEqual(self.client.get('/api/tvs/foyer/state').status_code, 403)
        self.assertEqual(self.client.post('/api/tv-targets/tv:foyer/power', headers=self.headers, json={'state': 'on'}).status_code, 403)

    def test_legacy_reads_union_diagnostics_and_controls_without_granting_writes(self):
        with closing(self.w._db()) as conn:
            conn.execute("DELETE FROM group_pages WHERE group_id=2 AND page_key='page:tv_diagnostics'")
            conn.execute("DELETE FROM group_pages WHERE group_id=3 AND page_key='tvdevice:ground:*'")
            conn.execute("INSERT INTO group_pages(group_id,page_key) VALUES(3,'tvdevice:ground:hall')")
            conn.execute('INSERT INTO user_groups(user_id,group_id) VALUES(2,3)'); conn.commit()
        self.assertEqual(self.client.get('/api/tvs/foyer/state').status_code, 200)
        self.assertEqual(self.client.get('/api/tvs/hall/state').status_code, 200)
        self.assertEqual({tv['id'] for tv in self.client.get('/api/tvs').json['tvs']}, {'foyer', 'hall'})
        self.assertEqual(self.client.post('/api/tvs/hall/volume', headers=self.headers, json={'level': 40}).status_code, 403)
