"""Run dLive / full unittest regression from private storage with hardware guards.

Custom requests BaseAdapters remain executable (timer tests use these in-memory
transports); ordinary HTTPAdapters and all non-loopback socket traffic are denied.
Cold import redirects the checkout auth path before webui can initialize it.
"""
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
    names = sys.argv[1:] or ['test_dlive', 'test_dlive_webui', 'test_dlive_acceptance', 'test_dlive_configuration', 'test_digico', 'test_digico_webui', 'test_frontend_compatibility']
    original_connect = socket.socket.connect
    original_sendto = socket.socket.sendto
    original_db = sqlite3.connect
    import requests
    original_request = requests.sessions.Session.request
    checkout_db = ROOT / 'auth.db'
    before = checkout_db.stat() if checkout_db.exists() else None

    def loopback(address):
        if not isinstance(address, tuple):
            return False
        if address[0] == 'localhost':
            return True
        try:
            return ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            return False

    def connect(sock, address):
        if not loopback(address):
            raise AssertionError('Non-loopback socket blocked in dLive tests')
        return original_connect(sock, address)

    def sendto(sock, data, *args):
        if not loopback(args[-1]):
            raise AssertionError('Non-loopback UDP blocked in dLive tests')
        return original_sendto(sock, data, *args)

    def synthetic_request(session, method, url, **kwargs):
        if isinstance(session.get_adapter(url), requests.adapters.HTTPAdapter):
            raise AssertionError('Outbound HTTP blocked in dLive tests')
        return original_request(session, method, url, **kwargs)

    with tempfile.TemporaryDirectory(prefix='tdeck-dlive-tests-', dir=os.environ.get('TMPDIR')) as directory:
        os.chdir(directory)
        private_db = Path(directory) / 'auth.db'
        def isolated_db(database, *args, **kwargs):
            if str(database) == str(checkout_db):
                database = str(private_db)
            return original_db(database, *args, **kwargs)
        with (patch('sqlite3.connect', isolated_db), patch('socket.socket.connect', connect),
              patch('socket.socket.sendto', sendto),
              patch('requests.sessions.Session.request', synthetic_request)):
            with patch.object(threading.Thread, 'start'):
                import webui
            webui._AUTH_DB_PATH = private_db
            suite = (unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
                     if names == ['--all'] else unittest.defaultTestLoader.loadTestsFromNames(names))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        assert (checkout_db.stat() if checkout_db.exists() else None) == before, 'Checkout auth DB changed'
    return not result.wasSuccessful()


if __name__ == '__main__':
    sys.exit(main())
