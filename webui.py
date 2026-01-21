from flask import Flask, render_template, jsonify, request
import logging
import threading
import time
from pathlib import Path
import sys
import subprocess
import shlex
from collections import deque

from werkzeug.serving import make_server
import json
import re
from datetime import datetime

from package.core import list_apps, get_app
import package.apps  # noqa: F401
try:
    from package.apps.calendar import utils
except Exception:
    # Fallback lightweight utils if importing the calendar utils fails (e.g., missing companion/requests)
    import json

    def _load_cfg():
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                return json.load(f) or {}
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


@app.context_processor
def _inject_theme():
    try:
        cfg = utils.get_config() if hasattr(utils, 'get_config') else {}
        dark_mode = bool(cfg.get('dark_mode', False))
    except Exception:
        dark_mode = False
    return {
        'dark_mode': dark_mode,
        'bs_theme': 'dark' if dark_mode else 'light',
    }


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

# Upcoming trigger cache (to avoid recomputing schedule for each client refresh)
_upcoming_triggers_cache = {'ts': 0.0, 'events_file': '', 'payload': None}
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


@app.route('/')
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
            url = str(getattr(trig, 'buttonURL', '') or '').strip() if trig is not None else ''
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

            tpl = tpl_by_url.get(url) if url else None
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
                    'buttonURL': url,
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

    now = time.time()
    with _status_cache_lock:
        if (
            _upcoming_triggers_cache.get('payload') is not None
            and _upcoming_triggers_cache.get('events_file') == events_file
            and (now - float(_upcoming_triggers_cache.get('ts', 0.0))) < _UPCOMING_TRIGGERS_TTL_SECONDS
        ):
            return jsonify(_upcoming_triggers_cache.get('payload'))

    payload = _compute_upcoming_triggers_payload(events_file=events_file, limit=3)

    with _status_cache_lock:
        _upcoming_triggers_cache['ts'] = now
        _upcoming_triggers_cache['events_file'] = events_file
        _upcoming_triggers_cache['payload'] = payload

    resp = jsonify(payload)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/calendar')
def calendar_page():
    return render_template('calendar.html')


@app.route('/templates')
def templates_page():
    return render_template('templates.html')


@app.route('/videohub')
def videohub_page():
    return render_template('videohub.html')


@app.route('/timers')
def timers_page():
    return render_template('timers.html')


@app.route('/config')
def config_page():
    return render_template('config.html')


@app.route('/console')
def console_page():
    return render_template('console.html')


@app.route('/calendar/new')
def calendar_new_page():
    return render_template('calendar_new.html')


@app.route('/calendar/edit/<int:ident>')
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
        normalized_times.append(t2)
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
        normalized_times.append(t2)
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
                    {'minutes': t.minutes, 'typeOfTrigger': getattr(t.typeOfTrigger, 'name', str(t.typeOfTrigger)), 'buttonURL': t.buttonURL}
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
    return jsonify({'connected': status})


@app.route('/api/propresenter_status')
def propresenter_status():
    """Lightweight ProPresenter connectivity check for the UI indicator.

    Intentionally does not print to console to avoid noisy logs.
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

    return jsonify({'connected': bool(status)})


@app.route('/api/config', methods=['GET'])
def api_get_config():
    try:
        cfg = utils.get_config()
        return jsonify(cfg)
    except Exception:
        return jsonify({})


@app.route('/api/config', methods=['POST'])
def api_set_config():
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
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


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
            _console_append(f"[VIDEOHUB] Applied preset #{preset_id}\n")
        except Exception:
            pass
        return jsonify({'ok': True, 'result': result})
    except KeyError:
        return jsonify({'ok': False, 'error': 'preset not found'}), 404
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


_BTN_FULL_RE = re.compile(r'^location/\d+/\d+/\d+/press$')
_BTN_SHORT_RE = re.compile(r'^\d+/\d+/\d+$')


def _normalize_companion_button_url(raw: str) -> str | None:
    s = (raw or '').strip()
    if not s:
        return None
    if _BTN_FULL_RE.match(s):
        return s
    if _BTN_SHORT_RE.match(s):
        return f'location/{s}/press'
    return None


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

    return jsonify({
        'propresenter_timer_index': propresenter_timer_index,
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

                    value = f"{label}: {pretty_time}"

                    if not comp.SetVariable(var_name, value):
                        companion_failed += 1
        except Exception:
            companion_updated = False

        return jsonify({
            'ok': True,
            'timer_presets': normalized_presets,
            'propresenter_timer_index': cfg.get('propresenter_timer_index', 1),
            'companion_names_updated': companion_updated,
            'companion_names_failed': companion_failed,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


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

    if not presets:
        return jsonify({'ok': False, 'error': 'no presets configured (timer_presets.json is empty)', 'preset_count': 0}), 400

    preset_index = preset_number - 1
    if preset_index < 0 or preset_index >= len(presets):
        return jsonify({'ok': False, 'error': f'preset out of range (1..{len(presets)})', 'preset_count': len(presets)}), 400

    selected = presets[preset_index]
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
        preset_number=preset_number,
        preset_name=preset_name,
        time_str=time_str,
        button_presses=presses,
    )

    # Keep original time validation for timer control, but don't prevent button presses.
    if not _validate_time_hhmm(time_str):
        return jsonify({
            'ok': True,
            'preset': preset_number,
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
        })

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
            preset_number=preset_number,
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
        return jsonify({
            'ok': True,
            'preset': preset_number,
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
        })

    # If ProPresenter client is missing, still succeed for Companion presses.
    if ProPresentor is None:
        try:
            _console_append('[TIMERS] ProPresenter client not available; skipped timer control\n')
        except Exception:
            pass
        return jsonify({
            'ok': True,
            'preset': preset_number,
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
        })

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
            return jsonify({
                'ok': True,
                'error': 'failed to stop timer (legacy sequence)',
                'preset': preset_number,
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
            })

        if wait_stop_ms:
            time.sleep(wait_stop_ms / 1000.0)

        set_ok = bool(pp.SetCountdownToTime(pp_timer_id, time_str))
        if not set_ok:
            return jsonify({
                'ok': True,
                'error': 'failed to set timer (legacy sequence)',
                'preset': preset_number,
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
            })

        if wait_set_ms:
            time.sleep(wait_set_ms / 1000.0)

        reset_ok = bool(pp.timer_operation(pp_timer_id, 'reset'))
        if not reset_ok:
            return jsonify({
                'ok': True,
                'error': 'timer set, but failed to reset (legacy sequence)',
                'preset': preset_number,
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
            })

        if wait_reset_ms:
            time.sleep(wait_reset_ms / 1000.0)

        start_ok = bool(pp.timer_operation(pp_timer_id, 'start'))
        if not start_ok:
            return jsonify({
                'ok': True,
                'error': 'timer set, but failed to start (legacy sequence)',
                'preset': preset_number,
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
            })

        return jsonify({
            'ok': True,
            'preset': preset_number,
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
        })

    # Normal flow (latest versions): set -> reset -> start
    set_ok = bool(pp.SetCountdownToTime(pp_timer_id, time_str))
    if not set_ok:
        return jsonify({
            'ok': True,
            'error': 'failed to set timer (check ProPresenter connection and timer index)',
            'preset': preset_number,
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
        })

    # ProPresenter often needs a reset/restart after changing timer config
    # for the UI to reflect the new time correctly.
    reset_ok = bool(pp.timer_operation(pp_timer_id, 'reset'))
    if not reset_ok:
        return jsonify({
            'ok': True,
            'error': 'timer set, but failed to reset (check ProPresenter timer state/permissions)',
            'preset': preset_number,
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
        })

    # Start countdown immediately (per OpenAPI: GET /v1/timer/{id}/{operation})
    start_ok = bool(pp.timer_operation(pp_timer_id, 'start'))

    if not start_ok:
        return jsonify({
            'ok': True,
            'error': 'timer set, but failed to start (check ProPresenter timer state/permissions)',
            'preset': preset_number,
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
        })

    return jsonify({
        'ok': True,
        'preset': preset_number,
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
                {'minutes': t.minutes, 'typeOfTrigger': getattr(t.typeOfTrigger, 'name', str(t.typeOfTrigger)), 'buttonURL': t.buttonURL}
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

            btn_raw = (t.get('buttonURL') or '').strip()
            btn_final = ''
            if btn_raw:
                if _re.match(r'^location/\d+/\d+/\d+/press$', btn_raw):
                    btn_final = btn_raw
                elif _re.match(r'^\d+/\d+/\d+$', btn_raw):
                    btn_final = f'location/{btn_raw}/press'
                else:
                    return jsonify({'ok': False, 'error': f"Invalid buttonURL format: {btn_raw}. Use '1/2/3' or 'location/1/2/3/press'"}), 400
            else:
                btn_final = ''

            times.append(TimeOfTrigger(mins, typ, btn_final))

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

            # buttonURL handling: prefer a full button URL from templates (location/x/y/z/press)
            btn_raw = (t.get('buttonURL') or '').strip()
            btn_final = ''
            if btn_raw:
                # if already a full URL like location/.../press, accept
                if _re.match(r'^location/\d+/\d+/\d+/press$', btn_raw):
                    btn_final = btn_raw
                # if it's a short pattern like 1/2/3, convert
                elif _re.match(r'^\d+/\d+/\d+$', btn_raw):
                    btn_final = f'location/{btn_raw}/press'
                else:
                    return jsonify({'ok': False, 'error': f"Invalid buttonURL format: {btn_raw}. Use '1/2/3' or 'location/1/2/3/press'"}), 400
            else:
                btn_final = ''

            times.append(TimeOfTrigger(mins, typ, btn_final))

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
