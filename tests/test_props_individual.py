"""Individual saved-library commands use the same safe direct-only runtime."""
import unittest

ID = '4cddf2c5-7355-4347-980c-b13f12cbfa85'


class IndividualTests(unittest.TestCase):
    def setUp(self):
        from test_props import PropsWebTests
        self.f = PropsWebTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.data = self.f.store.save_library([{'id': ID, 'name': 'Dedicated Welcome', 'prop_uuid': ID}],
            {'folders': [], 'items': [{'id': ID}]}, 'initial')

    def test_library_trigger_without_order_uses_one_command_and_distinct_history(self):
        f = self.f
        result = f.client.post('/api/v1/props/library/trigger', json={'id': ID, 'revision': self.data['revision']})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.headers['X-TDeck-API-Version'], '1')
        last = result.get_json()['last_triggered']
        self.assertEqual(last['id'], ID)
        self.assertIsNone(last['position'])
        self.assertEqual(last['kind'], 'library')
        self.assertEqual(f.client.get('/api/props').get_json()['last_triggered'], last)
        f.pp.get_command.assert_called_once_with('prop/' + ID + '/trigger')
        events = [c for c in f.log.call_args_list if c.args[0] == 'props.library.trigger']
        self.assertEqual(events[-1].kwargs['details'], last)
        self.assertEqual(f.store.read(), self.data)


    def test_invalid_stale_missing_deleted_unavailable_and_uncertain_commands_never_replay(self):
        f = self.f
        payload = {'id': ID, 'revision': self.data['revision']}
        for extra in ({'id': None}, {'id': []}, {'id': ID.upper()}, {'id': 'raw-uuid'}, {'id': True}):
            self.assertEqual(f.client.post('/api/props/library/trigger', json={**payload, **extra}).status_code, 400)
        for body in ({}, {**payload, 'position': 1}, {**payload, 'prop_uuid': ID}, {**payload, 'use_macros': False}):
            self.assertEqual(f.client.post('/api/props/library/trigger', json=body).status_code, 400)
        self.assertEqual(f.client.post('/api/props/library/trigger', json={**payload, 'revision': 'stale'}).status_code, 409)
        f.pp.get_json.return_value = []; f.pp.get_json.side_effect = None
        f.service.catalog.refresh_now()
        self.assertEqual(f.client.post('/api/props/library/trigger', json=payload).status_code, 400)
        f.pp.get_command.assert_not_called()
        f.pp.get_json.return_value = [{'id': {'uuid': ID, 'name': 'Original'}}]
        f.service.catalog.refresh_now()
        self.assertEqual(f.client.post('/api/props/library/trigger', json=payload).status_code, 200)
        last = f.service.last_triggered
        f.pp.get_command.return_value = None
        self.assertEqual(f.client.post('/api/props/library/trigger', json=payload).status_code, 502)
        self.assertEqual(f.pp.get_command.call_count, 2)
        self.assertEqual(f.service.last_triggered, last)
        deleted = f.store.save_library([], {'folders': [], 'items': []}, self.data['revision'])
        self.assertEqual(f.client.post('/api/props/library/trigger', json=payload).status_code, 409)
        self.assertEqual(f.client.post('/api/props/library/trigger', json={**payload, 'revision': deleted['revision']}).status_code, 400)
        self.assertEqual(f.pp.get_command.call_count, 2)

    def test_individual_and_numbered_commands_share_guard_and_capture_target_during_edits(self):
        import threading
        f = self.f
        data = f.store.save_order([ID], self.data['revision'])
        entered, release = threading.Event(), threading.Event()
        results = []
        def command(path):
            entered.set()
            if not release.wait(3): raise RuntimeError('Fixture timeout')
            return ''
        f.pp.get_command.side_effect = command
        def run():
            results.append(f.client.post('/api/props/library/trigger', json={'id': ID, 'revision': data['revision']}))
        writer = threading.Thread(target=run); writer.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(f.webui.app.test_client().get('/api/props').status_code, 200)
            for path, selection in (('/api/props/trigger', {'position': 1}), ('/api/props/library/trigger', {'id': ID})):
                self.assertEqual(f.webui.app.test_client().post(path, json={**selection, 'revision': data['revision']}).status_code, 409)
            edited = f.store.save_order([], data['revision'])
            f.store.save_library([], {'folders': [], 'items': []}, edited['revision'])
        finally:
            release.set(); writer.join(4)
        self.assertFalse(writer.is_alive())
        self.assertEqual(results[0].status_code, 200)
        self.assertEqual(results[0].get_json()['last_triggered']['revision'], data['revision'])
        f.pp.get_command.assert_called_once_with('prop/' + ID + '/trigger')


class IndividualSecurityTests(unittest.TestCase):
    def setUp(self):
        from test_props import PropsSecurityTests
        self.f = PropsSecurityTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.data = self.f.store.save_library([{'id': ID, 'name': 'Welcome', 'prop_uuid': ID}],
            {'folders': [], 'items': [{'id': ID}]}, 'initial')

    def test_explicit_policy_scope_page_csrf_origin_v1_and_audit(self):
        import api_security
        f = self.f
        path = '/api/v1/props/library/trigger'
        body = {'id': ID, 'revision': self.data['revision']}
        policy = f.webui._api_policy(path, 'POST')
        self.assertEqual(policy['scope'], 'props'); self.assertEqual(policy['pages'], ('page:props',))
        # New unknown Props endpoints must be fail-closed, not a wildcard grant.
        self.assertIsNone(f.webui._api_policy('/api/props/arbitrary/uuid/trigger', 'POST'))
        with f.user(2):
            for headers in ({}, {'Origin': 'https://evil.test', 'X-CSRF-Token': 'props-csrf'}):
                self.assertEqual(f.client.post(path, json=body, headers=headers).status_code, 403)
            self.assertEqual(f.client.post(path, json=body, headers=f.headers).status_code, 200)
            self.assertEqual(f.client.post(path, json={**body, 'revision': 'stale'}, headers=f.headers).status_code, 409)
        with f.user(3):
            self.assertEqual(f.client.post(path, json=body, headers=f.headers).status_code, 403)
        for scope, code in [('read', 403), ('props', 200)]:
            record = api_security.create_service_token(f.webui._AUTH_DB_PATH, name='Individual fixture', scopes=[scope])
            self.assertEqual(f.client.post(path, json=body, headers={'Authorization': 'Bearer ' + record['token']}).status_code, code)
        from flask_login import AnonymousUserMixin
        from unittest.mock import patch
        with patch.object(f.webui, 'current_user', AnonymousUserMixin()):
            self.assertEqual(f.client.post(path, json=body).status_code, 401)
        self.assertEqual(f.pp.get_command.call_count, 2)
        self.assertTrue(any(c.args[0] == 'props.library.trigger' and c.kwargs.get('status') == 'failure' for c in f.log.call_args_list) or
                        any(c.args[0].startswith('api.') for c in f.log.call_args_list))


if __name__ == '__main__':
    unittest.main()
