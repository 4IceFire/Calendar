"""Props HTTP/UI routes. Central webui policy enforces session/token security."""
import copy
import os
import threading
from pathlib import Path

from props import PropsStore, PropsService, PropsTriggerState, PropsConflict, _validate_document, target_uuid
from propresentor import ProPresentor

_service_lock = threading.Lock()
_service = None
_service_key = None
# Coordination belongs to the installation, not its replaceable PP endpoint.
# Retain history on endpoint/storage switches, isolated by resolved storage path.
_trigger_states = {}


def props_storage_path(web):
    return Path(os.environ.get('TDECK_PROPS_FILE', str(Path(getattr(web.utils, 'CONFIG_FILE', 'config.json')).parent / 'props.json')))


def get_props_service(web):
    global _service, _service_key
    cfg = web.utils.get_config()
    path = props_storage_path(web)
    host, port = str(cfg.get('propresenter_ip', '127.0.0.1')), int(cfg.get('propresenter_port', 1400))
    key = (str(path.resolve()), host, port)
    with _service_lock:
        if _service is None or key != _service_key:
            trigger_state = _trigger_states.get(key[0])
            if trigger_state is None:
                trigger_state = _trigger_states[key[0]] = PropsTriggerState()
            # Capture immutable endpoint values in this service's factory; an
            # in-flight command never follows later configuration changes.
            _service = PropsService(PropsStore(path), lambda: ProPresentor(host, port, timeout=2.0),
                                    trigger_state=trigger_state)
            _service_key = key
        return _service


def register(web):
    app, request, jsonify = web.app, web.request, web.jsonify

    def mode():
        return web.utils.get_config().get('propresenter_props_use_macros', False) is True

    def state(refresh=False):
        service = web._get_props_service()
        if not service.catalog.diagnostics()['refreshing']:
            actor = web.capture_activity_actor()
            service.on_catalog = lambda success: web.log_event('props.catalog.complete',
                'Refreshed ProPresenter Props catalog' if success else 'Props catalog refresh failed; last successful catalog retained',
                status='success' if success else 'failure', **actor)
        # Pair the storage revision and mode under the config-write lock.
        with web._media_operation_lock:
            return service.state(mode(), refresh)

    def guarded(action, callback):
        try:
            result = callback()
            if request.method not in ('GET', 'HEAD'):
                web.log_event(action, 'Props ' + action.rsplit('.', 1)[-1] + ' succeeded', status='success',
                              details=result['last_triggered'] if action == 'props.preset.trigger' else
                                      {'revision': result.get('revision'), 'mapping_count': len(result.get('library', [])),
                                       'preset_count': len(result.get('order', []))})
            return jsonify(ok=True, **result)
        except (ValueError, OSError, RuntimeError) as exc:
            code = 409 if isinstance(exc, PropsConflict) else 503 if isinstance(exc, OSError) else 502 if isinstance(exc, RuntimeError) else 400
            web.log_event(action, str(exc), status='failure', details={'path': request.path})
            return jsonify(ok=False, error=str(exc)), code

    def body(fields):
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != set(fields):
            raise ValueError('Expected fields: ' + ', '.join(fields))
        return value

    @app.route('/api/props', methods=['GET'])
    def api_props():
        return guarded('props.read', state)

    @app.route('/api/props/order', methods=['PUT'])
    def api_props_order():
        def save():
            value = body(('revision', 'order'))
            web._get_props_service().store.save_order(value['order'], value['revision'])
            return state()
        return guarded('props.order.update', save)

    @app.route('/api/props/library', methods=['PUT'])
    def api_props_library():
        def save():
            value = body(('revision', 'library', 'organization'))
            _validate_document({'version':1, 'revision':value['revision'], 'library':value['library'],
                                'order':[], 'organization':value['organization']})
            service = web._get_props_service()
            # Existing unavailable references may be retained/renamed; new targets must be in catalog.
            existing = {item['id']: item for item in service.store.read()['library']}
            catalog = service.catalog.get()
            props = {target_uuid(item['uuid']) for item in catalog['props']}
            macros = {target_uuid(item['uuid']) for item in catalog['macros']}
            if not isinstance(value['library'], list):
                raise ValueError('Library must be an array')
            for item in value['library']:
                if not isinstance(item, dict):
                    raise ValueError('Invalid mapping')
                old = existing.get(item.get('id'), {})
                for key, targets in (('prop_uuid', props), ('macro_uuid', macros)):
                    target = item.get(key)
                    if target is not None and target != old.get(key):
                        if catalog['stale'] or catalog['lastError'] or target_uuid(target) not in targets:
                            raise ValueError('Refresh catalog and select an existing ProPresenter target')
            service.store.save_library(value['library'], value['organization'], value['revision'])
            return state()
        return guarded('props.library.update', save)

    @app.route('/api/props/catalog/refresh', methods=['POST'])
    def api_props_catalog_refresh():
        return guarded('props.catalog.refresh', lambda: state(refresh=True))

    @app.route('/api/props/trigger', methods=['POST'])
    def api_props_trigger():
        def trigger():
            value = body(('revision', 'position', 'use_macros'))
            return {'last_triggered': web._get_props_service().trigger(value['position'], value['revision'],
                value['use_macros'], validate_mode=mode, snapshot_lock=web._media_operation_lock)}
        return guarded('props.preset.trigger', trigger)

    @app.route('/api/props/settings', methods=['PUT'])
    def api_props_settings():
        def save():
            value = body(('use_macros', 'previous_use_macros', 'revision'))
            if type(value['use_macros']) is not bool or type(value['previous_use_macros']) is not bool:
                raise ValueError('Use Macros for Props must be boolean')
            service = web._get_props_service()
            if not service.lock.acquire(blocking=False):
                raise PropsConflict('A prop trigger is in progress; retry settings after it completes')
            try:
                with web._media_operation_lock:
                    cfg = copy.deepcopy(web.utils.get_config())
                    if (cfg.get('propresenter_props_use_macros', False) is True) != value['previous_use_macros']:
                        raise PropsConflict('Props settings changed. Reload.')
                    current = service.store.read()
                    service.store.save_order(current['order'], value['revision'])
                    cfg['propresenter_props_use_macros'] = value['use_macros']
                    web.utils.save_config(cfg)
                    web.utils.reload_config(force=True)
            finally:
                service.lock.release()
            return state()
        return guarded('props.settings.update', save)

    @app.route('/props')
    @web.require_page('page:props', 'Props')
    def props_page():
        return web.render_template('props.html', page_title='Props', configure_props=web.can_access('page:props_configure'))

    @app.route('/props/configure')
    @web.require_page('page:props_configure', 'Configure Props')
    def props_configure_page():
        return web.render_template('props_configure.html', page_title='Configure Props')
