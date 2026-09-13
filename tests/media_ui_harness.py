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
        self.slots = {}
        self.timer = None

    def snapshot(self):
        with self.lock:
            config = self.get_config()
            state = {
                'enabled': config['atem_media_enabled'], 'connected': config['atem_media_enabled'],
                'ready': config['atem_media_enabled'], 'generation': 1,
                'product': 'Simulated ATEM',
                'videoMode': {'name': '1080p60', 'width': 1920, 'height': 1080},
                'capabilities': {'players': 4, 'stills': 64},
                'destinations': config['atem_media_destinations'],
                'players': [{'player': item['player'], 'type': 'still',
                             'slot': self.slots.get(item['player'], item['slots'][0]),
                             'stillSlot': self.slots.get(item['player'], item['slots'][0]),
                             'fillSource': 3000 + item['player'] * 10}
                            for item in config['atem_media_destinations']],
                'stills': [], 'job': copy.deepcopy(self.job), 'error': None,
            }
        return state

    def load(self, media_id, player, *, on_complete=None):
        from atem_media import BusyError
        with self.lock:
            if self.job and self.job['status'] not in ('succeeded', 'failed'):
                raise BusyError('Another image is loading in this fixture session.')
            destination = next((item for item in self.get_config()['atem_media_destinations']
                                if item['player'] == player), None)
            if destination is None:
                raise ValueError('Choose a configured fixture player.')
            item = self.library.get(media_id)
            current_slot = self.slots.get(player, destination['slots'][0])
            slot = next(slot for slot in destination['slots'] if slot != current_slot)
            self.job = {
                'id': uuid.uuid4().hex, 'mediaId': media_id, 'mediaName': item['name'],
                'player': player, 'slot': slot, 'status': 'uploading', 'generation': 1,
                'error': None, 'createdAt': time.time(), 'updatedAt': time.time(),
            }
            self.started = time.monotonic()
            self.callback = on_complete
            self.timer = threading.Timer(.8, self.complete)
            self.timer.daemon = True
            self.timer.start()
            return copy.deepcopy(self.job)

    def complete(self):
        with self.lock:
            self.job['status'] = 'succeeded'
            self.slots[self.job['player']] = self.job['slot']
            completed = copy.deepcopy(self.job)
            callback, self.callback = self.callback, None
        if callback:
            callback(completed)

    def close(self):
        if self.timer:
            self.timer.cancel()


class FixtureVideoHub:
    def __init__(self):
        self.routing = [1, 2, 1]

    def get_routing_state_strict(self):
        return {'input_count': 8, 'output_count': 3, 'routing': list(self.routing)}

    def snapshot(self, **_kwargs):
        return {'ok': True, 'configured': True, 'stale': False, 'refreshing': False,
                'inputs': [{'number': i, 'label': name} for i, name in enumerate(
                    ['ProPresenter', 'Camera', 'Stage', 'Computer', 'Spare 1', 'Spare 2', 'Media A', 'Media B'], 1)],
                'outputs': [{'number': i, 'label': name} for i, name in enumerate(['Foyer', 'Kids', 'Hall'], 1)],
                'routing': list(self.routing)}

    def route_video_output(self, *, output, input_, monitoring=False):
        self.routing[output] = input_ + 1

    def verify_video_output_route(self, *, output, input_):
        return self.routing[output] == input_ + 1


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
                {'player': 2, 'label': 'Media A', 'slots': [41, 42], 'videohub_input': 7},
                {'player': 4, 'label': 'Media B', 'slots': [43, 44], 'videohub_input': 8},
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
        videohubs = {}
        routing_managers = {}
        manager_lock = threading.Lock()

        def client_identity():
            identity = webui.session.get('_media_fixture_client')
            if not identity:
                identity = uuid.uuid4().hex
                webui.session['_media_fixture_client'] = identity
            return identity

        def get_manager():
            identity = client_identity()
            with manager_lock:
                return managers.setdefault(identity, FixtureManager(library, utils.get_config))

        stack.enter_context(patch.object(webui, '_get_atem_media_manager', side_effect=get_manager))

        def get_videohub():
            identity = client_identity()
            with manager_lock:
                return videohubs.setdefault(identity, FixtureVideoHub())

        def get_routing_manager(**_kwargs):
            from media_routing import MediaRoutingManager
            identity = client_identity()
            manager, videohub = get_manager(), get_videohub()
            with manager_lock:
                if identity not in routing_managers:
                    routing_managers[identity] = MediaRoutingManager(
                        get_config=utils.get_config, get_media_manager=lambda: manager,
                        read_videohub=videohub.get_routing_state_strict,
                        route_videohub=lambda output, input_: videohub.route_video_output(output=output - 1, input_=input_ - 1))
                return routing_managers[identity]

        stack.enter_context(patch.object(webui, '_get_media_routing_manager', side_effect=get_routing_manager))
        stack.enter_context(patch.object(webui, '_active_media_job', side_effect=lambda:
            get_routing_manager().active_job() or get_manager().snapshot().get('job') or {}))
        stack.enter_context(patch.object(webui, '_get_videohub_state_snapshot', side_effect=lambda **kwargs: get_videohub().snapshot()))
        stack.enter_context(patch.object(webui, '_get_videohub_client_from_config', side_effect=get_videohub))
        stack.enter_context(patch.object(webui, '_invalidate_videohub_state_snapshot'))

        def blocked_route(**_kwargs):
            return webui.jsonify({'ok': False, 'error': 'This route is disabled in the isolated Media UI fixture.'}), 404

        allowed = {
            'static', 'media_page', 'media_image', 'media_thumbnail', 'api_media_list', 'api_media_upload',
            'api_media_edit', 'api_atem_media_state', 'api_atem_media_load', 'api_atem_media_config',
            'routing_page', 'media_upload_page', 'api_media_display', 'api_media_display_job',
            'api_videohub_state', 'api_videohub_labels', 'api_videohub_route',
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
            return webui.render_template('media.html', media_permissions={'upload': False, 'manage': False, 'load': False},
                                        output=1, output_label='Foyer', can_display=False, hide_connection_status=True)

        print('Isolated Media demo: http://127.0.0.1:5063/routing', flush=True)
        webui.app.run(host='127.0.0.1', port=5063, debug=False, use_reloader=False, threaded=True)


if __name__ == '__main__':
    main()
