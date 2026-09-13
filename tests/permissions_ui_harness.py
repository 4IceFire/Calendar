"""Isolated group-permissions browser fixture, with temporary auth data only.

Run ``python tests/permissions_ui_harness.py`` and use
TDECK_BASE_URL=http://127.0.0.1:5064 for group_permissions.spec.js (workers=1).
Use ``--view-as`` for the real-auth fixture on port 5065 and view_as.spec.js.
Outbound sockets, hardware clients, scheduler threads and unrelated routes are
blocked. The fixture exercises the real group editor and group update API.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import logging
import os
import secrets
import sys
import tempfile
import types
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--view-as', action='store_true')
    parser.add_argument('--presets', action='store_true')
    args = parser.parse_args()
    presets_mode = args.presets
    view_as_mode = args.view_as or presets_mode
    port = 5066 if presets_mode else (5065 if view_as_mode else 5064)
    sys.path.insert(0, str(PROJECT))
    with tempfile.TemporaryDirectory(prefix='tdeck-permissions-ui-') as temporary, ExitStack() as stack:
        root = Path(temporary)
        previous_directory = Path.cwd()
        os.chdir(root)
        stack.callback(os.chdir, previous_directory)
        config = {'auth_enabled': view_as_mode, 'flask_secret_key': secrets.token_hex(32),
                  'webserver_port': port, 'dark_mode': True}
        if view_as_mode:
            config.update(auth_idle_timeout_enabled=False, atem_ip='192.0.2.1',
                          atem_media_enabled=True, atem_media_node_path='',
                          atem_media_destinations=[{'player': 2, 'label': 'Media A', 'slots': [41, 42],
                                                    'videohub_input': 7}])
        config_path = root / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        for operation in ('connect', 'connect_ex', 'sendto'):
            stack.enter_context(patch('socket.socket.' + operation,
                                      side_effect=RuntimeError('Outbound networking is disabled in the permissions fixture')))
        companion_stub = types.ModuleType('companion')
        companion_stub.Companion = Mock(return_value=None)
        stack.enter_context(patch.dict(sys.modules, {'companion': companion_stub}))
        log_handler = logging.NullHandler()
        logger = logging.getLogger('calendar')
        logger.addHandler(log_handler)
        stack.callback(logger.removeHandler, log_handler)
        with patch('threading.Thread.start'):
            from package.apps.calendar import utils
            stack.enter_context(patch.object(utils, 'get_config', side_effect=lambda: copy.deepcopy(config)))
            stack.enter_context(patch.object(utils, 'CONFIG_FILE', str(config_path)))
            stack.enter_context(patch.object(utils, 'reload_config', return_value=True))
            import webui

        stack.enter_context(patch.object(webui, '_AUTH_DB_PATH', root / 'auth.db'))
        # The group editor now reads the preset catalogue too. Keep every
        # media read/write in this temporary fixture, including non-auth mode.
        from media_library import MediaLibrary
        fixture_library = MediaLibrary(root / 'initial-media')
        stack.enter_context(patch.object(webui, '_get_media_library', return_value=fixture_library))
        if not view_as_mode:
            stack.enter_context(patch.object(webui, 'log_event', return_value=None))
        else:
            stack.enter_context(patch.object(webui, '_bootstrap_default_users_roles'))
        stack.enter_context(patch.object(webui, '_digico_aux_options', return_value=[
            {'channel': 1, 'label': 'Lead vocal'}, {'channel': 2, 'label': 'Keys'},
        ]))
        stack.enter_context(patch.object(webui, '_get_atem_audio_sources_for_permissions', return_value=[
            {'id': 'master', 'label': 'Master'}, {'id': 1, 'label': 'Stage'},
        ]))
        stack.enter_context(patch.object(webui, '_load_companion_surfaces', return_value=[
            {'id': 'foyer', 'label': 'Foyer'}, {'id': 'stage', 'label': 'Stage'},
        ]))
        stack.enter_context(patch.object(webui, '_pixie_permission_catalog', return_value={
            'auditoriums': [
                {'id': 'main', 'name': 'Main auditorium', 'devices': [
                    {'id': 'main-front', 'name': 'Front lights'}, {'id': 'main-back', 'name': 'Back lights'},
                ]},
                {'id': 'kids', 'name': 'Kids room', 'devices': [
                    {'id': 'kids-light', 'name': 'Room lights'},
                ]},
            ],
            'scenes': [{'id': 'welcome', 'name': 'Welcome', 'enabled': True}],
        }))
        webui._init_auth_db()
        media_fixture = {}
        if view_as_mode:
            from media_library import MediaLibrary
            from media_routing import MediaRoutingManager
            from media_ui_harness import FixtureManager, FixtureVideoHub, fixture_image
            stack.enter_context(patch.object(webui, '_get_media_library', side_effect=lambda: media_fixture['library']))
            stack.enter_context(patch.object(webui, '_get_atem_media_manager', side_effect=lambda: media_fixture['atem']))
            stack.enter_context(patch.object(webui, '_get_media_routing_manager', side_effect=lambda **_kwargs: media_fixture['routing']))
            stack.enter_context(patch.object(webui, '_active_media_job', side_effect=lambda:
                webui._routing_preset_runner.active_job() or media_fixture['routing'].active_job() or media_fixture['atem'].snapshot().get('job') or {}))
            stack.enter_context(patch.object(webui, '_get_videohub_state_snapshot', side_effect=lambda **_kwargs: media_fixture['hub'].snapshot()))
            stack.enter_context(patch.object(webui, '_get_videohub_client_from_config', side_effect=lambda: media_fixture['hub']))
            stack.enter_context(patch.object(webui, '_invalidate_videohub_state_snapshot'))

        def reset_fixture():
            with closing(webui._db()) as conn:
                conn.execute('DELETE FROM user_groups')
                conn.execute('DELETE FROM group_pages')
                conn.execute('DELETE FROM groups')
                conn.execute("INSERT INTO groups (id,name,is_admin,is_system) VALUES (1,'Admin',1,1)")
                conn.execute("INSERT INTO groups (id,name,is_admin) VALUES (68,'Media Testing',0)")
                conn.execute("INSERT INTO groups (id,name,is_admin) VALUES (69,'Routing',0)")
                conn.execute('''UPDATE groups SET videohub_allowed_outputs=?, videohub_allowed_inputs=?,
                    videohub_allowed_presets=?, videohub_can_edit_presets=0, companion_click_surfaces=?,
                    digico_allowed_auxes=?, pixie_allowed_auditoriums=?, pixie_allowed_devices=?,
                    pixie_allowed_scenes=?, atem_allowed_audio_sources=?, atem_can_solo_audio=1,
                    atem_can_monitor_audio=0 WHERE id=68''',
                    ('[1, 2]', '[1, 7]', '[3]', '["foyer"]', '["1"]', '["main"]',
                     '{"main":["main-front"]}', '["welcome"]', '["master"]'))
                conn.executemany('INSERT INTO group_pages (group_id,page_key) VALUES (68,?)',
                                 [(key,) for key in ('page:account', 'page:media', 'page:routing')])
                conn.execute("INSERT INTO group_pages (group_id,page_key) VALUES (69,'page:routing')")
                if view_as_mode:
                    conn.execute('DELETE FROM user_sessions')
                    conn.execute('DELETE FROM users')
                    conn.executemany('INSERT INTO users(id,username,password_hash,full_name,email) VALUES(?,?,?,?,?)', [
                        (1, 'fixture-admin', webui.generate_password_hash('fixture-password'), 'Fixture Admin', 'admin@example.invalid'),
                        (2, 'media-operator', webui.generate_password_hash('fixture-password'), 'Media Operator', 'operator@example.invalid'),
                    ])
                    conn.executemany('INSERT INTO user_groups(user_id,group_id) VALUES(?,?)', [(1, 1), (2, 68)])
                    # Match sites with a separate legacy Media/upload group.
                    conn.execute("INSERT INTO groups(id,name,is_admin) VALUES(70,'Media access',0)")
                    conn.execute("DELETE FROM group_pages WHERE group_id=68 AND page_key='page:media'")
                    conn.executemany('INSERT INTO group_pages(group_id,page_key) VALUES(70,?)',
                                     [('page:media',), ('page:media_upload',)])
                    conn.execute('INSERT INTO user_groups(user_id,group_id) VALUES(2,70)')
                    conn.execute("UPDATE groups SET videohub_allowed_outputs='[1]' WHERE id=68")
                conn.commit()
            if view_as_mode:
                library = MediaLibrary(root / ('media-' + secrets.token_hex(4)))
                library.upload(io.BytesIO(fixture_image('WELCOME', '#236858')), 'welcome.png', 'Welcome')
                atem = FixtureManager(library, utils.get_config)
                hub = FixtureVideoHub()
                routing = MediaRoutingManager(get_config=utils.get_config, get_media_manager=lambda: atem,
                    read_videohub=hub.get_routing_state_strict,
                    route_videohub=lambda output, input_: hub.route_video_output(output=output - 1, input_=input_ - 1))
                media_fixture.update(library=library, atem=atem, hub=hub, routing=routing)
                if presets_mode:
                    from routing_presets import RoutingPresetStore, RoutingPresetRunner
                    webui._routing_preset_runner = RoutingPresetRunner()
                    store = RoutingPresetStore(library.root)
                    image_id = library.list()[0]['id']
                    team_image = library.upload(io.BytesIO(fixture_image('TEAM NIGHT', '#643956')), 'team.png', 'Team graphic')
                    choose = store.save({'name': 'Team Night', 'media_id': team_image['id'], 'description': 'Show the team welcome image.'})
                    fixed = store.save({'name': "Mother's Day", 'media_id': image_id, 'output': 1,
                        'actions': [{'label': 'Start the welcome timer', 'method': 'POST', 'path': '/api/timers/apply', 'body': {'preset': 1}}]})
                    store.save({'name': 'Private preset', 'media_id': image_id, 'output': 2})
                    media_fixture['actions'] = []
                    with closing(webui._db()) as conn:
                        conn.execute('DELETE FROM user_groups WHERE user_id=2 AND group_id=70')
                        conn.executemany('INSERT INTO group_pages(group_id,page_key) VALUES(68,?)',
                            [('page:routing_presets',), ('preset:' + choose['id'],), ('preset:' + fixed['id'],)])
                        conn.commit()
            return webui.jsonify({'ok': True})

        with webui.app.app_context():
            reset_fixture()

        def blocked_route(**_kwargs):
            return webui.jsonify({'ok': False, 'error': 'This route is disabled in the isolated permissions fixture.'}), 404

        allowed = {'static', 'admin_permissions_page', 'api_admin_group_update'}
        if view_as_mode:
            allowed.update({'login_page', 'logout_page', 'admin_user_detail_page', 'admin_view_as_user',
                'stop_view_as_user', 'auth_ping', 'auth_touch', 'account_password_page',
                'media_page', 'media_upload_page', 'media_image', 'media_thumbnail',
                'api_media_list', 'api_media_upload', 'api_media_display', 'api_media_display_job',
                'routing_page', 'api_videohub_state', 'api_videohub_labels', 'atem_media_setup_page'})
        if presets_mode:
            allowed.update({'routing_presets_config_page', 'api_routing_presets_config', 'routing_presets_page',
                            'api_routing_presets_list', 'api_routing_preset_prepare', 'api_routing_preset_apply',
                            'api_routing_preset_job'})
        for endpoint in list(webui.app.view_functions):
            if endpoint not in allowed:
                webui.app.view_functions[endpoint] = blocked_route
        webui.app.view_functions['api_status_summary'] = lambda: webui.jsonify({
            'ok': True, **{key: {'connected': False} for key in ('companion', 'propresenter', 'videohub', 'digico', 'atem', 'pixie')},
        })
        webui.app.view_functions['api_activity_log_alerts'] = lambda: webui.jsonify({'ok': True, 'count': 0})
        webui.app.view_functions['api_client_errors'] = lambda: webui.jsonify({'ok': True})
        if presets_mode:
            def timer_action():
                media_fixture['actions'].append(webui.request.get_json())
                return webui.jsonify(ok=True)
            webui.app.view_functions['api_apply_timer_preset'] = timer_action

        @webui.app.get('/__permissions_fixture__/health')
        def fixture_health():
            return webui.jsonify({'fixture': 'tdeck-routing-presets-ui' if presets_mode else ('tdeck-view-as-ui' if view_as_mode else 'tdeck-permissions-ui'), 'isolated': True})

        @webui.app.route('/__permissions_fixture__/state', methods=['GET', 'POST'])
        def fixture_state():
            if webui.request.method == 'POST':
                data = webui.request.get_json(silent=True) or {}
                routes = data.get('routing')
                if (not presets_mode or not isinstance(routes, list) or len(routes) != 3
                        or any(type(value) is not int or not 1 <= value <= 8 for value in routes)):
                    return webui.jsonify(ok=False), 400
                media_fixture['hub'].routing = list(routes)
            return webui.jsonify(actions=media_fixture.get('actions', []),
                routing=list(media_fixture['hub'].routing) if view_as_mode else [],
                media_job=media_fixture['atem'].snapshot().get('job') if view_as_mode else None)

        webui.app.add_url_rule('/__permissions_fixture__/reset', 'fixture_reset', reset_fixture, methods=['POST'])
        if view_as_mode:
            # Only these fixture-control endpoints bypass auth in this isolated
            # process. Application pages/APIs keep the production auth gate.
            def fixture_control_gate():
                if webui.request.endpoint in ('fixture_health', 'fixture_reset', 'fixture_state'):
                    return webui.app.view_functions[webui.request.endpoint]()
            webui.app.before_request_funcs[None].insert(0, fixture_control_gate)
            print(f'Isolated View as demo: http://127.0.0.1:{port}/admin/users/2 (fixture-admin / fixture-password)', flush=True)
        else:
            print(f'Isolated permissions demo: http://127.0.0.1:{port}/admin/permissions?tab=groups#role-68', flush=True)
        webui.app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == '__main__':
    main()
