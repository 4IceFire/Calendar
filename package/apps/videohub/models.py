from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class VideohubRoute:
    """A single route mapping.

    Stored as 1-based numbers for user convenience; input=None means
    Keep Current Routing unless routing_preset is an image-only reference.
    Converted to 0-based when sent to the VideoHub protocol.
    """

    output: int
    input: int | None
    monitoring: bool = False
    routing_preset: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "output": int(self.output),
            "input": None if self.input is None else int(self.input),
        }
        if self.routing_preset is not None:
            out.pop('input')
            out['routing_preset'] = self.routing_preset
        # Keep storage compact: only persist when a route targets the monitoring table.
        if bool(self.monitoring):
            out["monitoring"] = True
        return out


@dataclass
class VideohubPreset:
    id: int
    name: str
    routes: list[VideohubRoute]
    locked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "name": str(self.name),
            "routes": [r.to_dict() for r in (self.routes or [])],
            "locked": bool(self.locked),
        }
