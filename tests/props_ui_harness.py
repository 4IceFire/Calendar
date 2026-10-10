"""Loopback-only Props browser fixture. Temporary auth/data; outbound blocked."""
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
    with tempfile.TemporaryDirectory(prefix='tdeck-props-ui-', dir=os.environ.get('TMPDIR')) as directory:
        os.chdir(directory)
        with patch('requests.sessions.Session.request', side_effect=RuntimeError('Outbound blocked in Props fixture')):
            from test_props import PropsSecurityTests
            fixture = PropsSecurityTests()
            fixture.setUp()
            catalog = lambda kind: [
                {'id': {'uuid': '4cddf2c5-7355-4347-980c-b13f12cbfa85', 'name': 'Original'}},
                {'id': {'uuid': 'edfad26c-6b88-4932-a996-497136f2cedf', 'name': 'Closing'}}
            ] if kind == 'props' else (_ for _ in ()).throw(AssertionError('Unexpected catalog API'))
            fixture.pp.get_json.side_effect = catalog
            web = fixture.webui
            fixture.cfg['auth_idle_timeout_enabled'] = False
            for p in (patch.object(web.utils, 'save_config', side_effect=lambda value: fixture.cfg.update(value)),
                      patch.object(web.utils, 'reload_config'),
                      patch.object(web, '_integration_status_summary', create=True, return_value={})):
                p.start(); fixture.addCleanup(p.stop)
            allowed = {'static', 'props_page', 'props_configure_page', 'admin_permissions', 'auth_ping', 'auth_touch'}
            def blocked(**kwargs):
                return web.jsonify(ok=False, error='Disabled in isolated fixture'), 404
            for name in list(web.app.view_functions):
                if name not in allowed and not name.startswith(('api_props', 'api_v1__api_props')):
                    web.app.view_functions[name] = blocked

            def gate():
                if web.request.path == '/__props_fixture__/health':
                    return web.jsonify(fixture='tdeck-props-ui', isolated=True, calls=fixture.pp.get_command.call_count)
                if web.request.path == '/__props_fixture__/login':
                    role = web.request.args.get('role')
                    uid = 2 if role == 'operator' else 3 if role == 'denied' else 1
                    if role == 'operator':
                        web._set_group_pages(3, [])
                    web.session.clear()
                    web.session.update(_user_id=str(uid), _auth_session_id='fixture-' + str(uid), _fresh=True,
                                       _last_activity=int(time.time()), _csrf='props-csrf')
                    return web.jsonify(ok=True)
                if web.request.path == '/__props_fixture__/reset':
                    fixture.store.path.unlink(missing_ok=True)
                    fixture.service._trigger_state.last_triggered = None
                    fixture.pp.reset_mock()
                    fixture.pp.get_json.side_effect = catalog
                    fixture.pp.get_command.return_value = ''
                    web._set_group_pages(3, ['page:props_configure'])
                    fixture.service.catalog.refresh_now()
                    return web.jsonify(ok=True)
                if web.request.path == '/__props_fixture__/catalog-missing':
                    fixture.pp.get_json.return_value = []
                    fixture.pp.get_json.side_effect = None
                    fixture.service.catalog.refresh_now()
                    return web.jsonify(ok=True)
                if web.request.path == '/__props_fixture__/failure':
                    fixture.pp.get_command.return_value = None
                    return web.jsonify(ok=True)
            web.app.before_request_funcs[None].insert(0, gate)
            from werkzeug.serving import make_server
            server = make_server('127.0.0.1', 0, web.app, threaded=True)
            ready.write_text(json.dumps({'url': 'http://127.0.0.1:' + str(server.server_port)}))
            print('Props fixture listening on ' + str(server.server_port), flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
                fixture.doCleanups()


if __name__ == '__main__':
    main()
