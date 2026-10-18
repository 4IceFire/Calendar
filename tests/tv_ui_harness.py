"""Run an owned, marked loopback TV fixture; never use staging/hardware."""
import argparse
import json
from pathlib import Path
from flask import jsonify, request
from werkzeug.serving import make_server
from tv_fixture import TVFixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    args = parser.parse_args()
    with TVFixture() as fx:
        app = fx.w.app
        allowed = {'static', 'login_page', 'logout_page', 'hisense_config_page', 'admin_permissions_page',
                   'api_status_summary', 'api_activity_log_alerts', 'api_client_errors'}
        def blocked_route(**kwargs):
            return jsonify({'ok': False, 'error': 'Not available in the isolated TV fixture'}), 403
        for endpoint in list(app.view_functions):
            if endpoint not in allowed and not endpoint.startswith(('tv_', 'api_hisense_', 'api_v1__api_hisense_', 'api_admin_', 'auth_')):
                app.view_functions[endpoint] = blocked_route
        # No generic setup restart/save even inside the temporary fixture.
        app.view_functions['api_hisense_config_put'] = blocked_route
        app.view_functions['api_v1__api_hisense_config_put'] = blocked_route
        # Fixture-only controls are not part of the application source routes.
        @app.get('/__tv_fixture')
        def fixture_marker():
            return jsonify({'fixture': 'tdeck-tv-isolated', 'root': str(fx.root),
                            'workers': {ident: controller._thread.is_alive() for ident, controller in fx.manager.controllers.items()},
                            'commands': {ident: fake.commands for ident, fake in fx.clients.items()},
                            'image': fx.image['id']})

        @app.post('/__tv_fixture/state')
        def fixture_state():
            data = request.get_json()
            fake = fx.clients['foyer']
            if 'fail' in data:
                fake.fail = data['fail']
            if 'ignore' in data:
                fake.ignore = data['ignore']
            if 'volume' in data:
                fake.volume = data['volume']
            if 'source' in data:
                fake.source = data['source']
            if 'external' in data:
                fake.external = data['external']
            if fake.external:
                fake.controller._on_state_change({'_tdeck_volume': True, 'volume_type': 1, 'volume_value': 53})
            else:
                fake.controller._poll()
            return jsonify({'ok': True})

        @app.post('/__tv_fixture/reset')
        def fixture_reset():
            from tv_observation import Observation
            if not all(controller._thread.is_alive() for controller in fx.manager.controllers.values()):
                return jsonify({'ok': False, 'error': 'Fixture worker stopped'}), 500
            for fake in fx.clients.values():
                fake.fail = False; fake.ignore = False; fake.external = False
                fake.volume = 25; fake.source = 'HDMI1'; fake.commands.clear()
                fake.controller.observation = Observation()
                fake.controller._set_expected_off(False)
                fake.controller._connect()
                fake.controller._poll()
            fx.hub.routing[:] = [1, 2, 1]
            fx.cfg['hisense_media_mappings'] = {'foyer': {'output': 1, 'source': 'HDMI1'}}
            return jsonify({'ok': True, 'fixture': 'tdeck-tv-isolated'})

        server = make_server('127.0.0.1', 0, app, threaded=True)
        def fixture_control_gate():
            if request.endpoint in ('fixture_marker', 'fixture_state', 'fixture_reset'):
                return app.view_functions[request.endpoint]()
        app.before_request_funcs[None].insert(0, fixture_control_gate)
        url = 'http://127.0.0.1:' + str(server.server_port)
        Path(args.ready_file).write_text(json.dumps({'url': url, 'fixture': 'tdeck-tv-isolated', 'root': str(fx.root)}))
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
