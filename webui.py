from flask import Flask, render_template, jsonify, request, redirect, url_for, session, abort, send_file
import logging
import threading
import time
from pathlib import Path
import sys
import subprocess
import shlex
from collections import deque
import sqlite3
import secrets

from werkzeug.serving import make_server
import json
import re
from datetime import datetime, timedelta

from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from werkzeug.security import check_password_hash, generate_password_hash


# --- Home overview state (best-effort, in-memory) ---
_home_overview_lock = threading.Lock()
_home_last_timer_preset: dict = {'preset': None, 'name': None, 'time': None, 'ts': None}
_home_last_videohub_preset: dict = {'id': None, 'ts': None}
_home_last_videohub_route: dict = {'output': None, 'input': None, 'monitor': None, 'ts': None}


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
        if not p.exists():
            return {}
        raw = p.read_text(encoding='utf-8')
        obj = json.loads(raw or '{}')
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _home_state_save(payload: dict) -> None:
    p = _home_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        tmp.replace(p)
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
        # Per-role VideoHub allow-lists (stored as JSON string or NULL for inherit)
        try:
            cols = [str(r['name']) for r in conn.execute('PRAGMA table_info(roles)').fetchall()]
        except Exception:
            cols = []
        # Per-role idle logout timeout override (minutes). NULL => inherit from config.
        # 0 => disable idle logout for that role.
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


def _parse_role_allowlist_field(raw: str | None) -> list[int]:
    """Parse a role allow-list input.

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


def _get_role_videohub_allowlists(role_id: int | None) -> tuple[list[int], list[int]]:
    if role_id is None:
        return ([], [])
    conn = _db()
    try:
        row = conn.execute(
            'SELECT videohub_allowed_outputs, videohub_allowed_inputs FROM roles WHERE id=?',
            (int(role_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return ([], [])
    outs = _parse_role_allowlist_field(row['videohub_allowed_outputs'])
    ins = _parse_role_allowlist_field(row['videohub_allowed_inputs'])
    return (outs, ins)


def _set_role_videohub_allowlists(role_id: int, outputs_raw: str | None, inputs_raw: str | None) -> None:
    outs = _parse_role_allowlist_field(outputs_raw)
    ins = _parse_role_allowlist_field(inputs_raw)
    conn = _db()
    try:
        conn.execute(
            'UPDATE roles SET videohub_allowed_outputs=?, videohub_allowed_inputs=? WHERE id=?',
            (
                json.dumps(outs),
                json.dumps(ins),
                int(role_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _get_role_videohub_allowed_preset_ids(role_id: int | None) -> list[int]:
    if role_id is None:
        return []
    conn = _db()
    try:
        row = conn.execute(
            'SELECT videohub_allowed_presets FROM roles WHERE id=?',
            (int(role_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return []
    return _parse_role_allowlist_field(row['videohub_allowed_presets'])


def _set_role_videohub_allowed_preset_ids(role_id: int, presets_raw: str | None) -> None:
    preset_ids = _parse_role_allowlist_field(presets_raw)
    conn = _db()
    try:
        conn.execute(
            'UPDATE roles SET videohub_allowed_presets=? WHERE id=?',
            (
                json.dumps(preset_ids),
                int(role_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _audit(action: str, detail: str | None = None) -> None:
    try:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        ts = ''
    try:
        uid = int(current_user.get_id()) if getattr(current_user, 'is_authenticated', False) else None
    except Exception:
        uid = None
    try:
        uname = str(getattr(current_user, 'username', None) or '') if getattr(current_user, 'is_authenticated', False) else ''
    except Exception:
        uname = ''
    try:
        ip = str(request.remote_addr or '')
    except Exception:
        ip = ''
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


def _get_role_by_name(name: str) -> sqlite3.Row | None:
    conn = _db()
    try:
        cur = conn.execute('SELECT id,name FROM roles WHERE name=?', (name,))
        return cur.fetchone()
    finally:
        conn.close()


def _ensure_role(name: str) -> int:
    conn = _db()
    try:
        row = conn.execute('SELECT id FROM roles WHERE name=?', (name,)).fetchone()
        if row:
            return int(row['id'])
        conn.execute('INSERT INTO roles(name) VALUES (?)', (name,))
        conn.commit()
        row2 = conn.execute('SELECT id FROM roles WHERE name=?', (name,)).fetchone()
        return int(row2['id'])
    finally:
        conn.close()


def _set_role_pages(role_id: int, page_keys: list[str]) -> None:
    role_id = int(role_id)
    keys = [k for k in (page_keys or []) if str(k or '').strip()]
    conn = _db()
    try:
        conn.execute('DELETE FROM role_pages WHERE role_id=?', (role_id,))
        for k in keys:
            conn.execute('INSERT OR IGNORE INTO role_pages(role_id,page_key) VALUES (?,?)', (role_id, str(k)))
        conn.commit()
    finally:
        conn.close()


def _bootstrap_default_users_roles() -> None:
    """Create initial roles and default admin/admin if missing."""
    _init_auth_db()

    admin_role_id = _ensure_role('Admin')
    td_role_id = _ensure_role('TD')
    sp_role_id = _ensure_role('SP')

    # Seed initial page access lists once. Do not overwrite user-managed
    # assignments from the Admin UI.
    try:
        conn = _db()
        try:
            td_has = conn.execute('SELECT 1 FROM role_pages WHERE role_id=? LIMIT 1', (td_role_id,)).fetchone()
            sp_has = conn.execute('SELECT 1 FROM role_pages WHERE role_id=? LIMIT 1', (sp_role_id,)).fetchone()
        finally:
            conn.close()

        if not td_has or not sp_has:
            all_pages = sorted(_PAGE_REGISTRY.keys())
            if 'page:home' not in all_pages:
                all_pages = ['page:home', *all_pages]

            if not td_has:
                td_pages = [k for k in all_pages if k not in ('page:config', 'page:admin')]
                _set_role_pages(td_role_id, td_pages)
            if not sp_has:
                sp_pages = [k for k in all_pages if k in ('page:home', 'page:timers')]
                _set_role_pages(sp_role_id, sp_pages)
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
                ('admin', generate_password_hash('admin'), admin_role_id, 1, now, now),
            )
            conn.commit()
    finally:
        conn.close()


def _user_record(user_id: int) -> sqlite3.Row | None:
    conn = _db()
    try:
        return conn.execute(
            'SELECT u.*, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id WHERE u.id=?',
            (int(user_id),),
        ).fetchone()
    finally:
        conn.close()


def _user_by_username(username: str) -> sqlite3.Row | None:
    conn = _db()
    try:
        return conn.execute(
            'SELECT u.*, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id WHERE lower(u.username)=lower(?)',
            (str(username or ''),),
        ).fetchone()
    finally:
        conn.close()


def _role_allows_page(role_id: int | None, role_name: str | None, page_key: str) -> bool:
    if not page_key:
        return False
    if str(role_name or '') == 'Admin':
        return True
    if role_id is None:
        return False
    conn = _db()
    try:
        row = conn.execute(
            'SELECT 1 FROM role_pages WHERE role_id=? AND page_key=?',
            (int(role_id), str(page_key)),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def _role_idle_timeout_override_minutes(role_id: int | None) -> int | None:
    if role_id is None:
        return None
    conn = _db()
    try:
        row = conn.execute(
            'SELECT auth_idle_timeout_minutes_override FROM roles WHERE id=?',
            (int(role_id),),
        ).fetchone()
        if not row:
            return None
        v = row['auth_idle_timeout_minutes_override']
        if v is None:
            return None
        try:
            return int(v)
        except Exception:
            return None
    finally:
        conn.close()


def _parse_idle_timeout_override_raw(raw: str | None) -> int | None:
    """Parse a per-role idle timeout override.

    Returns:
      None => inherit from global config
      0 => disable idle logout for this role
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


def _set_role_idle_timeout_override(role_id: int, raw: str | None) -> None:
    v = _parse_idle_timeout_override_raw(raw)
    conn = _db()
    try:
        conn.execute(
            'UPDATE roles SET auth_idle_timeout_minutes_override=? WHERE id=?',
            (v, int(role_id)),
        )
        conn.commit()
    finally:
        conn.close()


class _User(UserMixin):
    def __init__(self, row: sqlite3.Row):
        self.id = int(row['id'])
        self.username = str(row['username'])
        self.role_id = int(row['role_id']) if row['role_id'] is not None else None
        self.role_name = str(row['role_name'] or '') if row['role_name'] is not None else ''
        self._active = bool(int(row['is_active'] or 0))

    def is_active(self) -> bool:
        return bool(self._active)


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
        return _role_allows_page(getattr(current_user, 'role_id', None), getattr(current_user, 'role_name', None), page_key)
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
    except Exception:
        sent = None
    return bool(sent) and str(sent) == str(session.get('_csrf'))


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
        'can_access': can_access,
        'csrf_token': _csrf_token,
        'current_user': current_user,
        'is_authenticated': is_authed,
    }


@app.before_request
def _auth_gate():
    if not _auth_enabled():
        return None

    # Always allow static + API + login/logout
    p = request.path or ''
    if p.startswith('/static/') or p.startswith('/api/') or p == '/login' or p == '/logout':
        return None

    # Ensure auth DB + defaults exist when auth is enabled
    try:
        _bootstrap_default_users_roles()
    except Exception:
        pass

    if not getattr(current_user, 'is_authenticated', False):
        nxt = request.full_path if request.query_string else request.path
        return redirect(url_for('login_page', next=nxt))

    # Idle timeout
    cfg = _auth_cfg()
    try:
        idle_enabled = bool(cfg.get('auth_idle_timeout_enabled', True))
    except Exception:
        idle_enabled = True
    try:
        idle_minutes = int(cfg.get('auth_idle_timeout_minutes', 2))
    except Exception:
        idle_minutes = 2
    idle_minutes = max(1, min(idle_minutes, 24 * 60))

    if idle_enabled:
        now = int(time.time())
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

    # CSRF protect non-API mutating requests
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if not _validate_csrf():
            _audit('csrf_fail', f'path={p}')
            return abort(400)

    # Authorization for pages
    view_fn = app.view_functions.get(request.endpoint)
    page_key = getattr(view_fn, '_required_page_key', None) if view_fn else None
    if not page_key:
        _audit('deny_missing_page_key', f'endpoint={request.endpoint} path={p}')
        return abort(403)

    if not can_access(str(page_key)):
        _audit('deny_page', f'page={page_key} path={p}')
        return abort(403)

    return None


@app.route('/auth/ping', methods=['GET'])
def auth_ping():
    # Handled by _auth_gate (returns 204 or redirects on timeout)
    return ('', 204)


@app.route('/auth/touch', methods=['GET'])
def auth_touch():
    # Handled by _auth_gate (refreshes last-activity and returns 204)
    return ('', 204)


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


# --- Console capture + CLI runner (Web UI) ---
_CONSOLE_MAX_LINES = 2000
_console_lock = threading.Lock()
_console_lines: deque[tuple[int, str, str]] = deque(maxlen=_CONSOLE_MAX_LINES)
_console_seq = 0


def _console_append(text: str) -> None:
    """Append text to the in-memory console buffer.

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


class _ConsoleTee:
    """Tee writes to the original stream AND the console buffer."""

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
        # Hide noisy request/access logs in the Web Console.
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
        if '"GET /api/console/logs' in msg or '"POST /api/console/run' in msg:
            return False
        return True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = str(record.getMessage())
        _console_append(msg + "\n")


def _install_console_capture() -> None:
    """Capture server stdout/stderr + logging into the console buffer."""
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

TEMPLATES_DIR = Path.cwd()
TRIGGER_TEMPLATES = TEMPLATES_DIR / 'trigger_templates.json'
BUTTON_TEMPLATES = TEMPLATES_DIR / 'button_templates.json'

# ensure template files exist
for p in (TRIGGER_TEMPLATES, BUTTON_TEMPLATES):
    if not p.exists():
        p.write_text('[]', encoding='utf-8')


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
_status_cache_lock = threading.Lock()
_STATUS_CACHE_TTL_SECONDS = 2.0

# Track last-known connectivity so we can log state changes (ONLINE/OFFLINE)
# without spamming the console on every poll.
_connectivity_last: dict[str, bool | None] = {
    'companion': None,
    'propresenter': None,
    'videohub': None,
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
        _console_append(f"[STATUS] {label} is now {state}{suffix}\n")
    except Exception:
        pass

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


def stop_http_server() -> None:
    global _http_server, _server_thread
    with _server_lock:
        if _http_server is None:
            return
        try:
            _http_server.shutdown()
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
            _audit('login_fail', f'username={username}')
            return render_template('login.html', page_title='Login', error='Invalid username or password', next=next_url), 401

        if not bool(int(row['is_active'] or 0)):
            _audit('login_fail_inactive', f'username={username}')
            return render_template('login.html', page_title='Login', error='Account is disabled', next=next_url), 403

        try:
            ok = check_password_hash(str(row['password_hash'] or ''), password)
        except Exception:
            ok = False

        if not ok:
            _audit('login_fail', f'username={username}')
            return render_template('login.html', page_title='Login', error='Invalid username or password', next=next_url), 401

        user = _User(row)
        login_user(user)
        session['_last_activity'] = int(time.time())
        _audit('login_ok', f'username={username}')

        return redirect(next_url or '/')

    timeout = request.args.get('timeout')
    msg = 'You have been logged out due to inactivity.' if timeout else None
    return render_template('login.html', page_title='Login', message=msg, next=next_url)


@app.route('/logout')
def logout_page():
    if _auth_enabled() and getattr(current_user, 'is_authenticated', False):
        _audit('logout')
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

        cfg = _auth_cfg()
        try:
            min_len = int(cfg.get('auth_min_password_length', 6))
        except Exception:
            min_len = 6
        min_len = max(4, min(min_len, 128))

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
                'UPDATE users SET password_hash=?, updated_at=? WHERE id=?',
                (generate_password_hash(new_pw), now, int(current_user.get_id())),
            )
            conn.commit()
        finally:
            conn.close()

        _audit('password_change_ok')
        return redirect('/')

    return render_template('account_password.html', page_title='Change Password')


@app.route('/admin/roles', methods=['GET', 'POST'])
@require_page('page:admin', 'Admin')
def admin_roles_page():
    # When auth is disabled, admin pages are still reachable; make sure the DB
    # and default roles exist so the page can render.
    try:
        _bootstrap_default_users_roles()
    except Exception:
        try:
            _init_auth_db()
        except Exception:
            pass

    if request.method == 'POST':
        action = str(request.form.get('action') or '').strip()

        if action == 'create_role':
            name = str(request.form.get('role_name') or '').strip()
            if name:
                try:
                    _ensure_role(name)
                    _audit('role_create', name)
                except Exception:
                    pass

        if action == 'delete_role':
            role_id = request.form.get('role_id')
            try:
                rid = int(role_id)
            except Exception:
                rid = None
            if rid:
                conn = _db()
                try:
                    r = conn.execute('SELECT id,name FROM roles WHERE id=?', (rid,)).fetchone()
                    if r and str(r['name']) != 'Admin':
                        # Unassign users from this role
                        conn.execute('UPDATE users SET role_id=NULL WHERE role_id=?', (rid,))
                        conn.execute('DELETE FROM role_pages WHERE role_id=?', (rid,))
                        conn.execute('DELETE FROM roles WHERE id=?', (rid,))
                        conn.commit()
                        _audit('role_delete', str(r['name']))
                finally:
                    conn.close()

        if action == 'save_pages':
            role_id = request.form.get('role_id')
            try:
                rid = int(role_id)
            except Exception:
                rid = None
            if rid:
                # Admin role is allow-all and not editable here.
                role_name = ''
                try:
                    conn = _db()
                    try:
                        rr = conn.execute('SELECT name FROM roles WHERE id=?', (rid,)).fetchone()
                        role_name = str(rr['name'] or '') if rr else ''
                    finally:
                        conn.close()
                except Exception:
                    role_name = ''

                if role_name != 'Admin':
                    keys = request.form.getlist('page_keys')
                    try:
                        _set_role_pages(rid, [str(k) for k in keys])
                        _audit('role_pages_update', f'role_id={rid} keys={len(keys)}')
                    except Exception:
                        pass

                    # Per-role Routing allow-lists (only update if routing page is selected)
                    try:
                        if 'page:routing' in [str(k) for k in keys]:
                            outs_raw = request.form.get('videohub_allowed_outputs_role')
                            ins_raw = request.form.get('videohub_allowed_inputs_role')
                            _set_role_videohub_allowlists(rid, outs_raw, ins_raw)
                            _audit('role_videohub_allowlists_update', f'role_id={rid}')
                    except Exception:
                        pass

                    # Per-role VideoHub preset visibility (only update if VideoHub page is selected)
                    try:
                        if 'page:videohub' in [str(k) for k in keys]:
                            preset_ids_raw = request.form.get('videohub_allowed_presets_role')
                            _set_role_videohub_allowed_preset_ids(rid, preset_ids_raw)
                            _audit('role_videohub_preset_allowlist_update', f'role_id={rid}')
                    except Exception:
                        pass

    conn = _db()
    try:
        roles = conn.execute(
            'SELECT id,name,videohub_allowed_outputs,videohub_allowed_inputs,videohub_allowed_presets FROM roles ORDER BY lower(name)'
        ).fetchall()
        role_pages = conn.execute('SELECT role_id,page_key FROM role_pages').fetchall()
    finally:
        conn.close()

    pages = sorted([(k, v.get('name') or k) for k, v in _PAGE_REGISTRY.items()], key=lambda x: x[1].lower())
    role_to_pages: dict[int, set[str]] = {}
    for rp in role_pages or []:
        try:
            role_to_pages.setdefault(int(rp['role_id']), set()).add(str(rp['page_key']))
        except Exception:
            continue

    role_to_vh: dict[int, dict[str, str]] = {}
    for r in roles or []:
        try:
            rid = int(r['id'])
        except Exception:
            continue

        # Store raw text for editing; blank means "allow all".
        out_raw = r['videohub_allowed_outputs']
        in_raw = r['videohub_allowed_inputs']
        preset_raw = r['videohub_allowed_presets']
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
        role_to_vh[rid] = {
            'outputs': out_s,
            'inputs': in_s,
            'presets': preset_s,
        }

    return render_template(
        'admin_roles.html',
        page_title='Access Levels',
        roles=roles,
        pages=pages,
        role_to_pages=role_to_pages,
        role_to_vh=role_to_vh,
    )


@app.route('/api/admin/roles/<int:role_id>', methods=['POST'])
@require_page('page:admin', 'Admin')
def api_admin_role_update(role_id: int):
    """Update role settings via JSON (used by Access Levels auto-save UI)."""
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        data = {}

    rid = int(role_id)
    role_name = ''
    try:
        conn = _db()
        try:
            rr = conn.execute('SELECT name FROM roles WHERE id=?', (rid,)).fetchone()
            role_name = str(rr['name'] or '') if rr else ''
        finally:
            conn.close()
    except Exception:
        role_name = ''

    # Admin role is allow-all and not editable for page keys/allow-lists.
    if role_name != 'Admin':
        try:
            keys = data.get('page_keys')
            if not isinstance(keys, list):
                keys = []
            keys = [str(k) for k in keys]
            _set_role_pages(rid, keys)
            _audit('role_pages_update', f'role_id={rid} keys={len(keys)}')
        except Exception:
            pass

        # Per-role Routing allow-lists (only update if routing page is selected)
        try:
            keys_set = set([str(k) for k in (data.get('page_keys') or [])])
            if 'page:routing' in keys_set:
                outs_raw = data.get('videohub_allowed_outputs_role')
                ins_raw = data.get('videohub_allowed_inputs_role')
                _set_role_videohub_allowlists(rid, outs_raw, ins_raw)
                _audit('role_videohub_allowlists_update', f'role_id={rid}')
        except Exception:
            pass

        # Per-role VideoHub preset visibility (only update if VideoHub page is selected)
        try:
            keys_set = set([str(k) for k in (data.get('page_keys') or [])])
            if 'page:videohub' in keys_set:
                preset_ids_raw = data.get('videohub_allowed_presets_role')
                _set_role_videohub_allowed_preset_ids(rid, preset_ids_raw)
                _audit('role_videohub_preset_allowlist_update', f'role_id={rid}')
        except Exception:
            pass

    return jsonify({'ok': True})


@app.route('/admin/users', methods=['GET', 'POST'])
@require_page('page:admin', 'Admin')
def admin_users_page():
    # When auth is disabled, admin pages are still reachable; make sure the DB
    # and default roles exist so the page can render.
    try:
        _bootstrap_default_users_roles()
    except Exception:
        try:
            _init_auth_db()
        except Exception:
            pass

    cfg = _auth_cfg()
    try:
        min_len = int(cfg.get('auth_min_password_length', 6))
    except Exception:
        min_len = 6
    min_len = max(4, min(min_len, 128))

    if request.method == 'POST':
        action = str(request.form.get('action') or '').strip()

        if action == 'create_user':
            username = str(request.form.get('username') or '').strip()
            password = str(request.form.get('password') or '')
            role_id = request.form.get('role_id')
            try:
                rid = int(role_id) if role_id else None
            except Exception:
                rid = None

            if username and len(password) >= min_len:
                conn = _db()
                try:
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    conn.execute(
                        'INSERT INTO users(username,password_hash,role_id,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                        (username, generate_password_hash(password), rid, 1, now, now),
                    )
                    conn.commit()
                    _audit('user_create', username)
                except Exception:
                    pass
                finally:
                    conn.close()

        if action == 'update_user':
            user_id = request.form.get('user_id')
            try:
                uid = int(user_id)
            except Exception:
                uid = None
            role_id = request.form.get('role_id')
            try:
                rid = int(role_id) if role_id else None
            except Exception:
                rid = None
            is_active = 1 if request.form.get('is_active') == 'on' else 0

            if uid:
                conn = _db()
                try:
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    conn.execute('UPDATE users SET role_id=?, is_active=?, updated_at=? WHERE id=?', (rid, is_active, now, uid))
                    conn.commit()
                    _audit('user_update', f'id={uid}')
                finally:
                    conn.close()

        if action == 'reset_password':
            user_id = request.form.get('user_id')
            new_pw = str(request.form.get('new_password') or '')
            try:
                uid = int(user_id)
            except Exception:
                uid = None
            if uid and len(new_pw) >= min_len:
                conn = _db()
                try:
                    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    conn.execute('UPDATE users SET password_hash=?, updated_at=? WHERE id=?', (generate_password_hash(new_pw), now, uid))
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
                    # Prevent deleting yourself or the last admin
                    if int(current_user.get_id()) == uid:
                        pass
                    else:
                        # check if user is admin
                        row = conn.execute('SELECT u.id, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id WHERE u.id=?', (uid,)).fetchone()
                        if row and str(row['role_name'] or '') == 'Admin':
                            admins = conn.execute(
                                "SELECT count(*) AS c FROM users u LEFT JOIN roles r ON r.id=u.role_id WHERE r.name='Admin' AND u.is_active=1"
                            ).fetchone()
                            if admins and int(admins['c'] or 0) <= 1:
                                pass
                            else:
                                conn.execute('DELETE FROM users WHERE id=?', (uid,))
                                conn.commit()
                                _audit('user_delete', f'id={uid}')
                        else:
                            conn.execute('DELETE FROM users WHERE id=?', (uid,))
                            conn.commit()
                            _audit('user_delete', f'id={uid}')
                finally:
                    conn.close()

    conn = _db()
    try:
        users = conn.execute(
            'SELECT u.id,u.username,u.is_active,u.role_id, r.name AS role_name FROM users u LEFT JOIN roles r ON r.id=u.role_id ORDER BY lower(u.username)'
        ).fetchall()
        roles = conn.execute('SELECT id,name FROM roles ORDER BY lower(name)').fetchall()
    finally:
        conn.close()

    return render_template('admin_users.html', page_title='Users', users=users, roles=roles, min_len=min_len)


@app.get('/admin/backup/authdb')
@require_page('page:admin', 'Admin')
def admin_backup_authdb():
    """Download the auth database as a backup (Admin only)."""
    try:
        _bootstrap_default_users_roles()
    except Exception:
        pass

    try:
        src = Path(_AUTH_DB_PATH)
        if not src.exists():
            _init_auth_db()
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        filename = f'auth-{stamp}.db'
        return send_file(str(src), as_attachment=True, download_name=filename)
    except Exception as e:
        try:
            _audit('backup_authdb_fail', str(e))
        except Exception:
            pass
        return abort(500)


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
        arr = _read_json_file(BUTTON_TEMPLATES)
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
            pattern = _extract_pattern_from_button_url(url) or ''

            offset_min = None
            try:
                offset_min = int(getattr(trig, 'timer', 0)) if trig is not None else 0
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

            out.append(
                {
                    'due_ms': due_ms,
                    'seconds_until': seconds_until,
                    'event': event_name or '(unnamed)',
                    'event_id': event_id,
                    'offset_min': offset_min,
                    'offset': offset_label,
                    'actionType': action_type,
                    'buttonURL': url if action_type != 'api' else '',
                    'api': api if (action_type == 'api' and isinstance(api, dict)) else None,
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
    # Allow-list is configured as 1-based preset IDs, per role.
    # Blank/NULL => allow all.
    allowed_preset_ids: list[int] = []
    try:
        if _auth_enabled() and getattr(current_user, 'is_authenticated', False):
            # Admin is allow-all by design.
            if str(getattr(current_user, 'role_name', '') or '') == 'Admin':
                allowed_preset_ids = []
            else:
                allowed_preset_ids = _get_role_videohub_allowed_preset_ids(getattr(current_user, 'role_id', None))
    except Exception:
        pass

    return render_template('videohub.html', allowed_preset_ids=allowed_preset_ids)


@app.route('/routing')
@require_page('page:routing', 'Routing')
def routing_page():
    # Allow-lists are configured as 1-based indices, per role.
    # Blank/NULL => allow all.
    allowed_outputs: list[int] = []
    allowed_inputs: list[int] = []
    try:
        if _auth_enabled() and getattr(current_user, 'is_authenticated', False):
            # Admin is allow-all by design.
            if str(getattr(current_user, 'role_name', '') or '') == 'Admin':
                allowed_outputs = []
                allowed_inputs = []
            else:
                ro, ri = _get_role_videohub_allowlists(getattr(current_user, 'role_id', None))
                allowed_outputs = ro
                allowed_inputs = ri
    except Exception:
        pass

    return render_template('routing.html', allowed_outputs=allowed_outputs, allowed_inputs=allowed_inputs)


@app.route('/timers')
@require_page('page:timers', 'Timers')
def timers_page():
    return render_template('timers.html')


@app.route('/config')
@require_page('page:config', 'Config')
def config_page():
    return render_template('config.html')


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
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding='utf-8') or '[]')
    except Exception:
        return []


def _write_json_file(p: Path, data):
    try:
        p.write_text(json.dumps(data, indent=2), encoding='utf-8')
        return True
    except Exception:
        return False


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


def _find_duplicate_button_template(arr: list, *, url: str, pattern: str, exclude_idx: int | None = None) -> dict | None:
    url = (url or '').strip()
    pattern = (pattern or '').strip()
    if not url and not pattern:
        return None
    for i, tpl in enumerate(arr or []):
        if exclude_idx is not None and i == exclude_idx:
            continue
        if not isinstance(tpl, dict):
            continue
        existing_pattern = (tpl.get('pattern') or '').strip()
        existing_url = _button_template_effective_url(tpl)
        if url and existing_url == url:
            return {
                'index': i,
                'label': (tpl.get('label') or '').strip(),
                'pattern': existing_pattern,
                'buttonURL': existing_url,
            }
        if pattern and existing_pattern == pattern:
            return {
                'index': i,
                'label': (tpl.get('label') or '').strip(),
                'pattern': existing_pattern,
                'buttonURL': existing_url,
            }
    return None


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
        trigs = _read_json_file(TRIGGER_TEMPLATES)
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
            _write_json_file(TRIGGER_TEMPLATES, normalized)
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
    btns = _read_json_file(BUTTON_TEMPLATES)
    trigs = _read_json_file(TRIGGER_TEMPLATES)
    # normalize older 'spec' entries to 'times'
    normalized = []
    changed = False
    for t in trigs:
        if not isinstance(t, dict):
            normalized.append(t); continue
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
        normalized.append(t)
    trigs = normalized
    # if we normalized old entries, persist the normalized form back to file
    if changed:
        try:
            _write_json_file(TRIGGER_TEMPLATES, trigs)
        except Exception:
            pass
    return jsonify({'buttons': btns, 'triggers': trigs})


@app.route('/api/templates/button', methods=['POST'])
def api_add_button_template():
    body = request.get_json() or {}
    label = (body.get('label') or '').strip()
    pattern = (body.get('pattern') or '').strip()
    if not label or not pattern:
        return jsonify({'ok': False, 'error': 'label and pattern required'}), 400
    # validate pattern: must be three integers separated by '/'
    if not re.match(r'^\d+\/\d+\/\d+$', pattern):
        return jsonify({'ok': False, 'error': 'pattern must be like "1/0/1" (three integers separated by "/")'}), 400

    arr = _read_json_file(BUTTON_TEMPLATES)
    button_url = f"location/{pattern}/press"
    dup = _find_duplicate_button_template(arr, url=button_url, pattern=pattern)
    if dup:
        return jsonify({
            'ok': False,
            'error': 'duplicate button template',
            'existing': dup,
        }), 409
    arr.append({'label': label, 'pattern': pattern, 'buttonURL': button_url})
    ok = _write_json_file(BUTTON_TEMPLATES, arr)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
    return jsonify({'ok': True, 'template': arr[-1]})


@app.route('/api/templates/button/<int:idx>', methods=['DELETE'])
def api_delete_button_template(idx: int):
    arr = _read_json_file(BUTTON_TEMPLATES)
    if idx < 0 or idx >= len(arr):
        return jsonify({'ok': False, 'error': 'index out of range'}), 404
    removed = arr.pop(idx)
    ok = _write_json_file(BUTTON_TEMPLATES, arr)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
    return jsonify({'ok': True, 'removed': removed})


@app.route('/api/templates/button/<int:idx>', methods=['PUT'])
def api_update_button_template(idx: int):
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

    arr = _read_json_file(BUTTON_TEMPLATES)
    if idx < 0 or idx >= len(arr):
        return jsonify({'ok': False, 'error': 'index out of range'}), 404

    dup = _find_duplicate_button_template(arr, url=new_url, pattern=pattern, exclude_idx=idx)
    if dup:
        return jsonify({
            'ok': False,
            'error': 'duplicate button template',
            'existing': dup,
        }), 409

    old = arr[idx] if isinstance(arr[idx], dict) else {}
    try:
        old_url = str(old.get('buttonURL') or (f"location/{old.get('pattern')}/press" if old.get('pattern') else '')).strip()
    except Exception:
        old_url = ''

    arr[idx] = {'label': label, 'pattern': pattern, 'buttonURL': new_url}
    ok = _write_json_file(BUTTON_TEMPLATES, arr)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500

    # Propagate URL change across stored data
    replace_stats = _replace_button_url_everywhere(old_url, new_url)
    return jsonify({'ok': True, 'template': arr[idx], 'replaced': {'old': old_url, 'new': new_url}, **replace_stats})


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
            normalized_times.append(t3)
    arr = _read_json_file(TRIGGER_TEMPLATES)
    arr.append({'label': label, 'times': normalized_times})
    ok = _write_json_file(TRIGGER_TEMPLATES, arr)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
    return jsonify({'ok': True, 'template': arr[-1]})


@app.route('/api/templates/trigger/<int:idx>', methods=['DELETE'])
def api_delete_trigger_template(idx: int):
    arr = _read_json_file(TRIGGER_TEMPLATES)
    if idx < 0 or idx >= len(arr):
        return jsonify({'ok': False, 'error': 'index out of range'}), 404
    removed = arr.pop(idx)
    ok = _write_json_file(TRIGGER_TEMPLATES, arr)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
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
            normalized_times.append(t3)
    arr = _read_json_file(TRIGGER_TEMPLATES)
    if idx < 0 or idx >= len(arr):
        return jsonify({'ok': False, 'error': 'index out of range'}), 404
    arr[idx] = {'label': label, 'times': normalized_times}
    ok = _write_json_file(TRIGGER_TEMPLATES, arr)
    if not ok:
        return jsonify({'ok': False, 'error': 'failed to save'}), 500
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
                        'actionType': str(getattr(t, 'actionType', 'companion') or 'companion').lower(),
                        'buttonURL': t.buttonURL,
                        'api': getattr(t, 'api', None) if str(getattr(t, 'actionType', 'companion') or 'companion').lower() == 'api' else None,
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


@app.route('/api/companion_status')
def companion_status():
    # Cache to prevent N clients polling every 10s from causing N blocking
    # network probes at the same moment.
    now = time.time()
    with _status_cache_lock:
        if (now - float(_companion_status_cache.get('ts', 0.0))) < _STATUS_CACHE_TTL_SECONDS:
            return jsonify({'connected': bool(_companion_status_cache.get('connected', False))})

    c = utils.get_companion()
    status = False
    try:
        if c is None:
            status = False
        else:
            # actively check connectivity if possible to return an up-to-date result
            try:
                if hasattr(c, 'check_connection'):
                    status = bool(c.check_connection())
                else:
                    status = bool(getattr(c, 'connected', False))
            except Exception:
                # fall back to stored flag
                status = bool(getattr(c, 'connected', False))
    except Exception:
        status = False

    with _status_cache_lock:
        _companion_status_cache['ts'] = now
        _companion_status_cache['connected'] = bool(status)

    # Log only on ONLINE/OFFLINE transitions.
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        ip = str(cfg.get('companion_ip', '')).strip()
        port = int(cfg.get('companion_port', 0))
        detail = f"{ip}:{port}" if ip and port else (ip or '')
    except Exception:
        detail = ''
    _log_connectivity_change('companion', bool(status), detail=detail)

    return jsonify({'connected': status})


@app.route('/api/propresenter_status')
def propresenter_status():
    """Lightweight ProPresenter connectivity check for the UI indicator.
    """
    now = time.time()
    with _status_cache_lock:
        if (now - float(_propresenter_status_cache.get('ts', 0.0))) < _STATUS_CACHE_TTL_SECONDS:
            return jsonify({'connected': bool(_propresenter_status_cache.get('connected', False))})

    status = False
    try:
        if ProPresentor is None:
            status = False
        else:
            try:
                cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
            except Exception:
                cfg = {}

            ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
            try:
                port = int(cfg.get('propresenter_port', 1025))
            except Exception:
                port = 1025

            # Keep this fast; it's polled periodically.
            pp = ProPresentor(ip, port, timeout=1.0, verify_on_init=False, debug=False)
            status = bool(pp.check_connection())
    except Exception:
        status = False

    with _status_cache_lock:
        _propresenter_status_cache['ts'] = now
        _propresenter_status_cache['connected'] = bool(status)

    try:
        detail = f"{ip}:{port}" if ip and port else ''
    except Exception:
        detail = ''
    _log_connectivity_change('propresenter', bool(status), detail=detail)

    return jsonify({'connected': status})


@app.route('/api/videohub_status')
def videohub_status():
    """Lightweight VideoHub connectivity check for the UI indicator.

    Uses the same caching strategy as the other status endpoints.
    """
    now = time.time()
    with _status_cache_lock:
        if (now - float(_videohub_status_cache.get('ts', 0.0))) < _STATUS_CACHE_TTL_SECONDS:
            return jsonify({'connected': bool(_videohub_status_cache.get('connected', False))})

    status = False
    try:
        vh = _get_videohub_client_from_config()
        if vh is None:
            status = False
        else:
            status = bool(vh.ping())
    except Exception:
        status = False

    with _status_cache_lock:
        _videohub_status_cache['ts'] = now
        _videohub_status_cache['connected'] = bool(status)

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        ip = str(cfg.get('videohub_ip', '')).strip()
        port = int(cfg.get('videohub_port', 0))
        detail = f"{ip}:{port}" if ip and port else (ip or '')
    except Exception:
        detail = ''
    _log_connectivity_change('videohub', bool(status), detail=detail)

    return jsonify({'connected': bool(status)})


@app.route('/api/config', methods=['GET'])
def api_get_config():
    if _auth_enabled():
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
def api_set_config():
    if _auth_enabled():
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

        # Compute port change before writing so we can tell the UI what's happening.
        try:
            old_port = int(cfg.get('webserver_port', cfg.get('server_port', 5000)))
        except Exception:
            old_port = 5000

        # merge provided values
        cfg.update(new)

        # Legacy: global Routing allow-lists are no longer used (now per Access Level).
        cfg.pop('videohub_allowed_outputs', None)
        cfg.pop('videohub_allowed_inputs', None)

        # persist
        utils.save_config(cfg)
        utils.reload_config(force=True)
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
                    _console_append(f"[WEB] Port changed; restarting server on port {port}...\n")
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
        return jsonify({'ok': True, 'preset': preset.to_dict() if hasattr(preset, 'to_dict') else preset})
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
        return jsonify({'ok': True, 'preset': preset.to_dict() if hasattr(preset, 'to_dict') else preset})
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
        return jsonify({'ok': True, 'preset': updated.to_dict() if hasattr(updated, 'to_dict') else updated})
    except KeyError:
        return jsonify({'ok': False, 'error': 'preset not found'}), 404
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/videohub/presets/<int:preset_id>/apply', methods=['POST'])
def api_videohub_presets_apply(preset_id: int):
    app_inst = _get_videohub_app()
    if app_inst is None or not hasattr(app_inst, 'apply_preset'):
        return jsonify({'ok': False, 'error': 'VideoHub backend not available'}), 500
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}
    try:
        result = app_inst.apply_preset(cfg, preset_id)  # type: ignore[attr-defined]
        try:
            _home_set_last_videohub_preset(preset_id=preset_id)
        except Exception:
            pass
        try:
            _console_append(f"[VIDEOHUB] Applied preset #{preset_id}\n")
        except Exception:
            pass
        return jsonify({'ok': True, 'result': result})
    except KeyError:
        return jsonify({'ok': False, 'error': 'preset not found'}), 404
    except Exception as e:
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
            _console_append(f"[VIDEOHUB] Saved snapshot from device as preset #{getattr(preset, 'id', '?')}\n")
        except Exception:
            pass

        return jsonify({'ok': True, 'preset': preset.to_dict() if hasattr(preset, 'to_dict') else preset})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/videohub/labels', methods=['GET'])
def api_videohub_labels():
    """Return VideoHub input/output labels for UI dropdowns.

    This endpoint is best-effort. If the router isn't configured/reachable,
    it returns a numeric fallback list so the UI can still function.
    """

    # Default to 40, since common VideoHubs are 40x40.
    fallback_count = 40
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    vh = _get_videohub_client_from_config()
    if vh is None:
        # Not configured.
        nums = [{"number": i, "label": ""} for i in range(1, fallback_count + 1)]
        return jsonify({
            'ok': True,
            'configured': False,
            'inputs': nums,
            'outputs': nums,
        })

    try:
        labels = vh.get_labels(fallback_count=fallback_count)
        return jsonify({
            'ok': True,
            'configured': True,
            'inputs': labels.get('inputs', []),
            'outputs': labels.get('outputs', []),
        })
    except Exception as e:
        nums = [{"number": i, "label": ""} for i in range(1, fallback_count + 1)]
        return jsonify({
            'ok': True,
            'configured': True,
            'error': str(e),
            'inputs': nums,
            'outputs': nums,
        })


@app.route('/api/videohub/state', methods=['GET'])
def api_videohub_state():
    """Return VideoHub labels + current routing snapshot.

    Best-effort: if the router isn't reachable, returns a numeric fallback list
    and an identity-style routing mapping.
    """

    fallback_count = 40

    vh = _get_videohub_client_from_config()
    if vh is None:
        nums = [{"number": i, "label": ""} for i in range(1, fallback_count + 1)]
        return jsonify({
            'ok': True,
            'configured': False,
            'inputs': nums,
            'outputs': nums,
            'routing': [i for i in range(1, fallback_count + 1)],
        })

    try:
        if hasattr(vh, 'get_state'):
            st = vh.get_state(fallback_count=fallback_count)
            inputs = st.get('inputs') or []
            outputs = st.get('outputs') or []
            routing = st.get('routing') or []
        else:
            # Backwards compatibility if older client is present.
            labels = vh.get_labels(fallback_count=fallback_count)
            inputs = labels.get('inputs', [])
            outputs = labels.get('outputs', [])
            n = max(fallback_count, len(inputs), len(outputs))
            routing = [i for i in range(1, n + 1)]

        return jsonify({
            'ok': True,
            'configured': True,
            'inputs': inputs,
            'outputs': outputs,
            'routing': routing,
        })
    except Exception as e:
        nums = [{"number": i, "label": ""} for i in range(1, fallback_count + 1)]
        return jsonify({
            'ok': True,
            'configured': True,
            'error': str(e),
            'inputs': nums,
            'outputs': nums,
            'routing': [i for i in range(1, fallback_count + 1)],
        })


@app.route('/api/home/overview', methods=['GET'])
def api_home_overview():
    """Lightweight Home dashboard data.

    Best-effort and intentionally minimal.
    """

    # Timers
    try:
        timer_presets = list(utils.load_timer_presets()) if hasattr(utils, 'load_timer_presets') else []
    except Exception:
        timer_presets = []

    # Sync from disk first so multi-process deployments stay consistent.
    try:
        _home_state_sync_from_disk()
    except Exception:
        pass

    with _home_overview_lock:
        last_timer = dict(_home_last_timer_preset)
        last_vh = dict(_home_last_videohub_preset)
        last_vh_route = dict(_home_last_videohub_route)

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

    return jsonify({'ok': True, 'timers': timers_payload, 'videohub': videohub_payload, 'ts': time.time()})


@app.route('/api/console/logs', methods=['GET'])
def api_console_logs():
    """Return captured stdout/stderr/logging lines.

    Query:
      - since: last seen line id (int). Returns only newer lines when possible.
      - limit: max lines to return (int, default 400, max 2000)
    """
    try:
        since = int(request.args.get('since', '0') or '0')
    except Exception:
        since = 0

    try:
        limit = int(request.args.get('limit', '400') or '400')
    except Exception:
        limit = 400
    if limit < 1:
        limit = 1
    if limit > _CONSOLE_MAX_LINES:
        limit = _CONSOLE_MAX_LINES

    with _console_lock:
        lines = list(_console_lines)
        next_id = _console_seq

    if since > 0:
        lines = [(i, ts, t) for (i, ts, t) in lines if i > since]
    if len(lines) > limit:
        lines = lines[-limit:]

    return jsonify({
        'ok': True,
        'next': next_id,
        'lines': [{'ts': ts, 'text': t} for (_, ts, t) in lines],
    })


def _project_root() -> str:
    try:
        return str(Path(__file__).resolve().parent)
    except Exception:
        return str(Path.cwd())


@app.route('/api/console/run', methods=['POST'])
def api_console_run():
    """Run a cli.py command (subcommands only, no shell).

    Body JSON:
      {"command": "list"}

    Executed as: <python> cli.py <args...>
    """
    body = request.get_json(silent=True) or {}
    cmd = str(body.get('command', '')).strip()
    if not cmd:
        return jsonify({'ok': False, 'error': 'Missing command'}), 400

    try:
        args = shlex.split(cmd, posix=False)
    except Exception:
        args = cmd.split()

    if not args:
        return jsonify({'ok': False, 'error': 'Missing command'}), 400

    # Be forgiving: users may type prefixes out of habit.
    # Allow: "list", "cli list", "cli.py list", "python cli.py list"
    try:
        a0 = str(args[0]).lower()
    except Exception:
        a0 = ''

    if a0 in ('cli',):
        args = args[1:]
    elif a0.endswith('cli.py'):
        args = args[1:]
    elif a0 in ('python', 'python3', 'py') and len(args) >= 2:
        try:
            a1 = str(args[1]).lower()
        except Exception:
            a1 = ''
        if a1.endswith('cli.py'):
            args = args[2:]

    if not args:
        args = ['--help']

    if len(args) == 1 and args[0].lower() == 'help':
        args = ['--help']

    py = sys.executable or 'python'
    cli_path = str(Path(_project_root()) / 'cli.py')

    _console_append(f"\n$ cli {' '.join(args)}\n")

    try:
        proc = subprocess.run(
            [py, cli_path, *args],
            cwd=_project_root(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _console_append("[cli] ERROR: command timed out\n")
        return jsonify({'ok': False, 'error': 'Command timed out'}), 408
    except Exception as e:
        _console_append(f"[cli] ERROR: {e}\n")
        return jsonify({'ok': False, 'error': str(e)}), 500

    out = proc.stdout or ''
    err = proc.stderr or ''
    if out:
        _console_append(out)
        if not out.endswith('\n'):
            _console_append('\n')
    if err:
        _console_append(err)
        if not err.endswith('\n'):
            _console_append('\n')

    return jsonify({
        'ok': True,
        'exit_code': int(proc.returncode),
        'stdout': out,
        'stderr': err,
    })


def _validate_time_hhmm(s: str) -> bool:
    try:
        datetime.strptime(s, '%H:%M')
        return True
    except Exception:
        return False


_RELATIVE_MINUTES_RE = re.compile(r'^\$(?P<sign>[+-])(?P<minutes>\d+)$')


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
      - "$-60" / "$+15" (minutes relative to event_start/base_time)

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
            return None, 'relative time must look like "$-60" (minutes) or "$+15"', True

        base_dt, err = _resolve_base_datetime_for_relative_time(body)
        if err:
            return None, err, True

        minutes = int(m.group('minutes'))
        sign = -1 if m.group('sign') == '-' else 1
        dt = base_dt + timedelta(minutes=sign * minutes)
        return dt.strftime('%H:%M'), None, True

    if not _validate_time_hhmm(s):
        return None, 'time must be HH:MM', False

    return s, None, False


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
    """
    if not isinstance(raw, dict):
        return None, 'trigger must be an object'

    out: dict = {}

    # typeOfTrigger + minutes are normalized by callers, but keep safe defaults.
    try:
        out['typeOfTrigger'] = str(raw.get('typeOfTrigger', 'AT')).upper()
    except Exception:
        out['typeOfTrigger'] = 'AT'
    try:
        out['minutes'] = int(raw.get('minutes', 0) or 0)
    except Exception:
        out['minutes'] = 0

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
        # infer based on presence of API fields
        if api_obj is not None or raw.get('path') or raw.get('method'):
            action_type = 'api'
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
            return None, 'api path must be a relative /videohub/... (the /api prefix is added automatically)'
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
        _console_append(
            f"[TIMERS] Timer preset #{preset_number} '{preset_name or time_str}' -> firing {len(presses)} Companion press(es) now\n"
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
            _console_append("[TIMERS] Companion not connected; timer presses skipped\n")
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
            _console_append(f"[TIMERS] POST {u} -> {'OK' if ok else 'FAIL'}\n")
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

    presets = body.get('timer_presets')
    if presets is None:
        # allow alternate key
        presets = body.get('presets')

    if not isinstance(presets, list):
        return jsonify({'ok': False, 'error': 'timer_presets must be an array of presets'}), 400

    normalized_presets: list[dict] = []
    for v in presets:
        if isinstance(v, dict):
            time_str = str(v.get('time', '')).strip()
            name_str = str(v.get('name', '')).strip()
            raw_presses = v.get('button_presses')
            if raw_presses is None and 'buttonPresses' in v:
                raw_presses = v.get('buttonPresses')
            if raw_presses is None and 'actions' in v:
                raw_presses = v.get('actions')
        else:
            time_str = str(v).strip()
            name_str = ''
            raw_presses = None

        if not time_str:
            continue
        if not _validate_time_hhmm(time_str):
            return jsonify({'ok': False, 'error': f'invalid time: {time_str}. Use HH:MM'}), 400

        # Always ensure each timer has a name; default to its time.
        if not name_str:
            name_str = time_str

        # Normalize button presses
        presses_out: list[dict[str, str]] = []
        if raw_presses is not None:
            if isinstance(raw_presses, dict) or isinstance(raw_presses, str):
                raw_list = [raw_presses]
            else:
                raw_list = raw_presses

            if not isinstance(raw_list, list):
                return jsonify({'ok': False, 'error': 'button_presses must be an array of button press entries'}), 400

            if len(raw_list) > 50:
                return jsonify({'ok': False, 'error': 'too many button presses (max 50 per timer)'}), 400

            for item in raw_list:
                if isinstance(item, str):
                    u = _normalize_companion_button_url(item)
                elif isinstance(item, dict):
                    u = _normalize_companion_button_url(str(item.get('buttonURL') or item.get('url') or item.get('button_url') or ''))
                else:
                    u = None

                if not u:
                    return jsonify({'ok': False, 'error': "Invalid buttonURL in button_presses. Use '1/2/3' or 'location/1/2/3/press'"}), 400
                presses_out.append({'buttonURL': u})

        obj = {'time': time_str, 'name': name_str}
        if presses_out:
            obj['button_presses'] = presses_out

        normalized_presets.append(obj)

    if len(normalized_presets) < 1:
        return jsonify({'ok': False, 'error': 'timer_presets must contain at least 1 entry'}), 400
    if len(normalized_presets) > 100:
        return jsonify({'ok': False, 'error': 'timer_presets too large (max 100)'}), 400

    try:
        propresenter_timer_index = int(body.get('propresenter_timer_index', body.get('timer_index', None)))
    except Exception:
        propresenter_timer_index = None

    stream_start_raw = body.get('stream_start_preset')
    if stream_start_raw is None:
        stream_start_raw = body.get('streamStartPreset')

    stream_start_preset: int | None = None
    if stream_start_raw is not None:
        try:
            if str(stream_start_raw).strip() == '':
                stream_start_preset = 0
            else:
                stream_start_preset = int(stream_start_raw)
        except Exception:
            return jsonify({'ok': False, 'error': 'stream_start_preset must be an integer (1-based)'}), 400

        if stream_start_preset != 0 and not (1 <= stream_start_preset <= len(normalized_presets)):
            return jsonify({
                'ok': False,
                'error': f'stream_start_preset out of range (1..{len(normalized_presets)})'
            }), 400


    try:
        cfg = utils.get_config()
    except Exception:
        cfg = {}

    # presets are stored outside config.json
    try:
        if hasattr(utils, 'save_timer_presets'):
            utils.save_timer_presets(normalized_presets)
    except Exception:
        pass
    if propresenter_timer_index is not None:
        cfg['propresenter_timer_index'] = propresenter_timer_index
        # remove legacy key if present
        cfg.pop('timer_index', None)
    if stream_start_preset is not None:
        cfg['stream_start_preset'] = stream_start_preset

    try:
        utils.save_config(cfg)
        utils.reload_config(force=True)

        # Debug-only: log when presets/config are successfully saved.
        if _is_debug_enabled():
            try:
                _console_append(
                    f"[TIMERS] Saved {len(normalized_presets)} preset(s); "
                    f"propresenter_timer_index={cfg.get('propresenter_timer_index', 1)}\n"
                )
            except Exception:
                pass

        # Push names to Companion custom variables, e.g. timer_name_1, timer_name_2, ...
        companion_updated = False
        companion_failed = 0
        try:
            prefix = str(cfg.get('companion_timer_name', '')).strip()
        except Exception:
            prefix = ''
        try:
            comp = utils.get_companion() if hasattr(utils, 'get_companion') else None
            if prefix and comp is not None:
                companion_updated = True
                for i, p in enumerate(normalized_presets, start=1):
                    var_name = f"{prefix}{i}"

                    # Format: "timer_name_{index}: HH:MMam" (12-hour with am/pm)
                    try:
                        t = str(p.get('time', '')).strip()
                    except Exception:
                        t = ''
                    try:
                        dt = datetime.strptime(t, '%H:%M')
                        pretty_time = dt.strftime('%I:%M%p').lower()
                    except Exception:
                        pretty_time = t

                    # Use the saved preset name as the label; fall back to the variable name.
                    try:
                        label = str(p.get('name', '')).strip()
                    except Exception:
                        label = ''

                    # If the label is missing (or just equals the raw HH:MM), use the variable name as label.
                    if (not label) or (label == t):
                        label = var_name

                    value = f"{label}\n{pretty_time}"

                    if not comp.SetVariable(var_name, value):
                        companion_failed += 1
        except Exception:
            companion_updated = False

        return jsonify({
            'ok': True,
            'timer_presets': normalized_presets,
            'propresenter_timer_index': cfg.get('propresenter_timer_index', 1),
            'stream_start_preset': cfg.get('stream_start_preset', 0),
            'companion_names_updated': companion_updated,
            'companion_names_failed': companion_failed,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/timers/preset', methods=['POST', 'PATCH'])
def api_update_timer_preset():
    """Update a single timer preset's time (and optionally name) by 1-based preset number.

    Body example:
      {"preset": 2, "time": "08:15"}
      {"preset": 2, "time": "08:15", "name": "Walk-in"}
    """

    body = request.get_json(silent=True) or {}

    def _get_ci(d: dict, *keys: str):
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

    preset_raw = _get_ci(body, 'preset', 'preset_index', 'index', 'number')
    if preset_raw is None:
        preset_raw = request.args.get('preset') or request.args.get('index')

    time_raw = _get_ci(body, 'time', 'hhmm', 'value')
    if time_raw is None:
        time_raw = request.args.get('time')

    apply_raw = _get_ci(body, 'apply', 'apply_now', 'applypreset')
    if apply_raw is None:
        apply_raw = request.args.get('apply')

    def _coerce_bool(v) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        s = str(v).strip().lower()
        if s in ('1', 'true', 't', 'yes', 'y', 'on'):
            return True
        if s in ('0', 'false', 'f', 'no', 'n', 'off'):
            return False
        return False

    apply_now = _coerce_bool(apply_raw)

    try:
        preset_number = int(preset_raw)
    except Exception:
        return jsonify({'ok': False, 'error': 'preset must be an integer (1-based)'}), 400

    time_str, time_err, time_was_relative = _resolve_time_hhmm_input(time_raw, body=body)
    if time_err:
        return jsonify({'ok': False, 'error': time_err}), 400

    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
    except Exception:
        cfg = {}

    try:
        presets = list(utils.load_timer_presets()) if hasattr(utils, 'load_timer_presets') else []
    except Exception:
        presets = []

    if not presets:
        return jsonify({'ok': False, 'error': 'no presets configured (timer_presets.json is empty)', 'preset_count': 0}), 400

    preset_index = preset_number - 1
    if preset_index < 0 or preset_index >= len(presets):
        return jsonify({'ok': False, 'error': f'preset out of range (1..{len(presets)})', 'preset_count': len(presets)}), 400

    current = presets[preset_index]
    if isinstance(current, dict):
        updated = dict(current)
    else:
        # legacy string format support
        updated = {'time': str(current).strip(), 'name': str(current).strip()}

    updated['time'] = time_str

    # Optional name update
    name_raw = _get_ci(body, 'name', 'label')
    if name_raw is not None:
        updated['name'] = str(name_raw or '').strip() or updated.get('name', '')

    presets[preset_index] = updated

    try:
        if hasattr(utils, 'save_timer_presets'):
            utils.save_timer_presets(presets)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'failed to save presets: {e}'}), 500

    # Keep Companion custom vars in sync for this one preset (best-effort)
    companion_updated = False
    companion_error = None
    try:
        companion_updated, companion_error = _sync_companion_timer_variable_for_preset(
            cfg=cfg,
            preset_number=preset_number,
            preset=updated,
        )
    except Exception:
        companion_updated = False

    if apply_now:
        try:
            _console_append(
                f"[TIMERS] /api/timers/preset apply=true from {request.remote_addr} "
                f"preset={preset_number} time={time_str}\n"
            )
        except Exception:
            pass

        payload, status = _apply_timer_preset_number(preset_number=preset_number, cfg=cfg, presets=presets)
        # Include update info for visibility
        try:
            if isinstance(payload, dict):
                payload['timer_preset'] = updated
                payload['companion_updated'] = companion_updated
                payload['companion_error'] = companion_error
                payload['updated_then_applied'] = True
                if time_was_relative:
                    payload['time_input'] = str(time_raw)
        except Exception:
            pass
        return jsonify(payload), status

    return jsonify({
        'ok': True,
        'preset': preset_number,
        'preset_count': len(presets),
        'timer_preset': updated,
        'companion_updated': companion_updated,
        'companion_error': companion_error,
        'updated_then_applied': False,
        'time_input': str(time_raw) if time_was_relative else None,
    })


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

    # We don't yet know the ProPresenter timer id here; use a placeholder.
    # If ProPresenter is available, we'll re-fire using the real timer id
    # after computing pp_timer_id to keep cancellation semantics consistent.
    press_info = _fire_timer_button_presses_now(
        pp_timer_id=-1,
        preset_number=int(preset_number),
        preset_name=preset_name,
        time_str=time_str,
        button_presses=presses,
    )

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

    try:
        pp_timer_index = int(cfg.get('propresenter_timer_index', cfg.get('timer_index', 1)))
    except Exception:
        pp_timer_index = 1

    # ProPresenter's HTTP API timer IDs are 0-based indices. Keep the config
    # value human-friendly (1-based), but convert for API calls.
    # Backward compatibility: if someone configured 0 explicitly, keep it.
    pp_timer_id = pp_timer_index - 1 if pp_timer_index > 0 else 0

    # Re-fire using the real timer id so subsequent presses replace the prior job.
    try:
        press_info = _fire_timer_button_presses_now(
            pp_timer_id=pp_timer_id,
            preset_number=int(preset_number),
            preset_name=preset_name,
            time_str=time_str,
            button_presses=presses,
        )
    except Exception:
        pass

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
            _console_append('[TIMERS] ProPresenter client not available; skipped timer control\n')
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
        _console_append(
            f"[COMPANION] Received /api/timers/apply from {request.remote_addr} "
            f"args={_truncate_for_log(dict(request.args))} "
            f"json={_truncate_for_log(body_for_log)}\n"
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
        _console_append(f"[PP] /api/propresenter/timer/set timer_id={timer_id} time={time_str} -> {'OK' if set_ok else 'FAIL'}\n")
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
        _console_append(f"[PP] /api/propresenter/timer/start timer_id={timer_id} -> {'OK' if ok else 'FAIL'}\n")
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
        _console_append(f"[PP] /api/propresenter/timer/stop timer_id={timer_id} -> {'OK' if ok else 'FAIL'}\n")
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
        _console_append(f"[PP] /api/propresenter/timer/reset timer_id={timer_id} -> {'OK' if ok else 'FAIL'}\n")
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
        _console_append(f"[PP] /api/propresenter/stage/message -> {'OK' if sent else 'FAIL'}{extra}\n")
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
        _console_append(f"[PP] /api/propresenter/stage/clear -> {'OK' if cleared else 'FAIL'}\n")
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
        _console_append(
            f"[COMPANION] Received /api/propresenter/stage/stream_start from {request.remote_addr} "
            f"args={_truncate_for_log(dict(request.args))} "
            f"json={_truncate_for_log(body_for_log)}\n"
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
        _console_append(
            f"[PP] /api/propresenter/stage/stream_start preset={preset_number} -> {'OK' if sent else 'FAIL'}{extra}\n"
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
        _console_append(
            f"[COMPANION] Received /api/videohub/route from {request.remote_addr} "
            f"args={_truncate_for_log(dict(request.args))} "
            f"json={_truncate_for_log(body)}\n"
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
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    try:
        _home_set_last_videohub_route(output=output_n, input_=input_n, monitor=monitor)
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
                    'actionType': str(getattr(t, 'actionType', 'companion') or 'companion').lower(),
                    'buttonURL': t.buttonURL,
                    'api': getattr(t, 'api', None) if str(getattr(t, 'actionType', 'companion') or 'companion').lower() == 'api' else None,
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
            btn_final = str(t3.get('buttonURL') or '') if action_type != 'api' else ''
            api_obj = t3.get('api') if action_type == 'api' else None

            times.append(TimeOfTrigger(mins, typ, btn_final, actionType=action_type, api=api_obj))

        # replace fields on existing event object
        ev.name = name
        ev.day = WeekDay[day] if day in WeekDay.__members__ else WeekDay.Monday
        ev.date = date_obj
        ev.time = time_obj
        ev.repeating = repeating
        ev.active = active
        ev.times = times
        ev.times.sort()

        storage.save_events(events, events_file)
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
            btn_final = str(t3.get('buttonURL') or '') if action_type != 'api' else ''
            api_obj = t3.get('api') if action_type == 'api' else None

            times.append(TimeOfTrigger(mins, typ, btn_final, actionType=action_type, api=api_obj))

        ev = Event(name, new_id, WeekDay[day] if day in WeekDay.__members__ else WeekDay.Monday, date_obj, time_obj, repeating, times, active)
        events.append(ev)
        storage.save_events(events, events_file)
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


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
