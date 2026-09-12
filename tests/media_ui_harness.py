"""Loopback-only Media UI fixture; never starts hardware clients or the scheduler.

Run from Calendar: python tests/media_ui_harness.py
Then set TDECK_BASE_URL=http://127.0.0.1:5063 and run the media.spec.js tests.
Every mutable file lives in a temporary directory. Outbound sockets are blocked,
import-time background threads are suppressed, and unrelated routes are disabled.
"""
from __future__ import annotations

import copy
import io
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
import time
import types
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw


PROJECT = Path(__file__).resolve().parents[1]


def fixture_image(title: str, color: str) -> bytes:
    image = Image.new('RGB', (1280, 720), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((70, 70, 1210, 650), outline='#ffffff', width=3)
    draw.text((110, 280), title, fill='#ffffff', font_size=70)
    draw.text((115, 380), 'TDeck media library', fill='#ffffff', font_size=28)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


class FixtureManager:
    def __init__(self, library, get_config):
        self.library = library
        self.get_config = get_config
        self.job = None
        self.started = 0
        self.callback = None
        self.lock = threading.Lock()

    def snapshot(self):
        callback = None
        completed = None
        with self.lock:
            if self.job and self.job['status'] not in ('succeeded', 'failed'):
                elapsed = time.monotonic() - self.started
                self.job['status'] = 'uploading' if elapsed < .8 else 'succeeded'
                if self.job['status'] == 'succeeded':
                    completed = copy.deepcopy(self.job)
                    callback, self.callback = self.callback, None
            config = self.get_config()
            state = {
                'enabled': config['atem_media_enabled'], 'connected': config['atem_media_enabled'],
                'product': 'ATEM 4 M/E Broadcast Studio 4K (fixture)',
                'videoMode': {'name': '1080p60', 'width': 1920, 'height': 1080},
                'capabilities': {'players': 4, 'stills': 64},
                'destinations': config['atem_media_destinations'],
                'players': [{'player': item['player'], 'type': 'still', 'slot': item['slots'][0]}
                            for item in config['atem_media_destinations']],
                'stills': [], 'job': copy.deepcopy(self.job), 'error': None,
            }
        if callback:
            callback(completed)
        return state

    def load(self, media_id, player, on_complete=None):
        from atem_media import BusyError
        with self.lock:
            if self.job and self.job['status'] not in ('succeeded', 'failed'):
                raise BusyError('Another image is loading in this fixture session.')
            destination = next((item for item in self.get_config()['atem_media_destinations']
                                if item['player'] == player), None)
            if destination is None:
                raise ValueError('Choose a configured fixture player.')
            item = self.library.get(media_id)
            self.job = {
                'id': uuid.uuid4().hex, 'mediaId': media_id, 'mediaName': item['name'],
                'player': player, 'slot': destination['slots'][0], 'status': 'queued',
                'error': None, 'createdAt': time.time(), 'updatedAt': time.time(),
            }
            self.started = time.monotonic()
            self.callback = on_complete
            return copy.deepcopy(self.job)


def main():
    sys.path.insert(0, str(PROJECT))
    with tempfile.TemporaryDirectory(prefix='tdeck-media-ui-') as temporary, ExitStack() as stack:
        root = Path(temporary)
        original_cwd = Path.cwd()
        os.chdir(root)
        stack.callback(os.chdir, original_cwd)
        config_path = root / 'config.json'
        config = {
            'auth_enabled': False, 'flask_secret_key': secrets.token_hex(32),
            'webserver_port': 5063, 'dark_mode': False, 'atem_ip': '192.0.2.1',
            'atem_media_enabled': True, 'atem_media_node_path': '',
            'atem_media_destinations': [
                {'player': 2, 'label': 'Foyer', 'slots': [41, 42]},
                {'player': 4, 'label': 'Kids', 'slots': [43, 44]},
            ],
        }
        config_path.write_text(json.dumps(config), encoding='utf-8')
        # Listening and replying to browsers still work. This process cannot
        # establish an outbound connection or send a UDP hardware datagram.
        for operation in ('connect', 'connect_ex', 'sendto'):
            stack.enter_context(patch('socket.socket.' + operation,
                                      side_effect=RuntimeError('Outbound networking is disabled in the Media UI fixture')))
        companion_stub = types.ModuleType('companion')
        companion_stub.Companion = Mock(return_value=None)
        stack.enter_context(patch.dict(sys.modules, {'companion': companion_stub}))
        fixture_log_handler = logging.NullHandler()
        fixture_logger = logging.getLogger('calendar')
        fixture_logger.addHandler(fixture_log_handler)
        stack.callback(fixture_logger.removeHandler, fixture_log_handler)
        with patch('threading.Thread.start'):
            from package.apps.calendar import utils
            stack.enter_context(patch.object(utils, 'get_config', side_effect=lambda: copy.deepcopy(config)))
            stack.enter_context(patch.object(utils, 'CONFIG_FILE', str(config_path)))

            def reload_config(force=False):
                config.clear()
                config.update(json.loads(config_path.read_text(encoding='utf-8')))
                return True

            stack.enter_context(patch.object(utils, 'reload_config', side_effect=reload_config))
            import webui

        from media_library import MediaLibrary
        library = MediaLibrary(root / 'media_library')
        welcome_png = fixture_image('WELCOME', '#204a69')
        library.upload(io.BytesIO(welcome_png), 'welcome.png', 'Welcome')
        preset = library.upload(io.BytesIO(fixture_image("MOTHER’S DAY", '#8e516c')), 'mothers-day.png', "Mother's Day")
        library.update(preset['id'], preset['name'], True)
        stack.enter_context(patch.object(webui, '_AUTH_DB_PATH', root / 'auth.db'))
        stack.enter_context(patch.object(webui, 'log_event', return_value=None))
        stack.enter_context(patch.object(webui, '_get_media_library', return_value=library))
        webui._init_auth_db()
        managers = {}
        manager_lock = threading.Lock()

        def get_manager():
            identity = webui.session.get('_media_fixture_client')
            if not identity:
                identity = uuid.uuid4().hex
                webui.session['_media_fixture_client'] = identity
            with manager_lock:
                return managers.setdefault(identity, FixtureManager(library, utils.get_config))

        stack.enter_context(patch.object(webui, '_get_atem_media_manager', side_effect=get_manager))

        def blocked_route(**_kwargs):
            return webui.jsonify({'ok': False, 'error': 'This route is disabled in the isolated Media UI fixture.'}), 404

        allowed = {
            'static', 'media_page', 'media_image', 'media_thumbnail', 'api_media_list', 'api_media_upload',
            'api_media_edit', 'api_atem_media_state', 'api_atem_media_load', 'api_atem_media_config',
        }
        # Endpoint names for image/setup routes can change without opening other
        # integrations: retain only the explicitly scoped Media URL rules.
        for rule in webui.app.url_map.iter_rules():
            if rule.rule.startswith('/media/images/') or rule.rule == '/config/atem-media':
                allowed.add(rule.endpoint)
        for endpoint in list(webui.app.view_functions):
            if endpoint not in allowed:
                webui.app.view_functions[endpoint] = blocked_route

        def status_summary():
            return webui.jsonify({'ok': True, **{key: {'connected': False}
                                                for key in ('companion', 'propresenter', 'videohub', 'digico', 'atem', 'pixie')}})

        webui.app.view_functions['api_status_summary'] = status_summary
        webui.app.view_functions['api_activity_log_alerts'] = lambda: webui.jsonify({'ok': True, 'count': 0, 'failures': 0, 'warnings': 0})
        webui.app.view_functions['api_client_errors'] = lambda: webui.jsonify({'ok': True})

        @webui.app.get('/__media_fixture__/health')
        def fixture_health():
            return webui.jsonify({'fixture': 'tdeck-media-ui', 'isolated': True})

        @webui.app.get('/__media_fixture__/upload.png')
        def fixture_upload():
            return webui.Response(welcome_png, mimetype='image/png')

        @webui.app.get('/__media_fixture__/readonly')
        def fixture_readonly():
            return webui.render_template('media.html', media_permissions={'upload': False, 'manage': False, 'load': False}, can_configure_media=False)

        print('Isolated Media UI fixture: http://127.0.0.1:5063/media', flush=True)
        webui.app.run(host='127.0.0.1', port=5063, debug=False, use_reloader=False, threaded=True)


if __name__ == '__main__':
    main()
