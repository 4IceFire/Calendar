"""Asynchronous ATEM still-media transport, independent of Record Audio.

The private Node worker owns a separate connection and can select a reserved AUX.
Only a user-requested job may upload/select media; reconnects only refresh state.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any


JOB_TIMEOUT = 180.0
_TERMINAL = {"succeeded", "failed"}
_MANAGER_LOCK = threading.Lock()
_MANAGER = None
_MANAGER_KEY = None


class BusyError(ValueError):
    """Another media load is still in progress."""


def _positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{label} must be a positive whole number")
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{label} must be a positive whole number") from None
    if result < 1:
        raise ValueError(f"{label} must be a positive whole number")
    return result


def validate_media_config(cfg):
    """Return a normalized config copy; capacity is checked against the device."""
    result = dict(cfg or {})
    enabled = result.get("atem_media_enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("ATEM media enabled must be true or false")
    result["atem_media_enabled"] = enabled
    node_path = result.get("atem_media_node_path", "")
    if not isinstance(node_path, str):
        raise ValueError("Node executable must be a path or blank")
    result["atem_media_node_path"] = node_path.strip()
    destinations = result.get("atem_media_destinations", [])
    if not isinstance(destinations, list):
        raise ValueError("ATEM media destinations must be a list")
    normalized, players, slots, auxes, inputs = [], set(), set(), set(), set()
    for destination in destinations:
        if not isinstance(destination, dict):
            raise ValueError("Each ATEM media destination must be an object")
        player = _positive_int(destination.get("player"), "Media player")
        if player in players:
            raise ValueError(f"Media Player {player} is configured more than once")
        players.add(player)
        reserved = destination.get("slots", [])
        if not isinstance(reserved, list) or len(reserved) < 2:
            raise ValueError(f"Media Player {player} needs at least two reserved still slots")
        reserved = [_positive_int(slot, "Still slot") for slot in reserved]
        if len(set(reserved)) != len(reserved):
            raise ValueError(f"Media Player {player} contains a duplicate still slot")
        if slots.intersection(reserved):
            raise ValueError("Reserved still slots cannot be shared between destinations")
        slots.update(reserved)
        label = destination.get("label", "")
        if not isinstance(label, str):
            raise ValueError("Media destination label must be text")
        entry = {"player": player, "label": label.strip()[:100] or f"Media Player {player}", "slots": reserved}
        aux, videohub_input = destination.get("aux"), destination.get("videohub_input")
        has_aux, has_input = aux not in (None, ""), videohub_input not in (None, "")
        if has_aux != has_input:
            raise ValueError("Set both the ATEM AUX and VideoHub input, or leave both blank")
        if has_aux:
            aux = _positive_int(aux, "ATEM AUX")
            videohub_input = _positive_int(videohub_input, "VideoHub input")
            if aux in auxes:
                raise ValueError("ATEM AUXes cannot be shared between media destinations")
            if videohub_input in inputs:
                raise ValueError("VideoHub inputs cannot be shared between media destinations")
            auxes.add(aux)
            inputs.add(videohub_input)
            entry.update(aux=aux, videohub_input=videohub_input)
        normalized.append(entry)
    result["atem_media_destinations"] = normalized
    return result


class _NodeBridge:
    """Private JSON-lines IPC. A timeout kills the session, including pending work."""

    def __init__(self, cfg, on_event, *, worker_path=None):
        self.cfg = cfg
        self.on_event = on_event
        self._worker_path = worker_path
        self._process = None
        self._lock = threading.RLock()
        self._pending = {}
        self._closed = False
        self._stderr_tail = ""
        self._stderr_thread = None
        self._reader_thread = None
        self._failure = ""

    def start(self):
        configured = self.cfg.get("atem_media_node_path", "")
        executable = configured or shutil.which("node")
        if not executable or (configured and not Path(configured).is_file()):
            raise RuntimeError("Node.js is unavailable. Install Node.js and run npm ci in Calendar, or configure its executable path.")
        worker = Path(self._worker_path) if self._worker_path else Path(__file__).with_name("atem_media_worker.cjs")
        with self._lock:
            if self._closed:
                raise RuntimeError("ATEM media worker has closed")
            self._process = subprocess.Popen(
                [str(executable), str(worker)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=str(worker.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            self._stderr_thread = threading.Thread(target=self._read_stderr, name="tdeck-atem-media-errors", daemon=True)
            self._reader_thread = threading.Thread(target=self._read, name="tdeck-atem-media-ipc", daemon=True)
            self._stderr_thread.start()
            self._reader_thread.start()
        try:
            self.request("init", {"host": self.cfg.get("atem_ip") or self.cfg.get("atem_host") or "127.0.0.1",
                                  "port": int(self.cfg.get("atem_port") or 9910)}, timeout=10)
        except RuntimeError:
            # An import failure may exit before init is written. Let the reader
            # collect its explanation before the manager closes this bridge.
            if self._process.poll() is not None:
                self._reader_thread.join(timeout=1)
                if self._failure:
                    raise RuntimeError(self._failure) from None
            raise

    def _read_stderr(self):
        try:
            while chunk := self._process.stderr.read(1024):
                # Drain continuously, but never retain an unbounded crash/log stream.
                with self._lock:
                    self._stderr_tail = (self._stderr_tail + chunk)[-4096:]
        except (OSError, ValueError):
            pass

    def _stopped_message(self, read_error=""):
        code = self._process.poll()
        prefix = "ATEM media worker stopped" + (f" (exit code {code})" if code is not None else "")
        with self._lock:
            tail = self._stderr_tail
        tail = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", tail)
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        # Prefer the exception message over file paths, stack frames and Node's
        # trailing version line. Detailed state is restricted to Config access.
        detail = next((line for line in lines if re.match(r"\w*Error(?: \[[\w_]+\])?:", line)), "")
        detail = detail or read_error or (lines[-1] if lines else "No error details were reported.")
        detail = " ".join(detail.split())[:320]
        hint = " Run npm ci --omit=dev in Calendar." if (
            "Cannot find module" in detail or "MODULE_NOT_FOUND" in tail) else ""
        return f"{prefix}: {detail}{hint}"[:500]

    def _read(self):
        process = self._process
        read_error = ""
        try:
            for line in process.stdout:
                if len(line) > 262144:
                    raise RuntimeError("ATEM media worker returned an oversized response")
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message:
                    with self._lock:
                        target = self._pending.get(str(message["id"]))
                    if target is not None:
                        target.put(message)
                elif "event" in message:
                    self.on_event(message)
        except Exception as error:
            read_error = str(error)[:320]
        finally:
            with self._lock:
                intentional = self._closed
            if not intentional:
                # stdout can close before the process exits. Stop it before
                # waiting for stderr EOF so neither reader can block forever.
                try:
                    process.wait(timeout=0.2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                self._stderr_thread.join(timeout=0.5)
            with self._lock:
                intentional = self._closed
                failure = "ATEM media operation was interrupted" if intentional else self._stopped_message(read_error)
                self._failure = failure
                pending = list(self._pending.values())
            for target in pending:
                target.put({"error": failure})
            if not intentional:
                self.on_event({"event": "stopped", "error": failure})

    def request(self, operation, data=None, *, timeout=10):
        request_id = uuid.uuid4().hex
        result_queue = queue.Queue()
        with self._lock:
            process = self._process
            if self._closed or process is None:
                raise RuntimeError(self._failure or "ATEM media worker is unavailable")
            exited = process.poll() is not None
            if not exited:
                self._pending[request_id] = result_queue
                try:
                    process.stdin.write(json.dumps({"id": request_id, "op": operation, **(data or {})}, ensure_ascii=True) + "\n")
                    process.stdin.flush()
                except Exception:
                    self._pending.pop(request_id, None)
                    raise RuntimeError("Cannot communicate with the ATEM media worker") from None
        if exited:
            # A child may crash between requests. Wait outside the IPC lock so
            # its readers can finish capturing the error before cleanup begins.
            if self._reader_thread is not None and self._reader_thread is not threading.current_thread():
                self._reader_thread.join(timeout=1)
            raise RuntimeError(self._failure or self._stopped_message())
        try:
            result = result_queue.get(timeout=max(0.01, timeout))
            if result.get("error"):
                raise RuntimeError(str(result["error"])[:500])
            return result.get("result")
        except queue.Empty:
            self.close()
            raise TimeoutError("ATEM media operation timed out; completion was not confirmed") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def close(self):
        with self._lock:
            self._closed = True
            process = self._process
            pending = list(self._pending.values())
        for target in pending:
            target.put({"error": "ATEM media operation was interrupted"})
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for reader in (self._reader_thread, self._stderr_thread):
                if reader is not None and reader is not threading.current_thread():
                    reader.join(timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except (AttributeError, OSError):
                    pass


class AtemMediaManager:
    def __init__(self, cfg, library, *, bridge_factory=None, job_timeout=JOB_TIMEOUT):
        self.cfg = validate_media_config(cfg)
        self.library = library
        self._bridge_factory = bridge_factory or _NodeBridge
        self._job_timeout = job_timeout
        self._lock = threading.RLock()
        self._bridge = None
        self._starting = False
        self._closed = False
        self._retry_at = 0.0
        self._failures = 0
        self._job = None
        self._callback = None
        self._timer = None
        self._state = {"connected": False, "ready": False, "product": "", "videoMode": None,
                       "capabilities": {"players": 0, "stills": 0, "auxes": 0}, "players": [], "stills": [], "auxes": [],
                       "generation": None, "revision": None, "error": ""}

    def snapshot(self):
        with self._lock:
            if (self.cfg["atem_media_enabled"] and not self._closed and self._bridge is None
                    and not self._starting and time.monotonic() >= self._retry_at):
                self._starting = True
                threading.Thread(target=self._start, name="tdeck-atem-media-connect", daemon=True).start()
            return {**copy.deepcopy(self._state), "enabled": self.cfg["atem_media_enabled"],
                    "destinations": copy.deepcopy(self.cfg["atem_media_destinations"]),
                    "job": copy.deepcopy(self._job)}

    def _start(self):
        bridge = self._bridge_factory(self.cfg, lambda message: self._event(bridge, message))
        with self._lock:
            if self._closed:
                self._starting = False
                return
            self._bridge = bridge
            self._state.update(connected=False, ready=False, generation=None, revision=None)
        try:
            bridge.start()
        except Exception as exc:
            bridge.close()
            self._event(bridge, {"event": "stopped", "error": str(exc)})
        finally:
            with self._lock:
                self._starting = False

    def _event(self, bridge, message):
        with self._lock:
            if self._closed or bridge is not self._bridge:
                return
            if message.get("event") == "state":
                state = message.get("state")
                if isinstance(state, dict):
                    previous = self._state.get("generation")
                    incoming = state.get("generation")
                    if isinstance(previous, int) and isinstance(incoming, int) and incoming < previous:
                        return
                    old_revision, new_revision = self._state.get("revision"), state.get("revision")
                    if (previous == incoming and isinstance(old_revision, int) and isinstance(new_revision, int)
                            and new_revision < old_revision):
                        return
                    self._state.update(copy.deepcopy(state))
                    if state.get("connected"):
                        self._failures = 0
            elif message.get("event") == "stage":
                if (self._job and self._job["id"] == message.get("jobId")
                        and self._job["status"] not in _TERMINAL
                        and message.get("status") in {"uploading", "selecting", "routing"}):
                    self._job.update(status=message["status"], slot=message.get("slot"), updatedAt=time.time())
            elif message.get("event") == "stopped":
                self._state.update(connected=False, ready=False, error=str(message.get("error") or "ATEM media worker stopped")[:500])
                self._bridge = None
                self._failures += 1
                self._retry_at = time.monotonic() + min(30, 2 ** min(self._failures, 5))

    @staticmethod
    def _check_state(state, destination, aux=None):
        if not state.get("connected") or not state.get("ready"):
            raise ValueError("ATEM media is not ready. Wait for a complete switcher connection.")
        capabilities = state.get("capabilities") or {}
        if destination["player"] > int(capabilities.get("players") or 0):
            raise ValueError("The configured media player is not available on this ATEM")
        if any(slot > int(capabilities.get("stills") or 0) for slot in destination["slots"]):
            raise ValueError("A reserved still slot exceeds this ATEM's media pool capacity")
        if aux is not None:
            if aux > int(capabilities.get("auxes") or 0):
                raise ValueError("The configured AUX is not available on this ATEM")
            current = next((item for item in state.get("auxes", []) if item.get("aux") == aux), None)
            if not current or not isinstance(current.get("source"), int):
                raise ValueError("ATEM AUX routing state is not ready")
            player = next((item for item in state.get("players", []) if item.get("player") == destination["player"]), {})
            if not isinstance(player.get("fillSource"), int) or player["fillSource"] < 1:
                raise ValueError("This ATEM has not reported an available media player source for AUX routing")

    def load(self, media_id, player, *, aux=None, on_complete=None):
        player = _positive_int(player, "Media player")
        destination = next((item for item in self.cfg["atem_media_destinations"] if item["player"] == player), None)
        if destination is None:
            raise ValueError("This media player has not been configured for TDeck")
        if aux is not None:
            aux = _positive_int(aux, "ATEM AUX")
            if destination.get("aux") != aux:
                raise ValueError("This AUX has not been reserved for the selected media player")
        media = self.library.get(media_id)
        if not media:
            raise ValueError("The selected media item no longer exists")
        with self._lock:
            if self._closed or not self.cfg["atem_media_enabled"]:
                raise ValueError("ATEM media is disabled")
            if self._job and self._job["status"] not in _TERMINAL:
                raise BusyError("Another image is loading. Wait for it to finish.")
            self._check_state(self._state, destination, aux)
            if self._bridge is None:
                raise ValueError("ATEM media is not connected")
            job = {"id": uuid.uuid4().hex, "mediaId": str(media_id),
                   "mediaName": str(media.get("name") or media.get("displayName") or media_id)[:200],
                   "player": player, "slot": None, "status": "queued", "error": "",
                   "createdAt": time.time(), "updatedAt": time.time()}
            if aux is not None:
                job["aux"] = aux
            self._job = job
            self._callback = on_complete
            result = copy.deepcopy(job)
            bridge = self._bridge
            deadline = time.monotonic() + self._job_timeout
            self._timer = threading.Timer(self._job_timeout, self._expired, args=(job["id"], bridge))
            self._timer.daemon = True
            self._timer.start()
            threading.Thread(target=self._run_job, args=(job["id"], destination, bridge, deadline, aux),
                             name="tdeck-atem-media-load", daemon=True).start()
            return result

    def _remaining(self, job_id, deadline):
        with self._lock:
            if self._closed or not self._job or self._job["id"] != job_id or self._job["status"] in _TERMINAL:
                raise RuntimeError("ATEM media job is no longer active")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Image load timed out; completion was not confirmed")
        return remaining

    def _run_job(self, job_id, destination, bridge, deadline, aux=None):
        frame_path = None
        try:
            state = bridge.request("snapshot", timeout=min(10, self._remaining(job_id, deadline)))
            self._check_state(state, destination, aux)
            with self._lock:
                self._remaining(job_id, deadline)
                self._job.update(status="preparing", updatedAt=time.time())
                media_id = self._job["mediaId"]
                media_name = self._job["mediaName"]
            mode = state.get("videoMode") or {}
            width, height = int(mode.get("width") or 0), int(mode.get("height") or 0)
            if width < 1 or height < 1 or width * height > 7680 * 4320:
                raise ValueError("ATEM video resolution is unavailable or unsupported")
            rgba = self.library.frame(media_id, width, height)
            self._remaining(job_id, deadline)
            if not isinstance(rgba, bytes) or len(rgba) != width * height * 4:
                raise ValueError("Prepared image does not match the ATEM resolution")
            with tempfile.NamedTemporaryFile(prefix="tdeck-atem-frame-", suffix=".rgba", delete=False) as frame:
                frame_path = frame.name
                frame.write(rgba)
            remaining = self._remaining(job_id, deadline)
            result = bridge.request("load", {"jobId": job_id, "player": destination["player"],
                "allowedSlots": destination["slots"], "framePath": frame_path, "name": media_name,
                "width": width, "height": height, "generation": state.get("generation"),
                "aux": aux, "expectedAux": next((item for item in state.get("auxes", []) if item.get("aux") == aux), None),
                "expectedPlayer": next((player for player in state.get("players", [])
                                        if player.get("player") == destination["player"]), None),
                "videoModeId": mode.get("id"), "timeoutMs": max(1, int(remaining * 1000))}, timeout=remaining)
            self._remaining(job_id, deadline)
            if not isinstance(result, dict) or not result.get("confirmed"):
                raise RuntimeError("ATEM did not confirm the selected image")
            confirmed_state = result.get("state") or {}
            if not confirmed_state.get("connected") or confirmed_state.get("generation") != state.get("generation"):
                raise RuntimeError("ATEM connection changed before completion was confirmed")
            self._event(bridge, {"event": "state", "state": confirmed_state})
            self._finish(job_id, "succeeded", slot=result.get("slot"),
                         bridge=bridge, generation=state.get("generation"), deadline=deadline)
        except Exception as exc:
            self._drop_bridge(bridge, str(exc))
            self._finish(job_id, "failed", error=str(exc))
        finally:
            if frame_path is not None:
                try:
                    os.unlink(frame_path)
                except OSError:
                    pass

    def _drop_bridge(self, bridge, error):
        self._event(bridge, {"event": "stopped", "error": error})
        bridge.close()

    def _expired(self, job_id, bridge):
        with self._lock:
            active = self._job and self._job["id"] == job_id and self._job["status"] not in _TERMINAL
        if active:
            self._drop_bridge(bridge, "Image load timed out; completion was not confirmed")
            self._finish(job_id, "failed", error="Image load timed out; completion was not confirmed")

    def _finish(self, job_id, status, *, error="", slot=None, bridge=None, generation=None, deadline=None):
        with self._lock:
            if not self._job or self._job["id"] != job_id or self._job["status"] in _TERMINAL:
                return
            if status == "succeeded":
                self._remaining(job_id, deadline)
                if (self._bridge is not bridge or not self._state.get("connected")
                        or self._state.get("generation") != generation):
                    raise RuntimeError("ATEM connection changed before completion was confirmed")
                aux = self._job.get("aux")
                if aux is not None:
                    player = next((item for item in self._state.get("players", []) if item.get("player") == self._job["player"]), {})
                    current_aux = next((item for item in self._state.get("auxes", []) if item.get("aux") == aux), {})
                    if (player.get("type") != "still" or player.get("slot") != slot
                            or not isinstance(player.get("fillSource"), int)
                            or current_aux.get("source") != player["fillSource"]):
                        raise RuntimeError("ATEM media or AUX routing changed before completion was confirmed")
                self._job["generation"] = generation
            self._job.update(status=status, error=error[:500], updatedAt=time.time())
            if slot is not None:
                self._job["slot"] = slot
            if self._timer:
                self._timer.cancel()
                self._timer = None
            callback, self._callback = self._callback, None
            result = copy.deepcopy(self._job)
        if callback is not None:
            try:
                callback(result)
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._closed = True
            bridge, self._bridge = self._bridge, None
            job_id = self._job["id"] if self._job else None
            self._state.update(connected=False, ready=False)
        if bridge:
            bridge.close()
        if job_id:
            self._finish(job_id, "failed", error="ATEM media settings changed or the service stopped")


def get_atem_media_manager(cfg, library):
    global _MANAGER, _MANAGER_KEY
    normalized = validate_media_config(cfg)
    keys = ("atem_ip", "atem_host", "atem_port", "atem_media_enabled", "atem_media_node_path", "atem_media_destinations")
    key = (json.dumps({name: normalized.get(name) for name in keys}, sort_keys=True), id(library))
    with _MANAGER_LOCK:
        if _MANAGER is None or key != _MANAGER_KEY:
            previous = _MANAGER
            if previous:
                with previous._lock:
                    if previous._job and previous._job["status"] not in _TERMINAL:
                        raise BusyError("Wait for the current image load before changing ATEM media settings")
            _MANAGER = AtemMediaManager(normalized, library)
            _MANAGER_KEY = key
            if previous:
                previous.close()
        return _MANAGER


def peek_atem_media_job():
    """Inspect the current job without constructing a manager or starting IPC."""
    with _MANAGER_LOCK:
        if _MANAGER is None:
            return None
        with _MANAGER._lock:
            return copy.deepcopy(_MANAGER._job)


def reset_atem_media_manager():
    """Release the singleton for shutdown and hardware-independent tests."""
    global _MANAGER, _MANAGER_KEY
    with _MANAGER_LOCK:
        if _MANAGER:
            _MANAGER.close()
        _MANAGER = _MANAGER_KEY = None
