from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class VideohubRoute:
    """A single route mapping.

    Stored as 1-based numbers for user convenience.
    Converted to 0-based when sent to the VideoHub protocol.
    """

    output: int
    input: int
    monitoring: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "output": int(self.output),
            "input": int(self.input),
        }
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
