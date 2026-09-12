"""Media jobs and the real Node worker logic, using fakes only."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from atem_media import (AtemMediaManager, BusyError, _NodeBridge, get_atem_media_manager,
                       peek_atem_media_job, reset_atem_media_manager, validate_media_config)


def configuration():
    return {"atem_media_enabled": True,
            "atem_media_destinations": [{"player": 1, "label": "Foyer", "slots": [63, 64]}]}


def state():
    return {"connected": True, "ready": True, "generation": 1, "product": "Fake ATEM",
            "videoMode": {"id": 13, "name": "N1080p5994", "width": 2, "height": 2},
            "capabilities": {"players": 4, "stills": 64},
            "players": [{"player": 1, "type": "still", "slot": 63}], "stills": [], "error": ""}


class FakeLibrary:
    def __init__(self):
        self.gate = None

    def get(self, media_id):
        return {"id": media_id, "name": "Mother's Day"} if media_id == "image-1" else None

    def frame(self, media_id, width, height):
        if self.gate:
            self.gate.wait(2)
        return bytes(width * height * 4)


class FakeBridge:
    def __init__(self, cfg, callback):
        self.callback = callback
        self.current_state = state()
        self.closed = False
        self.requests = []
        self.on_load = None
        self.frame_path = None

    def start(self):
        self.callback({"event": "state", "state": self.current_state})

    def request(self, operation, data=None, timeout=None):
        self.requests.append(operation)
        if operation == "snapshot":
            return copy.deepcopy(self.current_state)
        if operation == "load":
            self.frame_path = data["framePath"]
            assert Path(self.frame_path).read_bytes() == bytes(16)
            if self.on_load:
                return self.on_load(data)
            self.callback({"event": "stage", "jobId": data["jobId"], "status": "uploading", "slot": 64})
            self.current_state["players"][0]["slot"] = 64
            return {"confirmed": True, "slot": 64, "state": copy.deepcopy(self.current_state)}
        raise AssertionError(operation)

    def close(self):
        self.closed = True


def eventually(condition, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("Condition did not become true")


class MediaManagerTests(unittest.TestCase):
    def setUp(self):
        self.library = FakeLibrary()
        self.bridges = []

    def manager(self, cfg=None, **kwargs):
        def factory(cfg, callback):
            bridge = FakeBridge(cfg, callback)
            self.bridges.append(bridge)
            return bridge
        manager = AtemMediaManager(cfg or configuration(), self.library, bridge_factory=factory, **kwargs)
        self.addCleanup(manager.close)
        return manager

    def connected(self, **kwargs):
        manager = self.manager(**kwargs)
        manager.snapshot()
        eventually(lambda: manager.snapshot()["connected"])
        return manager, self.bridges[0]

    def test_config_normalizes_without_mutating_and_does_not_assume_32_slots(self):
        cfg = configuration()
        cfg["atem_media_destinations"][0]["player"] = "1"
        cfg["unrelated"] = "retained"
        result = validate_media_config(cfg)
        self.assertEqual(result["atem_media_destinations"][0]["player"], 1)
        self.assertEqual(result["atem_media_destinations"][0]["slots"], [63, 64])
        self.assertEqual(result["unrelated"], "retained")
        self.assertEqual(cfg["atem_media_destinations"][0]["player"], "1")

    def test_config_rejects_duplicates_shared_slots_and_invalid_numbers(self):
        cases = [
            [{"player": 1, "slots": [1]}],
            [{"player": 1, "slots": [1, 1]}],
            [{"player": 1, "slots": [0, 2]}],
            [{"player": True, "slots": [1, 2]}],
            [{"player": 1.5, "slots": [1, 2]}],
            [{"player": 1, "slots": [1, 2]}, {"player": 1, "slots": [3, 4]}],
            [{"player": 1, "slots": [1, 2]}, {"player": 2, "slots": [2, 3]}],
        ]
        for destinations in cases:
            with self.subTest(destinations=destinations), self.assertRaises(ValueError):
                validate_media_config({"atem_media_destinations": destinations})

    def test_disabled_snapshot_does_not_create_worker(self):
        manager = self.manager({"atem_media_enabled": False})
        for _ in range(5):
            self.assertFalse(manager.snapshot()["enabled"])
        self.assertEqual(self.bridges, [])

    def test_snapshot_starts_only_one_connection_and_never_loads_on_reconnect(self):
        manager, bridge = self.connected()
        for _ in range(20):
            manager.snapshot()
        bridge.callback({"event": "state", "state": {**state(), "connected": False, "generation": 2}})
        bridge.callback({"event": "state", "state": {**state(), "generation": 3}})
        self.assertEqual(len(self.bridges), 1)
        self.assertEqual(bridge.requests, [])
        self.assertIsNone(manager.snapshot()["job"])

    def test_success_is_async_callback_once_and_temp_frame_removed(self):
        manager, bridge = self.connected()
        completed = []
        queued = manager.load("image-1", 1, on_complete=completed.append)
        self.assertEqual(queued["status"], "queued")
        eventually(lambda: len(completed) == 1)
        eventually(lambda: not Path(bridge.frame_path).exists())
        self.assertEqual(completed[0]["status"], "succeeded")
        self.assertEqual(completed[0]["slot"], 64)
        self.assertEqual(bridge.requests, ["snapshot", "load"])
        manager.close()
        self.assertEqual(len(completed), 1)

    def test_single_job_and_snapshots_remain_immediate_during_preparation(self):
        self.library.gate = threading.Event()
        self.addCleanup(self.library.gate.set)
        manager, _ = self.connected()
        manager.load("image-1", 1)
        eventually(lambda: manager.snapshot()["job"]["status"] == "preparing")
        started = time.monotonic()
        self.assertTrue(manager.snapshot()["connected"])
        self.assertLess(time.monotonic() - started, 0.05)
        with self.assertRaises(BusyError):
            manager.load("image-1", 1)

    def test_hardware_capability_validation_happens_before_queue(self):
        manager, bridge = self.connected()
        bridge.callback({"event": "state", "state": {**state(), "capabilities": {"players": 2, "stills": 32}}})
        with self.assertRaisesRegex(ValueError, "capacity"):
            manager.load("image-1", 1)
        self.assertEqual(bridge.requests, [])

    def test_unconfirmed_transfer_fails_and_resets_session(self):
        manager, bridge = self.connected()
        bridge.on_load = lambda data: {"confirmed": False}
        completed = []
        manager.load("image-1", 1, on_complete=completed.append)
        eventually(lambda: completed)
        self.assertEqual(completed[0]["status"], "failed")
        self.assertTrue(bridge.closed)
        self.assertFalse(manager.snapshot()["connected"])
        eventually(lambda: not Path(bridge.frame_path).exists())

    def test_late_preparation_cannot_upload_after_total_deadline(self):
        self.library.gate = threading.Event()
        self.addCleanup(self.library.gate.set)
        manager, bridge = self.connected(job_timeout=0.04)
        completed = []
        manager.load("image-1", 1, on_complete=completed.append)
        eventually(lambda: completed)
        self.assertEqual(completed[0]["status"], "failed")
        self.library.gate.set()
        time.sleep(0.03)
        self.assertNotIn("load", bridge.requests)
        self.assertTrue(bridge.closed)
        self.assertEqual(len(completed), 1)

    def test_old_success_after_disconnect_cannot_overwrite_new_state(self):
        manager, bridge = self.connected()
        def stale_success(data):
            old_state = state()
            bridge.callback({"event": "state", "state": {**old_state, "connected": False, "generation": 2}})
            return {"confirmed": True, "slot": 64, "state": old_state}
        bridge.on_load = stale_success
        completed = []
        manager.load("image-1", 1, on_complete=completed.append)
        eventually(lambda: completed)
        self.assertEqual(completed[0]["status"], "failed")
        self.assertFalse(manager.snapshot()["connected"])
        self.assertEqual(manager.snapshot()["generation"], 2)

    def test_response_snapshot_cannot_replace_newer_same_connection_state(self):
        manager, bridge = self.connected()
        def delayed_response(data):
            old_state = {**state(), "revision": 2}
            newer_state = {**state(), "revision": 3, "players": [{"player": 1, "type": "still", "slot": 5}]}
            bridge.callback({"event": "state", "state": newer_state})
            return {"confirmed": True, "slot": 64, "state": old_state}
        bridge.on_load = delayed_response
        completed = []
        manager.load("image-1", 1, on_complete=completed.append)
        eventually(lambda: completed)
        self.assertEqual(manager.snapshot()["revision"], 3)
        self.assertEqual(manager.snapshot()["players"][0]["slot"], 5)

    def test_peek_never_creates_worker_and_factory_refuses_active_replacement(self):
        reset_atem_media_manager()
        self.addCleanup(reset_atem_media_manager)
        self.assertIsNone(peek_atem_media_job())
        singleton = get_atem_media_manager(configuration(), self.library)
        self.assertIsNone(peek_atem_media_job())
        self.assertIsNone(singleton._bridge)
        singleton._job = {"id": "active", "status": "uploading"}
        job = peek_atem_media_job()
        job["status"] = "failed"
        self.assertEqual(peek_atem_media_job()["status"], "uploading")
        with self.assertRaises(BusyError):
            get_atem_media_manager({**configuration(), "atem_media_enabled": False}, self.library)

    def test_config_close_fails_inflight_job_once_without_later_upload(self):
        self.library.gate = threading.Event()
        self.addCleanup(self.library.gate.set)
        manager, bridge = self.connected()
        completed = []
        manager.load("image-1", 1, on_complete=completed.append)
        eventually(lambda: manager.snapshot()["job"]["status"] == "preparing")
        manager.close()
        self.library.gate.set()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["status"], "failed")
        self.assertTrue(bridge.closed)


NODE_TESTS = r"""
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { Enums } = require('atem-connection');
const { MediaWorker } = require('./atem_media_worker.cjs');
class FakeAtem extends EventEmitter {
    constructor(scenario) {
        super(); this.scenario = scenario; this.calls = []; this.disconnected = false;
        this.state = { info: { productIdentifier: 'Fake ATEM', capabilities: { mediaPlayers: 2 }, mediaPool: { stillCount: 64 } },
            settings: { videoMode: Enums.VideoMode.P720p50 }, media: {
                players: [{ sourceType: 1, stillIndex: 62, clipIndex: 0 }, { sourceType: 1, stillIndex: 0, clipIndex: 0 }],
                stillPool: Array.from({length:64}, () => ({ isUsed: false, fileName: '', hash: '' })) } };
    }
    async connect() { this.emit('connected'); }
    async disconnect() { this.disconnected = true; this.emit('disconnected'); }
    async destroy() {}
    async uploadStill(index, encoded) {
        this.calls.push(['upload', index]);
        if (this.scenario === 'hang-transfer') return new Promise(() => {});
        if (this.scenario === 'transfer-failure') throw Error('Transfer failed');
        if (this.scenario === 'reconnect') { this.emit('disconnected'); this.emit('connected'); }
        if (this.scenario === 'format-change') this.state.settings.videoMode = Enums.VideoMode.P1080p50;
        if (this.scenario === 'operator-change') this.state.media.players[0].stillIndex = 3;
        if (this.scenario === 'slot-taken') this.state.media.players[1].stillIndex = index;
        this.state.media.stillPool[index] = { isUsed: true, fileName: 'Uploaded', hash: this.scenario === 'bad-hash' ? 'wrong' : encoded.hash };
        this.emit('stateChanged');
    }
    async setMediaPlayerSource(props, index) {
        this.calls.push(['select', index, props.stillIndex]);
        if (this.scenario === 'ack-without-readback') return;
        Object.assign(this.state.media.players[index], props);
        this.emit('stateChanged');
    }
}
(async () => {
    const results = [];
    for (const scenario of ['success', 'transfer-failure', 'reconnect', 'format-change', 'operator-change', 'slot-taken', 'bad-hash', 'ack-without-readback', 'all-slots-selected', 'incomplete-state', 'preparation-change']) {
        const atem = new FakeAtem(scenario), events = [];
        const worker = new MediaWorker(atem, event => events.push(event), {
            readFrame: async (_, length) => Buffer.alloc(length),
            convert: () => ({ encodedData: Buffer.alloc(8), rawDataLength: 1280*720*4, isRleEncoded: true, hash: 'verified-hash' })
        });
        await worker.init({ host: 'fake-never-network', port: 9910 });
        assert.equal(atem.calls.length, 0, 'init must not upload or select');
        if (scenario === 'all-slots-selected') atem.state.media.players[1].stillIndex = 63;
        if (scenario === 'incomplete-state') atem.state.media.players[1] = undefined;
        const request = { jobId: 'test', player: 1, allowedSlots: [63,64], width: 1280, height: 720,
            expectedPlayer: worker.snapshot().players[0],
            videoModeId: Enums.VideoMode.P720p50, generation: worker.generation, timeoutMs: 90, framePath: 'injected-test-frame', name: 'Test' };
        if (scenario === 'preparation-change') atem.state.media.players[0].stillIndex = 1;
        if (scenario === 'success') {
            const result = await worker.load(request);
            assert.equal(result.confirmed, true);
            assert.equal(result.slot, 64);
            assert.deepEqual(atem.calls, [['upload',63],['select',0,63]]);
            assert.deepEqual(events.filter(e=>e.event==='stage').map(e=>e.status), ['uploading','selecting']);
            assert.equal(result.state.players[0].slot, 64);
        } else {
            await assert.rejects(worker.load(request));
            if (scenario !== 'ack-without-readback') assert.equal(atem.calls.filter(c=>c[0]==='select').length,0, scenario+' must not select');
            if (scenario === 'all-slots-selected' || scenario === 'incomplete-state' || scenario === 'preparation-change') assert.equal(atem.calls.length,0);
            else assert.equal(atem.disconnected,true,'failed transfer must stop its connection');
        }
        await worker.close();
        results.push(scenario);
    }
    process.stdout.write(JSON.stringify(results));
})().catch(error => { console.error(error); process.exitCode=1; });
"""


class NodeMediaWorkerTests(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node or not (ROOT / "node_modules" / "atem-connection").is_dir():
            self.skipTest("Run npm ci and install Node.js to run media worker tests")

    def test_worker_with_fake_atem_never_contacts_hardware(self):
        result = subprocess.run([self.node, "-e", NODE_TESTS], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 11)

    def _ipc_bridge(self, folder, scenario="success"):
        fake_code = NODE_TESTS.split("(async () => {")[0]
        fake_code = fake_code.replace("require('atem-connection')", "require(" + json.dumps(str(ROOT / "node_modules" / "atem-connection")) + ")")
        fake_code = fake_code.replace("require('./atem_media_worker.cjs')", "require(" + json.dumps(str(ROOT / "atem_media_worker.cjs")) + ")")
        fake_code += "\nrequire(" + json.dumps(str(ROOT / "atem_media_worker.cjs")) + ").runIpc(new FakeAtem(" + json.dumps(scenario) + "));\n"
        worker_path = Path(folder) / "fake-worker.cjs"
        worker_path.write_text(fake_code, encoding="utf-8")
        events = []
        bridge = _NodeBridge({"atem_media_node_path": self.node, "atem_ip": "fake-never-network", "atem_port": 9910},
                             events.append, worker_path=worker_path)
        self.addCleanup(bridge.close)
        bridge.start()
        return bridge, events

    def test_real_json_lines_ipc_real_frame_conversion_and_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, events = self._ipc_bridge(folder)
            current = bridge.request("snapshot")
            self.assertTrue(current["ready"])
            self.assertEqual(current["capabilities"], {"players": 2, "stills": 64})
            frame = Path(folder) / "tdeck-atem-frame-test.rgba"
            frame.write_bytes(bytes(1280 * 720 * 4))
            result = bridge.request("load", {"jobId": "ipc-test", "player": 1, "allowedSlots": [63, 64],
                "framePath": str(frame), "name": "Test image", "width": 1280, "height": 720,
                "expectedPlayer": current["players"][0],
                "generation": current["generation"], "videoModeId": current["videoMode"]["id"], "timeoutMs": 10000}, timeout=10)
            self.assertTrue(result["confirmed"])
            self.assertEqual(result["state"]["players"][0]["slot"], 64)
            self.assertEqual([event["status"] for event in events if event["event"] == "stage"], ["uploading", "selecting"])
            bridge.close()

    def test_ipc_timeout_terminates_pending_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, _ = self._ipc_bridge(folder, "hang-transfer")
            current = bridge.request("snapshot")
            frame = Path(folder) / "tdeck-atem-frame-timeout.rgba"
            frame.write_bytes(bytes(1280 * 720 * 4))
            with self.assertRaises(TimeoutError):
                bridge.request("load", {"jobId": "ipc-timeout", "player": 1, "allowedSlots": [63, 64],
                    "framePath": str(frame), "width": 1280, "height": 720,
                    "expectedPlayer": current["players"][0],
                    "generation": current["generation"], "videoModeId": current["videoMode"]["id"], "timeoutMs": 10000}, timeout=0.1)
            self.assertIsNotNone(bridge._process.poll())
            with self.assertRaises(RuntimeError):
                bridge.request("snapshot")


if __name__ == "__main__":
    unittest.main()
