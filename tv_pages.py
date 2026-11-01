"""Room-scoped TV interfaces. Existing TV groups ARE rooms; IDs never change."""
import copy
import math
import io
import uuid
import warnings
from pathlib import Path
from urllib.parse import quote
import re
import threading
import time
from functools import wraps
from flask import abort, jsonify, request, g, send_file
from flask_login import current_user


def room_catalog(cfg):
    tvs = {tv['id']: tv for tv in cfg.get('hisense_tvs', []) if isinstance(tv, dict) and tv.get('id')}
    assigned = set()
    rooms = []
    for group in cfg.get('hisense_tv_groups', []):
        ids = [ident for ident in group.get('tv_ids', []) if ident in tvs and ident not in assigned]
        assigned.update(ids)
        rooms.append({'id': group['id'], 'name': group.get('name') or group['id'],
                      'tvs': [tvs[ident] for ident in ids]})
    # '@' cannot occur in an existing normalized Hisense group ID. Never let
    # a valid legacy group called '_ungrouped' also grant the root's TVs.
    rooms.append({'id': '@ungrouped', 'name': 'Ungrouped', 'tvs': [tv for ident, tv in tvs.items() if ident not in assigned]})
    return rooms


def permitted_ids(cfg, rows, grants):
    result = set()
    for row in rows:
        keys = grants.get(int(row['id']), set())
        for room in room_catalog(cfg):
            room_id = room['id']
            if 'tvroom:' + room_id not in keys:
                continue
            for tv in room['tvs']:
                if 'tvdevice:' + room_id + ':*' in keys or 'tvdevice:' + room_id + ':' + tv['id'] in keys:
                    result.add(tv['id'])
    return result


def public_tv(tv):
    result = {key: copy.deepcopy(tv[key]) for key in ('id', 'name', 'connected', 'enabled', 'expectedOff', 'sources', 'observation') if key in tv}
    result['readError'] = 'TV connection/read failed' if tv.get('readFailure') or (not tv.get('connected') and not tv.get('expectedOff')) else ''
    return result


def validate_mappings(value, cfg):
    """One canonical contract for dedicated saves and imported setup."""
    if not isinstance(value, dict):
        raise ValueError('TV media mappings must be an object.')
    valid_ids = {tv['id'] for room in room_catalog(cfg) for tv in room['tvs']}
    result = {}
    for ident, feed in value.items():
        if (ident not in valid_ids or not isinstance(feed, dict) or set(feed) != {'output', 'source'}
            or isinstance(feed['output'], bool) or not isinstance(feed['output'], int) or not 1 <= feed['output'] <= 65535
            or not isinstance(feed['source'], str) or not re.fullmatch(r'[A-Za-z0-9_. -]{1,80}', feed['source'].strip())
            or not feed['source'].strip()):
            raise ValueError('Invalid TV media mapping. Choose an existing TV, positive output and feed input ID.')
        result[ident] = {'output': feed['output'], 'source': feed['source'].strip()}
    return result


def images_dir():
    return Path(__file__).resolve().parent / 'tv_room_images'


def image_path(filename):
    if not isinstance(filename, str) or not re.fullmatch(r'[a-f0-9]{32}\.png', filename):
        raise ValueError('Choose an uploaded room image.')
    root = images_dir().resolve()
    path = root / filename
    if path.is_symlink() or path.resolve().parent != root or not path.is_file():
        raise ValueError('Room image is missing. Upload it again.')
    return path


_room_image_lock = threading.RLock()


def preview_image_path(filename):
    """Small local display derivative; preserve the uploaded original."""
    from PIL import Image
    original = image_path(filename)
    with _room_image_lock:
        folder = images_dir() / 'previews'
        if folder.is_symlink():
            raise ValueError('Invalid room preview directory.')
        folder.mkdir(exist_ok=True)
        cached = folder / (original.stem + '.jpg')
        if cached.is_symlink():
            raise ValueError('Invalid room preview file.')
        if cached.is_file() and cached.stat().st_mtime_ns >= original.stat().st_mtime_ns:
            return cached
        if original.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('Room image exceeds the image limit.')
        with Image.open(original) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError('Room image exceeds the pixel limit.')
            image.load()
            preview = image.convert('RGB')
            preview.thumbnail((1280, 1280))
            preview.info.clear()
            encoded = io.BytesIO()
            preview.save(encoded, format='JPEG', quality=82, optimize=True, progressive=True)
        content = encoded.getvalue()
        if len(content) > 2 * 1024 * 1024:
            raise ValueError('Room preview exceeds the cache entry limit.')
        assets = [entry for entry in folder.glob('*.jpg') if entry != cached and re.fullmatch(r'[a-f0-9]{32}\.jpg', entry.name) and entry.is_file() and not entry.is_symlink()]
        total = sum(entry.stat().st_size for entry in assets)
        for entry in sorted(assets, key=lambda entry: entry.stat().st_mtime_ns):
            if total + len(content) <= 16 * 1024 * 1024:
                break
            total -= entry.stat().st_size
            entry.unlink()
        temporary = folder / (uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(content)
            temporary.replace(cached)
        finally:
            temporary.unlink(missing_ok=True)
        return cached


def background_url(room, layout):
    filename = layout.get('background')
    return '/tv-room-images/' + quote(room, safe='') + '/' + filename + '?preview=1' if filename else ''


def validate_layouts(value, cfg):
    """Positions belong to existing TVs in existing rooms, not VideoHub outputs."""
    if not isinstance(value, dict):
        raise ValueError('Room layouts must be an object.')
    rooms = {room['id']: {tv['id'] for tv in room['tvs']} for room in room_catalog(cfg)}
    clean = {}
    for ident, layout in value.items():
        if ident not in rooms or not isinstance(layout, dict) or set(layout) - {'positions', 'background'}:
            raise ValueError('Choose an existing TV room.')
        positions = layout.get('positions', {})
        if not isinstance(positions, dict):
            raise ValueError('TV positions must be an object.')
        for tv, point in positions.items():
            if tv not in rooms[ident] or not isinstance(point, dict) or set(point) != {'x', 'y'}:
                raise ValueError('Positions must refer to TVs in this room.')
            if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not 0 <= v <= 1 or not math.isfinite(v) for v in point.values()):
                raise ValueError('Positions must be normalized numbers between 0 and 1.')
        result = {'positions': copy.deepcopy(positions)}
        if layout.get('background'):
            if not isinstance(layout['background'], str) or not re.fullmatch(r'[a-f0-9]{32}\.png', layout['background']):
                raise ValueError('Choose an uploaded room image.')
            result['background'] = layout['background']
        clean[ident] = result
    return clean


def current_layouts(cfg):
    # Removed rooms/moved TVs must never break the page or leak old positions.
    clean = {}
    saved = cfg.get('hisense_room_layouts', {})
    if isinstance(saved, dict):
        for room in room_catalog(cfg):
            if room['id'] not in saved:
                continue
            value = saved[room['id']]
            if not isinstance(value, dict):
                continue
            value = copy.deepcopy(value)
            members = {tv['id'] for tv in room['tvs']}
            value['positions'] = {key: point for key, point in value.get('positions', {}).items() if key in members} if isinstance(value.get('positions', {}), dict) else {}
            try:
                clean.update(validate_layouts({room['id']: value}, cfg))
            except ValueError:
                pass
    return clean


def register(w):
    app = w.app
    jobs = {}
    job_lock = threading.RLock()

    def ids(page):
        cfg = w.utils.get_config()
        if not w._auth_enabled() or (getattr(current_user, 'is_authenticated', False) and current_user.is_admin_group):
            return {tv['id'] for room in room_catalog(cfg) for tv in room['tvs']}
        uid = int(current_user.get_id()) if getattr(current_user, 'is_authenticated', False) else None
        rows = w._get_user_groups_for_page(uid, page)
        conn = w._db()
        try:
            grants = {int(row['id']): {entry['page_key'] for entry in conn.execute(
                'SELECT page_key FROM group_pages WHERE group_id=?', (row['id'],)).fetchall()} for row in rows}
        finally:
            conn.close()
        return permitted_ids(cfg, rows, grants)

    def authorize(tv_id, page='page:tv_controls'):
        if not w.can_access(page) or tv_id not in ids(page):
            abort(403)

    def mapping(tv_id):
        try:
            cfg = w.utils.get_config()
            saved = cfg.get('hisense_media_mappings', {})
            if not isinstance(saved, dict):
                raise ValueError('Invalid TV media mappings')
            value = validate_mappings({tv_id: saved.get(tv_id)}, cfg)[tv_id]
        except (ValueError, TypeError, KeyError):
            value = {}
        output = value.get('output')
        source = value.get('source')
        if isinstance(output, bool) or not isinstance(output, int) or not 1 <= output <= 65535 or not isinstance(source, str) or not source.strip():
            raise ValueError('Media mapping required: ask an administrator to set the VideoHub output and TV feed input in TV Setup.')
        return {'output': output, 'source': source}

    def state_payload(page, allowed=None):
        allowed = ids(page) if allowed is None else allowed
        manager, error = w._hisense_manager_or_error()
        if error:
            return error
        snapshot = manager.status()
        controls = ids('page:tv_controls') if w.can_access('page:tv_controls') else set()
        tvs = []
        for tv in snapshot.get('tvs', []):
            if tv['id'] not in allowed:
                continue
            item = public_tv(tv)
            item['canControl'] = tv['id'] in controls
            try:
                item['mediaMapping'] = mapping(tv['id'])
            except ValueError:
                item['mediaMapping'] = None
            tvs.append(item)
            controller = getattr(manager, 'controllers', {}).get(tv['id'])
            if controller:
                controller.visible_until = time.monotonic() + 5
                controller.fast_until = max(controller.fast_until, controller.visible_until)
        rooms = []
        for room in room_catalog(w.utils.get_config()):
            members = [tv['id'] for tv in room['tvs'] if tv['id'] in allowed]
            if members:
                layout = current_layouts(w.utils.get_config()).get(room['id'], {'positions': {}})
                layout['positions'] = {key: point for key, point in layout['positions'].items() if key in members}
                layout['backgroundUrl'] = background_url(room['id'], layout)
                rooms.append({'id': room['id'], 'name': room['name'], 'tvIds': members, 'layout': layout})
        return jsonify({'ok': True, 'tvs': tvs, 'rooms': rooms})

    @app.post('/api/config/tv-room-layouts/background')
    def tv_room_background_upload():
        from PIL import Image, ImageOps, UnidentifiedImageError
        principal = 'user:' + str(current_user.get_id()) if getattr(current_user, 'is_authenticated', False) else 'address:' + str(request.remote_addr or 'unknown')
        admission_error = w._admit_media_upload(principal)
        if admission_error is not None:
            return admission_error
        upload = request.files.get('file')
        if upload is None or set(request.files) != {'file'} or len(request.files.getlist('file')) != 1 or set(request.form) - {'_csrf'}:
            abort(400)
        content = upload.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            abort(413)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as source:
                    if source.format not in ('JPEG', 'PNG', 'WEBP') or source.width * source.height > 16_000_000 or getattr(source, 'n_frames', 1) != 1:
                        raise ValueError('Use a still JPEG, PNG or WebP up to 16 megapixels.')
                    source.load()
                    normalized = ImageOps.exif_transpose(source).convert('RGB')
                    normalized.thumbnail((3840, 2160))
                    normalized.info.clear()
                    encoded = io.BytesIO()
                    normalized.save(encoded, format='PNG')
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            return jsonify({'ok': False, 'error': 'Use a valid still JPEG, PNG or WebP up to 10 MB and 16 megapixels.'}), 400
        content = encoded.getvalue()
        if len(content) > 10 * 1024 * 1024:
            abort(413)
        with w._media_operation_lock:
            folder = images_dir()
            folder.mkdir(parents=True, exist_ok=True)
            assets = [p for p in folder.glob('*.png') if p.is_file() and not p.is_symlink()]
            if len(assets) >= 100 or sum(p.stat().st_size for p in assets) + len(content) > 100 * 1024 * 1024:
                return jsonify({'ok': False, 'error': 'Room image storage is full (100 images / 100 MB). Ask an administrator to archive unused room images.'}), 409
            filename = uuid.uuid4().hex + '.png'
            path = folder / filename
            # Exclusive creation; no caller-selected paths, extensions or metadata.
            with path.open('xb') as stream:
                stream.write(content)
        w.log_event('hisense.room.image', 'Uploaded a TV room background (not assigned until Save)', status='success')
        return jsonify({'ok': True, 'image': filename})

    @app.get('/tv-room-images/<room_id>/<filename>')
    def tv_room_image(room_id, filename):
        cfg = w.utils.get_config()
        if not w.can_access('page:config'):
            permitted = set().union(*(ids(page) for page in ('page:tv_controls', 'page:tv_diagnostics') if w.can_access(page)))
            room = next((room for room in room_catalog(cfg) if room['id'] == room_id), None)
            if not room or not any(tv['id'] in permitted for tv in room['tvs']):
                abort(403)
            if current_layouts(cfg).get(room_id, {}).get('background') != filename:
                abort(404)
        try:
            preview = request.args.get('preview') == '1'
            path = preview_image_path(filename) if preview else image_path(filename)
        except (ValueError, OSError):
            abort(404)
        response = send_file(path, mimetype='image/jpeg' if preview else 'image/png', conditional=True)
        response.headers['Cache-Control'] = 'private, no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    tv_room_image._required_any_page_keys = ('page:config', 'page:tv_controls', 'page:tv_diagnostics')

    @app.get('/config/tv-rooms')
    @w.require_page('page:config', 'TV Room Pictures')
    def tv_room_editor():
        return w.render_template('tv_rooms.html')

    @app.route('/api/config/tv-room-layouts', methods=['GET', 'PUT'])
    def tv_room_layouts():
        if request.method == 'GET':
            cfg = w.utils.get_config()
            return jsonify({'ok': True, 'layouts': current_layouts(cfg),
                            'images': sorted(p.name for p in images_dir().glob('*.png') if re.fullmatch(r'[a-f0-9]{32}\.png', p.name) and p.is_file() and not p.is_symlink())[:100],
                            'rooms': [{'id': room['id'], 'name': room['name'],
                                       'tvs': [{'id': tv['id'], 'name': tv.get('name', tv['id'])} for tv in room['tvs']]}
                                      for room in room_catalog(cfg)]})
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'layouts'}:
            abort(400)
        with w._media_operation_lock:
            cfg = w.utils.get_config()
            try:
                cfg['hisense_room_layouts'] = validate_layouts(data['layouts'], cfg)
                for layout in cfg['hisense_room_layouts'].values():
                    if layout.get('background'):
                        image_path(layout['background'])
            except ValueError as error:
                return jsonify({'ok': False, 'error': str(error)}), 400
            w.utils.save_config(cfg)
            w.utils.reload_config(force=True)
        w.log_event('hisense.room.layout', 'Saved TV room pictures and positions (no device commands)', status='success')
        return jsonify({'ok': True})

    @app.get('/tv-diagnostics')
    @w.require_page('page:tv_diagnostics', 'TV Diagnostics')
    def tv_diagnostics():
        return w.render_template('tv_pages.html', diagnostics=True)

    @app.get('/tv-controls')
    @w.require_page('page:tv_controls', 'TV Controls')
    def tv_controls():
        selected_tv = request.args.get('tv', '')
        if selected_tv:
            authorize(selected_tv)
        return_to = 'diagnostics' if request.args.get('from') == 'diagnostics' and w.can_access('page:tv_diagnostics') else ''
        return w.render_template('tv_pages.html', diagnostics=False, selected_tv=selected_tv, return_to=return_to)

    @app.get('/api/tv-pages/state')
    def tv_page_state():
        page = 'page:tv_diagnostics' if request.args.get('mode') == 'diagnostics' else 'page:tv_controls'
        if not w.can_access(page):
            abort(403)
        return state_payload(page)

    @app.get('/tv-controls/<tv_id>/media')
    @w.require_page('page:tv_controls', 'TV Controls')
    def tv_media_page(tv_id):
        authorize(tv_id)
        try:
            feed = mapping(tv_id)
        except ValueError as error:
            return str(error), 409
        return w.render_template('media.html', output=feed['output'], output_label='this TV',
                                 can_display=True, tv_id=tv_id, hide_connection_status=True,
                                 media_permissions={'upload': False, 'save': False}, retention_days=7)

    def media_item(item, tv_id):
        # The shared catalogue has already applied missing-file flags and
        # uploader-field permissions in one bounded index read.
        result = dict(item)
        result['url'] = '/tv-media/' + tv_id + '/' + item['id'] + '/image'
        result['thumbnail_url'] = '/tv-media/' + tv_id + '/' + item['id'] + '/thumbnail'
        return result

    def public_job(job):
        result = w._public_media_display_job(job)
        if job.get('status') == 'confirmation_required':
            # Do not disclose room/output labels outside the reader's TV scope.
            result['message'] = 'This image will also display on ' + ', '.join(
                'Output ' + str(number) for number in job['allocation']['sharedOutputs']) + '. Continue?'
        return result

    @app.get('/api/tv-media/<tv_id>/images')
    def tv_media_list(tv_id):
        authorize(tv_id)
        return jsonify({'ok': True, 'items': [media_item(item, tv_id) for item in w._media_catalog_payload()]})

    @app.get('/tv-media/<tv_id>/<media_id>/<kind>')
    @w.require_page('page:tv_controls', 'TV Controls')
    def tv_media_image(tv_id, media_id, kind):
        authorize(tv_id)
        try:
            item = w._media_authorized_item(media_id)
            if item.get('temporary') or kind not in ('image', 'thumbnail'):
                abort(403)
            return w._send_media_image(media_id, thumbnail=kind == 'thumbnail')
        except KeyError:
            abort(404)

    @app.post('/api/tv-media/<tv_id>/display')
    def tv_media_display(tv_id):
        from atem_media import BusyError
        authorize(tv_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - {'media_id', 'shared_confirmation'} or not isinstance(data.get('media_id'), str):
            return jsonify({'ok': False, 'error': 'Choose a saved image.'}), 400
        actor = w.capture_activity_actor()
        try:
            with w._media_operation_lock:
                feed = mapping(tv_id)
                manager, error = w._hisense_manager_or_error()
                if error:
                    return error
                observation = manager.get(tv_id).status().get('observation', {}).get('fields', {})
                source = observation.get('source', {})
                if source.get('stale', True) or source.get('value') != feed['source']:
                    raise ValueError('Select the configured TV feed input with the remote first; fresh TV source feedback is required. Media never changes TV power or input.')
                item = w._media_authorized_item(data['media_id'])
                if item.get('temporary'):
                    abort(403)
                confirmation = w._read_shared_media_confirmation(data.get('shared_confirmation'), data['media_id'], feed['output'])
                def completed(job):
                    w.log_event('hisense.media.display', 'TV media display ' + job['status'],
                                status='success' if job['status'] == 'succeeded' else 'warning',
                                target_type='hisense_tv', target_id=tv_id,
                                details={'job_id': job['id'], 'status': job['status']}, **actor)
                job = w._get_media_routing_manager(refresh=True).display(data['media_id'], feed['output'],
                        allowed_inputs=None, on_complete=completed, shared_confirmation=confirmation)
                with job_lock:
                    jobs[job['id']] = {'owner': w._preset_owner(), 'tv': tv_id, 'mapping': feed, 'time': time.monotonic()}
                    for key in list(jobs):
                        if time.monotonic() - jobs[key]['time'] > 3600:
                            del jobs[key]
                    while len(jobs) > 200:
                        del jobs[next(iter(jobs))]
            return jsonify({'ok': True, 'job': public_job(job)}), 202
        except BusyError:
            return jsonify({'ok': False, 'error': 'Media is busy. Wait before trying again.'}), 409
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            return jsonify({'ok': False, 'error': str(error) if isinstance(error, ValueError) else 'Media display failed; check Activity Log.'}), 409

    @app.get('/api/tv-media/<tv_id>/display/<job_id>')
    def tv_media_job(tv_id, job_id):
        authorize(tv_id)
        with job_lock:
            saved = jobs.get(job_id)
            if not saved or saved['owner'] != w._preset_owner() or saved['tv'] != tv_id:
                abort(404)
            try:
                if mapping(tv_id) != saved['mapping']:
                    abort(403)
            except ValueError:
                abort(403)
        try:
            return jsonify({'ok': True, 'job': public_job(w._get_media_routing_manager().get_job(job_id))})
        except KeyError:
            abort(404)

    @app.route('/api/config/tv-media-mappings', methods=['GET', 'PUT'])
    def tv_media_mappings():
        cfg = w.utils.get_config()
        if request.method == 'GET':
            saved = cfg.get('hisense_media_mappings', {})
            clean = {}
            if isinstance(saved, dict):
                for ident, feed in saved.items():
                    try:
                        clean.update(validate_mappings({ident: feed}, cfg))
                    except ValueError:
                        pass
            warning = '' if saved == clean else 'Some saved mappings are invalid or refer to removed TVs. Review the entries before saving.'
            return jsonify({'ok': True, 'mappings': clean, 'warning': warning, 'rooms': room_catalog(cfg)})
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'mappings'} or not isinstance(data['mappings'], dict):
            abort(400)
        with w._media_operation_lock:
            cfg = w.utils.get_config()
            try:
                cfg['hisense_media_mappings'] = validate_mappings(data['mappings'], cfg)
            except ValueError as error:
                return jsonify({'ok': False, 'error': str(error)}), 400
            w.utils.save_config(cfg)
            w.utils.reload_config(force=True)
        w.log_event('hisense.media.mapping', 'Updated TV media signal paths (no device commands)', source='web', status='success')
        return jsonify({'ok': True})

    # Wrap legacy operational endpoints too. Tokens keep their existing contract;
    # browser scopes must never be bypassable using a Companion-shaped URL.
    def scoped(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            legacy = getattr(g, 'api_principal', {}).get('type') == 'legacy_anonymous'
            if w._api_request_is_automation_principal() or legacy or w.can_access('page:config'):
                return fn(*args, **kwargs)
            target = kwargs.get('target_id', kwargs.get('tv_id', ''))
            write = request.method not in ('GET', 'HEAD')
            page = 'page:tv_controls' if write else ('page:tv_diagnostics' if w.can_access('page:tv_diagnostics') else 'page:tv_controls')
            allowed = ids(page)
            if not write:
                allowed = set().union(*(ids(key) for key in ('page:tv_controls', 'page:tv_diagnostics') if w.can_access(key)))
            if target.startswith('group:'):
                # Aggregates can leak hidden members. Never execute browser group controls.
                if write:
                    abort(403)
                room_id = target[6:]
                room = next((room for room in room_catalog(w.utils.get_config()) if room['id'] == room_id), None)
                if not room or not room['tvs'] or not all(tv['id'] in allowed for tv in room['tvs']):
                    abort(403)
            elif target:
                if (target[3:] if target.startswith('tv:') else target) not in allowed:
                    abort(403)
            if write:
                data = request.get_json(silent=True) or {}
                if request.path.endswith('/power') and data.get('state') not in ('on', 'off'):
                    abort(400)
                if request.path.endswith('/reconnect'):
                    abort(403)
            response = app.make_response(fn(*args, **kwargs))
            payload = response.get_json(silent=True)
            if isinstance(payload, dict):
                if 'tvs' in payload:
                    return state_payload(page, allowed=allowed)
                for key in ('tv', 'target'):
                    if isinstance(payload.get(key), dict):
                        payload[key] = public_tv(payload[key])
                if payload.get('error'):
                    payload['error'] = 'TV operation failed; check connection or setup.'
                response.set_data(app.json.dumps(payload))
            return response
        return wrapped

    for name in list(app.view_functions):
        if name.startswith('api_hisense_') and name not in ('api_hisense_config_get', 'api_hisense_config_put') and not any(part in name for part in ('pair', 'preflight')):
            app.view_functions[name] = scoped(app.view_functions[name])

    original_media_job = app.view_functions['api_media_display_job']
    @wraps(original_media_job)
    def routing_job_without_tv_leak(job_id):
        with job_lock:
            if job_id in jobs:
                abort(403)
        return original_media_job(job_id)
    app.view_functions['api_media_display_job'] = routing_job_without_tv_leak

    @app.context_processor
    def tv_permission_catalog():
        # Setup catalog is only rendered by the Admin group editor.
        return {'tv_permission_rooms': room_catalog(w.utils.get_config()) if request.path == '/admin/permissions' else []}
