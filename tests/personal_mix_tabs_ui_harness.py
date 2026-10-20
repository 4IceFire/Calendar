"""Synthetic Personal Mixes fixture: private auth, no outbound hardware sockets."""
import argparse
import copy
import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


class FakeMixer:
    def __init__(self):
        self.reset()

    def reset(self, mode='groups'):
        self.writes = []
        self.reads = {'state': 0, 'meters': 0, 'config': 0}
        self.revision = 1
        self.auxes = [dict(channel=n, enabled=True, label=label, stereo=n == 1)
                      for n, label in [(1, 'Drums'), (2, 'Vocals'), (3, 'Restricted')]]
        headings = ['Vocals'] * 24 + ['Band', '', 'Keys', 'Tracks', 'Acoustic', 'Electric', 'Brass', 'Strings', 'FX', 'Vocals']
        if mode == 'ungrouped':
            headings = ['', '']
        elif mode == 'empty':
            headings = []
        elif mode == 'names':
            headings = ['Vocals', 'vocals', '<FX> / 🎸', 'Vocals']
        self.channels = [dict(channel=n, enabled=True, label=f'Input {n}', group=group,
                              level=-12.0, pan=.5, sendOn=True, meter=50)
                         for n, group in enumerate(headings, 1)]
        self.values = {}

    def mixer_config(self):
        self.reads['config'] += 1
        return dict(auxes=self.auxes, channels=self.channels, snapshot='Synthetic desk', revision=self.revision)

    def status(self):
        return dict(connected=True, running=True, ready=True, revision=self.revision)

    def route_enabled(self, aux, channel=None):
        return aux in (1, 2, 3) and (channel is None or any(c['channel'] == channel for c in self.channels))

    def request_aux_state(self, aux):
        self.reads['state'] += 1

    def aux_state(self, aux):
        channels = copy.deepcopy(self.channels)
        for channel in channels:
            channel.update(self.values.get((aux, channel['channel']), {}))
        return dict(aux=self.auxes[aux - 1], channels=channels, revision=self.revision)

    def input_meter_state(self):
        self.reads['meters'] += 1
        return dict(channels=self.channels, revision=self.revision)

    def write(self, aux, channel, field, value):
        self.writes.append(dict(aux=aux, channel=channel, field=field, value=value))
        self.values.setdefault((aux, channel), {})[field] = value
        self.revision += 1
        return value

    def set_level(self, aux, channel, value):
        return self.write(aux, channel, 'level', value)

    def set_pan(self, aux, channel, value):
        return self.write(aux, channel, 'pan', value)

    def set_send_on(self, aux, channel, value):
        return self.write(aux, channel, 'sendOn', value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ready-file', required=True)
    args = parser.parse_args()
    ready = Path(args.ready_file).resolve()
    checkout_db = PROJECT / 'auth.db'
    before = checkout_db.stat() if checkout_db.exists() else None
    with tempfile.TemporaryDirectory(prefix='tdeck-mix-tabs-', dir=os.environ.get('TMPDIR')) as directory:
        os.chdir(directory)
        original_connect = sqlite3.connect

        def isolated_db(database, *args, **kwargs):
            if str(database) == str(checkout_db):
                database = str(Path(directory) / 'cold-auth.db')
            return original_connect(database, *args, **kwargs)

        blocked = RuntimeError('Outbound hardware blocked in Personal Mixes fixture')
        with (patch('requests.sessions.Session.request', side_effect=blocked),
              patch('socket.socket.connect', side_effect=blocked),
              patch('socket.socket.sendto', side_effect=blocked)):
            with patch('sqlite3.connect', isolated_db), patch.object(threading.Thread, 'start'):
                from test_videohub_ignore import VideoHubIgnoreApiTests
                fixture = VideoHubIgnoreApiTests()
                fixture.setUp()
            assert (checkout_db.stat() if checkout_db.exists() else None) == before, 'Checkout auth DB changed'
            web = fixture.webui
            mixer = FakeMixer()
            fixture.cfg.update(digico_enabled=True)
            fixture.stack.enter_context(patch.object(web, '_get_digico_client_from_config', return_value=mixer))
            fixture.stack.enter_context(patch.object(web, '_get_status_snapshot', return_value={}))
            web._set_group_pages(2, ['page:digico_mixer'])
            conn = web._db()
            conn.execute('UPDATE groups SET digico_allowed_auxes=? WHERE id=2', ('["1","2"]',))
            conn.commit()
            conn.close()
            allowed = {'static', 'personal_mixes_page', 'auth_ping', 'auth_touch', 'api_status_summary', 'api_activity_log_alerts'}
            for name in list(web.app.view_functions):
                if name not in allowed and not name.startswith(('api_digico_', 'api_v1__api_digico_')):
                    web.app.view_functions[name] = lambda **kw: (web.jsonify(ok=False, error='Disabled in fixture'), 404)

            def gate():
                prefix = '/__personal_mix_tabs_fixture__/'
                path = web.request.path
                if path == prefix + 'health':
                    return web.jsonify(fixture='tdeck-personal-mix-tabs', isolated=True,
                                       writes=mixer.writes, reads=mixer.reads, checkout_db_unchanged=(checkout_db.stat() if checkout_db.exists() else None) == before)
                if path == prefix + 'reset' and web.request.method == 'POST':
                    mixer.reset(web.request.args.get('mode', 'groups'))
                    return web.jsonify(ok=True)
                if path == prefix + 'login':
                    # Use this browser's session, not the test client's cookie jar.
                    import time
                    uid = 3 if web.request.args.get('role') == 'denied' else 2
                    web.session.clear()
                    web.session.update(_user_id=str(uid), _fresh=True, _auth_session_id=f'fixture-{uid}',
                                       _last_activity=int(time.time()), _csrf='mix-tabs-csrf')
                    return web.jsonify(ok=True)
                if path == prefix + 'feedback' and web.request.method == 'POST':
                    mixer.values[(1, 25)] = dict(level=-6, pan=.75, sendOn=False)
                    mixer.channels[24]['meter'] = 84
                    mixer.revision += 1
                    return web.jsonify(ok=True)

            web.app.before_request_funcs[None].insert(0, gate)
            from werkzeug.serving import make_server
            server = make_server('127.0.0.1', 0, web.app, threaded=True)
            ready.write_text(json.dumps(dict(url=f'http://127.0.0.1:{server.server_port}', fixture='tdeck-personal-mix-tabs', storage=directory)))
            print(f'Personal Mix tabs fixture listening on {server.server_port}', flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
                fixture.doCleanups()


if __name__ == '__main__':
    main()
