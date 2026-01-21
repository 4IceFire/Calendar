from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from package.apps.videohub.models import VideohubPreset, VideohubRoute


DEFAULT_PRESETS_FILE = "videohub_presets.json"

_lock = threading.Lock()


def _coerce_route(value: Any) -> VideohubRoute | None:
    if not isinstance(value, dict):
        return None

    try:
        output = int(value.get("output"))
        input_ = int(value.get("input"))
    except Exception:
        return None

    try:
        monitoring = bool(value.get("monitoring", value.get("monitor", False)))
    except Exception:
        monitoring = False

    if output <= 0 or input_ <= 0:
        return None

    return VideohubRoute(output=output, input=input_, monitoring=monitoring)


def _coerce_preset(value: Any, *, next_id: int) -> tuple[VideohubPreset | None, int]:
    if not isinstance(value, dict):
        return None, next_id

    raw_id = value.get("id")
    preset_id: int
    if isinstance(raw_id, int) and raw_id > 0:
        preset_id = raw_id
    else:
        preset_id = next_id
        next_id += 1

    name = str(value.get("name") or value.get("label") or f"Preset {preset_id}").strip()
    if not name:
        name = f"Preset {preset_id}"

    routes_raw = value.get("routes")
    if routes_raw is None:
        routes_raw = value.get("mappings")

    routes: list[VideohubRoute] = []
    if isinstance(routes_raw, list):
        for r in routes_raw:
            rr = _coerce_route(r)
            if rr is not None:
                routes.append(rr)

    return VideohubPreset(id=preset_id, name=name, routes=routes), next_id


def load_presets(path: str | Path = DEFAULT_PRESETS_FILE) -> list[VideohubPreset]:
    p = Path(path)
    with _lock:
        try:
            if not p.exists():
                p.write_text("[]", encoding="utf-8")
                return []
            data = json.loads(p.read_text(encoding="utf-8") or "[]")
        except Exception:
            return []

    if not isinstance(data, list):
        return []

    # Determine next id from file
    max_id = 0
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            max_id = max(max_id, int(item.get("id")))

    next_id = max_id + 1

    out: list[VideohubPreset] = []
    changed = False
    for item in data:
        preset, next_id = _coerce_preset(item, next_id=next_id)
        if preset is None:
            changed = True
            continue
        out.append(preset)

        # Ensure normalized structure is persisted
        if not isinstance(item, dict) or ("id" not in item) or ("routes" not in item) or ("name" not in item):
            changed = True

    if changed:
        save_presets(out, path=p)

    return out


def save_presets(presets: list[VideohubPreset], path: str | Path = DEFAULT_PRESETS_FILE) -> None:
    p = Path(path)
    data = [pr.to_dict() for pr in (presets or [])]
    with _lock:
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
