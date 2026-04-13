from __future__ import annotations

import json
import queue
import socket
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import requests
import sounddevice as sd


SENDER_CONFIG_FILE = 'transcription_sender_config.json'
SENDER_PID_FILE = 'transcription_sender_ui.pid'
DEFAULT_LOCAL_PORT = 8766

_DEFAULT_CONFIG: dict[str, Any] = {
    'server': 'http://127.0.0.1:5000',
    'token': '',
    'device': '',
    'source_name': socket.gethostname(),
    'sample_rate': 16000,
    'chunk_ms': 200,
    'channels': 1,
    'ui_port': DEFAULT_LOCAL_PORT,
    'open_browser_on_launch': True,
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def sender_config_path() -> Path:
    return _repo_root() / SENDER_CONFIG_FILE


def sender_pid_path() -> Path:
    return _repo_root() / SENDER_PID_FILE


def load_sender_config() -> dict[str, Any]:
    p = sender_config_path()
    cfg = dict(_DEFAULT_CONFIG)
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding='utf-8') or '{}')
            if isinstance(raw, dict):
                cfg.update(raw)
    except Exception:
        pass
    return normalize_sender_config(cfg)


def normalize_sender_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(_DEFAULT_CONFIG)
    if isinstance(raw, dict):
        data.update(raw)

    data['server'] = str(data.get('server') or '').strip().rstrip('/') or _DEFAULT_CONFIG['server']
    data['token'] = str(data.get('token') or '').strip()
    data['device'] = str(data.get('device') or '').strip()
    data['source_name'] = str(data.get('source_name') or '').strip() or socket.gethostname()

    try:
        data['sample_rate'] = max(8000, min(96000, int(data.get('sample_rate', 16000))))
    except Exception:
        data['sample_rate'] = 16000

    try:
        data['chunk_ms'] = max(40, min(4000, int(data.get('chunk_ms', 200))))
    except Exception:
        data['chunk_ms'] = 200

    try:
        ch = int(data.get('channels', 1))
    except Exception:
        ch = 1
    data['channels'] = 2 if ch == 2 else 1

    try:
        data['ui_port'] = max(1024, min(65535, int(data.get('ui_port', DEFAULT_LOCAL_PORT))))
    except Exception:
        data['ui_port'] = DEFAULT_LOCAL_PORT

    data['open_browser_on_launch'] = bool(data.get('open_browser_on_launch', True))
    return data


def save_sender_config(cfg: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_sender_config(cfg)
    p = sender_config_path()
    p.write_text(json.dumps(normalized, indent=2), encoding='utf-8')
    return normalized


def list_input_devices() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        devices = sd.query_devices()
    except Exception:
        return out
    for idx, dev in enumerate(devices):
        try:
            max_inputs = int(dev.get('max_input_channels', 0) or 0)
        except Exception:
            max_inputs = 0
        if max_inputs <= 0:
            continue
        try:
            samplerate = int(float(dev.get('default_samplerate', 0) or 0))
        except Exception:
            samplerate = 0
        out.append({
            'id': str(idx),
            'name': str(dev.get('name', f'Device {idx}') or f'Device {idx}'),
            'max_input_channels': max_inputs,
            'default_samplerate': samplerate,
        })
    return out


def device_value_to_sounddevice_arg(value: str | None):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return s


class SenderService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._audio_thread: threading.Thread | None = None
        self._upload_thread: threading.Thread | None = None
        self._queue: queue.Queue[bytes] | None = None
        self._session = requests.Session()
        self._source_id = str(uuid.uuid4())
        self._status = 'idle'
        self._last_error = ''
        self._last_ok_at: float | None = None
        self._current_config = load_sender_config()
        self._active_device_name = ''
        self._status_since = time.time()

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._current_config)

    def save_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        normalized = save_sender_config(cfg)
        with self._lock:
            self._current_config = normalized
        return normalized

    def _set_status(self, status: str, error: str = '') -> None:
        with self._lock:
            self._status = str(status or 'idle')
            self._last_error = str(error or '')
            self._status_since = time.time()

    def status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                'status': self._status,
                'last_error': self._last_error,
                'last_ok_at': self._last_ok_at,
                'active_device_name': self._active_device_name,
                'status_since': self._status_since,
                'config': dict(self._current_config),
                'streaming': self._audio_thread is not None and self._audio_thread.is_alive(),
            }

    def stop(self) -> None:
        self._stop_event.set()
        audio_thread = self._audio_thread
        upload_thread = self._upload_thread
        if audio_thread is not None and audio_thread.is_alive():
            audio_thread.join(timeout=2)
        if upload_thread is not None and upload_thread.is_alive():
            upload_thread.join(timeout=2)
        with self._lock:
            self._audio_thread = None
            self._upload_thread = None
            self._queue = None
            self._active_device_name = ''
        self._stop_event = threading.Event()
        self._set_status('idle', '')

    def start(self, cfg: dict[str, Any] | None = None) -> tuple[bool, str | None]:
        if cfg is not None:
            self.save_config(cfg)
        config = self.get_config()
        if not str(config.get('server') or '').strip():
            return False, 'Server URL is required.'
        if not str(config.get('token') or '').strip():
            return False, 'Token is required.'
        if self.status_snapshot().get('streaming'):
            return True, None

        self.stop()
        q: queue.Queue[bytes] = queue.Queue(maxsize=32)
        self._queue = q
        self._stop_event = threading.Event()
        self._set_status('connecting', '')

        blocksize = max(1, int((int(config['sample_rate']) * max(40, int(config['chunk_ms']))) / 1000))

        def audio_loop():
            device_arg = device_value_to_sounddevice_arg(str(config.get('device') or ''))
            selected = ''
            for dev in list_input_devices():
                if str(dev['id']) == str(config.get('device') or ''):
                    selected = str(dev['name'])
                    break
            with self._lock:
                self._active_device_name = selected

            def audio_callback(indata, frames, time_info, status):
                if status:
                    self._set_status('error', f'Audio device status: {status}')
                try:
                    q.put_nowait(bytes(indata))
                except queue.Full:
                    try:
                        q.get_nowait()
                    except Exception:
                        pass
                    try:
                        q.put_nowait(bytes(indata))
                    except Exception:
                        pass

            try:
                with sd.RawInputStream(
                    samplerate=int(config['sample_rate']),
                    blocksize=blocksize,
                    dtype='int16',
                    channels=int(config['channels']),
                    device=device_arg,
                    callback=audio_callback,
                ):
                    while not self._stop_event.is_set():
                        time.sleep(0.2)
            except Exception as e:
                self._set_status('error', str(e))
                self._stop_event.set()

        def upload_loop():
            backoff = 1.0
            post_url = f"{str(config['server']).rstrip('/')}/api/transcription/audio"
            while not self._stop_event.is_set():
                try:
                    chunk = q.get(timeout=0.5)
                except queue.Empty:
                    continue

                try:
                    res = self._session.post(
                        post_url,
                        data=chunk,
                        timeout=10,
                        headers={
                            'Content-Type': 'application/octet-stream',
                            'X-TDeck-Transcription-Token': str(config['token']),
                            'X-Source-Id': self._source_id,
                            'X-Source-Name': str(config['source_name']),
                            'X-Audio-Sample-Rate': str(int(config['sample_rate'])),
                            'X-Audio-Channels': str(int(config['channels'])),
                            'X-Audio-Sample-Width': '2',
                        },
                    )
                    if res.ok:
                        with self._lock:
                            self._last_ok_at = time.time()
                        self._set_status('streaming', '')
                        backoff = 1.0
                        continue
                    self._set_status('error', f'HTTP {res.status_code}: {res.text[:200]}')
                except Exception as e:
                    self._set_status('error', str(e))

                time.sleep(backoff)
                backoff = min(backoff * 1.8, 8.0)

        self._audio_thread = threading.Thread(target=audio_loop, daemon=True)
        self._upload_thread = threading.Thread(target=upload_loop, daemon=True)
        self._audio_thread.start()
        self._upload_thread.start()
        return True, None

    def test_connection(self, cfg: dict[str, Any] | None = None) -> tuple[bool, str, dict[str, Any] | None]:
        config = normalize_sender_config(cfg or self.get_config())
        server = str(config.get('server') or '').rstrip('/')
        if not server:
            return False, 'Server URL is required.', None
        try:
            res = self._session.get(f'{server}/api/transcription/config/test', timeout=8)
        except Exception as e:
            return False, str(e), None
        try:
            data = res.json()
        except Exception:
            data = None
        if not res.ok:
            return False, f'HTTP {res.status_code}', data if isinstance(data, dict) else None
        if isinstance(data, dict):
            return True, '', data
        return True, '', None


def open_local_browser(port: int) -> None:
    try:
        webbrowser.open(f'http://127.0.0.1:{int(port)}')
    except Exception:
        pass
