"""A&H dLive MIDI/TCP provider for the existing Personal Mixes contract.

Wire reference: dLive MIDI Over TCP/IP Protocol V2.0 pp1-2 and reference
chart (also V1.9). No AH-Net, audio, scene, preamp or whole-input controls.
Only received reports are observations; socket writes never populate values.
"""
from __future__ import annotations

import math
import socket
import threading
import time
from collections import deque
from typing import Any

HEADER = bytes.fromhex('f0 00 00 1a 50 10 01 00')
CAPABILITIES = dict(level=True, pan=False, sendOn=False, inputMeters=False,
                    names=True, masterLevel=True, masterMute=True, me1=False)
UNSUPPORTED = ('Per-send mute, send pan, input meters and ME-1 remote control are '
               'unavailable in this provider. Whole-input mutes are never substituted.')


def encode_level(db):
    if isinstance(db, bool):
        raise ValueError('Level must be a number, not boolean')
    value = float(db)
    if not math.isfinite(value) or not -150 <= value <= 10:
        raise ValueError('Level must be finite and between -150 and +10 dB')
    # Correct only floating-point roundoff at an exact MIDI step boundary.
    return max(0, min(127, int((value + 54) * 127 / 64 + 1e-9)))


def decode_level(value):
    return -150.0 if value == 0 else value * 64 / 127 - 54


class MidiStream:
    """Bounded MIDI framing over arbitrary TCP chunks, including running status."""
    def __init__(self):
        self.status = None
        self.data = bytearray()
        self.sysex = None

    def feed(self, raw):
        messages = []
        for byte in raw:
            if byte >= 0xf8:  # real-time bytes do not cancel running status
                continue
            if byte == 0xf0:
                self.status = None
                self.data.clear()
                self.sysex = bytearray([byte])
            elif self.sysex is not None:
                if byte == 0xf7:
                    self.sysex.append(byte)
                    messages.append(bytes(self.sysex))
                    self.sysex = None
                elif byte >= 0x80 or len(self.sysex) >= 512:
                    self.sysex = None
                else:
                    self.sysex.append(byte)
            elif byte >= 0x80:
                self.status = byte if byte < 0xf0 else None
                self.data.clear()
            elif self.status is not None:
                self.data.append(byte)
                size = 1 if self.status & 0xf0 in (0xc0, 0xd0) else 2
                if len(self.data) == size:
                    messages.append(bytes([self.status]) + self.data)
                    self.data.clear()
        return messages


class DLiveMixerClient:
    def __init__(self, config):
        self.config = dict(config)
        self.base = int(config.get('dlive_midi_channel', 1)) - 1
        self.auxes = [dict(x) for x in config.get('dlive_auxes', [])]
        self.channels = [dict(x) for x in config.get('dlive_channels', [])]
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.sock = None
        self.revision = 1
        self.observed = {}
        self.master = {}
        self.nrpn = {}
        self.names = {}
        self.pending = deque()
        self.pending_set = set()
        self.last_request = {}
        self.demand_order = deque()
        self.read_cursor = {}
        self.read_due = {}
        self.read_queries = {}
        # Interval is between bounded TCP read batches, not individual fields.
        # Reserve one slot for discovery/confirmation so it cannot starve.
        self.read_batch_limit = 16
        self.demand_ttl = 65.0  # includes the UI's 30-second hidden polling
        self.last_error = ''
        self.last_received = 0
        self._expired = set()
        self.retry = float(config.get('dlive_retry_interval', 1))
        self.spacing = float(config.get('dlive_request_interval', .025))
        self.stale_after = float(config.get('dlive_stale_after', 10))

    def start(self):
        with self.lock:
            if not self.config.get('dlive_enabled') or not self.config.get('dlive_ip'):
                return
            if self.thread and self.thread.is_alive():
                return
            if self.stop.is_set():
                return
            self.thread = threading.Thread(target=self._run, name='dlive-midi', daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        with self.lock:
            sock = self.sock
            self.sock = None
            self.revision += 1
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(3)

    def _aux(self, number):
        return next((a for a in self.auxes if a['channel'] == number), None)

    def _destination(self, aux):
        return self.base + 2, (0x40 if aux.get('stereo') else 0) + aux['bus'] - 1

    def _query(self, body):
        message = HEADER + bytes(body) + b'\xf7'
        with self.lock:
            if message not in self.pending_set:
                self.pending.append(message)
                self.pending_set.add(message)

    def request_aux_state(self, number):
        with self.lock:
            aux = self._aux(number)
            if not aux or not aux.get('enabled', True):
                raise KeyError('AUX not found')
            now = time.monotonic()
            self.last_request[number] = now
            if number not in self.read_queries:
                dest, note = self._destination(aux)
                bodies = [[dest, 5, 11, 23, note], [dest, 5, 9, note]]
                bodies.extend([self.base, 5, 15, 13, c['channel'] - 1, dest, note]
                              for c in self.channels if c.get('enabled', True))
                self.read_queries[number] = tuple(HEADER + bytes(body) + b'\xf7' for body in bodies)
            if number not in self.demand_order:
                self.demand_order.append(number)

    def _read_batch(self, now):
        """Round-robin fields across shared AUX demand, never FIFO snapshots.

        Called by the sole transport worker under lock. Browser frequency only
        renews an AUX lease; it never adds queries or resets another AUX's turn.
        A full sweep rests briefly; hidden clients keep getting background reads.
        """
        for number in list(self.demand_order):
            if now - self.last_request.get(number, -self.demand_ttl) > self.demand_ttl:
                self.demand_order.remove(number)
                self.read_cursor.pop(number, None)
                self.read_due.pop(number, None)
        batch = []
        misses = 0
        while self.demand_order and len(batch) < self.read_batch_limit - 1:
            number = self.demand_order[0]
            self.demand_order.rotate(-1)
            if now < self.read_due.get(number, 0):
                misses += 1
                if misses >= len(self.demand_order):
                    break
                continue
            misses = 0
            queries = self.read_queries[number]
            cursor = self.read_cursor.get(number, 0)
            batch.append(queries[cursor])
            cursor = (cursor + 1) % len(queries)
            self.read_cursor[number] = cursor
            if cursor == 0:
                self.read_due[number] = now + min(1.0, self.stale_after / 3)
        # At least one background slot per batch; use idle slots for names.
        while self.pending and len(batch) < self.read_batch_limit:
            message = self.pending.popleft()
            self.pending_set.discard(message)
            batch.append(message)
        return batch

    def _run(self):
        while not self.stop.is_set():
            sock = None
            try:
                sock = socket.create_connection((self.config['dlive_ip'], int(self.config.get('dlive_port', 51325))), timeout=1)
                sock.settimeout(.025)
                with self.lock:
                    if self.stop.is_set():
                        return
                    self.sock = sock
                    self.last_received = 0
                    self.observed.clear()
                    self.master.clear()
                    self.nrpn.clear()
                    self._expired.clear()
                    self.read_cursor.clear()
                    self.read_due.clear()
                    self.pending.clear()
                    self.pending_set.clear()
                    self.revision += 1
                    for channel in self.channels:
                        self._query([self.base, 1, channel['channel'] - 1])
                    for aux in self.auxes:
                        self._query([*self._destination(aux)[:1], 1, self._destination(aux)[1]])
                stream = MidiStream()
                last_send = 0
                last_rx = time.monotonic()
                last_heartbeat = last_rx
                while not self.stop.is_set():
                    now = time.monotonic()
                    if self.channels and now - last_heartbeat >= self.stale_after / 3:
                        # The protocol has no invented ping/ACK. Get a mapped
                        # input name so a quiet healthy desk remains connected.
                        self._query([self.base, 1, self.channels[0]['channel'] - 1])
                        last_heartbeat = now
                    with self.lock:
                        if now - last_send >= self.spacing:
                            batch = self._read_batch(now)
                            last_send = now  # idle worker also waits, rather than spinning
                        else:
                            batch = []
                    if batch:
                        with self.send_lock:
                            sock.sendall(b''.join(batch))
                        last_send = time.monotonic()
                    # Wake for the next send deadline even when the last read
                    # returned no data; do not impose another full recv delay.
                    sock.settimeout(max(.001, min(.025, self.spacing - (time.monotonic() - last_send))))
                    try:
                        raw = sock.recv(4096)
                    except socket.timeout:
                        raw = None
                    if raw == b'':
                        raise ConnectionError('dLive closed the connection')
                    if raw:
                        last_rx = time.monotonic()
                        for message in stream.feed(raw):
                            self._receive(message)
                    if time.monotonic() - last_rx > self.stale_after:
                        raise TimeoutError('No dLive reports; check MIDI transmit/receive enable')
            except (OSError, ValueError) as exc:
                with self.lock:
                    self.last_error = str(exc)[:200]
            finally:
                with self.lock:
                    if self.sock is sock:
                        self.sock = None
                        self.revision += 1
                if sock:
                    sock.close()
            self.stop.wait(self.retry)

    def _receive(self, message):
        if len(message) == 3 and message[0] & 0xf0 in (0xb0, 0x90):
            midi, control, value = message
            destination = midi & 15
            with self.lock:
                if midi & 0xf0 == 0xb0:
                    state = self.nrpn.setdefault(destination, {})
                    state[control] = value
                    if control != 6 or state.get(98) != 23 or 99 not in state:
                        return
                    note, field, value = state[99], 'level', decode_level(value)
                else:
                    if value == 0:
                        return
                    note, field, value = control, 'mute', value >= 64
                aux = next((a for a in self.auxes if self._destination(a) == (destination, note)), None)
                if aux:
                    self.master[(aux['channel'], field)] = (value, time.time())
                    self.last_received = time.time()
                    self.revision += 1
            return
        if not message.startswith(HEADER):
            return
        body = message[len(HEADER):-1]
        with self.lock:
            if len(body) == 6 and body[0:2] == bytes([self.base, 13]):
                _, _, channel, dest, note, value = body
                aux = next((a for a in self.auxes if self._destination(a) == (dest, note)), None)
                if aux and any(c['channel'] == channel + 1 for c in self.channels):
                    self.observed[(aux['channel'], channel + 1)] = (decode_level(value), time.time())
                    self._expired.discard((aux['channel'], channel + 1))
                else:
                    return
            elif len(body) >= 3 and body[1] == 2:
                self.names[(body[0], body[2])] = body[3:].decode('ascii', errors='replace')[:80]
            else:
                return
            self.last_received = time.time()
            self.revision += 1

    def route_enabled(self, aux, channel=None):
        item = self._aux(aux)
        return bool(item and item.get('enabled', True) and (channel is None or any(
            c['channel'] == channel and c.get('enabled', True) for c in self.channels)))

    def mixer_config(self) -> dict[str, Any]:
        with self.lock:
            auxes = []
            channels = []
            for item in self.auxes:
                name = self.names.get(self._destination(item), f"{'Stereo' if item.get('stereo') else 'Mono'} Aux {item['bus']}")
                auxes.append(dict(item, deskLabel=name, label=item.get('label') or name,
                                  colour=item.get('colour') or '#3478f6', order=item.get('order', item['channel'])))
            for item in self.channels:
                name = self.names.get((self.base, item['channel'] - 1), f"Input {item['channel']}")
                channels.append(dict(item, deskLabel=name, label=item.get('label') or name,
                                     order=item.get('order', item['channel'])))
            return dict(auxes=sorted(auxes, key=lambda a: a['order']),
                        channels=sorted(channels, key=lambda c: c['order']),
                        revision=self.revision, snapshot='', provider='dlive',
                        capabilities=CAPABILITIES, capabilityNotice=UNSUPPORTED)

    def aux_state(self, number) -> dict[str, Any]:
        self.request_aux_state(number)
        cfg = self.mixer_config()
        with self.lock:
            values = []
            for channel in cfg['channels']:
                value, stamp = self.observed.get((number, channel['channel']), (None, None))
                fresh = bool(self.sock and stamp and time.time() - stamp <= self.stale_after)
                values.append(dict(channel, level=value if fresh else None, sendOn=None, pan=None,
                                   observedAt=stamp, stale=not fresh))
            master = {}
            for field in ('level', 'mute'):
                value, stamp = self.master.get((number, field), (None, None))
                fresh = bool(self.sock and stamp and time.time() - stamp <= self.stale_after)
                master[field] = value if fresh else None
                master[field + 'ObservedAt'] = stamp
            return dict(aux=next(a for a in cfg['auxes'] if a['channel'] == number), master=master,
                        channels=values, revision=self.revision, snapshot='', capabilities=CAPABILITIES)

    def _write_master(self, aux, message):
        if not self.route_enabled(aux):
            raise ValueError('AUX not enabled')
        with self.lock, self.send_lock:
            if not self.sock or self.stop.is_set():
                raise ConnectionError('dLive disconnected; command was not queued')
            self.sock.sendall(message)
            self.last_request.pop(aux, None)
            self.request_aux_state(aux)

    def set_master_level(self, aux, db):
        item = self._aux(aux)
        if not item:
            raise ValueError('AUX not found')
        dest, note = self._destination(item)
        value = encode_level(db)
        self._write_master(aux, bytes([0xb0 | dest, 99, note, 0xb0 | dest, 98, 23, 0xb0 | dest, 6, value]))
        return decode_level(value)

    def set_master_mute(self, aux, muted):
        if type(muted) is not bool:
            raise ValueError('Master mute must be boolean')
        item = self._aux(aux)
        if not item:
            raise ValueError('AUX not found')
        dest, note = self._destination(item)
        self._write_master(aux, bytes([0x90 | dest, note, 127 if muted else 63, 0x90 | dest, note, 0]))
        return muted

    def set_level(self, aux, channel, db):
        if not self.route_enabled(aux, channel):
            raise ValueError('Mixer route not enabled')
        value = encode_level(db)
        dest, note = self._destination(self._aux(aux))
        message = HEADER + bytes([self.base, 13, channel - 1, dest, note, value, 247])
        with self.lock, self.send_lock:
            if not self.sock or self.stop.is_set():
                raise ConnectionError('dLive disconnected; command was not queued')
            self.sock.sendall(message)
            self.last_request.pop(aux, None)
            self.request_aux_state(aux)
        return decode_level(value)

    def set_pan(self, *args):
        raise ValueError('dLive MIDI protocol does not document per-send pan')

    def set_send_on(self, *args):
        raise ValueError('Per-send mute unavailable; whole-input mutes are forbidden')

    def input_meter_state(self):
        return dict(channels=[], revision=0, active=False, supported=False)

    def restart_discovery(self):
        with self.lock:
            for channel in self.channels:
                self._query([self.base, 1, channel['channel'] - 1])
            for aux in self.auxes:
                dest, note = self._destination(aux)
                self._query([dest, 1, note])

    def status(self) -> dict[str, Any]:
        with self.lock:
            active = [number for number, stamp in self.last_request.items()
                      if time.monotonic() - stamp <= self.demand_ttl]
            fields = sum(len(self.read_queries[number]) for number in active)
            sweep = (fields * self.spacing / (self.read_batch_limit - 1) +
                     min(1.0, self.stale_after / 3)) if fields else 0
            # An estimate, not a device observation or a throughput guarantee.
            # Keep half the freshness window for response/OS/network delays.
            warning = (f'Active dLive demand exceeds the reserved refresh budget '
                       f'(estimated sweep {sweep:.1f}s; freshness {self.stale_after:g}s). '
                       'Reduce active AUXes/inputs or the read-batch interval; slow or partial '
                       'desk reports may expire. Verify the physical console before live use.'
                       if sweep > self.stale_after / 2 else '')
            expired = {key for key, (_, stamp) in {**self.observed, **self.master}.items()
                       if not self.sock or time.time() - stamp > self.stale_after}
            if expired != self._expired:
                self._expired = expired
                self.revision += 1
            connected = bool(self.sock and self.last_received and time.time() - self.last_received < self.stale_after)
            return dict(provider='dlive', configured=bool(self.config.get('dlive_ip')),
                        enabled=bool(self.config.get('dlive_enabled')), running=bool(self.thread and self.thread.is_alive()),
                        connected=connected, ready=connected, revision=self.revision,
                        desk=f"{self.config.get('dlive_ip', '')}:{self.config.get('dlive_port', 51325)}",
                        lastError=self.last_error, capabilities=CAPABILITIES, capabilityNotice=UNSUPPORTED,
                        pollingWarning=warning,
                        polling=dict(activeAuxes=len(active), fields=fields, batchLimit=self.read_batch_limit,
                                     interval=self.spacing, estimatedSweepSeconds=sweep,
                                     demandLeaseSeconds=self.demand_ttl),
                        channels=len(self.channels), auxes=len(self.auxes))
