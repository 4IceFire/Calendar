"""pyvidaa adapter separating fresh receipts from queried broker snapshots."""
import json
import threading
import time
from typing import Any

from pyvidaa import VidaaTV
from pyvidaa.topics import (
    TOPIC_GET_STATE, TOPIC_STATE_RESPONSE, TOPIC_GET_VOLUME, TOPIC_GET_SOURCES,
    TOPIC_GET_DEVICE_INFO, TOPIC_GET_TV_INFO, TOPIC_SOURCES_RESPONSE,
    TOPIC_DEVICE_INFO_RESPONSE, TOPIC_TV_INFO_RESPONSE, get_topic,
)
from tv_observation import main_volume

_READ_REPLIES = (
    (TOPIC_GET_SOURCES, 'sources', TOPIC_SOURCES_RESPONSE, list),
    (TOPIC_GET_DEVICE_INFO, 'device_info', TOPIC_DEVICE_INFO_RESPONSE, dict),
    (TOPIC_GET_TV_INFO, 'tv_info', TOPIC_TV_INFO_RESPONSE, dict),
)
_DIRECT_STATE = '/remoteapp/mobile/{client}/ui_service/data/state'


class ObservedVidaaTV(VidaaTV):
    strict_observations = True
    wake_uses_wol = True  # Raw ON toggles are forbidden; use wake_from_standby.
    standby_settle_seconds = 2.0

    def __init__(self, *args, **kwargs):
        self._receipt = kwargs.pop('on_state_change', None)
        self._transport_lock = threading.RLock()
        self._transport_epoch = 0
        self._receipt_serial = 0
        self._receipts: dict[str, Any] = {key: None for key in ('state', 'volume', 'sources', 'device_info', 'tv_info')}
        self._receipt_events = {key: threading.Event() for key in self._receipts}
        self._read_starts = {key: 0 for key in self._receipts}
        self._snapshot = None
        self._snapshot_start = 0
        self._snapshot_serial = 0
        self._snapshot_event = threading.Event()
        self._standby_since = None
        self._wake_serial = 0
        self._power_arrival_serial = 0
        self._auth_inflight = 0
        self._wake_changed = threading.Event()
        self._standby_start = 0
        self._standby_event = threading.Event()
        super().__init__(*args, on_state_change=None, **kwargs)

    def _invalidate_transport(self):
        # A reconnect on the SAME Paho object is still a new transport epoch.
        self._transport_epoch += 1
        self._standby_since = None
        self._wake_changed.set()
        self._snapshot = None
        self._snapshot_event.set()
        self._standby_event.set()
        for kind, event in self._receipt_events.items():
            self._receipts[kind] = None
            event.set()

    def _on_connect(self, client, userdata, flags, rc):
        with self._transport_lock:
            if client is not self._client:
                return
            self._invalidate_transport()
            self._connected = False
            super()._on_connect(client, userdata, flags, rc)
            if rc == 0:
                self._client.subscribe(get_topic(_DIRECT_STATE, self.client_id))

    def _on_disconnect(self, client, userdata, rc):
        with self._transport_lock:
            if client is not self._client:
                return
            self._invalidate_transport()
            super()._on_disconnect(client, userdata, rc)

    def _on_message(self, client, userdata, msg):
        with self._transport_lock:
            if client is not self._client or not self._connected:
                return
            epoch = self._transport_epoch
            self._receipt_serial += 1
            serial = self._receipt_serial
            received_at = time.monotonic()
            # Match the inherited authentication parser, including the real
            # client-specific authenticationcode topic (not a state reply).
            auth_receipt = 'authentication' in msg.topic and 'tokenissuance' not in msg.topic
            power_receipt = (
                msg.topic in (TOPIC_STATE_RESPONSE, get_topic(_DIRECT_STATE, self.client_id))
                or ('broadcast' in msg.topic and '/volume' not in msg.topic
                    and 'volumechange' not in msg.topic))
            if power_receipt or auth_receipt:
                self._power_arrival_serial = serial
            if auth_receipt:
                self._auth_inflight += 1
                self._snapshot = None
                self._snapshot_event.set()
                self._standby_since = None
                self._wake_changed.set()
        # Preserve the inherited auth/token/refresh parser. Its generic response
        # cache is deliberately never used for these strict reads.
        try:
            super()._on_message(client, userdata, msg)
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeError):
            return
        finally:
            if auth_receipt:
                with self._transport_lock:
                    self._auth_inflight -= 1
        with self._transport_lock:
            if epoch != self._transport_epoch or client is not self._client or not self._connected:
                return
            for _, kind, response, expected_type in _READ_REPLIES:
                if msg.topic == get_topic(response, self.client_id) and isinstance(payload, expected_type):
                    if not getattr(msg, 'retain', False) and serial > self._read_starts[kind]:
                        self._receipts[kind] = payload
                        self._receipt_events[kind].set()
                    return
            if not isinstance(payload, dict):
                return
            state_type = payload.get('statetype')
            if (power_receipt and serial > max(self._snapshot_start, self._wake_serial)
                    and any(key in payload for key in ('statetype', 'sourceid', 'sourcename', 'error'))):
                self._wake_serial = serial
                if state_type == 'fake_sleep_0' and not payload.get('error'):
                    if self._standby_since is None:
                        self._standby_since = received_at
                else:
                    self._standby_since = None
                    self._wake_changed.set()
            if (msg.topic in (TOPIC_STATE_RESPONSE, get_topic(_DIRECT_STATE, self.client_id))
                    and ('statetype' in payload or payload.get('error')
                         or 'sourceid' in payload or 'sourcename' in payload)):
                if serial <= max(self._snapshot_start, self._snapshot_serial):
                    return
                self._snapshot_serial = serial
                if not state_type:
                    self._snapshot = None
                    self._snapshot_event.set()
            if payload.get('error') or (state_type is not None and (
                    not isinstance(state_type, str) or state_type.lower().startswith('auth')
                    or state_type.lower() in ('error', 'unknown'))):
                if (msg.topic in (TOPIC_STATE_RESPONSE, get_topic(_DIRECT_STATE, self.client_id))
                        and serial > self._snapshot_start):
                    self._snapshot = None
                    self._snapshot_event.set()
                return
            kind = None
            if '/volume' in msg.topic or 'volumechange' in msg.topic:
                kind = 'volume'
            elif ('broadcast' in msg.topic or msg.topic == get_topic(_DIRECT_STATE, self.client_id)) and ('statetype' in payload or 'sourceid' in payload or 'sourcename' in payload):
                kind = 'state'
            if not kind:
                return
            retained = bool(getattr(msg, 'retain', False))
            if (kind == 'state' and state_type and serial > self._snapshot_start
                    and msg.topic in (TOPIC_STATE_RESPONSE, get_topic(_DIRECT_STATE, self.client_id))):
                self._snapshot = dict(payload, _tdeck_retained=retained)
                self._snapshot_event.set()
            if self._receipt:
                self._receipt(dict(payload, _tdeck_volume=(kind == 'volume'), _tdeck_retained=retained))
            if retained:
                return
            if kind == 'state' and state_type == 'fake_sleep_0' and serial > self._standby_start:
                self._standby_event.set()
            # Mute/alternate-audio observations do not complete main-volume reads.
            if kind == 'volume' and main_volume(payload) is None:
                if payload.get('volume_type', 0) != 2:
                    # A new non-main/invalid level revokes a captured numeric
                    # precondition; mute-only feedback leaves it unchanged.
                    self._receipts['volume'] = None
                return
            if serial > self._read_starts[kind]:
                self._receipts[kind] = payload
                self._receipt_events[kind].set()

    def _request(self, topic, payload='', timeout=5.0):
        for request_topic, kind, _, _ in _READ_REPLIES:
            if topic == get_topic(request_topic, self.client_id):
                return self._fresh(kind, request_topic, timeout, payload)
        return super()._request(topic, payload, timeout)

    def _fresh(self, kind, topic, timeout, payload=''):
        with self._transport_lock:
            if not self._connected:
                return None
            epoch = self._transport_epoch
            event = self._receipt_events[kind] = threading.Event()
            self._read_starts[kind] = self._receipt_serial
            self._receipts[kind] = None
            if not self._publish(get_topic(topic, self.client_id), payload):
                return None
        if not event.wait(max(0, timeout)):
            return None
        with self._transport_lock:
            return self._receipts[kind] if epoch == self._transport_epoch and self._connected else None

    def get_power_state(self, timeout=2.0):
        """Query a broker snapshot, not necessarily fresh physical telemetry."""
        with self._transport_lock:
            if not self._connected:
                return None
            epoch = self._transport_epoch
            event = self._snapshot_event = threading.Event()
            self._snapshot = None
            self._snapshot_start = self._receipt_serial
            try:
                if not self._publish(get_topic(TOPIC_GET_STATE, self.client_id), ''):
                    return None
                result, _ = self._client.subscribe(TOPIC_STATE_RESPONSE)
                if result != 0:
                    return None
            except (OSError, RuntimeError, ValueError):
                return None
        if not event.wait(max(0, timeout)):
            return None
        with self._transport_lock:
            return self._snapshot if epoch == self._transport_epoch and self._connected else None

    def wake_from_standby(self, *, cancelled=lambda: False):
        """Guard one ON toggle; snapshots authorize control, not confirmation.

        Outcomes are explicit: sent, attempted_failed, already_on, unknown.
        Receipt-time dwell avoids toggling straight back after a recent OFF.
        No transport/controller lock is held while settling or querying.
        """
        epoch = self._transport_epoch
        state = self.get_power_state()
        with self._transport_lock:
            if (epoch != self._transport_epoch or not self._connected or not state
                    or self._auth_inflight or self.needs_authentication()):
                return 'unknown'
            state = self._snapshot
            if not state or self._snapshot_serial <= self._snapshot_start:
                return 'unknown'
            if state.get('statetype') != 'fake_sleep_0':
                return 'already_on'
            since = self._standby_since
            if since is None:
                return 'unknown'
            remaining = max(0, self.standby_settle_seconds - (time.monotonic() - since))
            self._wake_changed.clear()
        if cancelled():
            return 'unknown'
        if remaining:
            if self._wake_changed.wait(remaining) or cancelled():
                return 'unknown'
            state = self.get_power_state()
        with self._transport_lock:
            state = self._snapshot if state else None
            if (cancelled() or epoch != self._transport_epoch or not self._connected
                    or self._auth_inflight or self.needs_authentication()
                    or not state or self._snapshot_serial <= self._snapshot_start
                    or self._wake_serial < self._power_arrival_serial
                    or state.get('statetype') != 'fake_sleep_0'
                    or self._standby_since != since
                    or time.monotonic() - since < self.standby_settle_seconds):
                return 'unknown'
            try:
                return 'sent' if self.send_key('KEY_POWER') else 'attempted_failed'
            except Exception:
                # A failed publish may still have reached the broker. Caller
                # must consume this attempt rather than replaying a toggle.
                return 'attempted_failed'

    def power_off(self):
        """Guard one OFF toggle with a queried snapshot; acceptance is not confirmation."""
        epoch = self._transport_epoch
        state = self.get_power_state()
        with self._transport_lock:
            # The getter result can already be superseded by a live receipt.
            # Revalidate the current query snapshot while holding the publish lock.
            state = self._snapshot if state else None
            state_type = state.get('statetype') if state else None
            if (epoch != self._transport_epoch or not self._connected
                    or self._auth_inflight or self.needs_authentication()
                    or self._snapshot_serial <= self._snapshot_start
                    or self._wake_serial < self._power_arrival_serial
                    or not isinstance(state_type, str) or not state_type
                    or state_type.lower().startswith('auth')
                    or state_type.lower() in ('error', 'unknown') or state.get('error')):
                return False
            if state_type == 'fake_sleep_0':
                return True
            self._standby_event.clear()
            self._standby_start = self._receipt_serial
            try:
                if not self.send_key('KEY_POWER'):
                    return False
            except (OSError, RuntimeError, ValueError):
                return False
        self._standby_event.wait(1.5)
        return True

    def get_state(self, timeout=2.0):
        state = self._fresh('state', TOPIC_GET_STATE, timeout)
        if not state or not state.get('statetype'):
            return None
        return state

    def volume_up(self):
        return self.adjust_volume(1)

    def volume_down(self):
        return self.adjust_volume(-1)

    def adjust_volume(self, step, *, before_write=lambda baseline, target: None, timeout=2.0):
        """Use fresh main-speaker feedback; accepted volume keys can be ignored."""
        epoch = self._transport_epoch
        level = self.get_volume(timeout=timeout)
        with self._transport_lock:
            if (epoch != self._transport_epoch or not self._connected
                    or self._auth_inflight or self.needs_authentication()):
                return False
            if level is None:
                # Preserve existing external/unknown-volume key passthrough,
                # without inventing a numeric target or a confirmation baseline.
                before_write(None, None)
                return self.send_key('KEY_VOLUMEUP' if step > 0 else 'KEY_VOLUMEDOWN')
            if main_volume(self._receipts['volume'] or {}) != level:
                return False
            target = max(0, min(100, level + step))
            before_write(level, target)
            return self.set_volume(target)

    def get_volume(self, timeout=2.0):
        payload = self._fresh('volume', TOPIC_GET_VOLUME, timeout)
        if not payload or payload.get('volume_type', 0) != 0:
            return None
        return main_volume(payload)
