"""Run unittest from synthetic storage with outbound sockets blocked."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

if __name__ == '__main__':
    names = sys.argv[1:] or ['test_timers_direct', 'test_stream_delay']
    with tempfile.TemporaryDirectory(prefix='tdeck-timers-tests-', dir=os.environ.get('TMPDIR')) as tmp:
        os.chdir(tmp)
        import requests
        original_request = requests.sessions.Session.request
        def synthetic_request(session, method, url, **kwargs):
            if isinstance(session.get_adapter(url), requests.adapters.HTTPAdapter):
                raise AssertionError('Outbound HTTP blocked in timer tests')
            return original_request(session, method, url, **kwargs)
        import socket
        original_connect = socket.socket.connect
        def fixture_connect(sock, address):
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1', 'localhost'):
                raise AssertionError('Non-loopback outbound socket blocked in timer tests')
            return original_connect(sock, address)
        original_sendto = socket.socket.sendto
        def fixture_sendto(sock, data, *args):
            address = args[-1]
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1', 'localhost'):
                raise AssertionError('Non-loopback UDP blocked in timer tests')
            return original_sendto(sock, data, *args)
        with patch('socket.socket.connect', fixture_connect), \
             patch('socket.socket.sendto', fixture_sendto), \
             patch('requests.sessions.Session.request', synthetic_request):
            suite = (unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
                     if names == ['--all'] else unittest.defaultTestLoader.loadTestsFromNames(names))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
