"""Isolated real-auth Routing/Record Audio fixture on 127.0.0.1:5068."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    with tempfile.TemporaryDirectory(prefix='tdeck-default-views-') as directory:
        os.chdir(directory)
        from test_admin_default_views import AdminDefaultViewsTests, webui
        fixture = AdminDefaultViewsTests()
        fixture.setUp()
        fixture.cfg['auth_idle_timeout_enabled'] = False
        fixture.execute("INSERT INTO group_pages(group_id,page_key) VALUES(2,'page:atem_audio')")
        fixture.execute("UPDATE groups SET atem_allowed_audio_sources='[\"1\"]', atem_can_monitor_audio=0 WHERE id=2")
        sources = [{'id': id_, 'label': label, 'volume': -12, 'muted': False,
                    'level': {'left': -20, 'right': -18}}
                   for id_, label in [('master', 'Master'), ('1', 'Stage'), ('2', 'Playback')]]
        snapshot = {'ok': True, 'configured': True, 'inputs': [
            {'number': 1, 'label': 'Stage'}, {'number': 2, 'label': 'Playback'}, {'number': 3, 'label': 'Camera'}],
            'outputs': [{'number': 1, 'label': 'Foyer'}, {'number': 2, 'label': 'Main'}], 'routing': [1, 2]}
        fixture.stack.enter_context(patch.object(webui, '_get_videohub_state_snapshot', return_value=snapshot))
        allowed = {'static', 'routing_page', 'foyer_audio_page', 'api_admin_default_view_update',
                   'api_videohub_state', 'api_videohub_labels', 'auth_ping', 'auth_touch'}
        def blocked(**_kwargs):
            return webui.jsonify(ok=False, error='Disabled in isolated fixture'), 404
        for name in list(webui.app.view_functions):
            if name not in allowed:
                webui.app.view_functions[name] = blocked
        webui.app.view_functions['api_atem_audio_state'] = lambda: webui.jsonify(ok=True, sources=sources,
            monitor={'volume': -18, 'enabled': True, 'dim': False}, metering={'active': True})
        webui.app.view_functions['api_atem_audio_meters'] = lambda: webui.jsonify(ok=True, sources={s['id']: s['level'] for s in sources})

        def control_gate():
            if webui.request.path == '/__default_views_fixture__/health':
                return webui.jsonify(fixture='tdeck-default-views-ui', isolated=True)
            if webui.request.path == '/__default_views_fixture__/login':
                uid = 2 if webui.request.args.get('role') == 'operator' else 1
                webui.session.clear()
                webui.session.update(_user_id=str(uid), _auth_session_id=f'login-{uid}', _fresh=True,
                                     _last_activity=int(time.time()), _csrf='fixture-csrf')
                return webui.jsonify(ok=True)
            if webui.request.path == '/__default_views_fixture__/reset':
                fixture.execute("DELETE FROM auth_meta WHERE key LIKE 'admin_default_view:%'")
                return webui.jsonify(ok=True)
        webui.app.before_request_funcs[None].insert(0, control_gate)
        try:
            webui.app.run(host='127.0.0.1', port=5068, debug=False, use_reloader=False, threaded=True)
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    main()
