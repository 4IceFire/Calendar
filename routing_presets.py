"""Saved routing presets and one-shot, asynchronous preset execution.

Authorization and operational API policy live in webui. Nothing executes on
startup, lookup or configuration save. An execution ID is never replayed.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

from atem_media import BusyError
from media_library import _atomic_write

_ID = re.compile(r'[a-f0-9]{32}\Z')
_STORE_LOCK = threading.RLock()
MAX_PRESETS = 500
MAX_ACTIONS = 20


def preset_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('Select a saved preset or image.')
    return value


def validate_action(value):
    if not isinstance(value, dict) or set(value) - {'label', 'method', 'path', 'body'}:
        raise ValueError('An API action needs a method, local path and optional JSON body.')
    if any(not isinstance(value.get(key, default), str) for key, default in
           (('method', 'POST'), ('path', ''), ('label', ''))):
        raise ValueError('Use text for the action description, method and path.')
    method = value.get('method', 'POST').upper()
    path = value.get('path', '').strip()
    parts = urlsplit(path)
    if (method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') or len(path) > 1024
            or not path.startswith('/api/') or parts.scheme or parts.netloc or parts.fragment
            or '\\' in path or '://' in path or '%' in parts.path or '//' in parts.path
            or any(segment in ('.', '..') for segment in parts.path.split('/'))
            or any(ord(char) < 32 for char in path)):
        raise ValueError('Use a local TDeck /api/ path and a supported HTTP method.')
    label = str(value.get('label', '')).strip()
    if not label or len(label) > 120:
        raise ValueError('Give each API action a short description for the confirmation screen.')
    body = value.get('body')
    try:
        if len(json.dumps(body, allow_nan=False).encode()) > 32768:
            raise ValueError('Action body is too large.')
    except (TypeError, ValueError):
        raise ValueError('Use a valid JSON body of at most 32 KB.') from None
    if method == 'GET' and body is not None:
        raise ValueError('GET actions use the path query, not a JSON body.')
    return {'label': label, 'method': method, 'path': path, 'body': copy.deepcopy(body)}


def validate_preset(value):
    if not isinstance(value, dict) or set(value) - {'name', 'description', 'media_id', 'output', 'actions', 'enabled'}:
        raise ValueError('Supply a name, image, optional output and API actions.')
    if any(not isinstance(value.get(key, ''), str) for key in ('name', 'description')):
        raise ValueError('Use text for the preset name and description.')
    name = value.get('name', '').strip()
    description = value.get('description', '').strip()
    if not name or len(name) > 120 or len(description) > 400:
        raise ValueError('Use a preset name of 1–120 characters and a description up to 400 characters.')
    output = value.get('output')
    if output is not None and (type(output) is not int or output < 1):
        raise ValueError('Choose a VideoHub output or leave it for the user to choose.')
    actions = value.get('actions', [])
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise ValueError(f'A preset can contain at most {MAX_ACTIONS} API actions.')
    enabled = value.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError('Enabled must be true or false.')
    result = {'name': name, 'description': description, 'media_id': preset_id(value.get('media_id')),
              'output': output, 'enabled': enabled, 'actions': [validate_action(action) for action in actions]}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 65536:
        raise ValueError('Preset configuration exceeds 64 KB.')
    return result


class RoutingPresetStore:
    def __init__(self, root, legacy_images=()):
        self.path = Path(root) / 'routing_presets.json'
        with _STORE_LOCK:
            if not self.path.exists():
                entries = []
                for image in legacy_images:
                    if image.get('preset'):
                        if len(entries) >= MAX_PRESETS:
                            raise ValueError('Too many legacy image presets to import. The limit is 500.')
                        entries.append({**validate_preset({'name': image['name'], 'media_id': image['id']}),
                                        'id': uuid.uuid4().hex, 'revision': uuid.uuid4().hex})
                self._write(entries)

    def _read(self):
        try:
            if self.path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('Preset file is too large')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or not isinstance(data.get('presets'), list) or len(data['presets']) > MAX_PRESETS:
                raise ValueError('Invalid preset file')
            items, ids = [], set()
            for item in data['presets']:
                identity, revision = preset_id(item['id']), preset_id(item['revision'])
                if identity in ids:
                    raise ValueError('Duplicate preset')
                ids.add(identity)
                items.append({**validate_preset({k: v for k, v in item.items() if k not in ('id', 'revision')}),
                              'id': identity, 'revision': revision})
            return items
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise OSError('Routing presets cannot be read. Restore the preset file from a backup.') from error

    def _write(self, items):
        _atomic_write(self.path, json.dumps({'version': 1, 'presets': items}, ensure_ascii=False, indent=2).encode('utf-8'))

    def list(self):
        with _STORE_LOCK:
            return sorted(self._read(), key=lambda item: (item['name'].casefold(), item['id']))

    def get(self, identity):
        identity = preset_id(identity)
        return next((item for item in self.list() if item['id'] == identity), None)

    def save(self, value, identity=None, expected_revision=None):
        value = validate_preset(value)
        with _STORE_LOCK:
            items = self._read()
            old = next((item for item in items if item['id'] == identity), None)
            if identity and old is None:
                raise KeyError('Preset not found.')
            if old and old['revision'] != expected_revision:
                raise ValueError('This preset changed. Refresh before saving it again.')
            if not old and len(items) >= MAX_PRESETS:
                raise ValueError('The preset limit has been reached.')
            result = {**value, 'id': identity or uuid.uuid4().hex, 'revision': uuid.uuid4().hex}
            self._write([item for item in items if item['id'] != identity] + [result])
            return copy.deepcopy(result)

    def delete(self, identity, revision):
        with _STORE_LOCK:
            item = self.get(identity)
            if item is None:
                raise KeyError('Preset not found.')
            if item['revision'] != revision:
                raise ValueError('This preset changed. Refresh before deleting it.')
            self._write([entry for entry in self._read() if entry['id'] != identity])


class RoutingPresetRunner:
    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = OrderedDict()
        self._active = None

    def active_job(self):
        with self._lock:
            return copy.deepcopy(self._jobs.get(self._active))

    def get(self, identity):
        with self._lock:
            return copy.deepcopy(self._jobs.get(identity))

    def start(self, identity, preset, output, owner, *, display, execute, completed):
        with self._lock:
            existing = self._jobs.get(identity)
            if existing:
                if existing['owner'] != owner or existing['presetId'] != preset['id']:
                    raise ValueError('This confirmation belongs to another request.')
                return copy.deepcopy(existing)
            if self._active:
                raise BusyError('Another preset is running. Please wait for it to finish.')
            job = {'id': identity, 'presetId': preset['id'], 'name': preset['name'], 'mediaId': preset['media_id'],
                   'output': output, 'owner': owner, 'status': 'loading', 'message': 'Displaying the preset image…',
                   'imageDisplayed': False, 'actionsCompleted': 0, 'createdAt': time.time()}
            # Confirmations expire in 5 minutes. Retain executions longer to
            # prevent a retried confirmation from executing its actions twice.
            for key, value in list(self._jobs.items()):
                if time.time() - value['createdAt'] > 600 and key != self._active:
                    del self._jobs[key]
            if len(self._jobs) >= 500:
                raise BusyError('Too many recent presets. Please wait a few minutes.')
            self._jobs[identity], self._active = job, identity
            result = copy.deepcopy(job)

        def after_display(outcome):
            with self._lock:
                if job['status'] != 'loading':
                    return
                job['sharedOutputs'] = list(outcome.get('sharedOutputs') or [])
                if outcome.get('status') == 'succeeded':
                    job.update(status='actions', imageDisplayed=True, message='Finishing the preset…')
                else:
                    job['displayError'] = str(outcome.get('internalError') or '')[:1000]
                    message = str(outcome.get('message') or 'The image could not be displayed.')[:500]
                    self._finish(job, False, message + ' No extra actions were run.', completed)
                    return
            try:
                for index, action in enumerate(preset['actions']):
                    if not execute(copy.deepcopy(action)):
                        self._finish(job, False, f'The image is displayed, but “{action["label"]}” failed. Later actions were not run.', completed)
                        return
                    with self._lock:
                        job['actionsCompleted'] = index + 1
                self._finish(job, True, 'Preset applied successfully.', completed)
            except Exception:
                self._finish(job, False, 'The image is displayed, but the extra actions could not finish. Check the screen before trying again.', completed)

        try:
            display(after_display)
        except Exception:
            self._finish(job, False, 'The preset could not start. No extra actions were run.', completed)
            raise
        return self.get(identity) or result

    def _finish(self, job, success, message, completed):
        with self._lock:
            if job['status'] in ('succeeded', 'failed'):
                return
            job.update(status='succeeded' if success else 'failed', message=message)
            self._active = None
            result = copy.deepcopy(job)
        completed(result)
