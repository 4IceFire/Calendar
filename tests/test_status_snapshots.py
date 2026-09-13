from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import webui


class IntegrationStatusSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.refresh_lock = threading.Lock()
        self.atem = Mock()
        self.atem.ping.return_value = False
        self.logs = Mock()
        self.cfg = {'atem_ip': '192.0.2.1', 'atem_port': 9910}
        self.snapshot_cache = {'ts': 0.0, 'payload': None}
        self.atem_cache = {'ts': 0.0, 'connected': True}
        self.connectivity = dict.fromkeys(webui._connectivity_last, None)
        self.connectivity['atem'] = True
        contexts = [
            patch.object(webui.utils, 'get_config', return_value=self.cfg),
            patch.object(webui, '_status_snapshot_refresh_lock', self.refresh_lock),
            patch.object(webui, '_status_snapshot_cache', self.snapshot_cache),
            patch.object(webui, '_atem_status_cache', self.atem_cache),
            patch.object(webui, '_atem_probe_failures', 0),
            patch.object(webui, '_connectivity_last', self.connectivity),
            patch.object(webui, '_get_atem_client_from_config', return_value=self.atem),
            patch.object(webui, 'log_event', self.logs),
        ]
        for service in ('companion', 'propresenter', 'videohub', 'digico', 'hisense', 'pixie', 'scheduler'):
            contexts.append(patch.object(
                webui, f'_probe_{service}_status',
                return_value={'connected': False, 'enabled': False, 'checked_at': time.time()},
            ))
            if service != 'scheduler':
                contexts.append(patch.object(
                    webui, f'_{service}_status_cache', {'ts': 0.0, 'connected': False},
                ))
        for context in contexts:
            context.start()
            self.addCleanup(context.stop)

    def tearDown(self):
        self.release.set()
        # Finish the background worker before the hardware fakes are removed.
        self.assertTrue(self.refresh_lock.acquire(timeout=3.0))
        self.refresh_lock.release()

    def _slow_probe(self, cfg):
        self.entered.set()
        if not self.release.wait(3.0):
            raise TimeoutError('Test did not release the fake status probe')
        return {'connected': False, 'checked_at': time.time()}

    def _finish_refresh(self):
        self.release.set()
        self.assertTrue(self.refresh_lock.acquire(timeout=2.0))
        self.refresh_lock.release()

    def _assert_shared_nonblocking_readers(self, *, populated):
        if populated:
            old_snapshot = {
                'ok': True, 'ts': 1.0,
                'atem': {'connected': True, 'detail': 'Last known connection'},
            }
            self.snapshot_cache.update(ts=1.0, payload=old_snapshot)
        with patch.object(webui, '_probe_companion_status', side_effect=self._slow_probe) as probe:
            with ThreadPoolExecutor(max_workers=10) as readers:
                results = [readers.submit(webui._get_status_snapshot) for _ in range(25)]
                try:
                    self.assertTrue(self.entered.wait(0.5))
                    # Readers must finish while the one hardware refresh is blocked.
                    snapshots = [result.result(timeout=0.5) for result in results]
                    self.assertEqual(probe.call_count, 1)
                    for snapshot in snapshots:
                        self.assertTrue(snapshot['ok'])
                        self.assertEqual(snapshot['atem']['connected'], populated)
                        if populated:
                            self.assertEqual(snapshot['atem']['detail'], 'Last known connection')
                    # The periodic synchronous refresher also joins the same flight.
                    current = webui._refresh_status_snapshot()
                    self.assertEqual(current['atem']['connected'], populated)
                    self.assertEqual(probe.call_count, 1)
                    self.atem.ping.assert_not_called()
                    self.logs.assert_not_called()
                finally:
                    self._finish_refresh()
            self.assertEqual(probe.call_count, 1)
        self.atem.ping.assert_called_once()
        self.assertEqual(webui._atem_probe_failures, 1)

    def test_cold_concurrent_readers_return_immediately_and_share_one_refresh(self):
        self._assert_shared_nonblocking_readers(populated=False)

    def test_stale_concurrent_readers_preserve_snapshot_and_share_one_refresh(self):
        self._assert_shared_nonblocking_readers(populated=True)
        self.logs.assert_not_called()
        self.assertTrue(webui._get_status_snapshot()['atem']['connected'])
        # Offline hysteresis counts actual refreshes, never browser count.
        self.assertTrue(webui._refresh_status_snapshot()['atem']['connected'])
        self.logs.assert_not_called()
        self.assertFalse(webui._refresh_status_snapshot()['atem']['connected'])
        self.atem.ping.return_value = True
        self.assertTrue(webui._refresh_status_snapshot()['atem']['connected'])
        self.assertEqual(webui._atem_probe_failures, 0)
        self.assertEqual(
            [call.args[0] for call in self.logs.call_args_list],
            ['atem.connection.disconnected', 'atem.connection.connected'],
        )

    def test_fresh_snapshot_does_not_probe_hardware(self):
        now = time.time()
        payload = {'ok': True, 'ts': now, 'atem': {'connected': True}}
        self.snapshot_cache.update(ts=now, payload=payload)
        self.assertEqual(webui._get_status_snapshot(), payload)
        self.atem.ping.assert_not_called()
        self.assertFalse(self.refresh_lock.locked())

    def test_failed_refresh_releases_guard_and_keeps_previous_snapshot(self):
        payload = {'ok': True, 'ts': 1.0, 'atem': {'connected': True}}
        self.snapshot_cache.update(ts=1.0, payload=payload)
        with patch.object(webui, '_probe_companion_status', side_effect=RuntimeError('fake probe failed')):
            with self.assertRaisesRegex(RuntimeError, 'fake probe failed'):
                webui._refresh_status_snapshot()
        self.assertFalse(self.refresh_lock.locked())
        self.assertEqual(webui._cached_status_snapshot(), payload)
        self.atem.ping.return_value = True
        self.assertTrue(webui._refresh_status_snapshot()['atem']['connected'])
        self.atem.ping.assert_called_once()


if __name__ == '__main__':
    unittest.main()
