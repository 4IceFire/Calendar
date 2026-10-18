"""pyvidaa 2.1 adapter: only MQTT receipts constitute fresh observations.

The upstream getters return caches on timeout. Volume callbacks are not passed
through on_state_change upstream, so intercept receipt without changing parsing,
authentication or subscriptions. No alternate-volume numeric scale is assumed.
"""
import json
import threading
from pyvidaa import VidaaTV
from pyvidaa.topics import TOPIC_GET_STATE, TOPIC_GET_VOLUME, get_topic
from tv_observation import main_volume
from typing import Any


class ObservedVidaaTV(VidaaTV):
    strict_observations = True

    def __init__(self, *args, **kwargs):
        self._receipt = kwargs.pop('on_state_change', None)
        self._receipts: dict[str, Any] = {'state': None, 'volume': None}
        self._receipt_events = {key: threading.Event() for key in self._receipts}
        super().__init__(*args, on_state_change=None, **kwargs)

    def _on_message(self, client, userdata, msg):
        super()._on_message(client, userdata, msg)
        try:
            payload = json.loads(msg.payload.decode())
        except (ValueError, UnicodeError):
            return
        if not isinstance(payload, dict):
            return
        kind = None
        if '/volume' in msg.topic or 'volumechange' in msg.topic:
            kind = 'volume'
        elif 'broadcast' in msg.topic and ('statetype' in payload or 'sourceid' in payload or 'sourcename' in payload):
            kind = 'state'
        if kind:
            self._receipts[kind] = payload
            if self._receipt:
                self._receipt(dict(payload, _tdeck_volume=(kind == 'volume')))
            self._receipt_events[kind].set()

    def _fresh(self, kind, topic, timeout):
        event = self._receipt_events[kind]
        event.clear()
        if not self._publish(get_topic(topic, self.client_id), ''):
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
