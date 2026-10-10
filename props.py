"""Shared TDeck prop mappings and running order; never owns PP resources."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path

_LOCK = threading.RLock()


def target_uuid(value):
    """Validate external hyphenated UUIDs without rewriting their spelling."""
    if not isinstance(value, str):
        raise ValueError('Target must be a hyphenated UUID')
    identity = uuid.UUID(value)
    if str(identity) != value.lower():
        raise ValueError('Target must be a hyphenated UUID')
    return identity


def _validate_document(data):
    from catalog_organization import validate_organization
    if not isinstance(data, dict) or set(data) != {'version', 'revision', 'library', 'order', 'organization'}:
        raise ValueError('Invalid Props document')
    if type(data['version']) is not int or data['version'] not in (1, 2) or not isinstance(data['revision'], str):
        raise ValueError('Unsupported Props storage version')
    if data['revision'] != 'initial' and str(uuid.UUID(data['revision'])) != data['revision']:
        raise ValueError('Invalid revision')
    library, order = data['library'], data['order']
    if not isinstance(library, list) or len(library) > 500 or not isinstance(order, list) or len(order) > 500:
        raise ValueError('Props storage limits exceeded')
    ids, targets = set(), set()
    for item in library:
        fields = {'id', 'name', 'prop_uuid'}
        if data['version'] == 1:
            fields.add('macro_uuid')
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError('Invalid mapping')
        for key in fields - {'name'}:
            value = item[key]
            if key == 'macro_uuid' and value is None:
                continue
            if key == 'id':
                if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                    raise ValueError('Invalid UUID')
            else:
                target_uuid(value)
        if not isinstance(item['name'], str) or not item['name'].strip() or len(item['name']) > 120:
            raise ValueError('Invalid name')
        if item['id'] in ids or target_uuid(item['prop_uuid']) in targets:
            raise ValueError('Duplicate mapping')
        ids.add(item['id']); targets.add(target_uuid(item['prop_uuid']))
    if any(not isinstance(item, str) or item not in ids for item in order):
        raise ValueError('Dangling running order reference')
    normalized = copy.deepcopy(data)
    # Validate the complete legacy document before discarding retired references.
    # Read normalization never writes; only deliberate saves/imports persist v2.
    normalized['version'] = 2
    normalized['library'] = [{key: item[key] for key in ('id', 'name', 'prop_uuid')} for item in library]
    normalized['organization'] = validate_organization(data['organization'], ids)
    return normalized


class PropsConflict(ValueError):
    pass


class PropsStore:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        with _LOCK:
            if not self.path.exists():
                return {'version': 2, 'revision': 'initial', 'library': [], 'order': [],
                        'organization': {'folders': [], 'items': []}}
            try:
                if self.path.stat().st_size > 1024 * 1024:
                    raise ValueError('Storage size limit exceeded')
                data = json.loads(self.path.read_text(encoding='utf-8'))
                return _validate_document(data)
            except Exception as exc:
                raise OSError('Props storage is corrupt; restore a backup') from exc

    def import_document(self, document):
        document = _validate_document(document)
        with _LOCK:
            return self._save(document, None, overwrite=True)

    def _save(self, data, revision, *, overwrite=False):
        with _LOCK:
            if not overwrite and self.read()['revision'] != revision:
                raise PropsConflict('Props changed. Reload before saving or triggering.')
            data = copy.deepcopy(data)
            data['revision'] = str(uuid.uuid4())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.props-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(data, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return data

    def save_order(self, order, revision):
        with _LOCK:
            data = self.read()
            ids = {item['id'] for item in data['library']}
            if not isinstance(order, list) or len(order) > 500 or any(not isinstance(i, str) or i not in ids for i in order):
                raise ValueError('Order must contain at most 500 existing library IDs')
            data['order'] = list(order)
            return self._save(data, revision)

    def save_library(self, library, organization, revision):
        from catalog_organization import validate_organization
        with _LOCK:
            data = self.read()
            if not isinstance(library, list) or len(library) > 500:
                raise ValueError('Library must contain at most 500 mappings')
            normalized, ids, targets = [], set(), set()
            for item in library:
                if not isinstance(item, dict) or set(item) != {'id', 'name', 'prop_uuid'}:
                    raise ValueError('Invalid mapping fields')
                for key in ('id', 'prop_uuid'):
                    value = item[key]
                    if key == 'id':
                        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                            raise ValueError('Mapping IDs must be canonical UUIDs')
                    else:
                        target_uuid(value)
                if not isinstance(item['name'], str) or not item['name'].strip() or len(item['name']) > 120:
                    raise ValueError('Friendly name must contain 1–120 characters')
                if item['id'] in ids or target_uuid(item['prop_uuid']) in targets:
                    raise ValueError('One library entry per ProPresenter prop UUID')
                ids.add(item['id']); targets.add(target_uuid(item['prop_uuid']))
                normalized.append(dict(item))
            if any(identity not in ids for identity in data['order']):
                raise ValueError('Remove referenced presets from the order before deleting a mapping')
            data['library'] = normalized
            data['organization'] = validate_organization(organization, ids)
            return self._save(data, revision)


class PropsTriggerState:
    """Process-local command guard/history for one logical Props storage."""
    def __init__(self):
        self.lock = threading.Lock()
        self.last_triggered = None


class PropsService:
    """Endpoint-bound catalog/client with installation-wide command state."""
    def __init__(self, store, client_factory, *, trigger_state=None):
        from device_snapshot import SharedSnapshotCache
        self.store = store
        self.client_factory = client_factory
        self._trigger_state = trigger_state if trigger_state is not None else PropsTriggerState()
        self.lock = self._trigger_state.lock
        self.on_catalog = None
        self.catalog = SharedSnapshotCache(self._load_catalog, lambda: {'props': []},
                                          fresh_for=300, thread_name='props-catalog')

    @property
    def last_triggered(self):
        return self._trigger_state.last_triggered

    def _load_catalog(self):
        callback = self.on_catalog
        succeeded = False
        try:
            result = self._read_catalog()
            succeeded = True
            return result
        finally:
            if callback:
                callback(succeeded)

    def _read_catalog(self):
        client = self.client_factory()
        result = {}
        for kind in ('props',):
            raw = client.get_json(kind)
            if not isinstance(raw, list) or len(raw) > 10000:
                raise RuntimeError('ProPresenter catalog unavailable or invalid')
            entries, seen = [], set()
            for item in raw:
                identity = item.get('id') if isinstance(item, dict) else None
                if not isinstance(identity, dict):
                    raise RuntimeError('Invalid ProPresenter catalog identity')
                value = identity.get('uuid')
                parsed = target_uuid(value)
                if parsed in seen:
                    raise RuntimeError('Invalid or duplicate ProPresenter UUID')
                seen.add(parsed)
                entries.append({'uuid': value, 'name': str(identity.get('name') or value)[:120]})
            result[kind] = entries
        return result

    def state(self, refresh=False):
        data = self.store.read()
        catalog = self.catalog.get(force_refresh=refresh)
        if catalog['lastError']:
            catalog['stale'] = True
        targets = {target_uuid(entry['uuid']) for entry in catalog['props']}
        library = []
        for item in data['library']:
            available = bool(target_uuid(item['prop_uuid']) in targets and not catalog['lastError'] and not catalog['stale'])
            reason = None if available else ('Catalog unavailable or stale' if catalog['lastError'] or catalog['stale'] else 'Selected target missing')
            library.append({**item, 'available': available, 'unavailable_reason': reason})
        by_id = {item['id']: item for item in library}
        presets = [{**by_id[identity], 'position': position} for position, identity in enumerate(data['order'], 1)]
        return {**data, 'library': library, 'presets': presets, 'catalog': catalog,
                'use_macros': False, 'last_triggered': copy.deepcopy(self.last_triggered)}

    def trigger(self, position, revision):
        return self._trigger(position, revision, kind='preset')

    def trigger_library(self, identity, revision):
        return self._trigger(identity, revision, kind='library')

    def _trigger(self, selection, revision, *, kind):
        if not self.lock.acquire(blocking=False):
            raise PropsConflict('A prop trigger is already in progress; do not replay')
        try:
            with _LOCK:
                state = self.state()
                if state['revision'] != revision:
                    raise PropsConflict('Props changed. Reload before triggering.')
                position = selection if kind == 'preset' else None
                if kind == 'preset':
                    if type(position) is not int or position < 1 or position > len(state['presets']):
                        raise ValueError('Position is outside the running order')
                    preset = state['presets'][position - 1]
                else:
                    if not isinstance(selection, str):
                        raise ValueError('Select an existing saved library ID')
                    preset = next((item for item in state['library'] if item['id'] == selection), None)
                    if preset is None:
                        raise ValueError('Saved library mapping is missing or deleted')
                if not preset['available']:
                    raise ValueError(preset['unavailable_reason'])
                target = preset['prop_uuid']
            # The trigger guard still rejects second triggers, but local
            # reads and edits must not wait for hardware. The validated target is
            # captured above; later order edits cannot remap this command.
            if self.client_factory().get_command('prop/' + target + '/trigger') is None:
                raise RuntimeError('ProPresenter trigger failed or outcome unknown. Do not automatically retry.')
            from datetime import datetime, timezone
            self._trigger_state.last_triggered = {'position': position, 'id': preset['id'], 'name': preset['name'],
                                   'revision': revision, 'kind': kind, 'mode': 'prop', 'at': datetime.now(timezone.utc).isoformat()}
            return copy.deepcopy(self.last_triggered)
        finally:
            self.lock.release()
