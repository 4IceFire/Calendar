"""Serialized, user-requested image display through ATEM and VideoHub.

Fresh VideoHub reads must contain device-reported port counts and every route;
the ordinary UI snapshot (which can include fallback ports) is not suitable.
Injected hardware callbacks must use bounded I/O timeouts. The routing lock is
held until those callbacks return, including errors, so a timed-out write cannot
outlive its reservation. Nothing runs on construction, startup or reconnect.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager

from atem_media import BusyError, validate_media_config


VIDEO_ROUTING_LOCK = threading.Lock()
DISPLAY_TIMEOUT = 200.0
_TERMINAL = {"succeeded", "failed"}
_MESSAGES = {
    "queued": "Preparing your image…",
    "preparing": "Preparing your image…",
    "loading": "Loading your image…",
    "routing": "Displaying your image…",
    "succeeded": "Image displayed successfully.",
}


@contextmanager
def video_routing_guard():
    """Reserve VideoHub writes without making an HTTP request wait."""
    if not VIDEO_ROUTING_LOCK.acquire(blocking=False):
        raise BusyError("An image or route is being applied. Please wait for it to finish.")
    try:
        yield
    finally:
        VIDEO_ROUTING_LOCK.release()


def _port(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{label} must be a positive whole number")
    try:
        result = int(value)
    except ValueError:
        raise ValueError(f"{label} must be a positive whole number") from None
    if result < 1:
        raise ValueError(f"{label} must be a positive whole number")
    return result


def _strict_state(state):
    if not isinstance(state, dict):
        raise ValueError("VideoHub did not return a fresh routing state")
    # Counts are deliberately required: inferring them from labels/routes could
    # silently omit a real output or authorize a phantom fallback output.
    inputs = _port(state.get("input_count"), "VideoHub input count")
    outputs = _port(state.get("output_count"), "VideoHub output count")
    routes = state.get("routing")
    if (state.get("stale") or state.get("refreshing") or state.get("error")
            or state.get("configured") is False or state.get("ok") is False
            or not isinstance(routes, list) or len(routes) != outputs):
        raise ValueError("VideoHub did not confirm every output route")
    routes = [_port(source, "VideoHub routed input") for source in routes]
    if any(source > inputs for source in routes):
        raise ValueError("VideoHub returned a route outside its input count")
    return {"input_count": inputs, "output_count": outputs, "routing": routes}


def _configuration(cfg):
    normalized = validate_media_config(cfg)
    if not normalized["atem_media_enabled"]:
        raise ValueError("Image display is not enabled. Ask an administrator to finish Media setup.")
    destinations = []
    inputs = set()
    for item in normalized["atem_media_destinations"]:
        if item.get("videohub_input") is None:
            continue  # Existing players may remain available for Config testing.
        source = _port(item.get("videohub_input"), "VideoHub input")
        if source in inputs:
            raise ValueError("Media destinations must have distinct VideoHub inputs")
        inputs.add(source)
        destinations.append({**item, "videohub_input": source})
    if not destinations:
        raise ValueError("Image display is not configured. Ask an administrator to finish Media setup.")
    return normalized, destinations


class _DisplayFailure(RuntimeError):
    def __init__(self, message, detail):
        super().__init__(detail)
        self.public_message = message


class MediaRoutingManager:
    """One asynchronous display job, with at most 32 recent job records.

    read_videohub() -> {input_count, output_count, routing: [1-based inputs]}.
    route_videohub(output, input) writes and verifies using 1-based port numbers.
    get_media_manager() returns the shared ATEM manager, whose load callback must
    report success only after the still and player selection are confirmed.
    ATEM output routing is managed manually outside TDeck.
    """

    def __init__(self, *, get_config, get_media_manager, read_videohub,
                 route_videohub, job_timeout=DISPLAY_TIMEOUT):
        self._get_config = get_config
        self._get_media_manager = get_media_manager
        self._read_videohub = read_videohub
        self._route_videohub = route_videohub
        self._job_timeout = float(job_timeout)
        if not 0 < self._job_timeout <= DISPLAY_TIMEOUT:
            raise ValueError("Display timeout must be greater than zero and at most 200 seconds")
        self._lock = threading.RLock()
        self._jobs = OrderedDict()
        self._active_id = None

    def display(self, media_id, output, allowed_inputs=None, on_complete=None):
        """Queue a display; authorization for the target belongs to the caller."""
        output = _port(output, "Output")
        if not isinstance(media_id, str) or not media_id.strip() or len(media_id) > 128:
            raise ValueError("Choose a saved image.")
        if allowed_inputs is not None and not isinstance(allowed_inputs, (list, tuple, set)):
            raise ValueError("Allowed inputs must be a list of port numbers")
        allowed = {_port(source, "Allowed input") for source in (allowed_inputs or [])}
        if not VIDEO_ROUTING_LOCK.acquire(blocking=False):
            raise BusyError("An image or route is being applied. Please wait for it to finish.")
        job_id = None
        try:
            cfg, destinations = _configuration(self._get_config())
            now = time.time()
            job = {"id": uuid.uuid4().hex, "mediaId": media_id, "output": output,
                   "player": None, "videohubInput": None,
                   "status": "queued", "message": _MESSAGES["queued"], "error": "",
                   "createdAt": now, "updatedAt": now}
            job_id = job["id"]
            with self._lock:
                self._jobs[job_id] = job
                self._active_id = job_id
                while len(self._jobs) > 32:
                    self._jobs.popitem(last=False)
                result = copy.deepcopy(job)
            deadline = time.monotonic() + self._job_timeout
            threading.Thread(target=self._run,
                             args=(job_id, cfg, destinations, allowed, deadline, on_complete),
                             name="tdeck-media-display", daemon=True).start()
            return result
        except Exception:
            with self._lock:
                if job_id:
                    self._jobs.pop(job_id, None)
                    self._active_id = None
            VIDEO_ROUTING_LOCK.release()
            raise

    def get_job(self, job_id):
        with self._lock:
            if not isinstance(job_id, str) or job_id not in self._jobs:
                raise KeyError(job_id)
            return copy.deepcopy(self._jobs[job_id])

    def active_job(self):
        with self._lock:
            return copy.deepcopy(self._jobs.get(self._active_id))

    def snapshot(self):
        with self._lock:
            job = self._jobs.get(self._active_id)
            if job is None and self._jobs:
                job = next(reversed(self._jobs.values()))
            return {"job": copy.deepcopy(job)}

    def _update(self, job_id, status, **fields):
        with self._lock:
            job = self._jobs[job_id]
            job.update(status=status, message=_MESSAGES[status], updatedAt=time.time(), **fields)

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Display job reached its deadline")
        return remaining

    def _read(self, deadline):
        self._remaining(deadline)
        result = _strict_state(self._read_videohub())
        self._remaining(deadline)
        return result

    @staticmethod
    def _choose(destinations, state, output, allowed):
        if output > state["output_count"]:
            raise _DisplayFailure("This output is unavailable. Return to Routing and choose another output.",
                                  "Requested output exceeds the device-reported output count")
        if any(item["videohub_input"] > state["input_count"] for item in destinations):
            raise _DisplayFailure("Image display needs attention in Config.",
                                  "Configured media input exceeds the device-reported input count")
        eligible = [item for item in destinations if not allowed or item["videohub_input"] in allowed]
        if not eligible:
            raise _DisplayFailure("No image source is available for your access. Ask an administrator for help.",
                                  "No configured media VideoHub input is allowed for this user")
        used_elsewhere = {source for index, source in enumerate(state["routing"], 1) if index != output}
        free = [item for item in eligible if item["videohub_input"] not in used_elsewhere]
        if not free:
            raise _DisplayFailure("All image players are in use on other outputs. Ask an administrator for help.",
                                  "Every allowed media input is currently routed to another output")
        current = state["routing"][output - 1]
        return next((item for item in free if item["videohub_input"] == current), free[0])

    def _check_unchanged(self, cfg, initial, current, destination, output):
        now, _ = _configuration(self._get_config())
        keys = ("atem_ip", "atem_host", "atem_port", "atem_media_enabled", "atem_media_node_path",
                "atem_media_destinations", "videohub_ip", "videohub_host", "videohub_port")
        source = destination["videohub_input"]
        changed = (any(cfg.get(key) != now.get(key) for key in keys)
                   or initial["input_count"] != current["input_count"]
                   or initial["output_count"] != current["output_count"]
                   or initial["routing"][output - 1] != current["routing"][output - 1]
                   or any(value == source for index, value in enumerate(current["routing"], 1) if index != output))
        if changed:
            raise _DisplayFailure("Routing changed while preparing your image. Check the output before trying again.",
                                  "Setup, the target route, or the allocated media input changed during display")

    def _wait_ready(self, manager, deadline):
        connection_deadline = min(deadline, time.monotonic() + 10)
        while True:
            self._remaining(deadline)
            state = manager.snapshot()
            if state.get("connected") and state.get("ready"):
                return
            if time.monotonic() >= connection_deadline:
                raise _DisplayFailure("Image display is unavailable. Ask an administrator to check the connection.",
                                      "ATEM did not provide ready media state within the connection deadline")
            time.sleep(min(.05, max(.001, connection_deadline - time.monotonic())))

    @staticmethod
    def _check_media_selection(manager, outcome, destination):
        state = manager.snapshot()
        player = next((item for item in state.get("players", [])
                       if item.get("player") == destination["player"]), {})
        if (not state.get("connected") or not state.get("ready")
                or outcome.get("generation") is None
                or state.get("generation") != outcome["generation"]
                or player.get("type") != "still" or not outcome.get("slot")
                or player.get("slot") != outcome["slot"]):
            raise _DisplayFailure("The image source changed. Check the output before trying again.",
                                  "ATEM connection or selected still changed after the media transfer")

    def _run(self, job_id, cfg, destinations, allowed, deadline, on_complete):
        stage = "preparing"
        try:
            self._update(job_id, stage)
            job = self.get_job(job_id)
            output = job["output"]
            initial = self._read(deadline)
            destination = self._choose(destinations, initial, output, allowed)
            self._update(job_id, stage, player=destination["player"],
                         videohubInput=destination["videohub_input"])
            manager = self._get_media_manager()
            self._wait_ready(manager, deadline)
            self._check_unchanged(cfg, initial, self._read(deadline), destination, output)
            stage = "loading"
            self._update(job_id, stage)
            completed = threading.Event()
            outcome = {}
            outcome_lock = threading.Lock()

            def media_completed(result):
                if not isinstance(result, dict) or result.get("status") not in _TERMINAL:
                    return
                with outcome_lock:
                    if completed.is_set():
                        return
                    outcome.update(copy.deepcopy(result))
                    completed.set()

            manager.load(job["mediaId"], destination["player"], on_complete=media_completed)
            try:
                if not completed.wait(self._remaining(deadline)):
                    raise TimeoutError("ATEM did not finish the display job before its deadline")
                self._remaining(deadline)
            except TimeoutError:
                # Closing fences late ATEM commands before releasing the
                # VideoHub reservation. The transport also has its own deadline.
                manager.close()
                raise
            if outcome.get("status") != "succeeded":
                raise _DisplayFailure("The image could not be loaded. Please try again or ask an administrator.",
                                      str(outcome.get("error") or "ATEM media was not confirmed"))
            stage = "routing"
            self._update(job_id, stage, mediaName=outcome.get("mediaName", ""))
            self._check_unchanged(cfg, initial, self._read(deadline), destination, output)
            self._check_media_selection(manager, outcome, destination)
            self._remaining(deadline)
            self._route_videohub(output, destination["videohub_input"])
            final = self._read(deadline)
            if (final["output_count"] != initial["output_count"]
                    or final["input_count"] != initial["input_count"]
                    or final["routing"][output - 1] != destination["videohub_input"]
                    or any(value == destination["videohub_input"]
                           for index, value in enumerate(final["routing"], 1) if index != output)):
                raise _DisplayFailure("The TV did not confirm the image. Check the output before trying again.",
                                      "VideoHub final route readback did not confirm an exclusive target route")
            self._check_media_selection(manager, outcome, destination)
            self._finish(job_id, "succeeded", on_complete)
        except Exception as error:
            if isinstance(error, _DisplayFailure):
                message = error.public_message
            elif isinstance(error, TimeoutError):
                message = "Displaying the image took too long. Check the output before trying again."
            elif stage == "routing":
                message = "The TV could not confirm the image. Check the output before trying again."
            elif stage == "loading":
                message = "The image could not be loaded. Please try again or ask an administrator."
            else:
                message = "Image display is unavailable. Ask an administrator to check Media setup."
            self._finish(job_id, "failed", on_complete, error=message, internal_error=str(error))

    def _finish(self, job_id, status, callback, *, error="", internal_error=""):
        with self._lock:
            job = self._jobs[job_id]
            if job["status"] in _TERMINAL:
                return
            job.update(status=status, message=error or _MESSAGES[status], error=error,
                       internalError=internal_error[:1000], updatedAt=time.time())
            self._active_id = None
            result = copy.deepcopy(job)
        VIDEO_ROUTING_LOCK.release()
        if callback:
            try:
                callback(result)
            except Exception:
                pass  # A logging failure must not change confirmed hardware state.
