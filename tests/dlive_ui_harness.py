"""Loopback dLive TCP desk + real TDeck auth/API + unchanged mixer layout."""
import argparse
import copy
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    args = parser.parse_args()
    original_connect = socket.socket.connect
    original_db = sqlite3.connect
    checkout = ROOT / 'auth.db'
    before = checkout.stat() if checkout.exists() else None
    with tempfile.TemporaryDirectory(prefix='tdeck-dlive-browser-', dir=os.environ.get('TMPDIR')) as directory:
        os.chdir(directory)
        def connect(sock, address):
            if not isinstance(address, tuple) or not ipaddress.ip_address(address[0]).is_loopback:
                raise RuntimeError('Non-loopback hardware blocked in dLive fixture')
            return original_connect(sock, address)
        def db(database, *args, **kwargs):
            if str(database) == str(checkout):
                database = str(Path(directory) / 'cold-auth.db')
            return original_db(database, *args, **kwargs)
        with (patch('socket.socket.connect', connect),
              patch('socket.socket.sendto', side_effect=RuntimeError('UDP hardware blocked')),
              patch('requests.sessions.Session.request', side_effect=RuntimeError('HTTP hardware blocked'))):
            with patch('sqlite3.connect', db), patch.object(threading.Thread, 'start'):
                import test_videohub_ignore
                fixture = test_videohub_ignore.VideoHubIgnoreApiTests()
                fixture.setUp()
            from test_dlive import Desk, config
            import personal_mixes
            desk = Desk()
            web = fixture.webui
            fixture.cfg.update(config(desk.port))
            # Use production freshness, not the unit test's accelerated 2s clock.
            fixture.cfg['dlive_stale_after'] = 10
            fixture.cfg['digico_auxes'] = [dict(label='Old SD9 IEM', enabled=True)]
            fixture.cfg['digico_enabled'] = False
            fixture.cfg['dlive_auxes'].append(dict(channel=1001, bus=1, stereo=False, label='Mono IEM'))
            fixture.cfg['dlive_channels'] = [dict(channel=n, label=f'Input {n}', group=group) for n, group in
                                             [(1, 'Vocals'), (2, ''), (3, 'Band'), (4, '')]]
            initial = copy.deepcopy(fixture.cfg)
            web._set_group_pages(2, ['page:digico_mixer'])
            web._set_group_digico_allowed_auxes(2, ['2002'])
            fixture.stack.enter_context(patch.object(web, '_get_status_snapshot', return_value={}))
            fixture.stack.enter_context(patch.object(web, '_get_atem_audio_sources_for_permissions', return_value=[]))
            fixture.stack.enter_context(patch.object(web.utils, 'save_config', side_effect=lambda cfg: fixture.cfg.update(copy.deepcopy(cfg))))
            fixture.stack.enter_context(patch.object(web.utils, 'reload_config'))
            # Exercise the real indexed DiGiCo normalizer without opening UDP.
            # Synthetic discovery is injected into its ordinary cache only.
            from digico import DigicoConfig, DigicoMixerClient
            def mixer_factory(cfg):
                if cfg.get('personal_mix_provider') != 'digico':
                    return personal_mixes.get_client(cfg)
                mixer = DigicoMixerClient(DigicoConfig.from_mapping(dict(cfg, digico_enabled=False)))
                mixer._cache['/Console/Input_Channels'] = {'args': [len(cfg.get('digico_channels') or [])], 'ts': time.time()}
                mixer._cache['/Console/Aux_Outputs/modes'] = {'args': [1] * len(cfg.get('digico_auxes') or []), 'ts': time.time()}
                return mixer
            fixture.stack.enter_context(patch.object(web, '_digico_client_factory', side_effect=mixer_factory))
            allowed = {'static', 'personal_mixes_page', 'digico_config_page', 'auth_ping', 'auth_touch', 'api_status_summary', 'api_activity_log_alerts', 'admin_permissions_page', 'api_admin_group_update', 'api_admin_group_organization'}
            for name in list(web.app.view_functions):
                if name not in allowed and not name.startswith(('api_digico_', 'api_v1__api_digico_')):
                    web.app.view_functions[name] = lambda **kw: (web.jsonify(ok=False), 404)
            def gate():
                prefix = '/__dlive_fixture__/'
                path = web.request.path
                if path == prefix + 'health':
                    conn = web._db()
                    try:
                        grants = json.loads(conn.execute('SELECT digico_allowed_auxes FROM groups WHERE id=2').fetchone()[0])
                    finally:
                        conn.close()
                    return web.jsonify(fixture='tdeck-dlive', isolated=True, messages=[m.hex() for m in desk.messages],
                                       config=fixture.cfg, grants=grants, checkout_db_unchanged=(checkout.stat() if checkout.exists() else None) == before)
                if path == prefix + 'reset' and web.request.method == 'POST':
                    personal_mixes.close_client()
                    fixture.cfg.clear()
                    fixture.cfg.update(copy.deepcopy(initial))
                    web._set_group_pages(2, ['page:digico_mixer'])
                    web._set_group_digico_allowed_auxes(2, ['2002'])
                    desk.messages.clear()
                    desk.level = 107
                    return web.jsonify(ok=True)
                if path == prefix + 'grants' and web.request.method == 'POST':
                    web._set_group_digico_allowed_auxes(2, web.request.get_json())
                    return web.jsonify(ok=True)
                if path == prefix + 'login':
                    uid = 1 if web.request.args.get('admin') == '1' else 2
                    web.session.clear()
                    web.session.update(_user_id=str(uid), _fresh=True, _auth_session_id=f'fixture-{uid}',
                                       _last_activity=int(time.time()), _csrf='dlive-csrf')
                    return web.jsonify(ok=True)
                if path == prefix + 'feedback' and web.request.method == 'POST':
                    desk.level = 87
                    return web.jsonify(ok=True)
            web.app.before_request_funcs[None].insert(0, gate)
            from werkzeug.serving import make_server
            server = make_server('127.0.0.1', 0, web.app, threaded=True)
            Path(args.ready_file).write_text(json.dumps(dict(url=f'http://127.0.0.1:{server.server_port}', fixture='tdeck-dlive')))
            assert (checkout.stat() if checkout.exists() else None) == before
            try:
                server.serve_forever()
            finally:
                server.server_close()
                personal_mixes.close_client()
                desk.close()
                fixture.doCleanups()


if __name__ == '__main__':
    main()
