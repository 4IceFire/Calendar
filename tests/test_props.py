import importlib.util
import tempfile
import unittest
from pathlib import Path


class PropsStoreTests(unittest.TestCase):

    def test_import_persists_canonical_organization_without_mutating_input(self):
        from props import PropsStore
        import copy
        with tempfile.TemporaryDirectory() as root:
            store = PropsStore(Path(root) / 'props.json')
            document = store.read()
            for organization, expected in [({}, {'folders': [], 'items': []}),
                    ({'folders': [{'id': ' folder ', 'name': 42}], 'items': []},
                     {'folders': [{'id': 'folder', 'name': '42', 'order': 0}], 'items': []})]:
                document['organization'] = organization
                original = copy.deepcopy(document)
                saved = store.import_document(document)
                self.assertEqual(saved['organization'], expected)
                self.assertEqual(store.read()['organization'], expected)
                self.assertEqual(document, original)

    def test_import_revalidates_document_atomically_and_invalidates_revision(self):
        from props import PropsStore
        with tempfile.TemporaryDirectory() as root:
            store = PropsStore(Path(root) / 'props.json')
            self.assertTrue(hasattr(store, 'import_document'), 'Validated import missing')
            before = store.save_order([], 'initial')
            restored = store.import_document(before)
            self.assertNotEqual(before['revision'], restored['revision'])
            with self.assertRaises(ValueError):
                store.import_document({})
            self.assertEqual(store.read(), restored)

    def test_external_uuid_case_is_preserved_and_aliases_are_not_distinct_props(self):
        from unittest.mock import Mock
        from props import PropsService, PropsStore
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.store = PropsStore(Path(temp.name) / 'props.json')
        upper = '4CDDF2C5-7355-4347-980C-B13F12CBFA85'
        lower = upper.lower()
        mapping = {'id':lower,'name':'Welcome','prop_uuid':upper}
        data = self.store.save_library([mapping], {'folders':[],'items':[{'id':lower}]}, 'initial')
        duplicate = {**mapping, 'id':'203878e3-0fd0-4e47-9dc6-fdeaf5185368', 'prop_uuid':lower}
        with self.assertRaises(ValueError): self.store.save_library([mapping, duplicate], {'folders':[],'items':[]}, data['revision'])
        data = self.store.save_order([lower], data['revision'])
        pp = Mock(); pp.get_json.side_effect = lambda kind: [{'id':{'uuid':lower,'name':'Original'}}] if kind == 'props' else []
        pp.get_command.return_value = ''
        service = PropsService(self.store, lambda: pp); service.catalog.refresh_now()
        self.assertTrue(service.state()['presets'][0]['available'])
        service.trigger(1, data['revision'])
        pp.get_command.assert_called_once_with('prop/' + upper + '/trigger')
        pp.get_json.side_effect = lambda kind: [{'id':{'uuid':upper,'name':'Original'}}] if kind == 'props' else []
        service.catalog.refresh_now()
        self.assertEqual(service.state()['catalog']['props'][0]['uuid'], upper)
        self.assertTrue(service.state()['presets'][0]['available'])

    def test_catalog_completion_logs_success_and_failure_without_losing_snapshot(self):
        from props import PropsService, PropsStore
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as root:
            pp = Mock(); pp.get_json.return_value = []
            service = PropsService(PropsStore(Path(root) / 'props.json'), lambda: pp)
            self.assertTrue(hasattr(service, 'on_catalog'), 'Catalog completion logging missing')
            events = []; service.on_catalog = lambda success: events.append(success)
            self.assertTrue(service.catalog.refresh_now())
            pp.get_json.return_value = None
            self.assertFalse(service.catalog.refresh_now())
            self.assertEqual(events, [True, False])

    def test_atomic_concurrent_saves_and_limits(self):
        from props import PropsStore, PropsConflict
        import threading
        with tempfile.TemporaryDirectory() as root:
            store = PropsStore(Path(root) / 'props.json')
            results = []
            barrier = threading.Barrier(2)
            def save():
                barrier.wait()
                try: store.save_order([], 'initial'); results.append('saved')
                except PropsConflict: results.append('conflict')
            threads = [threading.Thread(target=save) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(3)
            self.assertEqual(sorted(results), ['conflict','saved'])
            for order in (None, 'bad', [False], ['missing'] * 501):
                with self.assertRaises(ValueError): store.save_order(order, store.read()['revision'])
            with self.assertRaises(ValueError): store.save_library([{}] * 501, {}, store.read()['revision'])
            store.path.write_text(' ' * (1024 * 1024 + 1))
            with self.assertRaises(OSError): store.read()

    def test_nonblocking_singleflight_catalog_and_invalid_targets_never_trigger(self):
        from props import PropsStore, PropsService, PropsConflict
        from unittest.mock import Mock
        import threading, time
        with tempfile.TemporaryDirectory() as root:
            store = PropsStore(Path(root) / 'props.json')
            pp = Mock(); entered = threading.Event(); release = threading.Event()
            def catalog(kind):
                entered.set(); release.wait(3); return []
            pp.get_json.side_effect = catalog
            service = PropsService(store, lambda: pp)
            start = time.monotonic(); first = service.state()
            self.assertLess(time.monotonic() - start, .5)
            self.assertTrue(entered.wait(1))
            self.assertTrue(first['catalog']['stale'])
            for _ in range(10): service.state(refresh=True)
            self.assertEqual(pp.get_json.call_count, 1)
            release.set()
            deadline = time.monotonic() + 3
            while service.catalog.diagnostics()['refreshing'] and time.monotonic() < deadline: time.sleep(.01)
            self.assertEqual(pp.get_json.call_count, 1)
            for position in (0, True, '1', 1):
                with self.assertRaises(ValueError): service.trigger(position, 'initial')
            with self.assertRaises(PropsConflict): service.trigger(1, 'stale')
            self.assertEqual(pp.get_command.call_count, 0)

    def test_empty_versioned_store_round_trip(self):
        self.assertIsNotNone(importlib.util.find_spec('props'), 'Props implementation missing')
        from props import PropsStore
        with tempfile.TemporaryDirectory() as root:
            store = PropsStore(Path(root) / 'props.json')
            initial = store.read()
            self.assertEqual(initial['order'], [])
            saved = store.save_order([], initial['revision'])
            self.assertNotEqual(saved['revision'], initial['revision'])
            self.assertEqual(PropsStore(store.path).read(), saved)


    def test_mappings_repeated_order_and_delete_protection(self):
        from props import PropsStore
        with tempfile.TemporaryDirectory() as root:
            store = PropsStore(Path(root) / 'props.json')
            self.assertTrue(hasattr(store, 'save_library'), 'Library API missing')
            prop = '4cddf2c5-7355-4347-980c-b13f12cbfa85'
            data = store.save_library([{'id': prop, 'name': 'Welcome', 'prop_uuid': prop}],
                                      {'folders': [], 'items': [{'id': prop, 'folderId': None}]}, 'initial')
            data = store.save_order([prop, prop], data['revision'])
            self.assertEqual(data['order'], [prop, prop])
            with self.assertRaises(ValueError):
                store.save_library([], {'folders': [], 'items': []}, data['revision'])
            with self.assertRaises(ValueError):
                store.save_library(data['library'] * 2, data['organization'], data['revision'])
            with self.assertRaises(ValueError):
                store.save_order(['missing'], data['revision'])
            with self.assertRaises(ValueError):
                store.save_order([], 'stale')


    def test_catalog_trigger_and_last_success_without_replay(self):
        import props
        from unittest.mock import Mock
        self.assertTrue(hasattr(props, 'PropsService'), 'Runtime missing')
        with tempfile.TemporaryDirectory() as root:
            store = props.PropsStore(Path(root) / 'props.json')
            prop = '4cddf2c5-7355-4347-980c-b13f12cbfa85'
            entry = {'id': prop, 'name': 'Welcome', 'prop_uuid': prop}
            data = store.save_library([entry], {'folders': [], 'items': [{'id': prop}]}, 'initial')
            data = store.save_order([prop, prop], data['revision'])
            client = Mock()
            client.get_json.return_value = [{'id': {'uuid': prop, 'name': 'PP title'}}]
            client.get_command.return_value = ''
            service = props.PropsService(store, lambda: client)
            self.assertTrue(service.catalog.refresh_now())
            state = service.state()
            self.assertEqual([p['position'] for p in state['presets']], [1, 2])
            self.assertTrue(state['presets'][0]['available'])
            service.trigger(2, data['revision'])
            client.get_command.assert_called_once_with('prop/' + prop + '/trigger')
            self.assertEqual(service.state()['last_triggered']['position'], 2)
            client.get_command.return_value = None
            with self.assertRaises(RuntimeError):
                service.trigger(1, data['revision'])
            self.assertEqual(client.get_command.call_count, 2)
            self.assertEqual(client.get_command.call_args.args, ('prop/' + prop + '/trigger',))
            self.assertEqual(service.state()['last_triggered']['position'], 2)
            client.get_json.side_effect = RuntimeError('outage')
            self.assertFalse(service.catalog.refresh_now())
            self.assertEqual(len(service.state()['catalog']['props']), 1)
            self.assertIn('outage', service.state()['catalog']['lastError'])
            self.assertTrue(service.state()['catalog']['stale'])
            self.assertFalse(service.state()['presets'][0]['available'])


    def test_corrupt_schema_and_size_fail_closed(self):
        from props import PropsStore
        with tempfile.TemporaryDirectory() as root:
            store = PropsStore(Path(root) / 'props.json')
            for raw in ('{broken', '{}', '{"version":2}', '[]'):
                store.path.write_text(raw)
                with self.assertRaises(OSError):
                    store.read()
                with self.assertRaises(OSError):
                    store.save_order([], 'initial')
                self.assertEqual(store.path.read_text(), raw)


class PropsWebTests(unittest.TestCase):
    def setUp(self):
        import threading
        from unittest.mock import patch, Mock
        with patch.object(threading.Thread, 'start'):
            import webui
        from props import PropsStore, PropsService
        self.webui = webui
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PropsStore(Path(self.temp.name) / 'props.json')
        self.pp = Mock()
        self.pp.get_json.side_effect = lambda kind: [{'id': {'uuid': '4cddf2c5-7355-4347-980c-b13f12cbfa85', 'name': 'Original'}}] if kind == 'props' else []
        self.pp.get_command.return_value = ''
        self.service = PropsService(self.store, lambda: self.pp)
        self.service.catalog.refresh_now()
        self.cfg = {'auth_enabled': False, 'propresenter_props_use_macros': False}
        for target, value in [('_auth_enabled', lambda: False), ('_get_props_service', lambda: self.service)]:
            p = patch.object(webui, target, value, create=True); p.start(); self.addCleanup(p.stop)
        for target in ('get_config',):
            p = patch.object(webui.utils, target, return_value=self.cfg); p.start(); self.addCleanup(p.stop)
        p = patch.object(webui, 'log_event'); self.log = p.start(); self.addCleanup(p.stop)
        self.client = webui.app.test_client()


    def test_slow_trigger_keeps_state_reads_nonblocking_and_second_trigger_guarded(self):
        import threading
        from unittest.mock import patch
        prop = '4cddf2c5-7355-4347-980c-b13f12cbfa85'
        data = self.store.save_library([{'id': prop, 'name': 'Welcome', 'prop_uuid': prop}],
            {'folders': [], 'items': [{'id': prop}]}, 'initial')
        data = self.store.save_order([prop], data['revision'])
        entered, release, read_done = [threading.Event() for _ in range(3)]
        results = {}
        def command(path):
            entered.set()
            if not release.wait(3): raise RuntimeError('Test trigger release timed out')
            return ''
        def trigger():
            results['trigger'] = self.webui.app.test_client().post('/api/props/trigger', json={
                'position': 1, 'revision': data['revision'], 'use_macros': False})
        def read():
            results['read'] = self.webui.app.test_client().get('/api/v1/props')
            read_done.set()
        with patch.object(self.webui, '_auth_enabled', return_value=False):
            self.pp.get_command.side_effect = command
            writer, reader = threading.Thread(target=trigger), threading.Thread(target=read)
            writer.start()
            try:
                self.assertTrue(entered.wait(1))
                reader.start()
                self.assertTrue(read_done.wait(.5), 'GET waited for slow hardware trigger')
                self.assertEqual(results['read'].status_code, 200)
                self.assertIsNone(results['read'].get_json()['last_triggered'])
                self.assertEqual(self.client.put('/api/props/settings', json={
                    'revision': data['revision'], 'previous_use_macros': False, 'use_macros': True}).status_code, 404)
                self.assertEqual(self.client.post('/api/props/trigger', json={
                    'position': 1, 'revision': data['revision'], 'use_macros': False}).status_code, 409)
                edited = self.client.put('/api/props/order', json={'revision': data['revision'], 'order': []})
                self.assertEqual(edited.status_code, 200)
            finally:
                release.set(); writer.join(3)
                if reader.ident is not None: reader.join(3)
            self.assertFalse(writer.is_alive()); self.assertFalse(reader.is_alive())
            self.assertEqual(results['trigger'].status_code, 200)
            current = self.client.get('/api/props').get_json()
        self.assertEqual(current['order'], [])
        self.assertEqual(current['last_triggered']['revision'], data['revision'])
        self.assertFalse(current['use_macros'])
        self.pp.get_command.assert_called_once_with('prop/' + prop + '/trigger')


    def test_general_config_roundtrip_strips_retired_setting(self):
        from unittest.mock import patch
        self.cfg['propresenter_props_use_macros'] = True
        snapshot = self.client.get('/api/config').get_json()
        self.assertNotIn('propresenter_props_use_macros', snapshot)
        snapshot['debug'] = True
        with patch.object(self.webui.utils, 'save_config', side_effect=lambda cfg: (self.cfg.clear(), self.cfg.update(cfg))), patch.object(self.webui.utils, 'reload_config'):
            response = self.client.post('/api/config', json=snapshot)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('propresenter_props_use_macros', response.get_json()['config'])
        self.assertNotIn('propresenter_props_use_macros', self.cfg)

    def test_config_import_strips_retired_setting_and_restores_other_config(self):
        import json, zipfile
        from unittest.mock import patch
        root = Path(self.temp.name)
        target = root / 'config.json'
        target.write_text(json.dumps(self.cfg))
        with patch.object(self.webui, '_APP_ROOT', root), patch.object(self.webui.utils, 'CONFIG_FILE', str(target)), patch.object(self.webui, '_clear_imported_config_caches'):
            payload, _ = self.webui._create_config_transport_zip(['config'], reason='test')
            archive = root / 'import.zip'
            source = root / 'source.zip'; source.write_bytes(payload)
            with zipfile.ZipFile(source) as src, zipfile.ZipFile(archive, 'w') as dst:
                for name in src.namelist():
                    data = src.read(name)
                    if name == 'payload/config.json':
                        data = json.dumps({'propresenter_props_use_macros': True, 'debug': True, 'propresenter_is_latest': False})
                    dst.writestr(name, data)
            imported, _ = self.webui._apply_config_transport_import(archive, ['config'])
            self.assertEqual(imported[0]['id'], 'config')
            restored = json.loads(target.read_text())
            self.assertNotIn('propresenter_props_use_macros', restored)
            self.assertTrue(restored['debug'])
            self.assertIs(restored['propresenter_is_latest'], False)

    def test_config_zip_exports_imports_validated_props_and_fresh_revision(self):
        import os, zipfile, json
        from unittest.mock import patch
        prop = '4cddf2c5-7355-4347-980c-b13f12cbfa85'
        data = self.store.save_library([{'id':prop,'name':'Welcome','prop_uuid':prop}], {'folders':[],'items':[{'id':prop}]}, 'initial')
        data = self.store.save_order([prop, prop], data['revision'])
        root = Path(self.temp.name)
        with patch.object(self.webui, '_APP_ROOT', root), patch.dict(os.environ, {'TDECK_PROPS_FILE':str(self.store.path)}), patch.object(self.webui, '_clear_imported_config_caches'):
            payload, included = self.webui._create_config_transport_zip(['props'], reason='test')
            self.assertEqual([item['id'] for item in included], ['props'])
            archive = root / 'export.zip'; archive.write_bytes(payload)
            self.store.save_order([], data['revision'])
            imported, backup = self.webui._apply_config_transport_import(archive, ['props'])
            self.assertEqual(imported[0]['id'], 'props')
            self.assertTrue(backup.exists())
            restored = self.store.read()
            self.assertEqual(restored['order'], [prop, prop])
            self.assertNotEqual(restored['revision'], data['revision'])
            with zipfile.ZipFile(archive) as zf:
                manifest = json.loads(zf.read('manifest.json'))
            invalid = root / 'invalid.zip'
            with zipfile.ZipFile(invalid, 'w') as zf:
                zf.writestr('manifest.json', json.dumps(manifest)); zf.writestr('payload/props.json', '{}')
            with self.assertRaises(ValueError): self.webui._apply_config_transport_import(invalid, ['props'])
            self.assertEqual(self.store.read(), restored)

    def test_malformed_library_values_fail_closed_without_server_error(self):
        before = self.store.read()
        base = {'id':'4cddf2c5-7355-4347-980c-b13f12cbfa85','name':'Welcome','prop_uuid':'4cddf2c5-7355-4347-980c-b13f12cbfa85'}
        for field in ('id', 'prop_uuid'):
            for invalid in ([], {}, 1):
                with self.subTest(field=field, invalid=invalid):
                    response = self.client.put('/api/props/library', headers=getattr(self, 'headers', None), json={'revision':'initial','library':[{**base, field:invalid}],'organization':{'folders':[],'items':[]}})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.store.read(), before)
        self.pp.get_command.assert_not_called()

    def test_library_order_trigger_v1_and_audit(self):
        response = self.client.get('/api/v1/props')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.headers['X-TDeck-API-Version'], '1')
        data = response.get_json()
        prop = '4cddf2c5-7355-4347-980c-b13f12cbfa85'
        entry = {'id': prop, 'name': 'Welcome', 'prop_uuid': prop}
        response = self.client.put('/api/props/library', json={'revision': data['revision'], 'library': [entry],
                                          'organization': {'folders': [], 'items': [{'id': prop}]}})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.get_json()
        response = self.client.put('/api/props/order', json={'revision': data['revision'], 'order': [prop, prop]})
        self.assertEqual(response.status_code, 200, response.data)
        data = response.get_json()
        self.assertEqual([x['position'] for x in data['presets']], [1, 2])
        response = self.client.post('/api/v1/props/trigger', json={'revision': data['revision'], 'position': 2, 'use_macros': False})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.client.get('/api/props').get_json()['last_triggered']['position'], 2)
        self.assertEqual(self.pp.get_command.call_count, 1)
        self.assertGreaterEqual(self.log.call_count, 3)
        trigger_logs = [call for call in self.log.call_args_list if call.args[0] == 'props.preset.trigger']
        self.assertEqual(trigger_logs[-1].kwargs['details']['position'], 2)
        self.assertEqual(trigger_logs[-1].kwargs['details']['id'], prop)


class PropsSecurityTests(PropsWebTests):
    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        web = self.webui
        self.cfg.update(auth_enabled=True, api_legacy_anonymous_enabled=False, api_write_rate_limit_per_minute=600)
        for p in (patch.object(web, '_AUTH_DB_PATH', Path(self.temp.name) / 'auth.db'),
                  patch.object(web, '_auth_enabled', return_value=True),
                  patch.object(web, '_auth_cfg', return_value=self.cfg),
                  patch.object(web, '_bootstrap_default_users_roles'),
                  patch.object(web, '_touch_current_user_session', return_value=True)):
            p.start(); self.addCleanup(p.stop)
        web._init_auth_db()
        conn = web._db()
        conn.executemany('INSERT INTO users(id,username,password_hash) VALUES(?,?,?)', [(1,'fixture-admin','unused'), (2,'operator','unused'), (3,'other','unused')])
        conn.executemany('INSERT INTO groups(id,name,is_admin) VALUES(?,?,?)', [(1,'Admin',1), (2,'Props',0), (3,'Configure',0)])
        conn.executemany('INSERT INTO user_groups(user_id,group_id) VALUES(?,?)', [(1,1),(2,2),(2,3),(3,3)])
        conn.commit(); conn.close()
        web._set_group_pages(2, ['page:props'])
        web._set_group_pages(3, ['page:props_configure'])
        with self.client.session_transaction() as s: s['_csrf'] = 'props-csrf'
        self.headers = {'Origin': 'http://localhost', 'X-CSRF-Token': 'props-csrf'}
        with web._api_rate_lock: web._api_rate_events.clear()

    def user(self, uid):
        from unittest.mock import patch
        return patch.object(self.webui, 'current_user', self.webui._User(self.webui._user_record(uid)))

    def test_library_order_trigger_v1_and_audit(self):
        with self.user(1):
            self.assertEqual(self.client.get('/api/v1/props').status_code, 200)


    def test_malformed_library_values_fail_closed_without_server_error(self):
        with self.user(1):
            super().test_malformed_library_values_fail_closed_without_server_error()


    def test_general_config_roundtrip_strips_retired_setting(self):
        from unittest.mock import patch
        with patch.object(self.webui, '_auth_enabled', return_value=False):
            super().test_general_config_roundtrip_strips_retired_setting()


    def test_scoped_tokens_operate_but_never_configure_and_legacy_is_denied(self):
        import api_security
        prop = '4cddf2c5-7355-4347-980c-b13f12cbfa85'
        data = self.store.save_library([{'id':prop, 'name':'Welcome', 'prop_uuid':prop}], {'folders': [], 'items':[{'id':prop}]}, 'initial')
        data = self.store.save_order([prop], data['revision'])
        record = api_security.create_service_token(self.webui._AUTH_DB_PATH, name='Props fixture', scopes=['props'])
        headers = {'Authorization':'Bearer ' + record['token']}
        self.assertEqual(self.client.get('/api/v1/props', headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/v1/props/trigger', headers=headers, json={'position':1,'revision':data['revision'],'use_macros':False}).status_code, 200)
        for path in ('/api/props/library', '/api/props/order'):
            self.assertEqual(self.client.put(path, headers=headers, json={}).status_code, 403)
        reader = api_security.create_service_token(self.webui._AUTH_DB_PATH, name='Reader', scopes=['read'])
        self.assertEqual(self.client.get('/api/props', headers={'Authorization':'Bearer ' + reader['token']}).status_code, 200)
        self.assertEqual(self.client.post('/api/props/trigger', headers={'Authorization':'Bearer ' + reader['token']}, json={}).status_code, 403)
        from datetime import datetime, timezone, timedelta
        self.cfg.update(api_legacy_anonymous_enabled=True, api_legacy_anonymous_until=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat())
        from flask_login import AnonymousUserMixin
        from unittest.mock import patch
        with patch.object(self.webui, 'current_user', AnonymousUserMixin()):
            self.assertEqual(self.client.get('/api/props').status_code, 403)
            self.cfg['api_legacy_anonymous_enabled'] = False
            self.assertEqual(self.client.get('/api/props').status_code, 401)
        self.assertEqual(self.pp.get_command.call_count, 1)

    def test_props_navigation_landing_and_transport(self):
        with self.user(2):
            page = self.client.get('/props')
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'href="/props">Props</a>', page.data)
            self.assertEqual(self.webui._landing_page_for_user(self.webui._User(self.webui._user_record(2))), '/props')
        with self.user(1):
            page = self.client.get('/admin/permissions')
            self.assertEqual(page.status_code, 200, page.data[:400])
            self.assertIn(b'data-permission-page="page:props"', page.data)
            self.assertIn(b'value="page:props_configure"', page.data)
        self.assertIn('props', self.webui._config_transport_item_map())

    def test_page_union_configure_dependency_csrf_and_no_anonymous(self):
        web = self.webui
        with self.user(2):
            self.assertTrue(web.can_access('page:props_configure'))
            self.assertEqual(self.client.get('/api/props').status_code, 200)
            self.assertEqual(self.client.put('/api/props/order', json={'revision':'initial','order':[]}).status_code, 403)
            self.assertEqual(self.client.put('/api/props/order', headers=self.headers, json={'revision':'initial','order':[]}).status_code, 200)
            self.assertEqual(self.client.post('/api/props/catalog/refresh', headers=self.headers).status_code, 200)
        web._set_group_pages(3, [])
        with self.user(2):
            self.assertEqual(self.client.put('/api/props/library', headers=self.headers, json={}).status_code, 403)
        web._set_group_pages(3, ['page:props_configure'])
        with self.user(3):
            self.assertFalse(web.can_access('page:props_configure'))
            self.assertEqual(self.client.get('/api/props').status_code, 403)
        with web.app.test_request_context('/api/props'):
            self.assertFalse(web._api_legacy_path_allowed('/api/props', 'GET'))


class PropsFactoryLifecycleTests(unittest.TestCase):
    def test_endpoint_save_and_import_keep_trigger_guard_and_completion_history(self):
        import io, json, os, threading, zipfile
        from unittest.mock import patch
        import props_routes
        with patch.object(threading.Thread, 'start'):
            import webui as web
        from props import PropsStore

        prop = '4CDDF2C5-7355-4347-980C-B13F12CBFA85'
        identity = prop.lower()
        # Exercise both actual configuration write paths, never a fixed service.
        for write_path, trigger_kind in ((path, kind) for path in ('save', 'import') for kind in ('preset', 'library')):
            with self.subTest(write_path=write_path, trigger_kind=trigger_kind), tempfile.TemporaryDirectory() as root_name:
                root = Path(root_name)
                config_path = root / 'config.json'
                cfg = {'auth_enabled': False, 'propresenter_ip': '127.0.0.1',
                       'propresenter_port': 1400, 'propresenter_props_use_macros': False}
                config_path.write_text(json.dumps(cfg))
                store = PropsStore(root / 'props.json')
                data = store.save_library([{'id': identity, 'name': 'Welcome', 'prop_uuid': prop}], {'folders': [], 'items': [{'id': identity}]}, 'initial')
                data = store.save_order([identity], data['revision'])
                entered, release = threading.Event(), threading.Event()
                commands, results = [], {}

                class FakePP:
                    def __init__(self, host, port, timeout):
                        self.endpoint = (host, port)
                    def get_json(self, kind):
                        return [{'id': {'uuid': identity, 'name': 'Original'}}] if kind == 'props' else []
                    def get_command(self, path):
                        commands.append((self.endpoint, path))
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError('Test trigger release timed out')
                        return ''

                def save_config(value):
                    config_path.write_text(json.dumps(value))
                def reload_config(*args, **kwargs):
                    cfg.clear(); cfg.update(json.loads(config_path.read_text()))
                def update_endpoint(port):
                    client = web.app.test_client()
                    if write_path == 'save':
                        response = client.post('/api/config', json={'propresenter_port': port})
                    else:
                        payload, _ = web._create_config_transport_zip(['config'], reason='test')
                        modified = io.BytesIO()
                        with zipfile.ZipFile(io.BytesIO(payload)) as src, zipfile.ZipFile(modified, 'w') as dst:
                            for name in src.namelist():
                                value = src.read(name)
                                if name == 'payload/config.json':
                                    value = json.dumps({**cfg, 'propresenter_port': port})
                                dst.writestr(name, value)
                        modified.seek(0)
                        inspected = client.post('/api/config/import/inspect',
                            data={'file': (modified, 'config.zip')})
                        self.assertEqual(inspected.status_code, 200, inspected.data)
                        response = client.post('/api/config/import/apply',
                            json={'token': inspected.get_json()['token'], 'items': ['config']})
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertEqual(cfg['propresenter_port'], port)

                with patch.dict(os.environ, {'TDECK_PROPS_FILE': str(store.path)}), \
                     patch.object(props_routes, '_service', None), patch.object(props_routes, '_service_key', None), \
                     patch.object(props_routes, '_trigger_states', {}), \
                     patch.object(props_routes, 'ProPresentor', FakePP), \
                     patch.object(web, '_auth_enabled', return_value=False), \
                     patch.object(web.utils, 'CONFIG_FILE', str(config_path)), \
                     patch.object(web.utils, 'get_config', side_effect=lambda: dict(cfg)), \
                     patch.object(web.utils, 'save_config', side_effect=save_config), \
                     patch.object(web.utils, 'reload_config', side_effect=reload_config), \
                     patch.object(web, '_clear_imported_config_caches', side_effect=reload_config), \
                     patch.object(web, '_APP_ROOT', root), patch.object(web, 'log_event'), \
                     patch.object(web, '_audit'), patch.object(web, '_apply_logging_config'):
                    service = web._get_props_service()
                    self.assertTrue(service.catalog.refresh_now())
                    payload = {'revision': data['revision'], **({'id': identity} if trigger_kind == 'library' else {'position': 1})}
                    trigger_path = '/api/v1/props/library/trigger' if trigger_kind == 'library' else '/api/v1/props/trigger'
                    def trigger():
                        results['trigger'] = web.app.test_client().post(trigger_path, json=payload)
                    worker = threading.Thread(target=trigger)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        for port in (1401, 1400):
                            update_endpoint(port)
                            current = web._get_props_service()
                            self.assertTrue(current.catalog.refresh_now())
                            # B and then A must retain the trigger guard; retired settings stay absent.
                            settings = web.app.test_client().put('/api/props/settings', json={
                                'revision': data['revision'], 'previous_use_macros': False, 'use_macros': True})
                            self.assertEqual(settings.status_code, 404, settings.data)
                            second = web.app.test_client().post(trigger_path, json=payload)
                            self.assertEqual(second.status_code, 409, second.data)
                        read_done = threading.Event()
                        def read_and_edit():
                            results['read'] = web.app.test_client().get('/api/v1/props')
                            results['edit'] = web.app.test_client().put('/api/props/order',
                                json={'revision': data['revision'], 'order': []})
                            read_done.set()
                        reader = threading.Thread(target=read_and_edit)
                        reader.start()
                        try:
                            self.assertTrue(read_done.wait(.5), 'Local state/order waited for hardware')
                        finally:
                            if not read_done.is_set(): release.set()
                            reader.join(3)
                        self.assertFalse(reader.is_alive())
                        self.assertEqual(results['read'].status_code, 200)
                        self.assertEqual(results['edit'].status_code, 200)
                    finally:
                        release.set(); worker.join(3)
                    self.assertFalse(worker.is_alive(), 'Trigger/config lock deadlock')
                    self.assertEqual(results['trigger'].status_code, 200, results['trigger'].data)
                    history = results['trigger'].get_json()['last_triggered']
                    self.assertEqual(history['revision'], data['revision'])
                    self.assertEqual(history['mode'], 'prop')
                    self.assertEqual(history['position'], None if trigger_kind == 'library' else 1)
                    self.assertEqual(history['kind'], trigger_kind)
                    self.assertEqual(commands, [(('127.0.0.1', 1400), 'prop/' + prop + '/trigger')])
                    self.assertEqual(web.app.test_client().get('/api/v1/props').get_json()['last_triggered'], history)
                    # History survives another replacement after completion too.
                    update_endpoint(1401)
                    current = web._get_props_service()
                    self.assertEqual(current.state()['last_triggered'], history)
                    self.assertTrue(current.lock.acquire(blocking=False))
                    current.lock.release()
                    # Storage identity isolates both guard and history.
                    with patch.dict(os.environ, {'TDECK_PROPS_FILE': str(root / 'other-props.json')}):
                        other = web._get_props_service()
                        self.assertIsNone(other.state()['last_triggered'])
                        self.assertTrue(other.lock.acquire(blocking=False))
                        other.lock.release()
                    self.assertEqual(web._get_props_service().state()['last_triggered'], history)


if __name__ == '__main__':
    unittest.main()
