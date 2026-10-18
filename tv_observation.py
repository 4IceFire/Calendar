"""Device observation provenance. Never accept command values as telemetry."""
import copy
import threading
import time
from typing import Any


def main_volume(payload):
    if payload.get('volume_type', 0) != 0:
        return None
    value = next((payload[key] for key in ('volume_value', 'volume', 'value') if key in payload), None)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100 else None


class Observation:
    def __init__(self):
        self.lock = threading.RLock()
        self.fields: dict[str, dict[str, Any]] = {key: {'value': None, 'sampledAt': None} for key in ('power', 'source', 'volume', 'muted')}
        self.command = None
        self.failures = []
        self.audio = 'unknown'

    def report(self, field, value, *, at=None):
        at = time.time() if at is None else at
        with self.lock:
            previous = self.fields[field]['sampledAt']
            if previous is not None and at < previous:
                return
            self.fields[field] = {'value': value, 'sampledAt': at}
            cmd = self.command
            if cmd and cmd['status'] == 'pending' and cmd['field'] == field and at > cmd['requestedAt']:
                cmd['observedAt'] = at
                cmd['matches'] = value == cmd['requested']
                if cmd['matches'] and value is not None:
                    cmd['status'] = 'confirmed'

    def volume(self, payload, *, at=None):
        at = time.time() if at is None else at
        kind = payload.get('volume_type', 0)
        value = next((payload[key] for key in ('volume_value', 'volume', 'value') if key in payload), None)
        if kind == 2:
            if value in (0, 1):
                self.report('muted', value == 1, at=at)
            return
        with self.lock:
            previous = self.fields['volume']['sampledAt']
            if previous is not None and at < previous:
                return
            if kind != 0:
                self.audio = 'external-unverified'
                self.report('volume', None, at=at)
            elif main_volume(payload) is not None:
                self.audio = 'tv-speaker'
                self.report('volume', main_volume(payload), at=at)

    def request(self, operation, value, *, at=None):
        field, desired = {'power_on': ('power', 'on'), 'power_off': ('power', 'off'),
                          'volume_set': ('volume', value), 'source': ('source', value)}.get(operation, (None, None))
        with self.lock:
            self.command = {'operation': operation, 'field': field, 'requested': desired,
                            'requestedAt': time.time() if at is None else at, 'status': 'pending'}

    def failure(self, operation, *, at=None):
        with self.lock:
            if operation == 'status_read':
                self.failures = [entry for entry in self.failures if entry['operation'] != 'status_read']
            self.failures.insert(0, {'operation': operation, 'time': time.time() if at is None else at,
                                    'summary': 'TV operation failed. Check connection or setup; execution is not confirmed.'})
            del self.failures[10:]
            if self.command and operation == self.command['operation']:
                self.command['status'] = 'failed'

    def snapshot(self, *, now=None):
        now = time.time() if now is None else now
        with self.lock:
            fields = copy.deepcopy(self.fields)
            for field in fields.values():
                stamp = field['sampledAt']
                field['ageMs'] = None if stamp is None else max(0, round((now - stamp) * 1000))
                field['stale'] = stamp is None or now - stamp > 15
            command = copy.deepcopy(self.command)
            if command and command['status'] == 'pending' and now - command['requestedAt'] > 15:
                command['status'] = 'mismatch' if command.get('observedAt') else 'unconfirmed'
            return {'fields': fields, 'command': command, 'failures': copy.deepcopy(self.failures), 'audio': self.audio}
