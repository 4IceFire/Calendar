from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import numpy as _np
except Exception:  # pragma: no cover - optional at runtime until deps are installed
    _np = None

try:
    from RealtimeSTT import AudioToTextRecorder
except Exception:  # pragma: no cover - optional at runtime until deps are installed
    AudioToTextRecorder = None  # type: ignore[assignment]


class TranscriptionService:
    TARGET_SAMPLE_RATE = 16000
    TARGET_CHANNELS = 1
    TARGET_SAMPLE_WIDTH = 2

    def __init__(self, get_config, console_log=None) -> None:
        self._get_config = get_config
        self._console_log = console_log
        self._lock = threading.RLock()
        self._update_cond = threading.Condition(self._lock)
        self._recorder = None
        self._recorder_signature: tuple[Any, ...] | None = None
        self._finalizer_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._version = 0
        self._history_path = self._resolve_history_path()
        self._sessions = self._load_history()
        self._segments: list[dict[str, Any]] = []
        self._live_text = ''
        self._stabilized_text = ''
        self._service_error = ''
        self._active_sender: dict[str, Any] | None = None
        self._last_audio_ts: float | None = None
        self._last_segment_end_ts: float | None = None
        self._session_started_at: float | None = None
        self._session_id: str = self._new_session_id()
        self._recorder_ready = False
        self._receiver_bytes = 0
        self._action_seq = 0
        self._recent_action_events: list[dict[str, Any]] = []

    def _keyword_rules_path(self) -> Path:
        try:
            data_dir = Path('/data')
            if data_dir.exists() and data_dir.is_dir():
                return data_dir / 'transcription_keyword_rules.json'
        except Exception:
            pass
        return Path(__file__).resolve().parents[3] / 'transcription_keyword_rules.json'

    def get_available_actions(self) -> list[dict[str, str]]:
        return [
            {
                'id': 'flash_red_twice',
                'label': 'Flash Red Twice',
                'description': 'Flashes the transcription display red twice in a paging-style alert.',
            },
        ]

    def _normalize_keyword_rule(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        keyword = str(raw.get('keyword') or '').strip()
        action_id = str(raw.get('action_id') or raw.get('actionId') or '').strip()
        if not keyword or not action_id:
            return None
        valid_ids = {a['id'] for a in self.get_available_actions()}
        if action_id not in valid_ids:
            return None
        return {
            'id': str(raw.get('id') or uuid.uuid4().hex).strip() or uuid.uuid4().hex,
            'enabled': bool(raw.get('enabled', True)),
            'keyword': keyword,
            'action_id': action_id,
            'label': str(raw.get('label') or '').strip(),
        }

    def load_keyword_rules(self) -> list[dict[str, Any]]:
        p = self._keyword_rules_path()
        try:
            if not p.exists():
                return []
            raw = json.loads(p.read_text(encoding='utf-8') or '[]')
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            normalized = self._normalize_keyword_rule(item)
            if not normalized:
                continue
            rid = str(normalized['id'])
            if rid in seen:
                continue
            seen.add(rid)
            out.append(normalized)
        return out

    def save_keyword_rule(self, raw: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str | None]:
        normalized = self._normalize_keyword_rule(raw)
        if not normalized:
            return False, None, 'Keyword and action are required.'
        rules = self.load_keyword_rules()
        replaced = False
        for idx, item in enumerate(rules):
            if str(item.get('id')) == str(normalized['id']):
                rules[idx] = normalized
                replaced = True
                break
        if not replaced:
            rules.append(normalized)
        p = self._keyword_rules_path()
        try:
            p.write_text(json.dumps(rules, indent=2), encoding='utf-8')
        except Exception as e:
            return False, None, str(e)
        with self._lock:
            self._touch_locked()
        return True, normalized, None

    def delete_keyword_rule(self, rule_id: str) -> tuple[bool, str | None]:
        rid = str(rule_id or '').strip()
        if not rid:
            return False, 'Rule id is required.'
        rules = [r for r in self.load_keyword_rules() if str(r.get('id')) != rid]
        p = self._keyword_rules_path()
        try:
            p.write_text(json.dumps(rules, indent=2), encoding='utf-8')
        except Exception as e:
            return False, str(e)
        with self._lock:
            self._touch_locked()
        return True, None

    def _dispatch_action_locked(self, action_id: str, *, keyword: str, segment_text: str, rule_id: str) -> None:
        if action_id != 'flash_red_twice':
            return
        self._action_seq += 1
        self._recent_action_events.append({
            'seq': self._action_seq,
            'id': uuid.uuid4().hex,
            'action_id': action_id,
            'target': 'display',
            'keyword': keyword,
            'rule_id': rule_id,
            'segment_text': segment_text,
            'created_at': time.time(),
        })
        self._recent_action_events = self._recent_action_events[-20:]
        self._touch_locked()

    def _log(self, msg: str) -> None:
        try:
            if self._console_log is not None:
                self._console_log(msg.rstrip() + '\n')
        except Exception:
            pass

    def _resolve_history_path(self) -> Path:
        try:
            data_dir = Path('/data')
            if data_dir.exists() and data_dir.is_dir():
                return data_dir / 'transcription_sessions.json'
        except Exception:
            pass
        return Path(__file__).resolve().parents[3] / 'transcription_sessions.json'

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            if not self._history_path.exists():
                return []
            raw = json.loads(self._history_path.read_text(encoding='utf-8') or '[]')
            return raw if isinstance(raw, list) else []
        except Exception:
            return []

    def _save_history_locked(self) -> None:
        cfg = self._cfg()
        if not bool(cfg.get('transcription_keep_history', False)):
            return
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            trimmed = list(self._sessions)[-self._history_limit(cfg):]
            tmp = self._history_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(trimmed, indent=2), encoding='utf-8')
            tmp.replace(self._history_path)
        except Exception:
            pass

    def _cfg(self) -> dict[str, Any]:
        try:
            cfg = self._get_config() or {}
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _history_limit(self, cfg: dict[str, Any] | None = None) -> int:
        c = cfg or self._cfg()
        try:
            n = int(c.get('transcription_history_limit', 10))
        except Exception:
            n = 10
        return max(1, min(n, 200))

    def _new_session_id(self) -> str:
        return str(uuid.uuid4())

    def _touch_locked(self) -> None:
        self._version += 1
        self._update_cond.notify_all()

    def _recorder_signature_from_cfg(self, cfg: dict[str, Any]) -> tuple[Any, ...]:
        return (
            bool(cfg.get('transcription_enabled', False)),
            str(cfg.get('transcription_model', 'small.en')).strip(),
            str(cfg.get('transcription_realtime_model', 'tiny.en')).strip(),
            str(cfg.get('transcription_language', 'en')).strip(),
            str(cfg.get('transcription_device', 'cpu')).strip().lower(),
            bool(cfg.get('transcription_enable_realtime', True)),
        )

    def _recorder_params_from_cfg(self, cfg: dict[str, Any]) -> dict[str, Any]:
        return {
            'use_microphone': False,
            'spinner': False,
            'model': str(cfg.get('transcription_model', 'small.en')).strip() or 'small.en',
            'language': str(cfg.get('transcription_language', 'en')).strip(),
            'device': str(cfg.get('transcription_device', 'cpu')).strip().lower() or 'cpu',
            'enable_realtime_transcription': bool(cfg.get('transcription_enable_realtime', True)),
            'realtime_model_type': str(cfg.get('transcription_realtime_model', 'tiny.en')).strip() or 'tiny.en',
            'realtime_processing_pause': 0.12,
            'beam_size_realtime': 1,
            'beam_size': 3,
            'ensure_sentence_starting_uppercase': True,
            'ensure_sentence_ends_with_period': False,
            'silero_sensitivity': 0.2,
            'webrtc_sensitivity': 2,
            'post_speech_silence_duration': 0.35,
            'min_gap_between_recordings': 0,
            'min_length_of_recording': 0.25,
            'on_realtime_transcription_update': self._on_realtime_update,
            'on_realtime_transcription_stabilized': self._on_realtime_stabilized,
            'on_recording_start': self._on_recording_start,
            'on_recording_stop': self._on_recording_stop,
        }

    def _shutdown_recorder_locked(self) -> None:
        self._stop_event.set()
        recorder = self._recorder
        self._recorder = None
        self._recorder_signature = None
        self._recorder_ready = False
        if recorder is not None:
            try:
                shutdown = getattr(recorder, 'shutdown', None)
                if callable(shutdown):
                    shutdown()
            except Exception:
                pass

    def ensure_recorder(self) -> tuple[bool, str | None]:
        cfg = self._cfg()
        if not bool(cfg.get('transcription_enabled', False)):
            with self._lock:
                self._shutdown_recorder_locked()
                self._service_error = ''
                self._touch_locked()
            return False, 'Transcription is disabled in config.'

        if AudioToTextRecorder is None:
            with self._lock:
                self._shutdown_recorder_locked()
                self._service_error = 'RealtimeSTT is not installed on this server.'
                self._touch_locked()
            return False, 'RealtimeSTT is not installed on this server.'

        signature = self._recorder_signature_from_cfg(cfg)
        with self._lock:
            if self._recorder is not None and self._recorder_signature == signature:
                return True, None

            self._shutdown_recorder_locked()
            self._stop_event = threading.Event()
            self._service_error = ''

            try:
                self._recorder = AudioToTextRecorder(**self._recorder_params_from_cfg(cfg))
                self._recorder_signature = signature
                self._recorder_ready = True
                self._finalizer_thread = threading.Thread(target=self._finalizer_loop, daemon=True)
                self._finalizer_thread.start()
                self._log('[TRANSCRIPTION] Recorder initialised')
                self._touch_locked()
                return True, None
            except Exception as e:
                self._recorder = None
                self._recorder_signature = None
                self._recorder_ready = False
                self._service_error = str(e)
                self._log(f'[TRANSCRIPTION] Recorder init failed: {e}')
                self._touch_locked()
                return False, str(e)

    def _sender_timeout_seconds(self, cfg: dict[str, Any] | None = None) -> float:
        c = cfg or self._cfg()
        try:
            chunk_ms = int(c.get('transcription_chunk_ms', 200))
        except Exception:
            chunk_ms = 200
        return max(4.0, min(20.0, (chunk_ms / 1000.0) * 8.0))

    def _session_archive_from_locked(self) -> dict[str, Any] | None:
        if not self._segments:
            return None
        return {
            'id': self._session_id,
            'started_at': self._session_started_at,
            'ended_at': time.time(),
            'sender': dict(self._active_sender) if self._active_sender else None,
            'segments': list(self._segments),
        }

    def _archive_current_session_locked(self) -> None:
        cfg = self._cfg()
        if not bool(cfg.get('transcription_keep_history', False)):
            return
        archived = self._session_archive_from_locked()
        if not archived:
            return
        self._sessions.append(archived)
        self._sessions = self._sessions[-self._history_limit(cfg):]
        self._save_history_locked()

    def clear_session(self) -> None:
        with self._lock:
            self._archive_current_session_locked()
            self._segments = []
            self._live_text = ''
            self._stabilized_text = ''
            self._last_segment_end_ts = None
            self._session_started_at = None
            self._receiver_bytes = 0
            self._session_id = self._new_session_id()
            self._touch_locked()

    def stop_session(self) -> None:
        with self._lock:
            self._archive_current_session_locked()
            self._shutdown_recorder_locked()
            self._live_text = ''
            self._stabilized_text = ''
            self._active_sender = None
            self._last_audio_ts = None
            self._last_segment_end_ts = None
            self._receiver_bytes = 0
            self._session_started_at = None
            self._session_id = self._new_session_id()
            self._touch_locked()

    def _append_break_if_needed_locked(self, now_ts: float) -> None:
        cfg = self._cfg()
        soft = max(0.1, float(cfg.get('transcription_pause_soft_seconds', 1.0) or 1.0))
        hard = max(soft, float(cfg.get('transcription_pause_hard_seconds', 2.5) or 2.5))
        if self._last_segment_end_ts is None:
            return
        gap = now_ts - self._last_segment_end_ts
        if gap < soft:
            return
        self._segments.append({
            'id': str(uuid.uuid4()),
            'type': 'break',
            'level': 'hard' if gap >= hard else 'soft',
            'gap_seconds': round(gap, 2),
            'created_at': now_ts,
        })

    def _append_final_text(self, text: str) -> None:
        clean = str(text or '').strip()
        if not clean:
            return
        now_ts = time.time()
        with self._lock:
            if self._session_started_at is None:
                self._session_started_at = now_ts
            self._append_break_if_needed_locked(now_ts)
            self._segments.append({
                'id': str(uuid.uuid4()),
                'type': 'speech',
                'text': clean,
                'created_at': now_ts,
                'sender': dict(self._active_sender) if self._active_sender else None,
            })
            self._last_segment_end_ts = now_ts
            self._live_text = ''
            self._stabilized_text = ''
            lower_text = clean.casefold()
            for rule in self.load_keyword_rules():
                if not bool(rule.get('enabled', True)):
                    continue
                keyword = str(rule.get('keyword') or '').strip()
                if not keyword:
                    continue
                if keyword.casefold() in lower_text:
                    self._dispatch_action_locked(
                        str(rule.get('action_id') or ''),
                        keyword=keyword,
                        segment_text=clean,
                        rule_id=str(rule.get('id') or ''),
                    )
            self._touch_locked()
            self._save_history_locked()

    def _finalizer_loop(self) -> None:
        while not self._stop_event.is_set():
            recorder = self._recorder
            if recorder is None:
                return
            try:
                recorder.text(self._append_final_text)
            except Exception as e:
                with self._lock:
                    if self._stop_event.is_set():
                        return
                    self._service_error = str(e)
                    self._recorder_ready = False
                    self._touch_locked()
                self._log(f'[TRANSCRIPTION] Finalizer loop error: {e}')
                time.sleep(0.5)

    def _on_recording_start(self) -> None:
        with self._lock:
            if self._session_started_at is None:
                self._session_started_at = time.time()
            self._touch_locked()

    def _on_recording_stop(self) -> None:
        with self._lock:
            self._touch_locked()

    def _on_realtime_update(self, text: str) -> None:
        with self._lock:
            self._live_text = str(text or '').strip()
            self._touch_locked()

    def _on_realtime_stabilized(self, text: str) -> None:
        with self._lock:
            self._stabilized_text = str(text or '').strip()
            self._touch_locked()

    def _normalize_audio(self, chunk: bytes, sample_rate: int, channels: int, sample_width: int) -> bytes:
        if sample_width != 2:
            raise ValueError('Only 16-bit PCM audio is supported by this endpoint.')
        if not chunk:
            return b''
        if _np is None:
            if sample_rate != self.TARGET_SAMPLE_RATE or channels != self.TARGET_CHANNELS:
                raise ValueError('numpy is required for server-side resampling/mixdown.')
            return chunk

        data = _np.frombuffer(chunk, dtype=_np.int16)
        if channels > 1:
            usable = (len(data) // channels) * channels
            if usable <= 0:
                return b''
            data = data[:usable].reshape((-1, channels)).mean(axis=1).astype(_np.int16)
        elif channels < 1:
            raise ValueError('channels must be >= 1')

        if sample_rate != self.TARGET_SAMPLE_RATE:
            if len(data) <= 1:
                return b''
            src_idx = _np.arange(len(data), dtype=_np.float32)
            dst_len = max(1, int(round(len(data) * (self.TARGET_SAMPLE_RATE / float(sample_rate)))))
            dst_idx = _np.linspace(0, len(data) - 1, dst_len, dtype=_np.float32)
            data = _np.interp(dst_idx, src_idx, data.astype(_np.float32)).astype(_np.int16)

        return data.tobytes()

    def ingest_audio(
        self,
        chunk: bytes,
        *,
        sample_rate: int,
        channels: int,
        sample_width: int,
        source_id: str,
        source_name: str,
        remote_addr: str,
    ) -> tuple[bool, str | None]:
        cfg = self._cfg()
        if not bool(cfg.get('transcription_enabled', False)):
            return False, 'Transcription is disabled.'
        if not bool(cfg.get('transcription_remote_enabled', True)):
            return False, 'Remote audio ingest is disabled.'

        ok, err = self.ensure_recorder()
        if not ok:
            return False, err or 'Recorder unavailable.'

        now_ts = time.time()
        timeout_s = self._sender_timeout_seconds(cfg)

        with self._lock:
            active = dict(self._active_sender) if self._active_sender else None
            active_last = float(active.get('last_seen_at', 0.0)) if active else 0.0
            if active:
                same_sender = (
                    str(active.get('id') or '') == str(source_id or '')
                    or (
                        str(active.get('name') or '') == str(source_name or '')
                        and str(active.get('remote_addr') or '') == str(remote_addr or '')
                    )
                )
                if not same_sender and (now_ts - active_last) < timeout_s:
                    return False, 'Another sender is currently active.'

        try:
            pcm = self._normalize_audio(chunk, sample_rate, channels, sample_width)
        except Exception as e:
            return False, str(e)

        if not pcm:
            return True, None

        recorder = self._recorder
        if recorder is None:
            return False, 'Recorder is not ready.'

        try:
            recorder.feed_audio(pcm)
        except Exception as e:
            with self._lock:
                self._service_error = str(e)
                self._touch_locked()
            return False, str(e)

        with self._lock:
            if self._session_started_at is None:
                self._session_started_at = now_ts
            self._active_sender = {
                'id': str(source_id or '').strip() or str(uuid.uuid4()),
                'name': str(source_name or '').strip() or 'Remote Sender',
                'remote_addr': str(remote_addr or '').strip(),
                'last_seen_at': now_ts,
            }
            self._last_audio_ts = now_ts
            self._receiver_bytes += len(pcm)
            self._touch_locked()
        return True, None

    def control(self, action: str) -> tuple[bool, str]:
        act = str(action or '').strip().lower()
        if act == 'start':
            ok, err = self.ensure_recorder()
            return ok, '' if ok else (err or 'Unable to start recorder.')
        if act == 'stop':
            self.stop_session()
            return True, ''
        if act == 'clear':
            self.clear_session()
            return True, ''
        return False, 'Unsupported action.'

    def stream_wait(self, after_version: int | None, timeout: float = 15.0) -> tuple[int, dict[str, Any]]:
        target = 0 if after_version is None else int(after_version)
        with self._lock:
            if self._version <= target:
                self._update_cond.wait(timeout=timeout)
            return self._version, self._state_locked(include_history=False)

    def get_state(self, *, include_history: bool = True) -> dict[str, Any]:
        with self._lock:
            return self._state_locked(include_history=include_history)

    def _state_locked(self, *, include_history: bool) -> dict[str, Any]:
        cfg = self._cfg()
        now_ts = time.time()
        sender_timeout = self._sender_timeout_seconds(cfg)
        connected = self._last_audio_ts is not None and ((now_ts - self._last_audio_ts) < sender_timeout)
        paused = self._last_audio_ts is not None and ((now_ts - self._last_audio_ts) >= float(cfg.get('transcription_pause_soft_seconds', 1.0) or 1.0))
        server_state = 'off'
        if bool(cfg.get('transcription_enabled', False)):
            if AudioToTextRecorder is None:
                server_state = 'missing_dependency'
            elif self._service_error:
                server_state = 'error'
            elif self._recorder_ready:
                server_state = 'ready'
            else:
                server_state = 'idle'

        client_state = 'off' if not bool(cfg.get('transcription_enabled', False)) else 'waiting'
        if bool(cfg.get('transcription_enabled', False)):
            if connected and not paused:
                client_state = 'streaming'
            elif connected:
                client_state = 'paused'
            elif self._active_sender:
                client_state = 'disconnected'

        state = {
            'ok': True,
            'version': self._version,
            'status': server_state,
            'server_state': server_state,
            'client_state': client_state,
            'enabled': bool(cfg.get('transcription_enabled', False)),
            'realtime_supported': AudioToTextRecorder is not None,
            'error': self._service_error,
            'session': {
                'id': self._session_id,
                'started_at': self._session_started_at,
                'receiver_bytes': self._receiver_bytes,
            },
            'source': dict(self._active_sender) if self._active_sender else None,
            'live_text': self._live_text,
            'stabilized_text': self._stabilized_text,
            'segments': list(self._segments),
            'action_events': list(self._recent_action_events),
            'display': {
                'font_scale': float(cfg.get('transcription_font_scale', 1.0) or 1.0),
                'line_spacing': float(cfg.get('transcription_line_spacing', 1.25) or 1.25),
                'show_timestamps': bool(cfg.get('transcription_show_timestamps', True)),
                'show_live_line': bool(cfg.get('transcription_show_live_line', True)),
                'compact_mode': bool(cfg.get('transcription_segment_compact_mode', False)),
                'colors': {
                    'live_bg': str(cfg.get('transcription_color_live_bg', '#121a2c')).strip() or '#121a2c',
                    'live_text': str(cfg.get('transcription_color_live_text', '#f8fafc')).strip() or '#f8fafc',
                    'segment_bg': str(cfg.get('transcription_color_segment_bg', '#182235')).strip() or '#182235',
                    'segment_text': str(cfg.get('transcription_color_segment_text', '#e5e7eb')).strip() or '#e5e7eb',
                    'break_soft_bg': str(cfg.get('transcription_color_break_soft_bg', '#17354a')).strip() or '#17354a',
                    'break_soft_text': str(cfg.get('transcription_color_break_soft_text', '#7dd3fc')).strip() or '#7dd3fc',
                    'break_hard_bg': str(cfg.get('transcription_color_break_hard_bg', '#4f46e5')).strip() or '#4f46e5',
                    'break_hard_text': str(cfg.get('transcription_color_break_hard_text', '#ffffff')).strip() or '#ffffff',
                },
            },
            'settings': {
                'keep_history': bool(cfg.get('transcription_keep_history', False)),
                'source_name': str(cfg.get('transcription_source_name', 'Church Comms')).strip() or 'Church Comms',
            },
            'ts': now_ts,
        }
        if include_history:
            state['history'] = list(self._sessions)[-self._history_limit(cfg):]
        return state
