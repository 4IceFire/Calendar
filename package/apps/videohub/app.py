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

    def get_organization(self, cfg: dict) -> dict:
        return storage.load_organization(self._presets_file(cfg))

    def save_organization(self, cfg: dict, value: dict) -> dict:
        return storage.save_organization(value, self._presets_file(cfg))

    def get_preset(self, cfg: dict, preset_id: int) -> Optional[VideohubPreset]:
        presets = storage.load_presets(self._presets_file(cfg))
        for p in presets:
            if int(p.id) == int(preset_id):
                return p
        return None

    @storage.locked_mutation
    def upsert_preset(self, cfg: dict, preset: dict) -> VideohubPreset:
        presets = storage.load_presets(self._presets_file(cfg))

        raw_id = preset.get("id")
        target_id = int(raw_id) if isinstance(raw_id, int) and raw_id > 0 else None

        name = str(preset.get("name") or "").strip()
        if not name:
            raise ValueError("Preset name is required")

        routes_in: list[VideohubRoute] = []
        raw_routes = preset.get("routes")
        if isinstance(raw_routes, list):
            for r in raw_routes:
                if not isinstance(r, dict):
                    continue
                route = storage._coerce_route(r)
                if route is not None:
                    routes_in.append(route)

        # Update if id exists, else create with next id
        if target_id is not None:
            for i, p in enumerate(presets):
                if int(p.id) == int(target_id):
                    if 'expected' in preset and preset['expected'] != p.to_dict():
                        raise storage.PresetConflict('This preset changed. Reload before editing; your draft is retained.')
                    if bool(getattr(p, 'locked', False)):
                        raise ValueError('preset is locked')
                    updated = VideohubPreset(id=target_id, name=name, routes=routes_in)
                    presets[i] = updated
                    storage.save_presets(presets, self._presets_file(cfg))
                    return updated

        if target_id is not None:
            raise storage.PresetConflict('This preset was deleted. Your draft is retained.')
        next_id = storage.next_preset_id(self._presets_file(cfg))
        for p in presets:
            next_id = max(next_id, int(p.id) + 1)
        created = VideohubPreset(id=next_id, name=name, routes=routes_in, locked=False)
        presets.append(created)
        storage.save_presets(presets, self._presets_file(cfg))
        return created

    @storage.locked_mutation
    def delete_preset(self, cfg: dict, preset_id: int) -> bool:
        presets = storage.load_presets(self._presets_file(cfg))
        found = None
        for p in presets:
            if int(p.id) == int(preset_id):
                found = p
                break
        if found is None:
            return False
        if bool(getattr(found, 'locked', False)):
            raise ValueError('preset is locked')

        before = len(presets)
        presets = [p for p in presets if int(p.id) != int(preset_id)]
        if len(presets) == before:
            return False
        storage.save_presets(presets, self._presets_file(cfg))
        return True

    @storage.locked_mutation
    def set_preset_locked(self, cfg: dict, preset_id: int, locked: bool) -> VideohubPreset:
        presets = storage.load_presets(self._presets_file(cfg))
        for i, p in enumerate(presets):
            if int(p.id) == int(preset_id):
                updated = VideohubPreset(
                    id=int(p.id),
                    name=str(p.name),
                    routes=list(p.routes or []),
                    locked=bool(locked),
                )
                presets[i] = updated
                storage.save_presets(presets, self._presets_file(cfg))
                return updated
        raise KeyError('preset not found')

    def apply_preset(self, cfg: dict, preset_id: int) -> dict:
        preset = self.get_preset(cfg, preset_id)
        if preset is None:
            raise KeyError("preset not found")

        if any(getattr(r, 'routing_preset', None) is not None for r in preset.routes):
            raise ValueError('Image-bearing VideoHub presets require an explicit browser Apply Preset; automation is unsupported.')

        return self._apply_routes(cfg, preset)

    def _apply_routes(self, cfg: dict, preset: VideohubPreset) -> dict:
        """Apply a captured numeric-only snapshot; caller owns the reservation."""
        if any(getattr(r, 'routing_preset', None) is not None for r in preset.routes):
            raise ValueError('Image references cannot run as numeric routes.')
        # group into monitoring vs primary blocks
        primary: list[tuple[int, int]] = []
        monitoring: list[tuple[int, int]] = []

        for r in preset.routes or []:
            if r.input is None:
                # Ignore means no write (including monitoring), not a snapshot.
                continue
            # stored as 1-based -> protocol 0-based
            out_idx = int(r.output) - 1
            in_idx = int(r.input) - 1
            if out_idx < 0 or in_idx < 0:
                continue
            if bool(r.monitoring):
                monitoring.append((out_idx, in_idx))
            else:
                primary.append((out_idx, in_idx))

        # All-ignore presets succeed without a device or any hardware access.
        # Retain the legacy configuration check for empty presets.
        vh = None
        if primary or monitoring or not preset.routes:
            vh = get_videohub_client_from_config(cfg)
            if vh is None:
                raise ValueError("VideoHub not configured (set videohub_ip)")

        if primary:
            vh.route_video_outputs(routes=primary, monitoring=False)
        if monitoring:
            vh.route_video_outputs(routes=monitoring, monitoring=True)

        # A TCP send only confirms that the command left TDeck.  Read primary
        # routing back once so a successful API response means the VideoHub
        # actually applied the complete preset.  One state dump verifies all
        # outputs without multiplying device traffic per route.
        if primary:
            state = vh.get_state(fallback_count=40)
            routing = state.get("routing") if isinstance(state, dict) else None
            failed = []
            for output_idx, input_idx in primary:
                actual = None
                if isinstance(routing, list) and output_idx < len(routing):
                    try:
                        actual = int(routing[output_idx]) - 1
                    except Exception:
                        actual = None
                if actual != input_idx:
                    failed.append({
                        "output": output_idx + 1,
                        "expectedInput": input_idx + 1,
                        "actualInput": actual + 1 if actual is not None else None,
                    })
            if failed:
                raise RuntimeError(
                    f"VideoHub did not confirm {len(failed)} preset route(s): {failed}"
                )

        return {
            "id": preset.id,
            "name": preset.name,
            "routes": len(primary) + len(monitoring),
            "ignored": sum(r.input is None for r in preset.routes or []),
            "applied": True,
        }


def _factory() -> VideohubApp:
    return VideohubApp()


register_app("videohub", _factory)
