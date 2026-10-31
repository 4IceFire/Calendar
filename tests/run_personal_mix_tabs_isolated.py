"""Run mixer/API/frontend regressions without checkout auth or real hardware."""
import ipaddress
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


def main():
    names = sys.argv[1:] or ['test_digico', 'test_digico_webui', 'test_frontend_compatibility']
    original_connect = socket.socket.connect
    original_sendto = socket.socket.sendto
    original_db = sqlite3.connect
    checkout_db = ROOT / 'auth.db'
    before = checkout_db.stat() if checkout_db.exists() else None

    def is_loopback(address):
        if not isinstance(address, tuple):
            return False
        if address[0] == 'localhost':
            return True
        try:
            return ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            return False

    def loopback_connect(sock, address):
        if not is_loopback(address):
            raise AssertionError('Non-loopback socket blocked in mixer tests')
        return original_connect(sock, address)

    def loopback_sendto(sock, data, *args):
        address = args[-1]
        if not is_loopback(address):
            raise AssertionError('Non-loopback UDP blocked in mixer tests')
        return original_sendto(sock, data, *args)

    with tempfile.TemporaryDirectory(prefix='tdeck-mix-tabs-tests-', dir=os.environ.get('TMPDIR')) as directory:
        os.chdir(directory)
        private_db = Path(directory) / 'auth.db'

        def isolated_db(database, *args, **kwargs):
            if str(database) == str(checkout_db):
                database = str(private_db)
            return original_db(database, *args, **kwargs)

        with (patch('sqlite3.connect', isolated_db),
              patch('socket.socket.connect', loopback_connect),
              patch('socket.socket.sendto', loopback_sendto),
              patch('requests.sessions.Session.request', side_effect=AssertionError('Outbound HTTP blocked in mixer tests'))):
            with patch.object(threading.Thread, 'start'):
                import webui
            webui._AUTH_DB_PATH = private_db
            suite = unittest.defaultTestLoader.loadTestsFromNames(names)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        assert (checkout_db.stat() if checkout_db.exists() else None) == before, 'Checkout auth DB changed'
    return not result.wasSuccessful()


if __name__ == '__main__':
    sys.exit(main())
