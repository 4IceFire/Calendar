from __future__ import annotations

from typing import Dict, Optional

from package.core import AppBase, register_app
from package.apps.videohub import storage
from package.apps.videohub.models import VideohubPreset, VideohubRoute

from videohub import get_videohub_client_from_config


class VideohubApp(AppBase):
    """Backend for VideoHub routing presets.

    This is intentionally lightweight (no long-running socket). It exists to:
    - load/save presets in a dedicated JSON file
    - apply a preset to the configured VideoHub

    The instance is registered with `package.core` so `webui.py` can
    `get_app('videohub')` and call methods.
    """

    def __init__(self) -> None:
        self._running = False

    def start(self, blocking: bool = True) -> None:
        # No long-running loop needed. Mark as running so status pages can show it.
        self._running = True

    def stop(self) -> None:
        self._running = False

    def status(self) -> Dict:
        return {"running": bool(self._running)}

    def _presets_file(self, cfg: dict) -> str:
        try:
            v = str(cfg.get("videohub_presets_file") or storage.DEFAULT_PRESETS_FILE).strip()
        except Exception:
            v = storage.DEFAULT_PRESETS_FILE
        return v or storage.DEFAULT_PRESETS_FILE

    def list_presets(self, cfg: dict) -> list[dict]:
        presets = storage.load_presets(self._presets_file(cfg))
        return [p.to_dict() for p in presets]

    def get_preset(self, cfg: dict, preset_id: int) -> Optional[VideohubPreset]:
        presets = storage.load_presets(self._presets_file(cfg))
        for p in presets:
            if int(p.id) == int(preset_id):
                return p
        return None

    def upsert_preset(self, cfg: dict, preset: dict) -> VideohubPreset:
        presets = storage.load_presets(self._presets_file(cfg))

        raw_id = preset.get("id")
        target_id = int(raw_id) if isinstance(raw_id, int) and raw_id > 0 else None

        name = str(preset.get("name") or "").strip()
        if not name:
            name = "Unnamed Preset"

        routes_in: list[VideohubRoute] = []
        raw_routes = preset.get("routes")
        if isinstance(raw_routes, list):
            for r in raw_routes:
                if not isinstance(r, dict):
                    continue
                try:
                    out_n = int(r.get("output"))
                    in_n = int(r.get("input"))
                except Exception:
                    continue
                if out_n <= 0 or in_n <= 0:
                    continue
                routes_in.append(VideohubRoute(output=out_n, input=in_n, monitoring=bool(r.get("monitoring", r.get("monitor", False)))))

        # Update if id exists, else create with next id
        if target_id is not None:
            for i, p in enumerate(presets):
                if int(p.id) == int(target_id):
                    updated = VideohubPreset(id=target_id, name=name, routes=routes_in)
                    presets[i] = updated
                    storage.save_presets(presets, self._presets_file(cfg))
                    return updated

        next_id = 1
        for p in presets:
            next_id = max(next_id, int(p.id) + 1)
        created = VideohubPreset(id=next_id, name=name, routes=routes_in)
        presets.append(created)
        storage.save_presets(presets, self._presets_file(cfg))
        return created

    def delete_preset(self, cfg: dict, preset_id: int) -> bool:
        presets = storage.load_presets(self._presets_file(cfg))
        before = len(presets)
        presets = [p for p in presets if int(p.id) != int(preset_id)]
        if len(presets) == before:
            return False
        storage.save_presets(presets, self._presets_file(cfg))
        return True

    def apply_preset(self, cfg: dict, preset_id: int) -> dict:
        preset = self.get_preset(cfg, preset_id)
        if preset is None:
            raise KeyError("preset not found")

        vh = get_videohub_client_from_config(cfg)
        if vh is None:
            raise ValueError("VideoHub not configured (set videohub_ip)")

        # group into monitoring vs primary blocks
        primary: list[tuple[int, int]] = []
        monitoring: list[tuple[int, int]] = []

        for r in preset.routes or []:
            # stored as 1-based -> protocol 0-based
            out_idx = int(r.output) - 1
            in_idx = int(r.input) - 1
            if out_idx < 0 or in_idx < 0:
                continue
            if bool(r.monitoring):
                monitoring.append((out_idx, in_idx))
            else:
                primary.append((out_idx, in_idx))

        if primary:
            vh.route_video_outputs(routes=primary, monitoring=False)
        if monitoring:
            vh.route_video_outputs(routes=monitoring, monitoring=True)

        return {
            "id": preset.id,
            "name": preset.name,
            "routes": len(preset.routes or []),
            "applied": True,
        }


def _factory() -> VideohubApp:
    return VideohubApp()


register_app("videohub", _factory)
