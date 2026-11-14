"""Process-wide Personal Mixes provider selection; legacy HTTP names stay stable."""
import copy
import json
import math
import threading
from typing import Any

from digico import close_digico_client, get_digico_client_from_config
from dlive import DLiveMixerClient

DEFAULTS = dict(personal_mix_provider='digico', dlive_enabled=False, dlive_ip='',
                dlive_port=51325, dlive_midi_channel=1, dlive_request_interval=.025,
                dlive_retry_interval=1., dlive_stale_after=10., dlive_auxes=[], dlive_channels=[],
                dlive_channel_organization=None, digico_channel_organization=None)
CONFIG_KEYS = tuple(DEFAULTS)
LOCK = threading.RLock()
_CLIENT = None
_SIGNATURE = None


def is_protected_key(key):
    # Both desks are owned by dedicated setup; stale generic Config snapshots
    # must not separate physical mappings from their canonical organization.
    return key == 'personal_mix_provider' or key.startswith(('dlive_', 'digico_'))


def validate_config(raw):
    if any(key.startswith('dlive_') and key not in CONFIG_KEYS for key in raw):
        raise ValueError('Unknown dLive setting; TLS/authentication must not silently fall back to plain TCP')
    cfg = dict(DEFAULTS, **copy.deepcopy(raw))
    if cfg['personal_mix_provider'] not in ('digico', 'dlive'):
        raise ValueError('Choose DiGiCo or dLive')
    if type(cfg['dlive_enabled']) is not bool:
        raise ValueError('dlive_enabled must be boolean')
    host = cfg['dlive_ip']
    if not isinstance(host, str) or len(host) > 253 or any(c.isspace() or ord(c) < 32 for c in host):
        raise ValueError('dLive host must be a hostname or IP without whitespace')
    for key, low, high in [('dlive_port', 1, 65535), ('dlive_midi_channel', 1, 12)]:
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{key} must be an integer from {low} to {high}')
    if cfg['dlive_port'] in (51327, 51329):
        raise ValueError('TLS/authenticated dLive ports are not supported; never downgrade a TLS-only installation')
    for key, low, high in [('dlive_request_interval', .01, 5), ('dlive_retry_interval', .1, 30), ('dlive_stale_after', 1, 300)]:
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} must be finite, from {low} to {high}')
    for key, maximum in [('dlive_auxes', 93), ('dlive_channels', 128)]:
        items = cfg[key]
        if not isinstance(items, list) or len(items) > maximum:
            raise ValueError(f'{key} must be a list with at most {maximum} entries')
        seen = set()
        normalized = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f'{key} entries must be objects')
            number = item.get('channel')
            if type(number) is not int or number in seen:
                raise ValueError(f'{key} requires distinct integer channel IDs')
            seen.add(number)
            cleaned: dict[str, Any] = dict(channel=number)
            if key == 'dlive_auxes':
                stereo = item.get('stereo', False)
                bus = item.get('bus')
                if type(stereo) is not bool or type(bus) is not int or not 1 <= bus <= (31 if stereo else 62):
                    raise ValueError('Map a mono AUX 1–62 or stereo AUX 1–31 explicitly')
                if number != (2000 if stereo else 1000) + bus:
                    raise ValueError('dLive AUX IDs must be 1000 + mono bus or 2000 + stereo bus; SD9 grants are not migrated')
                cleaned.update(bus=bus, stereo=stereo)
            elif not 1 <= number <= 128:
                raise ValueError('dLive input channels must be 1–128')
            enabled = item.get('enabled', True)
            if type(enabled) is not bool:
                raise ValueError('Mapping enabled must be boolean')
            cleaned['enabled'] = enabled
            for field in ('label', 'group', 'icon', 'colour'):
                value = item.get(field, '')
                if not isinstance(value, str) or len(value) > 80:
                    raise ValueError(f'{field} must be text of at most 80 characters')
                cleaned[field] = value.strip()
            order = item.get('order', len(normalized) + 1)
            if type(order) is not int or not 1 <= order <= 10000:
                raise ValueError('Mapping order must be a positive integer')
            cleaned['order'] = order
            normalized.append(cleaned)
        cfg[key] = normalized
    if cfg['dlive_enabled'] and (not host or not cfg['dlive_auxes'] or not cfg['dlive_channels']):
        raise ValueError('Enabling dLive requires a host and explicit AUX/input mappings')
    from personal_mix_organization import validate_channel_organizations
    return validate_channel_organizations(cfg)


def get_client(config):
    global _CLIENT, _SIGNATURE
    cfg = validate_config(config)
    signature = json.dumps({k: cfg[k] for k in CONFIG_KEYS}, sort_keys=True)
    with LOCK:
        if cfg['personal_mix_provider'] == 'digico':
            if _CLIENT is not None:
                _CLIENT.close()
                _CLIENT = None
                _SIGNATURE = None
            return get_digico_client_from_config(cfg)
        close_digico_client()
        if _CLIENT is None or signature != _SIGNATURE:
            if _CLIENT is not None:
                _CLIENT.close()
            _CLIENT = DLiveMixerClient(cfg)
            _SIGNATURE = signature
        _CLIENT.start()
        return _CLIENT


def close_client():
    global _CLIENT, _SIGNATURE
    with LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
        _CLIENT = None
        _SIGNATURE = None
        close_digico_client()
