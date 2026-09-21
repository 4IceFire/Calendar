"""Temporary, hardware-free scheduler editor fixture on 127.0.0.1:5067."""
import copy
import json
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    with tempfile.TemporaryDirectory(prefix='tdeck-scheduler-ui-') as temporary, ExitStack() as stack:
        root = Path(temporary)
        os.chdir(root)
        config = {'auth_enabled': False, 'flask_secret_key': 'isolated-fixture-only',
                  'EVENTS_FILE': str(root / 'events.json'), 'webserver_port': 5067}
        (root / 'config.json').write_text(json.dumps(config))
        for operation in ('connect', 'connect_ex', 'sendto'):
            stack.enter_context(patch('socket.socket.' + operation, side_effect=RuntimeError('Hardware blocked in scheduler fixture')))
        with patch('threading.Thread.start'):
            import webui
        from routing_presets import RoutingPresetStore
        store = RoutingPresetStore(root)
        fixed = store.save({'name': 'Welcome fixed', 'media_id': 'a' * 32, 'output': 2})
        optional = store.save({'name': 'Welcome choose output', 'media_id': 'a' * 32})
        backend = Mock(list_presets=Mock(return_value=[{'id': 9, 'name': 'Stage'}]))
        for context in (
            patch.object(webui.utils, 'get_config', return_value=config),
            patch.object(webui.utils, 'reload_config', return_value=False),
            patch.object(webui.utils, 'load_timer_presets', return_value=[{'name': 'Service', 'time': '10:00'}]),
            patch.object(webui, '_get_routing_preset_store', return_value=store),
            patch.object(webui, '_get_videohub_app', return_value=backend),
            patch.object(webui, '_get_videohub_state_snapshot', return_value={'outputs': [{'number': 2, 'label': 'Foyer'}, {'number': 3, 'label': 'Hall'}]}),
            patch.object(webui, '_bootstrap_default_users_roles'),
            patch.object(webui, '_AUTH_DB_PATH', root / 'auth.db'),
            patch.object(webui, 'log_event'),
        ):
            stack.enter_context(context)
        @webui.app.route('/__scheduler_fixture__/health')
        def health():
            return webui.jsonify(fixture='tdeck-scheduler-ui', fixed=fixed['id'], optional=optional['id'])
        @webui.app.route('/__scheduler_fixture__/reset', methods=['POST'])
        def reset():
            (root / 'trigger_templates.json').write_text('[]')
            (root / 'events.json').write_text('[]')
            webui._trigger_templates_cache.update(snapshot=None, triggers=None)
            return webui.jsonify(ok=True)
        webui.app.run(host='127.0.0.1', port=5067, threaded=True, use_reloader=False)


if __name__ == '__main__':
    main()
