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
            app_inst = get_app(name)
            if app_inst is None:
                continue
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


def start_http_server(host: str, port: int) -> None:
    global _http_server, _server_thread
    with _server_lock:
        if _http_server is not None:
            return
        srv = make_server(host, port, app)
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

    running_apps: list[str] = []
    try:
        regs = list_apps()
        for name in regs.keys():
            running = False
            try:
                inst = get_app(name)
                if inst is not None and hasattr(inst, 'status'):
                    st = inst.status() or {}
                    running = bool(st.get('running', False))
                else:
                    running = name in _running_apps
            except Exception:
                running = name in _running_apps

            if running:
                running_apps.append(name)
    except Exception:
        running_apps = []

    running_apps = sorted(set(running_apps))

    return render_template(
        'home.html',
        events_file=events_file,
        companion_ip=companion_ip,
        running_apps=running_apps,
    )


@app.route('/apps')
def apps_page():
    return render_template('apps.html')


@app.route('/calendar')
def calendar_page():
    return render_template('calendar.html')


@app.route('/templates')
def templates_page():
    return render_template('templates.html')


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
    label = body.get('label')
    pattern = (body.get('pattern') or '').strip()
    if not label or not pattern:
        return jsonify({'ok': False, 'error': 'label and pattern required'}), 400
    # validate pattern: must be three integers separated by '/'
    if not re.match(r'^\d+\/\d+\/\d+$', pattern):
        return jsonify({'ok': False, 'error': 'pattern must be like "1/0/1" (three integers separated by "/")'}), 400

    arr = _read_json_file(BUTTON_TEMPLATES)
    button_url = f"location/{pattern}/press"
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
    arr = _read_json_file(TRIGGER_TEMPLATES)
    arr.append({'label': label, 'times': times})
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
    arr = _read_json_file(TRIGGER_TEMPLATES)
    if idx < 0 or idx >= len(arr):
        return jsonify({'ok': False, 'error': 'index out of range'}), 404
    arr[idx] = {'label': label, 'times': times}
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


@app.route('/api/apps')
def api_list_apps():
    regs = list_apps()
    out = []
    for name, factory in regs.items():
        running = False
        # prefer asking the singleton instance for its status()
        try:
            inst = get_app(name)
            if inst is not None and hasattr(inst, 'status'):
                st = inst.status() or {}
                # if status reports running, use that
                running = bool(st.get('running', False))
            else:
                running = name in _running_apps
        except Exception:
            running = name in _running_apps

        out.append({
            'name': name,
            'running': running,
        })
    return jsonify(out)


@app.route('/api/apps/<name>/start', methods=['POST'])
def api_start_app(name: str):
    regs = list_apps()
    if name not in regs:
        return jsonify({'ok': False, 'error': 'unknown app'}), 404
    # prefer the singleton instance so UI controls the same app the server uses
    try:
        inst = get_app(name)
        if inst is None:
            return jsonify({'ok': False, 'error': 'failed to construct app instance'}), 500

        # if app reports it's already running, return success
        try:
            st = inst.status() or {}
            if st.get('running'):
                _running_apps[name] = inst
                return jsonify({'ok': True, 'msg': 'already running'})
        except Exception:
            pass

        # start non-blocking
        try:
            inst.start(blocking=False)
        except TypeError:
            threading.Thread(target=lambda inst=inst: inst.start(), daemon=True).start()
        except Exception:
            pass

        _running_apps[name] = inst
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/apps/<name>/stop', methods=['POST'])
def api_stop_app(name: str):
    # prefer the singleton instance
    try:
        inst = get_app(name)
        if inst is None:
            return jsonify({'ok': False, 'error': 'unknown app'}), 404

        try:
            inst.stop()
        except Exception:
            pass

        # ensure we remove any record in our running map
        try:
            _running_apps.pop(name, None)
        except Exception:
            pass

        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/companion_status')
def companion_status():
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
    return jsonify({'connected': status})


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

    normalized_presets: list[dict[str, str]] = []
    for v in presets:
        if isinstance(v, dict):
            time_str = str(v.get('time', '')).strip()
            name_str = str(v.get('name', '')).strip()
        else:
            time_str = str(v).strip()
            name_str = ''

        if not time_str:
            continue
        if not _validate_time_hhmm(time_str):
            return jsonify({'ok': False, 'error': f'invalid time: {time_str}. Use HH:MM'}), 400

        # Always ensure each timer has a name; default to its time.
        if not name_str:
            name_str = time_str

        normalized_presets.append({'time': time_str, 'name': name_str})

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

    if ProPresentor is None:
        return jsonify({'ok': False, 'error': 'propresentor client not available'}), 500

    body = request.get_json(silent=True) or {}

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
    if not _validate_time_hhmm(time_str):
        return jsonify({'ok': False, 'error': f'invalid preset time in config: {time_str}'}), 500

    try:
        pp_timer_index = int(cfg.get('propresenter_timer_index', cfg.get('timer_index', 1)))
    except Exception:
        pp_timer_index = 1

    ip = str(cfg.get('propresenter_ip', '127.0.0.1'))
    try:
        port = int(cfg.get('propresenter_port', 1025))
    except Exception:
        return jsonify({'ok': False, 'error': 'propresenter_port must be an integer'}), 400

    pp = ProPresentor(ip, port)
    set_ok = bool(pp.SetCountdownToTime(pp_timer_index, time_str))
    if not set_ok:
        return jsonify({
            'ok': False,
            'error': 'failed to set timer (check ProPresenter connection and timer index)',
            'preset': preset_number,
            'preset_count': len(presets),
            'time': time_str,
            'propresenter_timer_index': pp_timer_index,
            'set': False,
            'reset': False,
            'started': False,
            'propresenter_ip': ip,
            'propresenter_port': port,
        }), 502

    # ProPresenter often needs a reset/restart after changing timer config
    # for the UI to reflect the new time correctly.
    reset_ok = bool(pp.timer_operation(pp_timer_index, 'reset'))
    if not reset_ok:
        return jsonify({
            'ok': False,
            'error': 'timer set, but failed to reset (check ProPresenter timer state/permissions)',
            'preset': preset_number,
            'preset_count': len(presets),
            'time': time_str,
            'propresenter_timer_index': pp_timer_index,
            'set': True,
            'reset': False,
            'started': False,
            'propresenter_ip': ip,
            'propresenter_port': port,
        }), 502

    # Start countdown immediately (per OpenAPI: GET /v1/timer/{id}/{operation})
    start_ok = bool(pp.timer_operation(pp_timer_index, 'start'))

    if not start_ok:
        return jsonify({
            'ok': False,
            'error': 'timer set, but failed to start (check ProPresenter timer state/permissions)',
            'preset': preset_number,
            'preset_count': len(presets),
            'time': time_str,
            'propresenter_timer_index': pp_timer_index,
            'set': True,
            'reset': True,
            'started': False,
            'propresenter_ip': ip,
            'propresenter_port': port,
        }), 502

    return jsonify({
        'ok': True,
        'preset': preset_number,
        'preset_count': len(presets),
        'time': time_str,
        'propresenter_timer_index': pp_timer_index,
        'set': True,
        'reset': True,
        'started': True,
        'propresenter_ip': ip,
        'propresenter_port': port,
    })


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
            try:
                mins = int(t.get('minutes', 0) or 0)
            except Exception:
                return jsonify({'ok': False, 'error': f"Invalid minutes value: {t.get('minutes')}"}), 400
            if mins < 0:
                return jsonify({'ok': False, 'error': f"Minutes must be >= 0: {mins}"}), 400

            typ_name = t.get('typeOfTrigger', 'AT')
            if typ_name not in TypeofTime.__members__:
                return jsonify({'ok': False, 'error': f"Invalid typeOfTrigger: {typ_name}"}), 400
            typ = TypeofTime[typ_name]

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
            try:
                mins = int(t.get('minutes', 0) or 0)
            except Exception:
                return jsonify({'ok': False, 'error': f"Invalid minutes value: {t.get('minutes')}"}), 400
            if mins < 0:
                return jsonify({'ok': False, 'error': f"Minutes must be >= 0: {mins}"}), 400

            # typeOfTrigger must map to TypeofTime
            typ_name = t.get('typeOfTrigger', 'AT')
            if typ_name not in TypeofTime.__members__:
                return jsonify({'ok': False, 'error': f"Invalid typeOfTrigger: {typ_name}"}), 400
            typ = TypeofTime[typ_name]

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
