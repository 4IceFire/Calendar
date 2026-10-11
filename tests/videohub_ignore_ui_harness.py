"""Loopback-only VideoHub Ignore fixture, temporary auth and blocked hardware."""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    args = parser.parse_args()
    ready = Path(args.ready_file).resolve()
    with tempfile.TemporaryDirectory(prefix='tdeck-vh-ignore-ui-', dir=os.environ.get('TMPDIR')) as root:
        os.chdir(root)
        with (patch('requests.sessions.Session.request', side_effect=RuntimeError('Outbound blocked in fixture')),
              patch('socket.create_connection', side_effect=RuntimeError('Hardware blocked in fixture'))):
            from test_videohub_ignore import VideoHubIgnoreApiTests
            fixture = VideoHubIgnoreApiTests()
            fixture.setUp()
            web = fixture.webui
            from test_media_routing import FakeMedia, configuration
            from media_routing import MediaRoutingManager
            from media_player_usage import PlayerUsage
            from unittest.mock import Mock
            fixture.cfg.update(configuration())
            media = FakeMedia()
            identity = '12345678-1234-4234-8234-123456789abc'
            item = {'id': identity, 'name': 'Welcome image', 'enabled': True,
                    'output': 9, 'media_id': 'fixture-image', 'actions': [{'path': '/api/timers/1/start'}]}
            store = Mock(); store.get.side_effect = lambda key: dict(item) if key == identity else None
            store.list.side_effect = lambda: [dict(item)]
            library = Mock(); library.get.return_value = {'temporary': False}
            manager = MediaRoutingManager(get_config=lambda: fixture.cfg, get_media_manager=lambda: media,
                read_videohub=lambda: {'input_count': 40, 'output_count': 40, 'routing': fixture.hub.routing[:]},
                route_videohub=lambda o, i: fixture.hub.route_video_outputs(routes=[(o - 1, i - 1)]))
            fixture.stack.enter_context(patch.object(web, '_get_media_library', return_value=library))
            fixture.stack.enter_context(patch.object(web, '_get_routing_preset_store', return_value=store))
            fixture.stack.enter_context(patch.object(web, '_get_media_routing_manager', return_value=manager))
            actions = fixture.stack.enter_context(patch.object(web, '_execute_routing_preset_action'))
            allowed = {'static', 'videohub_page', 'videohub_input_select_page', 'auth_ping', 'auth_touch'}
            def blocked(**kwargs):
                return web.jsonify(ok=False, error='Disabled in isolated fixture'), 404
            for name in list(web.app.view_functions):
                if name not in allowed and not name.startswith(('api_videohub', 'api_v1__api_videohub')):
                    web.app.view_functions[name] = blocked

            def gate():
                path = web.request.path
                if path == '/__videohub_ignore_fixture__/health':
                    return web.jsonify(fixture='tdeck-videohub-ignore-ui', isolated=True,
                                       writes=fixture.hub.writes, reads=fixture.hub.reads,
                                       routing=fixture.hub.routing, images=media.calls,
                                       actions=actions.call_count, routing_preset=identity)
                if path == '/__videohub_ignore_fixture__/reset':
                    Path(fixture.cfg['videohub_presets_file']).unlink(missing_ok=True)
                    fixture.hub.writes.clear()
                    fixture.hub.reads = 0
                    fixture.hub.routing = list(range(1, 41))
                    fixture.hub.fail_readback = False
                    fixture.log.reset_mock()
                    media.calls.clear()
                    manager._player_usage = PlayerUsage()
                    actions.reset_mock()
                    item['enabled'] = True
                    web._videohub_image_jobs.clear()
                    return web.jsonify(ok=True)
                if path == '/__videohub_ignore_fixture__/login':
                    uid = 2 if web.request.args.get('role') == 'operator' else 3 if web.request.args.get('role') == 'denied' else 1
                    web.session.clear()
                    web.session.update(_user_id=str(uid), _fresh=True, _auth_session_id=f'fixture-{uid}',
                                       _last_activity=int(time.time()), _csrf='ignore-csrf')
                    return web.jsonify(ok=True)
                if path == '/__videohub_ignore_fixture__/change-live':
                    fixture.hub.routing[0] = 12
                    return web.jsonify(ok=True)
                if path == '/__videohub_ignore_fixture__/image-ready':
                    fixture.hub.routing[4:6] = [1, 1]
                    return web.jsonify(ok=True)
                if path == '/__videohub_ignore_fixture__/failure':
                    fixture.hub.fail_readback = True
                    fixture.hub.routing[1] = 2
                    return web.jsonify(ok=True)
            web.app.before_request_funcs[None].insert(0, gate)
            from werkzeug.serving import make_server
            server = make_server('127.0.0.1', 0, web.app, threaded=True)
            ready.write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}'}))
            print(f'VideoHub Ignore fixture listening on {server.server_port}', flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
                fixture.doCleanups()


if __name__ == '__main__':
    main()
