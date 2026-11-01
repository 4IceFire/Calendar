"""pyvidaa 2.1 adapter: only MQTT receipts constitute fresh observations.

The upstream getters return caches on timeout. Volume callbacks are not passed
through on_state_change upstream, so intercept receipt without changing parsing,
authentication or subscriptions. No alternate-volume numeric scale is assumed.
"""
import json
import threading
from pyvidaa import VidaaTV
from pyvidaa.topics import (
    TOPIC_GET_STATE, TOPIC_GET_VOLUME, TOPIC_GET_SOURCES,
    TOPIC_GET_DEVICE_INFO, TOPIC_GET_TV_INFO, TOPIC_SOURCES_RESPONSE,
    TOPIC_DEVICE_INFO_RESPONSE, TOPIC_TV_INFO_RESPONSE, get_topic,
)

_READ_REPLIES = (
    (TOPIC_GET_SOURCES, 'sources', TOPIC_SOURCES_RESPONSE, list),
    (TOPIC_GET_DEVICE_INFO, 'device_info', TOPIC_DEVICE_INFO_RESPONSE, dict),
    (TOPIC_GET_TV_INFO, 'tv_info', TOPIC_TV_INFO_RESPONSE, dict),
)
from tv_observation import main_volume
from typing import Any


class ObservedVidaaTV(VidaaTV):
    strict_observations = True

    def __init__(self, *args, **kwargs):
        self._receipt = kwargs.pop('on_state_change', None)
        self._receipts: dict[str, Any] = {key: None for key in ('state', 'volume', 'sources', 'device_info', 'tv_info')}
        self._receipt_events = {key: threading.Event() for key in self._receipts}
        super().__init__(*args, on_state_change=None, **kwargs)

    def _on_message(self, client, userdata, msg):
        super()._on_message(client, userdata, msg)
        try:
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeError):
            return
        for _, kind, response, expected_type in _READ_REPLIES:
            if msg.topic == get_topic(response, self.client_id) and isinstance(payload, expected_type):
                self._receipts[kind] = payload
                self._receipt_events[kind].set()
                return
        if not isinstance(payload, dict):
            return
        state_type = payload.get('statetype')
        if state_type is not None and (not isinstance(state_type, str) or state_type.lower().startswith('auth') or state_type.lower() == 'error'):
            return
        kind = None
        if '/volume' in msg.topic or 'volumechange' in msg.topic:
            kind = 'volume'
        elif 'broadcast' in msg.topic and ('statetype' in payload or 'sourceid' in payload or 'sourcename' in payload):
            kind = 'state'
        if kind:
            if self._receipt:
                self._receipt(dict(payload, _tdeck_volume=(kind == 'volume')))
            # Mute/alternate-audio broadcasts are useful observations, but are
            # not the main-speaker reply the volume getter is waiting for.
            if kind == 'volume' and main_volume(payload) is None:
                return
            self._receipts[kind] = payload
            self._receipt_events[kind].set()

    def _request(self, topic, payload='', timeout=5.0):
        for request_topic, kind, _, _ in _READ_REPLIES:
            if topic == get_topic(request_topic, self.client_id):
                return self._fresh(kind, request_topic, timeout, payload)
        return super()._request(topic, payload, timeout)

    def _fresh(self, kind, topic, timeout, payload=''):
        event = self._receipt_events[kind]
        event.clear()
        if not self._publish(get_topic(topic, self.client_id), payload):
            return None
        return self._receipts[kind] if event.wait(timeout) else None

    def get_state(self, timeout=2.0):
        state = self._fresh('state', TOPIC_GET_STATE, timeout)
        if not state or not state.get('statetype') or state['statetype'] in ('authentication', 'error'):
            return None
        return state

    def get_volume(self, timeout=2.0):
        payload = self._fresh('volume', TOPIC_GET_VOLUME, timeout)
        if not payload or payload.get('volume_type', 0) != 0:
            return None
        return main_volume(payload)
