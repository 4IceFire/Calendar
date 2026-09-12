from flask import Flask, render_template, jsonify, request, redirect, url_for, session, abort, send_file, send_from_directory, Response, has_request_context, g
import copy
import fnmatch
import gzip
import hashlib
import io
import logging
import math
import os
import threading
import time
from pathlib import Path
import shutil
import sys
from collections import deque
from functools import wraps
import sqlite3
import secrets
import uuid
from typing import Any
import zipfile

from api_security import (
    SERVICE_TOKEN_SCOPES,
    authenticate_service_token,
    create_service_token,
    ensure_service_token_schema,
    list_service_tokens,
    revoke_service_token,
    rotate_service_token,
    token_allows,
)

from werkzeug.serving import make_server
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
import json
import re
from datetime import datetime, timedelta
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit

from package.json_cache import read_json, write_json

from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from werkzeug.security import check_password_hash, generate_password_hash

# --- Home overview state (best-effort, in-memory) ---
_home_overview_lock = threading.Lock()
_home_overview_cache_lock = threading.Lock()
_home_last_timer_preset: dict = {'preset': None, 'name': None, 'time': None, 'ts': None}
_home_last_videohub_preset: dict = {'id': None, 'ts': None}
_home_last_videohub_route: dict = {'output': None, 'input': None, 'monitor': None, 'ts': None}
_home_overview_cache: dict = {'stamp': None, 'payload': None}


def _home_state_path() -> Path:
    """Store lightweight dashboard state.

    Prefer /data when present (Docker volume), otherwise fall back to alongside auth.db.
    """
    try:
        data_dir = Path('/data')
        if data_dir.exists() and data_dir.is_dir():
            return data_dir / 'home_state.json'
    except Exception:
        pass

    try:
        return _AUTH_DB_PATH.with_name('home_state.json')
    except Exception:
        return Path(__file__).resolve().parent / 'home_state.json'


def _home_state_load() -> dict:
    p = _home_state_path()
    try:
        data, _ = read_json(p, default_factory=dict, create_if_missing=False)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _home_state_save(payload: dict) -> None:
    p = _home_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        write_json(p, payload)
    except Exception:
        try:
            p.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        except Exception:
            pass


def _home_state_sync_from_disk() -> None:
    """Best-effort sync disk state into memory (for multi-process / restart safety)."""
    st = _home_state_load()
    if not isinstance(st, dict):
        return
    with _home_overview_lock:
        try:
            t = st.get('last_timer_preset')
            if isinstance(t, dict):
                for k in ('preset', 'name', 'time', 'ts'):
                    if k in t:
                        _home_last_timer_preset[k] = t.get(k)
        except Exception:
            pass

        try:
            v = st.get('last_videohub_preset')
            if isinstance(v, dict):
                for k in ('id', 'ts'):
                    if k in v:
                        _home_last_videohub_preset[k] = v.get(k)
        except Exception:
            pass

        try:
            r = st.get('last_videohub_route')
            if isinstance(r, dict):
                for k in ('output', 'input', 'monitor', 'ts'):
                    if k in r:
                        _home_last_videohub_route[k] = r.get(k)
        except Exception:
            pass


def _home_state_persist() -> None:
    """Persist current in-memory Home overview state (best-effort)."""
    try:
        _home_state_save({
            'last_timer_preset': dict(_home_last_timer_preset),
            'last_videohub_preset': dict(_home_last_videohub_preset),
            'last_videohub_route': dict(_home_last_videohub_route),
        })
    except Exception:
        pass


# Seed from disk on startup so Home can show prior state.
try:
    _home_state_sync_from_disk()
except Exception:
    pass


def _home_set_last_timer_preset(*, preset_number: int, selected) -> None:
    try:
        n = int(preset_number)
    except Exception:
        return
    if n <= 0:
        return

    name = None
    time_str = None
    try:
        if isinstance(selected, dict):
            name = str(selected.get('name', '')).strip() or None
            time_str = str(selected.get('time', '')).strip() or None
        else:
            time_str = str(selected).strip() or None
            name = time_str
    except Exception:
        pass

    with _home_overview_lock:
        _home_last_timer_preset['preset'] = n
        _home_last_timer_preset['name'] = name
        _home_last_timer_preset['time'] = time_str
        _home_last_timer_preset['ts'] = time.time()

        _home_state_persist()


def _home_set_last_videohub_preset(*, preset_id: int) -> None:
    try:
        pid = int(preset_id)
    except Exception:
        return
    if pid <= 0:
        return
    with _home_overview_lock:
        _home_last_videohub_preset['id'] = pid
        _home_last_videohub_preset['ts'] = time.time()

        _home_state_persist()


def _home_set_last_videohub_route(*, output: int, input_: int, monitor: bool) -> None:
    try:
        out_n = int(output)
        in_n = int(input_)
    except Exception:
        return
    if out_n <= 0 or in_n <= 0:
        return
    with _home_overview_lock:
        _home_last_videohub_route['output'] = out_n
        _home_last_videohub_route['input'] = in_n
        _home_last_videohub_route['monitor'] = bool(monitor)
        _home_last_videohub_route['ts'] = time.time()

        _home_state_persist()

from package.core import list_apps, get_app
try:
    from package.apps.calendar import utils
except Exception:
    # Fallback lightweight utils if importing the calendar utils fails (e.g., missing companion/requests)
    import json

    _STUB_DEFAULTS = {
        "EVENTS_FILE": "events.json",
        "companion_ip": "127.0.0.1",
        "companion_port": 8888,
        "companion_timer_name": "timer_name_",
        "propresenter_ip": "127.0.0.1",
        "propresenter_port": 4000,
        "propresenter_timer_index": 2,
        "propresenter_is_latest": True,
        "propresenter_timer_wait_stop_ms": 200,
        "propresenter_timer_wait_set_ms": 600,
        "propresenter_timer_wait_reset_ms": 1000,
        "stream_start_preset": 0,
        "videohub_ip": "172.20.10.11",
        "videohub_port": 9990,
        "videohub_timeout": 2,
        "videohub_presets_file": "videohub_presets.json",
        "digico_enabled": False,
        "digico_ip": "",
        "digico_port": 9000,
        "digico_listen_address": "0.0.0.0",
        "digico_listen_port": 8000,
        "digico_request_interval": 0.1,
        "digico_retry_interval": 1.0,
        "digico_stale_after": 10.0,
        "digico_auxes": [],
        "digico_channels": [],
        "digico_external_devices": [],
        "hisense_enabled": False,
        "hisense_cert_path": "hisense_certs/vidaa_client.pem",
        "hisense_key_path": "hisense_certs/vidaa_client.key",
        "hisense_poll_interval": 10,
        "hisense_reconnect_interval": 15,
        "hisense_compatible_models": "Confirmed: 55A7G, 65A7G",
        "hisense_certificate_profiles": [],
        "hisense_tv_groups": [],
        "hisense_tvs": [],
        "pixie_network_mode": "disabled",
        "pixie_gateway_host": "",
        "pixie_home_id": "",
        "pixie_home_name": "",
        "pixie_net_id": "",
        "pixie_mesh_net": "",
        "pixie_mesh_net_2": "",
        "pixie_auditoriums": [],
        "pixie_devices": [],
        "pixie_scenes": [],
        "atem_ip": "127.0.0.1",
        "atem_port": 9910,
        "atem_timeout": 3,
        "webserver_port": 5000,
        "poll_interval": 1,
        "debug": False,
        "dark_mode": True,

        # Web UI message/alert auto-hide timeout
        # 0 disables auto-hide.
        "webui_message_timeout_seconds": 4,

        # Auth (Web UI pages only)
        "auth_enabled": True,
        "auth_idle_timeout_enabled": True,
        "auth_idle_timeout_minutes": 2,
        "auth_min_password_length": 6,
        "auth_lockout_failed_attempts": 5,

        # Scheduler/internal API
        "internal_api_timeout_seconds": 10,

    }

    def _seed_config_if_missing() -> dict:
        try:
            with open('config.json', 'w', encoding='utf-8') as out:
                json.dump(_STUB_DEFAULTS, out, indent=2)
        except Exception:
            pass
        return dict(_STUB_DEFAULTS)

    def _upgrade_cfg_if_needed(cfg: dict) -> dict:
        changed = False
        for k, v in _STUB_DEFAULTS.items():
            if k not in cfg:
                cfg[k] = v
                changed = True
        if changed:
            try:
                with open('config.json', 'w', encoding='utf-8') as out:
                    json.dump(cfg, out, indent=2)
            except Exception:
                pass
        return cfg

    def _load_cfg():
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                cfg = json.load(f) or {}
            if not isinstance(cfg, dict):
                return _seed_config_if_missing()
            return _upgrade_cfg_if_needed(cfg)
        except FileNotFoundError:
            return _seed_config_if_missing()
        except json.JSONDecodeError:
            return _seed_config_if_missing()
        except Exception:
            return {}

    class _StubUtils:
        def get_config(self):
            return _load_cfg()

        def reload_config(self, force: bool = False):
            # no-op
            return False

        def save_config(self, cfg):
            try:
                with open('config.json', 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass

        def get_companion(self):
            return None

    utils = _StubUtils()

app = Flask(__name__, template_folder='templates', static_folder='static')

_BUILD_ID_LOCK = threading.Lock()
_BUILD_ID_CACHE: str | None = None


def _build_id() -> str:
    """Return a non-secret release identifier for diagnostics and cache tracing."""
    configured = str(os.environ.get('TDECK_BUILD_ID') or '').strip()
    if configured:
        cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', configured).strip('-')
        if cleaned:
            return cleaned[:80]

    global _BUILD_ID_CACHE
    with _BUILD_ID_LOCK:
        if _BUILD_ID_CACHE:
            return _BUILD_ID_CACHE
        digest = hashlib.sha256()
        root = Path(__file__).resolve().parent
        for relative in ('webui.py', 'templates/base.html', 'static/app.js', 'static/client_telemetry.js'):
            try:
                digest.update(relative.encode('utf-8'))
                digest.update((root / relative).read_bytes())
            except Exception:
                continue
        _BUILD_ID_CACHE = f'local-{digest.hexdigest()[:12]}'
        return _BUILD_ID_CACHE

_COMPRESSIBLE_MIMETYPES = {
    'application/javascript',
    'application/json',
    'application/xml',
    'image/svg+xml',
    'text/css',
    'text/html',
    'text/javascript',
    'text/plain',
    'text/xml',
}

_STATIC_ASSET_MANIFEST_LOCK = threading.RLock()
_STATIC_ASSET_MANIFEST: dict[str, str] = {}
_STATIC_ASSET_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')


def _resolve_static_asset(filename: str) -> tuple[str, Path] | None:
    """Resolve a logical asset name without allowing it outside static/."""
    asset_name = str(filename or '').strip().replace('\\', '/')
    asset_name = asset_name.lstrip('/')
    parts = asset_name.split('/')
    if not asset_name or any(part in ('', '.', '..') for part in parts):
        return None
    try:
        static_root = Path(str(app.static_folder or 'static')).resolve()
        asset_path = (static_root / asset_name).resolve()
        asset_path.relative_to(static_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return asset_name, asset_path


def _static_asset_digest(filename: str) -> tuple[str, str] | None:
    """Return the logical name and SHA-256 of its current bytes.

    Hashing the bytes, rather than trusting timestamps, makes the generated URL
    a content identity. The small in-memory manifest is useful for diagnostics,
    while deliberately recomputing prevents same-size/timestamp deployments from
    retaining a stale URL.
    """
    resolved = _resolve_static_asset(filename)
    if resolved is None:
        return None
    asset_name, asset_path = resolved
    try:
        if not asset_path.is_file():
            return None
        hasher = hashlib.sha256()
        with asset_path.open('rb') as asset_stream:
            for chunk in iter(lambda: asset_stream.read(1024 * 1024), b''):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    except OSError:
        return None
    with _STATIC_ASSET_MANIFEST_LOCK:
        _STATIC_ASSET_MANIFEST[asset_name] = digest
    return asset_name, digest


@app.template_global()
def static_asset(filename: str) -> str:
    """Return a content-addressed URL for a bundled static asset.

    Invalid names collapse to the static root and missing files remain ordinary,
    revalidated 404 URLs; neither can be made immutable accidentally.
    """
    digest_entry = _static_asset_digest(filename)
    if digest_entry is not None:
        asset_name, digest = digest_entry
        return url_for('static', filename=asset_name, v=digest)
    resolved = _resolve_static_asset(filename)
    safe_name = resolved[0] if resolved is not None else ''
    return url_for('static', filename=safe_name)


def _request_has_current_static_digest() -> bool:
    if request.endpoint != 'static':
        return False
    requested_digest = str(request.args.get('v') or '').strip().lower()
    if not _STATIC_ASSET_DIGEST_RE.fullmatch(requested_digest):
        return False
    filename = str((request.view_args or {}).get('filename') or '')
    digest_entry = _static_asset_digest(filename)
    return bool(digest_entry and secrets.compare_digest(digest_entry[1], requested_digest))


@app.after_request
def _optimize_web_response(response):
    """Apply safe cache policies and compress text for slower clients."""
    try:
        request_path = request.path or ''
        is_static = request.endpoint == 'static' or request_path.startswith('/static/')
        if is_static and response.status_code in (200, 304):
            if _request_has_current_static_digest():
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            else:
                response.headers['Cache-Control'] = 'public, no-cache, max-age=0, must-revalidate'
        elif request_path.startswith('/api/'):
            # API state and live-control results must never be replayed from a
            # browser or intermediary cache.
            response.headers['Cache-Control'] = 'no-store'
        elif response.mimetype == 'text/html':
            # HTML can contain per-user navigation and CSRF/session state. Allow
            # browser history while requiring validation before reuse.
            response.headers['Cache-Control'] = 'private, no-cache, max-age=0, must-revalidate'
        elif (
            request_path.startswith('/config/')
            and 'attachment' in str(response.headers.get('Content-Disposition') or '').lower()
        ):
            response.headers['Cache-Control'] = 'private, no-store'

        accepts_gzip = request.accept_encodings['gzip'] > 0
        content_encoding = str(response.headers.get('Content-Encoding') or '').strip()
        cache_control = str(response.headers.get('Cache-Control') or '').lower()
        if response.status_code in (200, 304) and response.mimetype in _COMPRESSIBLE_MIMETYPES:
            response.headers['Vary'] = _append_vary_header(
                response.headers.get('Vary'),
                'Accept-Encoding',
            )
        should_compress = (
            accepts_gzip
            and not content_encoding
            and request.method != 'HEAD'
            and response.status_code == 200
            and 'no-transform' not in cache_control
            and response.mimetype in _COMPRESSIBLE_MIMETYPES
        )
        streamed_body = None
        if should_compress and response.direct_passthrough:
            if (request.path or '').startswith('/static/'):
                streamed_body = response.response
                response.direct_passthrough = False
            else:
                should_compress = False
        if should_compress:
            try:
                payload = response.get_data()
            finally:
                if streamed_body is not None and hasattr(streamed_body, 'close'):
                    streamed_body.close()
            if len(payload) >= 1024:
                compressed = gzip.compress(payload, compresslevel=6)
                if len(compressed) < len(payload):
                    response.set_data(compressed)
                    response.headers['Content-Encoding'] = 'gzip'
                    response.headers['Vary'] = _append_vary_header(
                        response.headers.get('Vary'),
                        'Accept-Encoding',
                    )
                    etag, _weak = response.get_etag()
                    if etag:
                        response.set_etag(etag, weak=True)
    except Exception:
        # Response optimization must never prevent the actual page/API response.
        pass
    return response


def _append_vary_header(current: str | None, value: str) -> str:
    values = [part.strip() for part in str(current or '').split(',') if part.strip()]
    if value.lower() not in {part.lower() for part in values}:
        values.append(value)
    return ', '.join(values)


def _auth_cfg() -> dict:
    try:
        return utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        return {}


def _auth_enabled() -> bool:
    cfg = _auth_cfg()
    try:
        return bool(cfg.get('auth_enabled', False))
    except Exception:
        return False


def _ensure_secret_key() -> None:
    """Ensure a stable Flask secret key exists for secure sessions."""
    try:
        cfg = _auth_cfg()
    except Exception:
        cfg = {}

    key = str(cfg.get('flask_secret_key') or '').strip()
    if not key:
        key = secrets.token_hex(32)
        try:
            cfg['flask_secret_key'] = key
            if hasattr(utils, 'save_config'):
                utils.save_config(cfg)
        except Exception:
            pass
    try:
        app.secret_key = key
    except Exception:
        pass


_ensure_secret_key()

# Cookie hardening (still helpful on LAN)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)


# --- Auth DB (SQLite) ---
_AUTH_DB_PATH = (Path(__file__).resolve().parent / 'auth.db')
_APP_ROOT = Path(__file__).resolve().parent
_COMPANION_SURFACES_PATH = _APP_ROOT / 'companion_surfaces.json'
_COMPANION_SURFACE_LAYOUTS: dict[str, tuple[int, int]] = {
    '2x5': (2, 5),
    '3x5': (3, 5),
    '4x5': (4, 5),
    '2x4': (2, 4),
    '3x4': (3, 4),
    '4x4': (4, 4),
    '4x8': (4, 8),
}
_COMPANION_SURFACE_DEFAULT_LAYOUT = '3x5'
_COMPANION_SURFACE_CELL_PX = 110
_COMPANION_SURFACE_GUTTER_PX = 10


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_AUTH_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init_auth_db() -> None:
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS roles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE
            )
            """
        )
        # Legacy per-role VideoHub allow-lists, kept so older auth.db files can migrate.
        try:
            cols = [str(r['name']) for r in conn.execute('PRAGMA table_info(roles)').fetchall()]
        except Exception:
            cols = []
        # Legacy per-role idle timeout override. NULL => inherit; 0 => disable.
        if 'auth_idle_timeout_minutes_override' not in cols:
            try:
                conn.execute('ALTER TABLE roles ADD COLUMN auth_idle_timeout_minutes_override INTEGER')
            except Exception:
                pass
        if 'videohub_allowed_outputs' not in cols:
            try:
                conn.execute('ALTER TABLE roles ADD COLUMN videohub_allowed_outputs TEXT')
            except Exception:
                pass
        if 'videohub_allowed_inputs' not in cols:
            try:
                conn.execute('ALTER TABLE roles ADD COLUMN videohub_allowed_inputs TEXT')
            except Exception:
                pass
        if 'videohub_allowed_presets' not in cols:
            try:
                conn.execute('ALTER TABLE roles ADD COLUMN videohub_allowed_presets TEXT')
            except Exception:
                pass
        if 'videohub_can_edit_presets' not in cols:
            try:
                conn.execute('ALTER TABLE roles ADD COLUMN videohub_can_edit_presets INTEGER')
            except Exception:
                pass
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              role_id INTEGER,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT,
              updated_at TEXT,
              FOREIGN KEY(role_id) REFERENCES roles(id)
            )
            """
        )
        try:
            user_cols = [str(r['name']) for r in conn.execute('PRAGMA table_info(users)').fetchall()]
        except Exception:
            user_cols = []
        for col_name, col_type in (
            ('email', 'TEXT'),
            ('full_name', 'TEXT'),
            ('is_locked', 'INTEGER NOT NULL DEFAULT 0'),
            ('locked_at', 'TEXT'),
            ('locked_reason', 'TEXT'),
            ('failed_login_count', 'INTEGER NOT NULL DEFAULT 0'),
            ('last_failed_login_at', 'TEXT'),
            ('force_password_change', 'INTEGER NOT NULL DEFAULT 0'),
            ('password_changed_at', 'TEXT'),
            ('last_login_at', 'TEXT'),
            ('created_by', 'INTEGER'),
            ('updated_by', 'INTEGER'),
            ('session_version', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            if col_name not in user_cols:
                try:
                    conn.execute(f'ALTER TABLE users ADD COLUMN {col_name} {col_type}')
                except Exception:
                    pass
        try:
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lower ON users(lower(username))')
        except Exception:
            pass
        try:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower_nonblank
                ON users(lower(email))
                WHERE email IS NOT NULL AND trim(email) != ''
                """
            )
        except Exception:
            pass
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              is_system INTEGER NOT NULL DEFAULT 0,
              is_admin INTEGER NOT NULL DEFAULT 0,
              auth_idle_timeout_minutes_override INTEGER,
              videohub_allowed_outputs TEXT,
              videohub_allowed_inputs TEXT,
              videohub_allowed_presets TEXT,
              videohub_can_edit_presets INTEGER,
              companion_click_surfaces TEXT,
              digico_allowed_auxes TEXT,
              pixie_allowed_auditoriums TEXT,
              pixie_allowed_devices TEXT,
              pixie_allowed_scenes TEXT,
              atem_allowed_audio_sources TEXT,
              atem_can_solo_audio INTEGER,
              atem_can_monitor_audio INTEGER
            )
            """
        )
        try:
            group_cols = [str(r['name']) for r in conn.execute('PRAGMA table_info(groups)').fetchall()]
        except Exception:
            group_cols = []
        for col_name, col_type in (
            ('is_system', 'INTEGER NOT NULL DEFAULT 0'),
            ('is_admin', 'INTEGER NOT NULL DEFAULT 0'),
            ('auth_idle_timeout_minutes_override', 'INTEGER'),
            ('videohub_allowed_outputs', 'TEXT'),
            ('videohub_allowed_inputs', 'TEXT'),
            ('videohub_allowed_presets', 'TEXT'),
            ('videohub_can_edit_presets', 'INTEGER'),
            ('companion_click_surfaces', 'TEXT'),
            ('digico_allowed_auxes', 'TEXT'),
            ('pixie_allowed_auditoriums', 'TEXT'),
            ('pixie_allowed_devices', 'TEXT'),
            ('pixie_allowed_scenes', 'TEXT'),
            ('atem_allowed_audio_sources', 'TEXT'),
            ('atem_can_solo_audio', 'INTEGER'),
            ('atem_can_monitor_audio', 'INTEGER'),
        ):
            if col_name not in group_cols:
                try:
                    conn.execute(f'ALTER TABLE groups ADD COLUMN {col_name} {col_type}')
                except Exception:
                    pass
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS group_pages (
              group_id INTEGER NOT NULL,
              page_key TEXT NOT NULL,
              UNIQUE(group_id, page_key),
              FOREIGN KEY(group_id) REFERENCES groups(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_groups (
              user_id INTEGER NOT NULL,
              group_id INTEGER NOT NULL,
              UNIQUE(user_id, group_id),
              FOREIGN KEY(user_id) REFERENCES users(id),
              FOREIGN KEY(group_id) REFERENCES groups(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS role_pages (
              role_id INTEGER NOT NULL,
              page_key TEXT NOT NULL,
              UNIQUE(role_id, page_key),
              FOREIGN KEY(role_id) REFERENCES roles(id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              user_id INTEGER,
              username TEXT,
              action TEXT NOT NULL,
              detail TEXT,
              ip TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              actor_user_id INTEGER,
              actor_username TEXT,
              actor_display TEXT,
              source TEXT NOT NULL DEFAULT 'system',
              action TEXT NOT NULL,
              target_type TEXT,
              target_id TEXT,
              status TEXT NOT NULL DEFAULT 'info',
              summary TEXT,
              details_json TEXT,
              ip TEXT,
              request_path TEXT
            )
            """
        )
        try:
            activity_cols = [str(r['name']) for r in conn.execute('PRAGMA table_info(activity_log)').fetchall()]
        except Exception:
            activity_cols = []
        for col_name, col_type in (
            ('actor_display', 'TEXT'),
            ('source', "TEXT NOT NULL DEFAULT 'system'"),
            ('target_type', 'TEXT'),
            ('target_id', 'TEXT'),
            ('status', "TEXT NOT NULL DEFAULT 'info'"),
            ('summary', 'TEXT'),
            ('details_json', 'TEXT'),
            ('request_path', 'TEXT'),
        ):
            if col_name not in activity_cols:
                try:
                    conn.execute(f'ALTER TABLE activity_log ADD COLUMN {col_name} {col_type}')
                except Exception:
                    pass
        try:
            conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_log_ts_id ON activity_log(ts DESC, id DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_log_actor ON activity_log(actor_user_id, id DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_log_action ON activity_log(action, id DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_log_status ON activity_log(status, id DESC)')
        except Exception:
            pass
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_log_ack (
              actor_user_id INTEGER PRIMARY KEY,
              acknowledged_activity_id INTEGER NOT NULL DEFAULT 0,
              acknowledged_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_meta (
              key TEXT PRIMARY KEY,
              value TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              revoked_at TEXT,
              ip TEXT,
              user_agent TEXT,
              session_version INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        migrated_row = conn.execute(
            'SELECT value FROM auth_meta WHERE key=?',
            ('audit_to_activity_log_migrated',),
        ).fetchone()
        migrated = str(migrated_row['value']) if migrated_row and migrated_row['value'] is not None else None
        if migrated != '1':
            try:
                rows = conn.execute(
                    """
                    SELECT id,ts,user_id,username,action,detail,ip
                    FROM audit
                    ORDER BY id ASC
                    """
                ).fetchall()
                for row in rows:
                    details = {'legacy_audit_id': int(row['id'])}
                    if row['detail'] is not None:
                        details['detail'] = str(row['detail'])
                    fallback_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    conn.execute(
                        """
                        INSERT INTO activity_log(
                          ts,actor_user_id,actor_username,actor_display,source,action,
                          status,summary,details_json,ip
                        )
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            str(row['ts'] or fallback_ts),
                            int(row['user_id']) if row['user_id'] is not None else None,
                            str(row['username'] or '') or None,
                            str(row['username'] or '') or 'System',
                            'legacy',
                            str(row['action'] or ''),
                            'info',
                            str(row['action'] or '').replace('_', ' '),
                            json.dumps(details, ensure_ascii=False),
                            str(row['ip'] or '') or None,
                        ),
                    )
                conn.execute(
                    'INSERT INTO auth_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    ('audit_to_activity_log_migrated', '1'),
                )
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    ensure_service_token_schema(_AUTH_DB_PATH)


def _auth_meta_get(key: str) -> str | None:
    conn = _db()
    try:
        row = conn.execute('SELECT value FROM auth_meta WHERE key=?', (str(key),)).fetchone()
        return str(row['value']) if row and row['value'] is not None else None
    finally:
        conn.close()


def _auth_meta_set(key: str, value: str) -> None:
    conn = _db()
    try:
        conn.execute(
            'INSERT INTO auth_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (str(key), str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def _coerce_allow_list(v):
    """Coerce stored allow-list value into a sorted unique list of positive ints."""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        # try JSON list first
        try:
            v = json.loads(s)
        except Exception:
            # fallback: comma/space-separated
            parts = [p.strip() for p in s.replace('\n', ',').replace('\t', ',').split(',')]
            nums = []
            for p in parts:
                if not p:
                    continue
                try:
                    n = int(p)
                except Exception:
                    continue
                if n > 0:
                    nums.append(n)
            return sorted(set(nums))
    if isinstance(v, int):
        return [v] if v > 0 else []
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        try:
            n = int(item)
        except Exception:
            continue
        if n > 0:
            out.append(n)
    return sorted(set(out))


def _parse_group_allowlist_field(raw: str | None) -> list[int]:
    """Parse a group allow-list input.

    Semantics:
      - Blank/NULL => allow all
      - "all" or "*" => allow all
      - JSON list or comma-separated => allow only those numbers
    """
    if raw is None:
        return []
    s = str(raw).strip()
    if not s:
        return []
    if s.lower() in ('all', '*', 'inherit', 'default', 'global'):
        # Backward-friendly aliases; all mean "allow all" now.
        return []
    return _coerce_allow_list(s)


def _set_group_videohub_allowlists(group_id: int, outputs_raw: str | None, inputs_raw: str | None) -> None:
    outs = _parse_group_allowlist_field(outputs_raw)
    ins = _parse_group_allowlist_field(inputs_raw)
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET videohub_allowed_outputs=?, videohub_allowed_inputs=? WHERE id=?',
            (
                json.dumps(outs),
                json.dumps(ins),
                int(group_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _set_group_videohub_allowed_preset_ids(group_id: int, presets_raw: str | None) -> None:
    preset_ids = _parse_group_allowlist_field(presets_raw)
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET videohub_allowed_presets=? WHERE id=?',
            (
                json.dumps(preset_ids),
                int(group_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _set_group_videohub_can_edit_presets(group_id: int, enabled: bool) -> None:
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET videohub_can_edit_presets=? WHERE id=?',
            (
                1 if bool(enabled) else 0,
                int(group_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _coerce_string_allow_list(v) -> list[str]:
    """Coerce stored string allow-list values into sorted unique non-empty IDs."""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            v = json.loads(s)
        except Exception:
            return sorted(set([p.strip() for p in re.split(r'[\s,]+', s) if p.strip()]))
    if isinstance(v, (int, float)):
        return [str(v)]
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        sid = str(item or '').strip()
        if sid:
            out.append(sid)
    return sorted(set(out))


def _set_group_companion_click_surfaces(group_id: int, surface_ids) -> None:
    surface_ids = _coerce_string_allow_list(surface_ids)
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET companion_click_surfaces=? WHERE id=?',
            (
                json.dumps(surface_ids),
                int(group_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _set_group_digico_allowed_auxes(group_id: int, aux_ids) -> None:
    """Store a group's Personal Mix AUX allow-list; blank means all AUXes."""
    aux_ids = _coerce_string_allow_list(aux_ids)
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET digico_allowed_auxes=? WHERE id=?',
            (json.dumps(aux_ids), int(group_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _coerce_pixie_device_permissions(value, allowed_auditoriums: list[str] | None = None) -> dict[str, str | list[str]]:
    """Normalize per-auditorium Pixie device permissions.

    ``"*"`` means all present and future devices in that auditorium.  Explicit
    lists stay scoped to the same group that grants the auditorium, preventing
    permissions from unrelated groups from combining accidentally.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = {}
    if not isinstance(value, dict):
        value = {}
    allowed = set(_coerce_string_allow_list(allowed_auditoriums)) if allowed_auditoriums is not None else None
    result: dict[str, str | list[str]] = {}
    for raw_auditorium_id, raw_devices in value.items():
        auditorium_id = str(raw_auditorium_id or '').strip()
        if not auditorium_id or (allowed is not None and auditorium_id not in allowed):
            continue
        if raw_devices in ('*', 'all', True, None):
            result[auditorium_id] = '*'
        else:
            result[auditorium_id] = _coerce_string_allow_list(raw_devices)
    if allowed is not None:
        for auditorium_id in allowed:
            result.setdefault(auditorium_id, '*')
    return result


def _set_group_pixie_permissions(group_id: int, auditorium_ids, device_permissions, scene_ids) -> None:
    auditorium_ids = _coerce_string_allow_list(auditorium_ids)
    scene_ids = _coerce_string_allow_list(scene_ids)
    device_permissions = _coerce_pixie_device_permissions(device_permissions, auditorium_ids)
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET pixie_allowed_auditoriums=?,pixie_allowed_devices=?,pixie_allowed_scenes=? WHERE id=?',
            (
                json.dumps(auditorium_ids),
                json.dumps(device_permissions, sort_keys=True),
                json.dumps(scene_ids),
                int(group_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _set_group_atem_audio_sources(group_id: int, source_ids) -> None:
    source_ids = _coerce_string_allow_list(source_ids)
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET atem_allowed_audio_sources=? WHERE id=?',
            (
                json.dumps(source_ids),
                int(group_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _set_group_atem_can_solo_audio(group_id: int, enabled: bool) -> None:
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET atem_can_solo_audio=? WHERE id=?',
            (
                1 if bool(enabled) else 0,
                int(group_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _set_group_atem_can_monitor_audio(group_id: int, enabled: bool) -> None:
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET atem_can_monitor_audio=? WHERE id=?',
            (
                1 if bool(enabled) else 0,
                int(group_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _set_group_pages(group_id: int, page_keys: list[str]) -> None:
    group_id = int(group_id)
    keys = [k for k in (page_keys or []) if str(k or '').strip()]
    conn = _db()
    try:
        conn.execute('DELETE FROM group_pages WHERE group_id=?', (group_id,))
        for k in keys:
            conn.execute('INSERT OR IGNORE INTO group_pages(group_id,page_key) VALUES (?,?)', (group_id, str(k)))
        conn.commit()
    finally:
        conn.close()


def _group_settings_snapshot(group_id: int) -> dict:
    gid = int(group_id)
    conn = _db()
    try:
        group = conn.execute('SELECT * FROM groups WHERE id=?', (gid,)).fetchone()
        pages = conn.execute('SELECT page_key FROM group_pages WHERE group_id=? ORDER BY page_key', (gid,)).fetchall()
    finally:
        conn.close()
    if not group:
        return {}
    return {
        'id': gid,
        'name': str(group['name'] or ''),
        'auth_idle_timeout_minutes_override': group['auth_idle_timeout_minutes_override'] if 'auth_idle_timeout_minutes_override' in group.keys() else None,
        'page_keys': sorted([str(r['page_key']) for r in pages or []]),
        'videohub_allowed_outputs': _parse_group_allowlist_field(group['videohub_allowed_outputs'] if 'videohub_allowed_outputs' in group.keys() else None),
        'videohub_allowed_inputs': _parse_group_allowlist_field(group['videohub_allowed_inputs'] if 'videohub_allowed_inputs' in group.keys() else None),
        'videohub_allowed_presets': _parse_group_allowlist_field(group['videohub_allowed_presets'] if 'videohub_allowed_presets' in group.keys() else None),
        'videohub_can_edit_presets': bool(int(group['videohub_can_edit_presets'] or 0)) if 'videohub_can_edit_presets' in group.keys() and group['videohub_can_edit_presets'] is not None else False,
        'companion_click_surfaces': _coerce_string_allow_list(group['companion_click_surfaces'] if 'companion_click_surfaces' in group.keys() else None),
        'digico_allowed_auxes': _coerce_string_allow_list(group['digico_allowed_auxes'] if 'digico_allowed_auxes' in group.keys() else None),
        'pixie_allowed_auditoriums': _coerce_string_allow_list(group['pixie_allowed_auditoriums'] if 'pixie_allowed_auditoriums' in group.keys() else None),
        'pixie_allowed_devices': _coerce_pixie_device_permissions(group['pixie_allowed_devices'] if 'pixie_allowed_devices' in group.keys() else None),
        'pixie_allowed_scenes': _coerce_string_allow_list(group['pixie_allowed_scenes'] if 'pixie_allowed_scenes' in group.keys() else None),
        'atem_allowed_audio_sources': _coerce_string_allow_list(group['atem_allowed_audio_sources'] if 'atem_allowed_audio_sources' in group.keys() else None),
        'atem_can_solo_audio': bool(int(group['atem_can_solo_audio'] or 0)) if 'atem_can_solo_audio' in group.keys() and group['atem_can_solo_audio'] is not None else False,
        'atem_can_monitor_audio': bool(int(group['atem_can_monitor_audio'] or 0)) if 'atem_can_monitor_audio' in group.keys() and group['atem_can_monitor_audio'] is not None else False,
    }


def _log_group_setting_changes(before: dict, after: dict) -> None:
    if not before or not after:
        return
    gid = after.get('id') or before.get('id')
    name = after.get('name') or before.get('name') or f'Group #{gid}'
    source = _activity_source_default()

    def _changed(key: str) -> bool:
        return before.get(key) != after.get(key)

    if _changed('page_keys'):
        before_pages = set(before.get('page_keys') or [])
        after_pages = set(after.get('page_keys') or [])
        added = sorted(after_pages - before_pages)
        removed = sorted(before_pages - after_pages)
        log_event(
            'group.pages.update',
            f"Updated page access for group '{name}'",
            source=source,
            status='success',
            target_type='group',
            target_id=gid,
            details={'group_id': gid, 'group_name': name, 'added_pages': added, 'removed_pages': removed, 'page_keys': sorted(after_pages)},
        )
    if _changed('auth_idle_timeout_minutes_override'):
        log_event(
            'group.idle_timeout.update',
            f"Updated idle timeout override for group '{name}'",
            source=source,
            status='success',
            target_type='group',
            target_id=gid,
            details={'group_id': gid, 'group_name': name, 'old': before.get('auth_idle_timeout_minutes_override'), 'new': after.get('auth_idle_timeout_minutes_override')},
        )
    if _changed('videohub_allowed_outputs') or _changed('videohub_allowed_inputs'):
        log_event(
            'group.videohub.routing_allowlist.update',
            f"Updated VideoHub routing allow-lists for group '{name}'",
            source=source,
            status='success',
            target_type='group',
            target_id=gid,
            details={'group_id': gid, 'group_name': name, 'old_outputs': before.get('videohub_allowed_outputs'), 'new_outputs': after.get('videohub_allowed_outputs'), 'old_inputs': before.get('videohub_allowed_inputs'), 'new_inputs': after.get('videohub_allowed_inputs')},
        )
    if _changed('videohub_allowed_presets'):
        log_event(
            'group.videohub.preset_allowlist.update',
            f"Updated VideoHub preset visibility for group '{name}'",
            source=source,
            status='success',
            target_type='group',
            target_id=gid,
            details={'group_id': gid, 'group_name': name, 'old': before.get('videohub_allowed_presets'), 'new': after.get('videohub_allowed_presets')},
        )
    if _changed('videohub_can_edit_presets'):
        log_event(
            'group.videohub.preset_edit.update',
            f"Updated VideoHub preset edit permission for group '{name}'",
            source=source,
            status='success',
            target_type='group',
            target_id=gid,
            details={'group_id': gid, 'group_name': name, 'old': before.get('videohub_can_edit_presets'), 'new': after.get('videohub_can_edit_presets')},
        )
    if _changed('companion_click_surfaces'):
        log_event(
            'group.companion.click_surfaces.update',
            f"Updated Companion click surfaces for group '{name}'",
            source=source,
            status='success',
            target_type='group',
            target_id=gid,
            details={'group_id': gid, 'group_name': name, 'old': before.get('companion_click_surfaces'), 'new': after.get('companion_click_surfaces')},
        )
    if _changed('digico_allowed_auxes'):
        log_event(
            'group.digico.aux_allowlist.update',
            f"Updated Personal Mix AUX access for group '{name}'",
            source=source,
            status='success',
            target_type='group',
            target_id=gid,
            details={'group_id': gid, 'group_name': name, 'old': before.get('digico_allowed_auxes'), 'new': after.get('digico_allowed_auxes')},
        )
    if _changed('pixie_allowed_auditoriums') or _changed('pixie_allowed_devices') or _changed('pixie_allowed_scenes'):
        log_event(
            'group.pixie.permissions.update',
            f"Updated Pixie Controls permissions for group '{name}'",
            source=source,
            status='success',
            target_type='group',
            target_id=gid,
            details={
                'group_id': gid,
                'group_name': name,
                'old_auditoriums': before.get('pixie_allowed_auditoriums'),
                'new_auditoriums': after.get('pixie_allowed_auditoriums'),
                'old_devices': before.get('pixie_allowed_devices'),
                'new_devices': after.get('pixie_allowed_devices'),
                'old_scenes': before.get('pixie_allowed_scenes'),
                'new_scenes': after.get('pixie_allowed_scenes'),
            },
        )
    if _changed('atem_allowed_audio_sources') or _changed('atem_can_solo_audio') or _changed('atem_can_monitor_audio'):
        log_event(
            'group.atem_audio.update',
            f"Updated ATEM audio permissions for group '{name}'",
            source=source,
            status='success',
            target_type='group',
            target_id=gid,
            details={
                'group_id': gid,
                'group_name': name,
                'old_sources': before.get('atem_allowed_audio_sources'),
                'new_sources': after.get('atem_allowed_audio_sources'),
                'old_can_solo': before.get('atem_can_solo_audio'),
                'new_can_solo': after.get('atem_can_solo_audio'),
                'old_can_monitor': before.get('atem_can_monitor_audio'),
                'new_can_monitor': after.get('atem_can_monitor_audio'),
            },
        )


def _get_user_groups(user_id: int | None) -> list[sqlite3.Row]:
    if user_id is None:
        return []
    conn = _db()
    try:
        return conn.execute(
            """
            SELECT g.*
            FROM groups g
            JOIN user_groups ug ON ug.group_id = g.id
            WHERE ug.user_id=?
            ORDER BY lower(g.name)
            """,
            (int(user_id),),
        ).fetchall()
    finally:
        conn.close()


def _get_user_access_snapshot(user_id: int | None) -> tuple[list[sqlite3.Row], dict[int, set[str]]]:
    """Load group settings and page grants in one database round trip."""
    if user_id is None:
        return [], {}
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT g.*,gp.page_key AS granted_page_key
            FROM groups g
            JOIN user_groups ug ON ug.group_id=g.id
            LEFT JOIN group_pages gp ON gp.group_id=g.id
            WHERE ug.user_id=?
            ORDER BY lower(g.name),gp.page_key
            """,
            (int(user_id),),
        ).fetchall()
    finally:
        conn.close()

    groups_by_id: dict[int, sqlite3.Row] = {}
    pages_by_group: dict[int, set[str]] = {}
    for row in rows or []:
        try:
            group_id = int(row['id'])
        except Exception:
            continue
        groups_by_id.setdefault(group_id, row)
        page_key = str(row['granted_page_key'] or '').strip()
        if page_key:
            pages_by_group.setdefault(group_id, set()).add(page_key)
    return list(groups_by_id.values()), pages_by_group


def _get_user_groups_for_page(user_id: int | None, page_key: str) -> list[sqlite3.Row]:
    if user_id is None or not page_key:
        return []
    conn = _db()
    try:
        return conn.execute(
            """
            SELECT DISTINCT g.*
            FROM groups g
            JOIN user_groups ug ON ug.group_id = g.id
            JOIN group_pages gp ON gp.group_id = g.id
            WHERE ug.user_id=? AND gp.page_key=?
            ORDER BY lower(g.name)
            """,
            (int(user_id), str(page_key)),
        ).fetchall()
    finally:
        conn.close()


def _user_is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    conn = _db()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM user_groups ug
            JOIN groups g ON g.id = ug.group_id
            WHERE ug.user_id=? AND g.is_admin=1
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def _can_manage_service_tokens_for_current_user() -> bool:
    """Token minting is reserved for administrators when auth is enabled."""
    if not _auth_enabled():
        return True
    try:
        if not getattr(current_user, 'is_authenticated', False):
            return False
        return _user_is_admin(int(current_user.get_id()))
    except Exception:
        return False


def _user_allows_page(user_id: int | None, page_key: str) -> bool:
    if not page_key or user_id is None:
        return False
    if _user_is_admin(user_id):
        return True
    conn = _db()
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM user_groups ug
            JOIN group_pages gp ON gp.group_id = ug.group_id
            WHERE ug.user_id=? AND gp.page_key=?
            LIMIT 1
            """,
            (int(user_id), str(page_key)),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def _effective_group_allowlist(rows: list[sqlite3.Row], column: str) -> list[int]:
    """Merge group allow-lists.

    Blank/NULL means allow all. If any assigned group allows all, the effective
    allow-list is blank/all. Otherwise return the union of all IDs.
    """
    if not rows:
        return []
    merged: set[int] = set()
    saw_restricted = False
    for row in rows:
        raw = row[column]
        try:
            raw_s = '' if raw is None else str(raw).strip()
        except Exception:
            raw_s = ''
        if not raw_s or raw_s.lower() in ('all', '*', 'inherit', 'default', 'global') or raw_s == '[]':
            return []
        nums = _parse_group_allowlist_field(raw_s)
        saw_restricted = True
        merged.update(nums)
    if not saw_restricted:
        return []
    return sorted(merged)


def _effective_group_string_allowlist(rows: list[sqlite3.Row], column: str) -> list[str]:
    """Merge string allow-lists.

    Blank/NULL/"[]" means allow all. If any assigned group allows all, return
    an empty list to represent unrestricted access.
    """
    if not rows:
        return []
    merged: set[str] = set()
    saw_restricted = False
    for row in rows:
        raw = row[column]
        try:
            raw_s = '' if raw is None else str(raw).strip()
        except Exception:
            raw_s = ''
        if not raw_s or raw_s.lower() in ('all', '*', 'inherit', 'default', 'global') or raw_s == '[]':
            return []
        values = _coerce_string_allow_list(raw_s)
        saw_restricted = True
        merged.update(values)
    if not saw_restricted:
        return []
    return sorted(merged)


def _effective_videohub_allowlists_for_user(user_id: int | None) -> tuple[list[int], list[int]]:
    try:
        if (
            has_request_context()
            and getattr(current_user, 'is_authenticated', False)
            and int(current_user.get_id()) == int(user_id)
            and hasattr(current_user, 'videohub_allowed_outputs')
        ):
            return (
                list(current_user.videohub_allowed_outputs),
                list(current_user.videohub_allowed_inputs),
            )
    except Exception:
        pass
    if user_id is None or _user_is_admin(user_id):
        return ([], [])
    groups = _get_user_groups_for_page(user_id, 'page:routing')
    return (
        _effective_group_allowlist(groups, 'videohub_allowed_outputs'),
        _effective_group_allowlist(groups, 'videohub_allowed_inputs'),
    )


def _effective_videohub_preset_ids_for_user(user_id: int | None) -> list[int]:
    if user_id is None or _user_is_admin(user_id):
        return []
    return _effective_group_allowlist(_get_user_groups_for_page(user_id, 'page:videohub'), 'videohub_allowed_presets')


def _effective_videohub_can_edit_presets_for_user(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if _user_is_admin(user_id):
        return True
    groups = _get_user_groups_for_page(user_id, 'page:videohub')
    if not groups:
        return False
    for row in groups:
        v = row['videohub_can_edit_presets']
        if v is None:
            continue
        try:
            if bool(int(v)):
                return True
        except Exception:
            continue
    return False


def _effective_companion_click_surface_ids_for_user(user_id: int | None) -> list[str]:
    if user_id is None or _user_is_admin(user_id):
        return []
    return _effective_group_string_allowlist(_get_user_groups(user_id), 'companion_click_surfaces')


def _effective_digico_aux_ids_for_user(user_id: int | None) -> list[str]:
    try:
        if (
            has_request_context()
            and getattr(current_user, 'is_authenticated', False)
            and int(current_user.get_id()) == int(user_id)
            and hasattr(current_user, 'digico_allowed_auxes')
        ):
            return list(current_user.digico_allowed_auxes)
    except Exception:
        pass
    if user_id is None or _user_is_admin(user_id):
        return []
    return _effective_group_string_allowlist(
        _get_user_groups_for_page(user_id, 'page:digico_mixer'),
        'digico_allowed_auxes',
    )


def _effective_pixie_permissions_for_user(user_id: int | None) -> dict[str, Any]:
    """Union Pixie grants while keeping device scope tied to its auditorium grant."""
    if not _auth_enabled() or (user_id is not None and _user_is_admin(user_id)):
        return {'all': True, 'auditoriums': {}, 'scenes': []}
    if user_id is None:
        return {'all': False, 'auditoriums': {}, 'scenes': []}
    rows = _get_user_groups_for_page(user_id, 'page:pixie_controls')
    auditorium_permissions: dict[str, None | set[str]] = {}
    scenes: set[str] = set()
    for row in rows:
        auditorium_ids = _coerce_string_allow_list(
            row['pixie_allowed_auditoriums'] if 'pixie_allowed_auditoriums' in row.keys() else None
        )
        device_map = _coerce_pixie_device_permissions(
            row['pixie_allowed_devices'] if 'pixie_allowed_devices' in row.keys() else None,
            auditorium_ids,
        )
        for auditorium_id in auditorium_ids:
            permission = device_map.get(auditorium_id, '*')
            if permission == '*':
                auditorium_permissions[auditorium_id] = None
                continue
            if auditorium_id in auditorium_permissions and auditorium_permissions[auditorium_id] is None:
                continue
            allowed = auditorium_permissions.setdefault(auditorium_id, set())
            if isinstance(allowed, set):
                allowed.update(_coerce_string_allow_list(permission))
        scenes.update(_coerce_string_allow_list(
            row['pixie_allowed_scenes'] if 'pixie_allowed_scenes' in row.keys() else None
        ))
    return {
        'all': False,
        'auditoriums': {
            auditorium_id: None if device_ids is None else sorted(device_ids)
            for auditorium_id, device_ids in auditorium_permissions.items()
        },
        'scenes': sorted(scenes),
    }


def _effective_atem_audio_source_ids_for_user(user_id: int | None) -> list[str]:
    if user_id is None:
        return []
    if _user_is_admin(user_id):
        return []
    groups = _get_user_groups(user_id)
    merged: set[str] = set()
    for row in groups:
        try:
            merged.update(_coerce_string_allow_list(row['atem_allowed_audio_sources']))
        except Exception:
            continue
    return sorted(merged)


def _effective_atem_can_solo_audio_for_user(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if _user_is_admin(user_id):
        return True
    groups = _get_user_groups(user_id)
    for row in groups:
        try:
            if bool(int(row['atem_can_solo_audio'] or 0)):
                return True
        except Exception:
            continue
    return False


def _effective_atem_can_monitor_audio_for_user(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if _user_is_admin(user_id):
        return True
    groups = _get_user_groups(user_id)
    for row in groups:
        try:
            if bool(int(row['atem_can_monitor_audio'] or 0)):
                return True
        except Exception:
            continue
    return False


def _can_click_companion_surface_for_current_user(surface_id: str) -> bool:
    sid = str(surface_id or '').strip()
    if not sid:
        return False
    try:
        if not _auth_enabled():
            return True
    except Exception:
        return True
    try:
        if not getattr(current_user, 'is_authenticated', False):
            return False
    except Exception:
        return False
    try:
        uid = int(current_user.get_id())
        if _user_is_admin(uid):
            return True
        allowed = _effective_companion_click_surface_ids_for_user(uid)
        return (not allowed) or (sid in set(allowed))
    except Exception:
        return False


def _admin_active_admin_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT count(DISTINCT u.id) AS c
        FROM users u
        JOIN user_groups ug ON ug.user_id=u.id
        JOIN groups g ON g.id=ug.group_id
        WHERE u.is_active=1 AND g.is_admin=1
        """
    ).fetchone()
    return int(row['c'] or 0) if row else 0


def _admin_user_has_admin_group(conn: sqlite3.Connection, uid: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM user_groups ug
        JOIN groups g ON g.id=ug.group_id
        WHERE ug.user_id=? AND g.is_admin=1
        LIMIT 1
        """,
        (int(uid),),
    ).fetchone()
    return bool(row)


def _admin_group_ids_include_admin(conn: sqlite3.Connection, group_ids: list[int]) -> bool:
    if not group_ids:
        return False
    placeholders = ','.join(['?'] * len(group_ids))
    row = conn.execute(
        f'SELECT 1 FROM groups WHERE is_admin=1 AND id IN ({placeholders}) LIMIT 1',
        tuple(group_ids),
    ).fetchone()
    return bool(row)


def _admin_replace_user_groups(conn: sqlite3.Connection, uid: int, group_ids: list[int]) -> None:
    conn.execute('DELETE FROM user_groups WHERE user_id=?', (int(uid),))
    for gid in group_ids:
        exists = conn.execute('SELECT 1 FROM groups WHERE id=?', (int(gid),)).fetchone()
        if exists:
            conn.execute(
                'INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES (?,?)',
                (int(uid), int(gid)),
            )


def _admin_update_user(conn: sqlite3.Connection, uid: int, group_ids: list[int], is_active: int) -> bool:
    row = conn.execute('SELECT is_active FROM users WHERE id=?', (int(uid),)).fetchone()
    if not row:
        return False
    if _would_remove_last_active_admin(conn, int(uid), is_active=bool(is_active), group_ids=group_ids):
        return False
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        'UPDATE users SET is_active=?, updated_at=?, updated_by=? WHERE id=?',
        (1 if is_active else 0, now, _current_admin_user_id(), int(uid)),
    )
    _admin_replace_user_groups(conn, uid, group_ids)
    return True


def _can_manage_videohub_rooms_for_current_user() -> bool:
    """Whether the current user should be allowed to access room management UI."""
    try:
        if not _auth_enabled():
            return True
    except Exception:
        return False

    try:
        if not getattr(current_user, 'is_authenticated', False):
            return False
    except Exception:
        return False

    try:
        if not can_access('page:videohub'):
            return False
    except Exception:
        return False

    try:
        uid = int(current_user.get_id())
        return bool(_effective_videohub_can_edit_presets_for_user(uid))
    except Exception:
        return False


_ACTIVITY_LIVE_MAX = 500
_activity_live_lock = threading.Lock()
_activity_live_events: deque[dict[str, Any]] = deque(maxlen=_ACTIVITY_LIVE_MAX)


def _activity_now() -> str:
    try:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def _activity_request_path() -> str:
    try:
        if has_request_context():
            return str(request.path or '')
    except Exception:
        pass
    return ''


def _activity_request_ip() -> str:
    try:
        if has_request_context():
            return str(request.remote_addr or '')
    except Exception:
        pass
    return ''


def _activity_current_actor() -> tuple[int | None, str, str]:
    try:
        if has_request_context() and getattr(current_user, 'is_authenticated', False):
            uid = int(current_user.get_id())
            uname = str(getattr(current_user, 'username', None) or '')
            return uid, uname, uname or f'User #{uid}'
    except Exception:
        pass
    try:
        if has_request_context():
            principal = getattr(g, 'api_principal', None)
            if isinstance(principal, dict) and principal.get('type') == 'service_token':
                name = str(principal.get('name') or f"Token #{principal.get('id')}")
                return None, f'service-token:{name}', f'Service token: {name}'
            if isinstance(principal, dict) and principal.get('type') == 'scheduler':
                return None, 'scheduler', 'Scheduler'
            if isinstance(principal, dict) and principal.get('type') == 'legacy_anonymous':
                return None, '', 'Legacy anonymous API'
    except Exception:
        pass
    return None, '', ''


def _activity_source_default() -> str:
    path = _activity_request_path()
    if path.startswith('/api/'):
        try:
            principal = getattr(g, 'api_principal', None)
            if isinstance(principal, dict) and principal.get('type') == 'service_token':
                return 'companion'
            if isinstance(principal, dict) and principal.get('type') == 'scheduler':
                return 'scheduler'
        except Exception:
            pass
        return 'api'
    if path:
        return 'web'
    return 'system'


_ACTIVITY_REDACT_KEYS = {
    'password',
    'new_password',
    'current_password',
    'password_hash',
    'token',
    'secret',
    'csrf',
    'flask_secret_key',
    'session',
    'cookie',
}


def _activity_sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return '...'
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:80]:
            key = str(k)
            if any(secret_key in key.lower() for secret_key in _ACTIVITY_REDACT_KEYS):
                out[key] = '[redacted]'
            else:
                out[key] = _activity_sanitize(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_activity_sanitize(v, depth=depth + 1) for v in list(value)[:80]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 1000:
            return value[:1000] + '...'
        return value
    try:
        return str(value)[:1000]
    except Exception:
        return ''


def _activity_details_json(details: Any) -> str | None:
    if details is None:
        return None
    try:
        text = json.dumps(_activity_sanitize(details), ensure_ascii=False)
    except Exception:
        try:
            text = json.dumps({'detail': str(details)[:1000]}, ensure_ascii=False)
        except Exception:
            return None
    if len(text) > 12000:
        text = text[:12000] + '...'
    return text


def _activity_ts_param(value: str | None, *, end: bool = False) -> str | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    raw = raw.replace('T', ' ')
    if len(raw) == 10:
        return raw + (' 23:59:59' if end else ' 00:00:00')
    if len(raw) == 16:
        return raw + (':59' if end else ':00')
    if len(raw) >= 19:
        return raw[:19]
    return None


def _activity_row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    def get(name: str, default=None):
        try:
            return row[name]  # type: ignore[index]
        except Exception:
            try:
                return row.get(name, default)  # type: ignore[attr-defined]
            except Exception:
                return default

    details_raw = get('details_json')
    details = None
    if details_raw:
        try:
            details = json.loads(str(details_raw))
        except Exception:
            details = details_raw
    return {
        'id': int(get('id') or 0),
        'ts': str(get('ts') or ''),
        'actor_user_id': get('actor_user_id'),
        'actor_username': str(get('actor_username') or ''),
        'actor_display': str(get('actor_display') or ''),
        'source': str(get('source') or 'system'),
        'action': str(get('action') or ''),
        'target_type': str(get('target_type') or ''),
        'target_id': str(get('target_id') or ''),
        'status': str(get('status') or 'info'),
        'summary': str(get('summary') or ''),
        'details': details,
        'details_json': str(details_raw or ''),
        'ip': str(get('ip') or ''),
        'request_path': str(get('request_path') or ''),
    }


_ACTIVITY_GLOBAL_ACK_USER_ID = 0


def _ensure_activity_log_ack_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_log_ack (
          actor_user_id INTEGER PRIMARY KEY,
          acknowledged_activity_id INTEGER NOT NULL DEFAULT 0,
          acknowledged_at TEXT
        )
        """
    )


def _activity_alert_summary() -> dict[str, Any]:
    conn = _db()
    try:
        _ensure_activity_log_ack_table(conn)
        ack_row = conn.execute(
            'SELECT max(acknowledged_activity_id) AS acknowledged_activity_id FROM activity_log_ack',
        ).fetchone()
        ack_id = int(ack_row['acknowledged_activity_id'] or 0) if ack_row else 0
        row = conn.execute(
            """
            SELECT
              count(*) AS c,
              max(id) AS max_id,
              sum(CASE WHEN lower(status)='failure' THEN 1 ELSE 0 END) AS failures,
              sum(CASE WHEN lower(status)='warning' THEN 1 ELSE 0 END) AS warnings
            FROM activity_log
            WHERE id > ?
              AND lower(status) IN ('warning', 'failure')
            """,
            (ack_id,),
        ).fetchone()
    finally:
        conn.close()
    total = int(row['c'] or 0) if row else 0
    failures = int(row['failures'] or 0) if row else 0
    warnings = int(row['warnings'] or 0) if row else 0
    return {
        'count': total,
        'failures': failures,
        'warnings': warnings,
        'severity': 'failure' if failures else ('warning' if warnings else ''),
        'acknowledged_activity_id': ack_id,
        'latest_activity_id': int(row['max_id'] or ack_id) if row else ack_id,
    }


def _activity_acknowledge_alerts() -> dict[str, Any]:
    conn = _db()
    try:
        _ensure_activity_log_ack_table(conn)
        row = conn.execute(
            "SELECT max(id) AS max_id FROM activity_log WHERE lower(status) IN ('warning', 'failure')"
        ).fetchone()
        max_id = int(row['max_id'] or 0) if row else 0
        conn.execute(
            """
            INSERT INTO activity_log_ack(actor_user_id, acknowledged_activity_id, acknowledged_at)
            VALUES(?, ?, ?)
            ON CONFLICT(actor_user_id) DO UPDATE SET
              acknowledged_activity_id=excluded.acknowledged_activity_id,
              acknowledged_at=excluded.acknowledged_at
            """,
            (_ACTIVITY_GLOBAL_ACK_USER_ID, max_id, _activity_now()),
        )
        conn.commit()
    finally:
        conn.close()
    return _activity_alert_summary()


def log_event(
    action: str,
    summary: str | None = None,
    *,
    source: str | None = None,
    status: str = 'info',
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: Any = None,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
    actor_display: str | None = None,
    ip: str | None = None,
    request_path: str | None = None,
    ts: str | None = None,
) -> dict[str, Any] | None:
    """Persist a structured activity event and publish it to the live buffer."""

    action_s = str(action or '').strip()
    if not action_s:
        return None
    status_s = str(status or 'info').strip().lower()
    if status_s not in ('success', 'failure', 'warning', 'info'):
        status_s = 'info'
    source_s = str(source or _activity_source_default() or 'system').strip().lower()
    ts_s = str(ts or _activity_now())

    current_uid, current_uname, current_display = _activity_current_actor()
    actor_uid = actor_user_id if actor_user_id is not None else current_uid
    actor_name = str(actor_username if actor_username is not None else current_uname).strip()
    actor_label = str(actor_display if actor_display is not None else current_display).strip()
    if not actor_label:
        if actor_name:
            actor_label = actor_name
        elif source_s == 'companion':
            actor_label = f"Companion{(' ' + str(ip or _activity_request_ip())) if (ip or _activity_request_ip()) else ''}"
        elif source_s == 'scheduler':
            actor_label = 'Scheduler'
        elif source_s == 'api':
            actor_label = f"API{(' ' + str(ip or _activity_request_ip())) if (ip or _activity_request_ip()) else ''}"
        else:
            actor_label = 'System'

    ip_s = str(ip if ip is not None else _activity_request_ip()).strip()
    path_s = str(request_path if request_path is not None else _activity_request_path()).strip()
    details_json = _activity_details_json(details)
    summary_s = str(summary or action_s.replace('_', ' ').replace('.', ' ')).strip()
    target_id_s = str(target_id) if target_id is not None else None

    row_id = None
    try:
        conn = _db()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL,
                  actor_user_id INTEGER,
                  actor_username TEXT,
                  actor_display TEXT,
                  source TEXT NOT NULL DEFAULT 'system',
                  action TEXT NOT NULL,
                  target_type TEXT,
                  target_id TEXT,
                  status TEXT NOT NULL DEFAULT 'info',
                  summary TEXT,
                  details_json TEXT,
                  ip TEXT,
                  request_path TEXT
                )
                """
            )
            cur = conn.execute(
                """
                INSERT INTO activity_log(
                  ts,actor_user_id,actor_username,actor_display,source,action,
                  target_type,target_id,status,summary,details_json,ip,request_path
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ts_s,
                    actor_uid,
                    actor_name or None,
                    actor_label or None,
                    source_s,
                    action_s,
                    str(target_type or '').strip() or None,
                    target_id_s,
                    status_s,
                    summary_s,
                    details_json,
                    ip_s or None,
                    path_s or None,
                ),
            )
            row_id = int(cur.lastrowid or 0)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        row_id = None

    event = {
        'id': int(row_id or 0),
        'ts': ts_s,
        'actor_user_id': actor_uid,
        'actor_username': actor_name,
        'actor_display': actor_label,
        'source': source_s,
        'action': action_s,
        'target_type': str(target_type or ''),
        'target_id': target_id_s or '',
        'status': status_s,
        'summary': summary_s,
        'details': _activity_sanitize(details) if details is not None else None,
        'details_json': details_json or '',
        'ip': ip_s,
        'request_path': path_s,
    }
    with _activity_live_lock:
        _activity_live_events.append(event)
    return event


def _audit(action: str, detail: str | None = None) -> None:
    ts = _activity_now()
    uid, uname, _actor_display = _activity_current_actor()
    ip = _activity_request_ip()
    try:
        log_event(
            str(action or ''),
            str(action or '').replace('_', ' '),
            source=_activity_source_default(),
            status='info',
            details={'detail': detail} if detail is not None else None,
            actor_user_id=uid,
            actor_username=uname or None,
            ip=ip or None,
            ts=ts,
        )
    except Exception:
        pass
    try:
        conn = _db()
        try:
            conn.execute(
                'INSERT INTO audit(ts,user_id,username,action,detail,ip) VALUES (?,?,?,?,?,?)',
                (ts, uid, uname or None, str(action or ''), (str(detail) if detail is not None else None), ip or None),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# --- Page registry (forward-compatible) ---
_PAGE_REGISTRY: dict[str, dict[str, str]] = {}


def _register_page(page_key: str, friendly_name: str) -> None:
    if not page_key:
        return
    if page_key not in _PAGE_REGISTRY:
        _PAGE_REGISTRY[page_key] = {'name': friendly_name or page_key}
    else:
        # keep the first friendly name unless it was empty
        if not _PAGE_REGISTRY[page_key].get('name') and friendly_name:
            _PAGE_REGISTRY[page_key]['name'] = friendly_name


def require_page(page_key: str, friendly_name: str):
    """Mark a view function as a protected page with a registry key."""
    _register_page(page_key, friendly_name)

    def _decorator(fn):
        setattr(fn, '_required_page_key', page_key)
        setattr(fn, '_required_page_name', friendly_name)
        return fn

    return _decorator


# Action grants use the existing group_pages storage and permission editor.
# They grant no page by themselves; all media operations also require page:media.
_register_page('page:media_upload', 'Media: Upload images')
_register_page('page:media_manage', 'Media: Manage images and presets')
_register_page('page:media_load', 'Media: Load ATEM players')


def _get_group_by_name(name: str) -> sqlite3.Row | None:
    conn = _db()
    try:
        cur = conn.execute('SELECT * FROM groups WHERE name=?', (name,))
        return cur.fetchone()
    finally:
        conn.close()


def _ensure_group(name: str, *, is_system: bool = False, is_admin: bool = False) -> int:
    conn = _db()
    try:
        row = conn.execute('SELECT id,is_system,is_admin FROM groups WHERE name=?', (name,)).fetchone()
        if row:
            updates = []
            params = []
            if is_system and not bool(int(row['is_system'] or 0)):
                updates.append('is_system=?')
                params.append(1)
            if is_admin and not bool(int(row['is_admin'] or 0)):
                updates.append('is_admin=?')
                params.append(1)
            if updates:
                params.append(int(row['id']))
                conn.execute(f"UPDATE groups SET {', '.join(updates)} WHERE id=?", tuple(params))
                conn.commit()
            return int(row['id'])
        conn.execute(
            'INSERT INTO groups(name,is_system,is_admin) VALUES (?,?,?)',
            (name, 1 if is_system else 0, 1 if is_admin else 0),
        )
        conn.commit()
        row2 = conn.execute('SELECT id FROM groups WHERE name=?', (name,)).fetchone()
        return int(row2['id'])
    finally:
        conn.close()


def _migrate_roles_to_groups() -> None:
    """Copy the old single-role model into groups/memberships once."""
    if _auth_meta_get('roles_to_groups_migrated') == '1':
        return

    conn = _db()
    try:
        existing_groups = conn.execute('SELECT count(*) AS c FROM groups').fetchone()
        roles = conn.execute(
            """
            SELECT id,name,auth_idle_timeout_minutes_override,videohub_allowed_outputs,
                   videohub_allowed_inputs,videohub_allowed_presets,videohub_can_edit_presets
            FROM roles
            """
        ).fetchall()
        if existing_groups and int(existing_groups['c'] or 0) > 0:
            role_to_group = {}
            for r in roles or []:
                existing = conn.execute('SELECT id FROM groups WHERE name=?', (str(r['name'] or '').strip(),)).fetchone()
                if existing:
                    role_to_group[int(r['id'])] = int(existing['id'])
            for u in conn.execute('SELECT id,role_id FROM users WHERE role_id IS NOT NULL').fetchall() or []:
                gid = role_to_group.get(int(u['role_id']))
                if gid:
                    conn.execute(
                        'INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES (?,?)',
                        (int(u['id']), gid),
                    )
            conn.execute(
                'INSERT INTO auth_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('roles_to_groups_migrated', '1'),
            )
            conn.commit()
            return

        role_to_group: dict[int, int] = {}
        for r in roles or []:
            name = str(r['name'] or '').strip()
            if not name:
                continue
            existing = conn.execute('SELECT id FROM groups WHERE name=?', (name,)).fetchone()
            if existing:
                gid = int(existing['id'])
                conn.execute(
                    """
                    UPDATE groups
                    SET is_system=CASE WHEN name='Admin' THEN 1 ELSE is_system END,
                        is_admin=CASE WHEN name='Admin' THEN 1 ELSE is_admin END,
                        auth_idle_timeout_minutes_override=COALESCE(auth_idle_timeout_minutes_override, ?),
                        videohub_allowed_outputs=COALESCE(videohub_allowed_outputs, ?),
                        videohub_allowed_inputs=COALESCE(videohub_allowed_inputs, ?),
                        videohub_allowed_presets=COALESCE(videohub_allowed_presets, ?),
                        videohub_can_edit_presets=COALESCE(videohub_can_edit_presets, ?)
                    WHERE id=?
                    """,
                    (
                        r['auth_idle_timeout_minutes_override'],
                        r['videohub_allowed_outputs'],
                        r['videohub_allowed_inputs'],
                        r['videohub_allowed_presets'],
                        r['videohub_can_edit_presets'],
                        gid,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO groups(
                      name,is_system,is_admin,auth_idle_timeout_minutes_override,
                      videohub_allowed_outputs,videohub_allowed_inputs,
                      videohub_allowed_presets,videohub_can_edit_presets
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        name,
                        1 if name == 'Admin' else 0,
                        1 if name == 'Admin' else 0,
                        r['auth_idle_timeout_minutes_override'],
                        r['videohub_allowed_outputs'],
                        r['videohub_allowed_inputs'],
                        r['videohub_allowed_presets'],
                        r['videohub_can_edit_presets'],
                    ),
                )
                gid = int(cur.lastrowid)
            role_to_group[int(r['id'])] = gid

        for rp in conn.execute('SELECT role_id,page_key FROM role_pages').fetchall() or []:
            gid = role_to_group.get(int(rp['role_id']))
            if gid:
                conn.execute(
                    'INSERT OR IGNORE INTO group_pages(group_id,page_key) VALUES (?,?)',
                    (gid, str(rp['page_key'])),
                )

        for u in conn.execute('SELECT id,role_id FROM users WHERE role_id IS NOT NULL').fetchall() or []:
            gid = role_to_group.get(int(u['role_id']))
            if gid:
                conn.execute(
                    'INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES (?,?)',
                    (int(u['id']), gid),
                )
        conn.execute(
            'INSERT INTO auth_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('roles_to_groups_migrated', '1'),
        )
        conn.commit()
    finally:
        conn.close()


def _bootstrap_default_users_roles() -> None:
    """Create initial groups and default admin/admin if missing."""
    _init_auth_db()
    defaults_seeded = _auth_meta_get('default_groups_seeded') == '1'
    had_groups_before_bootstrap = False
    try:
        conn = _db()
        try:
            row = conn.execute('SELECT count(*) AS c FROM groups').fetchone()
            had_groups_before_bootstrap = bool(row and int(row['c'] or 0) > 0)
        finally:
            conn.close()
    except Exception:
        had_groups_before_bootstrap = False

    _migrate_roles_to_groups()

    admin_group_id = _ensure_group('Admin', is_system=True, is_admin=True)
    seed_td_sp = (not defaults_seeded) and (not had_groups_before_bootstrap)
    td_group_id = _ensure_group('TD') if seed_td_sp else None
    sp_group_id = _ensure_group('SP') if seed_td_sp else None
    try:
        conn = _db()
        try:
            conn.execute("UPDATE groups SET is_system=0 WHERE is_admin=0 AND name!='Admin'")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

    # Seed initial page access lists once. Do not overwrite user-managed
    # assignments from the Admin UI.
    if seed_td_sp and td_group_id is not None and sp_group_id is not None:
        try:
            conn = _db()
            try:
                td_has = conn.execute('SELECT 1 FROM group_pages WHERE group_id=? LIMIT 1', (td_group_id,)).fetchone()
                sp_has = conn.execute('SELECT 1 FROM group_pages WHERE group_id=? LIMIT 1', (sp_group_id,)).fetchone()
            finally:
                conn.close()

            if not td_has or not sp_has:
                all_pages = sorted(_PAGE_REGISTRY.keys())
                if 'page:home' not in all_pages:
                    all_pages = ['page:home', *all_pages]

                if not td_has:
                    td_pages = [k for k in all_pages if k not in ('page:config', 'page:admin') and not k.startswith('page:media')]
                    _set_group_pages(td_group_id, td_pages)
                if not sp_has:
                    sp_pages = [k for k in all_pages if k in ('page:home', 'page:timers')]
                    _set_group_pages(sp_group_id, sp_pages)
        except Exception:
            pass
    if not defaults_seeded:
        try:
            _auth_meta_set('default_groups_seeded', '1')
        except Exception:
            pass

    # Default admin/admin
    conn = _db()
    try:
        row = conn.execute('SELECT id FROM users WHERE username=?', ('admin',)).fetchone()
        if not row:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute(
                'INSERT INTO users(username,password_hash,role_id,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                ('admin', generate_password_hash('admin'), None, 1, now, now),
            )
            row = conn.execute('SELECT id FROM users WHERE username=?', ('admin',)).fetchone()
            if row:
                conn.execute(
                    'INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES (?,?)',
                    (int(row['id']), admin_group_id),
                )
            conn.commit()
        else:
            conn.execute(
                'INSERT OR IGNORE INTO user_groups(user_id,group_id) VALUES (?,?)',
                (int(row['id']), admin_group_id),
            )
            conn.commit()
    finally:
        conn.close()


def _user_record(user_id: int) -> sqlite3.Row | None:
    conn = _db()
    try:
        return conn.execute(
            'SELECT u.* FROM users u WHERE u.id=?',
            (int(user_id),),
        ).fetchone()
    finally:
        conn.close()


def _user_by_username(username: str) -> sqlite3.Row | None:
    conn = _db()
    try:
        return conn.execute(
            'SELECT u.* FROM users u WHERE lower(u.username)=lower(?)',
            (str(username or ''),),
        ).fetchone()
    finally:
        conn.close()


def _auth_min_password_length() -> int:
    cfg = _auth_cfg()
    try:
        min_len = int(cfg.get('auth_min_password_length', 6))
    except Exception:
        min_len = 6
    return max(4, min(min_len, 128))


def _auth_lockout_failed_attempts() -> int:
    cfg = _auth_cfg()
    try:
        attempts = int(cfg.get('auth_lockout_failed_attempts', 5))
    except Exception:
        attempts = 5
    return max(1, min(attempts, 100))


def _now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _current_admin_user_id() -> int | None:
    try:
        if getattr(current_user, 'is_authenticated', False):
            return int(current_user.get_id())
    except Exception:
        pass
    return None


def _user_duplicate(conn: sqlite3.Connection, field: str, value: str, user_id: int | None = None) -> bool:
    value = str(value or '').strip()
    if not value:
        return False
    uid = int(user_id) if user_id else 0
    if field == 'email':
        row = conn.execute(
            "SELECT 1 FROM users WHERE trim(email)!='' AND lower(email)=lower(?) AND id<>? LIMIT 1",
            (value, uid),
        ).fetchone()
        return bool(row)
    row = conn.execute(
        'SELECT 1 FROM users WHERE lower(username)=lower(?) AND id<>? LIMIT 1',
        (value, uid),
    ).fetchone()
    return bool(row)


def _admin_active_admin_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT count(DISTINCT u.id) AS c
        FROM users u
        JOIN user_groups ug ON ug.user_id=u.id
        JOIN groups g ON g.id=ug.group_id
        WHERE u.is_active=1 AND COALESCE(u.is_locked,0)=0 AND g.is_admin=1
        """
    ).fetchone()
    return int(row['c'] or 0) if row else 0


def _admin_user_has_admin_group(conn: sqlite3.Connection, uid: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM user_groups ug
        JOIN groups g ON g.id=ug.group_id
        WHERE ug.user_id=? AND g.is_admin=1
        LIMIT 1
        """,
        (int(uid),),
    ).fetchone()
    return bool(row)


def _admin_group_ids_include_admin(conn: sqlite3.Connection, group_ids: list[int]) -> bool:
    if not group_ids:
        return False
    placeholders = ','.join(['?'] * len(group_ids))
    row = conn.execute(
        f'SELECT 1 FROM groups WHERE is_admin=1 AND id IN ({placeholders}) LIMIT 1',
        tuple(group_ids),
    ).fetchone()
    return bool(row)


def _would_remove_last_active_admin(
    conn: sqlite3.Connection,
    uid: int,
    *,
    is_active: bool | None = None,
    is_locked: bool | None = None,
    group_ids: list[int] | None = None,
) -> bool:
    row = conn.execute('SELECT is_active,is_locked FROM users WHERE id=?', (int(uid),)).fetchone()
    if not row:
        return False
    was_active = bool(int(row['is_active'] or 0))
    was_locked = bool(int(row['is_locked'] or 0))
    was_admin = _admin_user_has_admin_group(conn, uid)
    will_active = was_active if is_active is None else bool(is_active)
    will_locked = was_locked if is_locked is None else bool(is_locked)
    will_admin = was_admin if group_ids is None else _admin_group_ids_include_admin(conn, group_ids)
    if was_active and (not was_locked) and was_admin and ((not will_active) or will_locked or (not will_admin)):
        return _admin_active_admin_count(conn) <= 1
    return False


def _revoke_user_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    now = _now_str()
    conn.execute('UPDATE user_sessions SET revoked_at=COALESCE(revoked_at, ?) WHERE user_id=?', (now, int(user_id)))
    conn.execute(
        'UPDATE users SET session_version=COALESCE(session_version,0)+1, updated_at=? WHERE id=?',
        (now, int(user_id)),
    )


def _create_user_session(row: sqlite3.Row) -> None:
    sid = uuid.uuid4().hex
    now = _now_str()
    try:
        version = int(row['session_version'] or 0)
    except Exception:
        version = 0
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO user_sessions(id,user_id,created_at,last_seen_at,ip,user_agent,session_version)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                sid,
                int(row['id']),
                now,
                now,
                str(request.remote_addr or ''),
                str((request.user_agent.string if request.user_agent else '') or '')[:500],
                version,
            ),
        )
        conn.commit()
        session['_auth_session_id'] = sid
    finally:
        conn.close()


def _touch_current_user_session() -> bool:
    sid = str(session.get('_auth_session_id') or '').strip()
    uid = _current_admin_user_id()
    if uid is None:
        return False
    if not sid:
        row = _user_record(uid)
        if not row:
            return False
        if not bool(int(row['is_active'] or 0)) or bool(int(row['is_locked'] or 0)):
            return False
        _create_user_session(row)
        return True
    now = _now_str()
    conn = _db()
    try:
        row = conn.execute(
            """
            SELECT s.revoked_at,s.session_version AS session_row_version,
                   u.session_version AS user_session_version,u.is_active,u.is_locked
            FROM user_sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.id=? AND s.user_id=?
            """,
            (sid, uid),
        ).fetchone()
        if not row:
            return False
        if row['revoked_at'] or not bool(int(row['is_active'] or 0)) or bool(int(row['is_locked'] or 0)):
            return False
        if int(row['session_row_version'] or 0) != int(row['user_session_version'] or 0):
            return False
        conn.execute('UPDATE user_sessions SET last_seen_at=? WHERE id=?', (now, sid))
        conn.commit()
        return True
    finally:
        conn.close()


def _record_login_failure(row: sqlite3.Row | None, username: str) -> None:
    if not row:
        _audit('login_fail', f'username={username}')
        return
    threshold = _auth_lockout_failed_attempts()
    now = _now_str()
    locked_now = False
    conn = _db()
    try:
        latest = conn.execute('SELECT failed_login_count,is_locked FROM users WHERE id=?', (int(row['id']),)).fetchone()
        count = int((latest['failed_login_count'] if latest else row['failed_login_count']) or 0) + 1
        lock_now = count >= threshold and not bool(int((latest['is_locked'] if latest else row['is_locked']) or 0))
        if lock_now:
            conn.execute(
                """
                UPDATE users
                SET failed_login_count=?, last_failed_login_at=?, is_locked=1, locked_at=?, locked_reason=?, updated_at=?
                WHERE id=?
                """,
                (count, now, now, f'Too many failed login attempts ({threshold})', now, int(row['id'])),
            )
            _revoke_user_sessions(conn, int(row['id']))
            locked_now = True
        else:
            conn.execute(
                'UPDATE users SET failed_login_count=?, last_failed_login_at=?, updated_at=? WHERE id=?',
                (count, now, now, int(row['id'])),
            )
        conn.commit()
    finally:
        conn.close()
    if locked_now:
        _audit('user_lockout', f'id={int(row["id"])} username={username}')
    _audit('login_fail', f'username={username}')


def _record_login_success(user_id: int) -> sqlite3.Row | None:
    now = _now_str()
    conn = _db()
    try:
        conn.execute(
            'UPDATE users SET failed_login_count=0,last_failed_login_at=NULL,last_login_at=?,updated_at=? WHERE id=?',
            (now, now, int(user_id)),
        )
        conn.commit()
        return conn.execute('SELECT * FROM users WHERE id=?', (int(user_id),)).fetchone()
    finally:
        conn.close()


def _effective_permissions_for_user(user_id: int) -> list[dict[str, Any]]:
    groups = _get_user_groups(user_id)
    admin_names = [str(g['name'] or '') for g in groups if bool(int(g['is_admin'] or 0))]
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT gp.page_key,g.name
            FROM group_pages gp
            JOIN groups g ON g.id=gp.group_id
            JOIN user_groups ug ON ug.group_id=g.id
            WHERE ug.user_id=?
            ORDER BY lower(g.name)
            """,
            (int(user_id),),
        ).fetchall()
    finally:
        conn.close()
    grantors: dict[str, list[str]] = {}
    for r in rows or []:
        grantors.setdefault(str(r['page_key']), []).append(str(r['name'] or ''))
    out = []
    for key, meta in sorted(_PAGE_REGISTRY.items(), key=lambda item: str(item[1].get('name') or item[0]).lower()):
        names = list(admin_names) if admin_names else grantors.get(key, [])
        out.append({'key': key, 'name': meta.get('name') or key, 'granted_by': names})
    return out


def _admin_email_rows() -> list[sqlite3.Row]:
    conn = _db()
    try:
        return conn.execute(
            """
            SELECT DISTINCT u.id,u.username,u.full_name,u.email
            FROM users u
            JOIN user_groups ug ON ug.user_id=u.id
            JOIN groups g ON g.id=ug.group_id
            WHERE u.is_active=1 AND COALESCE(u.is_locked,0)=0 AND g.is_admin=1
              AND u.email IS NOT NULL AND trim(u.email)!=''
            ORDER BY lower(u.username)
            """
        ).fetchall()
    finally:
        conn.close()


def _effective_idle_timeout_override_minutes_for_user(user_id: int | None) -> int | None:
    """Return merged group idle override.

    None => inherit global config, 0 => disable, N => minutes. If multiple
    groups specify values, the most permissive value wins.
    """
    if user_id is None:
        return None
    groups = _get_user_groups(user_id)
    best: int | None = None
    for row in groups:
        v = row['auth_idle_timeout_minutes_override']
        if v is None:
            continue
        try:
            n = int(v)
        except Exception:
            continue
        if n <= 0:
            return 0
        if best is None or n > best:
            best = n
    return best


def _parse_idle_timeout_override_raw(raw: str | None) -> int | None:
    """Parse a group idle timeout override.

    Returns:
      None => inherit from global config
      0 => disable idle logout for this group
      N (>=1) => override minutes
    """
    try:
        s = str(raw or '').strip().lower()
    except Exception:
        s = ''
    if not s or s in ('inherit', 'default', 'global', 'none'):
        return None
    try:
        n = int(s)
    except Exception:
        return None
    # Clamp: 0 disables; otherwise 1..1440 minutes.
    if n <= 0:
        return 0
    return max(1, min(n, 24 * 60))


def _set_group_idle_timeout_override(group_id: int, raw: str | None) -> None:
    v = _parse_idle_timeout_override_raw(raw)
    conn = _db()
    try:
        conn.execute(
            'UPDATE groups SET auth_idle_timeout_minutes_override=? WHERE id=?',
            (v, int(group_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _effective_idle_timeout_minutes_for_current_user() -> int | None:
    """Return the current user's idle timeout, or None when idle logout is off."""
    cfg = _auth_cfg()
    try:
        idle_enabled = _cfg_bool(cfg, 'auth_idle_timeout_enabled', True)
    except Exception:
        idle_enabled = True
    if not idle_enabled:
        return None

    try:
        global_minutes = _cfg_int(cfg, 'auth_idle_timeout_minutes', 2, min_value=1, max_value=24 * 60)
    except Exception:
        global_minutes = 2

    try:
        if hasattr(current_user, 'idle_timeout_override'):
            group_minutes = current_user.idle_timeout_override
        else:
            group_minutes = _effective_idle_timeout_override_minutes_for_user(int(current_user.get_id()))
    except Exception:
        group_minutes = None
    if group_minutes is None:
        return global_minutes
    if group_minutes <= 0:
        return None
    return max(1, min(int(group_minutes), 24 * 60))


class _User(UserMixin):
    def __init__(self, row: sqlite3.Row):
        self.id = int(row['id'])
        self.username = str(row['username'])
        self._active = bool(int(row['is_active'] or 0))
        self.is_locked = bool(int(row['is_locked'] or 0)) if 'is_locked' in row.keys() else False
        self.force_password_change = bool(int(row['force_password_change'] or 0)) if 'force_password_change' in row.keys() else False
        try:
            groups, pages_by_group = _get_user_access_snapshot(self.id)
        except Exception:
            groups, pages_by_group = [], {}
        self.group_ids = [int(g['id']) for g in groups]
        self.group_names = [str(g['name'] or '') for g in groups]
        self.is_admin_group = any(bool(int(g['is_admin'] or 0)) for g in groups)
        self.page_keys = {
            page_key
            for group_pages in pages_by_group.values()
            for page_key in group_pages
        }

        routing_groups = [
            group for group in groups
            if 'page:routing' in pages_by_group.get(int(group['id']), set())
        ]
        digico_groups = [
            group for group in groups
            if 'page:digico_mixer' in pages_by_group.get(int(group['id']), set())
        ]
        self.videohub_allowed_outputs = [] if self.is_admin_group else _effective_group_allowlist(
            routing_groups, 'videohub_allowed_outputs'
        )
        self.videohub_allowed_inputs = [] if self.is_admin_group else _effective_group_allowlist(
            routing_groups, 'videohub_allowed_inputs'
        )
        self.digico_allowed_auxes = [] if self.is_admin_group else _effective_group_string_allowlist(
            digico_groups, 'digico_allowed_auxes'
        )
        self.idle_timeout_override = None
        for group in groups:
            value = group['auth_idle_timeout_minutes_override']
            if value is None:
                continue
            try:
                minutes = int(value)
            except Exception:
                continue
            if minutes <= 0:
                self.idle_timeout_override = 0
                break
            if self.idle_timeout_override is None or minutes > self.idle_timeout_override:
                self.idle_timeout_override = minutes

    def allows_page(self, page_key: str) -> bool:
        return bool(self.is_admin_group or str(page_key) in self.page_keys)

    def is_active(self) -> bool:
        return bool(self._active) and not bool(self.is_locked)


_PAGE_LANDING_PATHS = (
    ('page:home', '/'),
    ('page:timers', '/timers'),
    ('page:calendar', '/calendar'),
    ('page:videohub', '/videohub'),
    ('page:atem_audio', '/foyer-audio'),
    ('page:routing', '/routing'),
    ('page:media', '/media'),
    ('page:pixie_controls', '/pixie'),
    ('page:digico_mixer', '/personal-mixes'),
    ('page:surface_controls', '/surface-controls'),
    ('page:templates', '/templates'),
    ('page:api_reference', '/api-reference'),
    ('page:config', '/config'),
    ('page:console', '/console'),
    ('page:admin', '/admin/permissions'),
    ('page:account', '/account/password'),
)


def _landing_page_for_user(user: _User) -> str:
    """Return the first normal UI page available to a logged-in user."""
    for page_key, path in _PAGE_LANDING_PATHS:
        try:
            if user.allows_page(page_key):
                return path
        except Exception:
            continue
    # Password management is intentionally available to every authenticated
    # user, even when their group has no page permissions yet.
    return '/account/password'


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'


@login_manager.user_loader
def _load_user(user_id: str):
    try:
        row = _user_record(int(user_id))
        return _User(row) if row else None
    except Exception:
        return None


def can_access(page_key: str) -> bool:
    if not _auth_enabled():
        return True
    if not getattr(current_user, 'is_authenticated', False):
        return False
    try:
        allows_page = getattr(current_user, 'allows_page', None)
        if callable(allows_page):
            return bool(allows_page(page_key))
        return _user_allows_page(int(current_user.get_id()), page_key)
    except Exception:
        return False


def _csrf_token() -> str:
    tok = session.get('_csrf')
    if not tok:
        tok = secrets.token_hex(16)
        session['_csrf'] = tok
    return str(tok)


def _validate_csrf() -> bool:
    try:
        sent = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
    except RequestEntityTooLarge:
        raise
    except Exception:
        sent = None
    return bool(sent) and str(sent) == str(session.get('_csrf'))


_CLIENT_ERROR_RATE_LOCK = threading.Lock()
_CLIENT_ERROR_RATE_BUCKETS: dict[str, deque[float]] = {}
_CLIENT_ERROR_RATE_WINDOW_SECONDS = 60.0
_CLIENT_ERROR_RATE_MAX = 10
_CLIENT_ERROR_MAX_BODY_BYTES = 8192
_CLIENT_ERROR_SECRET_RE = re.compile(
    r'(?i)\b(password|passwd|token|secret|authorization|cookie|session|csrf)\b'
    r'\s*[:=]\s*([^\s,;&]+)'
)
_CLIENT_ERROR_BEARER_RE = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+\-/]+=*')
_CLIENT_ERROR_URL_QUERY_RE = re.compile(r'((?:https?://|/)[^\s?#]+)[?#][^\s]*')


def _client_error_text(value: Any, limit: int) -> str:
    text = str(value or '').replace('\x00', '')
    text = _CLIENT_ERROR_BEARER_RE.sub('Bearer [redacted]', text)
    text = _CLIENT_ERROR_SECRET_RE.sub(lambda match: f'{match.group(1)}=[redacted]', text)
    text = _CLIENT_ERROR_URL_QUERY_RE.sub(r'\1', text)
    return text[:max(0, int(limit))]


def _client_error_path(value: Any, limit: int = 240) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        parsed = urlsplit(text)
        if parsed.scheme or parsed.netloc:
            text = parsed.path or '/'
        else:
            text = text.split('?', 1)[0].split('#', 1)[0]
    except Exception:
        text = text.split('?', 1)[0].split('#', 1)[0]
    if not text.startswith('/'):
        text = '/' + text.lstrip('/')
    return _client_error_text(text, limit)


def _client_error_rate_allowed() -> bool:
    try:
        if getattr(current_user, 'is_authenticated', False):
            identity = f'user:{current_user.get_id()}'
        else:
            identity = f'ip:{request.remote_addr or "unknown"}'
    except Exception:
        identity = f'ip:{request.remote_addr or "unknown"}'
    now = time.monotonic()
    cutoff = now - _CLIENT_ERROR_RATE_WINDOW_SECONDS
    with _CLIENT_ERROR_RATE_LOCK:
        bucket = _CLIENT_ERROR_RATE_BUCKETS.setdefault(identity, deque())
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= _CLIENT_ERROR_RATE_MAX:
            return False
        bucket.append(now)
        if len(_CLIENT_ERROR_RATE_BUCKETS) > 1000:
            stale_keys = [key for key, values in _CLIENT_ERROR_RATE_BUCKETS.items() if not values or values[-1] <= cutoff]
            for key in stale_keys[:250]:
                _CLIENT_ERROR_RATE_BUCKETS.pop(key, None)
        return True


def _client_error_same_origin() -> bool:
    if str(request.headers.get('Sec-Fetch-Site') or '').strip().lower() == 'cross-site':
        return False
    origin = str(request.headers.get('Origin') or '').strip()
    if not origin:
        return True
    try:
        return urlsplit(origin).netloc.lower() == str(request.host or '').lower()
    except Exception:
        return False


def _client_error_payload(raw: Any) -> dict[str, Any]:
    body = raw if isinstance(raw, dict) else {}
    kind = str(body.get('kind') or 'error').strip().lower()
    if kind not in ('error', 'unhandledrejection'):
        kind = 'error'
    correlation_id = str(body.get('correlationId') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]{8,80}', correlation_id):
        correlation_id = uuid.uuid4().hex
    try:
        line = max(0, min(int(body.get('line') or 0), 10_000_000))
    except Exception:
        line = 0
    try:
        column = max(0, min(int(body.get('column') or 0), 10_000_000))
    except Exception:
        column = 0
    client_build_id = re.sub(
        r'[^A-Za-z0-9._-]+', '-', str(body.get('buildId') or '')
    ).strip('-')[:80]
    return {
        'kind': kind,
        'message': _client_error_text(body.get('message'), 600) or 'Unknown client error',
        'stack': _client_error_text(body.get('stack'), 2400),
        'source_path': _client_error_path(body.get('source')),
        'route': _client_error_path(body.get('route') or request.path),
        'line': line,
        'column': column,
        'client_build_id': client_build_id,
        'server_build_id': _build_id(),
        'correlation_id': correlation_id,
        # The server-observed User-Agent is used instead of accepting a
        # spoofable or accidentally sensitive arbitrary client field.
        'user_agent': _client_error_text(request.headers.get('User-Agent'), 400),
    }


@app.context_processor
def _inject_auth():
    # Flask-Login's `is_authenticated` is a property in newer versions and a
    # method in older versions. Templates/JS need a real boolean.
    try:
        v = getattr(current_user, 'is_authenticated', False)
        is_authed = bool(v() if callable(v) else v)
    except Exception:
        is_authed = False
    return {
        'auth_enabled': _auth_enabled(),
        'build_id': _build_id(),
        'can_click_companion_surface': _can_click_companion_surface_for_current_user,
        'can_access': can_access,
        'can_manage_service_tokens': _can_manage_service_tokens_for_current_user(),
        'can_manage_videohub_rooms': _can_manage_videohub_rooms_for_current_user,
        'csrf_token': _csrf_token,
        'current_user': current_user,
        'is_authenticated': is_authed,
    }


_API_MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
_api_rate_lock = threading.Lock()
_api_rate_events: dict[str, deque[float]] = {}
_api_legacy_warning_lock = threading.Lock()
_api_legacy_warning_at = 0.0


def _api_normalized_path(path: str | None = None) -> str:
    value = str(path if path is not None else (request.path or ''))
    if value == '/api/v1':
        return '/api'
    if value.startswith('/api/v1/'):
        return '/api/' + value[len('/api/v1/'):]
    return value


def _api_request_is_automation_principal() -> bool:
    try:
        principal = getattr(g, 'api_principal', None)
        return isinstance(principal, dict) and principal.get('type') in ('service_token', 'scheduler')
    except Exception:
        return False


def _api_policy(path: str, method: str) -> dict[str, Any] | None:
    """Return the required token scope and browser page capabilities.

    This is intentionally an allow-list.  A new API endpoint has no access in
    secure mode until its policy is deliberately classified here.
    """
    p = _api_normalized_path(path)
    verb = str(method or 'GET').upper()
    write = verb in _API_MUTATING_METHODS

    if p.startswith('/api/config/service-tokens'):
        return {'scope': 'admin', 'pages': ('page:config',), 'service_tokens': False}
    if p == '/api/config/atem-media':
        return {'scope': 'config', 'pages': ('page:config',), 'service_tokens': False}
    if p == '/api/atem/media/state':
        return {'scope': 'atem', 'pages': ('page:media', 'page:config'), 'service_tokens': False}
    if p == '/api/media' or p.startswith('/api/media/') or p.startswith('/api/atem/media/'):
        return {'scope': 'atem', 'pages': ('page:media',), 'service_tokens': False}
    if p.startswith('/api/admin/'):
        return {'scope': 'admin', 'pages': ('page:admin',)}
    if p.startswith('/api/config') or p.startswith('/api/companion-surfaces-config'):
        return {'scope': 'config', 'pages': ('page:config',)}
    if p == '/api/client-errors':
        # Browser-only diagnostics still pass through session, CSRF, origin,
        # request-size and write-rate enforcement before the route's tighter
        # telemetry-specific sanitization and rate limit.
        return {'scope': 'read', 'pages': (), 'service_tokens': False}
    if p.startswith('/api/activity-log'):
        return {'scope': 'admin', 'pages': ('page:console',)}
    if p.startswith('/api/hisense/config') or '/pair/' in p or p.endswith('/preflight'):
        return {'scope': 'tvs', 'pages': ('page:config',)}
    if p.startswith('/api/tvs') or p.startswith('/api/tv-targets'):
        return {'scope': 'tvs', 'pages': ('page:config',)}
    if p.startswith('/api/pixie/config') or p in ('/api/pixie/discover', '/api/pixie/homes'):
        return {'scope': 'pixie', 'pages': ('page:config',)}
    if p.startswith('/api/pixie/'):
        return {'scope': 'pixie', 'pages': ('page:pixie_controls',)}
    if p.startswith('/api/digico/setup') or p in ('/api/digico/restart', '/api/digico/discover'):
        return {'scope': 'digico', 'pages': ('page:config',)}
    if p.startswith('/api/digico/'):
        return {'scope': 'digico', 'pages': ('page:digico_mixer',)}
    if p.startswith('/api/atem/audio/'):
        return {'scope': 'atem', 'pages': ('page:atem_audio',)}
    if p == '/api/videohub/route':
        return {'scope': 'videohub', 'pages': ('page:routing',)}
    if p.startswith('/api/videohub/'):
        pages = ('page:videohub', 'page:routing') if not write else ('page:videohub',)
        return {'scope': 'videohub', 'pages': pages}
    if p.startswith('/api/timers'):
        pages = ('page:timers',) if write else ('page:timers', 'page:templates', 'page:calendar', 'page:home')
        return {'scope': 'timers', 'pages': pages}
    if p.startswith('/api/propresenter/'):
        return {'scope': 'propresenter', 'pages': ('page:timers',)}
    if p.startswith('/api/templates'):
        return {'scope': 'calendar', 'pages': ('page:templates', 'page:calendar')}
    if p.startswith('/api/events/') or p.startswith('/api/ui/events'):
        return {'scope': 'calendar', 'pages': ('page:calendar',)}
    if p.startswith('/api/ccb/'):
        return {'scope': 'ccb', 'pages': ('page:calendar',)}
    if p == '/api/upcoming_triggers':
        return {'scope': 'read', 'pages': ('page:home', 'page:calendar')}
    if p == '/api/home/overview':
        return {'scope': 'read', 'pages': ('page:home',)}
    if p in {
        '/api/companion_status', '/api/propresenter_status', '/api/videohub_status',
        '/api/digico_status', '/api/atem_status', '/api/status/summary',
        '/api/scheduler_status',
    }:
        return {'scope': 'read', 'pages': ()}
    return None


def _api_scheduler_path_denied(path: str) -> bool:
    """Keep scheduled actions out of credential and setup administration."""
    p = _api_normalized_path(path)
    if p.startswith(('/api/admin/', '/api/config', '/api/companion-surfaces-config', '/api/activity-log')):
        return True
    if p == '/api/client-errors':
        return True
    if p.startswith('/api/hisense/config') or '/pair/' in p or p.endswith('/preflight'):
        return True
    if p.startswith('/api/pixie/config') or p in ('/api/pixie/discover', '/api/pixie/homes'):
        return True
    if p.startswith('/api/digico/setup') or p in ('/api/digico/restart', '/api/digico/discover'):
        return True
    return False


def _api_scheduler_internal_gate():
    """Authorize only the private in-process Calendar scheduler principal."""
    marker = getattr(g, '_tdeck_scheduler_principal', None)
    if not isinstance(marker, dict) or marker.get('type') != 'scheduler':
        return _api_json_error(403, 'forbidden', 'The internal scheduler principal is invalid.')

    normalized = _api_normalized_path()
    policy = _api_policy(normalized, request.method)
    if (
        policy is None
        or policy.get('service_tokens') is False
        or str(policy.get('scope') or '') in ('admin', 'config')
        or _api_scheduler_path_denied(normalized)
    ):
        _api_security_event(
            'security.api.scheduler_scope_denied',
            'Denied an internal scheduler action outside its operational API boundary',
            status='failure',
            details={'path': normalized, 'method': request.method, 'endpoint': request.endpoint},
        )
        return _api_json_error(403, 'forbidden', 'The scheduler cannot call this API capability.')

    within_limit, limit = _api_request_within_size_limit(normalized)
    if not within_limit:
        return _api_json_error(413, 'request_too_large', f'Request body exceeds the {limit}-byte limit.')

    g.api_principal = dict(marker)
    return None


def _api_json_error(status: int, code: str, message: str, **extra):
    payload = {'ok': False, 'error': str(code), 'message': str(message)}
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = int(status)
    if int(status) == 401:
        response.headers['WWW-Authenticate'] = 'Bearer realm="TDeck API"'
    return response


def _api_security_event(action: str, summary: str, *, status: str = 'warning', details=None) -> None:
    try:
        log_event(
            action,
            summary,
            source='api',
            status=status,
            target_type='api_security',
            details=details or {},
        )
    except Exception:
        pass


def _api_legacy_anonymous_active() -> tuple[bool, str]:
    cfg = _auth_cfg()
    if not bool(cfg.get('api_legacy_anonymous_enabled', False)):
        return False, 'disabled'
    raw_expiry = str(cfg.get('api_legacy_anonymous_until') or '').strip()
    if not raw_expiry:
        return False, 'missing_expiry'
    try:
        expiry_ts = float(raw_expiry)
    except Exception:
        try:
            expiry = datetime.fromisoformat(raw_expiry.replace('Z', '+00:00'))
            expiry_ts = expiry.timestamp()
        except Exception:
            return False, 'invalid_expiry'
    remaining = expiry_ts - time.time()
    if remaining <= 0:
        return False, 'expired'
    # The escape hatch is intentionally impossible to configure as a
    # permanent mode.  Operators can renew it explicitly while migrating.
    if remaining > (31 * 24 * 60 * 60):
        return False, 'expiry_too_far_away'
    return True, datetime.fromtimestamp(expiry_ts).isoformat(timespec='seconds')


def _api_log_legacy_warning(expiry: str) -> None:
    global _api_legacy_warning_at
    now = time.monotonic()
    with _api_legacy_warning_lock:
        if now - _api_legacy_warning_at < 300:
            return
        _api_legacy_warning_at = now
    _api_security_event(
        'security.api.legacy_anonymous',
        'Accepted an anonymous API request through the temporary migration flag',
        details={'path': _api_normalized_path(), 'method': request.method, 'expires': expiry},
    )


def _api_legacy_path_allowed(path: str, method: str) -> bool:
    """Limit the migration escape hatch to the pre-existing Companion API."""
    p = _api_normalized_path(path)
    verb = str(method or 'GET').upper()
    exact = {
        ('GET', '/api/status/summary'),
        ('GET', '/api/home/overview'),
        ('GET', '/api/timers'),
        ('POST', '/api/timers/apply'),
        ('POST', '/api/timers/preset'),
        ('GET', '/api/videohub/presets'),
        ('GET', '/api/videohub/state'),
        ('POST', '/api/videohub/route'),
        ('GET', '/api/tvs'),
        ('GET', '/api/ccb/status'),
        ('GET', '/api/ccb/services/upcoming'),
        ('POST', '/api/ccb/services/refresh'),
        ('POST', '/api/ccb/access/clear'),
    }
    if (verb, p) in exact:
        return True
    patterns = (
        ('POST', r'/api/videohub/presets/\d+/apply'),
        ('GET', r'/api/tvs/[^/]+/state'),
        ('POST', r'/api/tvs/[^/]+/(?:power|volume|source|reconnect)'),
        ('GET', r'/api/tv-targets/[^/]+/state'),
        ('POST', r'/api/tv-targets/[^/]+/(?:power|volume|source|reconnect)'),
        ('POST', r'/api/ccb/services/[^/]+/(?:pull|apply|pull-and-apply)'),
    )
    return any(verb == allowed_method and re.fullmatch(pattern, p) for allowed_method, pattern in patterns)


def _api_same_origin_request() -> bool:
    cfg = _auth_cfg()
    configured = cfg.get('api_trusted_origins') or []
    if isinstance(configured, str):
        configured = [part.strip() for part in configured.split(',') if part.strip()]
    allowed = {str(value).rstrip('/') for value in configured if str(value).strip()}
    try:
        current = urlsplit(request.url_root)
        allowed.add(f'{current.scheme}://{current.netloc}')
    except Exception:
        pass
    supplied = str(request.headers.get('Origin') or '').strip()
    if not supplied:
        supplied = str(request.headers.get('Referer') or '').strip()
    if not supplied:
        return False
    try:
        candidate = urlsplit(supplied)
        origin = f'{candidate.scheme}://{candidate.netloc}'.rstrip('/')
    except Exception:
        return False
    return origin in allowed


def _api_request_within_size_limit(path: str) -> tuple[bool, int]:
    cfg = _auth_cfg()
    upload = path.startswith('/api/config/import/') or path.endswith('/background') or path == '/api/media/upload'
    key = 'api_upload_max_request_bytes' if upload else 'api_max_request_bytes'
    default_limit = 64 * 1024 * 1024 if upload else 2 * 1024 * 1024
    try:
        limit = max(1024, int(cfg.get(key, default_limit)))
    except Exception:
        limit = default_limit
    length = request.content_length
    return (length is None or int(length) <= limit), limit


def _api_rate_limit(principal_key: str) -> tuple[bool, int]:
    cfg = _auth_cfg()
    try:
        limit = max(10, min(10000, int(cfg.get('api_write_rate_limit_per_minute', 600))))
    except Exception:
        limit = 600
    now = time.monotonic()
    cutoff = now - 60.0
    with _api_rate_lock:
        events = _api_rate_events.setdefault(principal_key, deque())
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= limit:
            retry_after = max(1, int(61 - (now - events[0])))
            return False, retry_after
        events.append(now)
        if len(_api_rate_events) > 2000:
            stale = [key for key, values in _api_rate_events.items() if not values or values[-1] <= cutoff]
            for key in stale[:500]:
                _api_rate_events.pop(key, None)
    return True, 0


def _api_browser_resource_check(path: str, method: str):
    """Enforce resource capabilities that are narrower than page access."""
    p = _api_normalized_path(path)
    write = str(method).upper() in _API_MUTATING_METHODS
    try:
        user_id = int(current_user.get_id())
    except Exception:
        return _api_json_error(403, 'forbidden', 'The session has no valid user identity.')

    if p.startswith('/api/config/service-tokens') and not _user_is_admin(user_id):
        return _api_json_error(403, 'forbidden', 'Only administrators can manage API tokens.')

    if write and p.startswith('/api/videohub/'):
        is_apply = bool(re.fullmatch(r'/api/videohub/presets/\d+/apply', p))
        is_route = p == '/api/videohub/route'
        if not is_apply and not is_route and not _effective_videohub_can_edit_presets_for_user(user_id):
            return _api_json_error(403, 'forbidden', 'VideoHub editing is not assigned to your group.')
    preset_match = re.fullmatch(r'/api/videohub/presets/(\d+)/apply', p)
    if preset_match:
        allowed = _effective_videohub_preset_ids_for_user(user_id)
        if allowed and int(preset_match.group(1)) not in set(allowed):
            return _api_json_error(403, 'forbidden', 'This VideoHub preset is not assigned to your group.')
    if write and p == '/api/videohub/route':
        body = request.get_json(silent=True) or {}
        try:
            output_n = int(body.get('output') or body.get('destination') or request.args.get('output') or request.args.get('destination'))
            input_n = int(body.get('input') or body.get('source') or request.args.get('input') or request.args.get('source'))
            if bool(body.get('zero_based') or body.get('zerobased')):
                output_n += 1
                input_n += 1
        except Exception:
            return None  # The endpoint will return its normal validation error.
        allowed_outputs, allowed_inputs = _effective_videohub_allowlists_for_user(user_id)
        if allowed_outputs and output_n not in set(allowed_outputs):
            return _api_json_error(403, 'forbidden', 'This VideoHub output is not assigned to your group.')
        if allowed_inputs and input_n not in set(allowed_inputs):
            return _api_json_error(403, 'forbidden', 'This VideoHub input is not assigned to your group.')
    if write and p.startswith('/api/atem/audio/'):
        if _user_is_admin(user_id):
            return None
        body = request.get_json(silent=True) or {}
        if p.endswith('/monitor'):
            if not _effective_atem_can_monitor_audio_for_user(user_id):
                return _api_json_error(403, 'forbidden', 'ATEM monitor control is not assigned to your group.')
            return None
        source_id = str(body.get('source_id') or body.get('source') or '').strip()
        allowed_sources = set(_effective_atem_audio_source_ids_for_user(user_id))
        if not source_id or source_id not in allowed_sources:
            return _api_json_error(403, 'forbidden', 'This ATEM audio source is not assigned to your group.')
        if p.endswith('/solo') and not _effective_atem_can_solo_audio_for_user(user_id):
            return _api_json_error(403, 'forbidden', 'ATEM solo control is not assigned to your group.')
    return None


def _api_service_token_constraint_error(token_record: dict, path: str, method: str):
    constraints = token_record.get('constraints') if isinstance(token_record.get('constraints'), dict) else {}
    if not constraints:
        return None
    p = _api_normalized_path(path)
    verb = str(method or 'GET').upper()
    allowed_paths = constraints.get('allowed_paths') or []
    if allowed_paths and not any(
        fnmatch.fnmatchcase(f'{verb} {p}', str(pattern)) for pattern in allowed_paths
    ):
        return _api_json_error(403, 'token_constraint_denied', 'The service token does not allow this API action.')

    tv_targets = {str(value) for value in (constraints.get('tv_targets') or [])}
    tv_match = re.match(r'/api/(?:tvs|tv-targets)/([^/]+)', p)
    if tv_targets and tv_match:
        target = unquote(tv_match.group(1))
        if target not in tv_targets and f'tv:{target}' not in tv_targets:
            return _api_json_error(403, 'token_constraint_denied', 'The service token does not allow this TV target.')

    preset_ids = {int(value) for value in (constraints.get('videohub_presets') or [])}
    preset_match = re.fullmatch(r'/api/videohub/presets/(\d+)/apply', p)
    if preset_ids and preset_match and int(preset_match.group(1)) not in preset_ids:
        return _api_json_error(403, 'token_constraint_denied', 'The service token does not allow this VideoHub preset.')
    if p == '/api/videohub/route' and (constraints.get('videohub_outputs') or constraints.get('videohub_inputs')):
        body = request.get_json(silent=True) or {}
        try:
            output_n = int(body.get('output') or body.get('destination') or request.args.get('output') or request.args.get('destination'))
            input_n = int(body.get('input') or body.get('source') or request.args.get('input') or request.args.get('source'))
            if bool(body.get('zero_based') or body.get('zerobased')):
                output_n += 1
                input_n += 1
        except Exception:
            return None
        outputs = {int(value) for value in (constraints.get('videohub_outputs') or [])}
        inputs = {int(value) for value in (constraints.get('videohub_inputs') or [])}
        if outputs and output_n not in outputs:
            return _api_json_error(403, 'token_constraint_denied', 'The service token does not allow this VideoHub output.')
        if inputs and input_n not in inputs:
            return _api_json_error(403, 'token_constraint_denied', 'The service token does not allow this VideoHub input.')

    atem_sources = {str(value) for value in (constraints.get('atem_sources') or [])}
    if atem_sources and verb in _API_MUTATING_METHODS and p.startswith('/api/atem/audio/') and not p.endswith('/monitor'):
        body = request.get_json(silent=True) or {}
        source_id = str(body.get('source_id') or body.get('source') or '').strip()
        if source_id and source_id not in atem_sources:
            return _api_json_error(403, 'token_constraint_denied', 'The service token does not allow this ATEM source.')
    return None


def _api_security_gate():
    if not _auth_enabled():
        g.api_principal = {'type': 'auth_disabled', 'key': 'auth-disabled'}
        return None

    normalized = _api_normalized_path()
    policy = _api_policy(normalized, request.method)
    if policy is None:
        _api_security_event(
            'security.api.policy_missing',
            'Denied an API endpoint without an explicit security policy',
            status='failure',
            details={'path': normalized, 'method': request.method, 'endpoint': request.endpoint},
        )
        return _api_json_error(403, 'forbidden', 'This API endpoint has no configured access policy.')

    within_limit, limit = _api_request_within_size_limit(normalized)
    if not within_limit:
        _api_security_event(
            'security.api.request_too_large',
            'Rejected an oversized API request',
            details={'path': normalized, 'method': request.method, 'limit_bytes': limit, 'content_length': request.content_length},
        )
        return _api_json_error(413, 'request_too_large', f'Request body exceeds the {limit}-byte limit.')

    authorization = str(request.headers.get('Authorization') or '').strip()
    principal = None
    if authorization:
        scheme, separator, credential = authorization.partition(' ')
        if not separator or scheme.lower() != 'bearer' or not credential.strip():
            _api_security_event('security.api.invalid_authorization', 'Rejected a malformed API Authorization header')
            return _api_json_error(401, 'invalid_token', 'A valid Bearer service token is required.')
        token_record, token_error = authenticate_service_token(_AUTH_DB_PATH, credential.strip())
        if token_record is None:
            _api_security_event(
                'security.api.token_rejected',
                'Rejected an invalid, expired, or revoked service token',
                details={'reason': token_error, 'path': normalized, 'method': request.method},
            )
            return _api_json_error(401, token_error or 'invalid_token', 'The service token is invalid, expired, or revoked.')
        if policy.get('service_tokens') is False:
            return _api_json_error(403, 'forbidden', 'This endpoint accepts browser sessions only.')
        read_only = request.method in ('GET', 'HEAD')
        if not token_allows(token_record.get('scopes') or [], str(policy['scope']), read_only=read_only):
            _api_security_event(
                'security.api.token_scope_denied',
                'Denied a service token without the required scope',
                details={
                    'token_id': token_record.get('id'),
                    'token_name': token_record.get('name'),
                    'required_scope': policy['scope'],
                    'path': normalized,
                    'method': request.method,
                },
            )
            return _api_json_error(403, 'insufficient_scope', f"The service token requires the '{policy['scope']}' scope.")
        constraint_error = _api_service_token_constraint_error(token_record, normalized, request.method)
        if constraint_error is not None:
            _api_security_event(
                'security.api.token_constraint_denied',
                'Denied a service token outside its target/action constraints',
                details={'token_id': token_record.get('id'), 'token_name': token_record.get('name'), 'path': normalized, 'method': request.method},
            )
            return constraint_error
        principal = {
            'type': 'service_token',
            'key': f"token:{token_record['id']}",
            'id': token_record['id'],
            'name': token_record['name'],
            'scopes': token_record.get('scopes') or [],
            'constraints': token_record.get('constraints') or {},
        }
    elif getattr(current_user, 'is_authenticated', False):
        try:
            idle_minutes = _effective_idle_timeout_minutes_for_current_user()
            last_activity = int(session.get('_last_activity') or 0)
            if idle_minutes is not None and last_activity and (int(time.time()) - last_activity) > (idle_minutes * 60):
                logout_user()
                session.clear()
                _api_security_event('security.api.session_idle', 'Rejected an API request from an expired browser session')
                return _api_json_error(401, 'session_expired', 'The browser session expired due to inactivity.')
        except Exception:
            pass
        try:
            if not _touch_current_user_session():
                raise PermissionError('session revoked')
        except Exception:
            logout_user()
            session.clear()
            _api_security_event('security.api.session_rejected', 'Rejected a revoked or invalid browser session')
            return _api_json_error(401, 'unauthorized', 'The browser session is no longer valid.')
        try:
            user_id = int(current_user.get_id())
        except Exception:
            return _api_json_error(401, 'unauthorized', 'A logged-in browser session is required.')
        try:
            row = _user_record(user_id)
            if row and bool(int(row['force_password_change'] or 0)):
                return _api_json_error(403, 'password_change_required', 'Change your password before using the API.')
        except Exception:
            pass
        pages = tuple(policy.get('pages') or ())
        if pages and not any(can_access(page_key) for page_key in pages):
            _api_security_event(
                'security.api.page_denied',
                'Denied an API request outside the user’s page permissions',
                details={'user_id': user_id, 'required_pages': pages, 'path': normalized, 'method': request.method},
            )
            return _api_json_error(403, 'forbidden', 'Your groups do not grant access to this API capability.')
        if request.method in _API_MUTATING_METHODS:
            if not _validate_csrf():
                _api_security_event(
                    'security.api.csrf_denied',
                    'Rejected a browser API write with an invalid CSRF token',
                    details={'user_id': user_id, 'path': normalized, 'method': request.method},
                )
                return _api_json_error(403, 'invalid_csrf', 'A valid CSRF token is required.')
            if not _api_same_origin_request():
                _api_security_event(
                    'security.api.origin_denied',
                    'Rejected a browser API write from an untrusted origin',
                    details={'user_id': user_id, 'path': normalized, 'method': request.method},
                )
                return _api_json_error(403, 'invalid_origin', 'The request Origin or Referer is not trusted.')
        resource_error = _api_browser_resource_check(normalized, request.method)
        if resource_error is not None:
            return resource_error
        principal = {'type': 'browser_session', 'key': f'user:{user_id}', 'id': user_id, 'name': str(getattr(current_user, 'username', '') or '')}
    else:
        legacy_enabled, legacy_detail = _api_legacy_anonymous_active()
        if not legacy_enabled:
            if legacy_detail not in ('disabled', 'expired'):
                _api_security_event(
                    'security.api.legacy_flag_invalid',
                    'The legacy anonymous API flag was ignored because its expiry is unsafe or invalid',
                    details={'reason': legacy_detail},
                )
            return _api_json_error(401, 'unauthorized', 'Log in or provide a scoped Bearer service token.')
        if not _api_legacy_path_allowed(normalized, request.method):
            _api_security_event(
                'security.api.legacy_scope_denied',
                'Denied a legacy-anonymous request outside the Companion migration allow-list',
                details={'path': normalized, 'method': request.method},
            )
            return _api_json_error(403, 'forbidden', 'The temporary legacy flag does not allow this endpoint.')
        _api_log_legacy_warning(legacy_detail)
        principal = {'type': 'legacy_anonymous', 'key': f"legacy:{request.remote_addr or 'unknown'}", 'name': 'Legacy anonymous API'}

    g.api_principal = principal
    if request.method in _API_MUTATING_METHODS:
        allowed, retry_after = _api_rate_limit(str(principal['key']))
        if not allowed:
            _api_security_event(
                'security.api.rate_limited',
                'Rate-limited repeated API writes',
                details={'principal_type': principal.get('type'), 'principal_id': principal.get('id'), 'path': normalized},
            )
            response = _api_json_error(429, 'rate_limited', 'Too many API writes. Retry later.', retryAfter=retry_after)
            response.headers['Retry-After'] = str(retry_after)
            return response
    return None


@app.before_request
def _auth_gate():
    p = request.path or ''
    if _api_normalized_path(p) == '/api/media/upload':
        # Enforce streaming multipart size before authentication/CSRF accesses
        # request.form, including bodies with no Content-Length header.
        from media_library import MAX_UPLOAD_BYTES
        _, upload_limit = _api_request_within_size_limit('/api/media/upload')
        request.max_content_length = min(upload_limit, MAX_UPLOAD_BYTES + 1024 * 1024)

    # Only the in-process dispatcher can place this marker on Flask's request
    # context. It is not derived from an address, header, cookie, or payload.
    if p.startswith('/api/') and isinstance(getattr(g, '_tdeck_scheduler_principal', None), dict):
        return _api_scheduler_internal_gate()

    if not _auth_enabled():
        return None

    # API requests have a separate boundary for human sessions and automation.
    if p.startswith('/api/'):
        return _api_security_gate()

    # Always allow static + login/logout assets/pages.
    if (
        p.startswith('/static/')
        or p.startswith('/media/videohub_room_images/')
        or p == '/login'
        or p == '/logout'
    ):
        return None

    # Ensure auth DB + defaults exist when auth is enabled
    try:
        _bootstrap_default_users_roles()
    except Exception:
        pass

    if not getattr(current_user, 'is_authenticated', False):
        nxt = request.full_path if request.query_string else request.path
        return redirect(url_for('login_page', next=nxt))

    try:
        if not _touch_current_user_session():
            _audit('logout_session_revoked', f'path={p}')
            logout_user()
            session.clear()
            return redirect(url_for('login_page', next=p))
    except Exception:
        logout_user()
        session.clear()
        return redirect(url_for('login_page', next=p))

    # Idle timeout
    now = int(time.time())
    idle_minutes = _effective_idle_timeout_minutes_for_current_user()
    if idle_minutes is not None:
        last = int(session.get('_last_activity') or 0)
        if last and (now - last) > (idle_minutes * 60):
            try:
                _audit('logout_idle', f'idle_minutes={idle_minutes}')
            except Exception:
                pass
            logout_user()
            session.clear()
            return redirect(url_for('login_page', timeout=1))

    # Don't let background heartbeat requests keep the session alive.
    if p != '/auth/ping':
        session['_last_activity'] = now

    # Auth heartbeat endpoints: allow without page-key authorization.
    # - /auth/ping: detects timeout and redirects via the idle check above
    # - /auth/touch: refreshes last-activity when the user interacts on a page
    if p == '/auth/ping':
        return ('', 204)
    if p == '/auth/touch':
        try:
            session['_last_activity'] = int(time.time())
        except Exception:
            pass
        return ('', 204)

    try:
        row = _user_record(int(current_user.get_id()))
        if row and bool(int(row['force_password_change'] or 0)) and p != '/account/password':
            return redirect(url_for('account_password_page', force=1))
    except Exception:
        pass

    # CSRF protect non-API mutating requests
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if not _validate_csrf():
            _audit('csrf_fail', f'path={p}')
            return abort(400)

    # Authorization for pages
    view_fn = app.view_functions.get(request.endpoint)
    page_key = getattr(view_fn, '_required_page_key', None) if view_fn else None
    if p == '/account/password':
        return None
    if not page_key:
        _audit('deny_missing_page_key', f'endpoint={request.endpoint} path={p}')
        return abort(403)

    if not can_access(str(page_key)):
        _audit('deny_page', f'page={page_key} path={p}')
        return abort(403)

    return None


@app.errorhandler(RequestEntityTooLarge)
def _request_entity_too_large(error):
    if (request.path or '').startswith('/api/'):
        _api_security_event('security.api.request_too_large', 'Rejected an oversized API request',
                            details={'path': _api_normalized_path()})
        return _api_json_error(413, 'request_too_large', 'The upload exceeds the request size limit.')
    return error


@app.route('/auth/ping', methods=['GET'])
def auth_ping():
    # Handled by _auth_gate (returns 204 or redirects on timeout)
    return ('', 204)


@app.route('/auth/touch', methods=['GET'])
def auth_touch():
    # Handled by _auth_gate (refreshes last-activity and returns 204)
    return ('', 204)


@app.post('/api/client-errors')
def api_client_errors():
    """Accept a small, sanitized same-origin browser error report."""
    if _auth_enabled():
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'authentication_required'}), 401
        if not _validate_csrf():
            return jsonify({'ok': False, 'error': 'invalid_csrf'}), 400
    if not _client_error_same_origin():
        return jsonify({'ok': False, 'error': 'cross_site_request'}), 403
    if request.mimetype != 'application/json':
        return jsonify({'ok': False, 'error': 'json_required'}), 415
    if int(request.content_length or 0) > _CLIENT_ERROR_MAX_BODY_BYTES:
        return jsonify({'ok': False, 'error': 'payload_too_large'}), 413
    if not _client_error_rate_allowed():
        response = jsonify({'ok': False, 'error': 'rate_limited'})
        response.headers['Retry-After'] = str(int(_CLIENT_ERROR_RATE_WINDOW_SECONDS))
        return response, 429

    raw_report = request.get_json(silent=True)
    if not isinstance(raw_report, dict):
        return jsonify({'ok': False, 'error': 'invalid_report'}), 400
    report = _client_error_payload(raw_report)
    try:
        log_event(
            'client.error',
            f"Browser {report['kind']}: {report['message'][:180]}",
            source='web',
            status='warning',
            target_type='browser',
            target_id=report['client_build_id'] or report['server_build_id'],
            details=report,
            request_path=report['route'],
        )
    except Exception:
        # Telemetry must never turn a client-side issue into a page/API outage.
        return jsonify({'ok': False, 'error': 'telemetry_unavailable'}), 503
    return jsonify({'ok': True, 'requestId': report['correlation_id']}), 202


@app.context_processor
def _inject_theme():
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
        dark_mode = bool(cfg.get('dark_mode', False))
    except Exception:
        dark_mode = False
        cfg = {}
    try:
        timeout_s = int(cfg.get('webui_message_timeout_seconds', 4))
    except Exception:
        timeout_s = 4
    timeout_s = max(0, min(timeout_s, 600))
    return {
        'dark_mode': dark_mode,
        'bs_theme': 'dark' if dark_mode else 'light',
        'message_timeout_ms': int(timeout_s * 1000),
    }


@app.context_processor
def _inject_companion_surfaces():
    return {
        'companion_surface_by_id': _companion_surface_by_id,
        'companion_surface_url': _companion_surface_url,
    }


@app.errorhandler(400)
def _handle_bad_request(err):
    try:
        if (request.path or '').startswith('/api/'):
            return jsonify({'error': 'bad_request'}), 400
    except Exception:
        pass
    return render_template('bad_request.html'), 400


@app.errorhandler(403)
def _handle_forbidden(err):
    try:
        if (request.path or '').startswith('/api/'):
            return jsonify({'error': 'forbidden'}), 403
    except Exception:
        pass
    return render_template('access_denied.html'), 403


# --- Low-level stdout/stderr capture (diagnostic only) ---
_CONSOLE_MAX_LINES = 2000
_console_lock = threading.Lock()
_console_lines: deque[tuple[int, str, str]] = deque(maxlen=_CONSOLE_MAX_LINES)
_console_seq = 0


def _console_append(text: str) -> None:
    """Append text to the in-memory diagnostic buffer.

    The buffer stores newline-terminated lines for convenient rendering.
    """
    global _console_seq
    if text is None:
        return
    s = str(text)
    if not s:
        return
    with _console_lock:
        for line in s.splitlines(True):
            _console_seq += 1
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _console_lines.append((_console_seq, ts, line))


def _is_debug_enabled() -> bool:
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
        return bool(cfg.get('debug', False))
    except Exception:
        return False


def _truncate_for_log(value, max_len: int = 240) -> str:
    try:
        s = json.dumps(value, ensure_ascii=False)
    except Exception:
        try:
            s = str(value)
        except Exception:
            s = ''
    if len(s) > max_len:
        return s[:max_len] + '…'
    return s

_CONFIG_IMPORT_UPLOADS: dict[str, dict[str, Any]] = {}
_CONFIG_IMPORT_UPLOAD_LOCK = threading.Lock()


def _config_transport_log(message: str) -> None:
    line = f"[CONFIG] {message}"
    try:
        print(line)
    except Exception:
        pass
    try:
        log_event(
            'config.transport',
            line,
            source='web' if _activity_request_path() else 'system',
            status='info',
            target_type='config',
            details={'message': message},
        )
    except Exception:
        pass


def _config_access_error_json():
    if _auth_enabled():
        if _api_request_is_automation_principal():
            return None
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        if not can_access('page:config'):
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
    return None


def _safe_relpath(path: Path) -> str:
    try:
        return path.resolve().relative_to(_APP_ROOT.resolve()).as_posix()
    except Exception:
        return path.name


def _resolve_transport_path(relpath: str) -> Path | None:
    rel = str(relpath or '').replace('\\', '/').strip().lstrip('/')
    if not rel or rel.startswith('../') or '/../' in rel or rel == '..':
        return None
    try:
        out = (_APP_ROOT / rel).resolve()
        out.relative_to(_APP_ROOT.resolve())
    except Exception:
        return None
    return out


def _transport_file_item(item_id: str, label: str, relpath: str, description: str, *, actual_path: Path | None = None) -> dict[str, Any]:
    path = actual_path or _resolve_transport_path(relpath) or (_APP_ROOT / Path(relpath).name)
    item = {
        'id': item_id,
        'label': label,
        'kind': 'file',
        'path': relpath.replace('\\', '/').lstrip('/') if actual_path is not None else _safe_relpath(path),
        'description': description,
        'available': path.exists() and path.is_file(),
        'size': path.stat().st_size if path.exists() and path.is_file() else 0,
    }
    if actual_path is not None:
        item['_actual_path'] = str(actual_path)
    return item


def _transport_dir_item(item_id: str, label: str, relpath: str, description: str) -> dict[str, Any]:
    path = _resolve_transport_path(relpath) or (_APP_ROOT / Path(relpath).name)
    count = 0
    size = 0
    if path.exists() and path.is_dir():
        for child in path.rglob('*'):
            try:
                if child.is_file():
                    count += 1
                    size += child.stat().st_size
            except Exception:
                pass
    return {
        'id': item_id,
        'label': label,
        'kind': 'directory',
        'path': _safe_relpath(path),
        'description': description,
        'available': path.exists() and path.is_dir() and count > 0,
        'size': size,
        'file_count': count,
    }


def _config_transport_items() -> list[dict[str, Any]]:
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    items: list[dict[str, Any]] = [
        _transport_file_item('config', 'App config', 'config.json', 'Main TDeck settings, ports, integrations, theme, and auth options.'),
        _transport_file_item('events', 'Calendar events', str(cfg.get('EVENTS_FILE') or 'events.json'), 'Scheduled calendar events and their trigger definitions.'),
        _transport_file_item('timer_presets', 'Timer presets', str(getattr(utils, 'TIMER_PRESETS_FILE', 'timer_presets.json')), 'Timer preset names, times, and Companion button actions.'),
        _transport_file_item('trigger_templates', 'Trigger templates', 'trigger_templates.json', 'Reusable trigger templates for calendar events.'),
        _transport_file_item('button_templates', 'Button templates', 'button_templates.json', 'Reusable Companion button templates.'),
        _transport_file_item('calendar_triggers', 'Generated calendar triggers', 'calendar_triggers.json', 'Current generated trigger queue/state for the scheduler.'),
        _transport_file_item('companion_surfaces', 'Companion surfaces', 'companion_surfaces.json', 'Configured Companion surface catalogue and display slots.'),
        _transport_file_item('videohub_presets', 'VideoHub presets', str(cfg.get('videohub_presets_file') or 'videohub_presets.json'), 'VideoHub preset routes, names, and locks.'),
        _transport_file_item('videohub_rooms', 'VideoHub rooms', 'videohub_rooms.json', 'Global VideoHub room layout, output placement, backgrounds, and input filters.'),
        _transport_file_item('home_state', 'Home dashboard state', 'home_state.json', 'Last-known Home dashboard state used by the overview page.', actual_path=_home_state_path()),
        _transport_file_item('auth_db', 'Users database', _safe_relpath(_AUTH_DB_PATH), 'Users, groups, permissions, sessions, password hashes, and account security state.'),
        _transport_dir_item('videohub_room_images', 'VideoHub room media', 'videohub_room_images', 'Uploaded room background images and other local room media.'),
    ]

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get('path') or item.get('id') or '')
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _config_transport_item_map() -> dict[str, dict[str, Any]]:
    return {str(item.get('id')): item for item in _config_transport_items()}


def _expand_config_transport_selection(item_ids: list[str], item_map: dict[str, dict[str, Any]]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for raw in item_ids or []:
        item_id = str(raw or '').strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        selected.append(item_id)

    # Room backgrounds are referenced by videohub_rooms.json. Keep those files
    # travelling with the room config whenever the export/import package has them.
    if 'videohub_rooms' in seen and 'videohub_room_images' in item_map and 'videohub_room_images' not in seen:
        selected.append('videohub_room_images')
    return selected


def _zip_write_sqlite_backup(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
    with NamedTemporaryFile(prefix='tdeck-auth-', suffix='.db', delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        src = sqlite3.connect(str(path))
        dst = sqlite3.connect(str(tmp_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        zf.write(tmp_path, arcname)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _write_transport_item_to_zip(zf: zipfile.ZipFile, item: dict[str, Any], *, root: str = 'payload') -> bool:
    actual = str(item.get('_actual_path') or '').strip()
    path = Path(actual) if actual else _resolve_transport_path(str(item.get('path') or ''))
    if path is None or not path.exists():
        return False
    rel = str(item.get('path') or path.name).replace('\\', '/').lstrip('/')
    if item.get('kind') == 'directory':
        if not path.is_dir():
            return False
        wrote = False
        for child in path.rglob('*'):
            try:
                if not child.is_file():
                    continue
                child_rel = child.relative_to(_APP_ROOT).as_posix()
                zf.write(child, f'{root}/{child_rel}')
                wrote = True
            except Exception as e:
                _config_transport_log(f"Skipped {child}: {e}")
        return wrote

    if not path.is_file():
        return False
    if path.resolve() == _AUTH_DB_PATH.resolve():
        _zip_write_sqlite_backup(zf, path, f'{root}/{rel}')
    else:
        zf.write(path, f'{root}/{rel}')
    return True


def _create_config_transport_zip(item_ids: list[str], *, reason: str) -> tuple[bytes, list[dict[str, Any]]]:
    item_map = _config_transport_item_map()
    expanded_ids = _expand_config_transport_selection(item_ids, item_map)
    selected = [item_map[i] for i in expanded_ids if i in item_map and item_map[i].get('available')]
    manifest = {
        'format': 'tdeck-config-transport',
        'version': 1,
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'reason': reason,
        'items': [],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for item in selected:
            wrote = _write_transport_item_to_zip(zf, item)
            if wrote:
                manifest['items'].append({
                    'id': item.get('id'),
                    'label': item.get('label'),
                    'kind': item.get('kind'),
                    'path': item.get('path'),
                    'description': item.get('description'),
                    'size': item.get('size', 0),
                    'file_count': item.get('file_count', 1 if item.get('kind') == 'file' else 0),
                })
        zf.writestr('manifest.json', json.dumps(manifest, indent=2, ensure_ascii=False))
    return buf.getvalue(), list(manifest['items'])


def _inspect_config_transport_zip(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path, 'r') as zf:
        try:
            raw = zf.read('manifest.json')
        except KeyError:
            raise ValueError('This zip does not contain a TDeck config manifest.')
        manifest = json.loads(raw.decode('utf-8'))
        if not isinstance(manifest, dict) or manifest.get('format') != 'tdeck-config-transport':
            raise ValueError('This is not a TDeck config export.')
        names = set(zf.namelist())
    items = []
    for item in manifest.get('items') or []:
        if not isinstance(item, dict):
            continue
        rel = str(item.get('path') or '').replace('\\', '/').lstrip('/')
        if not rel:
            continue
        kind = str(item.get('kind') or 'file')
        if kind == 'directory':
            has_payload = any(n.startswith(f'payload/{rel.rstrip("/")}/') for n in names)
        else:
            has_payload = f'payload/{rel}' in names
        if has_payload:
            item = dict(item)
            item['available'] = True
            items.append(item)
    manifest['items'] = items
    return manifest


def _remember_config_import_upload(path: Path, manifest: dict[str, Any]) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _CONFIG_IMPORT_UPLOAD_LOCK:
        expired = [k for k, v in _CONFIG_IMPORT_UPLOADS.items() if now - float(v.get('ts') or 0) > 3600]
        for k in expired:
            old = _CONFIG_IMPORT_UPLOADS.pop(k, None)
            try:
                Path(str(old.get('path'))).unlink(missing_ok=True)
            except Exception:
                pass
        _CONFIG_IMPORT_UPLOADS[token] = {'path': str(path), 'manifest': manifest, 'ts': now}
    return token


def _create_pre_import_backup(selected_items: list[dict[str, Any]]) -> Path | None:
    ids = [str(item.get('id')) for item in selected_items if str(item.get('id') or '')]
    if not ids:
        return None
    data, items = _create_config_transport_zip(ids, reason='pre-import-backup')
    if not items:
        return None
    backup_dir = _APP_ROOT / 'config_import_backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    path = backup_dir / f'pre-import-{stamp}.zip'
    path.write_bytes(data)
    return path


def _clear_imported_config_caches() -> None:
    try:
        utils.reload_config(force=True)
    except Exception:
        pass
    try:
        _apply_logging_config()
    except Exception:
        pass
    try:
        _videohub_rooms_cache['snapshot'] = None
        _videohub_rooms_cache['config'] = None
    except Exception:
        pass
    try:
        _home_state_sync_from_disk()
    except Exception:
        pass
    try:
        _init_auth_db()
    except Exception:
        pass


def _clear_directory_contents(path: Path) -> tuple[int, list[str]]:
    removed = 0
    errors: list[str] = []
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return removed, errors
    if not path.is_dir():
        raise ValueError(f"Import target is not a directory: {path}")

    for child in list(path.iterdir()):
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                try:
                    child.chmod(0o666)
                except Exception:
                    pass
                child.unlink()
            removed += 1
        except Exception as e:
            errors.append(f"{child.name}: {e}")
    return removed, errors


def _apply_config_transport_import(zip_path: Path, selected_ids: list[str]) -> tuple[list[dict[str, Any]], Path | None]:
    manifest = _inspect_config_transport_zip(zip_path)
    item_map = {str(item.get('id')): item for item in manifest.get('items') or [] if isinstance(item, dict)}
    expanded_ids = _expand_config_transport_selection(selected_ids, item_map)
    selected = [item_map[i] for i in expanded_ids if i in item_map]
    if not selected:
        raise ValueError('Select at least one item to import.')

    backup_path = _create_pre_import_backup(selected)
    imported: list[dict[str, Any]] = []
    with zipfile.ZipFile(zip_path, 'r') as zf:
        all_names = set(zf.namelist())
        for item in selected:
            rel = str(item.get('path') or '').replace('\\', '/').lstrip('/')
            current_item = _config_transport_item_map().get(str(item.get('id') or '')) or {}
            actual_target = str(current_item.get('_actual_path') or '').strip()
            target = Path(actual_target) if actual_target else _resolve_transport_path(rel)
            if target is None:
                raise ValueError(f"Invalid import path for {item.get('label') or item.get('id')}.")
            kind = str(item.get('kind') or 'file')
            if kind == 'directory':
                prefix = f'payload/{rel.rstrip("/")}/'
                members = [n for n in all_names if n.startswith(prefix) and not n.endswith('/')]
                removed_count, clear_errors = _clear_directory_contents(target)
                if clear_errors:
                    _config_transport_log(
                        f"Could not remove {len(clear_errors)} existing file(s) from {target}; "
                        "continuing with imported files."
                    )
                count = 0
                for name in members:
                    child_rel = name[len('payload/'):].lstrip('/')
                    child_target = _resolve_transport_path(child_rel)
                    if child_target is None:
                        continue
                    child_target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, child_target.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                    count += 1
                imported.append({'id': item.get('id'), 'label': item.get('label'), 'path': rel, 'count': count, 'removed': removed_count})
            else:
                member = f'payload/{rel}'
                if member not in all_names:
                    raise ValueError(f"Missing payload for {item.get('label') or rel}.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                imported.append({'id': item.get('id'), 'label': item.get('label'), 'path': rel, 'count': 1})

    _clear_imported_config_caches()
    return imported, backup_path


class _ConsoleTee:
    """Tee writes to the original stream AND the diagnostic buffer."""

    def __init__(self, original, stream_name: str):
        self._original = original
        self._stream_name = stream_name

    def write(self, s):
        try:
            _console_append(s)
        except Exception:
            pass
        try:
            return self._original.write(s)
        except Exception:
            return 0

    def flush(self):
        try:
            return self._original.flush()
        except Exception:
            return None

    def isatty(self):
        try:
            return bool(self._original.isatty())
        except Exception:
            return False


class _ConsoleLogHandler(logging.Handler):
    def filter(self, record: logging.LogRecord) -> bool:
        # Hide noisy request/access logs from the diagnostic buffer.
        try:
            name = record.name or ''
        except Exception:
            name = ''
        if name.startswith('werkzeug'):
            return False

        # Extra safety: some environments may log access lines elsewhere.
        try:
            msg = record.getMessage() or ''
        except Exception:
            msg = ''
        if '"GET /api/activity-log' in msg or '"GET /api/activity-log/live' in msg:
            return False
        return True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = str(record.getMessage())
        _console_append(msg + "\n")


def _install_console_capture() -> None:
    """Capture server stdout/stderr + logging into the diagnostic buffer."""
    try:
        if not getattr(sys.stdout, '_webui_console_wrapped', False):
            sys.stdout = _ConsoleTee(sys.stdout, 'stdout')
            setattr(sys.stdout, '_webui_console_wrapped', True)
    except Exception:
        pass

    try:
        if not getattr(sys.stderr, '_webui_console_wrapped', False):
            sys.stderr = _ConsoleTee(sys.stderr, 'stderr')
            setattr(sys.stderr, '_webui_console_wrapped', True)
    except Exception:
        pass

    # Also attach a logging handler so we capture logs even if other handlers
    # hold references to the pre-wrap stderr stream.
    try:
        root = logging.getLogger()
        if not any(isinstance(h, _ConsoleLogHandler) for h in root.handlers):
            h = _ConsoleLogHandler()
            h.setLevel(logging.INFO)
            # Timestamp is added per-line in _console_append
            h.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))
            root.addHandler(h)
    except Exception:
        pass


_install_console_capture()


# Optional ProPresenter timer integration (Companion -> this Web UI -> ProPresenter)
try:
    from propresentor import ProPresentor
except Exception:
    ProPresentor = None

# Optional Blackmagic VideoHub TCP routing integration
try:
    from videohub import VideohubClient, get_videohub_client_from_config, DEFAULT_PORT as VIDEOHUB_DEFAULT_PORT
except Exception:
    VideohubClient = None  # type: ignore
    get_videohub_client_from_config = None  # type: ignore
    VIDEOHUB_DEFAULT_PORT = 9990

# Optional DiGiCo OSC / Personal Mixes integration
try:
    from digico import (
        close_digico_client as _close_digico_client,
        get_digico_client_from_config as _digico_client_factory,
    )
except Exception:
    _close_digico_client = None  # type: ignore
    _digico_client_factory = None  # type: ignore

# Optional native Hisense / VIDAA TV integration
try:
    from hisense import (
        close_hisense_manager as _close_hisense_manager,
        get_hisense_manager_from_config as _hisense_manager_factory,
    )
except Exception:
    _close_hisense_manager = None  # type: ignore
    _hisense_manager_factory = None  # type: ignore

# Optional direct SAL Pixie Plus Gateway integration
try:
    from pixie import (
        close_pixie_manager as _close_pixie_manager,
        discover_gateways as _pixie_discover_gateways,
        get_pixie_manager_from_config as _pixie_manager_factory,
        load_pixie_secrets as _load_pixie_secrets,
        provision_pixie_cloud as _provision_pixie_cloud,
        save_pixie_secrets as _save_pixie_secrets,
    )
except Exception:
    _close_pixie_manager = None  # type: ignore
    _pixie_discover_gateways = None  # type: ignore
    _pixie_manager_factory = None  # type: ignore
    _load_pixie_secrets = None  # type: ignore
    _provision_pixie_cloud = None  # type: ignore
    _save_pixie_secrets = None  # type: ignore

# Optional Blackmagic ATEM audio integration
try:
    from atem import AtemAudioClient, get_atem_client_from_config, DEFAULT_PORT as ATEM_DEFAULT_PORT
except Exception:
    AtemAudioClient = None  # type: ignore
    get_atem_client_from_config = None  # type: ignore
    ATEM_DEFAULT_PORT = 9910


def _apply_logging_config():
    """Adjust log levels for noisy servers (werkzeug) based on config debug flag.

    When `debug` in config is falsey we raise the log level to WARNING so
    frequent access logs (e.g. companion status polling) aren't printed.
    """
    try:
        cfg = utils.get_config()
        debug = bool(cfg.get('debug', False))
    except Exception:
        debug = False
    level = logging.INFO if debug else logging.WARNING
    try:
        logging.getLogger('werkzeug').setLevel(level)
    except Exception:
        pass
    try:
        logging.getLogger('flask.app').setLevel(level)
    except Exception:
        pass


# apply logging config at import/startup
_apply_logging_config()


def _get_videohub_client_from_config():
    if VideohubClient is None or get_videohub_client_from_config is None:
        return None
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    return get_videohub_client_from_config(cfg)


def _get_digico_client_from_config():
    if _digico_client_factory is None:
        return None
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        return _digico_client_factory(cfg)
    except Exception:
        return None


def _get_hisense_manager_from_config():
    if _hisense_manager_factory is None:
        return None
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        return _hisense_manager_factory(cfg, base_dir=Path.cwd())
    except Exception:
        return None


def _get_pixie_manager_from_config():
    if _pixie_manager_factory is None:
        return None
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        return _pixie_manager_factory(cfg, base_dir=_APP_ROOT)
    except Exception:
        return None


def _digico_aux_options() -> list[dict]:
    """Return discovered/configured AUX choices without failing the admin UI."""
    try:
        client = _get_digico_client_from_config()
        if client is not None:
            auxes = client.mixer_config().get('auxes', [])
            if isinstance(auxes, list) and auxes:
                return auxes
    except Exception:
        pass
    try:
        cfg = utils.get_config()
        configured = cfg.get('digico_auxes', [])
    except Exception:
        configured = []
    out = []
    for index, item in enumerate(configured if isinstance(configured, list) else [], start=1):
        if not isinstance(item, dict):
            continue
        out.append({
            'channel': index,
            'label': str(item.get('label') or f'Aux {index}'),
            'enabled': bool(item.get('enabled', True)),
        })
    return out


def _pixie_permission_catalog() -> dict[str, list[dict[str, Any]]]:
    """Return saved Pixie arrangement metadata without touching the Gateway."""
    try:
        cfg = utils.get_config()
    except Exception:
        cfg = {}
    raw_devices = cfg.get('pixie_devices') if isinstance(cfg.get('pixie_devices'), list) else []
    devices = {
        str(item.get('id')): {
            'id': str(item.get('id')),
            'name': str(item.get('name') or item.get('original_name') or item.get('id')),
        }
        for item in raw_devices if isinstance(item, dict) and str(item.get('id') or '').strip()
    }
    auditoriums = []
    for item in cfg.get('pixie_auditoriums') if isinstance(cfg.get('pixie_auditoriums'), list) else []:
        if not isinstance(item, dict) or not str(item.get('id') or '').strip():
            continue
        member_ids = [str(value) for value in item.get('device_ids') if str(value or '').strip()] if isinstance(item.get('device_ids'), list) else []
        auditoriums.append({
            'id': str(item['id']),
            'name': str(item.get('name') or item['id']),
            'devices': [devices[device_id] for device_id in member_ids if device_id in devices],
        })
    scenes = []
    for item in cfg.get('pixie_scenes') if isinstance(cfg.get('pixie_scenes'), list) else []:
        if not isinstance(item, dict) or not str(item.get('id') or '').strip():
            continue
        scenes.append({
            'id': str(item['id']),
            'name': str(item.get('name') or item.get('original_name') or item['id']),
            'enabled': bool(item.get('enabled', True)),
        })
    return {'auditoriums': auditoriums, 'scenes': scenes}


def _get_atem_client_from_config():
    if AtemAudioClient is None or get_atem_client_from_config is None:
        return None
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    return get_atem_client_from_config(cfg)


_atem_permission_sources_lock = threading.Lock()
_atem_permission_sources_cache: dict[str, Any] = {
    'ts': 0.0,
    'payload': None,
    'refreshing': False,
}
_ATEM_PERMISSION_SOURCES_TTL_SECONDS = 30.0


def _fallback_atem_permission_sources() -> list[dict[str, Any]]:
    try:
        if AtemAudioClient is not None:
            return AtemAudioClient.fallback_sources()
    except Exception:
        pass
    return [{'id': 'master', 'label': 'Master', 'kind': 'master'}]


def _refresh_atem_permission_sources() -> None:
    sources: list[dict[str, Any]] = []
    try:
        atem = _get_atem_client_from_config()
        if atem is not None:
            state = atem.get_audio_state()
            raw_sources = state.get('sources') if isinstance(state, dict) else None
            if isinstance(raw_sources, list):
                sources = [item for item in raw_sources if isinstance(item, dict)]
    except Exception:
        sources = []
    if not sources:
        sources = _fallback_atem_permission_sources()
    with _atem_permission_sources_lock:
        _atem_permission_sources_cache['ts'] = time.time()
        _atem_permission_sources_cache['payload'] = sources
        _atem_permission_sources_cache['refreshing'] = False


def _get_atem_audio_sources_for_permissions() -> list[dict[str, Any]]:
    """Return cached labels immediately; hardware refresh happens off-request."""
    now = time.time()
    start_refresh = False
    with _atem_permission_sources_lock:
        cached = _atem_permission_sources_cache.get('payload')
        age = now - float(_atem_permission_sources_cache.get('ts', 0.0) or 0.0)
        if age > _ATEM_PERMISSION_SOURCES_TTL_SECONDS and not bool(
            _atem_permission_sources_cache.get('refreshing', False)
        ):
            _atem_permission_sources_cache['refreshing'] = True
            start_refresh = True
        result = [dict(item) for item in cached] if isinstance(cached, list) and cached else None
    if start_refresh:
        threading.Thread(
            target=_refresh_atem_permission_sources,
            name='tdeck-atem-permission-refresh',
            daemon=True,
        ).start()
    return result or _fallback_atem_permission_sources()

TEMPLATES_DIR = Path.cwd()
TRIGGER_TEMPLATES = TEMPLATES_DIR / 'trigger_templates.json'
BUTTON_TEMPLATES = TEMPLATES_DIR / 'button_templates.json'

# ensure template files exist
for p in (TRIGGER_TEMPLATES, BUTTON_TEMPLATES):
    if not p.exists():
        p.write_text('[]', encoding='utf-8')


def _execute_scheduler_internal_action(action: dict, job=None) -> bool:
    """Dispatch a Calendar action inside this process without API credentials."""
    payload = action if isinstance(action, dict) else {}
    method = str(payload.get('method') or 'POST').strip().upper()
    path = str(payload.get('path') or '').strip()
    if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
        return False
    if not path.startswith('/api/') or '://' in path:
        return False

    request_args: dict[str, Any] = {'method': method}
    if method != 'GET' and 'body' in payload:
        request_args['json'] = payload.get('body')
    marker = {
        'type': 'scheduler',
        'key': 'scheduler:calendar',
        'name': 'TDeck Scheduler',
        'event_id': getattr(getattr(job, 'event', None), 'id', None),
        'event_name': str(getattr(getattr(job, 'event', None), 'name', '') or ''),
        'trigger_index': getattr(job, 'trigger_index', None),
    }
    try:
        with app.test_request_context(path, **request_args):
            g._tdeck_scheduler_principal = marker
            response = app.full_dispatch_request()
            return 200 <= int(response.status_code) < 300
    except Exception:
        logging.getLogger('calendar').exception(
            'In-process scheduler action dispatch failed for %s %s', method, path
        )
        return False


def _start_all_apps():
    """Start all registered apps in background threads."""
    apps = list_apps()
    for name in apps:
        try:
            # Avoid starting the same app multiple times (e.g. if the web server
            # is restarted due to a port change).
            try:
                if name in _running_apps:
                    continue
            except Exception:
                pass

            app_inst = get_app(name)
            if app_inst is None:
                continue

            if name == 'calendar' and hasattr(app_inst, 'set_internal_action_executor'):
                try:
                    app_inst.set_internal_action_executor(_execute_scheduler_internal_action)
                except Exception:
                    pass

            try:
                if hasattr(app_inst, 'status'):
                    st = app_inst.status() or {}
                    if bool(st.get('running', False)):
                        _running_apps[name] = app_inst
                        continue
            except Exception:
                pass

            # start non-blocking and record instance as running
            try:
                app_inst.start(blocking=False)
            except TypeError:
                # some apps may not accept blocking arg; start in a thread
                threading.Thread(target=lambda inst=app_inst: inst.start(), daemon=True).start()
            except Exception:
                # best-effort: ignore start failures
                pass

            try:
                _running_apps[name] = app_inst
            except Exception:
                pass
        except Exception:
            pass


# Track instances started via this web UI so we can stop them later
_running_apps: dict[str, object] = {}


# HTTP server control so we can start/stop/restart on config changes
_http_server = None
_server_thread = None
_server_lock = threading.Lock()

# Status endpoint caches (to avoid each connected browser triggering a blocking probe)
_companion_status_cache = {'ts': 0.0, 'connected': False}
_propresenter_status_cache = {'ts': 0.0, 'connected': False}
_videohub_status_cache = {'ts': 0.0, 'connected': False}
_digico_status_cache = {'ts': 0.0, 'connected': False}
_atem_status_cache = {'ts': 0.0, 'connected': False}
_hisense_status_cache = {'ts': 0.0, 'connected': False}
_pixie_status_cache = {'ts': 0.0, 'connected': False}
_status_snapshot_cache = {'ts': 0.0, 'payload': None}
_videohub_state_cache = {
    'ts': 0.0,
    'payload': None,
    'last_error': None,
    'failures': 0,
    'retry_after': 0.0,
    'invalidated': False,
}
_videohub_state_refresh_lock = threading.Lock()
_videohub_state_refreshing = False
_status_cache_lock = threading.Lock()
_status_refresher_lock = threading.Lock()
_status_refresher_started = False
_STATUS_CACHE_TTL_SECONDS = 2.0
_STATUS_REFRESH_INTERVAL_SECONDS = 5.0
_VIDEOHUB_STATE_CACHE_TTL_SECONDS = 5.0
_VIDEOHUB_STATE_RETRY_BASE_SECONDS = 0.5
_VIDEOHUB_STATE_RETRY_MAX_SECONDS = 10.0
_atem_probe_failures = 0
_ATEM_OFFLINE_AFTER_FAILURES = 3

# Track last-known connectivity so we can log state changes (ONLINE/OFFLINE)
# without spamming the console on every poll.
_connectivity_last: dict[str, bool | None] = {
    'companion': None,
    'propresenter': None,
    'videohub': None,
    'digico': None,
    'atem': None,
    'hisense': None,
    'pixie': None,
}


def _log_connectivity_change(service: str, connected: bool, *, detail: str = '') -> None:
    """Log a line when a service flips online/offline.

    This is used by the lightweight UI status endpoints which are polled
    frequently; we only emit output on transitions.
    """

    label = {
        'companion': 'Companion',
        'propresenter': 'ProPresenter',
        'videohub': 'VideoHub',
        'digico': 'DiGiCo',
        'atem': 'ATEM',
        'hisense': 'Hisense TVs',
        'pixie': 'Pixie',
    }.get(service, service)

    should_log = False
    with _status_cache_lock:
        prev = _connectivity_last.get(service, None)
        if prev is None:
            _connectivity_last[service] = bool(connected)
        elif bool(prev) != bool(connected):
            _connectivity_last[service] = bool(connected)
            should_log = True

    if not should_log:
        return

    state = 'ONLINE' if bool(connected) else 'OFFLINE'
    suffix = ''
    try:
        d = str(detail or '').strip()
        if d:
            suffix = f" ({d})"
    except Exception:
        suffix = ''

    try:
        log_event(
            f'{service}.connection.{"connected" if bool(connected) else "disconnected"}',
            f"{label} is now {state}{suffix}",
            source='system',
            status='success' if bool(connected) else 'warning',
            target_type='integration',
            target_id=str(service or label or ''),
            details={'service': service, 'label': label, 'connected': bool(connected), 'state': state, 'detail': detail},
        )
    except Exception:
        pass


def _probe_companion_status(cfg: dict) -> dict:
    connected = False
    detail = ''
    try:
        c = utils.get_companion()
        if c is None:
            connected = False
        else:
            try:
                if hasattr(c, 'check_connection'):
                    connected = bool(c.check_connection())
                else:
                    connected = bool(getattr(c, 'connected', False))
            except Exception:
                connected = bool(getattr(c, 'connected', False))
    except Exception:
        connected = False

    try:
        ip = str(cfg.get('companion_ip', '')).strip()
        port = int(cfg.get('companion_port', 0))
        detail = f"{ip}:{port}" if ip and port else (ip or '')
    except Exception:
        detail = ''

    return {
        'connected': bool(connected),
        'detail': detail,
        'checked_at': time.time(),
    }


def _probe_propresenter_status(cfg: dict) -> dict:
    connected = False
    detail = ''
    try:
        if ProPresentor is None:
            connected = False
        else:
            ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
            try:
                port = int(cfg.get('propresenter_port', 1025))
            except Exception:
                port = 1025
            pp = ProPresentor(ip, port, timeout=1.0, verify_on_init=False, debug=False)
            connected = bool(pp.check_connection())
            detail = f"{ip}:{port}" if ip and port else ''
    except Exception:
        connected = False
        detail = ''

    return {
        'connected': bool(connected),
        'detail': detail,
        'checked_at': time.time(),
    }


def _probe_videohub_status(cfg: dict) -> dict:
    connected = False
    detail = ''
    try:
        vh = _get_videohub_client_from_config()
        if vh is None:
            connected = False
        else:
            connected = bool(vh.ping())
    except Exception:
        connected = False

    try:
        ip = str(cfg.get('videohub_ip', '')).strip()
        port = int(cfg.get('videohub_port', 0))
        detail = f"{ip}:{port}" if ip and port else (ip or '')
    except Exception:
        detail = ''

    return {
        'connected': bool(connected),
        'detail': detail,
        'checked_at': time.time(),
    }


def _probe_digico_status(cfg: dict) -> dict:
    enabled = bool(cfg.get('digico_enabled', False))
    host = str(cfg.get('digico_ip') or '').strip()
    try:
        port = int(cfg.get('digico_port', 9000))
    except Exception:
        port = 9000
    detail = f"{host}:{port}" if host else ('disabled' if not enabled else 'not configured')
    client = _get_digico_client_from_config()
    status = client.status() if client is not None else {}
    return {
        'connected': bool(status.get('connected', False)),
        'enabled': enabled,
        'detail': detail,
        'checked_at': time.time(),
        'last_error': str(status.get('lastError') or ''),
    }


def _probe_atem_status(cfg: dict) -> dict:
    global _atem_probe_failures
    connected = False
    detail = ''
    try:
        atem = _get_atem_client_from_config()
        if atem is None:
            connected = False
        else:
            connected = bool(atem.ping())
    except Exception:
        connected = False

    try:
        ip = str(cfg.get('atem_ip', '')).strip()
        port = int(cfg.get('atem_port', ATEM_DEFAULT_PORT))
        detail = f"{ip}:{port}" if ip and port else (ip or '')
    except Exception:
        detail = ''

    raw_connected = bool(connected)
    with _status_cache_lock:
        was_connected = bool(_atem_status_cache.get('connected', False))

    if raw_connected:
        _atem_probe_failures = 0
    else:
        _atem_probe_failures += 1
        if was_connected and _atem_probe_failures < _ATEM_OFFLINE_AFTER_FAILURES:
            connected = True
            if detail:
                detail = f"{detail} (missed probe {_atem_probe_failures}/{_ATEM_OFFLINE_AFTER_FAILURES})"
        else:
            connected = False

    return {
        'connected': bool(connected),
        'detail': detail,
        'checked_at': time.time(),
    }


def _probe_hisense_status(cfg: dict) -> dict:
    enabled = bool(cfg.get('hisense_enabled', False))
    manager = _get_hisense_manager_from_config()
    status = manager.status() if manager is not None else {}
    online = int(status.get('online', 0) or 0)
    off = int(status.get('off', 0) or 0)
    total = int(status.get('total', 0) or 0)
    detail = f'{online}/{total} online'
    if off:
        detail += f', {off} intentionally off'
    return {
        # An intentionally powered-off TV is healthy even though its MQTT
        # transport is no longer connected. This keeps expected shutdowns out
        # of the integration error state and Activity Log disconnect warnings.
        'connected': bool(status.get('healthy', status.get('connected', False))),
        'enabled': enabled,
        'configured': bool(status.get('configured', False)),
        'available': bool(status.get('available', False)),
        'online': online,
        'off': off,
        'total': total,
        'detail': detail if enabled and total else ('disabled' if not enabled else 'not configured'),
        'checked_at': time.time(),
    }


def _probe_pixie_status(cfg: dict) -> dict:
    mode = str(cfg.get('pixie_network_mode') or 'disabled').strip().lower()
    manager = _get_pixie_manager_from_config()
    status = manager.status() if manager is not None else {}
    host = str(status.get('gatewayHost') or cfg.get('pixie_gateway_host') or '').strip()
    if mode == 'disabled':
        detail = 'disabled'
    elif status.get('connected'):
        detail = 'observe only' if mode == 'observe' else 'connected'
    else:
        detail = str(status.get('lastError') or (f'{host} offline' if host else 'not configured'))
    return {
        'connected': bool(status.get('connected', False)),
        'control_ready': bool(status.get('controlReady', False)),
        'enabled': mode != 'disabled',
        'mode': mode,
        'available': bool(status.get('available', manager is not None)),
        'detail': detail,
        'last_error': str(status.get('lastError') or ''),
        'device_count': int(status.get('deviceCount', 0) or 0),
        'scene_count': int(status.get('sceneCount', 0) or 0),
        'checked_at': time.time(),
    }


def _probe_scheduler_status(cfg: dict) -> dict:
    """Return the real calendar worker state, not merely object existence."""
    checked_at = time.time()
    try:
        calendar_app = get_app('calendar')
        status = dict(calendar_app.status() or {}) if calendar_app is not None else {}
    except Exception as exc:
        return {
            'running': False,
            'healthy': False,
            'detail': f'status error: {exc}',
            'checked_at': checked_at,
        }

    running = bool(status.get('running', False))
    watcher_alive = bool(status.get('watcher_alive', False))
    last_tick_at = status.get('last_tick_at')
    tick_age_seconds = None
    if last_tick_at:
        try:
            tick_age_seconds = max(
                0.0,
                (datetime.now() - datetime.fromisoformat(str(last_tick_at))).total_seconds(),
            )
        except Exception:
            tick_age_seconds = None
    try:
        # A configured internal API action can legitimately hold the scheduler
        # thread for several seconds. Do not call that a stale heartbeat while
        # its bounded request timeout is still in force.
        stale_after = max(
            30.0,
            float(cfg.get('poll_interval', 1.0)) * 5.0,
            float(cfg.get('internal_api_timeout_seconds', 10.0)) + 5.0,
        )
    except Exception:
        stale_after = 5.0
    heartbeat_current = tick_age_seconds is None or tick_age_seconds <= stale_after
    healthy = running and watcher_alive and heartbeat_current and not status.get('last_watcher_error')
    if not running:
        detail = str(status.get('last_error') or 'scheduler worker is not running')
    elif not watcher_alive:
        detail = 'events/config file watcher is not running'
    elif not heartbeat_current:
        detail = f'scheduler heartbeat is {tick_age_seconds:.1f}s old'
    elif status.get('last_watcher_error'):
        detail = str(status.get('last_watcher_error'))
    else:
        detail = 'running'

    status.update({
        'running': running,
        'healthy': healthy,
        'detail': detail,
        'tick_age_seconds': tick_age_seconds,
        'checked_at': checked_at,
    })
    return status


def _refresh_status_snapshot() -> dict:
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    companion = _probe_companion_status(cfg)
    propresenter = _probe_propresenter_status(cfg)
    videohub = _probe_videohub_status(cfg)
    digico = _probe_digico_status(cfg)
    atem = _probe_atem_status(cfg)
    hisense = _probe_hisense_status(cfg)
    pixie = _probe_pixie_status(cfg)
    scheduler = _probe_scheduler_status(cfg)
    now = time.time()

    payload = {
        'ok': True,
        'ts': now,
        'companion': companion,
        'propresenter': propresenter,
        'videohub': videohub,
        'digico': digico,
        'atem': atem,
        'hisense': hisense,
        'pixie': pixie,
        'scheduler': scheduler,
    }

    with _status_cache_lock:
        _status_snapshot_cache['ts'] = now
        _status_snapshot_cache['payload'] = payload
        _companion_status_cache['ts'] = companion.get('checked_at', now)
        _companion_status_cache['connected'] = bool(companion.get('connected', False))
        _propresenter_status_cache['ts'] = propresenter.get('checked_at', now)
        _propresenter_status_cache['connected'] = bool(propresenter.get('connected', False))
        _videohub_status_cache['ts'] = videohub.get('checked_at', now)
        _videohub_status_cache['connected'] = bool(videohub.get('connected', False))
        _digico_status_cache['ts'] = digico.get('checked_at', now)
        _digico_status_cache['connected'] = bool(digico.get('connected', False))
        _atem_status_cache['ts'] = atem.get('checked_at', now)
        _atem_status_cache['connected'] = bool(atem.get('connected', False))
        _hisense_status_cache['ts'] = hisense.get('checked_at', now)
        _hisense_status_cache['connected'] = bool(hisense.get('connected', False))
        _pixie_status_cache['ts'] = pixie.get('checked_at', now)
        _pixie_status_cache['connected'] = bool(pixie.get('connected', False))

    _log_connectivity_change('companion', bool(companion.get('connected', False)), detail=str(companion.get('detail') or ''))
    _log_connectivity_change('propresenter', bool(propresenter.get('connected', False)), detail=str(propresenter.get('detail') or ''))
    _log_connectivity_change('videohub', bool(videohub.get('connected', False)), detail=str(videohub.get('detail') or ''))
    if bool(digico.get('enabled', False)):
        _log_connectivity_change('digico', bool(digico.get('connected', False)), detail=str(digico.get('detail') or ''))
    _log_connectivity_change('atem', bool(atem.get('connected', False)), detail=str(atem.get('detail') or ''))
    if bool(hisense.get('enabled', False)):
        _log_connectivity_change('hisense', bool(hisense.get('connected', False)), detail=str(hisense.get('detail') or ''))
    if bool(pixie.get('enabled', False)):
        _log_connectivity_change('pixie', bool(pixie.get('connected', False)), detail=str(pixie.get('detail') or ''))

    return payload


def _get_status_snapshot() -> dict:
    now = time.time()
    with _status_cache_lock:
        payload = _status_snapshot_cache.get('payload')
        ts = float(_status_snapshot_cache.get('ts', 0.0) or 0.0)

    if isinstance(payload, dict):
        if (now - ts) <= (_STATUS_REFRESH_INTERVAL_SECONDS * 2.0):
            return payload

    return _refresh_status_snapshot()


def _status_refresher_loop() -> None:
    while True:
        time.sleep(_STATUS_REFRESH_INTERVAL_SECONDS)
        try:
            _refresh_status_snapshot()
        except Exception:
            pass


def _ensure_status_refresher_started() -> None:
    global _status_refresher_started
    with _status_refresher_lock:
        if _status_refresher_started:
            return
        _status_refresher_started = True
        threading.Thread(target=_status_refresher_loop, daemon=True).start()

# Upcoming trigger cache (to avoid recomputing schedule for each client refresh)
_upcoming_triggers_cache = {'ts': 0.0, 'events_file': '', 'limit': None, 'payload': None}
_UPCOMING_TRIGGERS_TTL_SECONDS = 1.0


def start_http_server(host: str, port: int) -> None:
    global _http_server, _server_thread
    with _server_lock:
        if _http_server is not None:
            return
        # Threaded server is important here: the UI polls status endpoints and
        # some endpoints perform short network checks. Without threading, one
        # slow request can block all users.
        srv = make_server(host, port, app, threaded=True)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        _http_server = srv
        _server_thread = thread
        thread.start()
        # start apps after the server is up
        try:
            _start_all_apps()
        except Exception:
            pass
        try:
            _get_digico_client_from_config()
        except Exception:
            pass
        try:
            _get_hisense_manager_from_config()
        except Exception:
            pass
        try:
            _get_pixie_manager_from_config()
        except Exception:
            pass
        try:
            _refresh_status_snapshot()
        except Exception:
            pass
        try:
            _ensure_status_refresher_started()
        except Exception:
            pass


def stop_http_server() -> None:
    global _http_server, _server_thread
    with _server_lock:
        if _http_server is None:
            return
        try:
            _http_server.shutdown()
        except Exception:
            pass
        try:
            if _close_digico_client is not None:
                _close_digico_client()
        except Exception:
            pass
        try:
            if _close_hisense_manager is not None:
                _close_hisense_manager()
        except Exception:
            pass
        try:
            if _close_pixie_manager is not None:
                _close_pixie_manager()
        except Exception:
            pass
        _http_server = None
        _server_thread = None


def restart_http_server(host: str, port: int) -> None:
    stop_http_server()
    start_http_server(host, port)



def _poll_interval_seconds(cfg: dict) -> float:
    try:
        v = float(cfg.get('poll_interval', 1.0))
    except Exception:
        v = 1.0
    # Keep the watcher responsive but avoid pathological values
    if v < 0.2:
        return 0.2
    if v > 60.0:
        return 60.0
    return v


# Config watcher: restart server when `webserver_port` changes in config.json
def _config_watcher():
    try:
        utils.reload_config(force=True)
    except Exception:
        pass
    # ensure logging reflects loaded config
    try:
        _apply_logging_config()
    except Exception:
        pass
    cfg = utils.get_config()
    prev_port = int(cfg.get('webserver_port', cfg.get('server_port', 5000)))
    while True:
        time.sleep(_poll_interval_seconds(cfg))
        try:
            changed = utils.reload_config()
        except Exception:
            changed = False
        if changed:
            cfg = utils.get_config()
            new_port = int(cfg.get('webserver_port', cfg.get('server_port', 5000)))
            if new_port != prev_port:
                try:
                    restart_http_server('0.0.0.0', new_port)
                except Exception:
                    pass
                prev_port = new_port
        else:
            # still refresh local cfg snapshot so poll_interval updates from other
            # code paths (or failed reloads) can take effect eventually
            try:
                cfg = utils.get_config()
            except Exception:
                pass


# start config watcher thread
threading.Thread(target=_config_watcher, daemon=True).start()


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if not _auth_enabled():
        return redirect('/')

    # Ensure DB + default admin exists before first login
    try:
        _bootstrap_default_users_roles()
    except Exception:
        pass

    next_url = request.args.get('next') or request.form.get('next') or ''
    # Safety: only allow local redirects
    if next_url and (next_url.startswith('http://') or next_url.startswith('https://') or '://' in next_url):
        next_url = ''

    if request.method == 'POST':
        if not _validate_csrf():
            _audit('login_csrf_fail')
            return abort(400)

        username = str(request.form.get('username') or '').strip()
        password = str(request.form.get('password') or '')

        row = _user_by_username(username)
        if not row:
            _record_login_failure(None, username)
            return render_template('login.html', page_title='Login', error='Invalid username or password', next=next_url), 401

        if not bool(int(row['is_active'] or 0)):
            _audit('login_fail_inactive', f'username={username}')
            return render_template('login.html', page_title='Login', error='Account is disabled', next=next_url), 403

        if bool(int(row['is_locked'] or 0)):
            _audit('login_fail_locked', f'username={username}')
            return render_template('login.html', page_title='Login', error='Account is locked. Ask an admin to unlock it.', next=next_url), 403

        try:
            ok = check_password_hash(str(row['password_hash'] or ''), password)
        except Exception:
            ok = False

        if not ok:
            _record_login_failure(row, username)
            return render_template('login.html', page_title='Login', error='Invalid username or password', next=next_url), 401

        refreshed = _record_login_success(int(row['id'])) or row
        user = _User(refreshed)
        login_user(user)
        session['_last_activity'] = int(time.time())
        _create_user_session(refreshed)
        _audit('login_ok', f'username={username}')

        if bool(int(refreshed['force_password_change'] or 0)):
            return redirect(url_for('account_password_page', force=1))

        redirect_target = next_url or '/'
        requested_path = redirect_target.split('?', 1)[0].split('#', 1)[0]
        if requested_path in ('', '/') and not user.allows_page('page:home'):
            redirect_target = _landing_page_for_user(user)

        return redirect(redirect_target)

    timeout = request.args.get('timeout')
    msg = 'You have been logged out due to inactivity.' if timeout else None
    return render_template('login.html', page_title='Login', message=msg, next=next_url)


@app.route('/logout')
def logout_page():
    if _auth_enabled() and getattr(current_user, 'is_authenticated', False):
        _audit('logout')
        try:
            sid = str(session.get('_auth_session_id') or '').strip()
            if sid:
                conn = _db()
                try:
                    conn.execute('UPDATE user_sessions SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?', (_now_str(), sid))
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            pass
    try:
        logout_user()
    except Exception:
        pass
    try:
        session.clear()
    except Exception:
        pass
    return redirect('/login' if _auth_enabled() else '/')


@app.route('/account/password', methods=['GET', 'POST'])
@require_page('page:account', 'Account')
def account_password_page():
    if request.method == 'POST':
        current_pw = str(request.form.get('current_password') or '')
        new_pw = str(request.form.get('new_password') or '')
        confirm_pw = str(request.form.get('confirm_password') or '')

        min_len = _auth_min_password_length()

        if new_pw != confirm_pw:
            return render_template('account_password.html', page_title='Change Password', error='Passwords do not match')
        if len(new_pw) < min_len:
            return render_template('account_password.html', page_title='Change Password', error=f'Password must be at least {min_len} characters')

        # Verify current password
        row = _user_record(int(current_user.get_id()))
        if not row or not check_password_hash(str(row['password_hash'] or ''), current_pw):
            _audit('password_change_fail', 'bad_current_password')
            return render_template('account_password.html', page_title='Change Password', error='Current password is incorrect')

        conn = _db()
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute(
                'UPDATE users SET password_hash=?, force_password_change=0, password_changed_at=?, updated_at=? WHERE id=?',
                (generate_password_hash(new_pw), now, now, int(current_user.get_id())),
            )
            conn.commit()
        finally:
            conn.close()

        _audit('password_change_ok')
        return redirect('/')

    force = bool(str(request.args.get('force') or '').strip())
    return render_template('account_password.html', page_title='Change Password', force_change=force)


@app.route('/admin/permissions', methods=['GET', 'POST'])
@require_page('page:admin', 'Admin')
def admin_permissions_page():
    # When auth is disabled, admin pages are still reachable; make sure the DB
    # and default groups exist so the page can render.
    try:
        _bootstrap_default_users_roles()
    except Exception:
        try:
            _init_auth_db()
        except Exception:
            pass

    if request.method == 'POST':
        action = str(request.form.get('action') or '').strip()

        def _permissions_redirect(tab: str, error: str | None = None):
            tab_name = 'groups' if tab == 'groups' else 'users'
            kwargs = {'tab': tab_name}
            if error:
                kwargs['error'] = str(error)
            return redirect(url_for('admin_permissions_page', **kwargs) + f'#{tab_name}')

        def _form_group_ids() -> list[int]:
            out: list[int] = []
            for raw in request.form.getlist('group_ids'):
                try:
                    gid = int(raw)
                except Exception:
                    continue
                if gid > 0 and gid not in out:
                    out.append(gid)
            return out

        if action == 'create_group':
            name = str(request.form.get('group_name') or '').strip()
            if not name:
                return _permissions_redirect('groups', 'Enter a group name.')
            conn = _db()
            try:
                existing = conn.execute('SELECT 1 FROM groups WHERE lower(name)=lower(?) LIMIT 1', (name,)).fetchone()
            finally:
                conn.close()
            if existing:
                return _permissions_redirect('groups', f'The group "{name}" already exists. Use a different group name.')
            try:
                _ensure_group(name)
                _audit('group_create', name)
            except Exception:
                return _permissions_redirect('groups', 'Could not create group. Use a different group name.')

        if action == 'delete_group':
            group_id = request.form.get('group_id')
            try:
                gid = int(group_id)
            except Exception:
                gid = None
            if gid:
                conn = _db()
                try:
                    g = conn.execute('SELECT id,name,is_admin FROM groups WHERE id=?', (gid,)).fetchone()
                    if g and not bool(int(g['is_admin'] or 0)):
                        conn.execute('DELETE FROM user_groups WHERE group_id=?', (gid,))
                        conn.execute('DELETE FROM group_pages WHERE group_id=?', (gid,))
                        conn.execute('DELETE FROM groups WHERE id=?', (gid,))
                        conn.commit()
                        _audit('group_delete', str(g['name']))
                finally:
                    conn.close()

        if action == 'save_group':
            group_id = request.form.get('group_id')
            try:
                gid = int(group_id)
            except Exception:
                gid = None
            if gid:
                is_admin_group = False
                try:
                    conn = _db()
                    try:
                        gr = conn.execute('SELECT is_admin FROM groups WHERE id=?', (gid,)).fetchone()
                        is_admin_group = bool(gr and int(gr['is_admin'] or 0))
                    finally:
                        conn.close()
                except Exception:
                    is_admin_group = False

                if not is_admin_group:
                    before_group = _group_settings_snapshot(gid)
                    if 'auth_idle_timeout_minutes_override_role' in request.form:
                        try:
                            _set_group_idle_timeout_override(gid, request.form.get('auth_idle_timeout_minutes_override_role'))
                        except Exception:
                            pass

                    keys = request.form.getlist('page_keys')
                    try:
                        _set_group_pages(gid, [str(k) for k in keys])
                    except Exception:
                        pass

                    # Per-group Routing allow-lists (only update if routing page is selected)
                    try:
                        if 'page:routing' in [str(k) for k in keys]:
                            outs_raw = request.form.get('videohub_allowed_outputs_role')
                            ins_raw = request.form.get('videohub_allowed_inputs_role')
                            _set_group_videohub_allowlists(gid, outs_raw, ins_raw)
                    except Exception:
                        pass

                    # Per-group VideoHub preset visibility (only update if VideoHub page is selected)
                    try:
                        if 'page:videohub' in [str(k) for k in keys]:
                            preset_ids_raw = request.form.get('videohub_allowed_presets_role')
                            _set_group_videohub_allowed_preset_ids(gid, preset_ids_raw)
                    except Exception:
                        pass

                    # Per-group VideoHub preset editing toggle (only update if VideoHub page is selected)
                    try:
                        if 'page:videohub' in [str(k) for k in keys]:
                            can_edit = (request.form.get('videohub_can_edit_presets_role') == 'on')
                            _set_group_videohub_can_edit_presets(gid, bool(can_edit))
                    except Exception:
                        pass

                    # Per-group Companion surface click allow-list.
                    try:
                        _set_group_companion_click_surfaces(gid, request.form.getlist('companion_click_surfaces_role'))
                    except Exception:
                        pass
                    # Per-group Personal Mix AUX allow-list.
                    try:
                        if 'page:digico_mixer' in [str(k) for k in keys]:
                            _set_group_digico_allowed_auxes(gid, request.form.getlist('digico_allowed_auxes_role'))
                    except Exception:
                        pass

                    # Per-group ATEM audio controls. Page access and channel
                    # grants are separate so different groups can combine.
                    try:
                        if 'page:atem_audio' in [str(k) for k in keys]:
                            _set_group_atem_audio_sources(gid, request.form.getlist('atem_allowed_audio_sources_role'))
                            _set_group_atem_can_solo_audio(gid, request.form.get('atem_can_solo_audio_role') == 'on')
                            _set_group_atem_can_monitor_audio(gid, request.form.get('atem_can_monitor_audio_role') == 'on')
                    except Exception:
                        pass
                    try:
                        _log_group_setting_changes(before_group, _group_settings_snapshot(gid))
                    except Exception:
                        pass

        if action == 'create_user':
            min_len = _auth_min_password_length()
            username = str(request.form.get('username') or '').strip()
            full_name = str(request.form.get('full_name') or '').strip()
            email = str(request.form.get('email') or '').strip()
            password = str(request.form.get('password') or '')
            group_ids = _form_group_ids()

            if not username:
                return _permissions_redirect('users', 'Enter a username.')
            if not full_name:
                return _permissions_redirect('users', 'Enter a full name.')
            if not email:
                return _permissions_redirect('users', 'Enter an email address.')
            if len(password) < min_len:
                return _permissions_redirect('users', f'Password must be at least {min_len} characters.')
            conn = _db()
            try:
                existing = _user_duplicate(conn, 'username', username)
                existing_email = _user_duplicate(conn, 'email', email)
            finally:
                conn.close()
            if existing:
                return _permissions_redirect('users', f'The user "{username}" already exists. Use a different username.')
            if existing_email:
                return _permissions_redirect('users', f'The email "{email}" is already being used. Use a different email address.')
            conn = _db()
            try:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                actor = _current_admin_user_id()
                cur = conn.execute(
                    """
                    INSERT INTO users(
                      username,full_name,email,password_hash,role_id,is_active,
                      created_at,updated_at,created_by,updated_by,password_changed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (username, full_name, email, generate_password_hash(password), None, 1, now, now, actor, actor, now),
                )
                _admin_replace_user_groups(conn, int(cur.lastrowid), group_ids)
                conn.commit()
                _audit('user_create', username)
            except Exception:
                return _permissions_redirect('users', 'Could not create user. Use a different username.')
            finally:
                conn.close()

        if action == 'reset_password':
            min_len = _auth_min_password_length()
            user_id = request.form.get('user_id')
            new_pw = str(request.form.get('new_password') or '')
            try:
                uid = int(user_id)
            except Exception:
                uid = None
            if not uid:
                return _permissions_redirect('users', 'Select a user before resetting a password.')
            if len(new_pw) < min_len:
                return _permissions_redirect('users', f'Password must be at least {min_len} characters.')
            conn = _db()
            try:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                conn.execute(
                    'UPDATE users SET password_hash=?, password_changed_at=?, updated_at=?, updated_by=? WHERE id=?',
                    (generate_password_hash(new_pw), now, now, _current_admin_user_id(), uid),
                )
                conn.commit()
                _audit('user_password_reset', f'id={uid}')
            finally:
                conn.close()

        if action == 'delete_user':
            user_id = request.form.get('user_id')
            try:
                uid = int(user_id)
            except Exception:
                uid = None
            if uid:
                conn = _db()
                try:
                    try:
                        current_uid = int(current_user.get_id())
                    except Exception:
                        current_uid = None
                    if current_uid == uid:
                        pass
                    else:
                        row = conn.execute('SELECT is_active FROM users WHERE id=?', (uid,)).fetchone()
                        is_active_user = bool(row and int(row['is_active'] or 0))
                        if is_active_user and _admin_user_has_admin_group(conn, uid) and _admin_active_admin_count(conn) <= 1:
                            pass
                        else:
                            conn.execute('DELETE FROM user_groups WHERE user_id=?', (uid,))
                            conn.execute('UPDATE user_sessions SET revoked_at=COALESCE(revoked_at, ?) WHERE user_id=?', (_now_str(), uid))
                            conn.execute('DELETE FROM users WHERE id=?', (uid,))
                            conn.commit()
                            _audit('user_delete', f'id={uid}')
                finally:
                    conn.close()

        if action in ('create_group', 'delete_group', 'save_group'):
            return _permissions_redirect('groups')
        if action in ('create_user', 'reset_password', 'delete_user'):
            return _permissions_redirect('users')

    conn = _db()
    try:
        groups = conn.execute(
            'SELECT id,name,is_system,is_admin,auth_idle_timeout_minutes_override,videohub_allowed_outputs,videohub_allowed_inputs,videohub_allowed_presets,videohub_can_edit_presets,companion_click_surfaces,digico_allowed_auxes,pixie_allowed_auditoriums,pixie_allowed_devices,pixie_allowed_scenes,atem_allowed_audio_sources,atem_can_solo_audio,atem_can_monitor_audio FROM groups ORDER BY is_system DESC, lower(name)'
        ).fetchall()
        group_pages = conn.execute('SELECT group_id,page_key FROM group_pages').fetchall()
        group_users = conn.execute(
            """
            SELECT ug.group_id,u.username
            FROM user_groups ug
            JOIN users u ON u.id=ug.user_id
            ORDER BY lower(u.username)
            """
        ).fetchall()
        users = conn.execute(
            """
            SELECT u.id,u.username,u.full_name,u.email,u.is_active,u.is_locked
            FROM users u
            ORDER BY lower(u.username)
            """
        ).fetchall()
        memberships = conn.execute(
            """
            SELECT ug.user_id,g.id AS group_id,g.name AS group_name,g.is_admin
            FROM user_groups ug
            JOIN groups g ON g.id=ug.group_id
            ORDER BY lower(g.name)
            """
        ).fetchall()
    finally:
        conn.close()

    pages = sorted([(k, v.get('name') or k) for k, v in _PAGE_REGISTRY.items()], key=lambda x: x[1].lower())
    group_to_pages: dict[int, set[str]] = {}
    for gp in group_pages or []:
        try:
            group_to_pages.setdefault(int(gp['group_id']), set()).add(str(gp['page_key']))
        except Exception:
            continue

    group_to_users: dict[int, list[str]] = {}
    for gu in group_users or []:
        try:
            group_to_users.setdefault(int(gu['group_id']), []).append(str(gu['username'] or ''))
        except Exception:
            continue

    group_to_vh: dict[int, dict[str, str]] = {}
    group_to_companion: dict[int, dict[str, list[str]]] = {}
    group_to_digico: dict[int, dict[str, list[str]]] = {}
    group_to_pixie: dict[int, dict[str, Any]] = {}
    group_to_atem: dict[int, dict[str, Any]] = {}
    for g in groups or []:
        try:
            gid = int(g['id'])
        except Exception:
            continue

        # Store raw text for editing; blank means "allow all".
        out_raw = g['videohub_allowed_outputs']
        in_raw = g['videohub_allowed_inputs']
        preset_raw = g['videohub_allowed_presets']
        can_edit_raw = g['videohub_can_edit_presets']
        try:
            out_s = '' if out_raw is None else str(out_raw).strip()
        except Exception:
            out_s = ''
        try:
            in_s = '' if in_raw is None else str(in_raw).strip()
        except Exception:
            in_s = ''
        try:
            preset_s = '' if preset_raw is None else str(preset_raw).strip()
        except Exception:
            preset_s = ''
        if out_s == '[]':
            out_s = ''
        if in_s == '[]':
            in_s = ''
        if preset_s == '[]':
            preset_s = ''
        try:
            can_edit = True if can_edit_raw is None else bool(int(can_edit_raw))
        except Exception:
            can_edit = True
        group_to_vh[gid] = {
            'outputs': out_s,
            'inputs': in_s,
            'presets': preset_s,
            'can_edit_presets': bool(can_edit),
        }
        group_to_companion[gid] = {
            'click_surfaces': _coerce_string_allow_list(g['companion_click_surfaces']),
        }
        group_to_digico[gid] = {
            'allowed_auxes': _coerce_string_allow_list(g['digico_allowed_auxes']),
        }
        pixie_auditoriums = _coerce_string_allow_list(g['pixie_allowed_auditoriums'])
        group_to_pixie[gid] = {
            'auditoriums': pixie_auditoriums,
            'devices': _coerce_pixie_device_permissions(g['pixie_allowed_devices'], pixie_auditoriums),
            'scenes': _coerce_string_allow_list(g['pixie_allowed_scenes']),
        }
        try:
            atem_can_solo = bool(int(g['atem_can_solo_audio'] or 0))
        except Exception:
            atem_can_solo = False
        try:
            atem_can_monitor = bool(int(g['atem_can_monitor_audio'] or 0))
        except Exception:
            atem_can_monitor = False
        group_to_atem[gid] = {
            'audio_sources': _coerce_string_allow_list(g['atem_allowed_audio_sources']),
            'can_solo': bool(atem_can_solo),
            'can_monitor': bool(atem_can_monitor),
        }

    user_to_groups: dict[int, list[sqlite3.Row]] = {}
    user_to_group_ids: dict[int, set[int]] = {}
    for m in memberships or []:
        try:
            uid = int(m['user_id'])
            gid = int(m['group_id'])
        except Exception:
            continue
        user_to_groups.setdefault(uid, []).append(m)
        user_to_group_ids.setdefault(uid, set()).add(gid)

    min_len = _auth_min_password_length()

    return render_template(
        'admin_permissions.html',
        page_title='Permissions',
        active_tab='groups' if str(request.args.get('tab') or '').lower() == 'groups' else 'users',
        feedback_error=str(request.args.get('error') or ''),
        groups=groups,
        pages=pages,
        group_to_pages=group_to_pages,
        group_to_vh=group_to_vh,
        group_to_companion=group_to_companion,
        group_to_digico=group_to_digico,
        digico_auxes=_digico_aux_options(),
        group_to_pixie=group_to_pixie,
        pixie_catalog=_pixie_permission_catalog(),
        group_to_atem=group_to_atem,
        atem_audio_sources=_get_atem_audio_sources_for_permissions(),
        companion_surfaces=_load_companion_surfaces(),
        group_to_users=group_to_users,
        users=users,
        user_to_groups=user_to_groups,
        user_to_group_ids=user_to_group_ids,
        min_len=min_len,
    )


@app.route('/admin/groups')
@require_page('page:admin', 'Admin')
def admin_groups_page():
    return redirect(url_for('admin_permissions_page', tab='groups') + '#groups')


@app.route('/api/admin/groups/<int:group_id>', methods=['POST'])
@require_page('page:admin', 'Admin')
def api_admin_group_update(group_id: int):
    """Update group settings via JSON (used by Groups auto-save UI)."""
    if _auth_enabled():
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        if not can_access('page:admin'):
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
        if not _validate_csrf():
            return jsonify({'ok': False, 'error': 'invalid CSRF token'}), 400
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}

    gid = int(group_id)
    is_admin_group = False
    try:
        conn = _db()
        try:
            gr = conn.execute('SELECT is_admin FROM groups WHERE id=?', (gid,)).fetchone()
            is_admin_group = bool(gr and int(gr['is_admin'] or 0))
        finally:
            conn.close()
    except Exception:
        is_admin_group = False

    # Admin groups are allow-all and not editable here.
    if not is_admin_group:
        before_group = _group_settings_snapshot(gid)
        if 'auth_idle_timeout_minutes_override_role' in data:
            try:
                _set_group_idle_timeout_override(gid, data.get('auth_idle_timeout_minutes_override_role'))
            except Exception:
                pass

        try:
            keys = data.get('page_keys')
            if not isinstance(keys, list):
                keys = []
            keys = [str(k) for k in keys]
            _set_group_pages(gid, keys)
        except Exception:
            pass

        # Per-group Routing allow-lists (only update if routing page is selected)
        try:
            keys_set = set([str(k) for k in (data.get('page_keys') or [])])
            if 'page:routing' in keys_set:
                outs_raw = data.get('videohub_allowed_outputs_role')
                ins_raw = data.get('videohub_allowed_inputs_role')
                _set_group_videohub_allowlists(gid, outs_raw, ins_raw)
        except Exception:
            pass

        # Per-group VideoHub preset visibility (only update if VideoHub page is selected)
        try:
            keys_set = set([str(k) for k in (data.get('page_keys') or [])])
            if 'page:videohub' in keys_set:
                preset_ids_raw = data.get('videohub_allowed_presets_role')
                _set_group_videohub_allowed_preset_ids(gid, preset_ids_raw)
        except Exception:
            pass

        # Per-group VideoHub preset editing toggle (only update if VideoHub page is selected)
        try:
            keys_set = set([str(k) for k in (data.get('page_keys') or [])])
            if 'page:videohub' in keys_set:
                enabled_raw = data.get('videohub_can_edit_presets_role')
                enabled = bool(enabled_raw) if isinstance(enabled_raw, bool) else (str(enabled_raw).strip().lower() in ('1', 'true', 'yes', 'y', 'on'))
                _set_group_videohub_can_edit_presets(gid, bool(enabled))
        except Exception:
            pass

        # Per-group Companion surface click allow-list.
        try:
            if 'companion_click_surfaces_role' in data:
                _set_group_companion_click_surfaces(gid, data.get('companion_click_surfaces_role'))
        except Exception:
            pass
        # Per-group Personal Mix AUX allow-list (only meaningful with page access).
        try:
            keys_set = set([str(k) for k in (data.get('page_keys') or [])])
            if 'page:digico_mixer' in keys_set and 'digico_allowed_auxes_role' in data:
                _set_group_digico_allowed_auxes(gid, data.get('digico_allowed_auxes_role'))
        except Exception:
            pass

        # Pixie auditorium, same-auditorium device, and scene grants.
        try:
            keys_set = set([str(k) for k in (data.get('page_keys') or [])])
            if 'page:pixie_controls' in keys_set:
                _set_group_pixie_permissions(
                    gid,
                    data.get('pixie_allowed_auditoriums_role'),
                    data.get('pixie_allowed_devices_role'),
                    data.get('pixie_allowed_scenes_role'),
                )
        except Exception:
            pass

        # Per-group ATEM audio controls. Page access and channel grants are
        # separate so different groups can combine.
        try:
            keys_set = set([str(k) for k in (data.get('page_keys') or [])])
            if 'page:atem_audio' in keys_set:
                _set_group_atem_audio_sources(gid, data.get('atem_allowed_audio_sources_role'))
                enabled_raw = data.get('atem_can_solo_audio_role')
                enabled = bool(enabled_raw) if isinstance(enabled_raw, bool) else (str(enabled_raw).strip().lower() in ('1', 'true', 'yes', 'y', 'on'))
                _set_group_atem_can_solo_audio(gid, bool(enabled))
                monitor_raw = data.get('atem_can_monitor_audio_role')
                monitor_enabled = bool(monitor_raw) if isinstance(monitor_raw, bool) else (str(monitor_raw).strip().lower() in ('1', 'true', 'yes', 'y', 'on'))
                _set_group_atem_can_monitor_audio(gid, bool(monitor_enabled))
        except Exception:
            pass
        try:
            _log_group_setting_changes(before_group, _group_settings_snapshot(gid))
        except Exception:
            pass

    return jsonify({'ok': True})


@app.route('/api/admin/users/<int:user_id>', methods=['POST'])
@require_page('page:admin', 'Admin')
def api_admin_user_update(user_id: int):
    """Auto-save user account status and group membership."""
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    group_ids_raw = data.get('group_ids')
    group_ids: list[int] = []
    if isinstance(group_ids_raw, list):
        for raw in group_ids_raw:
            try:
                gid = int(raw)
            except Exception:
                continue
            if gid > 0 and gid not in group_ids:
                group_ids.append(gid)
    is_active_raw = data.get('is_active', True)
    is_active = bool(is_active_raw) if isinstance(is_active_raw, bool) else (str(is_active_raw).strip().lower() in ('1', 'true', 'yes', 'y', 'on'))

    conn = _db()
    try:
        before = _user_access_snapshot(conn, int(user_id))
        ok = _admin_update_user(conn, int(user_id), group_ids, 1 if is_active else 0)
        if not ok:
            conn.rollback()
            return jsonify({'ok': False, 'error': 'Cannot remove or disable the last active admin user'}), 400
        conn.commit()
        after = _user_access_snapshot(conn, int(user_id))
        group_changes = _group_snapshot_diff(before.get('groups') or [], after.get('groups') or [])
        if before.get('is_active') != after.get('is_active') or group_changes.get('added_groups') or group_changes.get('removed_groups'):
            log_event(
                'user.access.update',
                f"Updated access for user '{after.get('username') or before.get('username') or user_id}'",
                source='api',
                status='success',
                target_type='user',
                target_id=int(user_id),
                details={
                    'user_id': int(user_id),
                    'username': after.get('username') or before.get('username'),
                    'active': {'old': before.get('is_active'), 'new': after.get('is_active')},
                    **group_changes,
                },
            )
    finally:
        conn.close()
    return jsonify({'ok': True})


def _generated_password() -> str:
    return secrets.token_urlsafe(12).replace('-', 'A').replace('_', '9')[:16]


def _user_access_snapshot(conn: sqlite3.Connection, user_id: int) -> dict:
    user = conn.execute('SELECT id,username,full_name,email,is_active FROM users WHERE id=?', (int(user_id),)).fetchone()
    groups = conn.execute(
        """
        SELECT g.id,g.name
        FROM user_groups ug
        JOIN groups g ON g.id=ug.group_id
        WHERE ug.user_id=?
        ORDER BY lower(g.name)
        """,
        (int(user_id),),
    ).fetchall()
    return {
        'id': int(user_id),
        'username': str(user['username'] or '') if user else '',
        'full_name': str(user['full_name'] or '') if user else '',
        'email': str(user['email'] or '') if user else '',
        'is_active': bool(int(user['is_active'] or 0)) if user else False,
        'groups': [{'id': int(g['id']), 'name': str(g['name'] or '')} for g in groups or []],
    }


def _group_snapshot_diff(old_groups: list[dict], new_groups: list[dict]) -> dict:
    old_by_id = {int(g.get('id')): dict(g) for g in old_groups or [] if g.get('id') is not None}
    new_by_id = {int(g.get('id')): dict(g) for g in new_groups or [] if g.get('id') is not None}
    added = [new_by_id[gid] for gid in sorted(set(new_by_id) - set(old_by_id))]
    removed = [old_by_id[gid] for gid in sorted(set(old_by_id) - set(new_by_id))]
    return {'added_groups': added, 'removed_groups': removed}


def _admin_user_detail_context(user_id: int, error: str | None = None, message: str | None = None) -> dict[str, Any]:
    conn = _db()
    try:
        user = conn.execute(
            """
            SELECT u.*,
                   cu.username AS created_by_name,
                   uu.username AS updated_by_name
            FROM users u
            LEFT JOIN users cu ON cu.id=u.created_by
            LEFT JOIN users uu ON uu.id=u.updated_by
            WHERE u.id=?
            """,
            (int(user_id),),
        ).fetchone()
        if not user:
            abort(404)
        groups = conn.execute(
            'SELECT id,name,is_admin,is_system FROM groups ORDER BY is_system DESC, lower(name)'
        ).fetchall()
        assigned = conn.execute('SELECT group_id FROM user_groups WHERE user_id=?', (int(user_id),)).fetchall()
        sessions_rows = conn.execute(
            """
            SELECT id,created_at,last_seen_at,revoked_at,ip,user_agent
            FROM user_sessions
            WHERE user_id=?
            ORDER BY revoked_at IS NOT NULL, last_seen_at DESC
            LIMIT 20
            """,
            (int(user_id),),
        ).fetchall()
        audit_rows = conn.execute(
            """
            SELECT
              ts,
              actor_username AS username,
              action,
              COALESCE(summary, details_json) AS detail,
              ip
            FROM activity_log
            WHERE actor_user_id=?
               OR (target_type='user' AND target_id=?)
               OR details_json LIKE ?
            ORDER BY id DESC
            LIMIT 30
            """,
            (int(user_id), str(int(user_id)), f'%id={int(user_id)}%'),
        ).fetchall()
    finally:
        conn.close()
    generated_password = session.pop('_admin_generated_password', None)
    return {
        'page_title': f'User: {user["username"]}',
        'user_row': user,
        'groups': groups,
        'assigned_group_ids': {int(r['group_id']) for r in assigned or []},
        'effective_permissions': _effective_permissions_for_user(int(user_id)),
        'sessions': sessions_rows,
        'audit_rows': audit_rows,
        'min_len': _auth_min_password_length(),
        'lockout_attempts': _auth_lockout_failed_attempts(),
        'admin_email_rows': _admin_email_rows(),
        'generated_password': generated_password,
        'error': error,
        'message': message,
        'saved': str(request.args.get('saved') or '').strip(),
    }


@app.route('/admin/users/<int:user_id>', methods=['GET', 'POST'])
@require_page('page:admin', 'Admin')
def admin_user_detail_page(user_id: int):
    try:
        _bootstrap_default_users_roles()
    except Exception:
        pass

    def _form_group_ids() -> list[int]:
        out: list[int] = []
        for raw in request.form.getlist('group_ids'):
            try:
                gid = int(raw)
            except Exception:
                continue
            if gid > 0 and gid not in out:
                out.append(gid)
        return out

    if request.method == 'POST':
        action = str(request.form.get('action') or '').strip()
        actor = _current_admin_user_id()
        conn = _db()
        try:
            user = conn.execute('SELECT * FROM users WHERE id=?', (int(user_id),)).fetchone()
            if not user:
                abort(404)
            now = _now_str()

            if action == 'update_profile':
                username = str(request.form.get('username') or '').strip()
                full_name = str(request.form.get('full_name') or '').strip()
                email = str(request.form.get('email') or '').strip()
                if not username:
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='Enter a username.'))
                if not full_name:
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='Enter a full name.'))
                if not email:
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='Enter an email address.'))
                if _user_duplicate(conn, 'username', username, user_id):
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='That username is already in use.'))
                if _user_duplicate(conn, 'email', email, user_id):
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='That email address is already in use.'))
                conn.execute(
                    'UPDATE users SET username=?,full_name=?,email=?,updated_at=?,updated_by=? WHERE id=?',
                    (username, full_name, email, now, actor, int(user_id)),
                )
                conn.commit()
                log_event(
                    'user.profile.update',
                    f"Updated profile for user '{username}'",
                    source='web',
                    status='success',
                    target_type='user',
                    target_id=int(user_id),
                    details={
                        'user_id': int(user_id),
                        'username': {'old': str(user['username'] or ''), 'new': username},
                        'full_name': {'old': str(user['full_name'] or ''), 'new': full_name},
                        'email': {'old': str(user['email'] or ''), 'new': email},
                    },
                )
                return redirect(url_for('admin_user_detail_page', user_id=int(user_id), saved='profile'))

            if action == 'update_access':
                is_active = request.form.get('is_active') == 'on'
                group_ids = _form_group_ids()
                if _would_remove_last_active_admin(conn, int(user_id), is_active=is_active, group_ids=group_ids):
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='Cannot remove or disable the last active admin user.'))
                before = _user_access_snapshot(conn, int(user_id))
                conn.execute(
                    'UPDATE users SET is_active=?,updated_at=?,updated_by=? WHERE id=?',
                    (1 if is_active else 0, now, actor, int(user_id)),
                )
                _admin_replace_user_groups(conn, int(user_id), group_ids)
                conn.commit()
                after = _user_access_snapshot(conn, int(user_id))
                group_changes = _group_snapshot_diff(before.get('groups') or [], after.get('groups') or [])
                if before.get('is_active') != after.get('is_active') or group_changes.get('added_groups') or group_changes.get('removed_groups'):
                    log_event(
                        'user.access.update',
                        f"Updated access for user '{after.get('username') or before.get('username') or user_id}'",
                        source='web',
                        status='success',
                        target_type='user',
                        target_id=int(user_id),
                        details={
                            'user_id': int(user_id),
                            'username': after.get('username') or before.get('username'),
                            'active': {'old': before.get('is_active'), 'new': after.get('is_active')},
                            **group_changes,
                        },
                    )
                return redirect(url_for('admin_user_detail_page', user_id=int(user_id), saved='access'))

            if action == 'lock_user':
                if actor == int(user_id):
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='You cannot lock your own account.'))
                if _would_remove_last_active_admin(conn, int(user_id), is_locked=True):
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='Cannot lock the last active admin user.'))
                conn.execute(
                    'UPDATE users SET is_locked=1,locked_at=?,locked_reason=?,updated_at=?,updated_by=? WHERE id=?',
                    (now, 'Locked by admin', now, actor, int(user_id)),
                )
                _revoke_user_sessions(conn, int(user_id))
                conn.commit()
                _audit('user_lock', f'id={int(user_id)}')
                return redirect(url_for('admin_user_detail_page', user_id=int(user_id), saved='locked'))

            if action == 'unlock_user':
                conn.execute(
                    """
                    UPDATE users
                    SET is_locked=0,locked_at=NULL,locked_reason=NULL,failed_login_count=0,last_failed_login_at=NULL,updated_at=?,updated_by=?
                    WHERE id=?
                    """,
                    (now, actor, int(user_id)),
                )
                conn.commit()
                _audit('user_unlock', f'id={int(user_id)}')
                return redirect(url_for('admin_user_detail_page', user_id=int(user_id), saved='unlocked'))

            if action == 'reset_password':
                min_len = _auth_min_password_length()
                force_change = request.form.get('force_password_change') == 'on'
                generate_temp = request.form.get('generate_password') == '1'
                new_pw = _generated_password() if generate_temp else str(request.form.get('new_password') or '')
                if len(new_pw) < min_len:
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error=f'Password must be at least {min_len} characters.'))
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash=?,password_changed_at=?,force_password_change=?,failed_login_count=0,last_failed_login_at=NULL,
                        updated_at=?,updated_by=?
                    WHERE id=?
                    """,
                    (generate_password_hash(new_pw), now, 1 if force_change else 0, now, actor, int(user_id)),
                )
                if force_change:
                    _revoke_user_sessions(conn, int(user_id))
                conn.commit()
                if generate_temp:
                    session['_admin_generated_password'] = new_pw
                _audit('user_password_reset', f'id={int(user_id)} force_change={int(force_change)} generated={int(generate_temp)}')
                return redirect(url_for('admin_user_detail_page', user_id=int(user_id), saved='password'))

            if action == 'sign_out_everywhere':
                _revoke_user_sessions(conn, int(user_id))
                conn.commit()
                _audit('user_sessions_revoke', f'id={int(user_id)}')
                return redirect(url_for('admin_user_detail_page', user_id=int(user_id), saved='sessions'))

            if action == 'delete_user':
                if actor == int(user_id):
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='You cannot delete your own account.'))
                if _would_remove_last_active_admin(conn, int(user_id), is_active=False):
                    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, error='Cannot delete the last active admin user.'))
                username = str(user['username'] or '')
                conn.execute('DELETE FROM user_groups WHERE user_id=?', (int(user_id),))
                conn.execute('UPDATE user_sessions SET revoked_at=COALESCE(revoked_at, ?) WHERE user_id=?', (now, int(user_id)))
                conn.execute('DELETE FROM users WHERE id=?', (int(user_id),))
                conn.commit()
                _audit('user_delete', f'id={int(user_id)} username={username}')
                return redirect(url_for('admin_permissions_page', tab='users') + '#users')
        finally:
            conn.close()

    saved = str(request.args.get('saved') or '').strip()
    message = None
    if saved:
        message = 'Changes saved.'
    return render_template('admin_user_detail.html', **_admin_user_detail_context(user_id, message=message))


@app.route('/admin/users', methods=['GET', 'POST'])
@require_page('page:admin', 'Admin')
def admin_users_page():
    return redirect(url_for('admin_permissions_page', tab='users') + '#users')


@app.route('/')
@require_page('page:home', 'Home')
def home():
    # Provide lightweight status data for the Home page.
    try:
        cfg = utils.get_config()
    except Exception:
        cfg = {}

    try:
        events_file = str(cfg.get('EVENTS_FILE', 'events.json'))
    except Exception:
        events_file = 'events.json'

    try:
        companion_ip = str(cfg.get('companion_ip', '127.0.0.1'))
    except Exception:
        companion_ip = '127.0.0.1'
    try:
        companion_port = int(cfg.get('companion_port', 8000))
    except Exception:
        companion_port = 8000

    try:
        propresenter_ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
    except Exception:
        propresenter_ip = '127.0.0.1'
    try:
        propresenter_port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        propresenter_port = 1025

    companion_hostport = f"{companion_ip}:{companion_port}"
    propresenter_hostport = f"{propresenter_ip}:{propresenter_port}"

    # List base event times (not individual triggers)
    event_times: list[dict[str, str]] = []
    try:
        from package.apps.calendar import storage
        if hasattr(storage, 'load_events_safe'):
            events = storage.load_events_safe(events_file)
        else:
            events = storage.load_events(events_file)
        for e in events or []:
            try:
                if not getattr(e, 'active', True):
                    continue
            except Exception:
                pass
            try:
                day_obj = getattr(e, 'day', None)
                day_name = getattr(day_obj, 'name', None) or str(day_obj or '')
            except Exception:
                day_name = ''
            try:
                day_val = int(getattr(getattr(e, 'day', None), 'value', 999))
            except Exception:
                day_val = 999
            try:
                t = getattr(e, 'time', None)
                time_str = t.strftime('%H:%M') if t is not None else ''
            except Exception:
                time_str = ''
            try:
                name = str(getattr(e, 'name', '') or '').strip()
            except Exception:
                name = ''

            event_times.append({'name': name or '(unnamed)', 'day': day_name, 'time': time_str, '_day': str(day_val)})

        def _sort_key(ev: dict) -> tuple[int, str, str]:
            try:
                dv = int(ev.get('_day') or 999)
            except Exception:
                dv = 999
            return (dv, str(ev.get('time') or ''), str(ev.get('name') or ''))

        event_times.sort(key=_sort_key)
        # drop internal sort key
        for ev in event_times:
            ev.pop('_day', None)
    except Exception:
        event_times = []

    return render_template(
        'home.html',
        events_file=events_file,
        companion_hostport=companion_hostport,
        propresenter_hostport=propresenter_hostport,
        event_times=event_times,
    )


def _compute_upcoming_triggers_payload(*, events_file: str, limit: int = 3) -> dict:
    """Compute next upcoming triggers across active events.

    Uses the same scheduling logic as the background scheduler: next weekly
    occurrence + trigger offsets.
    """
    from datetime import datetime
    import heapq

    now = datetime.now().replace(microsecond=0)

    try:
        from package.apps.calendar import storage
        from package.apps.calendar.scheduler import next_weekly_occurrence, push_triggers_for_occurrence
    except Exception:
        return {'now_ms': int(time.time() * 1000), 'triggers': []}

    # Map buttonURL -> template info (for nicer display)
    tpl_by_url: dict[str, dict] = {}
    try:
        tree, arr, _ = _load_button_templates_bundle()
        for tpl in arr or []:
            if not isinstance(tpl, dict):
                continue
            url = _button_template_effective_url(tpl)
            if url:
                tpl_by_url[url] = tpl
    except Exception:
        tpl_by_url = {}

    try:
        loaded = storage.load_events_safe(events_file) if hasattr(storage, 'load_events_safe') else storage.load_events(events_file)
    except Exception:
        loaded = []

    heap = []
    for ev in loaded or []:
        try:
            if not getattr(ev, 'active', True):
                continue
        except Exception:
            pass

        occ = None
        try:
            occ = next_weekly_occurrence(ev, now)
        except Exception:
            occ = None
        if occ is None:
            continue
        try:
            push_triggers_for_occurrence(heap, ev, occ, now)
        except Exception:
            continue

    try:
        heapq.heapify(heap)
    except Exception:
        pass

    out = []
    for job in sorted(heap)[: max(0, int(limit))]:
        try:
            due = getattr(job, 'due', None)
            if due is None:
                continue
            due = due.replace(microsecond=0)
            due_ms = int(due.timestamp() * 1000)
            seconds_until = int((due - now).total_seconds())
            if seconds_until < 0:
                # should not happen (we only schedule future jobs), but be safe
                continue

            ev = getattr(job, 'event', None)
            event_name = str(getattr(ev, 'name', '') or '').strip() if ev is not None else ''
            event_id = getattr(ev, 'id', None) if ev is not None else None

            trig = getattr(job, 'trigger', None)
            action_type = str(getattr(trig, 'actionType', 'companion') or 'companion').lower() if trig is not None else 'companion'
            url = str(getattr(trig, 'buttonURL', '') or '').strip() if trig is not None else ''
            api = getattr(trig, 'api', None) if trig is not None else None
            timer = getattr(trig, 'timer', None) if trig is not None else None
            pattern = _extract_pattern_from_button_url(url) or ''

            offset_min = None
            try:
                offset_min = int(getattr(trig, 'offset_minutes', 0)) if trig is not None else 0
            except Exception:
                offset_min = 0
            if offset_min > 0:
                offset_label = f"+{offset_min}m"
            elif offset_min < 0:
                offset_label = f"{offset_min}m"
            else:
                offset_label = "0m"

            tpl = tpl_by_url.get(url) if (url and action_type != 'api') else None
            button_label = ''
            button_pattern = ''
            try:
                if isinstance(tpl, dict):
                    button_label = str(tpl.get('label') or '').strip()
                    button_pattern = str(tpl.get('pattern') or '').strip()
            except Exception:
                pass
            if not button_pattern:
                button_pattern = pattern

            # Prefer explicit trigger name; otherwise fall back per action type.
            trig_name = ''
            try:
                trig_name = str(getattr(trig, 'name', '') or '').strip() if trig is not None else ''
            except Exception:
                trig_name = ''
            if not trig_name:
                if action_type == 'api':
                    try:
                        trig_name = str((api or {}).get('path') or '').strip() if isinstance(api, dict) else ''
                    except Exception:
                        trig_name = ''
                elif action_type == 'timer':
                    try:
                        tpreset = int((timer or {}).get('preset')) if isinstance(timer, dict) else None
                    except Exception:
                        tpreset = None
                    ttime = str((timer or {}).get('time') or '').strip() if isinstance(timer, dict) else ''
                    tapply = bool((timer or {}).get('apply', False)) if isinstance(timer, dict) else False
                    if tpreset is not None:
                        trig_name = f"Timer preset #{tpreset}" + (f" -> {ttime}" if ttime else "") + (" (apply)" if tapply else "")
                    else:
                        trig_name = "Timer preset"
                else:
                    trig_name = button_label or url or ''

            out.append(
                {
                    'due_ms': due_ms,
                    'seconds_until': seconds_until,
                    'event': event_name or '(unnamed)',
                    'event_id': event_id,
                    'offset_min': offset_min,
                    'offset': offset_label,
                    'name': trig_name,
                    'actionType': action_type,
                    'buttonURL': url if action_type == 'companion' else '',
                    'api': api if (action_type == 'api' and isinstance(api, dict)) else None,
                    'timer': timer if (action_type == 'timer' and isinstance(timer, dict)) else None,
                    'button': {
                        'label': button_label,
                        'pattern': button_pattern,
                    },
                }
            )
        except Exception:
            continue

    return {'now_ms': int(now.timestamp() * 1000), 'triggers': out}


@app.route('/api/upcoming_triggers')
def api_upcoming_triggers():
    try:
        cfg = utils.get_config()
    except Exception:
        cfg = {}

    try:
        events_file = str(cfg.get('EVENTS_FILE', 'events.json'))
    except Exception:
        events_file = 'events.json'

    # optional limit override (defaults to dashboard-friendly 3)
    try:
        limit = int(request.args.get('limit', 3))
    except Exception:
        limit = 3
    limit = max(0, min(limit, 500))

    now = time.time()
    with _status_cache_lock:
        if (
            _upcoming_triggers_cache.get('payload') is not None
            and _upcoming_triggers_cache.get('events_file') == events_file
            and int(_upcoming_triggers_cache.get('limit') or 0) == int(limit)
            and (now - float(_upcoming_triggers_cache.get('ts', 0.0))) < _UPCOMING_TRIGGERS_TTL_SECONDS
        ):
            return jsonify(_upcoming_triggers_cache.get('payload'))

    payload = _compute_upcoming_triggers_payload(events_file=events_file, limit=limit)

    with _status_cache_lock:
        _upcoming_triggers_cache['ts'] = now
        _upcoming_triggers_cache['events_file'] = events_file
        _upcoming_triggers_cache['limit'] = limit
        _upcoming_triggers_cache['payload'] = payload

    resp = jsonify(payload)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/calendar')
@require_page('page:calendar', 'Schedule')
def calendar_page():
    return render_template('calendar.html')


@app.route('/surface-controls')
@require_page('page:surface_controls', 'Surface Controls')
def surface_controls_page():
    return render_template('surface_controls.html', companion_surface_displays=_load_surface_control_displays())


@app.route('/calendar/triggers')
@require_page('page:calendar', 'Schedule')
def calendar_triggers_page():
    return render_template('calendar_triggers.html')


@app.route('/templates')
@require_page('page:templates', 'Templates')
def templates_page():
    return render_template('templates.html')


@app.route('/api-reference')
@require_page('page:api_reference', 'API Reference')
def api_reference_page():
    p = Path.cwd() / 'API_REFERENCE.md'
    try:
        content = p.read_text(encoding='utf-8') if p.exists() else "# API Reference\n\nMissing API_REFERENCE.md."
    except Exception:
        content = "# API Reference\n\nFailed to read API_REFERENCE.md."
    return render_template('api_reference.html', api_reference_content=content)


@app.route('/videohub')
@require_page('page:videohub', 'VideoHub')
def videohub_page():
    # Allow-list is configured as 1-based preset IDs, per group.
    # Blank/NULL => allow all.
    allowed_preset_ids: list[int] = []
    can_edit_presets = True
    try:
        if _auth_enabled() and getattr(current_user, 'is_authenticated', False):
            uid = int(current_user.get_id())
            allowed_preset_ids = _effective_videohub_preset_ids_for_user(uid)
            can_edit_presets = _effective_videohub_can_edit_presets_for_user(uid)
    except Exception:
        pass

    return render_template(
        'videohub.html',
        allowed_preset_ids=allowed_preset_ids,
        can_edit_presets=bool(can_edit_presets),
        can_manage_rooms=bool(_can_manage_videohub_rooms_for_current_user()),
    )


@app.route('/videohub/rooms')
@require_page('page:videohub', 'VideoHub')
def videohub_rooms_page():
    if not _can_manage_videohub_rooms_for_current_user():
        return abort(403)
    return render_template('videohub_rooms.html')


@app.route('/videohub/input-select')
@require_page('page:videohub', 'VideoHub')
def videohub_input_select_page():
    return render_template(
        'videohub_input_select.html',
        can_edit_presets=bool(_can_edit_videohub_presets_current_user()),
    )


@app.route('/videohub/monitor')
@require_page('page:videohub', 'VideoHub')
def videohub_monitor_page():
    return render_template('videohub_monitor.html')


@app.route('/foyer-audio')
@require_page('page:atem_audio', 'Record Audio')
def foyer_audio_page():
    allowed_source_ids: list[str] = []
    allow_all = True
    can_solo = True
    can_monitor = True
    try:
        if _auth_enabled():
            if not getattr(current_user, 'is_authenticated', False):
                allowed_source_ids = []
                allow_all = False
                can_solo = False
                can_monitor = False
            else:
                uid = int(current_user.get_id())
                if _user_is_admin(uid):
                    allowed_source_ids = []
                    allow_all = True
                    can_solo = True
                    can_monitor = True
                else:
                    allowed_source_ids = _effective_atem_audio_source_ids_for_user(uid)
                    allow_all = False
                    can_solo = _effective_atem_can_solo_audio_for_user(uid)
                    can_monitor = _effective_atem_can_monitor_audio_for_user(uid)
    except Exception:
        allowed_source_ids = []
        allow_all = False
        can_solo = False
        can_monitor = False
    return render_template(
        'foyer_audio.html',
        allowed_source_ids=allowed_source_ids,
        atem_allow_all=bool(allow_all),
        atem_can_solo=bool(can_solo),
        atem_can_monitor=bool(can_monitor),
    )


def _atem_audio_access_debug_for_user(user_id: int | None) -> dict[str, Any]:
    if user_id is None:
        return {
            'authenticated': False,
            'is_admin': False,
            'can_access_page': False,
            'effective_allowed_source_ids': [],
            'effective_can_solo': False,
            'effective_can_monitor': False,
            'groups': [],
        }
    groups = _get_user_groups(user_id)
    out_groups: list[dict[str, Any]] = []
    for row in groups:
        gid = None
        name = ''
        try:
            gid = int(row['id'])
            name = str(row['name'] or '')
        except Exception:
            pass
        page_keys: list[str] = []
        if gid is not None:
            conn = _db()
            try:
                page_rows = conn.execute('SELECT page_key FROM group_pages WHERE group_id=? ORDER BY page_key', (gid,)).fetchall()
                page_keys = [str(r['page_key']) for r in page_rows or []]
            finally:
                conn.close()
        raw_sources = ''
        raw_can_solo = None
        raw_can_monitor = None
        try:
            raw_sources = '' if row['atem_allowed_audio_sources'] is None else str(row['atem_allowed_audio_sources'])
        except Exception:
            raw_sources = ''
        try:
            raw_can_solo = row['atem_can_solo_audio']
        except Exception:
            raw_can_solo = None
        try:
            raw_can_monitor = row['atem_can_monitor_audio']
        except Exception:
            raw_can_monitor = None
        out_groups.append({
            'id': gid,
            'name': name,
            'is_admin': bool(int(row['is_admin'] or 0)) if 'is_admin' in row.keys() else False,
            'page_keys': page_keys,
            'has_foyer_audio_page': 'page:atem_audio' in page_keys,
            'raw_atem_allowed_audio_sources': raw_sources,
            'parsed_atem_allowed_audio_sources': _coerce_string_allow_list(raw_sources),
            'raw_atem_can_solo_audio': raw_can_solo,
            'atem_can_solo_audio': bool(int(raw_can_solo or 0)) if raw_can_solo is not None else False,
            'raw_atem_can_monitor_audio': raw_can_monitor,
            'atem_can_monitor_audio': bool(int(raw_can_monitor or 0)) if raw_can_monitor is not None else False,
        })

    source_state: dict[str, Any] = {'ok': False, 'source_ids': [], 'error': ''}
    try:
        atem = _get_atem_client_from_config()
        if atem is not None:
            state = atem.get_audio_state()
            sources = state.get('sources') if isinstance(state, dict) else []
            source_state = {
                'ok': True,
                'source_ids': [str(s.get('id')) for s in sources if isinstance(s, dict)],
                'source_labels': [{'id': str(s.get('id')), 'label': str(s.get('label') or '')} for s in sources if isinstance(s, dict)],
                'metering': state.get('metering') if isinstance(state, dict) else None,
                'levels': [
                    {
                        'id': str(s.get('id')),
                        'label': str(s.get('label') or ''),
                        'level': s.get('level'),
                    }
                    for s in sources
                    if isinstance(s, dict)
                ],
            }
    except Exception as e:
        try:
            fallback = AtemAudioClient.fallback_sources() if AtemAudioClient is not None else []
            source_state = {
                'ok': False,
                'source_ids': [str(s.get('id')) for s in fallback if isinstance(s, dict)],
                'source_labels': [{'id': str(s.get('id')), 'label': str(s.get('label') or '')} for s in fallback if isinstance(s, dict)],
                'levels': [],
                'error': str(e),
            }
        except Exception:
            source_state = {'ok': False, 'source_ids': [], 'error': str(e)}

    return {
        'authenticated': True,
        'user_id': int(user_id),
        'is_admin': _user_is_admin(user_id),
        'can_access_page': _user_allows_page(user_id, 'page:atem_audio'),
        'effective_allowed_source_ids': _effective_atem_audio_source_ids_for_user(user_id),
        'effective_can_solo': _effective_atem_can_solo_audio_for_user(user_id),
        'effective_can_monitor': _effective_atem_can_monitor_audio_for_user(user_id),
        'groups': out_groups,
        'atem_sources_seen_by_tdeck': source_state,
    }


@app.route('/foyer-audio/debug')
@require_page('page:atem_audio', 'Record Audio')
def foyer_audio_debug_page():
    try:
        if _auth_enabled() and getattr(current_user, 'is_authenticated', False):
            payload = _atem_audio_access_debug_for_user(int(current_user.get_id()))
        else:
            payload = {
                'authenticated': not _auth_enabled(),
                'auth_enabled': _auth_enabled(),
                'is_admin': True,
                'can_access_page': True,
                'effective_allowed_source_ids': [],
                'effective_can_solo': True,
                'groups': [],
            }
    except Exception as e:
        payload = {'ok': False, 'error': str(e)}
    return jsonify(payload)


@app.route('/routing')
@require_page('page:routing', 'Routing')
def routing_page():
    # Allow-lists are configured as 1-based indices, per group.
    # Blank/NULL => allow all.
    allowed_outputs: list[int] = []
    allowed_inputs: list[int] = []
    try:
        if _auth_enabled() and getattr(current_user, 'is_authenticated', False):
            ro, ri = _effective_videohub_allowlists_for_user(int(current_user.get_id()))
            allowed_outputs = ro
            allowed_inputs = ri
    except Exception:
        pass

    return render_template('routing.html', allowed_outputs=allowed_outputs, allowed_inputs=allowed_inputs)


_media_library_lock = threading.Lock()
_media_operation_lock = threading.RLock()
_media_library_instance = None
_MEDIA_CONFIG_DEFAULTS = {
    'atem_media_enabled': False,
    'atem_media_node_path': '',
    'atem_media_destinations': [],
}
_MEDIA_ACTIVE_JOBS = {'queued', 'preparing', 'uploading', 'selecting'}


def _media_library_root() -> Path:
    override = os.environ.get('TDECK_MEDIA_DIR', '').strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name != 'nt' and Path('/data').is_dir():
        return Path('/data/media_library')
    return Path(__file__).resolve().parent / 'media_library'


def _get_media_library():
    from media_library import MediaLibrary
    global _media_library_instance
    with _media_library_lock:
        if _media_library_instance is None:
            _media_library_instance = MediaLibrary(_media_library_root())
        return _media_library_instance


def _get_atem_media_manager():
    from atem_media import get_atem_media_manager
    return get_atem_media_manager(utils.get_config(), _get_media_library())


def _active_media_job():
    from atem_media import peek_atem_media_job
    return peek_atem_media_job() or {}


def _serialize_media_configuration(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _media_operation_lock:
            return fn(*args, **kwargs)
    return wrapped


def _media_permissions() -> dict[str, bool]:
    return {
        action: bool(can_access('page:media') and can_access('page:media_' + action))
        for action in ('upload', 'manage', 'load')
    }


def _media_action_denied(action: str):
    log_event('security.media.permission_denied', f'Denied media {action} without its group permission',
              status='warning', details={'capability': 'page:media_' + action})
    return _api_json_error(403, 'forbidden', f'This action requires the Media {action} permission.')


def _media_item_payload(item: dict) -> dict:
    return {
        **item,
        'url': url_for('media_image', media_id=item['id']),
        'thumbnail_url': url_for('media_thumbnail', media_id=item['id']),
    }


def _media_api_failure(action: str, error: Exception, status_code: int = 400):
    if isinstance(error, KeyError):
        message, status_code = 'Image not found.', 404
    elif isinstance(error, (OSError, RuntimeError)):
        message, status_code = 'The media operation could not finish. Check the Activity Log.', 503
    else:
        message = str(error)
    log_event(action, message, status='failure', details={'error': str(error)})
    return jsonify({'ok': False, 'error': message}), status_code


@app.route('/media')
@require_page('page:media', 'Media Library')
def media_page():
    return render_template(
        'media.html', media_permissions=_media_permissions(),
        can_configure_media=can_access('page:config'),
    )


@app.route('/config/atem-media')
@require_page('page:config', 'Config')
def atem_media_setup_page():
    return render_template('media.html', setup_only=True, config_active_tab='atem-media',
                           media_permissions={'upload': False, 'manage': False, 'load': False},
                           can_configure_media=True)


@app.route('/media/images/<media_id>.png')
@require_page('page:media', 'Media Library')
def media_image(media_id: str):
    return _send_media_image(media_id, thumbnail=False)


@app.route('/media/thumbnails/<media_id>.png')
@require_page('page:media', 'Media Library')
def media_thumbnail(media_id: str):
    return _send_media_image(media_id, thumbnail=True)


def _send_media_image(media_id: str, *, thumbnail: bool):
    try:
        # Open while the operation lock is held so a concurrent deletion cannot
        # substitute a missing path between validation and response creation.
        with _media_operation_lock:
            path = _get_media_library().path(media_id, thumbnail=thumbnail)
            response = send_file(path, mimetype='image/png', conditional=True)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    except (KeyError, FileNotFoundError):
        abort(404)


@app.route('/api/media')
def api_media_list():
    try:
        return jsonify({
            'ok': True, 'items': [_media_item_payload(item) for item in _get_media_library().list()],
            'permissions': _media_permissions(),
        })
    except (ValueError, OSError, RuntimeError) as error:
        return _media_api_failure('media.library.read', error)


@app.route('/api/media/upload', methods=['POST'])
def api_media_upload():
    if not _media_permissions()['upload']:
        return _media_action_denied('upload')
    upload = request.files.get('file')
    if upload is None:
        return _api_json_error(400, 'missing_file', 'Choose an image to upload.')
    try:
        item = _get_media_library().upload(upload.stream, upload.filename or '', request.form.get('name', ''))
        log_event('media.image.upload', f"Uploaded image '{item['name']}'", status='success',
                  target_type='media_image', target_id=item['id'], details=item)
        return jsonify({'ok': True, 'item': _media_item_payload(item)}), 201
    except (ValueError, OSError, RuntimeError) as error:
        return _media_api_failure('media.image.upload', error)


@app.route('/api/media/<media_id>', methods=['PATCH', 'DELETE'])
def api_media_edit(media_id: str):
    if not _media_permissions()['manage']:
        return _media_action_denied('manage')
    action = 'media.image.delete' if request.method == 'DELETE' else 'media.image.update'
    try:
        with _media_operation_lock:
            library = _get_media_library()
            existing = library.get(media_id)
            if request.method == 'DELETE':
                job = _active_media_job()
                if job.get('status') in _MEDIA_ACTIVE_JOBS and job.get('mediaId') == media_id:
                    return _api_json_error(409, 'busy', 'This image is being loaded. Wait for the job to finish.')
                item = library.delete(media_id)
            else:
                data = request.get_json(silent=True)
                if not isinstance(data, dict) or set(data) - {'name', 'preset'}:
                    raise ValueError('Supply an image name and/or preset flag.')
                preset = data.get('preset', existing['preset'])
                if not isinstance(preset, bool):
                    raise ValueError('Preset must be true or false.')
                item = library.update(media_id, data.get('name', existing['name']), preset=preset)
        verb = 'Deleted' if request.method == 'DELETE' else 'Updated'
        log_event(action, f"{verb} image '{item['name']}'", status='success',
                  target_type='media_image', target_id=media_id, details=item)
        return jsonify({'ok': True, 'item': _media_item_payload(item)})
    except (KeyError, ValueError, OSError, RuntimeError) as error:
        return _media_api_failure(action, error)


@app.route('/api/atem/media/state')
def api_atem_media_state():
    try:
        return jsonify({**_get_atem_media_manager().snapshot(), 'ok': True})
    except (ValueError, OSError, RuntimeError) as error:
        return jsonify({'ok': True, 'enabled': False, 'connected': False, 'destinations': [],
                        'players': [], 'stills': [], 'job': None, 'error': str(error)})


@app.route('/api/atem/media/load', methods=['POST'])
def api_atem_media_load():
    if not _media_permissions()['load']:
        return _media_action_denied('load')
    from atem_media import BusyError
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _api_json_error(400, 'invalid_request', 'Choose an image and a configured player.')
    uid, username, display = _activity_current_actor()
    actor = {'actor_user_id': uid, 'actor_username': username, 'actor_display': display,
             'ip': _activity_request_ip(), 'request_path': request.path, 'source': 'web'}

    def completed(job):
        success = job.get('status') == 'succeeded'
        summary = (f"Loaded '{job.get('mediaName', '')}' on ATEM Media Player {job.get('player')}"
                   if success else f"Failed to load ATEM Media Player {job.get('player')}")
        log_event('atem.media.load', summary, status='success' if success else 'failure',
                  target_type='atem_media_player', target_id=job.get('player'), details=job, **actor)

    try:
        with _media_operation_lock:
            media_id = data.get('media_id')
            if not isinstance(media_id, str):
                raise ValueError('Choose a saved image.')
            _get_media_library().get(media_id)
            job = _get_atem_media_manager().load(media_id, data.get('player'), on_complete=completed)
        log_event('atem.media.load.queued', 'Queued image for ATEM media player', status='info',
                  target_type='atem_media_player', target_id=job.get('player'), details=job)
        return jsonify({'ok': True, 'job': job}), 202
    except BusyError as error:
        return _media_api_failure('atem.media.load', error, 409)
    except (KeyError, ValueError, OSError, RuntimeError) as error:
        return _media_api_failure('atem.media.load', error)


@app.route('/api/config/atem-media', methods=['GET', 'PUT'])
def api_atem_media_config():
    from atem_media import validate_media_config
    if request.method == 'GET':
        cfg = utils.get_config()
        return jsonify({'ok': True, 'config': {key: cfg.get(key, value) for key, value in _MEDIA_CONFIG_DEFAULTS.items()}})
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - set(_MEDIA_CONFIG_DEFAULTS):
        return _api_json_error(400, 'invalid_request', 'Supply only ATEM media setup fields.')
    try:
        with _media_operation_lock:
            job = _active_media_job()
            if job.get('status') in _MEDIA_ACTIVE_JOBS:
                return _api_json_error(409, 'busy', 'Wait for the current image load to finish before changing setup.')
            cfg = validate_media_config({**utils.get_config(), **data})
            if (cfg.get('atem_media_node_path', '') != utils.get_config().get('atem_media_node_path', '')
                    and _auth_enabled() and not _can_manage_service_tokens_for_current_user()):
                return _api_json_error(403, 'forbidden', 'Only an administrator may change the server Node.js executable.')
            if not write_json(Path(utils.CONFIG_FILE).resolve(), cfg):
                raise OSError('Could not save ATEM media configuration.')
            utils.reload_config(force=True)
        selected = {key: cfg.get(key, value) for key, value in _MEDIA_CONFIG_DEFAULTS.items()}
        log_event('atem.media.config.update', 'Updated ATEM media setup', status='success',
                  details={'enabled': selected['atem_media_enabled'], 'destinations': selected['atem_media_destinations']})
        return jsonify({'ok': True, 'config': selected})
    except (ValueError, OSError, RuntimeError) as error:
        return _media_api_failure('atem.media.config.update', error)


@app.route('/timers')
@require_page('page:timers', 'Timers')
def timers_page():
    return render_template('timers.html')


@app.route('/pixie')
@require_page('page:pixie_controls', 'Pixie Controls')
def pixie_controls_page():
    return render_template('pixie_controls.html')


@app.route('/personal-mixes')
@require_page('page:digico_mixer', 'Personal Mixes')
def personal_mixes_page():
    return render_template('personal_mixes.html')

@app.route('/config')
@require_page('page:config', 'Config')
def config_page():
    return render_template('config.html')


@app.route('/config/digico')
@require_page('page:config', 'Config')
def digico_config_page():
    return render_template('digico_setup.html')


@app.route('/config/tvs')
@require_page('page:config', 'Config')
def hisense_config_page():
    return render_template('hisense_setup.html')


@app.route('/config/pixie')
@require_page('page:config', 'Config')
def pixie_config_page():
    return render_template('pixie_setup.html')


@app.route('/config/export', methods=['GET', 'POST'])
@require_page('page:config', 'Config')
def config_export_page():
    items = _config_transport_items()
    if request.method == 'POST':
        selected = request.form.getlist('items')
        try:
            data, exported = _create_config_transport_zip(selected, reason='manual-export')
            if not exported:
                return render_template('config_export.html', items=items, error='Select at least one available item to export.')
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            filename = f'tdeck-config-{stamp}.zip'
            _config_transport_log(f"Exported config package {filename}: {', '.join(str(i.get('path')) for i in exported)}")
            try:
                _audit('config_export', f"items={len(exported)}")
            except Exception:
                pass
            return send_file(
                io.BytesIO(data),
                mimetype='application/zip',
                as_attachment=True,
                download_name=filename,
            )
        except Exception as e:
            _config_transport_log(f"Export failed: {e}")
            try:
                _audit('config_export_fail', str(e))
            except Exception:
                pass
            return render_template('config_export.html', items=items, error=str(e))
    return render_template('config_export.html', items=items)


@app.route('/config/import')
@require_page('page:config', 'Config')
def config_import_page():
    return render_template('config_import.html')


@app.route('/config/api-tokens')
@require_page('page:config', 'Config')
def config_api_tokens_page():
    if not _can_manage_service_tokens_for_current_user():
        abort(403)
    return render_template('config_api_tokens.html', service_token_scopes=SERVICE_TOKEN_SCOPES)


@app.route('/config/companion-surfaces')
@require_page('page:config', 'Config')
def companion_surfaces_config_page():
    return render_template('companion_surfaces_config.html')


@app.route('/console')
@require_page('page:console', 'Console')
def console_page():
    return render_template('console.html')


@app.route('/calendar/new')
@require_page('page:calendar', 'Schedule')
def calendar_new_page():
    return render_template('calendar_new.html')


@app.route('/calendar/edit/<int:ident>')
@require_page('page:calendar', 'Schedule')
def calendar_edit_page(ident: int):
    # Render the same create page; client JS will fetch event data and switch to edit mode
    return render_template('calendar_new.html')


def _read_json_file(p: Path):
    try:
        data, _ = read_json(p, default_factory=list)
        return data
    except Exception:
        return []


def _write_json_file(p: Path, data):
    return bool(write_json(p, data))


def _default_companion_surface_config() -> dict:
    return {
        'surfaces': [
            {
                'id': 'test-surface',
                'label': 'Test',
                'layout': '3x5',
            },
            {
                'id': 'test-2',
                'label': 'Another Test',
                'layout': '2x5',
            },
        ],
        'surface_controls': [
            {
                'surface_id': 'test-surface',
                'label': 'Left display',
                'size': '1',
            },
            {
                'surface_id': 'test-2',
                'label': 'Right display',
                'size': '1',
            },
        ],
    }


def _css_scale_value(value, default: str = '1') -> str:
    raw = str(value if value is not None else '').strip()
    try:
        number = float(raw)
    except Exception:
        try:
            number = float(default)
        except Exception:
            number = 1.0
    if not math.isfinite(number) or number <= 0:
        number = 1.0
    if number.is_integer():
        return str(int(number))
    return f'{number:g}'


def _companion_surface_layout_value(value) -> str:
    layout = str(value or '').strip().lower().replace(' ', '')
    return layout if layout in _COMPANION_SURFACE_LAYOUTS else _COMPANION_SURFACE_DEFAULT_LAYOUT


def _companion_surface_dimensions(layout: str, size: str = '1') -> tuple[str, str]:
    rows, cols = _COMPANION_SURFACE_LAYOUTS.get(
        _companion_surface_layout_value(layout),
        _COMPANION_SURFACE_LAYOUTS[_COMPANION_SURFACE_DEFAULT_LAYOUT],
    )
    try:
        scale = float(size)
    except Exception:
        scale = 1.0
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0
    width = int(round(((cols * _COMPANION_SURFACE_CELL_PX) + _COMPANION_SURFACE_GUTTER_PX) * scale))
    height = int(round(((rows * _COMPANION_SURFACE_CELL_PX) + _COMPANION_SURFACE_GUTTER_PX) * scale))
    return f'{width}px', f'{height}px'


def _normalize_companion_surface(raw) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get('id') or raw.get('surface_id') or '').strip()
    if not sid:
        return None
    label = str(raw.get('label') or raw.get('name') or sid).strip()
    return {
        'id': sid,
        'label': label or sid,
        'layout': _companion_surface_layout_value(raw.get('layout') or raw.get('dimensions') or raw.get('surface_layout')),
    }


def _normalize_companion_surface_display(raw, surfaces_by_id: dict[str, dict[str, str]], include_render_size: bool = True) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get('surface_id') or raw.get('id') or '').strip()
    if not sid:
        return None
    surface = surfaces_by_id.get(sid) or {'id': sid, 'label': sid}
    label = str(raw.get('label') or surface.get('label') or sid).strip()
    size = _css_scale_value(raw.get('size'), '1')
    out = {
        'id': sid,
        'surface_id': sid,
        'label': label or sid,
        'size': size,
    }
    if include_render_size:
        out['width'], out['height'] = _companion_surface_dimensions(surface.get('layout'), size)
    return out


def _normalize_companion_surface_display_list(raw_items, surfaces_by_id: dict[str, dict[str, str]], *, include_render_size: bool = True) -> list[dict[str, str]]:
    displays: list[dict[str, str]] = []
    if not isinstance(raw_items, list):
        return displays
    for item in raw_items:
        display = _normalize_companion_surface_display(item, surfaces_by_id, include_render_size=include_render_size)
        if display:
            displays.append(display)
    return displays


def _load_companion_surface_config() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    try:
        data, _ = read_json(_COMPANION_SURFACES_PATH, default_factory=_default_companion_surface_config)
    except Exception:
        data = _default_companion_surface_config()

    # Backward compatibility: the original file was a plain list of surfaces.
    if isinstance(data, list):
        surfaces_raw = data
        displays_raw = data
    elif isinstance(data, dict):
        surfaces_raw = data.get('surfaces')
        displays_raw = data.get('surface_controls') or data.get('displays')
        if not isinstance(surfaces_raw, list):
            surfaces_raw = []
        if not isinstance(displays_raw, list):
            displays_raw = surfaces_raw
    else:
        data = _default_companion_surface_config()
        surfaces_raw = data['surfaces']
        displays_raw = data['surface_controls']

    surfaces: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in surfaces_raw:
        surface = _normalize_companion_surface(item)
        if not surface:
            continue
        sid = surface['id']
        if sid in seen:
            continue
        seen.add(sid)
        surfaces.append(surface)

    surfaces_by_id = {s['id']: s for s in surfaces}
    displays = _normalize_companion_surface_display_list(displays_raw, surfaces_by_id)
    return surfaces, displays


def _companion_surface_config_payload() -> dict:
    surfaces, displays = _load_companion_surface_config()
    return {
        'surfaces': surfaces,
        'surface_controls': displays,
    }


def _normalize_companion_surface_config_payload(raw) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, 'Payload must be an object.'

    surfaces_raw = raw.get('surfaces')
    displays_raw = raw.get('surface_controls') if 'surface_controls' in raw else raw.get('displays')
    if not isinstance(surfaces_raw, list):
        return None, 'surfaces must be a list.'
    if displays_raw is None:
        displays_raw = []
    if not isinstance(displays_raw, list):
        return None, 'surface_controls must be a list.'

    surfaces: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in surfaces_raw:
        if not isinstance(item, dict):
            return None, 'Each surface must be an object.'
        if not str(item.get('id') or item.get('surface_id') or '').strip():
            return None, 'Every surface needs an ID.'
        if not str(item.get('label') or item.get('name') or '').strip():
            return None, 'Every surface needs a label.'
        raw_layout = str(item.get('layout') or item.get('dimensions') or item.get('surface_layout') or '').strip().lower().replace(' ', '')
        if raw_layout and raw_layout not in _COMPANION_SURFACE_LAYOUTS:
            return None, 'Every surface needs a valid layout.'
        surface = _normalize_companion_surface(item)
        if not surface:
            continue
        sid = surface['id']
        if sid in seen:
            return None, f'Duplicate surface ID: {sid}'
        seen.add(sid)
        surfaces.append(surface)

    surfaces_by_id = {s['id']: s for s in surfaces}
    def _normalize_payload_displays(items, list_label: str) -> tuple[list[dict[str, str]] | None, str | None]:
        displays_out: list[dict[str, str]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                return None, f'Each {list_label} display must be an object.'
            if not str(item.get('surface_id') or item.get('id') or '').strip():
                return None, f'{list_label} display {index} needs a surface.'
            if not str(item.get('label') or '').strip():
                return None, f'{list_label} display {index} needs a label.'
            if 'size' in item:
                try:
                    size_value = float(str(item.get('size') or '').strip())
                except Exception:
                    return None, f'{list_label} display {index} has an invalid size value.'
                if not math.isfinite(size_value) or size_value <= 0:
                    return None, f'{list_label} display {index} size must be greater than zero.'
            display = _normalize_companion_surface_display(item, surfaces_by_id, include_render_size=False)
            if not display:
                continue
            sid = display['surface_id']
            if sid not in surfaces_by_id:
                return None, f'{list_label} display references unknown surface ID: {sid}'
            displays_out.append(display)
        return displays_out, None

    displays, err = _normalize_payload_displays(displays_raw, 'Surface Controls Page')
    if err or displays is None:
        return None, err or 'Invalid Surface Controls Page displays.'

    return {
        'surfaces': surfaces,
        'surface_controls': displays,
    }, None


def _save_companion_surface_config(payload: dict) -> bool:
    try:
        _COMPANION_SURFACES_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return bool(write_json(_COMPANION_SURFACES_PATH, payload))


def _load_companion_surfaces() -> list[dict[str, str]]:
    surfaces, _ = _load_companion_surface_config()
    return surfaces


def _load_surface_control_displays() -> list[dict[str, str]]:
    _, displays = _load_companion_surface_config()
    return displays


def _companion_surface_by_id(surface_id: str) -> dict[str, str] | None:
    sid = str(surface_id or '').strip()
    if not sid:
        return None
    for surface in _load_companion_surfaces():
        if str(surface.get('id') or '') == sid:
            return surface
    return None


def _companion_base_url() -> str:
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    host = str(cfg.get('companion_surface_ip') or cfg.get('companion_ip') or '127.0.0.1').strip() or '127.0.0.1'
    try:
        port = int(cfg.get('companion_surface_port') or cfg.get('companion_port') or 8000)
    except Exception:
        port = 8000
    return f"http://{host}:{port}"


def _companion_surface_url(surface_id: str) -> str:
    sid = str(surface_id or '').strip().strip('/')
    return f"{_companion_base_url()}/emulator/{sid}"


_videohub_rooms_lock = threading.Lock()
_templates_cache_lock = threading.RLock()
_VIDEOHUB_ROOM_ALLOWED_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
_VIDEOHUB_ROOM_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_button_templates_cache = {'snapshot': None, 'tree': None, 'buttons': None}
_trigger_templates_cache = {'snapshot': None, 'triggers': None}
_videohub_rooms_cache = {'snapshot': None, 'config': None}


def _path_snapshot(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
    except Exception:
        return None
    try:
        return (int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return None


def _videohub_rooms_config_path() -> Path:
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        raw = str(cfg.get('videohub_rooms_file') or '').strip()
    except Exception:
        raw = ''
    if raw:
        try:
            return Path(raw)
        except Exception:
            pass
    return Path(__file__).resolve().parent / 'videohub_rooms.json'


def _videohub_rooms_images_dir() -> Path:
    return Path(__file__).resolve().parent / 'videohub_room_images'


def _default_videohub_rooms_config() -> dict:
    return {
        'version': 1,
        'rooms': [],
        'output_layouts': {},
        'filtered_inputs': [],
    }


def _normalize_videohub_rooms_config(raw) -> dict:
    data = raw if isinstance(raw, dict) else {}
    out = _default_videohub_rooms_config()

    rooms_raw = data.get('rooms')
    rooms: list[dict] = []
    seen_room_ids: set[str] = set()
    if isinstance(rooms_raw, list):
        for item in rooms_raw:
            if not isinstance(item, dict):
                continue
            rid = str(item.get('id') or '').strip()
            if not rid:
                rid = uuid.uuid4().hex
            if rid in seen_room_ids:
                continue
            seen_room_ids.add(rid)

            name = str(item.get('name') or '').strip()
            if not name:
                name = f'Room {len(rooms) + 1}'

            bg = str(item.get('background_image') or '').strip()
            if bg:
                bg = Path(bg).name
            rooms.append({
                'id': rid,
                'name': name[:120],
                'background_image': bg,
            })
    out['rooms'] = rooms

    room_id_set = set([r['id'] for r in rooms])
    layouts_raw = data.get('output_layouts')
    layouts: dict[str, dict] = {}
    if isinstance(layouts_raw, dict):
        for k, v in layouts_raw.items():
            try:
                out_n = int(k)
            except Exception:
                continue
            if out_n <= 0 or not isinstance(v, dict):
                continue

            rid = str(v.get('room_id') or '').strip()
            if rid and rid not in room_id_set:
                rid = ''

            try:
                x = float(v.get('x'))
            except Exception:
                x = 50.0
            try:
                y = float(v.get('y'))
            except Exception:
                y = 50.0
            x = max(0.0, min(100.0, x))
            y = max(0.0, min(100.0, y))
            layouts[str(out_n)] = {'room_id': rid, 'x': x, 'y': y}
    out['output_layouts'] = layouts

    filt_raw = data.get('filtered_inputs')
    filtered_inputs: list[int] = []
    seen_inputs: set[int] = set()
    if isinstance(filt_raw, list):
        for item in filt_raw:
            try:
                n = int(item)
            except Exception:
                continue
            if n <= 0 or n in seen_inputs:
                continue
            seen_inputs.add(n)
            filtered_inputs.append(n)
    out['filtered_inputs'] = filtered_inputs
    return out


def _videohub_room_name_map(cfg: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for room in (cfg.get('rooms') or []):
        if not isinstance(room, dict):
            continue
        rid = str(room.get('id') or '').strip()
        if rid:
            names[rid] = str(room.get('name') or rid)
    names[''] = 'Unassigned'
    return names


def _videohub_rooms_diff(old_cfg: dict, new_cfg: dict) -> dict:
    old_rooms = {str(r.get('id') or ''): dict(r) for r in (old_cfg.get('rooms') or []) if isinstance(r, dict) and str(r.get('id') or '').strip()}
    new_rooms = {str(r.get('id') or ''): dict(r) for r in (new_cfg.get('rooms') or []) if isinstance(r, dict) and str(r.get('id') or '').strip()}
    room_names = {**_videohub_room_name_map(old_cfg), **_videohub_room_name_map(new_cfg)}
    created = [{'id': rid, 'name': room_names.get(rid, rid)} for rid in sorted(set(new_rooms) - set(old_rooms))]
    deleted = [{'id': rid, 'name': room_names.get(rid, rid)} for rid in sorted(set(old_rooms) - set(new_rooms))]
    updated = []
    for rid in sorted(set(old_rooms) & set(new_rooms)):
        changes = {}
        for key in ('name', 'background_image'):
            if old_rooms[rid].get(key) != new_rooms[rid].get(key):
                changes[key] = {'old': old_rooms[rid].get(key), 'new': new_rooms[rid].get(key)}
        if changes:
            updated.append({'id': rid, 'name': room_names.get(rid, rid), 'changes': changes})

    old_layouts = old_cfg.get('output_layouts') if isinstance(old_cfg.get('output_layouts'), dict) else {}
    new_layouts = new_cfg.get('output_layouts') if isinstance(new_cfg.get('output_layouts'), dict) else {}
    output_room_changes = []
    for out_n in sorted(set([str(k) for k in old_layouts.keys()] + [str(k) for k in new_layouts.keys()]), key=lambda x: int(x) if str(x).isdigit() else 999999):
        old_layout = old_layouts.get(out_n) if isinstance(old_layouts.get(out_n), dict) else {}
        new_layout = new_layouts.get(out_n) if isinstance(new_layouts.get(out_n), dict) else {}
        old_room = str(old_layout.get('room_id') or '')
        new_room = str(new_layout.get('room_id') or '')
        if old_room != new_room:
            output_room_changes.append({
                'output': int(out_n) if str(out_n).isdigit() else out_n,
                'old_room_id': old_room,
                'old_room_name': room_names.get(old_room, old_room or 'Unassigned'),
                'new_room_id': new_room,
                'new_room_name': room_names.get(new_room, new_room or 'Unassigned'),
            })

    old_inputs = [int(x) for x in (old_cfg.get('filtered_inputs') or []) if isinstance(x, int) or str(x).isdigit()]
    new_inputs = [int(x) for x in (new_cfg.get('filtered_inputs') or []) if isinstance(x, int) or str(x).isdigit()]
    diff = {
        'rooms_created': created,
        'rooms_updated': updated,
        'rooms_deleted': deleted,
        'output_room_changes': output_room_changes,
    }
    added_inputs = sorted(set(new_inputs) - set(old_inputs))
    removed_inputs = sorted(set(old_inputs) - set(new_inputs))
    if added_inputs or removed_inputs:
        diff['filtered_inputs'] = {'added': added_inputs, 'removed': removed_inputs}
    return diff


def _load_videohub_rooms_config() -> dict:
    p = _videohub_rooms_config_path()
    with _videohub_rooms_lock:
        sig = _path_snapshot(p)
        with _templates_cache_lock:
            if sig is not None and _videohub_rooms_cache.get('snapshot') == sig:
                cached = _videohub_rooms_cache.get('config')
                if isinstance(cached, dict):
                    return copy.deepcopy(cached)

        def _transform(raw):
            normalized = _normalize_videohub_rooms_config(raw)
            return normalized, normalized != raw

        try:
            cfg, changed = read_json(
                p,
                default_factory=_default_videohub_rooms_config,
                create_if_missing=True,
                transform=_transform,
            )
        except Exception:
            return _default_videohub_rooms_config()

        if changed:
            try:
                _save_videohub_rooms_config(cfg)
            except Exception:
                pass
        else:
            with _templates_cache_lock:
                _videohub_rooms_cache['snapshot'] = _path_snapshot(p)
                _videohub_rooms_cache['config'] = copy.deepcopy(cfg)
        return cfg


def _save_videohub_rooms_config(data: dict) -> dict:
    p = _videohub_rooms_config_path()
    normalized = _normalize_videohub_rooms_config(data)
    with _videohub_rooms_lock:
        if not write_json(p, normalized):
            return normalized
        with _templates_cache_lock:
            _videohub_rooms_cache['snapshot'] = _path_snapshot(p)
            _videohub_rooms_cache['config'] = copy.deepcopy(normalized)
    return normalized


def _can_edit_videohub_presets_current_user() -> bool:
    try:
        if not _auth_enabled():
            return True
    except Exception:
        return False
    try:
        if not getattr(current_user, 'is_authenticated', False):
            return False
    except Exception:
        return False
    try:
        return bool(_effective_videohub_can_edit_presets_for_user(int(current_user.get_id())))
    except Exception:
        return False


def _api_requires_videohub_edit() -> bool:
    """API auth guard for room metadata mutating routes."""
    try:
        if not _auth_enabled():
            return True
        if _api_request_is_automation_principal():
            return True
    except Exception:
        return False
    try:
        if not getattr(current_user, 'is_authenticated', False):
            return False
    except Exception:
        return False
    return bool(_can_edit_videohub_presets_current_user())


def _delete_room_background_image(filename: str) -> None:
    fn = Path(str(filename or '')).name
    if not fn:
        return
    try:
        p = _videohub_rooms_images_dir() / fn
        if p.exists() and p.is_file():
            p.unlink()
    except Exception:
        pass


def _coerce_trigger_times_list(val):
    """Return a list of trigger specs from a trigger template 'times' field."""
    if val is None:
        return []
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
        except Exception:
            parsed = None
        val = parsed if parsed is not None else []
    if isinstance(val, dict):
        return [val]
    if isinstance(val, list):
        return val
    return []


def _extract_pattern_from_button_url(button_url: str) -> str | None:
    try:
        s = str(button_url or '').strip()
    except Exception:
        return None
    m = re.match(r'^location\/(\d+\/\d+\/\d+)\/press$', s)
    if m and m.group(1):
        return m.group(1)
    if re.match(r'^\d+\/\d+\/\d+$', s):
        return s
    return None


def _button_template_effective_url(tpl: dict) -> str:
    if not isinstance(tpl, dict):
        return ''
    url = (tpl.get('buttonURL') or '').strip()
    if url:
        return url
    pattern = (tpl.get('pattern') or '').strip()
    if pattern:
        return f'location/{pattern}/press'
    return ''


def _find_duplicate_button_template(
    arr: list,
    *,
    url: str,
    pattern: str,
    exclude_idx: int | None = None,
    exclude_id: str | None = None,
) -> dict | None:
    url = (url or '').strip()
    pattern = (pattern or '').strip()
    if not url and not pattern:
        return None
    for i, tpl in enumerate(arr or []):
        if exclude_idx is not None and i == exclude_idx:
            continue
        if exclude_id is not None and str(tpl.get('id') or '').strip() == str(exclude_id).strip():
            continue
        if not isinstance(tpl, dict):
            continue
        existing_pattern = (tpl.get('pattern') or '').strip()
        existing_url = _button_template_effective_url(tpl)
        if url and existing_url == url:
            return {
                'index': i,
                'id': str(tpl.get('id') or '').strip() or None,
                'label': (tpl.get('label') or '').strip(),
                'pattern': existing_pattern,
                'buttonURL': existing_url,
            }
        if pattern and existing_pattern == pattern:
            return {
                'index': i,
                'id': str(tpl.get('id') or '').strip() or None,
                'label': (tpl.get('label') or '').strip(),
                'pattern': existing_pattern,
                'buttonURL': existing_url,
            }
    return None


def _uuid4_str() -> str:
    try:
        return str(uuid.uuid4())
    except Exception:
        # Extremely defensive fallback
        return str(int(time.time() * 1000))


def _normalize_button_templates_tree(raw) -> tuple[dict, bool]:
    """Normalize button templates into the current tree structure."""
    changed = False

    if isinstance(raw, dict):
        folders = raw.get('folders') if isinstance(raw.get('folders'), list) else []
        templates = raw.get('templates') if isinstance(raw.get('templates'), list) else []
    elif isinstance(raw, list):
        folders = []
        templates = raw
        changed = True
    else:
        folders = []
        templates = []
        changed = True

    norm_folders: list[dict] = []
    folder_ids: set[str] = set()
    for i, f in enumerate(folders):
        if not isinstance(f, dict):
            changed = True
            continue
        fid = str(f.get('id') or '').strip() or _uuid4_str()
        if fid in folder_ids:
            fid = _uuid4_str()
            changed = True
        folder_ids.add(fid)
        name = str(f.get('name') or '').strip() or 'Folder'
        parent_id = str(f.get('parentId') or '').strip() or None
        order = f.get('order', i)
        try:
            order = int(order)
        except Exception:
            order = i
            changed = True
        norm_folders.append({'id': fid, 'name': name, 'parentId': parent_id, 'order': order})

    norm_templates: list[dict] = []
    for i, t in enumerate(templates):
        if not isinstance(t, dict):
            changed = True
            continue
        tid = str(t.get('id') or '').strip() or _uuid4_str()
        label = str(t.get('label') or '').strip()
        pattern = str(t.get('pattern') or '').strip()
        url = str(t.get('buttonURL') or '').strip()
        if not url and pattern:
            url = f'location/{pattern}/press'
            changed = True
        folder_id = str(t.get('folderId') or '').strip() or None
        order = t.get('order', i)
        try:
            order = int(order)
        except Exception:
            order = i
            changed = True
        norm_templates.append({'id': tid, 'label': label, 'pattern': pattern, 'buttonURL': url, 'folderId': folder_id, 'order': order})

    folder_ids = {f['id'] for f in norm_folders}
    for t in norm_templates:
        if t.get('folderId') and t['folderId'] not in folder_ids:
            t['folderId'] = None
            changed = True
    for f in norm_folders:
        pid = f.get('parentId')
        if pid and pid not in folder_ids:
            f['parentId'] = None
            changed = True

    return {'version': 2, 'folders': norm_folders, 'templates': norm_templates}, changed


def _load_button_templates_bundle() -> tuple[dict, list[dict], bool]:
    sig = _path_snapshot(BUTTON_TEMPLATES)
    with _templates_cache_lock:
        if sig is not None and _button_templates_cache.get('snapshot') == sig:
            cached_tree = _button_templates_cache.get('tree')
            cached_buttons = _button_templates_cache.get('buttons')
            if isinstance(cached_tree, dict) and isinstance(cached_buttons, list):
                return copy.deepcopy(cached_tree), copy.deepcopy(cached_buttons), False

    tree, changed = read_json(
        BUTTON_TEMPLATES,
        default_factory=lambda: {'version': 2, 'folders': [], 'templates': []},
        create_if_missing=True,
        transform=_normalize_button_templates_tree,
    )
    saved = True
    if changed:
        saved = _write_json_file(BUTTON_TEMPLATES, tree)

    buttons = _flatten_button_templates_tree(tree)
    if saved:
        with _templates_cache_lock:
            _button_templates_cache['snapshot'] = _path_snapshot(BUTTON_TEMPLATES)
            _button_templates_cache['tree'] = copy.deepcopy(tree)
            _button_templates_cache['buttons'] = copy.deepcopy(buttons)
    return tree, buttons, changed


def _load_button_templates_tree() -> tuple[dict, bool]:
    tree, _, changed = _load_button_templates_bundle()
    return tree, changed


def _button_templates_tree_diff(old_tree: dict, new_tree: dict) -> dict:
    def _by_id(items) -> dict[str, dict]:
        out = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            iid = str(item.get('id') or '').strip()
            if iid:
                out[iid] = dict(item)
        return out

    old_folders = _by_id(old_tree.get('folders') if isinstance(old_tree, dict) else [])
    new_folders = _by_id(new_tree.get('folders') if isinstance(new_tree, dict) else [])
    old_templates = _by_id(old_tree.get('templates') if isinstance(old_tree, dict) else [])
    new_templates = _by_id(new_tree.get('templates') if isinstance(new_tree, dict) else [])

    def _item_name(item: dict, fallback: str) -> str:
        return str(item.get('name') or item.get('label') or fallback)

    diff = {
        'folders_created': [],
        'folders_updated': [],
        'folders_deleted': [],
        'templates_created': [],
        'templates_updated': [],
        'templates_deleted': [],
    }

    for fid in sorted(set(new_folders) - set(old_folders)):
        diff['folders_created'].append({'id': fid, 'name': _item_name(new_folders[fid], fid), 'parent_id': new_folders[fid].get('parentId')})
    for fid in sorted(set(old_folders) - set(new_folders)):
        diff['folders_deleted'].append({'id': fid, 'name': _item_name(old_folders[fid], fid), 'parent_id': old_folders[fid].get('parentId')})
    for fid in sorted(set(old_folders) & set(new_folders)):
        changes = {}
        for key in ('name', 'parentId', 'order'):
            if old_folders[fid].get(key) != new_folders[fid].get(key):
                changes[key] = {'old': old_folders[fid].get(key), 'new': new_folders[fid].get(key)}
        if changes:
            diff['folders_updated'].append({'id': fid, 'name': _item_name(new_folders[fid], fid), 'changes': changes})

    for tid in sorted(set(new_templates) - set(old_templates)):
        diff['templates_created'].append({'id': tid, 'label': _item_name(new_templates[tid], tid), 'folder_id': new_templates[tid].get('folderId')})
    for tid in sorted(set(old_templates) - set(new_templates)):
        diff['templates_deleted'].append({'id': tid, 'label': _item_name(old_templates[tid], tid), 'folder_id': old_templates[tid].get('folderId')})
    for tid in sorted(set(old_templates) & set(new_templates)):
        changes = {}
        for key in ('label', 'pattern', 'buttonURL', 'folderId', 'order'):
            if old_templates[tid].get(key) != new_templates[tid].get(key):
                changes[key] = {'old': old_templates[tid].get(key), 'new': new_templates[tid].get(key)}
        if changes:
            diff['templates_updated'].append({'id': tid, 'label': _item_name(new_templates[tid], tid), 'changes': changes})
    return {key: value for key, value in diff.items() if value}


def _button_templates_tree_diff_summary(diff: dict) -> str:
    parts = []
    labels = (
        ('folders_created', 'folder created'),
        ('folders_updated', 'folder changed'),
        ('folders_deleted', 'folder deleted'),
        ('templates_created', 'template created'),
        ('templates_updated', 'template changed'),
        ('templates_deleted', 'template deleted'),
    )
    for key, label in labels:
        count = len(diff.get(key) or [])
        if count:
            parts.append(f"{count} {label}{'' if count == 1 else 's'}")
    return ', '.join(parts) if parts else 'no visible changes'


def _save_button_templates_tree(tree: dict) -> bool:
    if not isinstance(tree, dict):
        return False
    out = {
        'version': 2,
        'folders': tree.get('folders') if isinstance(tree.get('folders'), list) else [],
        'templates': tree.get('templates') if isinstance(tree.get('templates'), list) else [],
    }
    ok = _write_json_file(BUTTON_TEMPLATES, out)
    if ok:
        buttons = _flatten_button_templates_tree(out)
        with _templates_cache_lock:
            _button_templates_cache['snapshot'] = _path_snapshot(BUTTON_TEMPLATES)
            _button_templates_cache['tree'] = copy.deepcopy(out)
            _button_templates_cache['buttons'] = copy.deepcopy(buttons)
    return ok


def _flatten_button_templates_tree(tree: dict) -> list[dict]:
    """Return templates in a stable UI order (root + depth-first by folder order)."""
    templates = tree.get('templates') if isinstance(tree, dict) else None
    folders = tree.get('folders') if isinstance(tree, dict) else None
    templates = templates if isinstance(templates, list) else []
    folders = folders if isinstance(folders, list) else []

    # Build folder children map
    folder_children: dict[str | None, list[dict]] = {}
    for f in folders:
        if not isinstance(f, dict):
            continue
        pid = f.get('parentId') if f.get('parentId') else None
        folder_children.setdefault(pid, []).append(f)
    for pid in list(folder_children.keys()):
        folder_children[pid].sort(key=lambda x: int(x.get('order', 0)))

    # Build template children map
    tpl_children: dict[str | None, list[dict]] = {}
    for t in templates:
        if not isinstance(t, dict):
            continue
        fid = t.get('folderId') if t.get('folderId') else None
        tpl_children.setdefault(fid, []).append(t)
    for fid in list(tpl_children.keys()):
        tpl_children[fid].sort(key=lambda x: int(x.get('order', 0)))

    out: list[dict] = []

    def walk_folder(parent_id: str | None) -> None:
        for t in tpl_children.get(parent_id, []):
            out.append(t)
        for f in folder_children.get(parent_id, []):
            walk_folder(str(f.get('id')))

    walk_folder(None)
    return out


def _normalize_trigger_templates_list(raw) -> tuple[list[dict], bool]:
    if not isinstance(raw, list):
        raw = []
    normalized: list[dict] = []
    changed = False
    for t in raw:
        if not isinstance(t, dict):
            normalized.append(t)
            continue
        if not str(t.get('id') or '').strip():
            t = dict(t)
            t['id'] = _uuid4_str()
            changed = True
        if 'times' not in t and 'spec' in t:
            val = t.get('spec')
            if isinstance(val, str):
                try:
                    val_parsed = json.loads(val)
                except Exception:
                    val_parsed = val
            else:
                val_parsed = val
            t['times'] = val_parsed
            t.pop('spec', None)
            changed = True

        times = _coerce_trigger_times_list(t.get('times'))
        norm_times = []
        for spec in times:
            if not isinstance(spec, dict):
                norm_times.append(spec)
                continue
            if not str(spec.get('uid') or '').strip():
                spec = dict(spec)
                spec['uid'] = _uuid4_str()
                changed = True
            norm_times.append(spec)
        if t.get('times') != norm_times:
            t = dict(t)
            t['times'] = norm_times
            changed = True
        normalized.append(t)
    return normalized, changed


def _load_trigger_templates_list() -> tuple[list[dict], bool]:
    sig = _path_snapshot(TRIGGER_TEMPLATES)
    with _templates_cache_lock:
        if sig is not None and _trigger_templates_cache.get('snapshot') == sig:
            cached = _trigger_templates_cache.get('triggers')
            if isinstance(cached, list):
                return copy.deepcopy(cached), False

    trigs, changed = read_json(
        TRIGGER_TEMPLATES,
        default_factory=list,
        create_if_missing=True,
        transform=_normalize_trigger_templates_list,
    )
    saved = True
    if changed:
        saved = _save_trigger_templates_list(trigs)
    if saved:
        with _templates_cache_lock:
            _trigger_templates_cache['snapshot'] = _path_snapshot(TRIGGER_TEMPLATES)
            _trigger_templates_cache['triggers'] = copy.deepcopy(trigs)
    return trigs, changed


def _save_trigger_templates_list(trigs: list[dict]) -> bool:
    ok = _write_json_file(TRIGGER_TEMPLATES, trigs)
    if ok:
        with _templates_cache_lock:
            _trigger_templates_cache['snapshot'] = _path_snapshot(TRIGGER_TEMPLATES)
            _trigger_templates_cache['triggers'] = copy.deepcopy(trigs)
    return ok


def _replace_button_url_everywhere(old_url: str, new_url: str) -> dict:
    """Best-effort replacement across stored data files."""
    out = {
        'trigger_templates_updated': 0,
        'events_updated': 0,
        'timer_presets_updated': 0,
    }

    if not old_url or not new_url or old_url == new_url:
        return out

    old_pattern = _extract_pattern_from_button_url(old_url)
    # 1) trigger_templates.json
    try:
        trigs, _ = _load_trigger_templates_list()
        changed = False
        replaced = 0
        normalized = []
        for t in trigs:
            if not isinstance(t, dict):
                normalized.append(t)
                continue
            times = _coerce_trigger_times_list(t.get('times'))
            new_times = []
            for spec in times:
                if isinstance(spec, dict):
                    current = str(spec.get('buttonURL', '')).strip()
                    if current == old_url or (old_pattern and current == old_pattern):
                        spec = dict(spec)
                        spec['buttonURL'] = new_url
                        replaced += 1
                        changed = True
                new_times.append(spec)
            t2 = dict(t)
            t2['times'] = new_times
            t2.pop('spec', None)
            normalized.append(t2)
        if changed:
            _save_trigger_templates_list(normalized)
        out['trigger_templates_updated'] = replaced
    except Exception:
        pass

    # 2) events.json (or configured events file)
    try:
        from package.apps.calendar import storage
        try:
            cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
        except Exception:
            cfg = {}
        events_file = cfg.get('EVENTS_FILE', storage.DEFAULT_EVENTS_FILE)
        events = storage.load_events(events_file)
        replaced = 0
        for e in events or []:
            for trig in getattr(e, 'times', []) or []:
                try:
                    cur = getattr(trig, 'buttonURL', '')
                    if cur == old_url or (old_pattern and cur == old_pattern):
                        trig.buttonURL = new_url
                        replaced += 1
                except Exception:
                    pass
        if replaced:
            storage.save_events(events, events_file)
        out['events_updated'] = replaced
    except Exception:
        pass

    # 3) timer_presets.json (button_presses)
    try:
        if hasattr(utils, 'load_timer_presets') and hasattr(utils, 'save_timer_presets'):
            presets = list(utils.load_timer_presets())
            replaced = 0
            any_changed = False
            for p in presets:
                if not isinstance(p, dict):
                    continue
                presses = p.get('button_presses')
                if presses is None and 'buttonPresses' in p:
                    presses = p.get('buttonPresses')
                if presses is None and 'actions' in p:
                    presses = p.get('actions')
                if not isinstance(presses, list):
                    continue
                preset_changed = False
                new_presses = []
                for entry in presses:
                    if isinstance(entry, dict):
                        current = str(entry.get('buttonURL', '')).strip()
                        if current == old_url or (old_pattern and current == old_pattern):
                            e2 = dict(entry)
                            e2['buttonURL'] = new_url
                            new_presses.append(e2)
                            replaced += 1
                            preset_changed = True
                        else:
                            new_presses.append(entry)
                    else:
                        new_presses.append(entry)
                if preset_changed:
                    p['button_presses'] = new_presses
                    p.pop('buttonPresses', None)
                    p.pop('actions', None)
                    any_changed = True
            if any_changed:
                utils.save_timer_presets(presets)
            out['timer_presets_updated'] = replaced
    except Exception:
        pass

    return out


@app.route('/api/templates')
def api_get_templates():
    tree, btns, _ = _load_button_templates_bundle()
    trigs, _ = _load_trigger_templates_list()
    return jsonify({'buttons': btns, 'buttons_tree': tree, 'triggers': trigs})


@app.route('/api/templates/button', methods=['POST'])
def api_add_button_template():
    body = request.get_json() or {}
    label = (body.get('label') or '').strip()
    pattern = (body.get('pattern') or '').strip()
    folder_id = (body.get('folderId') or body.get('folder_id') or '').strip() or None
    if not label or not pattern:
        return jsonify({'ok': False, 'error': 'label and pattern required'}), 400
    # validate pattern: must be three integers separated by '/'
    if not re.match(r'^\d+\/\d+\/\d+$', pattern):
        return jsonify({'ok': False, 'error': 'pattern must be like "1/0/1" (three integers separated by "/")'}), 400

    tree, _ = _load_button_templates_tree()
    arr = tree.get('templates') if isinstance(tree.get('templates'), list) else []
    button_url = f"location/{pattern}/press"
    dup = _find_duplicate_button_template(arr, url=button_url, pattern=pattern)
    if dup:
        return jsonify({
            'ok': False,
            'error': 'duplicate button template',
            'existing': dup,
        }), 409

    # order within the target folder/root (append)
    try:
        max_order = max([int(t.get('order', -1)) for t in arr if isinstance(t, dict) and (t.get('folderId') or None) == folder_id] or [-1])
    except Exception:
        max_order = -1

    tpl = {'id': _uuid4_str(), 'label': label, 'pattern': pattern, 'buttonURL': button_url, 'folderId': folder_id, 'order': int(max_order) + 1}
    arr.append(tpl)
    tree['templates'] = arr
    ok = _save_button_templates_tree(tree)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
    try:
        log_event('template.button.create', f"Created button template '{label}'", source='web', status='success', target_type='button_template', target_id=tpl.get('id'), details={'label': label, 'pattern': pattern, 'folder_id': folder_id})
    except Exception:
        pass
    return jsonify({'ok': True, 'template': tpl})


@app.route('/api/templates/button/<tpl_id>', methods=['DELETE'])
def api_delete_button_template(tpl_id: str):
    tpl_id = str(tpl_id or '').strip()
    tree, _ = _load_button_templates_tree()
    arr = tree.get('templates') if isinstance(tree.get('templates'), list) else []
    idx = None
    for i, t in enumerate(arr):
        if isinstance(t, dict) and str(t.get('id') or '').strip() == tpl_id:
            idx = i
            break
    if idx is None:
        return jsonify({'ok': False, 'error': 'template not found'}), 404
    removed = arr.pop(int(idx))
    tree['templates'] = arr
    ok = _save_button_templates_tree(tree)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
    try:
        log_event('template.button.delete', f"Deleted button template '{removed.get('label', tpl_id) if isinstance(removed, dict) else tpl_id}'", source='web', status='success', target_type='button_template', target_id=tpl_id, details={'removed': removed})
    except Exception:
        pass
    return jsonify({'ok': True, 'removed': removed})


@app.route('/api/templates/button/<tpl_id>', methods=['PUT'])
def api_update_button_template(tpl_id: str):
    body = request.get_json() or {}
    label = (body.get('label') or '').strip()
    pattern = (body.get('pattern') or '').strip()
    button_url_raw = (body.get('buttonURL') or '').strip()

    if not label:
        return jsonify({'ok': False, 'error': 'label required'}), 400

    # Allow updating via pattern or buttonURL.
    if not pattern and button_url_raw:
        pattern = _extract_pattern_from_button_url(button_url_raw) or ''

    if not pattern:
        return jsonify({'ok': False, 'error': 'pattern required (e.g. 1/0/1)'}), 400
    if not re.match(r'^\d+\/\d+\/\d+$', pattern):
        return jsonify({'ok': False, 'error': 'pattern must be like "1/0/1" (three integers separated by "/")'}), 400

    new_url = f'location/{pattern}/press'

    tpl_id = str(tpl_id or '').strip()
    tree, _ = _load_button_templates_tree()
    arr = tree.get('templates') if isinstance(tree.get('templates'), list) else []
    idx = None
    for i, t in enumerate(arr):
        if isinstance(t, dict) and str(t.get('id') or '').strip() == tpl_id:
            idx = i
            break
    if idx is None:
        return jsonify({'ok': False, 'error': 'template not found'}), 404

    dup = _find_duplicate_button_template(arr, url=new_url, pattern=pattern, exclude_idx=int(idx), exclude_id=tpl_id)
    if dup:
        return jsonify({
            'ok': False,
            'error': 'duplicate button template',
            'existing': dup,
        }), 409

    old = arr[int(idx)] if isinstance(arr[int(idx)], dict) else {}
    try:
        old_url = str(old.get('buttonURL') or (f"location/{old.get('pattern')}/press" if old.get('pattern') else '')).strip()
    except Exception:
        old_url = ''

    arr[int(idx)] = {
        'id': tpl_id,
        'label': label,
        'pattern': pattern,
        'buttonURL': new_url,
        'folderId': old.get('folderId') if isinstance(old, dict) else None,
        'order': old.get('order') if isinstance(old, dict) else 0,
    }
    tree['templates'] = arr
    ok = _save_button_templates_tree(tree)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500

    # Propagate URL change across stored data
    replace_stats = _replace_button_url_everywhere(old_url, new_url)
    try:
        log_event('template.button.update', f"Updated button template '{label}'", source='web', status='success', target_type='button_template', target_id=tpl_id, details={'old': old, 'new': arr[int(idx)], 'replaced': replace_stats})
    except Exception:
        pass
    return jsonify({'ok': True, 'template': arr[int(idx)], 'replaced': {'old': old_url, 'new': new_url}, **replace_stats})


@app.route('/api/templates/buttons_tree', methods=['GET'])
def api_get_buttons_tree():
    tree, _ = _load_button_templates_tree()
    return jsonify({'ok': True, 'tree': tree})


@app.route('/api/templates/buttons_tree', methods=['PUT'])
def api_put_buttons_tree():
    body = request.get_json() or {}
    tree = body.get('tree') if isinstance(body, dict) else None
    if not isinstance(tree, dict):
        return jsonify({'ok': False, 'error': 'tree required'}), 400

    # Save as-is (UI is the authority). A subsequent GET /api/templates will normalize.
    old_tree, _ = _load_button_templates_tree()
    ok = _save_button_templates_tree(tree)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
    try:
        diff = _button_templates_tree_diff(old_tree, tree)
        summary = _button_templates_tree_diff_summary(diff)
        log_event(
            'template.button.save_tree',
            f"Updated button template tree: {summary}",
            source='web',
            status='success',
            target_type='button_template_tree',
            details={'changes': diff, 'template_count': len(tree.get('templates') or []), 'folder_count': len(tree.get('folders') or [])},
        )
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/templates/trigger', methods=['POST'])
def api_add_trigger_template():
    body = request.get_json() or {}
    label = body.get('label')
    times = body.get('times')
    if not label or times is None:
        return jsonify({'ok': False, 'error': 'label and times required'}), 400
    # normalize times to a list
    if isinstance(times, dict):
        times = [times]
    if not isinstance(times, list):
        return jsonify({'ok': False, 'error': 'times must be an object or an array of trigger specs'}), 400

    # normalize: if typeOfTrigger is AT, minutes must be 0
    normalized_times = []
    for t in times:
        if not isinstance(t, dict):
            continue
        typ_name = str(t.get('typeOfTrigger', 'AT')).upper()
        mins_val = t.get('minutes', 0)
        if typ_name == 'AT':
            mins = 0
        else:
            try:
                mins = int(mins_val or 0)
            except Exception:
                return jsonify({'ok': False, 'error': f"Invalid minutes value: {mins_val}"}), 400
            if mins < 0:
                return jsonify({'ok': False, 'error': f"Minutes must be >= 0: {mins}"}), 400
        t2 = dict(t)
        t2['typeOfTrigger'] = typ_name
        t2['minutes'] = mins
        t3, err = _normalize_trigger_action_spec(t2)
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        if t3:
            if not str(t3.get('uid') or '').strip():
                t3 = dict(t3)
                t3['uid'] = _uuid4_str()
            normalized_times.append(t3)
    arr, _ = _load_trigger_templates_list()
    arr.append({'id': _uuid4_str(), 'label': label, 'times': normalized_times})
    ok = _save_trigger_templates_list(arr)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
    try:
        log_event('template.trigger.create', f"Created trigger template '{label}'", source='web', status='success', target_type='trigger_template', target_id=arr[-1].get('id'), details={'label': label, 'trigger_count': len(normalized_times)})
    except Exception:
        pass
    return jsonify({'ok': True, 'template': arr[-1]})


@app.route('/api/templates/trigger/<int:idx>', methods=['DELETE'])
def api_delete_trigger_template(idx: int):
    arr, _ = _load_trigger_templates_list()
    if idx < 0 or idx >= len(arr):
        return jsonify({'ok': False, 'error': 'index out of range'}), 404
    removed = arr.pop(idx)
    ok = _save_trigger_templates_list(arr)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
    try:
        log_event('template.trigger.delete', f"Deleted trigger template '{removed.get('label', idx) if isinstance(removed, dict) else idx}'", source='web', status='success', target_type='trigger_template', target_id=(removed.get('id') if isinstance(removed, dict) else idx), details={'removed': removed})
    except Exception:
        pass
    return jsonify({'ok': True, 'removed': removed})


@app.route('/api/templates/trigger/<int:idx>', methods=['PUT'])
def api_update_trigger_template(idx: int):
    body = request.get_json() or {}
    label = body.get('label')
    times = body.get('times')
    if not label or times is None:
        return jsonify({'ok': False, 'error': 'label and times required'}), 400
    if not isinstance(times, list):
        return jsonify({'ok': False, 'error': 'times must be an array of trigger specs'}), 400

    # normalize: if typeOfTrigger is AT, minutes must be 0
    normalized_times = []
    for t in times:
        if not isinstance(t, dict):
            continue
        typ_name = str(t.get('typeOfTrigger', 'AT')).upper()
        mins_val = t.get('minutes', 0)
        if typ_name == 'AT':
            mins = 0
        else:
            try:
                mins = int(mins_val or 0)
            except Exception:
                return jsonify({'ok': False, 'error': f"Invalid minutes value: {mins_val}"}), 400
            if mins < 0:
                return jsonify({'ok': False, 'error': f"Minutes must be >= 0: {mins}"}), 400
        t2 = dict(t)
        t2['typeOfTrigger'] = typ_name
        t2['minutes'] = mins
        t3, err = _normalize_trigger_action_spec(t2)
        if err:
            return jsonify({'ok': False, 'error': err}), 400
        if t3:
            if not str(t3.get('uid') or '').strip():
                t3 = dict(t3)
                t3['uid'] = _uuid4_str()
            normalized_times.append(t3)
    arr, _ = _load_trigger_templates_list()
    if idx < 0 or idx >= len(arr):
        return jsonify({'ok': False, 'error': 'index out of range'}), 404
    prev = arr[idx] if isinstance(arr[idx], dict) else {}
    tpl_id = str(prev.get('id') or '').strip() or _uuid4_str()
    arr[idx] = {'id': tpl_id, 'label': label, 'times': normalized_times}
    ok = _save_trigger_templates_list(arr)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
    try:
        log_event('template.trigger.update', f"Updated trigger template '{label}'", source='web', status='success', target_type='trigger_template', target_id=tpl_id, details={'old': prev, 'new': arr[idx], 'trigger_count': len(normalized_times)})
    except Exception:
        pass
    return jsonify({'ok': True, 'template': arr[idx]})


@app.route('/api/ui/events')
def api_ui_events():
    # return a JSON list of events for the UI (reads the same storage the calendar app uses)
    try:
        from package.apps.calendar import storage
    except Exception:
        return jsonify([])

    try:
        cfg = utils.get_config()
        events_file = cfg.get('EVENTS_FILE', storage.DEFAULT_EVENTS_FILE)
        events = storage.load_events(events_file)
        out = []
        for e in events:
            out.append({
                'id': getattr(e, 'id', None),
                'name': e.name,
                'date': e.date.strftime('%Y-%m-%d'),
                'time': e.time.strftime('%H:%M:%S'),
                'repeating': e.repeating,
                'active': getattr(e, 'active', True),
                'times': [
                    {
                        'minutes': t.minutes,
                        'typeOfTrigger': getattr(t.typeOfTrigger, 'name', str(t.typeOfTrigger)),
                        'uid': getattr(t, 'uid', None),
                        'name': str(getattr(t, 'name', '') or '').strip(),
                        'enabled': bool(getattr(t, 'enabled', True)),
                        'actionType': str(getattr(t, 'actionType', 'companion') or 'companion').lower(),
                        'buttonURL': t.buttonURL if str(getattr(t, 'actionType', 'companion') or 'companion').lower() == 'companion' else '',
                        'api': getattr(t, 'api', None) if str(getattr(t, 'actionType', 'companion') or 'companion').lower() == 'api' else None,
                        'timer': getattr(t, 'timer', None) if str(getattr(t, 'actionType', 'companion') or 'companion').lower() == 'timer' else None,
                    }
                    for t in e.times
                ],
            })
        resp = jsonify(out)
        # prevent client-side caching so manual edits to the events file are picked up
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp
    except Exception:
        return jsonify([])


_DIGICO_CONFIG_KEYS = (
    'digico_enabled',
    'digico_ip',
    'digico_port',
    'digico_listen_address',
    'digico_listen_port',
    'digico_request_interval',
    'digico_retry_interval',
    'digico_stale_after',
    'digico_auxes',
    'digico_channels',
    'digico_external_devices',
)


def _digico_api_guard(*, aux_number: int | None = None, config_only: bool = False):
    """Enforce login, page access and AUX scope on every DiGiCo API call."""
    if not _auth_enabled():
        return None
    if _api_request_is_automation_principal():
        return None
    if not getattr(current_user, 'is_authenticated', False):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    page_key = 'page:config' if config_only else 'page:digico_mixer'
    if not can_access(page_key):
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if aux_number is None or config_only:
        return None
    try:
        allowed = _effective_digico_aux_ids_for_user(int(current_user.get_id()))
    except Exception:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if allowed and str(int(aux_number)) not in set(allowed):
        return jsonify({'ok': False, 'error': 'This AUX is not assigned to your group.'}), 403
    return None


def _digico_allowed_aux_ids_current_user() -> list[str]:
    if not _auth_enabled():
        return []
    if _api_request_is_automation_principal():
        return []
    try:
        return _effective_digico_aux_ids_for_user(int(current_user.get_id()))
    except Exception:
        return []


def _digico_filtered_mixer_config(client) -> dict:
    cfg = client.mixer_config() if client is not None else {'auxes': [], 'channels': [], 'snapshot': '', 'revision': 0}
    allowed = set(_digico_allowed_aux_ids_current_user())
    auxes = [
        item for item in (cfg.get('auxes') or [])
        if bool(item.get('enabled', True)) and (not allowed or str(item.get('channel')) in allowed)
    ]
    channels = [item for item in (cfg.get('channels') or []) if bool(item.get('enabled', True))]
    return {**cfg, 'auxes': auxes, 'channels': channels}


def _digico_route_is_enabled(client, aux_number: int, channel_number: int | None = None) -> bool:
    route_enabled = getattr(client, 'route_enabled', None)
    if callable(route_enabled):
        try:
            return bool(route_enabled(aux_number, channel_number))
        except Exception:
            return False
    cfg = _digico_filtered_mixer_config(client)
    if not any(int(item.get('channel', 0)) == int(aux_number) for item in cfg.get('auxes', [])):
        return False
    if channel_number is not None and not any(
        int(item.get('channel', 0)) == int(channel_number) for item in cfg.get('channels', [])
    ):
        return False
    return True


def _digico_clean_number(value, default, low, high, *, integer=False):
    try:
        number = int(value) if integer else float(value)
    except Exception:
        number = default
    number = max(low, min(high, number))
    return int(number) if integer else float(number)


_DIGICO_ICON_IDS = frozenset({
    'vocals', 'drums', 'keyboard', 'acoustic', 'electric',
    'bass', 'speaker', 'headset', 'tracks', 'fx',
})


def _digico_clean_indexed_items(value, *, kind: str) -> list[dict]:
    if not isinstance(value, list):
        return []
    by_number: dict[int, dict] = {}
    limit = 512 if kind == 'channel' else 128
    for position, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw.get('channel', position))
        except Exception:
            number = position
        if number < 1 or number > limit:
            continue
        item = {
            'enabled': bool(raw.get('enabled', True)),
            'label': str(raw.get('label') or '').strip()[:80],
            'icon': '',
            'order': int(_digico_clean_number(raw.get('order', number), number, 1, limit, integer=True)),
        }
        icon = str(raw.get('icon') or '').strip()
        item['icon'] = icon if icon in _DIGICO_ICON_IDS else ''
        if kind == 'channel':
            item['group'] = str(raw.get('group') or '').strip()[:80]
        else:
            colour = str(raw.get('colour') or '').strip()
            item['colour'] = colour[:32]
        by_number[number] = item
    if not by_number:
        return []
    return [by_number.get(number, {}) for number in range(1, max(by_number) + 1)]


def _digico_clean_external_devices(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for raw in value[:64]:
        if not isinstance(raw, dict):
            continue
        ip = str(raw.get('ip') or '').strip()[:253]
        if not ip:
            continue
        out.append({
            'name': str(raw.get('name') or '').strip()[:80],
            'ip': ip,
            'port': int(_digico_clean_number(raw.get('port', 8000), 8000, 1, 65535, integer=True)),
            'enabled': bool(raw.get('enabled', True)),
            'broadcast': bool(raw.get('broadcast', True)),
            'loopback': bool(raw.get('loopback', False)),
        })
    return out


@app.route('/api/digico/setup', methods=['GET', 'POST'])
def api_digico_setup():
    denied = _digico_api_guard(config_only=True)
    if denied is not None:
        return denied
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'ok': False, 'error': 'invalid json'}), 400
        try:
            cfg = utils.get_config()
            old_cfg = copy.deepcopy(cfg)
            cfg['digico_enabled'] = bool(data.get('digico_enabled', cfg.get('digico_enabled', False)))
            cfg['digico_ip'] = str(data.get('digico_ip', cfg.get('digico_ip', '')) or '').strip()[:253]
            cfg['digico_port'] = int(_digico_clean_number(data.get('digico_port', cfg.get('digico_port', 9000)), 9000, 1, 65535, integer=True))
            cfg['digico_listen_address'] = str(data.get('digico_listen_address', cfg.get('digico_listen_address', '0.0.0.0')) or '0.0.0.0').strip()[:253]
            cfg['digico_listen_port'] = int(_digico_clean_number(data.get('digico_listen_port', cfg.get('digico_listen_port', 8000)), 8000, 1, 65535, integer=True))
            cfg['digico_request_interval'] = _digico_clean_number(data.get('digico_request_interval', cfg.get('digico_request_interval', 0.1)), 0.1, 0.025, 5.0)
            cfg['digico_retry_interval'] = _digico_clean_number(data.get('digico_retry_interval', cfg.get('digico_retry_interval', 1.0)), 1.0, 0.1, 30.0)
            cfg['digico_stale_after'] = _digico_clean_number(data.get('digico_stale_after', cfg.get('digico_stale_after', 10.0)), 10.0, 1.0, 300.0)
            if 'digico_auxes' in data:
                cfg['digico_auxes'] = _digico_clean_indexed_items(data.get('digico_auxes'), kind='aux')
            if 'digico_channels' in data:
                cfg['digico_channels'] = _digico_clean_indexed_items(data.get('digico_channels'), kind='channel')
            if 'digico_external_devices' in data:
                cfg['digico_external_devices'] = _digico_clean_external_devices(data.get('digico_external_devices'))
            utils.save_config(cfg)
            utils.reload_config(force=True)
            if _close_digico_client is not None:
                _close_digico_client()
            client = _get_digico_client_from_config()
            changed = sorted(k for k in _DIGICO_CONFIG_KEYS if old_cfg.get(k) != cfg.get(k))
            log_event(
                'digico.config.update',
                f"Updated DiGiCo mixer setup ({len(changed)} setting{'s' if len(changed) != 1 else ''})",
                source='web', status='success', target_type='integration', target_id='digico',
                details={'changed_keys': changed},
            )
            return jsonify({'ok': True, 'status': client.status() if client is not None else {}})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 500

    try:
        cfg = utils.get_config()
    except Exception:
        cfg = {}
    client = _get_digico_client_from_config()
    return jsonify({
        'ok': True,
        'config': {key: cfg.get(key) for key in _DIGICO_CONFIG_KEYS},
        'discovered': client.mixer_config() if client is not None else {'auxes': [], 'channels': []},
        'status': client.status() if client is not None else {},
    })


@app.route('/api/digico/restart', methods=['POST'])
def api_digico_restart():
    denied = _digico_api_guard(config_only=True)
    if denied is not None:
        return denied
    try:
        if _close_digico_client is not None:
            _close_digico_client()
        client = _get_digico_client_from_config()
        log_event('digico.restart', 'Restarted the DiGiCo mixer connection', source='web', status='success', target_type='integration', target_id='digico')
        return jsonify({'ok': True, 'status': client.status() if client is not None else {}})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/digico/discover', methods=['POST'])
def api_digico_discover():
    denied = _digico_api_guard(config_only=True)
    if denied is not None:
        return denied
    client = _get_digico_client_from_config()
    if client is None:
        return jsonify({'ok': False, 'error': 'DiGiCo integration is unavailable'}), 503
    client.restart_discovery()
    log_event('digico.discovery.restart', 'Restarted DiGiCo desk discovery', source='web', status='success', target_type='integration', target_id='digico')
    return jsonify({'ok': True, 'status': client.status()})


@app.route('/api/digico/mixer/config')
def api_digico_mixer_config():
    denied = _digico_api_guard()
    if denied is not None:
        return denied
    client = _get_digico_client_from_config()
    if client is None:
        return jsonify({'ok': False, 'error': 'DiGiCo integration is unavailable'}), 503
    return jsonify({'ok': True, **_digico_filtered_mixer_config(client), 'status': client.status()})


@app.route('/api/digico/input-meters')
def api_digico_input_meters():
    denied = _digico_api_guard()
    if denied is not None:
        return denied
    client = _get_digico_client_from_config()
    if client is None:
        return jsonify({'ok': False, 'error': 'DiGiCo integration is unavailable'}), 503
    try:
        payload = client.input_meter_state()
        current_revision = int(payload.get('revision', 0) or 0)
        known_revision = request.args.get('revision')
        try:
            known_revision_number = int(known_revision) if known_revision is not None else None
        except Exception:
            known_revision_number = None
        if known_revision_number is not None and known_revision_number == current_revision:
            return jsonify({
                'ok': True,
                'unchanged': True,
                'revision': current_revision,
            })
        return jsonify({'ok': True, **payload})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503


@app.route('/api/digico/aux/<int:aux_number>/state')
def api_digico_aux_state(aux_number: int):
    denied = _digico_api_guard(aux_number=aux_number)
    if denied is not None:
        return denied
    client = _get_digico_client_from_config()
    if client is None:
        return jsonify({'ok': False, 'error': 'DiGiCo integration is unavailable'}), 503
    if not _digico_route_is_enabled(client, aux_number):
        return jsonify({'ok': False, 'error': 'AUX not found'}), 404
    try:
        client.request_aux_state(aux_number)
        status = client.status()
        known_revision = request.args.get('revision')
        try:
            known_revision_number = int(known_revision) if known_revision is not None else None
        except Exception:
            known_revision_number = None
        current_revision = int(status.get('revision', 0) or 0)
        if known_revision_number is not None and known_revision_number == current_revision:
            return jsonify({
                'ok': True,
                'unchanged': True,
                'revision': current_revision,
                'status': status,
            })
        payload = client.aux_state(aux_number)
        payload['channels'] = [item for item in payload.get('channels', []) if bool(item.get('enabled', True))]
        return jsonify({'ok': True, **payload, 'status': client.status()})
    except KeyError:
        return jsonify({'ok': False, 'error': 'AUX not found'}), 404
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503


@app.route('/api/digico/aux/<int:aux_number>/channel/<int:channel_number>/<field>', methods=['POST'])
def api_digico_channel_control(aux_number: int, channel_number: int, field: str):
    denied = _digico_api_guard(aux_number=aux_number)
    if denied is not None:
        return denied
    if field not in ('level', 'pan', 'on'):
        return jsonify({'ok': False, 'error': 'Unknown control'}), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'value' not in data:
        return jsonify({'ok': False, 'error': 'A value is required'}), 400
    client = _get_digico_client_from_config()
    if client is None:
        return jsonify({'ok': False, 'error': 'DiGiCo integration is unavailable'}), 503
    if not _digico_route_is_enabled(client, aux_number, channel_number):
        return jsonify({'ok': False, 'error': 'Mixer route not found'}), 404
    try:
        if field == 'level':
            value = client.set_level(aux_number, channel_number, float(data['value']))
        elif field == 'pan':
            value = client.set_pan(aux_number, channel_number, float(data['value']))
        else:
            raw = data['value']
            if isinstance(raw, bool):
                enabled = raw
            elif isinstance(raw, (int, float)) and raw in (0, 1):
                enabled = bool(raw)
            elif isinstance(raw, str) and raw.strip().lower() in ('0', '1', 'false', 'true', 'off', 'on'):
                enabled = raw.strip().lower() in ('1', 'true', 'on')
            else:
                return jsonify({'ok': False, 'error': 'On/off value must be boolean'}), 400
            value = client.set_send_on(aux_number, channel_number, enabled)
        if bool(data.get('final', False)):
            log_event(
                f'digico.mix.{field}',
                f"Changed AUX {aux_number} channel {channel_number} {field}",
                source='web', status='success', target_type='digico_aux', target_id=str(aux_number),
                details={'aux': aux_number, 'channel': channel_number, 'field': field, 'value': value},
            )
        return jsonify({'ok': True, 'value': value})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503


@app.route('/api/companion_status')
def companion_status():
    snapshot = _get_status_snapshot()
    companion = snapshot.get('companion') if isinstance(snapshot, dict) else {}
    return jsonify({'connected': bool(companion.get('connected', False))})


@app.route('/api/propresenter_status')
def propresenter_status():
    """Lightweight ProPresenter connectivity check for the UI indicator."""
    snapshot = _get_status_snapshot()
    propresenter = snapshot.get('propresenter') if isinstance(snapshot, dict) else {}
    return jsonify({'connected': bool(propresenter.get('connected', False))})


@app.route('/api/videohub_status')
def videohub_status():
    """Lightweight VideoHub connectivity check for the UI indicator."""
    snapshot = _get_status_snapshot()
    videohub = snapshot.get('videohub') if isinstance(snapshot, dict) else {}
    return jsonify({'connected': bool(videohub.get('connected', False))})


@app.route('/api/digico_status')
def digico_status():
    """Lightweight DiGiCo connectivity check for the UI indicator."""
    snapshot = _get_status_snapshot()
    digico = snapshot.get('digico') if isinstance(snapshot, dict) else {}
    return jsonify({'connected': bool(digico.get('connected', False)), 'enabled': bool(digico.get('enabled', False))})


@app.route('/api/atem_status')
def atem_status():
    """Lightweight ATEM connectivity check for the UI indicator."""
    snapshot = _get_status_snapshot()
    atem = snapshot.get('atem') if isinstance(snapshot, dict) else {}
    return jsonify({'connected': bool(atem.get('connected', False))})


@app.route('/api/status/summary')
def api_status_summary():
    """Return a consolidated connectivity snapshot for the top navbar."""
    return jsonify(_get_status_snapshot())


@app.route('/api/scheduler_status')
def api_scheduler_status():
    """Expose scheduler liveness and queue diagnostics for operators."""
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    return jsonify(_probe_scheduler_status(cfg))


def _hisense_manager_or_error():
    manager = _get_hisense_manager_from_config()
    if manager is None:
        return None, (jsonify({'ok': False, 'error': 'Hisense support is unavailable; install the Python requirements'}), 503)
    return manager, None


def _hisense_log(action: str, tv_id: str, *, value: Any = None, result: dict | None = None) -> None:
    ok = bool((result or {}).get('ok', False))
    pending = bool((result or {}).get('pending', False))
    is_group = str(tv_id).startswith('group:')
    target_label = 'TV group' if is_group else 'TV'
    pending_suffix = ' (awaiting TV confirmation)' if pending else ''
    try:
        log_event(
            f'hisense.{action}',
            f"{target_label} {tv_id}: {action.replace('_', ' ')}{' ' + str(value) if value not in (None, '') else ''}{pending_suffix}",
            source='api',
            status='warning' if ok and pending else ('success' if ok else 'failure'),
            target_type='hisense_tv_group' if is_group else 'hisense_tv',
            target_id=tv_id,
            details={'action': action, 'value': value, 'result': result or {}},
        )
    except Exception:
        pass


@app.get('/api/tvs')
def api_hisense_tvs():
    manager, error = _hisense_manager_or_error()
    if error:
        return error
    return jsonify(manager.status())


@app.get('/api/tvs/<tv_id>/state')
def api_hisense_tv_state(tv_id: str):
    manager, error = _hisense_manager_or_error()
    if error:
        return error
    try:
        return jsonify({'ok': True, 'tv': manager.get(tv_id).status()})
    except KeyError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404


def _hisense_submit(tv_id: str, action: str, value: Any = None, *, wait: float = 4.0):
    manager, error = _hisense_manager_or_error()
    if error:
        return error
    try:
        result = manager.get(tv_id).submit(action, value, wait=wait)
        _hisense_log(action, tv_id, value=value, result=result)
        return jsonify(result), (200 if result.get('ok') else 502)
    except KeyError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404
    except (TypeError, ValueError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        result = {'ok': False, 'error': str(exc)}
        _hisense_log(action, tv_id, value=value, result=result)
        return jsonify(result), 500


def _hisense_submit_target(target_id: str, action: str, value: Any = None, *, wait: float = 4.0):
    manager, error = _hisense_manager_or_error()
    if error:
        return error
    try:
        result = manager.submit_target(target_id, action, value, wait=wait)
        _hisense_log(action, target_id, value=value, result=result)
        return jsonify(result), (200 if result.get('ok') else 502)
    except KeyError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404
    except (TypeError, ValueError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        result = {'ok': False, 'error': str(exc)}
        _hisense_log(action, target_id, value=value, result=result)
        return jsonify(result), 500


@app.post('/api/tvs/<tv_id>/power')
def api_hisense_tv_power(tv_id: str):
    body = request.get_json(silent=True) or {}
    state = str(body.get('state') or '').strip().lower()
    actions = {'on': 'power_on', 'off': 'power_off', 'toggle': 'power_toggle'}
    if state not in actions:
        return jsonify({'ok': False, 'error': 'state must be on, off, or toggle'}), 400
    # Wake-on-LAN returns immediately; the TV's controller reconnects in the background.
    return _hisense_submit(tv_id, actions[state], wait=4.0)


@app.post('/api/tvs/<tv_id>/volume')
def api_hisense_tv_volume(tv_id: str):
    body = request.get_json(silent=True) or {}
    if body.get('level') is not None:
        try:
            level = int(body.get('level'))
        except Exception:
            return jsonify({'ok': False, 'error': 'level must be a number from 0 to 100'}), 400
        if level < 0 or level > 100:
            return jsonify({'ok': False, 'error': 'level must be from 0 to 100'}), 400
        return _hisense_submit(tv_id, 'volume_set', level)
    action = str(body.get('action') or '').strip().lower()
    actions = {'up': 'volume_up', 'down': 'volume_down', 'mute': 'mute'}
    if action not in actions:
        return jsonify({'ok': False, 'error': 'provide level, or action up, down, or mute'}), 400
    return _hisense_submit(tv_id, actions[action])


@app.post('/api/tvs/<tv_id>/source')
def api_hisense_tv_source(tv_id: str):
    body = request.get_json(silent=True) or {}
    source = str(body.get('source') or '').strip()
    if not source:
        return jsonify({'ok': False, 'error': 'source is required'}), 400
    return _hisense_submit(tv_id, 'source', source)


@app.post('/api/tvs/<tv_id>/reconnect')
def api_hisense_tv_reconnect(tv_id: str):
    return _hisense_submit(tv_id, 'reconnect', wait=5.0)


@app.post('/api/tvs/<tv_id>/preflight')
def api_hisense_tv_preflight(tv_id: str):
    """Run setup-only, read-only connectivity and authentication diagnostics."""
    access_error = _config_access_error_json()
    if access_error:
        return access_error
    if _auth_enabled() and not _api_request_is_automation_principal() and not _validate_csrf():
        return jsonify({'ok': False, 'error': 'invalid CSRF token'}), 400
    manager, error = _hisense_manager_or_error()
    if error:
        return error
    try:
        result = manager.get(tv_id).submit('preflight', wait=10.0)
    except KeyError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404
    except (TypeError, ValueError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500

    report = result.get('preflight') if isinstance(result.get('preflight'), dict) else {}
    pending = bool(result.get('pending', False))
    diagnostic_state = str(report.get('state') or ('running' if pending else 'unknown'))
    try:
        log_event(
            'hisense.preflight',
            f'TV {tv_id}: read-only setup preflight {diagnostic_state}',
            source='web',
            status=(
                'success' if report.get('ready')
                else ('info' if diagnostic_state in {'queued', 'running', 'expected-off'} else 'warning')
            ),
            target_type='hisense_tv',
            target_id=tv_id,
            details={
                'state': diagnostic_state,
                'ready': bool(report.get('ready', False)),
                'check_statuses': {
                    str(check.get('id')): str(check.get('status'))
                    for check in report.get('checks', [])
                    if isinstance(check, dict) and check.get('id')
                },
            },
        )
    except Exception:
        pass
    return jsonify(result), (202 if pending else 200)


@app.get('/api/tv-targets/<target_id>/state')
def api_hisense_target_state(target_id: str):
    manager, error = _hisense_manager_or_error()
    if error:
        return error
    try:
        return jsonify({'ok': True, 'target': manager.target_status(target_id)})
    except KeyError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 404


@app.post('/api/tv-targets/<target_id>/power')
def api_hisense_target_power(target_id: str):
    body = request.get_json(silent=True) or {}
    state = str(body.get('state') or '').strip().lower()
    actions = {'on': 'power_on', 'off': 'power_off', 'toggle': 'power_toggle'}
    if state not in actions:
        return jsonify({'ok': False, 'error': 'state must be on, off, or toggle'}), 400
    return _hisense_submit_target(target_id, actions[state], wait=4.0)


@app.post('/api/tv-targets/<target_id>/volume')
def api_hisense_target_volume(target_id: str):
    body = request.get_json(silent=True) or {}
    if body.get('level') is not None:
        try:
            level = int(body.get('level'))
        except Exception:
            return jsonify({'ok': False, 'error': 'level must be a number from 0 to 100'}), 400
        if level < 0 or level > 100:
            return jsonify({'ok': False, 'error': 'level must be from 0 to 100'}), 400
        return _hisense_submit_target(target_id, 'volume_set', level)
    action = str(body.get('action') or '').strip().lower()
    actions = {'up': 'volume_up', 'down': 'volume_down', 'mute': 'mute'}
    if action not in actions:
        return jsonify({'ok': False, 'error': 'provide level, or action up, down, or mute'}), 400
    return _hisense_submit_target(target_id, actions[action])


@app.post('/api/tv-targets/<target_id>/source')
def api_hisense_target_source(target_id: str):
    body = request.get_json(silent=True) or {}
    source = str(body.get('source') or '').strip()
    if not source:
        return jsonify({'ok': False, 'error': 'source is required'}), 400
    return _hisense_submit_target(target_id, 'source', source)


@app.post('/api/tv-targets/<target_id>/reconnect')
def api_hisense_target_reconnect(target_id: str):
    return _hisense_submit_target(target_id, 'reconnect', wait=5.0)


@app.get('/api/hisense/config')
def api_hisense_config_get():
    access_error = _config_access_error_json()
    if access_error:
        return access_error
    cfg = utils.get_config()
    keys = (
        'hisense_enabled', 'hisense_cert_path', 'hisense_key_path',
        'hisense_poll_interval', 'hisense_reconnect_interval',
        'hisense_compatible_models', 'hisense_certificate_profiles',
        'hisense_tv_groups', 'hisense_tvs',
    )
    result = {key: cfg.get(key) for key in keys}
    if not isinstance(result.get('hisense_certificate_profiles'), list) or not result['hisense_certificate_profiles']:
        result['hisense_certificate_profiles'] = [{
            'id': 'default',
            'name': 'Default / legacy',
            'cert_path': result.get('hisense_cert_path') or 'hisense_certs/vidaa_client.pem',
            'key_path': result.get('hisense_key_path') or 'hisense_certs/vidaa_client.key',
            'compatible_models': '55A7G, 65A7G',
            'enabled': True,
        }]
    if not isinstance(result.get('hisense_tv_groups'), list):
        result['hisense_tv_groups'] = []
    return jsonify({'ok': True, 'config': result})


@app.put('/api/hisense/config')
def api_hisense_config_put():
    access_error = _config_access_error_json()
    if access_error:
        return access_error
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'ok': False, 'error': 'invalid JSON payload'}), 400
    cfg = utils.get_config()
    tvs = body.get('hisense_tvs')
    if not isinstance(tvs, list):
        return jsonify({'ok': False, 'error': 'hisense_tvs must be a list'}), 400
    raw_profiles = body.get('hisense_certificate_profiles')
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raw_profiles = cfg.get('hisense_certificate_profiles')
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raw_profiles = [{
            'id': 'default',
            'name': 'Default / legacy',
            'cert_path': cfg.get('hisense_cert_path') or 'hisense_certs/vidaa_client.pem',
            'key_path': cfg.get('hisense_key_path') or 'hisense_certs/vidaa_client.key',
            'compatible_models': cfg.get('hisense_compatible_models') or '55A7G, 65A7G',
            'enabled': True,
        }]
    profile_ids: set[str] = set()
    profiles = []
    for index, profile in enumerate(raw_profiles):
        if not isinstance(profile, dict):
            return jsonify({'ok': False, 'error': f'Certificate profile {index + 1} is invalid'}), 400
        ident = re.sub(r'[^a-z0-9_-]+', '-', str(profile.get('id') or '').strip().lower()).strip('-')
        if not ident or ident in profile_ids:
            return jsonify({'ok': False, 'error': f'Certificate profile {index + 1} needs a unique ID'}), 400
        profile_ids.add(ident)
        cert_path = str(profile.get('cert_path') or '').strip()
        key_path = str(profile.get('key_path') or '').strip()
        if not cert_path or not key_path:
            return jsonify({'ok': False, 'error': f'{ident} needs both a certificate and key path'}), 400
        profiles.append({
            'id': ident,
            'name': str(profile.get('name') or ident).strip(),
            'cert_path': cert_path,
            'key_path': key_path,
            'compatible_models': str(profile.get('compatible_models') or '').strip(),
            'enabled': bool(profile.get('enabled', True)),
        })
    seen: set[str] = set()
    normalized = []
    for index, tv in enumerate(tvs):
        if not isinstance(tv, dict):
            return jsonify({'ok': False, 'error': f'TV {index + 1} is invalid'}), 400
        ident = re.sub(r'[^a-z0-9_-]+', '-', str(tv.get('id') or '').strip().lower()).strip('-')
        if not ident or ident in seen:
            return jsonify({'ok': False, 'error': f'TV {index + 1} needs a unique ID'}), 400
        seen.add(ident)
        host = str(tv.get('host') or '').strip()
        mac = str(tv.get('mac') or '').strip().lower().replace('-', ':')
        uuid = str(tv.get('uuid') or '').strip().replace('-', ':')
        if bool(tv.get('enabled', True)) and (not host or not re.fullmatch(r'[0-9A-Za-z.-]+', host)):
            return jsonify({'ok': False, 'error': f'{ident} needs a valid IP address or hostname'}), 400
        if not mac:
            return jsonify({'ok': False, 'error': f'{ident} needs the TV MAC address for power-on'}), 400
        if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
            return jsonify({'ok': False, 'error': f'{ident} has an invalid MAC address'}), 400
        if uuid and not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', uuid):
            return jsonify({'ok': False, 'error': f'{ident} has an invalid paired device UUID'}), 400
        normalized.append({
            'id': ident,
            'name': str(tv.get('name') or ident).strip(),
            'host': host,
            'mac': mac,
            'uuid': uuid,
            # The simplified UI deliberately makes these backend concerns.
            # Existing configs migrate to automatic handling on the next save.
            'enabled': True,
            'auth_mode': 'auto',
            'certificate_profile': 'auto',
        })
    raw_groups = body.get('hisense_tv_groups')
    if raw_groups is None:
        raw_groups = []
    if not isinstance(raw_groups, list):
        return jsonify({'ok': False, 'error': 'hisense_tv_groups must be a list'}), 400
    group_ids: set[str] = set()
    assigned_group_tv_ids: set[str] = set()
    groups = []
    for index, group in enumerate(raw_groups):
        if not isinstance(group, dict):
            return jsonify({'ok': False, 'error': f'TV group {index + 1} is invalid'}), 400
        ident = re.sub(r'[^a-z0-9_-]+', '-', str(group.get('id') or '').strip().lower()).strip('-')
        if not ident or ident in group_ids:
            return jsonify({'ok': False, 'error': f'TV group {index + 1} needs a unique ID'}), 400
        group_ids.add(ident)
        member_ids = []
        members_seen: set[str] = set()
        for raw_tv_id in group.get('tv_ids') if isinstance(group.get('tv_ids'), list) else []:
            tv_id = re.sub(r'[^a-z0-9_-]+', '-', str(raw_tv_id or '').strip().lower()).strip('-')
            if tv_id in seen and tv_id not in members_seen and tv_id not in assigned_group_tv_ids:
                members_seen.add(tv_id)
                assigned_group_tv_ids.add(tv_id)
                member_ids.append(tv_id)
        groups.append({
            'id': ident,
            'name': str(group.get('name') or ident).strip(),
            'enabled': True,
            'tv_ids': member_ids,
        })
    primary_profile = profiles[0]
    updates = {
        'hisense_enabled': bool(normalized),
        # Keep the original keys synchronized for compatibility with older
        # TDeck builds and config exports.
        'hisense_cert_path': primary_profile['cert_path'],
        'hisense_key_path': primary_profile['key_path'],
        'hisense_poll_interval': max(2, int(cfg.get('hisense_poll_interval') or 10)),
        'hisense_reconnect_interval': max(2, int(cfg.get('hisense_reconnect_interval') or 15)),
        'hisense_compatible_models': str(
            cfg.get('hisense_compatible_models') or 'Confirmed: 55A7G, 65A7G'
        ).strip(),
        'hisense_certificate_profiles': profiles,
        'hisense_tv_groups': groups,
        'hisense_tvs': normalized,
    }
    cfg.update(updates)
    utils.save_config(cfg)
    utils.reload_config(force=True)
    if _close_hisense_manager is not None:
        _close_hisense_manager()
    manager = _get_hisense_manager_from_config()
    log_event(
        'hisense.config.update',
        f'Updated Hisense TV configuration ({len(normalized)} TVs, {len(groups)} groups)',
        source='web',
        status='success',
        target_type='config',
        target_id='hisense',
        details={
            'tv_ids': sorted(seen),
            'group_ids': [group['id'] for group in groups],
            'certificate_profiles': [profile['id'] for profile in profiles],
            'enabled': updates['hisense_enabled'],
        },
    )
    return jsonify({'ok': True, 'config': updates, 'status': manager.status() if manager else {}})


@app.post('/api/tvs/<tv_id>/pair/request')
def api_hisense_tv_pair_request(tv_id: str):
    access_error = _config_access_error_json()
    if access_error:
        return access_error
    return _hisense_submit(tv_id, 'pair_request', wait=5.0)


@app.post('/api/tvs/<tv_id>/pair/submit')
def api_hisense_tv_pair_submit(tv_id: str):
    access_error = _config_access_error_json()
    if access_error:
        return access_error
    body = request.get_json(silent=True) or {}
    return _hisense_submit(tv_id, 'pair_submit', str(body.get('pin') or '').strip(), wait=7.0)


def _pixie_api_guard(*, config_only: bool = False, require_csrf: bool = False):
    if _auth_enabled():
        if _api_request_is_automation_principal():
            return None
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        page_key = 'page:config' if config_only else 'page:pixie_controls'
        if not can_access(page_key):
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
        if require_csrf and not _validate_csrf():
            return jsonify({'ok': False, 'error': 'invalid CSRF token'}), 400
    return None


def _pixie_slug(value: Any, fallback: str = '') -> str:
    value = re.sub(r'[^a-z0-9_-]+', '-', str(value or '').strip().lower()).strip('-')
    return value or fallback


def _pixie_catalog(cfg: dict, status: dict | None = None) -> dict[str, list[dict[str, Any]]]:
    """Merge saved names/order with live inventory without losing missing devices."""
    status = status if isinstance(status, dict) else {}
    live_devices = {
        str(item.get('id')): item
        for item in status.get('devices') if isinstance(status.get('devices'), list) and isinstance(item, dict) and str(item.get('id') or '').strip()
    }
    live_scenes = {
        str(item.get('id')): item
        for item in status.get('scenes') if isinstance(status.get('scenes'), list) and isinstance(item, dict) and str(item.get('id') or '').strip()
    }
    saved_devices = cfg.get('pixie_devices') if isinstance(cfg.get('pixie_devices'), list) else []
    saved_scenes = cfg.get('pixie_scenes') if isinstance(cfg.get('pixie_scenes'), list) else []

    devices: list[dict[str, Any]] = []
    seen_devices: set[str] = set()
    for raw in [*saved_devices, *[item for ident, item in live_devices.items() if ident not in {str(v.get('id')) for v in saved_devices if isinstance(v, dict)}]]:
        if not isinstance(raw, dict):
            continue
        ident = str(raw.get('id') or '').strip()
        if not ident or ident in seen_devices:
            continue
        seen_devices.add(ident)
        live = live_devices.get(ident, {})
        original_name = str(live.get('name') or raw.get('original_name') or raw.get('name') or ident)
        detected_kind = str(live.get('kind') or raw.get('detected_kind') or 'unknown')
        control_type = str(raw.get('control_type') or 'automatic').strip().lower()
        if control_type not in ('automatic', 'dimmer', 'on_off'):
            control_type = 'automatic'
        resolved_type = control_type if control_type != 'automatic' else (
            'dimmer' if detected_kind == 'dimmer' else 'on_off' if detected_kind == 'switch' else 'unknown'
        )
        online = live.get('online') if isinstance(live.get('online'), bool) else None
        devices.append({
            'id': ident,
            'name': str(raw.get('name') or original_name),
            'original_name': original_name,
            'model': str(live.get('model') or raw.get('model') or ''),
            'detected_kind': detected_kind,
            'control_type': control_type,
            'resolved_type': resolved_type,
            'online': online,
            'on': live.get('on'),
            'brightness': live.get('brightness'),
            'missing': ident not in live_devices,
        })

    scenes: list[dict[str, Any]] = []
    seen_scenes: set[str] = set()
    saved_scene_ids = {str(v.get('id')) for v in saved_scenes if isinstance(v, dict)}
    for raw in [*saved_scenes, *[item for ident, item in live_scenes.items() if ident not in saved_scene_ids]]:
        if not isinstance(raw, dict):
            continue
        ident = str(raw.get('id') or '').strip()
        if not ident or ident in seen_scenes:
            continue
        seen_scenes.add(ident)
        live = live_scenes.get(ident, {})
        original_name = str(live.get('name') or raw.get('original_name') or raw.get('name') or ident)
        scenes.append({
            'id': ident,
            'name': str(raw.get('name') or original_name),
            'original_name': original_name,
            'enabled': bool(raw.get('enabled', True)),
            'missing': ident not in live_scenes,
        })

    auditoriums: list[dict[str, Any]] = []
    seen_auditoriums: set[str] = set()
    assigned: set[str] = set()
    for raw in cfg.get('pixie_auditoriums') if isinstance(cfg.get('pixie_auditoriums'), list) else []:
        if not isinstance(raw, dict):
            continue
        ident = _pixie_slug(raw.get('id'))
        if not ident or ident in seen_auditoriums:
            continue
        seen_auditoriums.add(ident)
        member_ids = []
        for value in raw.get('device_ids') if isinstance(raw.get('device_ids'), list) else []:
            device_id = str(value or '').strip()
            if device_id and device_id in seen_devices and device_id not in assigned:
                member_ids.append(device_id)
                assigned.add(device_id)
        auditoriums.append({'id': ident, 'name': str(raw.get('name') or ident), 'device_ids': member_ids})
    return {'auditoriums': auditoriums, 'devices': devices, 'scenes': scenes}


def _pixie_saved_catalog_payload(cfg: dict, status: dict) -> dict[str, Any]:
    catalog = _pixie_catalog(cfg, status)
    return {
        'pixie_auditoriums': catalog['auditoriums'],
        'pixie_devices': [{
            key: item.get(key) for key in ('id', 'name', 'original_name', 'model', 'detected_kind', 'control_type', 'online', 'missing')
        } for item in catalog['devices']],
        'pixie_scenes': [{
            key: item.get(key) for key in ('id', 'name', 'original_name', 'enabled', 'missing')
        } for item in catalog['scenes']],
    }


def _pixie_current_permissions() -> dict[str, Any]:
    if not _auth_enabled():
        return {'all': True, 'auditoriums': {}, 'scenes': []}
    try:
        return _effective_pixie_permissions_for_user(int(current_user.get_id()))
    except Exception:
        return {'all': False, 'auditoriums': {}, 'scenes': []}


def _pixie_operator_payload(cfg: dict, status: dict, selected_auditorium_id: str = '') -> dict[str, Any]:
    catalog = _pixie_catalog(cfg, status)
    permissions = _pixie_current_permissions()
    allow_all = bool(permissions.get('all'))
    allowed_auditoriums = permissions.get('auditoriums') if isinstance(permissions.get('auditoriums'), dict) else {}
    allowed_scenes = set(_coerce_string_allow_list(permissions.get('scenes')))
    device_by_id = {item['id']: item for item in catalog['devices']}

    auditoriums = []
    selected_devices = []
    selected_allowed = False
    for auditorium in catalog['auditoriums']:
        auditorium_id = auditorium['id']
        if not allow_all and auditorium_id not in allowed_auditoriums:
            continue
        device_scope = None if allow_all else allowed_auditoriums.get(auditorium_id)
        visible_devices = []
        for device_id in auditorium['device_ids']:
            if device_scope is not None and device_id not in set(_coerce_string_allow_list(device_scope)):
                continue
            device = device_by_id.get(device_id)
            if not device or device.get('resolved_type') == 'unknown':
                continue
            visible_devices.append({
                'id': device['id'],
                'name': device['name'],
                'controlType': device['resolved_type'],
                'online': device.get('online') if isinstance(device.get('online'), bool) else None,
                'on': device.get('on'),
                'brightness': device.get('brightness'),
                # A missing device or an explicit offline signal is disabled.
                # Unknown reachability remains operable because the Gateway's
                # numeric `online` inventory field is not a live status flag.
                'disabled': bool(device.get('missing')) or device.get('online') is False,
            })
        auditoriums.append({'id': auditorium_id, 'name': auditorium['name'], 'deviceCount': len(visible_devices)})
        if selected_auditorium_id == auditorium_id:
            selected_allowed = True
            selected_devices = visible_devices

    if selected_auditorium_id and not selected_allowed:
        raise PermissionError('This auditorium is not assigned to your group.')

    scenes = [
        {'id': scene['id'], 'name': scene['name'], 'available': not bool(scene.get('missing'))}
        for scene in catalog['scenes']
        if bool(scene.get('enabled', True)) and (allow_all or scene['id'] in allowed_scenes)
    ]
    return {
        'ok': True,
        'mode': str(status.get('mode') or cfg.get('pixie_network_mode') or 'disabled'),
        'connected': bool(status.get('connected', False)),
        'controlReady': bool(status.get('controlReady', False)),
        'lastError': str(status.get('lastError') or ''),
        'reachabilityAvailable': bool(status.get('reachabilityAvailable', False)),
        'reachabilityStale': bool(status.get('reachabilityStale', False)),
        'auditoriums': auditoriums,
        'selectedAuditoriumId': selected_auditorium_id,
        'devices': selected_devices,
        'scenes': scenes,
    }


@app.get('/api/pixie/config')
def api_pixie_config_get():
    access_error = _pixie_api_guard(config_only=True)
    if access_error:
        return access_error
    cfg = utils.get_config()
    manager = _get_pixie_manager_from_config()
    refresh = str(request.args.get('refresh') or '').lower() in ('1', 'true', 'yes')
    status = (manager.refresh_inventory(force=True) if refresh else manager.status()) if manager is not None else {'available': False, 'lastError': 'Pixie support is unavailable'}
    secrets_value = _load_pixie_secrets(_APP_ROOT) if _load_pixie_secrets is not None else None
    fields = {
        key: cfg.get(key) for key in (
            'pixie_network_mode', 'pixie_gateway_host', 'pixie_home_id', 'pixie_home_name',
            'pixie_net_id', 'pixie_mesh_net', 'pixie_mesh_net_2',
        )
    }
    fields.update(_pixie_saved_catalog_payload(cfg, status))
    fields['pixie_username'] = str(getattr(secrets_value, 'username', '') or '')
    fields['pixie_password_configured'] = bool(str(getattr(secrets_value, 'password', '') or ''))
    return jsonify({'ok': True, 'config': fields, 'status': status})


@app.post('/api/pixie/discover')
def api_pixie_discover():
    access_error = _pixie_api_guard(config_only=True, require_csrf=True)
    if access_error:
        return access_error
    if _pixie_discover_gateways is None:
        return jsonify({'ok': False, 'error': 'Pixie support is unavailable'}), 503
    try:
        gateways = _pixie_discover_gateways(5.0)
        return jsonify({'ok': True, 'gateways': gateways})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502


@app.post('/api/pixie/homes')
def api_pixie_homes():
    access_error = _pixie_api_guard(config_only=True, require_csrf=True)
    if access_error:
        return access_error
    if _provision_pixie_cloud is None:
        return jsonify({'ok': False, 'error': 'Pixie support is unavailable'}), 503
    body = request.get_json(silent=True) or {}
    stored = _load_pixie_secrets(_APP_ROOT) if _load_pixie_secrets is not None else None
    username = str(body.get('username') or getattr(stored, 'username', '') or '').strip()
    password = str(body.get('password') or getattr(stored, 'password', '') or '')
    try:
        provisioning = _provision_pixie_cloud(username, password)
        homes = [{
            'id': str(home.get('objectId') or ''),
            'name': str(home.get('name') or home.get('objectId') or ''),
        } for home in provisioning.get('homes') or []]
        return jsonify({'ok': True, 'homes': homes, 'currentHomeId': provisioning.get('login', {}).get('currentHomeId')})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502


def _normalize_pixie_setup_lists(body: dict, cfg: dict, status: dict) -> dict[str, Any]:
    merged = _pixie_catalog(cfg, status)
    known_devices = {item['id']: item for item in merged['devices']}
    known_scenes = {item['id']: item for item in merged['scenes']}

    raw_devices = body.get('pixie_devices')
    if not isinstance(raw_devices, list):
        raise ValueError('pixie_devices must be a list')
    devices = []
    seen_devices: set[str] = set()
    for index, raw in enumerate(raw_devices):
        if not isinstance(raw, dict):
            raise ValueError(f'Device {index + 1} is invalid')
        ident = str(raw.get('id') or '').strip()
        if not ident or ident in seen_devices or ident not in known_devices:
            raise ValueError(f'Device {index + 1} has an invalid or duplicate ID')
        seen_devices.add(ident)
        existing = known_devices[ident]
        control_type = str(raw.get('control_type') or 'automatic').strip().lower()
        if control_type not in ('automatic', 'dimmer', 'on_off'):
            raise ValueError(f'Device {ident} has an invalid control type')
        devices.append({
            'id': ident,
            'name': str(raw.get('name') or existing['original_name']).strip() or existing['original_name'],
            'original_name': existing['original_name'],
            'model': existing.get('model') or '',
            'detected_kind': existing.get('detected_kind') or 'unknown',
            'control_type': control_type,
        })

    raw_auditoriums = body.get('pixie_auditoriums')
    if not isinstance(raw_auditoriums, list):
        raise ValueError('pixie_auditoriums must be a list')
    auditoriums = []
    auditorium_ids: set[str] = set()
    assigned: set[str] = set()
    for index, raw in enumerate(raw_auditoriums):
        if not isinstance(raw, dict):
            raise ValueError(f'Auditorium {index + 1} is invalid')
        ident = _pixie_slug(raw.get('id'))
        if not ident or ident in auditorium_ids:
            raise ValueError(f'Auditorium {index + 1} needs a unique name and ID')
        auditorium_ids.add(ident)
        member_ids = []
        for value in raw.get('device_ids') if isinstance(raw.get('device_ids'), list) else []:
            device_id = str(value or '').strip()
            if device_id in seen_devices and device_id not in assigned:
                assigned.add(device_id)
                member_ids.append(device_id)
        auditoriums.append({'id': ident, 'name': str(raw.get('name') or ident).strip(), 'device_ids': member_ids})

    raw_scenes = body.get('pixie_scenes')
    if not isinstance(raw_scenes, list):
        raise ValueError('pixie_scenes must be a list')
    scenes = []
    seen_scenes: set[str] = set()
    for index, raw in enumerate(raw_scenes):
        if not isinstance(raw, dict):
            raise ValueError(f'Scene {index + 1} is invalid')
        ident = str(raw.get('id') or '').strip()
        if not ident or ident in seen_scenes or ident not in known_scenes:
            raise ValueError(f'Scene {index + 1} has an invalid or duplicate ID')
        seen_scenes.add(ident)
        existing = known_scenes[ident]
        scenes.append({
            'id': ident,
            'name': str(raw.get('name') or existing['original_name']).strip() or existing['original_name'],
            'original_name': existing['original_name'],
            'enabled': bool(raw.get('enabled', True)),
        })
    return {'pixie_auditoriums': auditoriums, 'pixie_devices': devices, 'pixie_scenes': scenes}


@app.put('/api/pixie/config')
def api_pixie_config_put():
    access_error = _pixie_api_guard(config_only=True, require_csrf=True)
    if access_error:
        return access_error
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'ok': False, 'error': 'invalid JSON payload'}), 400
    cfg = utils.get_config()
    old_cfg = copy.deepcopy(cfg)
    manager = _get_pixie_manager_from_config()
    status = manager.status() if manager is not None else {}
    stored = _load_pixie_secrets(_APP_ROOT) if _load_pixie_secrets is not None else None
    username = str(body.get('pixie_username') or getattr(stored, 'username', '') or '').strip()
    password = str(body.get('pixie_password') or getattr(stored, 'password', '') or '')
    mode = str(body.get('pixie_network_mode') or 'disabled').strip().lower()
    if mode not in ('disabled', 'observe', 'control'):
        return jsonify({'ok': False, 'error': 'Network mode must be disabled, observe, or control'}), 400
    gateway_host = str(body.get('pixie_gateway_host') or '').strip()
    home_id = str(body.get('pixie_home_id') or '').strip()
    try:
        lists = _normalize_pixie_setup_lists(body, cfg, status)
        connection = {
            'pixie_network_mode': mode,
            'pixie_gateway_host': gateway_host,
            'pixie_home_id': home_id,
            'pixie_home_name': str(cfg.get('pixie_home_name') or ''),
            'pixie_net_id': str(cfg.get('pixie_net_id') or ''),
            'pixie_mesh_net': str(cfg.get('pixie_mesh_net') or ''),
            'pixie_mesh_net_2': str(cfg.get('pixie_mesh_net_2') or ''),
        }
        credentials_changed = username != str(getattr(stored, 'username', '') or '') or bool(body.get('pixie_password'))
        home_changed = home_id != str(cfg.get('pixie_home_id') or '')
        needs_provision = mode != 'disabled' and (
            credentials_changed or home_changed or not connection['pixie_net_id'] or not connection['pixie_mesh_net_2']
        )
        if needs_provision:
            if _provision_pixie_cloud is None:
                raise RuntimeError('Pixie support is unavailable')
            provisioning = _provision_pixie_cloud(username, password)
            homes = provisioning.get('homes') or []
            selected_id = home_id or str(provisioning.get('login', {}).get('currentHomeId') or '')
            selected = next((home for home in homes if str(home.get('objectId')) == selected_id), None)
            if selected is None and len(homes) == 1:
                selected = homes[0]
            if selected is None:
                choices = ', '.join(f"{home.get('name')} ({home.get('objectId')})" for home in homes)
                raise ValueError(f"Select a Pixie Home; available Homes: {choices or 'none'}")
            connection.update({
                'pixie_home_id': str(selected.get('objectId') or ''),
                'pixie_home_name': str(selected.get('name') or selected.get('objectId') or ''),
                'pixie_net_id': str(selected.get('netId') or ''),
                'pixie_mesh_net': str(selected.get('meshNet') or ''),
                'pixie_mesh_net_2': str(selected.get('meshNet2') or ''),
            })
            if not connection['pixie_net_id'] or not connection['pixie_mesh_net_2']:
                raise ValueError('The selected Pixie Home did not provide Net ID and Mesh Net 2')

        cfg.update(connection)
        cfg.update(lists)
        if _save_pixie_secrets is None:
            raise RuntimeError('Pixie secrets support is unavailable')
        _save_pixie_secrets(_APP_ROOT, username, password)
        utils.save_config(cfg)
        utils.reload_config(force=True)
        if _close_pixie_manager is not None:
            _close_pixie_manager()
        restarted = _get_pixie_manager_from_config()
        new_status = restarted.status() if restarted is not None else {'available': False, 'lastError': 'Pixie support is unavailable'}
        changed_keys = sorted(key for key in set(old_cfg) | set(cfg) if old_cfg.get(key) != cfg.get(key))
        log_event(
            'pixie.config.update',
            f"Updated Pixie configuration ({len(lists['pixie_auditoriums'])} auditoriums, {len(lists['pixie_devices'])} devices, {len(lists['pixie_scenes'])} scenes)",
            source='web',
            status='success' if not new_status.get('lastError') else 'warning',
            target_type='config',
            target_id='pixie',
            details={
                'changed_keys': changed_keys,
                'network_mode': mode,
                'gateway_host': gateway_host,
                'home_id': connection['pixie_home_id'],
                'auditorium_ids': [item['id'] for item in lists['pixie_auditoriums']],
                'device_count': len(lists['pixie_devices']),
                'scene_count': len(lists['pixie_scenes']),
                'connection_ready': bool(new_status.get('controlReady')),
                'connection_error': str(new_status.get('lastError') or ''),
            },
        )
        return jsonify({'ok': True, 'config': {**connection, **lists}, 'status': new_status})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        log_event(
            'pixie.config.update',
            'Failed to update Pixie configuration',
            source='web',
            status='failure',
            target_type='config',
            target_id='pixie',
            details={'error': str(exc)},
        )
        return jsonify({'ok': False, 'error': str(exc)}), 502


@app.get('/api/pixie/state')
def api_pixie_state():
    access_error = _pixie_api_guard()
    if access_error:
        return access_error
    manager = _get_pixie_manager_from_config()
    if manager is None:
        return jsonify({'ok': False, 'error': 'Pixie support is unavailable'}), 503
    refresh = str(request.args.get('refresh') or '').lower() in ('1', 'true', 'yes')
    status = manager.refresh_inventory() if refresh else manager.status()
    cfg = utils.get_config()
    selected = str(request.args.get('auditorium_id') or '').strip()
    try:
        payload = _pixie_operator_payload(cfg, status, selected)
        response = jsonify(payload)
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response
    except PermissionError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 403


def _pixie_authorized_devices(cfg: dict, status: dict, auditorium_id: str, requested_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    catalog = _pixie_catalog(cfg, status)
    auditorium = next((item for item in catalog['auditoriums'] if item['id'] == auditorium_id), None)
    if auditorium is None:
        raise ValueError('Unknown auditorium')
    permissions = _pixie_current_permissions()
    allow_all = bool(permissions.get('all'))
    auditorium_scopes = permissions.get('auditoriums') if isinstance(permissions.get('auditoriums'), dict) else {}
    if not allow_all and auditorium_id not in auditorium_scopes:
        raise PermissionError('This auditorium is not assigned to your group.')
    scope = None if allow_all else auditorium_scopes.get(auditorium_id)
    allowed_ids = set(auditorium['device_ids'])
    if scope is not None:
        allowed_ids &= set(_coerce_string_allow_list(scope))
    requested = [str(value) for value in requested_ids]
    if not requested:
        raise ValueError('Select at least one device')
    if any(device_id not in allowed_ids for device_id in requested):
        raise PermissionError('One or more selected devices are not assigned to your group in this auditorium.')
    device_by_id = {item['id']: item for item in catalog['devices']}
    devices = [device_by_id[device_id] for device_id in requested if device_id in device_by_id]
    if len(devices) != len(requested):
        raise ValueError('One or more selected devices are unavailable')
    return devices, requested


@app.post('/api/pixie/devices/brightness')
def api_pixie_devices_brightness():
    access_error = _pixie_api_guard(require_csrf=True)
    if access_error:
        return access_error
    body = request.get_json(silent=True) or {}
    auditorium_id = str(body.get('auditorium_id') or '').strip()
    raw_ids = body.get('device_ids')
    try:
        level = int(body.get('level'))
    except Exception:
        return jsonify({'ok': False, 'error': 'level must be a whole number from 0 to 100'}), 400
    if level < 0 or level > 100:
        return jsonify({'ok': False, 'error': 'level must be from 0 to 100'}), 400
    if not auditorium_id or not isinstance(raw_ids, list):
        return jsonify({'ok': False, 'error': 'auditorium_id and device_ids are required'}), 400
    manager = _get_pixie_manager_from_config()
    if manager is None:
        return jsonify({'ok': False, 'error': 'Pixie support is unavailable'}), 503
    status = manager.status()
    cfg = utils.get_config()
    is_final = bool(body.get('final', False))
    try:
        devices, requested = _pixie_authorized_devices(cfg, status, auditorium_id, raw_ids)
        dimmer_ids = []
        on_off_ids = []
        skipped = []
        for device in devices:
            if device.get('online') is False:
                raise ValueError(f"{device.get('name')} is offline")
            resolved_type = device.get('resolved_type')
            if resolved_type == 'unknown':
                raise ValueError(f"{device.get('name')} has an unknown control type")
            if resolved_type == 'on_off' and level not in (0, 100):
                skipped.append(device['id'])
            elif resolved_type == 'on_off':
                on_off_ids.append(device['id'])
            else:
                dimmer_ids.append(device['id'])
        command_results = []
        if dimmer_ids:
            command_results.append(manager.set_brightness(dimmer_ids, level))
        if on_off_ids:
            command_results.append(manager.set_power(on_off_ids, level == 100))
        result = {
            'ok': all(bool(item.get('ok')) for item in command_results),
            'succeeded': [device_id for item in command_results for device_id in item.get('succeeded', [])],
            'failed': [failure for item in command_results for failure in item.get('failed', [])],
            'level': level,
        }
        result['skipped'] = skipped
        result['requested'] = requested
        result['ok'] = not bool(result.get('failed'))
        if is_final:
            auditorium_name = next((item.get('name') for item in cfg.get('pixie_auditoriums', []) if isinstance(item, dict) and item.get('id') == auditorium_id), auditorium_id)
            log_event(
                'pixie.devices.brightness',
                f"Set {len(result.get('succeeded') or [])} Pixie device(s) in {auditorium_name} to {level}%",
                source='web',
                status='success' if result['ok'] else ('warning' if result.get('succeeded') else 'failure'),
                target_type='pixie_auditorium',
                target_id=auditorium_id,
                details={
                    'level': level,
                    'requested_device_ids': requested,
                    'succeeded_device_ids': result.get('succeeded') or [],
                    'skipped_on_off_device_ids': skipped,
                    'failed': result.get('failed') or [],
                },
            )
        return jsonify(result), (200 if result['ok'] or result.get('succeeded') else 502)
    except PermissionError as exc:
        log_event(
            'pixie.permission.denied',
            'Denied a Pixie device control request',
            source='web',
            status='failure',
            target_type='pixie_auditorium',
            target_id=auditorium_id,
            details={'device_ids': [str(value) for value in raw_ids], 'error': str(exc)},
        )
        return jsonify({'ok': False, 'error': str(exc)}), 403
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        if is_final:
            log_event(
                'pixie.devices.brightness',
                'Failed to update Pixie devices',
                source='web',
                status='failure',
                target_type='pixie_auditorium',
                target_id=auditorium_id,
                details={'level': level, 'device_ids': [str(value) for value in raw_ids], 'error': str(exc)},
            )
        return jsonify({'ok': False, 'error': str(exc)}), 502


@app.post('/api/pixie/scenes/<scene_id>/activate')
def api_pixie_scene_activate(scene_id: str):
    access_error = _pixie_api_guard(require_csrf=True)
    if access_error:
        return access_error
    manager = _get_pixie_manager_from_config()
    if manager is None:
        return jsonify({'ok': False, 'error': 'Pixie support is unavailable'}), 503
    cfg = utils.get_config()
    status = manager.status()
    catalog = _pixie_catalog(cfg, status)
    scene = next((item for item in catalog['scenes'] if item['id'] == str(scene_id) and item.get('enabled', True)), None)
    permissions = _pixie_current_permissions()
    if scene is None:
        return jsonify({'ok': False, 'error': 'Unknown or disabled scene'}), 404
    if not permissions.get('all') and str(scene_id) not in set(_coerce_string_allow_list(permissions.get('scenes'))):
        log_event(
            'pixie.permission.denied',
            'Denied a Pixie scene activation request',
            source='web',
            status='failure',
            target_type='pixie_scene',
            target_id=scene_id,
            details={'scene_id': scene_id},
        )
        return jsonify({'ok': False, 'error': 'This scene is not assigned to your group.'}), 403
    try:
        result = manager.activate_scene(str(scene_id))
        log_event(
            'pixie.scene.activate',
            f"Activated Pixie scene {scene['name']}",
            source='web',
            status='success',
            target_type='pixie_scene',
            target_id=scene_id,
            details={'scene_id': scene_id, 'scene_name': scene['name']},
        )
        return jsonify(result)
    except Exception as exc:
        log_event(
            'pixie.scene.activate',
            f"Failed to activate Pixie scene {scene['name']}",
            source='web',
            status='failure',
            target_type='pixie_scene',
            target_id=scene_id,
            details={'scene_id': scene_id, 'scene_name': scene['name'], 'error': str(exc)},
        )
        return jsonify({'ok': False, 'error': str(exc)}), 502


def _filter_atem_audio_payload_for_current_principal(payload: dict, *, meters: bool = False) -> dict:
    if not _auth_enabled() or _api_request_is_automation_principal():
        return payload
    try:
        user_id = int(current_user.get_id())
        if _user_is_admin(user_id):
            return payload
        allowed = set(_effective_atem_audio_source_ids_for_user(user_id))
    except Exception:
        allowed = set()
    result = dict(payload or {})
    if meters:
        sources = result.get('sources') if isinstance(result.get('sources'), dict) else {}
        result['sources'] = {str(key): value for key, value in sources.items() if str(key) in allowed}
        if 'master' not in allowed:
            result['master'] = {}
    else:
        sources = result.get('sources') if isinstance(result.get('sources'), list) else []
        result['sources'] = [item for item in sources if isinstance(item, dict) and str(item.get('id')) in allowed]
        try:
            if not _effective_atem_can_monitor_audio_for_user(user_id):
                result.pop('monitor', None)
        except Exception:
            result.pop('monitor', None)
    return result


@app.route('/api/atem/audio/state')
def api_atem_audio_state():
    atem = _get_atem_client_from_config()
    if atem is None:
        try:
            fallback = AtemAudioClient.fallback_sources() if AtemAudioClient is not None else [{'id': 'master', 'label': 'Master', 'kind': 'master'}]
        except Exception:
            fallback = [{'id': 'master', 'label': 'Master', 'kind': 'master'}]
        payload = {
            'ok': False,
            'connected': False,
            'error': 'ATEM not configured',
            'sources': fallback,
            'stale': True,
            'refreshing': False,
            'sampledAt': None,
            'ageMs': None,
        }
        return jsonify(_filter_atem_audio_payload_for_current_principal(payload)), 200
    try:
        force = str(request.args.get('refresh') or '').strip().lower() in ('1', 'true', 'yes')
        if hasattr(atem, 'get_audio_state_snapshot'):
            state = atem.get_audio_state_snapshot(force_refresh=force)
        else:
            # Backward compatibility for third-party/fake clients.
            state = atem.get_audio_state()
        state['ok'] = bool(state.get('connected', True))
        return jsonify(_filter_atem_audio_payload_for_current_principal(state))
    except Exception as e:
        try:
            fallback = AtemAudioClient.fallback_sources() if AtemAudioClient is not None else [{'id': 'master', 'label': 'Master', 'kind': 'master'}]
        except Exception:
            fallback = [{'id': 'master', 'label': 'Master', 'kind': 'master'}]
        payload = {
            'ok': False,
            'connected': False,
            'error': str(e),
            'sources': fallback,
            'stale': True,
            'refreshing': False,
            'sampledAt': None,
            'ageMs': None,
        }
        return jsonify(_filter_atem_audio_payload_for_current_principal(payload)), 200


@app.route('/api/atem/audio/meters')
def api_atem_audio_meters():
    """Return compact levels from the singleton UDP meter receiver."""
    atem = _get_atem_client_from_config()
    if atem is None:
        return jsonify({
            'ok': False,
            'connected': False,
            'error': 'ATEM not configured',
            'master': {},
            'sources': {},
            'metering': {'enabled': False, 'connected': False, 'active': False},
            'sampledAt': None,
            'ageMs': None,
            'stale': True,
        }), 200
    try:
        if hasattr(atem, 'get_meter_snapshot'):
            return jsonify(_filter_atem_audio_payload_for_current_principal(atem.get_meter_snapshot(), meters=True))
        state = atem.get_audio_state()
        sources = state.get('sources') if isinstance(state, dict) else []
        payload = {
            'ok': bool(state.get('connected', False)),
            'connected': bool(state.get('connected', False)),
            'master': next((item.get('level') for item in sources if str(item.get('id')) == 'master'), {}),
            'sources': {
                str(item.get('id')): item.get('level') or {}
                for item in sources if isinstance(item, dict) and str(item.get('id')) != 'master'
            },
            'metering': state.get('metering') or {},
            'sampledAt': None,
            'ageMs': None,
            'stale': False,
        }
        return jsonify(_filter_atem_audio_payload_for_current_principal(payload, meters=True))
    except Exception as exc:
        payload = {
            'ok': False,
            'connected': False,
            'error': str(exc),
            'master': {},
            'sources': {},
            'metering': {'enabled': False, 'connected': False, 'active': False},
            'sampledAt': None,
            'ageMs': None,
            'stale': True,
        }
        return jsonify(_filter_atem_audio_payload_for_current_principal(payload, meters=True)), 200


@app.route('/api/atem/audio/volume', methods=['POST'])
def api_atem_audio_volume():
    atem = _get_atem_client_from_config()
    if atem is None:
        return jsonify({'ok': False, 'error': 'ATEM not configured'}), 400
    body = request.get_json(silent=True) or {}
    source_id = str(body.get('source_id') or body.get('source') or '').strip()
    try:
        db = float(body.get('db'))
    except Exception:
        return jsonify({'ok': False, 'error': 'db is required'}), 400
    if not source_id:
        return jsonify({'ok': False, 'error': 'source_id is required'}), 400
    try:
        atem.set_volume(source_id, db)
        log_event(
            'atem.audio.volume',
            f"Set ATEM audio volume for {source_id} to {db:.1f} dB",
            source='web',
            status='success',
            target_type='atem_audio_source',
            target_id=source_id,
            details={'source_id': source_id, 'db': db},
        )
        return jsonify({'ok': True, 'source_id': source_id, 'db': db})
    except Exception as e:
        log_event(
            'atem.audio.volume',
            f"Failed to set ATEM audio volume for {source_id}",
            source='web',
            status='failure',
            target_type='atem_audio_source',
            target_id=source_id,
            details={'source_id': source_id, 'db': db, 'error': str(e)},
        )
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/atem/audio/mute', methods=['POST'])
def api_atem_audio_mute():
    atem = _get_atem_client_from_config()
    if atem is None:
        return jsonify({'ok': False, 'error': 'ATEM not configured'}), 400
    body = request.get_json(silent=True) or {}
    source_id = str(body.get('source_id') or body.get('source') or '').strip()
    mix_option = str(body.get('mix_option') or body.get('mixOption') or '').strip().lower()
    muted = bool(body.get('muted'))
    if not source_id:
        return jsonify({'ok': False, 'error': 'source_id is required'}), 400
    try:
        if mix_option:
            atem.set_mix_option(source_id, mix_option)
            muted = mix_option == 'off'
        else:
            atem.set_mute(source_id, muted)
        log_event(
            'atem.audio.mute',
            f"Set ATEM audio source {source_id} to {mix_option or ('off' if muted else 'on')}",
            source='web',
            status='success',
            target_type='atem_audio_source',
            target_id=source_id,
            details={'source_id': source_id, 'muted': muted, 'mix_option': mix_option or ('off' if muted else 'on')},
        )
        return jsonify({'ok': True, 'source_id': source_id, 'muted': muted, 'mix_option': mix_option or ('off' if muted else 'on')})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/atem/audio/solo', methods=['POST'])
def api_atem_audio_solo():
    atem = _get_atem_client_from_config()
    if atem is None:
        return jsonify({'ok': False, 'error': 'ATEM not configured'}), 400
    body = request.get_json(silent=True) or {}
    source_id = str(body.get('source_id') or body.get('source') or '').strip()
    enabled = bool(body.get('enabled'))
    if not source_id:
        return jsonify({'ok': False, 'error': 'source_id is required'}), 400
    try:
        atem.set_solo(source_id, enabled)
        log_event(
            'atem.audio.solo',
            f"{'Enabled' if enabled else 'Disabled'} ATEM monitor solo for {source_id}",
            source='web',
            status='success',
            target_type='atem_audio_source',
            target_id=source_id,
            details={'source_id': source_id, 'enabled': enabled},
        )
        return jsonify({'ok': True, 'source_id': source_id, 'enabled': enabled})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/atem/audio/monitor', methods=['POST'])
def api_atem_audio_monitor():
    if _auth_enabled() and not _api_request_is_automation_principal():
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        try:
            if not _effective_atem_can_monitor_audio_for_user(int(current_user.get_id())):
                return jsonify({'ok': False, 'error': 'forbidden'}), 403
        except Exception:
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
    atem = _get_atem_client_from_config()
    if atem is None:
        return jsonify({'ok': False, 'error': 'ATEM not configured'}), 400
    body = request.get_json(silent=True) or {}
    changes: dict[str, Any] = {}
    if 'enabled' in body:
        changes['enabled'] = bool(body.get('enabled'))
    if 'dim' in body:
        changes['dim'] = bool(body.get('dim'))
    if 'volume' in body:
        try:
            changes['volume'] = float(body.get('volume'))
        except Exception:
            return jsonify({'ok': False, 'error': 'volume must be a number'}), 400
    if not changes:
        return jsonify({'ok': False, 'error': 'No monitor changes supplied'}), 400
    try:
        atem.set_monitor(
            enabled=changes.get('enabled') if 'enabled' in changes else None,
            dim=changes.get('dim') if 'dim' in changes else None,
            volume=changes.get('volume') if 'volume' in changes else None,
        )
        log_event(
            'atem.audio.monitor',
            'Updated ATEM monitor controls',
            source='web',
            status='success',
            target_type='atem_audio_monitor',
            target_id='monitor',
            details=changes,
        )
        return jsonify({'ok': True, 'monitor': changes})
    except Exception as e:
        log_event(
            'atem.audio.monitor',
            'Failed to update ATEM monitor controls',
            source='web',
            status='failure',
            target_type='atem_audio_monitor',
            target_id='monitor',
            details={**changes, 'error': str(e)},
        )
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/config', methods=['GET'])
def api_get_config():
    if _auth_enabled() and not _api_request_is_automation_principal():
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        if not can_access('page:config'):
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
    try:
        cfg = utils.get_config()
        # Legacy: global Routing allow-lists are no longer used (now per Access Level).
        try:
            cfg.pop('videohub_allowed_outputs', None)
            cfg.pop('videohub_allowed_inputs', None)
        except Exception:
            pass
        return jsonify(cfg)
    except Exception:
        return jsonify({})


@app.route('/api/config', methods=['POST'])
@_serialize_media_configuration
def api_set_config():
    if _auth_enabled() and not _api_request_is_automation_principal():
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        if not can_access('page:config'):
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
    try:
        new = request.get_json() or {}
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid json'}), 400

    try:
        cfg = utils.get_config()
        old_cfg = copy.deepcopy(cfg)

        # Compute port change before writing so we can tell the UI what's happening.
        try:
            old_port = int(cfg.get('webserver_port', cfg.get('server_port', 5000)))
        except Exception:
            old_port = 5000

        # merge provided values
        # Media setup is saved through its validated browser-only editor. The
        # general Config form includes an older full snapshot of hidden keys.
        cfg.update({key: value for key, value in new.items() if key not in _MEDIA_CONFIG_DEFAULTS})

        # Legacy: global Routing allow-lists are no longer used (now per Access Level).
        cfg.pop('videohub_allowed_outputs', None)
        cfg.pop('videohub_allowed_inputs', None)

        # persist
        utils.save_config(cfg)
        utils.reload_config(force=True)
        try:
            changed_keys = sorted([str(k) for k in set(old_cfg.keys()) | set(cfg.keys()) if old_cfg.get(k) != cfg.get(k)])
            log_event(
                'config.update',
                f"Updated config ({len(changed_keys)} setting{'s' if len(changed_keys) != 1 else ''})",
                source='web',
                status='success',
                target_type='config',
                target_id='config.json',
                details={'changed_keys': changed_keys},
            )
        except Exception:
            pass
        # re-apply logging configuration in case `debug` was changed
        try:
            _apply_logging_config()
        except Exception:
            pass

        # IMPORTANT:
        # Do NOT restart the server from inside this request handler.
        # The werkzeug `make_server` instance is single-threaded; calling
        # shutdown/restart inline can deadlock and make the UI appear to hang.
        # Port changes are handled by the background config watcher thread.
        try:
            new_port = int(cfg.get('webserver_port', cfg.get('server_port', 5000)))
        except Exception:
            new_port = old_port

        restart_required = bool(new_port != old_port)

        # If the port changed, restart the HTTP server asynchronously.
        # We deliberately do NOT restart inline in this request handler.
        if restart_required:
            def _restart_later(port: int):
                try:
                    # Give the response time to flush before restarting.
                    time.sleep(0.75)
                except Exception:
                    pass
                try:
                    log_event(
                        'web.port.restart',
                        f'Port changed; restarting server on port {port}',
                        source='system',
                        status='info',
                        target_type='webserver',
                        target_id=str(port),
                        details={'port': port},
                    )
                except Exception:
                    pass
                try:
                    restart_http_server('0.0.0.0', port)
                except Exception:
                    pass

            try:
                threading.Thread(target=_restart_later, args=(new_port,), daemon=True).start()
            except Exception:
                pass

        return jsonify({'ok': True, 'config': cfg, 'restart_required': restart_required, 'port': new_port})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _service_token_web_actor() -> str:
    username = str(getattr(current_user, 'username', '') or '').strip()
    if username:
        return username
    try:
        return f"User {int(current_user.get_id())}"
    except Exception:
        return 'TDeck administrator'


def _service_token_create_payload(body: Any) -> dict[str, Any]:
    payload = body if isinstance(body, dict) else {}
    name = str(payload.get('name') or '').strip()
    description = str(payload.get('description') or '').strip()
    if not name:
        raise ValueError('Token name is required.')
    if len(name) > 120:
        raise ValueError('Token name must be 120 characters or fewer.')
    if len(description) > 500:
        raise ValueError('Description must be 500 characters or fewer.')
    scopes = payload.get('scopes')
    if not isinstance(scopes, list):
        raise ValueError('Choose at least one token scope.')
    try:
        expires_in_days = int(payload.get('expires_in_days') or 0)
    except (TypeError, ValueError):
        raise ValueError('Expiry must be a whole number of days.') from None
    if expires_in_days < 1 or expires_in_days > 3650:
        raise ValueError('Expiry must be between 1 and 3650 days.')
    constraints = payload.get('constraints') or {}
    if not isinstance(constraints, dict):
        raise ValueError('Token constraints must be an object.')
    return {
        'name': name,
        'description': description,
        'scopes': scopes,
        'expires_in_days': expires_in_days,
        'constraints': constraints,
    }


@app.get('/api/config/service-tokens')
def api_config_service_tokens_list():
    return jsonify({
        'ok': True,
        'tokens': list_service_tokens(_AUTH_DB_PATH),
        'available_scopes': list(SERVICE_TOKEN_SCOPES),
    })


@app.post('/api/config/service-tokens')
def api_config_service_tokens_create():
    try:
        values = _service_token_create_payload(request.get_json(silent=True))
        record = create_service_token(
            _AUTH_DB_PATH,
            created_by=_service_token_web_actor(),
            **values,
        )
    except (TypeError, ValueError) as exc:
        return _api_json_error(400, 'invalid_request', str(exc))
    log_event(
        'security.service_token.create',
        f"Created service token '{record['name']}'",
        source='web',
        status='success',
        target_type='service_token',
        target_id=record['id'],
        details={
            'name': record['name'],
            'prefix': record['token_prefix'],
            'scopes': record['scopes'],
            'constraints': record.get('constraints') or {},
            'expires_at': record.get('expires_at'),
        },
    )
    return jsonify({'ok': True, 'token': record}), 201


@app.post('/api/config/service-tokens/<int:token_id>/rotate')
def api_config_service_tokens_rotate(token_id: int):
    body = request.get_json(silent=True)
    payload = body if isinstance(body, dict) else {}
    name = str(payload.get('name') or '').strip() or None
    if name and len(name) > 120:
        return _api_json_error(400, 'invalid_request', 'Token name must be 120 characters or fewer.')
    try:
        expires_in_days = int(payload.get('expires_in_days') or 0)
    except (TypeError, ValueError):
        return _api_json_error(400, 'invalid_request', 'Expiry must be a whole number of days.')
    if expires_in_days < 1 or expires_in_days > 3650:
        return _api_json_error(400, 'invalid_request', 'Expiry must be between 1 and 3650 days.')
    try:
        rotation = rotate_service_token(
            _AUTH_DB_PATH,
            token_id,
            name=name,
            created_by=_service_token_web_actor(),
            expires_in_days=expires_in_days,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        return _api_json_error(409, 'rotation_failed', str(exc))
    if rotation is None:
        return _api_json_error(404, 'not_found', 'Service token not found.')
    previous = rotation['previous']
    replacement = rotation['replacement']
    log_event(
        'security.service_token.rotate',
        f"Rotated service token '{previous['name']}'",
        source='web',
        status='success',
        target_type='service_token',
        target_id=replacement['id'],
        details={
            'old_id': previous['id'],
            'old_prefix': previous['token_prefix'],
            'new_id': replacement['id'],
            'new_prefix': replacement['token_prefix'],
            'scopes': replacement['scopes'],
            'expires_at': replacement.get('expires_at'),
        },
    )
    return jsonify({'ok': True, 'previous': previous, 'token': replacement})


@app.delete('/api/config/service-tokens/<int:token_id>')
def api_config_service_tokens_revoke(token_id: int):
    try:
        record = revoke_service_token(
            _AUTH_DB_PATH,
            token_id,
            revoked_by=_service_token_web_actor(),
        )
    except ValueError as exc:
        return _api_json_error(400, 'invalid_request', str(exc))
    if record is None:
        return _api_json_error(404, 'not_found', 'Service token not found.')
    log_event(
        'security.service_token.revoke',
        f"Revoked service token '{record['name']}'",
        source='web',
        status='success',
        target_type='service_token',
        target_id=record['id'],
        details={'name': record['name'], 'prefix': record['token_prefix']},
    )
    return jsonify({'ok': True, 'token': record})


@app.get('/api/config/export-items')
def api_config_export_items():
    access_error = _config_access_error_json()
    if access_error:
        return access_error
    return jsonify({'ok': True, 'items': _config_transport_items()})


@app.post('/api/config/import/inspect')
def api_config_import_inspect():
    access_error = _config_access_error_json()
    if access_error:
        return access_error

    upload = request.files.get('file')
    if upload is None or not str(upload.filename or '').strip():
        return jsonify({'ok': False, 'error': 'Upload a TDeck config export zip.'}), 400
    filename = secure_filename(upload.filename or 'config.zip')
    if not filename.lower().endswith('.zip'):
        return jsonify({'ok': False, 'error': 'Config imports must be .zip files exported from TDeck.'}), 400

    try:
        with NamedTemporaryFile(prefix='tdeck-config-import-', suffix='.zip', delete=False) as tmp:
            temp_path = Path(tmp.name)
            upload.save(tmp)
        manifest = _inspect_config_transport_zip(temp_path)
        token = _remember_config_import_upload(temp_path, manifest)
        _config_transport_log(f"Inspected config import {filename}: {len(manifest.get('items') or [])} item(s)")
        return jsonify({'ok': True, 'token': token, 'manifest': manifest})
    except Exception as e:
        try:
            temp_path.unlink(missing_ok=True)  # type: ignore[name-defined]
        except Exception:
            pass
        _config_transport_log(f"Import inspect failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.post('/api/config/import/apply')
def api_config_import_apply():
    access_error = _config_access_error_json()
    if access_error:
        return access_error

    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    token = str(body.get('token') or '').strip()
    selected = body.get('items') or []
    if not token:
        return jsonify({'ok': False, 'error': 'Import upload is missing. Upload the export zip again.'}), 400
    if not isinstance(selected, list):
        return jsonify({'ok': False, 'error': 'Invalid import selection.'}), 400
    selected_ids = [str(v) for v in selected if str(v or '').strip()]

    with _CONFIG_IMPORT_UPLOAD_LOCK:
        entry = _CONFIG_IMPORT_UPLOADS.pop(token, None)
    if not entry:
        return jsonify({'ok': False, 'error': 'Import upload expired. Upload the export zip again.'}), 400

    zip_path = Path(str(entry.get('path') or ''))
    try:
        imported, backup_path = _apply_config_transport_import(zip_path, selected_ids)
        _config_transport_log(
            f"Imported config items: {', '.join(str(i.get('path')) for i in imported)}"
            + (f"; backup={backup_path}" if backup_path else '')
        )
        try:
            _audit('config_import', f"items={len(imported)} backup={backup_path or ''}")
        except Exception:
            pass
        return jsonify({
            'ok': True,
            'imported': imported,
            'backup_path': str(backup_path) if backup_path else '',
        })
    except Exception as e:
        _config_transport_log(f"Import failed: {e}")
        try:
            _audit('config_import_fail', str(e))
        except Exception:
            pass
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        try:
            zip_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.route('/api/companion-surfaces-config', methods=['GET'])
def api_get_companion_surfaces_config():
    if _auth_enabled() and not _api_request_is_automation_principal():
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        if not can_access('page:config'):
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
    payload = _companion_surface_config_payload()
    resp = jsonify(payload)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/api/companion-surfaces-config', methods=['POST'])
def api_set_companion_surfaces_config():
    if _auth_enabled() and not _api_request_is_automation_principal():
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        if not can_access('page:config'):
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}
    payload, err = _normalize_companion_surface_config_payload(data)
    if err or payload is None:
        return jsonify({'ok': False, 'error': err or 'Invalid surface config.'}), 400
    try:
        if not _save_companion_surface_config(payload):
            return jsonify({'ok': False, 'error': 'Could not save companion_surfaces.json'}), 500
        _audit('companion_surfaces_config_update', f"surfaces={len(payload.get('surfaces') or [])} displays={len(payload.get('surface_controls') or [])}")
        return jsonify({'ok': True, 'config': payload})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# --- VideoHub presets API ---

def _get_videohub_app():
    try:
        return get_app('videohub')
    except Exception:
        return None


@app.route('/api/videohub/presets', methods=['GET'])
def api_videohub_presets_list():
    app_inst = _get_videohub_app()
    if app_inst is None or not hasattr(app_inst, 'list_presets'):
        return jsonify({'ok': False, 'error': 'VideoHub backend not available'}), 500
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        presets = app_inst.list_presets(cfg)  # type: ignore[attr-defined]
        if _auth_enabled() and not _api_request_is_automation_principal():
            try:
                allowed_ids = set(_effective_videohub_preset_ids_for_user(int(current_user.get_id())))
            except Exception:
                allowed_ids = set()
            if allowed_ids:
                presets = [
                    preset for preset in presets
                    if int((preset.get('id') if isinstance(preset, dict) else getattr(preset, 'id', 0)) or 0) in allowed_ids
                ]
        return jsonify({'ok': True, 'presets': presets})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/videohub/presets', methods=['POST'])
def api_videohub_presets_create():
    app_inst = _get_videohub_app()
    if app_inst is None or not hasattr(app_inst, 'upsert_preset'):
        return jsonify({'ok': False, 'error': 'VideoHub backend not available'}), 500
    try:
        body = request.get_json() or {}
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid json'}), 400
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        preset = app_inst.upsert_preset(cfg, body)  # type: ignore[attr-defined]
        preset_dict = preset.to_dict() if hasattr(preset, 'to_dict') else preset
        log_event(
            'videohub.preset.create',
            f"Created VideoHub preset #{preset_dict.get('id') if isinstance(preset_dict, dict) else ''}".strip(),
            source='web',
            status='success',
            target_type='videohub_preset',
            target_id=preset_dict.get('id') if isinstance(preset_dict, dict) else None,
            details={'preset': preset_dict},
        )
        return jsonify({'ok': True, 'preset': preset_dict})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/videohub/presets/<int:preset_id>', methods=['PUT'])
def api_videohub_presets_update(preset_id: int):
    app_inst = _get_videohub_app()
    if app_inst is None or not hasattr(app_inst, 'upsert_preset'):
        return jsonify({'ok': False, 'error': 'VideoHub backend not available'}), 500
    try:
        body = request.get_json() or {}
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid json'}), 400
    body = dict(body)
    body['id'] = preset_id
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        preset = app_inst.upsert_preset(cfg, body)  # type: ignore[attr-defined]
        preset_dict = preset.to_dict() if hasattr(preset, 'to_dict') else preset
        log_event(
            'videohub.preset.update',
            f"Updated VideoHub preset #{preset_id}",
            source='web',
            status='success',
            target_type='videohub_preset',
            target_id=preset_id,
            details={'preset': preset_dict},
        )
        return jsonify({'ok': True, 'preset': preset_dict})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/videohub/presets/<int:preset_id>', methods=['DELETE'])
def api_videohub_presets_delete(preset_id: int):
    app_inst = _get_videohub_app()
    if app_inst is None or not hasattr(app_inst, 'delete_preset'):
        return jsonify({'ok': False, 'error': 'VideoHub backend not available'}), 500
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        ok = bool(app_inst.delete_preset(cfg, preset_id))  # type: ignore[attr-defined]
        if not ok:
            return jsonify({'ok': False, 'error': 'preset not found'}), 404
        log_event(
            'videohub.preset.delete',
            f"Deleted VideoHub preset #{preset_id}",
            source='web',
            status='success',
            target_type='videohub_preset',
            target_id=preset_id,
        )
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/videohub/presets/<int:preset_id>/lock', methods=['POST'])
def api_videohub_presets_lock(preset_id: int):
    """Lock/unlock a preset to prevent accidental edits."""

    app_inst = _get_videohub_app()
    if app_inst is None or not hasattr(app_inst, 'set_preset_locked'):
        return jsonify({'ok': False, 'error': 'VideoHub backend not available'}), 500

    try:
        body = request.get_json() or {}
    except Exception:
        body = {}

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    locked = body.get('locked', None)
    if locked is None:
        # Toggle if caller didn't specify.
        try:
            cur = app_inst.get_preset(cfg, preset_id)  # type: ignore[attr-defined]
            locked = not bool(getattr(cur, 'locked', False)) if cur is not None else True
        except Exception:
            locked = True

    try:
        updated = app_inst.set_preset_locked(cfg, preset_id, bool(locked))  # type: ignore[attr-defined]
        updated_dict = updated.to_dict() if hasattr(updated, 'to_dict') else updated
        log_event(
            'videohub.preset.lock',
            f"{'Locked' if bool(locked) else 'Unlocked'} VideoHub preset #{preset_id}",
            source='web',
            status='success',
            target_type='videohub_preset',
            target_id=preset_id,
            details={'locked': bool(locked), 'preset': updated_dict},
        )
        return jsonify({'ok': True, 'preset': updated_dict})
    except KeyError:
        return jsonify({'ok': False, 'error': 'preset not found'}), 404
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/videohub/presets/<int:preset_id>/apply', methods=['POST'])
def api_videohub_presets_apply(preset_id: int):
    app_inst = _get_videohub_app()
    if app_inst is None or not hasattr(app_inst, 'apply_preset'):
        try:
            log_event(
                'videohub.preset.apply',
                f'Failed to apply VideoHub preset #{preset_id}: backend unavailable',
                source='api',
                status='failure',
                target_type='videohub_preset',
                target_id=preset_id,
                details={'preset_id': preset_id, 'error': 'VideoHub backend not available'},
            )
        except Exception:
            pass
        return jsonify({'ok': False, 'error': 'VideoHub backend not available'}), 500
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        result = app_inst.apply_preset(cfg, preset_id)  # type: ignore[attr-defined]
        _invalidate_videohub_state_snapshot()
        try:
            _home_set_last_videohub_preset(preset_id=preset_id)
        except Exception:
            pass
        try:
            log_event(
                'videohub.preset.apply',
                f'Applied VideoHub preset #{preset_id}',
                source='api',
                status='success',
                target_type='videohub_preset',
                target_id=preset_id,
                details={'preset_id': preset_id, 'result': result},
            )
        except Exception:
            pass
        return jsonify({'ok': True, 'result': result})
    except KeyError:
        try:
            log_event(
                'videohub.preset.apply',
                f'Failed to apply VideoHub preset #{preset_id}: preset not found',
                source='api',
                status='failure',
                target_type='videohub_preset',
                target_id=preset_id,
                details={'preset_id': preset_id, 'error': 'preset not found'},
            )
        except Exception:
            pass
        return jsonify({'ok': False, 'error': 'preset not found'}), 404
    except Exception as e:
        try:
            log_event(
                'videohub.preset.apply',
                f'Failed to apply VideoHub preset #{preset_id}',
                source='api',
                status='failure',
                target_type='videohub_preset',
                target_id=preset_id,
                details={'preset_id': preset_id, 'error': str(e)},
            )
        except Exception:
            pass
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/videohub/presets/from_device', methods=['POST'])
def api_videohub_presets_from_device():
    """Pull current routing from the configured VideoHub and save as a preset."""

    app_inst = _get_videohub_app()
    if app_inst is None or not hasattr(app_inst, 'upsert_preset'):
        return jsonify({'ok': False, 'error': 'VideoHub backend not available'}), 500

    try:
        body = request.get_json() or {}
    except Exception:
        body = {}

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    vh = _get_videohub_client_from_config()
    if vh is None:
        return jsonify({'ok': False, 'error': 'VideoHub not configured (set videohub_ip)'}), 400

    name = str(body.get('name') or '').strip()
    if not name:
        name = f"Snapshot {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    target_id = None
    try:
        raw_id = body.get('id', None)
        if isinstance(raw_id, int) and raw_id > 0:
            target_id = int(raw_id)
    except Exception:
        target_id = None

    try:
        st = vh.get_state(fallback_count=40) if hasattr(vh, 'get_state') else None
        routing = (st or {}).get('routing') if isinstance(st, dict) else None
        if not isinstance(routing, list) or not routing:
            # fallback identity
            routing = [i for i in range(1, 41)]

        routes = []
        for out_n, in_n in enumerate(routing, start=1):
            try:
                inp = int(in_n)
            except Exception:
                inp = 0
            if inp <= 0:
                continue
            routes.append({'output': out_n, 'input': inp})

        payload = {'name': name, 'routes': routes}
        if target_id is not None:
            payload['id'] = target_id

        preset = app_inst.upsert_preset(cfg, payload)  # type: ignore[attr-defined]
        try:
            saved_id = getattr(preset, 'id', None)
            log_event(
                'videohub.preset.snapshot',
                f"Saved VideoHub snapshot as preset #{saved_id or '?'}",
                source='api',
                status='success',
                target_type='videohub_preset',
                target_id=saved_id,
                details={'preset_id': saved_id, 'name': name, 'route_count': len(routes)},
            )
        except Exception:
            pass

        return jsonify({'ok': True, 'preset': preset.to_dict() if hasattr(preset, 'to_dict') else preset})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


def _videohub_state_fallback(*, configured: bool, refreshing: bool = False) -> dict[str, Any]:
    fallback_count = 40
    nums = [{"number": i, "label": ""} for i in range(1, fallback_count + 1)]
    return {
        'ok': True,
        'configured': bool(configured),
        'refreshing': bool(refreshing),
        'inputs': nums,
        'outputs': nums,
        'routing': [i for i in range(1, fallback_count + 1)],
    }


def _refresh_videohub_state_cache() -> None:
    global _videohub_state_refreshing
    fallback_count = 40
    payload: dict[str, Any] | None = None
    error: Exception | None = None
    try:
        vh = _get_videohub_client_from_config()
        if vh is None:
            payload = _videohub_state_fallback(configured=False)
        else:
            if hasattr(vh, 'get_state'):
                state = vh.get_state(fallback_count=fallback_count)
                inputs = state.get('inputs') or []
                outputs = state.get('outputs') or []
                routing = state.get('routing') or []
            else:
                labels = vh.get_labels(fallback_count=fallback_count)
                inputs = labels.get('inputs', [])
                outputs = labels.get('outputs', [])
                count = max(fallback_count, len(inputs), len(outputs))
                routing = [i for i in range(1, count + 1)]
            payload = {
                'ok': True,
                'configured': True,
                'inputs': inputs,
                'outputs': outputs,
                'routing': routing,
            }
    except Exception as exc:
        error = exc
    finally:
        now = time.time()
        with _status_cache_lock:
            if error is None and isinstance(payload, dict):
                _videohub_state_cache['ts'] = now
                _videohub_state_cache['payload'] = payload
                _videohub_state_cache['last_error'] = None
                _videohub_state_cache['failures'] = 0
                _videohub_state_cache['retry_after'] = 0.0
                _videohub_state_cache['invalidated'] = False
            else:
                failures = int(_videohub_state_cache.get('failures', 0) or 0) + 1
                delay = min(
                    _VIDEOHUB_STATE_RETRY_MAX_SECONDS,
                    _VIDEOHUB_STATE_RETRY_BASE_SECONDS * (2 ** max(0, failures - 1)),
                )
                _videohub_state_cache['last_error'] = str(error or 'VideoHub refresh failed')
                _videohub_state_cache['failures'] = failures
                _videohub_state_cache['retry_after'] = now + delay
        with _videohub_state_refresh_lock:
            _videohub_state_refreshing = False


def _start_videohub_state_refresh() -> bool:
    global _videohub_state_refreshing
    with _videohub_state_refresh_lock:
        if _videohub_state_refreshing:
            return False
        with _status_cache_lock:
            retry_after = float(_videohub_state_cache.get('retry_after', 0.0) or 0.0)
        if time.time() < retry_after:
            return False
        _videohub_state_refreshing = True
    threading.Thread(
        target=_refresh_videohub_state_cache,
        name='tdeck-videohub-state-refresh',
        daemon=True,
    ).start()
    return True


def _get_videohub_state_snapshot(*, force: bool = False) -> dict[str, Any]:
    """Return shared VideoHub state immediately and refresh it off-request."""
    now = time.time()
    with _status_cache_lock:
        cached = _videohub_state_cache.get('payload')
        sampled_at = float(_videohub_state_cache.get('ts', 0.0) or 0.0)
        age = max(0.0, now - sampled_at) if isinstance(cached, dict) and sampled_at else None
        last_error = _videohub_state_cache.get('last_error')
        failures = int(_videohub_state_cache.get('failures', 0) or 0)
        invalidated = bool(_videohub_state_cache.get('invalidated', False))
        fresh = (
            isinstance(cached, dict)
            and age is not None
            and age < _VIDEOHUB_STATE_CACHE_TTL_SECONDS
            and not invalidated
        )

    if force or not fresh:
        _start_videohub_state_refresh()
    with _videohub_state_refresh_lock:
        refreshing = bool(_videohub_state_refreshing)

    metadata = {
        'stale': not fresh,
        'refreshing': refreshing,
        'sampledAt': sampled_at or None,
        'ageMs': int(round(age * 1000.0)) if age is not None else None,
        'lastError': last_error,
        'consecutiveFailures': failures,
    }
    if isinstance(cached, dict):
        result = {**cached, **metadata}
        if last_error:
            result['error'] = str(last_error)
        return result

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
        configured = bool(str(cfg.get('videohub_ip') or cfg.get('videohub_host') or '').strip())
    except Exception:
        configured = False
    result = {**_videohub_state_fallback(configured=configured, refreshing=refreshing), **metadata}
    if last_error:
        result['error'] = str(last_error)
    return result


def _invalidate_videohub_state_snapshot(*, output_idx: int | None = None, input_idx: int | None = None) -> None:
    """Mark routing stale after a command and retain any safely-known route."""
    with _status_cache_lock:
        cached = _videohub_state_cache.get('payload')
        if isinstance(cached, dict) and output_idx is not None and input_idx is not None:
            updated = copy.deepcopy(cached)
            routing = list(updated.get('routing') or [])
            while len(routing) <= int(output_idx):
                routing.append(len(routing) + 1)
            routing[int(output_idx)] = int(input_idx) + 1
            updated['routing'] = routing
            _videohub_state_cache['payload'] = updated
        _videohub_state_cache['invalidated'] = True
        # A confirmed command is evidence that the device may have recovered.
        _videohub_state_cache['retry_after'] = 0.0
    _start_videohub_state_refresh()


@app.route('/api/videohub/labels', methods=['GET'])
def api_videohub_labels():
    """Return labels from the same non-blocking snapshot used by routing."""
    force = str(request.args.get('refresh') or '').strip().lower() in ('1', 'true', 'yes')
    snapshot = _get_videohub_state_snapshot(force=force)
    return jsonify({key: value for key, value in snapshot.items() if key != 'routing'})


@app.route('/api/videohub/state', methods=['GET'])
def api_videohub_state():
    """Return cached routing immediately and refresh hardware off-request."""
    force = str(request.args.get('refresh') or '').strip().lower() in ('1', 'true', 'yes')
    return jsonify(_get_videohub_state_snapshot(force=force))


@app.route('/media/videohub_room_images/<path:filename>', methods=['GET'])
@require_page('page:videohub', 'VideoHub')
def api_videohub_room_image(filename: str):
    fn = Path(str(filename or '')).name
    if not fn:
        return abort(404)
    folder = _videohub_rooms_images_dir()
    if not folder.exists():
        return abort(404)
    return send_from_directory(str(folder), fn)


@app.route('/api/videohub/rooms/config', methods=['GET'])
def api_videohub_rooms_config_get():
    try:
        cfg = _load_videohub_rooms_config()
        rooms = []
        for r in (cfg.get('rooms') or []):
            if not isinstance(r, dict):
                continue
            rr = dict(r)
            bg = str(rr.get('background_image') or '').strip()
            rr['background_url'] = f"/media/videohub_room_images/{bg}" if bg else ''
            rooms.append(rr)
        cfg['rooms'] = rooms
        return jsonify({'ok': True, 'config': cfg})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/videohub/rooms/config', methods=['PUT'])
def api_videohub_rooms_config_put():
    if not _api_requires_videohub_edit():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    try:
        body = request.get_json() or {}
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid json'}), 400
    if not isinstance(body, dict):
        return jsonify({'ok': False, 'error': 'invalid payload'}), 400

    old_cfg = _load_videohub_rooms_config()
    new_cfg = _save_videohub_rooms_config(body)
    room_changes = _videohub_rooms_diff(old_cfg, new_cfg)

    # Cleanup orphaned images when rooms are deleted or image is replaced.
    try:
        old_map = {}
        for r in (old_cfg.get('rooms') or []):
            if isinstance(r, dict):
                old_map[str(r.get('id') or '')] = str(r.get('background_image') or '')
        new_map = {}
        for r in (new_cfg.get('rooms') or []):
            if isinstance(r, dict):
                rid = str(r.get('id') or '')
                new_map[rid] = str(r.get('background_image') or '')
        for rid, old_img in old_map.items():
            if not old_img:
                continue
            if rid not in new_map or str(new_map.get(rid) or '') != old_img:
                _delete_room_background_image(old_img)
    except Exception:
        pass

    try:
        rooms = []
        for r in (new_cfg.get('rooms') or []):
            if not isinstance(r, dict):
                continue
            rr = dict(r)
            bg = str(rr.get('background_image') or '').strip()
            rr['background_url'] = f"/media/videohub_room_images/{bg}" if bg else ''
            rooms.append(rr)
        new_cfg['rooms'] = rooms
    except Exception:
        pass
    try:
        filtered_changed = bool(room_changes.get('filtered_inputs'))
        log_event(
            'videohub.room.config.save',
            (
                f"Saved VideoHub rooms "
                f"({len(room_changes.get('rooms_created') or [])} created, {len(room_changes.get('rooms_updated') or [])} updated, "
                f"{len(room_changes.get('rooms_deleted') or [])} deleted, {len(room_changes.get('output_room_changes') or [])} outputs moved"
                f"{', filtered inputs changed' if filtered_changed else ''})"
            ),
            source='web',
            status='success',
            target_type='videohub_rooms',
            target_id='videohub_rooms.json',
            details={'changes': room_changes, 'room_count': len(new_cfg.get('rooms') or [])},
        )
    except Exception:
        pass
    return jsonify({'ok': True, 'config': new_cfg})


@app.route('/api/videohub/rooms/background', methods=['POST'])
def api_videohub_rooms_background_upload():
    if not _api_requires_videohub_edit():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    room_id = str(request.form.get('room_id') or '').strip()
    if not room_id:
        return jsonify({'ok': False, 'error': 'room_id is required'}), 400
    upload = request.files.get('file')
    if upload is None:
        return jsonify({'ok': False, 'error': 'file is required'}), 400

    cfg = _load_videohub_rooms_config()
    room = None
    for r in (cfg.get('rooms') or []):
        if isinstance(r, dict) and str(r.get('id') or '') == room_id:
            room = r
            break
    if room is None:
        return jsonify({'ok': False, 'error': 'room not found'}), 404

    original_name = secure_filename(str(upload.filename or '').strip())
    ext = Path(original_name).suffix.lower()
    if ext not in _VIDEOHUB_ROOM_ALLOWED_EXTS:
        return jsonify({'ok': False, 'error': 'invalid file type'}), 400

    try:
        content = upload.read(_VIDEOHUB_ROOM_IMAGE_MAX_BYTES + 1)
    except Exception:
        return jsonify({'ok': False, 'error': 'failed to read upload'}), 400
    if not content:
        return jsonify({'ok': False, 'error': 'empty file'}), 400
    if len(content) > _VIDEOHUB_ROOM_IMAGE_MAX_BYTES:
        return jsonify({'ok': False, 'error': 'file exceeds 10MB limit'}), 400

    out_dir = _videohub_rooms_images_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    new_name = f"{room_id}_{uuid.uuid4().hex}{ext}"
    out_path = out_dir / new_name
    out_path.write_bytes(content)

    old_name = str(room.get('background_image') or '').strip()
    room['background_image'] = new_name
    saved = _save_videohub_rooms_config(cfg)
    if old_name and old_name != new_name:
        _delete_room_background_image(old_name)

    room_out = None
    for r in (saved.get('rooms') or []):
        if isinstance(r, dict) and str(r.get('id') or '') == room_id:
            room_out = dict(r)
            break
    if room_out is None:
        room_out = {'id': room_id, 'background_image': new_name}
    room_out['background_url'] = f"/media/videohub_room_images/{new_name}"
    try:
        log_event(
            'videohub.room.background.upload',
            f"Uploaded VideoHub room background for {room_id}",
            source='web',
            status='success',
            target_type='videohub_room',
            target_id=room_id,
            details={'filename': new_name, 'original_name': original_name, 'size_bytes': len(content), 'old_filename': old_name},
        )
    except Exception:
        pass
    return jsonify({'ok': True, 'room': room_out})


@app.route('/api/videohub/rooms/<string:room_id>/background', methods=['DELETE'])
def api_videohub_rooms_background_delete(room_id: str):
    if not _api_requires_videohub_edit():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    rid = str(room_id or '').strip()
    if not rid:
        return jsonify({'ok': False, 'error': 'room_id is required'}), 400

    cfg = _load_videohub_rooms_config()
    found = None
    for r in (cfg.get('rooms') or []):
        if isinstance(r, dict) and str(r.get('id') or '') == rid:
            found = r
            break
    if found is None:
        return jsonify({'ok': False, 'error': 'room not found'}), 404

    old_name = str(found.get('background_image') or '').strip()
    found['background_image'] = ''
    saved = _save_videohub_rooms_config(cfg)
    if old_name:
        _delete_room_background_image(old_name)

    room_out = None
    for r in (saved.get('rooms') or []):
        if isinstance(r, dict) and str(r.get('id') or '') == rid:
            room_out = dict(r)
            break
    if room_out is None:
        room_out = {'id': rid, 'background_image': ''}
    room_out['background_url'] = ''
    try:
        log_event(
            'videohub.room.background.delete',
            f"Deleted VideoHub room background for {rid}",
            source='web',
            status='success',
            target_type='videohub_room',
            target_id=rid,
            details={'filename': old_name},
        )
    except Exception:
        pass
    return jsonify({'ok': True, 'room': room_out})


@app.route('/api/home/overview', methods=['GET'])
def api_home_overview():
    """Lightweight Home dashboard data.

    Best-effort and intentionally minimal.
    """

    # Sync from disk first so multi-process deployments stay consistent.
    try:
        _home_state_sync_from_disk()
    except Exception:
        pass

    with _home_overview_lock:
        last_timer = dict(_home_last_timer_preset)
        last_vh = dict(_home_last_videohub_preset)
        last_vh_route = dict(_home_last_videohub_route)

    try:
        timer_presets_path = Path(str(getattr(utils, 'TIMER_PRESETS_FILE', 'timer_presets.json')))
    except Exception:
        timer_presets_path = Path('timer_presets.json')

    try:
        cfg = utils.get_config()
    except Exception:
        cfg = {}

    try:
        videohub_presets_path = Path(str(cfg.get('videohub_presets_file', 'videohub_presets.json')))
    except Exception:
        videohub_presets_path = Path('videohub_presets.json')

    cache_stamp = (
        _path_snapshot(timer_presets_path),
        _path_snapshot(_home_state_path()),
        _path_snapshot(videohub_presets_path),
        last_timer.get('preset'),
        last_timer.get('name'),
        last_timer.get('time'),
        last_timer.get('ts'),
        last_vh.get('id'),
        last_vh.get('ts'),
        last_vh_route.get('output'),
        last_vh_route.get('input'),
        last_vh_route.get('monitor'),
        last_vh_route.get('ts'),
    )

    with _home_overview_cache_lock:
        cached = _home_overview_cache.get('payload')
        if _home_overview_cache.get('stamp') == cache_stamp and isinstance(cached, dict):
            payload = dict(cached)
            payload['ts'] = time.time()
            return jsonify(payload)

    # Timers
    try:
        timer_presets = list(utils.load_timer_presets()) if hasattr(utils, 'load_timer_presets') else []
    except Exception:
        timer_presets = []

    def _timer_info(n: int):
        try:
            idx = int(n) - 1
        except Exception:
            return None
        if idx < 0 or idx >= len(timer_presets):
            return None
        p = timer_presets[idx]
        if isinstance(p, dict):
            name = str(p.get('name', '')).strip() or str(p.get('time', '')).strip()
            t = str(p.get('time', '')).strip()
        else:
            t = str(p).strip()
            name = t
        return {'preset': int(n), 'name': name, 'time': t}

    last_timer_num = None
    try:
        if last_timer.get('preset') is not None:
            last_timer_num = int(last_timer.get('preset'))
    except Exception:
        last_timer_num = None

    # If nothing has been pressed yet, treat preset 1 as the "next" suggestion.
    next_timer_num = None
    if timer_presets:
        if last_timer_num is None:
            next_timer_num = 1
        else:
            cand = last_timer_num + 1
            if 1 <= cand <= len(timer_presets):
                next_timer_num = cand

    timers_payload = {
        'last': _timer_info(last_timer_num) if last_timer_num else None,
        'next': _timer_info(next_timer_num) if next_timer_num else None,
        'preset_count': len(timer_presets),
        'last_ts': last_timer.get('ts'),
    }

    # VideoHub last applied preset name (best-effort)
    videohub_payload = {
        'last': None,
        'last_ts': last_vh.get('ts'),
        'route': None,
        'route_ts': last_vh_route.get('ts'),
    }
    try:
        last_id = last_vh.get('id')
        if last_id is not None:
            last_id = int(last_id)
            name = None
            try:
                app_inst = _get_videohub_app()
                if app_inst is not None and hasattr(app_inst, 'list_presets'):
                    cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
                    presets = app_inst.list_presets(cfg)  # type: ignore[attr-defined]
                    if isinstance(presets, list):
                        for p in presets:
                            try:
                                pid = int(p.get('id')) if isinstance(p, dict) and p.get('id') is not None else None
                            except Exception:
                                pid = None
                            if pid == last_id:
                                try:
                                    name = str(p.get('name', '')).strip() if isinstance(p, dict) else None
                                except Exception:
                                    name = None
                                break
            except Exception:
                name = None

            videohub_payload['last'] = {'id': last_id, 'name': name or f"Preset #{last_id}"}
    except Exception:
        pass

    # Include last route action (useful when operators route directly instead of applying presets)
    try:
        out_n = last_vh_route.get('output')
        in_n = last_vh_route.get('input')
        if out_n is not None and in_n is not None:
            videohub_payload['route'] = {
                'output': int(out_n),
                'input': int(in_n),
                'monitor': bool(last_vh_route.get('monitor') or False),
            }
    except Exception:
        pass

    payload = {'ok': True, 'timers': timers_payload, 'videohub': videohub_payload, 'ts': time.time()}
    with _home_overview_cache_lock:
        _home_overview_cache['stamp'] = cache_stamp
        _home_overview_cache['payload'] = payload
    return jsonify(payload)


def _activity_log_access_error():
    if not _auth_enabled():
        return None
    if _api_request_is_automation_principal():
        return None
    try:
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        if not can_access('page:console'):
            return jsonify({'ok': False, 'error': 'forbidden'}), 403
    except Exception:
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    return None


@app.route('/api/activity-log', methods=['GET'])
def api_activity_log():
    access_error = _activity_log_access_error()
    if access_error:
        return access_error

    try:
        limit = int(request.args.get('limit', '50') or '50')
    except Exception:
        limit = 50
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    try:
        page = int(request.args.get('page', '1') or '1')
    except Exception:
        page = 1
    if page < 1:
        page = 1

    where = []
    params: list[Any] = []
    for arg_name, col_name in (
        ('source', 'source'),
        ('status', 'status'),
        ('action', 'action'),
        ('target_type', 'target_type'),
        ('actor_user_id', 'actor_user_id'),
    ):
        raw = str(request.args.get(arg_name) or '').strip()
        if raw:
            where.append(f'{col_name} = ?')
            params.append(raw)
    start_ts = _activity_ts_param(request.args.get('start'))
    end_ts = _activity_ts_param(request.args.get('end'), end=True)
    if start_ts:
        where.append('ts >= ?')
        params.append(start_ts)
    if end_ts:
        where.append('ts <= ?')
        params.append(end_ts)
    q = str(request.args.get('q') or '').strip()
    if q:
        like = f'%{q}%'
        where.append('(summary LIKE ? OR action LIKE ? OR actor_display LIKE ? OR actor_username LIKE ? OR target_type LIKE ? OR target_id LIKE ? OR details_json LIKE ? OR request_path LIKE ? OR ip LIKE ?)')
        params.extend([like, like, like, like, like, like, like, like, like])
    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    sql = 'SELECT * FROM activity_log'
    if where:
        sql += where_sql
    sql += ' ORDER BY id DESC LIMIT ? OFFSET ?'
    query_params = list(params)
    query_params.extend([limit, (page - 1) * limit])

    try:
        conn = _db()
        try:
            rows = conn.execute(sql, tuple(query_params)).fetchall()
            count_row = conn.execute(f'SELECT count(*) AS c FROM activity_log{where_sql}', tuple(params)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    events = [_activity_row_to_dict(r) for r in rows]
    total = int(count_row['c'] or 0) if count_row else 0
    total_pages = max(1, math.ceil(total / limit)) if total else 1
    return jsonify({'ok': True, 'events': events, 'page': page, 'page_size': limit, 'total': total, 'total_pages': total_pages})


@app.route('/api/activity-log/live', methods=['GET'])
def api_activity_log_live():
    access_error = _activity_log_access_error()
    if access_error:
        return access_error

    try:
        since = int(request.args.get('since', '0') or '0')
    except Exception:
        since = 0

    source = str(request.args.get('source') or '').strip().lower()
    status = str(request.args.get('status') or '').strip().lower()
    q = str(request.args.get('q') or '').strip().lower()
    start_ts = _activity_ts_param(request.args.get('start'))
    end_ts = _activity_ts_param(request.args.get('end'), end=True)

    where = ['id > ?']
    params: list[Any] = [since]
    if source:
        where.append('source = ?')
        params.append(source)
    if status:
        where.append('status = ?')
        params.append(status)
    if start_ts:
        where.append('ts >= ?')
        params.append(start_ts)
    if end_ts:
        where.append('ts <= ?')
        params.append(end_ts)
    if q:
        like = f'%{q}%'
        where.append('(summary LIKE ? OR action LIKE ? OR actor_display LIKE ? OR actor_username LIKE ? OR target_type LIKE ? OR target_id LIKE ? OR details_json LIKE ? OR request_path LIKE ? OR ip LIKE ?)')
        params.extend([like, like, like, like, like, like, like, like, like])

    try:
        conn = _db()
        try:
            rows = conn.execute(
                f"SELECT * FROM activity_log WHERE {' AND '.join(where)} ORDER BY id ASC LIMIT 200",
                tuple(params),
            ).fetchall()
            max_row = conn.execute('SELECT max(id) AS max_id FROM activity_log').fetchone()
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    events = [_activity_row_to_dict(r) for r in rows]
    try:
        next_id = max(int(max_row['max_id'] or 0), since) if max_row else since
    except Exception:
        next_id = max([int(e.get('id') or 0) for e in events] + [since])
    return jsonify({'ok': True, 'next': next_id, 'events': events})


@app.route('/api/activity-log/alerts', methods=['GET'])
def api_activity_log_alerts():
    access_error = _activity_log_access_error()
    if access_error:
        return access_error
    try:
        return jsonify({'ok': True, **_activity_alert_summary()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/activity-log/alerts/acknowledge', methods=['POST'])
def api_activity_log_alerts_acknowledge():
    access_error = _activity_log_access_error()
    if access_error:
        return access_error
    try:
        return jsonify({'ok': True, **_activity_acknowledge_alerts()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _validate_time_hhmm(s: str) -> bool:
    return _normalize_time_hhmm(s) is not None


def _normalize_time_hhmm(value) -> str | None:
    try:
        if hasattr(utils, 'normalize_time_hhmm'):
            return utils.normalize_time_hhmm(value)
    except Exception:
        pass
    try:
        s = str(value or '').strip()
        dt = datetime.strptime(s, '%H:%M')
        return dt.strftime('%H:%M')
    except Exception:
        return None


_RELATIVE_MINUTES_RE = re.compile(r'^\$(?:(?P<zero>0)|(?P<sign>[+-])(?P<minutes>\d+))$')


def _parse_iso_datetime(s: str) -> datetime | None:
    try:
        raw = str(s or '').strip()
    except Exception:
        return None
    if not raw:
        return None

    # Accept common ISO forms, including trailing Z.
    if raw.endswith('Z'):
        raw = raw[:-1] + '+00:00'

    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _resolve_base_datetime_for_relative_time(body: dict) -> tuple[datetime | None, str | None]:
    if not isinstance(body, dict):
        return None, 'request body must be an object when using relative time'

    base_raw = (
        body.get('event_start')
        or body.get('eventStart')
        or body.get('base_time')
        or body.get('baseTime')
    )
    if base_raw is None:
        return None, 'relative time requires event_start (or base_time) in the request body'

    base_dt = _parse_iso_datetime(str(base_raw))
    if base_dt is None:
        return None, 'event_start/base_time must be an ISO datetime string (e.g. 2026-01-25T12:00:00)'

    return base_dt, None


def _resolve_time_hhmm_input(time_value: object, *, body: dict) -> tuple[str | None, str | None, bool]:
    """Resolve a time input into concrete HH:MM.

    Supports:
      - "HH:MM" (direct)
      - "$-60" / "$0" / "$+15" (minutes relative to event_start/base_time)

    Returns (time_str, error, was_relative).
    """
    try:
        s = str(time_value or '').strip()
    except Exception:
        s = ''

    if not s:
        return None, 'time is required', False

    if s.startswith('$'):
        m = _RELATIVE_MINUTES_RE.match(s)
        if not m:
            return None, 'relative time must look like "$-60", "$0", or "$+15"', True

        base_dt, err = _resolve_base_datetime_for_relative_time(body)
        if err:
            return None, err, True

        if m.group('zero') == '0':
            minutes = 0
            sign = 1
        else:
            minutes = int(m.group('minutes'))
            sign = -1 if m.group('sign') == '-' else 1
        dt = base_dt + timedelta(minutes=sign * minutes)
        return dt.strftime('%H:%M'), None, True

    normalized = _normalize_time_hhmm(s)
    if not normalized:
        return None, 'time must be HH:MM', False

    return normalized, None, False


def _format_time_hhmm_ampm(time_str: str) -> str:
    """Convert HH:MM -> H:MMAM (no space). Falls back to original on errors."""
    try:
        dt = datetime.strptime(str(time_str).strip(), '%H:%M')
    except Exception:
        return str(time_str or '').strip()
    hour = dt.hour % 12
    if hour == 0:
        hour = 12
    suffix = 'AM' if dt.hour < 12 else 'PM'
    return f"{hour}:{dt.minute:02d}{suffix}"


def _parse_timer_duration_minutes(value: object) -> tuple[int | None, str | None]:
    """Parse a human-entered duration into minutes.

    Accepts plain minutes ("15"), hours/minutes ("1h 30m"), and H:MM ("1:30").
    """
    try:
        raw = str(value or '').strip().lower()
    except Exception:
        raw = ''
    if not raw:
        return None, 'duration is required'

    raw = raw.replace(',', ' ')
    compact = re.sub(r'\s+', '', raw)

    if re.fullmatch(r'\d+', compact):
        minutes = int(compact)
    else:
        hm = re.fullmatch(r'(\d+):(\d{1,2})', compact)
        if hm:
            hours = int(hm.group(1))
            mins = int(hm.group(2))
            if mins > 59:
                return None, 'duration minutes must be 0..59 when using H:MM'
            minutes = (hours * 60) + mins
        else:
            token_re = re.compile(r'(\d+)(hours|hour|hrs|hr|h|minutes|minute|mins|min|m)')
            pos = 0
            hours = 0
            mins = 0
            saw_token = False
            for m in token_re.finditer(compact):
                if m.start() != pos:
                    return None, 'duration must look like 15m, 1h 30m, or 1:30'
                amount = int(m.group(1))
                unit = m.group(2)
                if unit.startswith('h'):
                    hours += amount
                else:
                    mins += amount
                saw_token = True
                pos = m.end()
            if not saw_token or pos != len(compact):
                return None, 'duration must look like 15m, 1h 30m, or 1:30'
            minutes = (hours * 60) + mins

    if minutes <= 0:
        return None, 'duration must be greater than 0 minutes'
    if minutes > 24 * 60:
        return None, 'duration must be 24 hours or less'
    return minutes, None


def _time_hhmm_to_minutes(time_str: str) -> int | None:
    normalized = _normalize_time_hhmm(time_str)
    if not normalized:
        return None
    try:
        h, m = normalized.split(':', 1)
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _minutes_to_time_hhmm(minutes: int) -> str:
    total = int(minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _resolve_stream_start_preset(cfg: dict, presets: list[dict]) -> tuple[int, dict] | None:
    """Return (1-based preset number, preset dict) for stream-start message."""
    try:
        preset_number = int(cfg.get('stream_start_preset', 0))
    except Exception:
        preset_number = 0
    if preset_number < 1:
        return None
    if preset_number > len(presets):
        return None
    try:
        preset = presets[preset_number - 1]
    except Exception:
        return None
    return preset_number, preset


def _build_stream_start_message(preset: dict) -> str | None:
    try:
        t = str((preset or {}).get('time', '')).strip()
    except Exception:
        t = ''
    if not t:
        return None
    if not _validate_time_hhmm(t):
        return None
    pretty = _format_time_hhmm_ampm(t)
    return f"STREAM {pretty}"


_BTN_FULL_RE = re.compile(r'^location/\d+/\d+/\d+/press$')
_BTN_SHORT_RE = re.compile(r'^\d+/\d+/\d+$')


def _normalize_internal_api_path(raw: str) -> str | None:
    """Normalize user-entered API paths to an internal /api/... path.

    Accepts:
      - /api/videohub/ping
      - /videohub/ping
      - videohub/ping
    Rejects absolute URLs.
    """
    try:
        s = str(raw or '').strip()
    except Exception:
        s = ''
    if not s:
        return None
    if '://' in s:
        return None
    if s.startswith('/api/'):
        return s
    if s.startswith('/'):
        return '/api' + s
    s = s.lstrip('/')
    return '/api/' + s


def _normalize_companion_button_url(raw: str) -> str | None:
    s = (raw or '').strip()
    if not s:
        return None
    if _BTN_FULL_RE.match(s):
        return s
    if _BTN_SHORT_RE.match(s):
        return f'location/{s}/press'
    return None


def _normalize_trigger_action_spec(raw: dict) -> tuple[dict | None, str | None]:
    """Normalize a trigger spec for storage.

    Supported action modes:
      - Companion: {actionType:'companion', buttonURL:'location/1/2/3/press'}
      - API: {actionType:'api', api:{method:'POST', path:'/api/...', body:{...}}}
      - Timer: {actionType:'timer', timer:{preset:1, time:'08:15'|'$-15'|'$0'|'$+15', apply:true}}
    """
    if not isinstance(raw, dict):
        return None, 'trigger must be an object'

    out: dict = {}

    # enabled flag (default True); support legacy 'active' key too.
    try:
        if 'enabled' in raw:
            out['enabled'] = bool(raw.get('enabled'))
        elif 'active' in raw:
            out['enabled'] = bool(raw.get('active'))
        else:
            out['enabled'] = True
    except Exception:
        out['enabled'] = True

    # typeOfTrigger + minutes are normalized by callers, but keep safe defaults.
    try:
        out['typeOfTrigger'] = str(raw.get('typeOfTrigger', 'AT')).upper()
    except Exception:
        out['typeOfTrigger'] = 'AT'
    try:
        out['minutes'] = int(raw.get('minutes', 0) or 0)
    except Exception:
        out['minutes'] = 0

    # Optional display name + stable uid (used for UI organization)
    try:
        name = str(raw.get('name') or '').strip()
    except Exception:
        name = ''
    if name:
        out['name'] = name
    try:
        uid_val = str(raw.get('uid') or '').strip()
    except Exception:
        uid_val = ''
    if uid_val:
        out['uid'] = uid_val

    action_type = str(raw.get('actionType') or raw.get('action_type') or '').strip().lower()

    api_obj = None
    if isinstance(raw.get('api'), dict):
        api_obj = raw.get('api')
    elif isinstance(raw.get('api'), str):
        # allow passing JSON for convenience
        try:
            api_obj = json.loads(raw.get('api') or '')
        except Exception:
            api_obj = None

    if not action_type:
        # infer based on payload fields
        if api_obj is not None or raw.get('path') or raw.get('method'):
            action_type = 'api'
        elif isinstance(raw.get('timer'), dict) or raw.get('preset') is not None:
            action_type = 'timer'
        else:
            action_type = 'companion'

    if action_type == 'api':
        method = None
        path = None
        body = None

        if isinstance(api_obj, dict):
            method = api_obj.get('method')
            path = api_obj.get('path')
            body = api_obj.get('body')
        else:
            method = raw.get('method')
            path = raw.get('path')
            body = raw.get('body')

        method = str(method or 'POST').strip().upper()
        path = str(path or '').strip()

        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
            return None, f'invalid api method: {method}'

        path_norm = _normalize_internal_api_path(path)
        if not path_norm or not path_norm.startswith('/api/'):
            return None, 'api path must be a relative path (the /api prefix is added automatically)'
        path = path_norm

        if isinstance(body, str) and body.strip():
            try:
                body = json.loads(body)
            except Exception:
                return None, 'api body must be valid JSON'

        if body is not None and not isinstance(body, (dict, list)):
            return None, 'api body must be an object, array, or empty'

        out['actionType'] = 'api'
        out['api'] = {'method': method, 'path': path}
        if body is not None:
            out['api']['body'] = body
        return out, None

    if action_type == 'timer':
        timer_obj = raw.get('timer') if isinstance(raw.get('timer'), dict) else None
        preset_raw = (timer_obj or {}).get('preset', raw.get('preset'))
        time_raw = (timer_obj or {}).get('time', raw.get('time'))
        apply_raw = (timer_obj or {}).get('apply', raw.get('apply'))

        try:
            preset = int(preset_raw)
        except Exception:
            return None, 'timer preset must be an integer (1-based)'
        if preset < 1:
            return None, 'timer preset must be >= 1'

        try:
            time_str = str(time_raw or '').strip()
        except Exception:
            time_str = ''
        if not (_validate_time_hhmm(time_str) or _RELATIVE_MINUTES_RE.match(time_str)):
            return None, 'timer time must be HH:MM or relative ($-15, $0, $+15)'

        apply_now = bool(apply_raw) if apply_raw is not None else False

        timer_payload: dict[str, Any] = {'preset': preset, 'time': time_str, 'apply': apply_now}

        out['actionType'] = 'timer'
        out['timer'] = timer_payload
        return out, None

    # Default to companion
    btn_raw = str(raw.get('buttonURL') or raw.get('button_url') or raw.get('url') or '').strip()
    btn_norm = _normalize_companion_button_url(btn_raw) if btn_raw else ''
    if btn_raw and not btn_norm:
        return None, "Invalid buttonURL format. Use '1/2/3' or 'location/1/2/3/press'"

    out['actionType'] = 'companion'
    out['buttonURL'] = btn_norm or ''
    return out, None


# --- Timer preset actions (Companion button presses) ---
_timer_action_lock = threading.Lock()
_timer_action_jobs: dict[int, threading.Timer] = {}
_timer_mutation_lock = threading.RLock()
_timer_companion_sync_lock = threading.Lock()
_timer_companion_sync_generation = 0


def _cancel_timer_action_job(pp_timer_id: int) -> None:
    with _timer_action_lock:
        old = _timer_action_jobs.pop(pp_timer_id, None)
        try:
            if old is not None:
                old.cancel()
        except Exception:
            pass


def _fire_timer_button_presses_now(*, pp_timer_id: int, preset_number: int, preset_name: str, time_str: str, button_presses: list[dict]) -> dict:
    """Fire configured Companion button presses immediately.

    This is intentionally "immediate on button press" behavior: when the timer
    preset is applied, we execute the press list right away.
    """
    _cancel_timer_action_job(pp_timer_id)

    presses: list[str] = []
    for p in button_presses or []:
        if isinstance(p, dict):
            u = _normalize_companion_button_url(str(p.get('buttonURL') or p.get('url') or ''))
        else:
            u = _normalize_companion_button_url(str(p or ''))
        if u:
            presses.append(u)

    if not presses:
        return {'fired': False, 'count': 0}

    try:
        log_event(
            'timers.companion.fire',
            f"Timer preset #{preset_number} '{preset_name or time_str}' firing {len(presses)} Companion press(es)",
            source='api',
            status='info',
            target_type='timer_preset',
            target_id=preset_number,
            details={'preset': preset_number, 'name': preset_name, 'time': time_str, 'press_count': len(presses)},
        )
    except Exception:
        pass

    try:
        comp = utils.get_companion() if hasattr(utils, 'get_companion') else None
    except Exception:
        comp = None

    try:
        if comp is not None and hasattr(comp, 'check_connection'):
            comp.check_connection()
    except Exception:
        pass

    if comp is None or not getattr(comp, 'connected', False):
        try:
            log_event(
                'timers.companion.fire',
                'Companion not connected; timer presses skipped',
                source='api',
                status='warning',
                target_type='timer_preset',
                target_id=preset_number,
                details={'preset': preset_number, 'press_count': len(presses), 'error': 'companion_not_connected'},
            )
        except Exception:
            pass
        return {'fired': False, 'count': len(presses), 'error': 'companion_not_connected'}

    ok_count = 0
    for u in presses:
        ok = False
        try:
            ok = bool(comp.post_command(u))
        except Exception:
            ok = False
        if ok:
            ok_count += 1

    try:
        log_event(
            'timers.companion.fire.complete',
            f"Timer preset #{preset_number} Companion presses complete: {ok_count} OK, {len(presses) - ok_count} failed",
            source='api',
            status='success' if ok_count == len(presses) else 'warning',
            target_type='timer_preset',
            target_id=preset_number,
            details={'preset': preset_number, 'ok': ok_count, 'fail': len(presses) - ok_count, 'presses': presses},
        )
    except Exception:
        pass

    return {'fired': True, 'count': len(presses), 'ok': ok_count, 'fail': len(presses) - ok_count}


def _cfg_bool(cfg: dict, key: str, default: bool = False) -> bool:
    try:
        v = cfg.get(key, default)
    except Exception:
        return bool(default)

    if isinstance(v, bool):
        return v
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in ('1', 'true', 't', 'yes', 'y', 'on'):
        return True
    if s in ('0', 'false', 'f', 'no', 'n', 'off'):
        return False
    return bool(default)


def _cfg_int(cfg: dict, key: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(cfg.get(key, default))
    except Exception:
        v = int(default)
    if min_value is not None and v < min_value:
        v = min_value
    if max_value is not None and v > max_value:
        v = max_value
    return v


def _sync_companion_timer_variable_for_preset(*, cfg: dict, preset_number: int, preset: dict) -> tuple[bool, str | None]:
    """Update Companion custom variable for a single timer preset.

    Uses config key `companion_timer_name` as the variable prefix.
    Writes value as:
      label\npretty_time
    """
    try:
        prefix = str(cfg.get('companion_timer_name', '')).strip()
    except Exception:
        prefix = ''
    if not prefix:
        return False, 'companion_timer_name not configured'

    try:
        comp = utils.get_companion() if hasattr(utils, 'get_companion') else None
    except Exception:
        comp = None
    if comp is None:
        return False, 'companion client not available'

    try:
        var_name = f"{prefix}{int(preset_number)}"
    except Exception:
        return False, 'invalid preset_number'

    try:
        t = str((preset or {}).get('time', '')).strip()
    except Exception:
        t = ''

    try:
        dt = datetime.strptime(t, '%H:%M')
        pretty_time = dt.strftime('%I:%M%p').lower()
    except Exception:
        pretty_time = t

    try:
        label = str((preset or {}).get('name', '')).strip()
    except Exception:
        label = ''

    # If the label is missing (or just equals the raw HH:MM), use the variable name as label.
    if (not label) or (label == t):
        label = var_name

    value = f"{label}\n{pretty_time}"
    try:
        ok = bool(comp.SetVariable(var_name, value))
    except Exception:
        ok = False
    return ok, None if ok else 'failed to set variable'


def _queue_companion_timer_variable_sync(*, cfg: dict, presets: list[dict]) -> None:
    """Best-effort Companion variable sync that never blocks timer saves."""
    global _timer_companion_sync_generation

    try:
        cfg_copy = copy.deepcopy(cfg or {})
    except Exception:
        cfg_copy = dict(cfg or {})
    try:
        presets_copy = copy.deepcopy(presets or [])
    except Exception:
        presets_copy = list(presets or [])

    with _timer_companion_sync_lock:
        _timer_companion_sync_generation += 1
        generation = _timer_companion_sync_generation

    def _worker() -> None:
        try:
            # Coalesce bursts of edits into one latest-state sync.
            time.sleep(0.25)
            with _timer_companion_sync_lock:
                if generation != _timer_companion_sync_generation:
                    return

            ok_count = 0
            fail_count = 0
            for i, preset in enumerate(presets_copy, start=1):
                ok, _err = _sync_companion_timer_variable_for_preset(
                    cfg=cfg_copy,
                    preset_number=i,
                    preset=preset if isinstance(preset, dict) else {},
                )
                if ok:
                    ok_count += 1
                else:
                    fail_count += 1

            if _is_debug_enabled():
                try:
                    _console_append(
                        f"[TIMERS] Companion variable sync finished: ok={ok_count} fail={fail_count}\n"
                    )
                except Exception:
                    pass
        except Exception as e:
            if _is_debug_enabled():
                try:
                    _console_append(f"[TIMERS] Companion variable sync error: {e}\n")
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()


def _timer_get_ci(d: dict, *keys: str):
    try:
        for k in keys:
            if k in d:
                return d.get(k)
        lower = {str(k).lower(): v for k, v in d.items()}
        for k in keys:
            lk = str(k).lower()
            if lk in lower:
                return lower.get(lk)
    except Exception:
        return None
    return None


def _timer_has_ci(d: dict, *keys: str) -> bool:
    try:
        lower = {str(k).lower() for k in d.keys()}
        return any((k in d) or (str(k).lower() in lower) for k in keys)
    except Exception:
        return False


def _timer_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ('1', 'true', 't', 'yes', 'y', 'on')


def _timer_normalize_button_presses(raw) -> tuple[list[dict[str, str]] | None, str | None]:
    if raw is None:
        return [], None
    raw_list = [raw] if isinstance(raw, (dict, str)) else raw
    if not isinstance(raw_list, list):
        return None, 'button_presses must be an array of button press entries'
    if len(raw_list) > 50:
        return None, 'too many button presses (max 50 per timer)'

    out: list[dict[str, str]] = []
    for item in raw_list:
        if isinstance(item, str):
            u = _normalize_companion_button_url(item)
        elif isinstance(item, dict):
            u = _normalize_companion_button_url(str(item.get('buttonURL') or item.get('url') or item.get('button_url') or ''))
        else:
            u = None
        if not u:
            return None, "Invalid buttonURL in button_presses. Use '1/2/3' or 'location/1/2/3/press'"
        out.append({'buttonURL': u})
    return out, None


def _timer_normalize_preset_for_save(value) -> tuple[dict | None, str | None]:
    if isinstance(value, dict):
        time_str = str(value.get('time', '')).strip()
        name_str = str(value.get('name', '')).strip()
        raw_presses = value.get('button_presses')
        if raw_presses is None and 'buttonPresses' in value:
            raw_presses = value.get('buttonPresses')
        if raw_presses is None and 'actions' in value:
            raw_presses = value.get('actions')
    else:
        time_str = str(value or '').strip()
        name_str = ''
        raw_presses = None

    if not time_str:
        return None, None
    normalized_time = _normalize_time_hhmm(time_str)
    if not normalized_time:
        return None, f'invalid time: {time_str}. Use HH:MM'
    time_str = normalized_time

    presses, err = _timer_normalize_button_presses(raw_presses)
    if err:
        return None, err

    obj = {'time': time_str, 'name': name_str or time_str}
    if presses:
        obj['button_presses'] = presses
    return obj, None


def _timer_normalize_preset_list(values) -> tuple[list[dict] | None, str | None]:
    if not isinstance(values, list):
        return None, 'timer_presets must be an array of presets'
    out: list[dict] = []
    for value in values:
        obj, err = _timer_normalize_preset_for_save(value)
        if err:
            return None, err
        if obj is not None:
            out.append(obj)
    if len(out) < 1:
        return None, 'timer_presets must contain at least 1 entry'
    if len(out) > 100:
        return None, 'timer_presets too large (max 100)'
    return out, None


def _timer_state_payload(*, cfg: dict, presets: list, **extra) -> dict:
    try:
        propresenter_timer_index = int(cfg.get('propresenter_timer_index', cfg.get('timer_index', 1)))
    except Exception:
        propresenter_timer_index = 1
    try:
        stream_start_preset = int(cfg.get('stream_start_preset', 0))
    except Exception:
        stream_start_preset = 0
    if stream_start_preset < 1:
        stream_start_preset = 0

    payload = {
        'ok': True,
        'propresenter_timer_index': propresenter_timer_index,
        'stream_start_preset': stream_start_preset,
        'timer_presets': presets,
        'preset_count': len(presets),
    }
    payload.update(extra)
    return payload


def _save_timer_config_changes(cfg: dict, changes: dict) -> tuple[dict, bool]:
    changed = False
    for key, value in (changes or {}).items():
        if cfg.get(key) != value:
            cfg[key] = value
            changed = True
    if not changed:
        return cfg, False

    utils.save_config(cfg)
    utils.reload_config(force=True)
    try:
        return utils.get_config(), True
    except Exception:
        return cfg, True


def _mutate_timers(body: dict) -> tuple[dict, int]:
    if not isinstance(body, dict):
        body = {}

    action = str(body.get('action') or '').strip().lower()
    if not action:
        action = 'update_preset' if _timer_has_ci(body, 'preset', 'preset_index', 'index', 'number') else 'replace_all'

    apply_after: tuple[int, dict, list, dict, object, bool] | None = None

    with _timer_mutation_lock:
        try:
            cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
        except Exception:
            cfg = {}
        try:
            presets = list(utils.load_timer_presets()) if hasattr(utils, 'load_timer_presets') else []
        except Exception:
            presets = []

        config_changes: dict = {}
        presets_changed = False
        extra: dict = {'action': action}

        try:
            if action in ('replace_all', 'set_all'):
                raw_presets = body.get('timer_presets')
                if raw_presets is None:
                    raw_presets = body.get('presets')
                normalized, err = _timer_normalize_preset_list(raw_presets)
                if err:
                    return {'ok': False, 'error': err}, 400

                allow_delete = _timer_bool(body.get('allow_delete'))
                if presets and len(normalized or []) < len(presets) and not allow_delete:
                    return {
                        'ok': False,
                        'error': (
                            f'refusing to shrink timer preset list from {len(presets)} to {len(normalized or [])} '
                            'without allow_delete=true'
                        ),
                    }, 409

                presets = normalized or []
                presets_changed = True

                stream_start_raw = body.get('stream_start_preset')
                if stream_start_raw is None:
                    stream_start_raw = body.get('streamStartPreset')
                if stream_start_raw is not None:
                    try:
                        stream_start = 0 if str(stream_start_raw).strip() == '' else int(stream_start_raw)
                    except Exception:
                        return {'ok': False, 'error': 'stream_start_preset must be an integer (1-based)'}, 400
                    if stream_start != 0 and not (1 <= stream_start <= len(presets)):
                        return {'ok': False, 'error': f'stream_start_preset out of range (1..{len(presets)})'}, 400
                    config_changes['stream_start_preset'] = stream_start

                if _timer_has_ci(body, 'propresenter_timer_index', 'timer_index'):
                    try:
                        config_changes['propresenter_timer_index'] = int(_timer_get_ci(body, 'propresenter_timer_index', 'timer_index'))
                    except Exception:
                        return {'ok': False, 'error': 'propresenter_timer_index must be an integer'}, 400
                    cfg.pop('timer_index', None)

            elif action == 'update_preset':
                if not presets:
                    return {'ok': False, 'error': 'no presets configured (timer_presets.json is empty)', 'preset_count': 0}, 400
                preset_raw = _timer_get_ci(body, 'preset', 'preset_index', 'index', 'number')
                try:
                    preset_number = int(preset_raw)
                except Exception:
                    return {'ok': False, 'error': 'preset must be an integer (1-based)'}, 400

                preset_index = preset_number - 1
                if preset_index < 0 or preset_index >= len(presets):
                    return {'ok': False, 'error': f'preset out of range (1..{len(presets)})', 'preset_count': len(presets)}, 400

                patch = body.get('patch') if isinstance(body.get('patch'), dict) else body
                current = dict(presets[preset_index]) if isinstance(presets[preset_index], dict) else {
                    'time': str(presets[preset_index]).strip(),
                    'name': str(presets[preset_index]).strip(),
                }
                updated = dict(current)
                time_was_relative = False
                time_raw = None

                if _timer_has_ci(patch, 'time', 'hhmm', 'value'):
                    time_raw = _timer_get_ci(patch, 'time', 'hhmm', 'value')
                    time_str, time_err, time_was_relative = _resolve_time_hhmm_input(time_raw, body=body)
                    if time_err:
                        return {'ok': False, 'error': time_err}, 400
                    updated['time'] = time_str

                if _timer_has_ci(patch, 'name', 'label'):
                    name_raw = _timer_get_ci(patch, 'name', 'label')
                    updated['name'] = str(name_raw or '').strip()

                if _timer_has_ci(patch, 'button_presses', 'buttonPresses', 'actions'):
                    raw_presses = _timer_get_ci(patch, 'button_presses', 'buttonPresses', 'actions')
                    presses, err = _timer_normalize_button_presses(raw_presses)
                    if err:
                        return {'ok': False, 'error': err}, 400
                    if presses:
                        updated['button_presses'] = presses
                    else:
                        updated.pop('button_presses', None)

                normalized_updated_time = _normalize_time_hhmm(str(updated.get('time', '')).strip())
                if not normalized_updated_time:
                    return {'ok': False, 'error': 'time must be HH:MM'}, 400
                updated['time'] = normalized_updated_time
                if not str(updated.get('name', '')).strip():
                    updated['name'] = str(updated.get('time', '')).strip()

                if updated != current:
                    presets[preset_index] = updated
                    presets_changed = True

                extra.update({
                    'preset': preset_number,
                    'timer_preset': updated,
                    'updated': presets_changed,
                    'time_input': str(time_raw) if time_was_relative else None,
                })

                if _timer_bool(_timer_get_ci(body, 'apply', 'apply_now', 'applypreset')):
                    apply_after = (
                        preset_number,
                        copy.deepcopy(cfg),
                        copy.deepcopy(presets),
                        copy.deepcopy(updated),
                        time_raw,
                        time_was_relative,
                    )

            elif action == 'create_preset':
                time_raw = body.get('time', '00:00')
                time_str, time_err, _time_was_relative = _resolve_time_hhmm_input(time_raw, body=body)
                if time_err:
                    return {'ok': False, 'error': time_err}, 400
                name = str(body.get('name') or '').strip() or time_str
                presses, err = _timer_normalize_button_presses(body.get('button_presses'))
                if err:
                    return {'ok': False, 'error': err}, 400
                item = {'time': time_str, 'name': name}
                if presses:
                    item['button_presses'] = presses
                presets.append(item)
                presets_changed = True
                extra.update({'preset': len(presets), 'timer_preset': item})

            elif action == 'delete_preset':
                preset_raw = _timer_get_ci(body, 'preset', 'preset_index', 'index', 'number')
                try:
                    preset_number = int(preset_raw)
                except Exception:
                    return {'ok': False, 'error': 'preset must be an integer (1-based)'}, 400
                if len(presets) <= 1:
                    return {'ok': False, 'error': 'at least one timer preset is required'}, 400
                preset_index = preset_number - 1
                if preset_index < 0 or preset_index >= len(presets):
                    return {'ok': False, 'error': f'preset out of range (1..{len(presets)})', 'preset_count': len(presets)}, 400
                removed = presets.pop(preset_index)
                presets_changed = True
                stream = _cfg_int(cfg, 'stream_start_preset', 0, min_value=0)
                if stream == preset_number:
                    config_changes['stream_start_preset'] = 0
                elif stream > preset_number:
                    config_changes['stream_start_preset'] = stream - 1
                extra.update({'preset': preset_number, 'removed': removed})

            elif action == 'move_preset':
                try:
                    src = int(_timer_get_ci(body, 'preset', 'from', 'source'))
                except Exception:
                    return {'ok': False, 'error': 'preset must be an integer (1-based)'}, 400
                direction = str(body.get('direction') or '').strip().lower()
                if _timer_has_ci(body, 'to', 'target'):
                    try:
                        dst = int(_timer_get_ci(body, 'to', 'target'))
                    except Exception:
                        return {'ok': False, 'error': 'to must be an integer (1-based)'}, 400
                elif direction == 'up':
                    dst = src - 1
                elif direction == 'down':
                    dst = src + 1
                else:
                    return {'ok': False, 'error': 'move_preset requires direction or to'}, 400

                if src < 1 or src > len(presets) or dst < 1 or dst > len(presets):
                    return {'ok': False, 'error': f'preset out of range (1..{len(presets)})', 'preset_count': len(presets)}, 400
                if src != dst:
                    item = presets.pop(src - 1)
                    presets.insert(dst - 1, item)
                    presets_changed = True
                    stream = _cfg_int(cfg, 'stream_start_preset', 0, min_value=0)
                    if stream == src:
                        config_changes['stream_start_preset'] = dst
                    elif src < stream <= dst:
                        config_changes['stream_start_preset'] = stream - 1
                    elif dst <= stream < src:
                        config_changes['stream_start_preset'] = stream + 1
                extra.update({'preset': src, 'to': dst})

            elif action == 'adjust_all_presets':
                raw_delta = _timer_get_ci(body, 'delta_minutes', 'delta', 'minutes')
                if raw_delta is None:
                    raw_duration = _timer_get_ci(body, 'duration', 'amount')
                    parsed_duration, duration_err = _parse_timer_duration_minutes(raw_duration)
                    if duration_err:
                        return {'ok': False, 'error': duration_err}, 400
                    sign_raw = str(_timer_get_ci(body, 'sign', 'direction') or '+').strip().lower()
                    sign = -1 if sign_raw in ('-', 'minus', 'subtract', 'remove', 'down') else 1
                    delta_minutes = sign * int(parsed_duration or 0)
                else:
                    try:
                        delta_minutes = int(raw_delta)
                    except Exception:
                        return {'ok': False, 'error': 'delta_minutes must be an integer'}, 400
                if delta_minutes == 0:
                    return {'ok': False, 'error': 'delta_minutes must not be 0'}, 400
                if abs(delta_minutes) > 24 * 60:
                    return {'ok': False, 'error': 'delta_minutes must be between -1440 and 1440'}, 400

                adjusted = 0
                skipped: list[int] = []
                updated_presets: list = []
                for idx, preset in enumerate(presets, start=1):
                    current = dict(preset) if isinstance(preset, dict) else {
                        'time': str(preset or '').strip(),
                        'name': str(preset or '').strip(),
                    }
                    current_minutes = _time_hhmm_to_minutes(str(current.get('time', '')).strip())
                    if current_minutes is None:
                        skipped.append(idx)
                        updated_presets.append(preset)
                        continue
                    current['time'] = _minutes_to_time_hhmm(current_minutes + delta_minutes)
                    if not str(current.get('name', '')).strip():
                        current['name'] = current['time']
                    updated_presets.append(current)
                    adjusted += 1

                if adjusted < 1:
                    return {'ok': False, 'error': 'no valid timer presets were available to adjust'}, 400

                if updated_presets != presets:
                    presets = updated_presets
                    presets_changed = True
                extra.update({
                    'delta_minutes': delta_minutes,
                    'adjusted_count': adjusted,
                    'skipped_presets': skipped,
                })

            elif action == 'set_stream_start_preset':
                try:
                    value = int(body.get('stream_start_preset', body.get('preset', 0)) or 0)
                except Exception:
                    return {'ok': False, 'error': 'stream_start_preset must be an integer (1-based)'}, 400
                if value != 0 and not (1 <= value <= len(presets)):
                    return {'ok': False, 'error': f'stream_start_preset out of range (1..{len(presets)})'}, 400
                config_changes['stream_start_preset'] = value
                extra.update({'stream_start_preset': value})

            else:
                return {'ok': False, 'error': f'unknown timer action: {action}'}, 400

            if presets_changed:
                if not hasattr(utils, 'save_timer_presets'):
                    return {'ok': False, 'error': 'timer preset storage is not available'}, 500
                utils.save_timer_presets(presets)

            if config_changes:
                cfg, config_changed = _save_timer_config_changes(cfg, config_changes)
            else:
                config_changed = False

            if presets_changed:
                _queue_companion_timer_variable_sync(cfg=cfg, presets=presets)

            extra.update({
                'presets_changed': presets_changed,
                'config_changed': config_changed,
            })
            if presets_changed or config_changed:
                timer_action_name = {
                    'replace_all': 'timers.preset.replace_all',
                    'set_all': 'timers.preset.replace_all',
                    'update_preset': 'timers.preset.update',
                    'create_preset': 'timers.preset.create',
                    'delete_preset': 'timers.preset.delete',
                    'move_preset': 'timers.preset.move',
                    'adjust_all_presets': 'timers.preset.adjust_all',
                    'set_stream_start_preset': 'timers.stream_start.update',
                }.get(action, f'timers.{action}')
                preset_target = extra.get('preset') if action != 'set_stream_start_preset' else extra.get('stream_start_preset')
                if action in ('replace_all', 'set_all'):
                    timer_summary = f"Replaced timer presets ({len(presets)} total)"
                    target_type = 'timer_presets'
                    preset_target = 'timer_presets.json'
                elif action == 'update_preset':
                    timer_summary = f"Updated timer preset #{extra.get('preset')}"
                    target_type = 'timer_preset'
                elif action == 'create_preset':
                    timer_summary = f"Created timer preset #{extra.get('preset')}"
                    target_type = 'timer_preset'
                elif action == 'delete_preset':
                    timer_summary = f"Deleted timer preset #{extra.get('preset')}"
                    target_type = 'timer_preset'
                elif action == 'move_preset':
                    timer_summary = f"Moved timer preset #{extra.get('preset')} to #{extra.get('to')}"
                    target_type = 'timer_preset'
                elif action == 'adjust_all_presets':
                    timer_summary = f"Adjusted {extra.get('adjusted_count', 0)} timer presets by {extra.get('delta_minutes')} minutes"
                    target_type = 'timer_presets'
                    preset_target = 'timer_presets.json'
                elif action == 'set_stream_start_preset':
                    timer_summary = f"Updated stream start timer preset to #{extra.get('stream_start_preset') or 'none'}"
                    target_type = 'timer_config'
                else:
                    timer_summary = f"Updated timers ({action})"
                    target_type = 'timer_presets'
                try:
                    log_event(
                        timer_action_name,
                        timer_summary,
                        source='api',
                        status='success',
                        target_type=target_type,
                        target_id=preset_target,
                        details={'action': action, 'changes': extra, 'config_changes': config_changes},
                    )
                except Exception:
                    pass
            payload = _timer_state_payload(cfg=cfg, presets=presets, **extra)

        except Exception as e:
            return {'ok': False, 'error': str(e)}, 500

    if apply_after is not None:
        preset_number, apply_cfg, apply_presets, updated, time_raw, time_was_relative = apply_after
        try:
            log_event(
                'timers.preset.apply',
                f"Applied timer preset #{preset_number}",
                source='api',
                status='info',
                target_type='timer_preset',
                target_id=preset_number,
                details={'preset': preset_number, 'time': updated.get('time'), 'time_input': str(time_raw) if time_was_relative else None},
            )
        except Exception:
            pass
        apply_payload, status = _apply_timer_preset_number(
            preset_number=preset_number,
            cfg=apply_cfg,
            presets=apply_presets,
        )
        if isinstance(apply_payload, dict):
            apply_payload.update({
                'timer_preset': updated,
                'timer_presets': apply_presets,
                'preset_count': len(apply_presets),
                'updated_then_applied': True,
                'time_input': str(time_raw) if time_was_relative else None,
            })
        return apply_payload, status

    return payload, 200


@app.route('/api/timers', methods=['GET'])
def api_get_timers():
    try:
        cfg = utils.get_config()
    except Exception:
        cfg = {}

    try:
        presets = utils.load_timer_presets() if hasattr(utils, 'load_timer_presets') else []
    except Exception:
        presets = []

    # Backward-compatible reads for legacy keys
    try:
        propresenter_timer_index = int(cfg.get('propresenter_timer_index', cfg.get('timer_index', 1)))
    except Exception:
        propresenter_timer_index = 1
    try:
        stream_start_preset = int(cfg.get('stream_start_preset', 0))
    except Exception:
        stream_start_preset = 0
    if stream_start_preset < 1:
        stream_start_preset = 0

    return jsonify({
        'propresenter_timer_index': propresenter_timer_index,
        'stream_start_preset': stream_start_preset,
        'timer_presets': presets,
    })


@app.route('/api/timers', methods=['POST'])
def api_set_timers():
    body = request.get_json(silent=True) or {}
    body['action'] = 'replace_all'
    payload, status = _mutate_timers(body)
    return jsonify(payload), status


@app.route('/api/timers/mutate', methods=['POST', 'PATCH'])
def api_mutate_timers():
    body = request.get_json(silent=True) or {}
    payload, status = _mutate_timers(body)
    return jsonify(payload), status


@app.route('/api/timers/preset', methods=['POST', 'PATCH'])
def api_update_timer_preset():
    """Update a single timer preset's time (and optionally name) by 1-based preset number.

    Body example:
      {"preset": 2, "time": "08:15"}
      {"preset": 2, "time": "08:15", "name": "Walk-in"}
    """

    body = request.get_json(silent=True) or {}
    if 'preset' not in body and 'index' not in body:
        preset_arg = request.args.get('preset') or request.args.get('index')
        if preset_arg is not None:
            body['preset'] = preset_arg
    if not _timer_has_ci(body, 'time', 'hhmm', 'value') and request.args.get('time') is not None:
        body['time'] = request.args.get('time')
    if not _timer_has_ci(body, 'apply', 'apply_now', 'applypreset') and request.args.get('apply') is not None:
        body['apply'] = request.args.get('apply')
    body['action'] = 'update_preset'
    payload, status = _mutate_timers(body)
    return jsonify(payload), status


def _apply_timer_preset_number(*, preset_number: int, cfg: dict, presets: list) -> tuple[dict, int]:
    """Core implementation for applying a timer preset by 1-based preset number.

    Returns (payload, http_status).
    """
    if not presets:
        return ({'ok': False, 'error': 'no presets configured (timer_presets.json is empty)', 'preset_count': 0}, 400)

    try:
        preset_index = int(preset_number) - 1
    except Exception:
        return ({'ok': False, 'error': 'preset must be an integer'}, 400)

    if preset_index < 0 or preset_index >= len(presets):
        return ({'ok': False, 'error': f'preset out of range (1..{len(presets)})', 'preset_count': len(presets)}, 400)

    selected = presets[preset_index]
    # Home dashboard: remember the last preset that was applied.
    try:
        _home_set_last_timer_preset(preset_number=int(preset_number), selected=selected)
    except Exception:
        pass
    if isinstance(selected, dict):
        time_str = str(selected.get('time', '')).strip()
    else:
        time_str = str(selected).strip()

    # Extract configured Companion presses for this preset and fire them
    # immediately, regardless of ProPresenter availability.
    try:
        preset_name = str(selected.get('name', '')).strip() if isinstance(selected, dict) else ''
    except Exception:
        preset_name = ''
    try:
        presses = selected.get('button_presses') if isinstance(selected, dict) else None
        if presses is None and isinstance(selected, dict) and 'buttonPresses' in selected:
            presses = selected.get('buttonPresses')
        if presses is None and isinstance(selected, dict) and 'actions' in selected:
            presses = selected.get('actions')
        if not isinstance(presses, list):
            presses = []
    except Exception:
        presses = []

    try:
        pp_timer_index = int(cfg.get('propresenter_timer_index', cfg.get('timer_index', 1)))
    except Exception:
        pp_timer_index = 1

    # ProPresenter's HTTP API timer IDs are 0-based indices. Keep the config
    # value human-friendly (1-based), but convert for API calls.
    # Backward compatibility: if someone configured 0 explicitly, keep it.
    pp_timer_id = pp_timer_index - 1 if pp_timer_index > 0 else 0

    # Fire configured Companion presses immediately, using the real timer id so
    # any prior scheduled action for this timer is cancelled consistently.
    try:
        press_info = _fire_timer_button_presses_now(
            pp_timer_id=pp_timer_id,
            preset_number=int(preset_number),
            preset_name=preset_name,
            time_str=time_str,
            button_presses=presses,
        )
    except Exception:
        press_info = {'fired': False, 'count': 0}

    # Keep original time validation for timer control, but don't prevent button presses.
    if not _validate_time_hhmm(time_str):
        return (
            {
                'ok': True,
                'preset': int(preset_number),
                'preset_count': len(presets),
                'time': time_str,
                'sequence': 'none',
                'set': False,
                'reset': False,
                'started': False,
                'button_presses': press_info,
                'propresenter': {
                    'ok': False,
                    'error': f'invalid preset time in config: {time_str}',
                },
            },
            200,
        )

    ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
    try:
        port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        return (
            {
                'ok': True,
                'preset': int(preset_number),
                'preset_count': len(presets),
                'time': time_str,
                'propresenter_timer_index': pp_timer_index,
                'propresenter_timer_id': pp_timer_id,
                'sequence': 'none',
                'set': False,
                'reset': False,
                'started': False,
                'button_presses': press_info,
                'propresenter': {
                    'ok': False,
                    'error': 'propresenter_port must be an integer',
                },
            },
            200,
        )

    # If ProPresenter client is missing, still succeed for Companion presses.
    if ProPresentor is None:
        try:
            log_event(
                'timers.propresenter.skip',
                'ProPresenter client not available; skipped timer control',
                source='api',
                status='warning',
                target_type='timer_preset',
                target_id=preset_number,
                details={'preset': preset_number, 'timer_id': pp_timer_id},
            )
        except Exception:
            pass
        return (
            {
                'ok': True,
                'preset': int(preset_number),
                'preset_count': len(presets),
                'time': time_str,
                'propresenter_timer_index': pp_timer_index,
                'propresenter_timer_id': pp_timer_id,
                'sequence': 'none',
                'set': False,
                'reset': False,
                'started': False,
                'button_presses': press_info,
                'propresenter': {
                    'ok': False,
                    'error': 'propresentor client not available',
                },
            },
            200,
        )

    # Some older ProPresenter versions have a bug where a normal `start` right
    # after setting/resetting a timer doesn't reliably start.
    # Control behavior via config.json:
    #   - propresenter_is_latest: true => normal flow (set -> reset -> start)
    #                             false => legacy workaround flow
    #   - propresenter_timer_wait_stop_ms (default 200)
    #   - propresenter_timer_wait_set_ms  (default 600)
    #   - propresenter_timer_wait_reset_ms(default 1000)
    is_latest = _cfg_bool(cfg, 'propresenter_is_latest', True)
    wait_stop_ms = _cfg_int(cfg, 'propresenter_timer_wait_stop_ms', 200, min_value=0, max_value=60000)
    wait_set_ms = _cfg_int(cfg, 'propresenter_timer_wait_set_ms', 600, min_value=0, max_value=60000)
    wait_reset_ms = _cfg_int(cfg, 'propresenter_timer_wait_reset_ms', 1000, min_value=0, max_value=60000)

    pp = ProPresentor(ip, port)

    if not is_latest:
        # Legacy workaround sequence:
        # Stop Timer -> wait -> Set -> wait -> Reset -> wait -> Start
        stop_ok = bool(pp.timer_operation(pp_timer_id, 'stop'))
        if not stop_ok:
            return (
                {
                    'ok': True,
                    'error': 'failed to stop timer (legacy sequence)',
                    'preset': int(preset_number),
                    'preset_count': len(presets),
                    'time': time_str,
                    'propresenter_timer_index': pp_timer_index,
                    'propresenter_timer_id': pp_timer_id,
                    'sequence': 'legacy',
                    'stop': False,
                    'set': False,
                    'reset': False,
                    'started': False,
                    'button_presses': press_info,
                    'propresenter_ip': ip,
                    'propresenter_port': port,
                    'waits_ms': {'after_stop': wait_stop_ms, 'after_set': wait_set_ms, 'after_reset': wait_reset_ms},
                },
                200,
            )

        if wait_stop_ms:
            time.sleep(wait_stop_ms / 1000.0)

        set_ok = bool(pp.SetCountdownToTime(pp_timer_id, time_str))
        if not set_ok:
            return (
                {
                    'ok': True,
                    'error': 'failed to set timer (legacy sequence)',
                    'preset': int(preset_number),
                    'preset_count': len(presets),
                    'time': time_str,
                    'propresenter_timer_index': pp_timer_index,
                    'propresenter_timer_id': pp_timer_id,
                    'sequence': 'legacy',
                    'stop': True,
                    'set': False,
                    'reset': False,
                    'started': False,
                    'button_presses': press_info,
                    'propresenter_ip': ip,
                    'propresenter_port': port,
                    'waits_ms': {'after_stop': wait_stop_ms, 'after_set': wait_set_ms, 'after_reset': wait_reset_ms},
                },
                200,
            )

        if wait_set_ms:
            time.sleep(wait_set_ms / 1000.0)

        reset_ok = bool(pp.timer_operation(pp_timer_id, 'reset'))
        if not reset_ok:
            return (
                {
                    'ok': True,
                    'error': 'timer set, but failed to reset (legacy sequence)',
                    'preset': int(preset_number),
                    'preset_count': len(presets),
                    'time': time_str,
                    'propresenter_timer_index': pp_timer_index,
                    'propresenter_timer_id': pp_timer_id,
                    'sequence': 'legacy',
                    'stop': True,
                    'set': True,
                    'reset': False,
                    'started': False,
                    'button_presses': press_info,
                    'propresenter_ip': ip,
                    'propresenter_port': port,
                    'waits_ms': {'after_stop': wait_stop_ms, 'after_set': wait_set_ms, 'after_reset': wait_reset_ms},
                },
                200,
            )

        if wait_reset_ms:
            time.sleep(wait_reset_ms / 1000.0)

        start_ok = bool(pp.timer_operation(pp_timer_id, 'start'))
        if not start_ok:
            return (
                {
                    'ok': True,
                    'error': 'timer set, but failed to start (legacy sequence)',
                    'preset': int(preset_number),
                    'preset_count': len(presets),
                    'time': time_str,
                    'propresenter_timer_index': pp_timer_index,
                    'propresenter_timer_id': pp_timer_id,
                    'sequence': 'legacy',
                    'stop': True,
                    'set': True,
                    'reset': True,
                    'started': False,
                    'button_presses': press_info,
                    'propresenter_ip': ip,
                    'propresenter_port': port,
                    'waits_ms': {'after_stop': wait_stop_ms, 'after_set': wait_set_ms, 'after_reset': wait_reset_ms},
                },
                200,
            )

        return (
            {
                'ok': True,
                'preset': int(preset_number),
                'preset_count': len(presets),
                'time': time_str,
                'propresenter_timer_index': pp_timer_index,
                'propresenter_timer_id': pp_timer_id,
                'sequence': 'legacy',
                'stop': True,
                'set': True,
                'reset': True,
                'started': True,
                'button_presses': press_info,
                'propresenter_ip': ip,
                'propresenter_port': port,
                'waits_ms': {'after_stop': wait_stop_ms, 'after_set': wait_set_ms, 'after_reset': wait_reset_ms},
            },
            200,
        )

    # Normal flow (latest versions): set -> reset -> start
    set_ok = bool(pp.SetCountdownToTime(pp_timer_id, time_str))
    if not set_ok:
        return (
            {
                'ok': True,
                'error': 'failed to set timer (check ProPresenter connection and timer index)',
                'preset': int(preset_number),
                'preset_count': len(presets),
                'time': time_str,
                'propresenter_timer_index': pp_timer_index,
                'propresenter_timer_id': pp_timer_id,
                'sequence': 'normal',
                'set': False,
                'reset': False,
                'started': False,
                'button_presses': press_info,
                'propresenter_ip': ip,
                'propresenter_port': port,
            },
            200,
        )

    # ProPresenter often needs a reset/restart after changing timer config
    # for the UI to reflect the new time correctly.
    reset_ok = bool(pp.timer_operation(pp_timer_id, 'reset'))
    if not reset_ok:
        return (
            {
                'ok': True,
                'error': 'timer set, but failed to reset (check ProPresenter timer state/permissions)',
                'preset': int(preset_number),
                'preset_count': len(presets),
                'time': time_str,
                'propresenter_timer_index': pp_timer_index,
                'propresenter_timer_id': pp_timer_id,
                'sequence': 'normal',
                'set': True,
                'reset': False,
                'started': False,
                'button_presses': press_info,
                'propresenter_ip': ip,
                'propresenter_port': port,
            },
            200,
        )

    # Start countdown immediately (per OpenAPI: GET /v1/timer/{id}/{operation})
    start_ok = bool(pp.timer_operation(pp_timer_id, 'start'))

    if not start_ok:
        return (
            {
                'ok': True,
                'error': 'timer set, but failed to start (check ProPresenter timer state/permissions)',
                'preset': int(preset_number),
                'preset_count': len(presets),
                'time': time_str,
                'propresenter_timer_index': pp_timer_index,
                'propresenter_timer_id': pp_timer_id,
                'sequence': 'normal',
                'set': True,
                'reset': True,
                'started': False,
                'button_presses': press_info,
                'propresenter_ip': ip,
                'propresenter_port': port,
            },
            200,
        )

    return (
        {
            'ok': True,
            'preset': int(preset_number),
            'preset_count': len(presets),
            'time': time_str,
            'propresenter_timer_index': pp_timer_index,
            'propresenter_timer_id': pp_timer_id,
            'sequence': 'normal',
            'set': True,
            'reset': True,
            'started': True,
            'button_presses': press_info,
            'propresenter_ip': ip,
            'propresenter_port': port,
        },
        200,
    )


@app.route('/api/timers/apply', methods=['POST'])
def api_apply_timer_preset():
    """Apply a timer preset selected by Companion and start the timer.

        Companion can call this endpoint with either:
            - JSON body: {"preset": 1}
            - Query param: ?preset=1

    Notes:
    - The preset value is ALWAYS 1-based: 1 selects the first entry.
    - Presets are stored in `timer_presets.json`.
    - The ProPresenter timer to update is `propresenter_timer_index` in config.json.
    """

    # Always log the incoming request (Companion button press visibility)
    # regardless of debug mode.
    try:
        body_for_log = request.get_json(silent=True)
    except Exception:
        body_for_log = None
    try:
        log_event(
            'companion.request.timers.apply',
            'Companion requested timer preset apply',
            source='companion',
            status='info',
            target_type='timer_preset',
            details={'args': dict(request.args), 'json': body_for_log},
        )
    except Exception:
        pass

    body = body_for_log or {}

    def _get_body_value_ci(d: dict, *keys: str):
        try:
            for k in keys:
                if k in d:
                    return d.get(k)
            lower = {str(k).lower(): v for k, v in d.items()}
            for k in keys:
                lk = str(k).lower()
                if lk in lower:
                    return lower.get(lk)
        except Exception:
            return None
        return None

    # Always treat the provided integer as 1-based (1 selects first preset).
    # Accept several common key names (including Companion-style TimerIndex).
    preset_raw = _get_body_value_ci(
        body,
        'preset',
        'preset_index',
        'index',
        'value',
        'timerindex',
        'timer_index',
        'TimerIndex',
    )
    if preset_raw is None:
        preset_raw = (
            request.args.get('preset')
            or request.args.get('timerindex')
            or request.args.get('timer_index')
            or request.args.get('TimerIndex')
        )

    try:
        preset_number = int(preset_raw)
    except Exception:
        return jsonify({'ok': False, 'error': 'preset must be an integer'}), 400

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    try:
        presets = utils.load_timer_presets() if hasattr(utils, 'load_timer_presets') else []
    except Exception:
        presets = []

    payload, status = _apply_timer_preset_number(preset_number=preset_number, cfg=cfg, presets=presets)
    return jsonify(payload), status


def _resolve_pp_timer_id_from_body(body: dict, cfg: dict) -> tuple[int | None, str | None]:
    """Resolve a ProPresenter timer id (0-based) from request body/config.

    Accepts either:
      - timer_id (0-based), or
      - timer_index / propresenter_timer_index (1-based)
    """
    raw_id = body.get('timer_id')
    if raw_id is not None:
        try:
            return int(raw_id), None
        except Exception:
            return None, 'timer_id must be an integer'

    raw_idx = body.get('timer_index')
    if raw_idx is None:
        raw_idx = body.get('propresenter_timer_index')
    if raw_idx is None:
        raw_idx = cfg.get('propresenter_timer_index', cfg.get('timer_index', 1))

    try:
        idx = int(raw_idx)
    except Exception:
        return None, 'timer_index must be an integer'

    # Config is human-friendly 1-based; ProPresenter API uses 0-based ids.
    return (idx - 1 if idx > 0 else 0), None


@app.route('/api/propresenter/timer/set', methods=['POST'])
def api_prop_set_timer():
    body = request.get_json(silent=True) or {}

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    time_str = str(body.get('time') or body.get('hhmm') or body.get('value') or '').strip()
    if not _validate_time_hhmm(time_str):
        return jsonify({'ok': False, 'error': 'time must be HH:MM'}), 400

    timer_id, err = _resolve_pp_timer_id_from_body(body, cfg)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    try:
        ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
        port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid propresenter_ip/propresenter_port in config'}), 500

    if ProPresentor is None:
        return jsonify({'ok': False, 'error': 'propresentor client not available'}), 500

    pp = ProPresentor(ip, port)
    set_ok = bool(pp.SetCountdownToTime(timer_id, time_str))

    do_reset = bool(body.get('reset', False))
    reset_ok = None
    if do_reset:
        reset_ok = bool(pp.timer_operation(timer_id, 'reset'))

    try:
        log_event(
            'propresenter.timer.set',
            f"Set ProPresenter timer {timer_id} to {time_str}",
            source='api',
            status='success' if set_ok else 'failure',
            target_type='propresenter_timer',
            target_id=timer_id,
            details={'timer_id': timer_id, 'time': time_str, 'reset': reset_ok},
        )
    except Exception:
        pass

    return jsonify({'ok': True, 'timer_id': timer_id, 'time': time_str, 'set': set_ok, 'reset': reset_ok, 'propresenter_ip': ip, 'propresenter_port': port})


@app.route('/api/propresenter/timer/start', methods=['POST'])
def api_prop_start_timer():
    body = request.get_json(silent=True) or {}

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    timer_id, err = _resolve_pp_timer_id_from_body(body, cfg)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    try:
        ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
        port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid propresenter_ip/propresenter_port in config'}), 500

    if ProPresentor is None:
        return jsonify({'ok': False, 'error': 'propresentor client not available'}), 500

    pp = ProPresentor(ip, port)
    ok = bool(pp.timer_operation(timer_id, 'start'))

    try:
        log_event(
            'propresenter.timer.start',
            f"Started ProPresenter timer {timer_id}",
            source='api',
            status='success' if ok else 'failure',
            target_type='propresenter_timer',
            target_id=timer_id,
            details={'timer_id': timer_id},
        )
    except Exception:
        pass

    return jsonify({'ok': True, 'timer_id': timer_id, 'started': ok, 'propresenter_ip': ip, 'propresenter_port': port})


@app.route('/api/propresenter/timer/stop', methods=['POST'])
def api_prop_stop_timer():
    body = request.get_json(silent=True) or {}

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    timer_id, err = _resolve_pp_timer_id_from_body(body, cfg)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    try:
        ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
        port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid propresenter_ip/propresenter_port in config'}), 500

    if ProPresentor is None:
        return jsonify({'ok': False, 'error': 'propresentor client not available'}), 500

    pp = ProPresentor(ip, port)
    ok = bool(pp.timer_operation(timer_id, 'stop'))

    try:
        log_event(
            'propresenter.timer.stop',
            f"Stopped ProPresenter timer {timer_id}",
            source='api',
            status='success' if ok else 'failure',
            target_type='propresenter_timer',
            target_id=timer_id,
            details={'timer_id': timer_id},
        )
    except Exception:
        pass

    return jsonify({'ok': True, 'timer_id': timer_id, 'stopped': ok, 'propresenter_ip': ip, 'propresenter_port': port})


@app.route('/api/propresenter/timer/reset', methods=['POST'])
def api_prop_reset_timer():
    body = request.get_json(silent=True) or {}

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    timer_id, err = _resolve_pp_timer_id_from_body(body, cfg)
    if err:
        return jsonify({'ok': False, 'error': err}), 400

    try:
        ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
        port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid propresenter_ip/propresenter_port in config'}), 500

    if ProPresentor is None:
        return jsonify({'ok': False, 'error': 'propresentor client not available'}), 500

    pp = ProPresentor(ip, port)
    ok = bool(pp.timer_operation(timer_id, 'reset'))

    try:
        log_event(
            'propresenter.timer.reset',
            f"Reset ProPresenter timer {timer_id}",
            source='api',
            status='success' if ok else 'failure',
            target_type='propresenter_timer',
            target_id=timer_id,
            details={'timer_id': timer_id},
        )
    except Exception:
        pass

    return jsonify({'ok': True, 'timer_id': timer_id, 'reset': ok, 'propresenter_ip': ip, 'propresenter_port': port})


@app.route('/api/propresenter/stage/message', methods=['POST'])
def api_prop_stage_message():
    """Send a stage display message to ProPresenter."""
    body = request.get_json(silent=True) or {}
    msg = body.get('message')
    if msg is None:
        msg = body.get('text') or body.get('value')

    try:
        message = str(msg or '').strip()
    except Exception:
        message = ''
    if not message:
        return jsonify({'ok': False, 'error': 'message is required'}), 400

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    try:
        ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
        port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid propresenter_ip/propresenter_port in config'}), 500

    if ProPresentor is None:
        return jsonify({'ok': False, 'error': 'propresentor client not available'}), 500

    pp = ProPresentor(ip, port)
    sent = bool(pp.set_stage_message(message))
    detail = getattr(pp, 'last_stage_message_error', None)

    try:
        extra = f" ({detail})" if detail and not sent else ""
        log_event(
            'propresenter.stage.message',
            f"Sent ProPresenter stage message: {'OK' if sent else 'FAIL'}{extra}",
            source='api',
            status='success' if sent else 'failure',
            target_type='propresenter_stage',
            details={'sent': sent, 'detail': detail, 'message': message},
        )
    except Exception:
        pass

    return jsonify({
        'ok': True,
        'message': message,
        'sent': sent,
        'detail': detail if not sent else None,
        'propresenter_ip': ip,
        'propresenter_port': port,
    })


@app.route('/api/propresenter/stage/clear', methods=['POST'])
def api_prop_stage_clear():
    """Clear the stage display message in ProPresenter."""
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    try:
        ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
        port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid propresenter_ip/propresenter_port in config'}), 500

    if ProPresentor is None:
        return jsonify({'ok': False, 'error': 'propresentor client not available'}), 500

    pp = ProPresentor(ip, port)
    cleared = bool(pp.clear_stage_message())

    try:
        log_event(
            'propresenter.stage.clear',
            'Cleared ProPresenter stage message',
            source='api',
            status='success' if cleared else 'failure',
            target_type='propresenter_stage',
            details={'cleared': cleared},
        )
    except Exception:
        pass

    return jsonify({
        'ok': True,
        'cleared': cleared,
        'propresenter_ip': ip,
        'propresenter_port': port,
    })


@app.route('/api/propresenter/stage/stream_start', methods=['POST'])
def api_prop_stage_stream_start():
    """Send stream-start stage message based on the configured timer preset."""
    # Log incoming request (Companion visibility)
    try:
        body_for_log = request.get_json(silent=True)
    except Exception:
        body_for_log = None
    try:
        log_event(
            'companion.request.propresenter.stream_start',
            'Companion requested stream-start stage message',
            source='companion',
            status='info',
            target_type='propresenter_stage',
            details={'args': dict(request.args), 'json': body_for_log},
        )
    except Exception:
        pass

    try:
        presets = list(utils.load_timer_presets()) if hasattr(utils, 'load_timer_presets') else []
    except Exception:
        presets = []

    if not presets:
        return jsonify({'ok': False, 'error': 'no presets configured (timer_presets.json is empty)'}), 400

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    resolved = _resolve_stream_start_preset(cfg, presets)
    if not resolved:
        return jsonify({'ok': False, 'error': 'stream_start_preset not configured'}), 400
    preset_number, preset = resolved

    message = _build_stream_start_message(preset)
    if not message:
        return jsonify({'ok': False, 'error': 'invalid stream_start_preset time'}), 400

    try:
        ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
        port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid propresenter_ip/propresenter_port in config'}), 500

    if ProPresentor is None:
        return jsonify({'ok': False, 'error': 'propresentor client not available'}), 500

    pp = ProPresentor(ip, port)
    sent = bool(pp.set_stage_message(message))
    detail = getattr(pp, 'last_stage_message_error', None)

    try:
        extra = f" ({detail})" if detail and not sent else ""
        log_event(
            'propresenter.stage.stream_start',
            f"Sent stream-start stage message from preset #{preset_number}: {'OK' if sent else 'FAIL'}{extra}",
            source='api',
            status='success' if sent else 'failure',
            target_type='timer_preset',
            target_id=preset_number,
            details={'preset': preset_number, 'sent': sent, 'detail': detail, 'message': message},
        )
    except Exception:
        pass

    return jsonify({
        'ok': True,
        'preset_number': preset_number,
        'preset_name': (preset or {}).get('name', ''),
        'preset_time': (preset or {}).get('time', ''),
        'message': message,
        'sent': sent,
        'detail': detail if not sent else None,
        'propresenter_ip': ip,
        'propresenter_port': port,
    })


@app.route('/api/videohub/ping', methods=['GET'])
def api_videohub_ping():
    """Best-effort connectivity check to the configured VideoHub."""
    vh = _get_videohub_client_from_config()
    if vh is None:
        return jsonify({'ok': False, 'error': "VideoHub not configured (set videohub_ip in config.json)"}), 400
    ok = vh.ping()
    return jsonify({'ok': bool(ok)})


@app.route('/api/videohub/route', methods=['POST'])
def api_videohub_route():
    """Route an input to an output on the configured VideoHub.

    Accepts JSON body (preferred):
      {"output": 1, "input": 3, "monitor": false, "zero_based": false}

    Notes:
    - Default interpretation is 1-based for humans.
    - Set zero_based=true to pass VideoHub-native indexes.
    """

    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}

    try:
        log_event(
            'companion.request.videohub.route',
            'Companion requested VideoHub route change',
            source='companion',
            status='info',
            target_type='videohub_route',
            details={'args': dict(request.args), 'json': body},
        )
    except Exception:
        pass

    vh = _get_videohub_client_from_config()
    if vh is None:
        return jsonify({'ok': False, 'error': "VideoHub not configured (set videohub_ip in config.json)"}), 400

    output_raw = body.get('output') or body.get('destination') or request.args.get('output') or request.args.get('destination')
    input_raw = body.get('input') or body.get('source') or request.args.get('input') or request.args.get('source')

    try:
        output_n = int(output_raw)
        input_n = int(input_raw)
    except Exception:
        return jsonify({'ok': False, 'error': 'output and input must be integers'}), 400

    monitor = bool(body.get('monitor') or body.get('monitoring') or False)
    zero_based = bool(body.get('zero_based') or body.get('zerobased') or False)

    output_idx = output_n if zero_based else output_n - 1
    input_idx = input_n if zero_based else input_n - 1

    try:
        vh.route_video_output(output=output_idx, input_=input_idx, monitoring=monitor)
        if not monitor:
            verified = False
            for _ in range(3):
                time.sleep(0.12)
                if vh.verify_video_output_route(output=output_idx, input_=input_idx):
                    verified = True
                    break
            if not verified:
                raise RuntimeError(
                    f'VideoHub did not confirm output {output_n} was routed to input {input_n}'
                )
    except Exception as e:
        try:
            log_event(
                'videohub.route',
                f'VideoHub route failed: output {output_n} to input {input_n}',
                source='api',
                status='failure',
                target_type='videohub_route',
                target_id=str(output_n),
                details={'output': output_n, 'input': input_n, 'monitor': monitor, 'zero_based': zero_based, 'error': str(e)},
            )
        except Exception:
            pass
        return jsonify({'ok': False, 'error': str(e)}), 500

    try:
        _home_set_last_videohub_route(output=output_n, input_=input_n, monitor=monitor)
    except Exception:
        pass
    if not monitor:
        _invalidate_videohub_state_snapshot(output_idx=output_idx, input_idx=input_idx)

    try:
        log_event(
            'videohub.route',
            f'Routed VideoHub output {output_n} to input {input_n}',
            source='api',
            status='success',
            target_type='videohub_route',
            target_id=str(output_n),
            details={'output': output_n, 'input': input_n, 'monitor': monitor, 'zero_based': zero_based},
        )
    except Exception:
        pass

    return jsonify({'ok': True, 'output': output_n, 'input': input_n, 'monitor': monitor, 'zero_based': zero_based})


@app.route('/api/events/<int:ident>', methods=['DELETE'])
def api_delete_event_ui(ident: int):
    """Allow the web UI to delete an event from the configured EVENTS_FILE.

    This mirrors the CLI delete behavior so the UI can operate without
    running the separate API server.
    """
    try:
        from package.apps.calendar import storage
    except Exception:
        return jsonify({'ok': False, 'error': 'storage unavailable'}), 500

    try:
        cfg = utils.get_config()
        events_file = cfg.get('EVENTS_FILE', storage.DEFAULT_EVENTS_FILE)
        events = storage.load_events(events_file)
        matching = [e for e in events if getattr(e, 'id', None) == ident]
        if not matching:
            return jsonify({'ok': False, 'error': 'Event not found'}), 404
        ev = matching[0]
        events.remove(ev)
        storage.save_events(events, events_file)
        try:
            log_event(
                'calendar.event.delete',
                f"Deleted event '{ev.name}'",
                source='web',
                status='success',
                target_type='calendar_event',
                target_id=ident,
                details={
                    'event_id': ident,
                    'event_name': ev.name,
                    'events_file': events_file,
                    'trigger_count': len(getattr(ev, 'times', []) or []),
                },
            )
        except Exception:
            pass
        return jsonify({'removed': True, 'id': ident, 'name': ev.name})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/events/<int:ident>', methods=['GET'])
def api_get_event_ui(ident: int):
    """Return a single event by id for the UI to edit."""
    try:
        from package.apps.calendar import storage
    except Exception:
        return jsonify({'ok': False, 'error': 'storage unavailable'}), 500

    try:
        cfg = utils.get_config()
        events_file = cfg.get('EVENTS_FILE', storage.DEFAULT_EVENTS_FILE)
        events = storage.load_events(events_file)
        matching = [e for e in events if getattr(e, 'id', None) == ident]
        if not matching:
            return jsonify({'ok': False, 'error': 'Event not found'}), 404
        e = matching[0]
        out = {
            'id': getattr(e, 'id', None),
            'name': e.name,
            'date': e.date.strftime('%Y-%m-%d'),
            'time': e.time.strftime('%H:%M:%S'),
            'repeating': e.repeating,
            'active': getattr(e, 'active', True),
            'day': getattr(e, 'day').name if getattr(e, 'day', None) is not None else 'Monday',
            'times': [
                {
                    'minutes': t.minutes,
                    'typeOfTrigger': getattr(t.typeOfTrigger, 'name', str(t.typeOfTrigger)),
                    'uid': getattr(t, 'uid', None),
                    'name': str(getattr(t, 'name', '') or '').strip(),
                    'enabled': bool(getattr(t, 'enabled', True)),
                    'actionType': str(getattr(t, 'actionType', 'companion') or 'companion').lower(),
                    'buttonURL': t.buttonURL if str(getattr(t, 'actionType', 'companion') or 'companion').lower() == 'companion' else '',
                    'api': getattr(t, 'api', None) if str(getattr(t, 'actionType', 'companion') or 'companion').lower() == 'api' else None,
                    'timer': getattr(t, 'timer', None) if str(getattr(t, 'actionType', 'companion') or 'companion').lower() == 'timer' else None,
                }
                for t in e.times
            ],
        }
        resp = jsonify(out)
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/events/<int:ident>', methods=['PUT'])
def api_update_event_ui(ident: int):
    """Update an existing event (from the UI) and persist to EVENTS_FILE."""
    try:
        body = request.get_json() or {}
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid json'}), 400

    try:
        from package.apps.calendar import storage
        from package.apps.calendar.models import Event, TimeOfTrigger, TypeofTime, WeekDay
    except Exception as e:
        return jsonify({'ok': False, 'error': 'storage/models unavailable: ' + str(e)}), 500

    try:
        cfg = utils.get_config()
        events_file = cfg.get('EVENTS_FILE', storage.DEFAULT_EVENTS_FILE)
        events = storage.load_events(events_file)
        matching = [e for e in events if getattr(e, 'id', None) == ident]
        if not matching:
            return jsonify({'ok': False, 'error': 'Event not found'}), 404
        ev = matching[0]

        name = body.get('name', ev.name)
        day = body.get('day', ev.day.name if getattr(ev, 'day', None) is not None else 'Monday')
        date_str = body.get('date', ev.date.strftime('%Y-%m-%d'))
        time_str = body.get('time', ev.time.strftime('%H:%M:%S'))
        repeating = bool(body.get('repeating', ev.repeating))
        active = bool(body.get('active', getattr(ev, 'active', True)))

        from datetime import datetime
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        time_obj = datetime.strptime(time_str, '%H:%M:%S').time() if len(time_str.split(':'))==3 else datetime.strptime(time_str, '%H:%M').time()

        # Support partial updates (e.g., toggling active) without requiring the
        # client to resend the full trigger list.
        if 'times' not in body: 
            times = ev.times 
        else: 
            times = [] 
            import re as _re 
            for t in body.get('times', []): 
                typ_name = t.get('typeOfTrigger', 'AT') 
                if typ_name not in TypeofTime.__members__: 
                    return jsonify({'ok': False, 'error': f"Invalid typeOfTrigger: {typ_name}"}), 400 
                typ = TypeofTime[typ_name] 
 
                # Minutes: if type is AT, always save 0 (ignore client input) 
                if typ_name == 'AT': 
                    mins = 0 
                else: 
                    try: 
                        mins = int(t.get('minutes', 0) or 0) 
                    except Exception: 
                        return jsonify({'ok': False, 'error': f"Invalid minutes value: {t.get('minutes')}"}), 400 
                    if mins < 0: 
                        return jsonify({'ok': False, 'error': f"Minutes must be >= 0: {mins}"}), 400 
 
                t2 = dict(t) 
                t2['typeOfTrigger'] = typ_name 
                t2['minutes'] = mins 
                t3, err = _normalize_trigger_action_spec(t2) 
                if err: 
                    return jsonify({'ok': False, 'error': err}), 400 
                if not t3: 
                    continue 
                action_type = str(t3.get('actionType') or 'companion').lower() 
                btn_final = str(t3.get('buttonURL') or '') if action_type == 'companion' else '' 
                api_obj = t3.get('api') if action_type == 'api' else None 
                timer_obj = t3.get('timer') if action_type == 'timer' else None
                enabled = bool(t3.get('enabled', True)) 
                trig_name = str(t3.get('name') or '').strip() 
                trig_uid = str(t3.get('uid') or '').strip() or _uuid4_str() 
 
                times.append( 
                    TimeOfTrigger( 
                        mins, 
                        typ, 
                        btn_final, 
                        name=trig_name, 
                        uid=trig_uid, 
                        actionType=action_type, 
                        api=api_obj, 
                        timer=timer_obj,
                        enabled=enabled, 
                    ) 
                ) 

        # replace fields on existing event object
        old_name = ev.name
        old_active = bool(getattr(ev, 'active', True))
        old_trigger_count = len(getattr(ev, 'times', []) or [])
        ev.name = name
        ev.day = WeekDay[day] if day in WeekDay.__members__ else WeekDay.Monday
        ev.date = date_obj
        ev.time = time_obj
        ev.repeating = repeating
        ev.active = active
        ev.times = times

        storage.save_events(events, events_file)
        try:
            log_event(
                'calendar.event.update',
                f"Updated event '{ev.name}'",
                source='web',
                status='success',
                target_type='calendar_event',
                target_id=ident,
                details={
                    'event_id': ident,
                    'old_name': old_name,
                    'event_name': ev.name,
                    'old_active': old_active,
                    'active': bool(ev.active),
                    'date': ev.date.strftime('%Y-%m-%d'),
                    'time': ev.time.strftime('%H:%M:%S'),
                    'repeating': bool(ev.repeating),
                    'old_trigger_count': old_trigger_count,
                    'trigger_count': len(ev.times or []),
                    'events_file': events_file,
                },
            )
        except Exception:
            pass
        return jsonify({'ok': True, 'id': ident})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/ui/events', methods=['POST'])
def api_create_event_ui():
    """Create an event from the UI and persist to the configured EVENTS_FILE."""
    try:
        body = request.get_json() or {}
    except Exception:
        return jsonify({'ok': False, 'error': 'invalid json'}), 400

    try:
        # require storage and models
        from package.apps.calendar import storage
        from package.apps.calendar.models import Event, TimeOfTrigger, TypeofTime, WeekDay
    except Exception as e:
        return jsonify({'ok': False, 'error': 'storage/models unavailable: ' + str(e)}), 500

    try:
        cfg = utils.get_config()
        events_file = cfg.get('EVENTS_FILE', storage.DEFAULT_EVENTS_FILE)
        events = storage.load_events(events_file)

        # determine new id
        max_id = 0
        for e in events:
            if isinstance(getattr(e, 'id', None), int) and e.id > max_id:
                max_id = e.id
        new_id = max_id + 1

        name = body.get('name', '')
        day = body.get('day', 'Monday')
        date_str = body.get('date', '1970-01-01')
        time_str = body.get('time', '00:00:00')
        repeating = bool(body.get('repeating', False))
        active = bool(body.get('active', True))

        from datetime import datetime
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        time_obj = datetime.strptime(time_str, '%H:%M:%S').time() if len(time_str.split(':'))==3 else datetime.strptime(time_str, '%H:%M').time()

        times = []
        import re as _re
        for t in body.get('times', []):
            # minutes must be 0 or positive integer
            # typeOfTrigger must map to TypeofTime
            typ_name = t.get('typeOfTrigger', 'AT')
            if typ_name not in TypeofTime.__members__:
                return jsonify({'ok': False, 'error': f"Invalid typeOfTrigger: {typ_name}"}), 400
            typ = TypeofTime[typ_name]

            # Minutes: if type is AT, always save 0 (ignore client input)
            if typ_name == 'AT':
                mins = 0
            else:
                try:
                    mins = int(t.get('minutes', 0) or 0)
                except Exception:
                    return jsonify({'ok': False, 'error': f"Invalid minutes value: {t.get('minutes')}"}), 400
                if mins < 0:
                    return jsonify({'ok': False, 'error': f"Minutes must be >= 0: {mins}"}), 400

            t2 = dict(t)
            t2['typeOfTrigger'] = typ_name
            t2['minutes'] = mins
            t3, err = _normalize_trigger_action_spec(t2)
            if err:
                return jsonify({'ok': False, 'error': err}), 400
            if not t3:
                continue
            action_type = str(t3.get('actionType') or 'companion').lower()
            btn_final = str(t3.get('buttonURL') or '') if action_type == 'companion' else ''
            api_obj = t3.get('api') if action_type == 'api' else None
            timer_obj = t3.get('timer') if action_type == 'timer' else None
            enabled = bool(t3.get('enabled', True))
            trig_name = str(t3.get('name') or '').strip()
            trig_uid = str(t3.get('uid') or '').strip() or _uuid4_str()

            times.append(
                TimeOfTrigger(
                    mins,
                    typ,
                    btn_final,
                    name=trig_name,
                    uid=trig_uid,
                    actionType=action_type,
                    api=api_obj,
                    timer=timer_obj,
                    enabled=enabled,
                )
            )

        ev = Event(name, new_id, WeekDay[day] if day in WeekDay.__members__ else WeekDay.Monday, date_obj, time_obj, repeating, times, active)
        events.append(ev)
        storage.save_events(events, events_file)
        try:
            log_event(
                'calendar.event.create',
                f"Created event '{ev.name}'",
                source='web',
                status='success',
                target_type='calendar_event',
                target_id=new_id,
                details={
                    'event_id': new_id,
                    'event_name': ev.name,
                    'active': bool(ev.active),
                    'date': ev.date.strftime('%Y-%m-%d'),
                    'time': ev.time.strftime('%H:%M:%S'),
                    'repeating': bool(ev.repeating),
                    'trigger_count': len(ev.times or []),
                    'events_file': events_file,
                },
            )
        except Exception:
            pass
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _register_api_v1_aliases() -> None:
    """Expose stable v1 aliases without changing legacy action payloads."""
    existing = {str(rule.rule) for rule in app.url_map.iter_rules()}
    for rule in list(app.url_map.iter_rules()):
        legacy_path = str(rule.rule)
        if not legacy_path.startswith('/api/') or legacy_path.startswith('/api/v1/'):
            continue
        alias = '/api/v1/' + legacy_path[len('/api/'):]
        if alias in existing:
            continue
        methods = sorted(set(rule.methods or ()) - {'HEAD', 'OPTIONS'})
        view_func = app.view_functions.get(rule.endpoint)
        if view_func is None:
            continue
        app.add_url_rule(
            alias,
            endpoint=f'api_v1__{rule.endpoint}',
            view_func=view_func,
            methods=methods,
        )
        existing.add(alias)


_register_api_v1_aliases()


@app.after_request
def _api_version_response_header(response):
    if (request.path or '').startswith('/api/v1/'):
        response.headers['X-TDeck-API-Version'] = '1'
    return response


if __name__ == '__main__':
    # Start server on configured port (key: `webserver_port`).
    try:
        utils.reload_config(force=True)
    except Exception:
        pass
    cfg = utils.get_config()
    host = cfg.get('webserver_host', '0.0.0.0')
    port = int(cfg.get('webserver_port', cfg.get('server_port', 5000)))
    print(f'Starting web UI on {host}:{port} (from config.json)')
    try:
        start_http_server(host, port)
        # keep main thread alive while server runs
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('Shutting down web UI')
        stop_http_server()
