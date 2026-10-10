"""Props HTTP/UI routes. Central webui policy enforces session/token security."""
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

    def state(refresh=False):
        service = web._get_props_service()
        if not service.catalog.diagnostics()['refreshing']:
            actor = web.capture_activity_actor()
            service.on_catalog = lambda success: web.log_event('props.catalog.complete',
                'Refreshed ProPresenter Props catalog' if success else 'Props catalog refresh failed; last successful catalog retained',
                status='success' if success else 'failure', **actor)
        return service.state(refresh=refresh)

    def guarded(action, callback):
        try:
            result = callback()
            if request.method not in ('GET', 'HEAD'):
                web.log_event(action, 'Props ' + action.rsplit('.', 1)[-1] + ' succeeded', status='success',
                              details=result['last_triggered'] if action in ('props.preset.trigger', 'props.library.trigger') else
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
            _validate_document({'version':2, 'revision':value['revision'], 'library':value['library'],
                                'order':[], 'organization':value['organization']})
            service = web._get_props_service()
            # Existing unavailable references may be retained/renamed; new targets must be in catalog.
            existing = {item['id']: item for item in service.store.read()['library']}
            catalog = service.catalog.get()
            props = {target_uuid(item['uuid']) for item in catalog['props']}

            if not isinstance(value['library'], list):
                raise ValueError('Library must be an array')
            for item in value['library']:
                if not isinstance(item, dict):
                    raise ValueError('Invalid mapping')
                old = existing.get(item.get('id'), {})
                target = item['prop_uuid']
                if target != old.get('prop_uuid'):
                    if catalog['stale'] or catalog['lastError'] or target_uuid(target) not in props:
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
            value = request.get_json(silent=True)
            if not isinstance(value, dict) or set(value) not in ({'revision', 'position'}, {'revision', 'position', 'use_macros'}):
                raise ValueError('Expected fields: revision, position')
            # Deprecated false-only shim for Companion 0.5.0; never a mode.
            if 'use_macros' in value and value['use_macros'] is not False:
                raise ValueError('Only direct Props triggers are supported; reload or update the client')
            return {'last_triggered': web._get_props_service().trigger(value['position'], value['revision'])}
        return guarded('props.preset.trigger', trigger)

    @app.route('/api/props/library/trigger', methods=['POST'])
    def api_props_library_trigger():
        def trigger():
            value = body(('id', 'revision'))
            return {'last_triggered': web._get_props_service().trigger_library(value['id'], value['revision'])}
        return guarded('props.library.trigger', trigger)

    @app.route('/props')
    @web.require_page('page:props', 'Props')
    def props_page():
        return web.render_template('props.html', page_title='Props', configure_props=web.can_access('page:props_configure'))

    @app.route('/props/configure')
    @web.require_page('page:props_configure', 'Configure Props')
    def props_configure_page():
        return web.render_template('props_configure.html', page_title='Configure Props')
