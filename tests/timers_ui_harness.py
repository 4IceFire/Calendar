"""Loopback synthetic Timers fixture; never loads a real instance config."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tdeck-timers-ui-', dir=os.environ.get('TMPDIR')) as tmp:
        os.chdir(tmp)
        with patch('socket.socket.connect', side_effect=AssertionError('Outbound blocked in Timers fixture')), \
             patch('requests.sessions.Session.request', side_effect=AssertionError('Outbound blocked in Timers fixture')):
            import webui as web
            from test_stream_delay_update import FakePP
            cfg = {'auth_enabled': False, 'propresenter_ip': 'fake.invalid', 'propresenter_port': 1025,
                   'propresenter_timer_index': 2, 'stream_start_preset': 1, 'stream_delay_preset': 0}
            original = [{'name': 'Start', 'time': '09:30'}, {'name': 'Delay', 'time': '23:59',
                        'button_presses': [{'buttonURL': 'location/1/0/1/press'}]}]
            presses = []
            class Companion:
                connected = True
                def check_connection(self): return True
                def post_command(self, url):
                    presses.append(url)
                    return True
            patches = [patch.object(web.utils, 'get_config', side_effect=lambda: copy.deepcopy(cfg)),
                       patch.object(web.utils, 'save_config', side_effect=lambda data: cfg.update(data)),
                       patch.object(web.utils, 'reload_config'),
                       patch.object(web.utils, 'get_companion', return_value=Companion()),
                       patch.object(web, 'ProPresentor', FakePP),
                       patch.object(web, '_queue_companion_timer_variable_sync'),
                       patch.object(web, '_integration_status_summary', return_value={}, create=True),
                       patch.object(web, '_home_set_last_timer_preset'),
                       patch.object(web, '_auth_enabled', return_value=False)]
            for p in patches: p.start()
            web.utils.save_timer_presets(original)
            allowed = {'static', 'timers_page', 'config_page', 'api_get_config', 'api_set_config',
                       'api_get_timers', 'api_set_timers', 'api_mutate_timers', 'api_update_timer_preset', 'api_apply_timer_preset',
                       'api_prop_stage_stream_delay', 'api_prop_stage_stream_start', 'api_templates'}
            def blocked(**kwargs): return web.jsonify(ok=False, error='Disabled in Timers fixture'), 404
            for name in list(web.app.view_functions):
                if name not in allowed:
                    web.app.view_functions[name] = blocked
            def gate():
                path = web.request.path
                if path == '/__timers_fixture__/health':
                    return web.jsonify(fixture='tdeck-timers-ui', isolated=True, calls=FakePP.calls, presses=presses,
                                       presets=web.utils.load_timer_presets())
                if path == '/__timers_fixture__/reset':
                    FakePP.calls = []; FakePP.fail = None; presses.clear()
                    cfg.update(stream_start_preset=1, stream_delay_preset=0)
                    web.utils.save_timer_presets(original)
                    return web.jsonify(ok=True)
                if path == '/__timers_fixture__/failure':
                    FakePP.fail = web.request.args.get('step')
                    return web.jsonify(ok=True)
                if path == '/__timers_fixture__/edit':
                    web.utils.update_timer_preset(2, time_str='10:00')
                    return web.jsonify(ok=True)
                if path == '/api/templates':
                    return web.jsonify(buttons=[])
            web.app.before_request_funcs[None].insert(0, gate)
            from werkzeug.serving import make_server
            server = make_server('127.0.0.1', 0, web.app, threaded=True)
            Path(args.ready_file).write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}'}))
            try:
                server.serve_forever()
            finally:
                server.server_close()
                for p in reversed(patches): p.stop()


if __name__ == '__main__':
    main()
