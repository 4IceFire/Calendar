"""Reusable isolated TV fixture. No network hardware, files outside checkout."""
import copy
import io
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]


class FakeTV:
    is_connected = True
    client_id = 'fixture'
    is_muted = False

    def __init__(self, controller):
        self.controller = controller
        self.volume = 25
        self.source = 'HDMI1'
        self.fail = False
        self.ignore = False
        self.external = False
        self.commands = []

    def get_state(self, **kwargs):
        return {'statetype': 'livetv', 'sourceid': self.source}

    def get_volume(self, **kwargs):
        return None if self.external else self.volume

    def get_sources(self, **kwargs):
        return [{'id': 'HDMI1', 'name': 'HDMI 1'}, {'id': 'HDMI2', 'name': 'HDMI 2'}]

    def set_volume(self, value):
        self.commands.append(('volume', value))
        if self.fail:
            raise RuntimeError('password=fixture-private-secret')
        if not self.ignore:
            self.volume = value
        return True

    def volume_up(self):
        return self.set_volume(min(100, self.volume + 1))

    def volume_down(self):
        return self.set_volume(max(0, self.volume - 1))

    def _publish(self, topic, payload):
        self.commands.append(('source', payload))
        if not self.ignore:
            self.source = payload['sourceid']
        return not self.fail

    def power_off(self):
        self.commands.append(('power_off', None))
        return not self.fail

    def disconnect(self):
        pass

    def send_key(self, key):
        self.commands.append(('key', key))
        return True


class TVFixture:
    def __enter__(self):
        self.stack = ExitStack()
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='tdeck-tv-', dir=os.environ.get('TMPDIR')))
        self.root = Path(self.temp)
        old_cwd = Path.cwd()
        os.chdir(self.root)
        self.stack.callback(os.chdir, old_cwd)
        sys.path.insert(0, str(PROJECT))
        for operation in ('connect', 'connect_ex', 'sendto'):
            self.stack.enter_context(patch('socket.socket.' + operation, side_effect=RuntimeError('Fixture blocks outbound hardware')))
        connect_db = sqlite3.connect
        def isolated_import_database(database, *args, **kwargs):
            # webui's default auth path is based on __file__, not cwd. Redirect
            # it before cold import so even initialization cannot touch a
            # checkout's existing runtime auth DB.
            if str(database) == str(PROJECT / 'auth.db'):
                database = self.root / 'auth.db'
            return connect_db(database, *args, **kwargs)
        with patch('threading.Thread.start'), patch('sqlite3.connect', side_effect=isolated_import_database):
            import webui
        self.w = webui
        self.cfg = {'auth_enabled': True, 'auth_idle_timeout_enabled': False, 'dark_mode': True,
                    'api_write_rate_limit_per_minute': 600, 'api_legacy_anonymous_enabled': False,
                    'hisense_enabled': True,
                    'hisense_tvs': [{'id': 'foyer', 'name': 'Ground Foyer (simulated)', 'host': '192.0.2.1', 'mac': '00:11:22:33:44:55'},
                                    {'id': 'hall', 'name': 'Hall TV (simulated)', 'host': '192.0.2.2', 'mac': '00:11:22:33:44:56'},
                                    {'id': 'private', 'name': 'Private TV', 'host': '192.0.2.3', 'mac': '00:11:22:33:44:57'}],
                    'hisense_tv_groups': [{'id': 'ground', 'name': 'Ground floor', 'tv_ids': ['foyer', 'hall']},
                                          {'id': 'private-room', 'name': 'Private room', 'tv_ids': ['private']}],
                    'hisense_media_mappings': {'foyer': {'output': 1, 'source': 'HDMI1'}},
                    'atem_ip': '192.0.2.10', 'atem_media_enabled': True, 'atem_media_node_path': '',
                    'atem_media_destinations': [{'player': 2, 'label': 'Media A', 'slots': [41, 42], 'videohub_input': 7}]}
        for context in (
            patch.object(webui, '_AUTH_DB_PATH', self.root / 'auth.db'),
            patch.object(webui.utils, 'get_config', side_effect=lambda: copy.deepcopy(self.cfg)),
            patch.object(webui.utils, 'CONFIG_FILE', str(self.root / 'config.json')),
            patch.object(webui.utils, 'reload_config', return_value=True),
            patch.object(webui.utils, 'save_config', side_effect=lambda cfg: self.cfg.update(copy.deepcopy(cfg))),
            patch.object(webui, '_bootstrap_default_users_roles'),
            patch.object(webui, '_get_status_snapshot', return_value={}),
            patch.object(webui, '_get_atem_audio_sources_for_permissions', return_value=[]),
            patch.object(webui, '_digico_aux_options', return_value=[]),
            patch.object(webui, '_pixie_permission_catalog', return_value={'auditoriums': [], 'scenes': []}),
            patch.object(webui, '_load_companion_surfaces', return_value=[]),
        ):
            self.stack.enter_context(context)
        self.log = self.stack.enter_context(patch.object(webui, 'log_event'))
        webui._init_auth_db()
        conn = webui._db()
        try:
            conn.execute('DELETE FROM user_groups'); conn.execute('DELETE FROM group_pages'); conn.execute('DELETE FROM groups'); conn.execute('DELETE FROM users')
            conn.executemany('INSERT INTO groups(id,name,is_admin) VALUES(?,?,?)', [(1, 'Admin', 1), (2, 'TV Operators', 0), (3, 'Diagnostics', 0), (4, 'No TV scope', 0)])
            grants = {2: ['page:tv_controls', 'page:tv_diagnostics', 'tvroom:ground', 'tvdevice:ground:foyer'],
                      3: ['page:tv_diagnostics', 'tvroom:ground', 'tvdevice:ground:*'], 4: ['page:tv_controls']}
            for gid, keys in grants.items():
                conn.executemany('INSERT INTO group_pages(group_id,page_key) VALUES(?,?)', [(gid, key) for key in keys])
            conn.executemany('INSERT INTO users(id,username,password_hash) VALUES(?,?,?)', [(uid, name, webui.generate_password_hash('fixture-password')) for uid, name in [(1, 'admin-tv'), (2, 'operator-tv'), (3, 'reader-tv'), (4, 'empty-tv')]])
            conn.executemany('INSERT INTO user_groups(user_id,group_id) VALUES(?,?)', [(uid, uid) for uid in range(1, 5)])
            conn.commit()
        finally:
            conn.close()
        from hisense import HisenseConfig, HisenseManager
        self.manager = HisenseManager(HisenseConfig.from_mapping(self.cfg, base_dir=self.root), client_factory=Mock(), wake_function=lambda *_: True)
        self.stack.callback(self.manager.close)
        self.clients = {}
        for ident, controller in self.manager.controllers.items():
            client = FakeTV(controller); self.clients[ident] = client
            def connect(tv=controller, fake=client, **kwargs):
                tv._client = fake; tv._state['connected'] = True; return True
            controller._connect = connect
            connect(); controller._poll()
            controller.actor_provider = webui._capture_hisense_actor
            controller.outcome_callback = webui._hisense_worker_outcome
            controller.start()
        self.stack.enter_context(patch.object(webui, '_get_hisense_manager_from_config', return_value=self.manager))
        from media_library import MediaLibrary
        from media_routing import MediaRoutingManager
        from media_ui_harness import FixtureManager, FixtureVideoHub, fixture_image
        self.library = MediaLibrary(self.root / 'media')
        self.image = self.library.upload(io.BytesIO(fixture_image('TV fixture', '#113355')), 'fixture.png', 'Welcome fixture')
        self.atem = FixtureManager(self.library, lambda: self.cfg)
        self.stack.callback(self.atem.close)
        self.hub = FixtureVideoHub()
        self.routing = MediaRoutingManager(get_config=lambda: self.cfg, get_media_manager=lambda: self.atem,
                    read_videohub=lambda: self.hub.get_routing_state_strict(),
                    route_videohub=lambda output, input_: self.hub.route_video_output(output=output - 1, input_=input_ - 1))
        for context in (patch.object(webui, '_get_media_library', return_value=self.library),
                        patch.object(webui, '_get_media_routing_manager', return_value=self.routing),
                        patch.object(webui, '_active_media_job', side_effect=lambda: self.routing.active_job() or {}),
                        patch.object(webui, '_get_videohub_state_snapshot', side_effect=lambda: self.hub.snapshot())):
            self.stack.enter_context(context)
        with webui._api_rate_lock:
            webui._api_rate_events.clear()
        return self

    def client(self, username='operator-tv'):
        client = self.w.app.test_client()
        client.get('/login')
        with client.session_transaction() as session:
            csrf = session['_csrf']
        response = client.post('/login', data={'username': username, 'password': 'fixture-password', '_csrf': csrf})
        assert response.status_code == 302, response.status_code
        return client

    def headers(self, client, origin='http://localhost'):
        with client.session_transaction() as session:
            return {'Origin': origin, 'X-CSRF-Token': session['_csrf']}

    def __exit__(self, *args):
        self.stack.close()
