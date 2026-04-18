from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_cache_lock = threading.RLock()
_json_cache: dict[str, tuple[tuple[int, int] | None, Any]] = {}


def _cache_key(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except Exception:
        return str(Path(path))


def _file_snapshot(path: str | Path) -> tuple[int, int] | None:
    try:
        st = Path(path).stat()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    try:
        return (int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return None


def remember_json(path: str | Path, value: Any, *, snapshot: tuple[int, int] | None = None) -> None:
    key = _cache_key(path)
    if snapshot is None:
        snapshot = _file_snapshot(path)
    with _cache_lock:
        _json_cache[key] = (snapshot, copy.deepcopy(value))


def invalidate_json(path: str | Path) -> None:
    key = _cache_key(path)
    with _cache_lock:
        _json_cache.pop(key, None)


def read_json(
    path: str | Path,
    *,
    default_factory: Callable[[], T] = lambda: None,  # type: ignore[assignment]
    create_if_missing: bool = False,
    transform: Callable[[Any], T | tuple[T, bool]] | None = None,
) -> tuple[T, bool]:
    """Read and optionally normalize a JSON file with mtime-based caching."""

    p = Path(path)
    key = _cache_key(p)
    snapshot = _file_snapshot(p)

    with _cache_lock:
        cached = _json_cache.get(key)
        if snapshot is not None and cached is not None and cached[0] == snapshot:
            return copy.deepcopy(cached[1]), False

    if not p.exists():
        value = default_factory()
        if create_if_missing:
            try:
                write_json(p, value)
            except Exception:
                pass
        else:
            remember_json(p, value, snapshot=None)
        return copy.deepcopy(value), False

    try:
        raw = json.loads(p.read_text(encoding="utf-8") or "null")
    except Exception:
        value = default_factory()
        remember_json(p, value, snapshot=_file_snapshot(p))
        return copy.deepcopy(value), False

    changed = False
    value: T
    try:
        if transform is None:
            value = raw  # type: ignore[assignment]
        else:
            transformed = transform(raw)
            if isinstance(transformed, tuple) and len(transformed) == 2 and isinstance(transformed[1], bool):
                value = transformed[0]
                changed = transformed[1]
            else:
                value = transformed  # type: ignore[assignment]
    except Exception:
        value = default_factory()
        changed = True

    if not changed:
        remember_json(p, value, snapshot=_file_snapshot(p))
    return copy.deepcopy(value), changed


def write_json(path: str | Path, data: Any) -> bool:
    """Atomically write JSON and refresh the cache."""

    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, p)
        except Exception:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            raise
    except Exception:
        return False

    remember_json(p, data, snapshot=_file_snapshot(p))
    return True
