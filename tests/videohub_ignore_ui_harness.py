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
                                       routing=fixture.hub.routing)
                if path == '/__videohub_ignore_fixture__/reset':
                    Path(fixture.cfg['videohub_presets_file']).unlink(missing_ok=True)
                    fixture.hub.writes.clear()
                    fixture.hub.reads = 0
                    fixture.hub.routing = list(range(1, 41))
                    fixture.hub.fail_readback = False
                    fixture.log.reset_mock()
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
