"""Replacement-only extension of the marked, hardware-blocked Media fixture.

Run with the same Python environment as media_ui_harness.py, on port 5063.
The shared fixture is not modified. Missing assets can only be seeded by creating
an explicitly marked synthetic entry, never by accepting a caller's image ID.
"""
from __future__ import annotations

import io
import sys
import threading
from functools import wraps
from pathlib import Path
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_ui_harness as fixture


MARKER = 'tdeck-media-replace-ui'
_original_run = Flask.run
_original_add_rule = Flask.add_url_rule
_replacement_views = {}


def capture_rule(self, rule, endpoint=None, view_func=None, **options):
    if rule.startswith('/api/media/') and rule.endswith('/replace'):
        _replacement_views[endpoint or view_func.__name__] = view_func
    return _original_add_rule(self, rule, endpoint, view_func, **options)


def run_fixture(self, *args, **kwargs):
    import webui

    # Hold an actual server response, not a replay of browser multipart data.
    # WebKit cannot reliably replay uploaded file bytes through route.fetch().
    gate_lock = threading.Lock()
    gate = {'image_id': None, 'arrived': threading.Event(), 'release': threading.Event()}

    def gated_view(view):
        @wraps(view)
        def wrapped(**kwargs):
            response = self.make_response(view(**kwargs))
            with gate_lock:
                held = gate['image_id'] == kwargs.get('media_id') and response.status_code == 200
                arrived, release = gate['arrived'], gate['release']
            if held:
                arrived.set()
                release.wait(20)  # Bounded even if the browser assertion fails.
            return response
        return wrapped

    # Restore only the captured replacement view; all device guards stay intact.
    self.view_functions.update({name: gated_view(view) for name, view in _replacement_views.items()})

    @self.post('/__media_fixture__/replace/hold')
    def hold_response():
        body = webui.request.get_json(silent=True) or {}
        if body.get('fixture') != MARKER:
            return webui.jsonify({'ok': False}), 400
        try:
            item = webui._get_media_library().get(body.get('id'))
        except (KeyError, TypeError):
            return webui.jsonify({'ok': False}), 400
        with gate_lock:
            gate['release'].set()
            gate.update(image_id=item['id'], arrived=threading.Event(), release=threading.Event())
        return webui.jsonify({'ok': True, 'fixture': MARKER})

    @self.get('/__media_fixture__/replace/held')
    def response_is_held():
        with gate_lock:
            waiting = gate['arrived'].is_set() and not gate['release'].is_set()
        return webui.jsonify({'fixture': MARKER, 'waiting': waiting})

    @self.post('/__media_fixture__/replace/release')
    def release_response():
        body = webui.request.get_json(silent=True) or {}
        if body.get('fixture') != MARKER:
            return webui.jsonify({'ok': False}), 400
        with gate_lock:
            gate['release'].set()
            gate['image_id'] = None
        return webui.jsonify({'ok': True, 'fixture': MARKER})

    @self.post('/__media_fixture__/replace/seed')
    def seed_missing():
        body = webui.request.get_json(silent=True) or {}
        if body.get('fixture') != MARKER or body.get('missing') not in ('none', 'file', 'thumbnail', 'both'):
            return webui.jsonify({'ok': False}), 400
        library = webui._get_media_library()
        item = library.upload(io.BytesIO(fixture.fixture_image('SYNTHETIC REPAIR', '#365070')),
                              'synthetic-repair.png', 'Synthetic repair ' + body['missing'],
                              uploaded_by='Synthetic fixture uploader', uploaded_by_id='synthetic-uploader-12345')
        library.update(item['id'], item['name'], True)
        for kind, folder in (('file', 'images'), ('thumbnail', 'thumbnails')):
            if body['missing'] in (kind, 'both'):
                (library.root / folder / (item['id'] + '.png')).unlink()
        return webui.jsonify({'ok': True, 'fixture': MARKER, 'id': item['id']})

    @self.get('/__media_fixture__/replace/health')
    def replacement_health():
        return webui.jsonify({'fixture': MARKER, 'isolated': True})

    # All browser projects share the auth-disabled loopback principal. Raise only
    # this isolated fixture's attempt budget; keep real admission and its single
    # upload semaphore intact. The production limit is restored when run exits.
    with patch.object(webui, '_MEDIA_UPLOADS_PER_MINUTE', 1000):
        return _original_run(self, *args, **kwargs)


if __name__ == '__main__':
    with patch.object(Flask, 'add_url_rule', capture_rule), patch.object(Flask, 'run', run_fixture):
        fixture.main()
