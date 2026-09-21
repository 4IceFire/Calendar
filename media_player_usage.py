"""Process-wide player recency, shared by displays and direct library tests.

Recency survives manager replacement, but resets on process restart. Never-used
players tie at zero; the caller's configured order is the stable tie breaker.
"""

import threading
import time


class PlayerUsage:
    def __init__(self):
        self._lock = threading.Lock()
        self._used = {}

    @staticmethod
    def _key(cfg, player):
        return (str(cfg.get('atem_ip') or cfg.get('atem_host') or '127.0.0.1'),
                int(cfg.get('atem_port') or 9910), int(player))

    def last_used(self, cfg, player):
        with self._lock:
            return self._used.get(self._key(cfg, player), 0)

    def record(self, cfg, player):
        with self._lock:
            self._used[self._key(cfg, player)] = time.monotonic_ns()


PLAYER_USAGE = PlayerUsage()
