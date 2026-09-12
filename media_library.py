"""Local, validated still images; no web application or hardware dependencies.

All metadata and image mutations for a root are serialized within this process.
Callers must authorize both metadata and ``path()`` responses before serving them.
Upload bytes are discarded after decoding; stored images contain no EXIF or XMP.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_DECODED_BYTES = 160_000_000
MAX_FRAME_PIXELS = 4096 * 2160
THUMBNAIL_SIZE = (480, 270)
MAX_NAME_LENGTH = 120
SUPPORTED_FORMATS = ("JPEG", "PNG", "WEBP", "HEIF")

register_heif_opener(thumbnails=False)

_locks_guard = threading.Lock()
_root_locks: dict[str, threading.RLock] = {}
_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


def _validate_id(image_id: str) -> str:
    if not isinstance(image_id, str) or not _ID_PATTERN.fullmatch(image_id):
        raise KeyError("Image not found")
    return image_id


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Image name must be text")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("Image name is required")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError(f"Image name must be {MAX_NAME_LENGTH} characters or fewer")
    return cleaned


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace a complete file, retaining the previous file on write failure."""
    fd, temporary = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _decode(content: bytes) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=SUPPORTED_FORMATS) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image exceeds the 40 megapixel limit")
                # Account for higher bit-depth decoder output as well as RGBA.
                bytes_per_sample = 4 if source.mode in ("I", "F") else (2 if "16" in source.mode else 1)
                decoded_size = width * height * max(4, len(source.getbands()) * bytes_per_sample)
                if decoded_size > MAX_DECODED_BYTES:
                    raise ValueError("Decoded image exceeds the memory limit")
                if getattr(source, "n_frames", 1) != 1 or getattr(source, "is_animated", False):
                    raise ValueError("Animated or multiple-image files are not supported; upload one still image")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                try:
                    profile = oriented.info.get("icc_profile")
                    if profile:
                        if len(profile) > 2 * 1024 * 1024:
                            raise ValueError("Image color profile is too large")
                        try:
                            normalized = ImageCms.profileToProfile(
                                oriented, io.BytesIO(profile), ImageCms.createProfile("sRGB"), outputMode="RGBA"
                            )
                        except (ImageCms.PyCMSError, OSError, TypeError, ValueError) as exc:
                            raise ValueError("Image color profile cannot be read") from exc
                    else:
                        normalized = oriented.convert("RGBA")
                finally:
                    oriented.close()
                # New pixel data plus an empty info dictionary prevent EXIF,
                # location, comments, profiles and XMP from reaching stored PNGs.
                normalized.info.clear()
                return normalized
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Image exceeds the pixel limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError) as exc:
        raise ValueError("Upload a valid JPEG, PNG, WebP, HEIC or HEIF still image") from exc


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class MediaLibrary:
    """A durable catalogue under an explicit root directory.

    Invalid uploads/edits raise ValueError, missing/invalid IDs raise KeyError,
    and filesystem or corrupt-index failures raise OSError. Returned metadata
    never includes a filesystem path. ``size_bytes`` is the stored PNG size.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        with _locks_guard:
            self._lock = _root_locks.setdefault(os.path.normcase(str(self.root)), threading.RLock())
        self._index = self.root / "index.json"
        with self._lock:
            (self.root / "images").mkdir(parents=True, exist_ok=True)
            (self.root / "thumbnails").mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, dict]:
        if not self._index.exists():
            return {}
        try:
            raw = json.loads(self._index.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("images"), list):
                raise ValueError("Invalid index structure")
            result = {}
            for item in raw["images"]:
                if not isinstance(item, dict):
                    raise ValueError("Invalid image entry")
                image_id = _validate_id(item.get("id"))
                if image_id in result:
                    raise ValueError("Duplicate image entry")
                if not isinstance(item.get("preset"), bool):
                    raise ValueError("Invalid preset value")
                for field in ("width", "height", "size_bytes"):
                    if type(item.get(field)) is not int or item[field] <= 0:
                        raise ValueError("Invalid image dimensions or size")
                if item["width"] * item["height"] > MAX_IMAGE_PIXELS:
                    raise ValueError("Invalid image dimensions")
                if not isinstance(item.get("created_at"), str):
                    raise ValueError("Invalid image timestamp")
                result[image_id] = {
                    "id": image_id, "name": _name(item.get("name")),
                    "width": item["width"], "height": item["height"],
                    "size_bytes": item["size_bytes"], "created_at": item["created_at"],
                    "preset": item["preset"],
                }
            return result
        except (ValueError, KeyError, TypeError) as exc:
            raise OSError("Media library index cannot be read; restore its backup before making changes") from exc

    def _save(self, entries: dict[str, dict]) -> None:
        payload = {"version": 1, "images": list(entries.values())}
        _atomic_write(self._index, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    def _asset_path(self, image_id: str, thumbnail: bool = False) -> Path:
        folder = self.root / ("thumbnails" if thumbnail else "images")
        path = folder / f"{_validate_id(image_id)}.png"
        if not path.resolve().is_relative_to(self.root):
            raise OSError("Media library image path is outside its storage directory")
        return path

    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._read().values(), key=lambda item: (
                not item["preset"], item["name"].casefold(), item["created_at"], item["id"]
            ))

    def get(self, image_id: str) -> dict:
        with self._lock:
            entry = self._read().get(_validate_id(image_id))
            if entry is None:
                raise KeyError("Image not found")
            return dict(entry)

    def upload(self, stream: BinaryIO, filename: str, name: str = "") -> dict:
        filename = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        label = _name(name or Path(filename).stem or "Uploaded image")
        with self._lock:
            content = stream.read(MAX_UPLOAD_BYTES + 1)
            if not isinstance(content, bytes) or not content:
                raise ValueError("Image file is empty or unreadable")
            if len(content) > MAX_UPLOAD_BYTES:
                raise ValueError("Image exceeds the 20 MB upload limit")
            with _decode(content) as image:
                width, height = image.size
                encoded = _png(image)
                with image.copy() as thumbnail:
                    thumbnail.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
                    preview = _png(thumbnail)
            entries = self._read()
            image_id = uuid.uuid4().hex
            entry = {
                "id": image_id, "name": label, "width": width, "height": height,
                "size_bytes": len(encoded), "created_at": datetime.now(timezone.utc).isoformat(),
                "preset": False,
            }
            original_path = self._asset_path(image_id)
            thumbnail_path = self._asset_path(image_id, True)
            try:
                _atomic_write(original_path, encoded)
                _atomic_write(thumbnail_path, preview)
                entries[image_id] = entry
                self._save(entries)
            except Exception:
                original_path.unlink(missing_ok=True)
                thumbnail_path.unlink(missing_ok=True)
                raise
            return dict(entry)

    def update(self, image_id: str, name: str, preset: bool = False) -> dict:
        label = _name(name)
        if not isinstance(preset, bool):
            raise ValueError("Preset must be true or false")
        with self._lock:
            entries = self._read()
            image_id = _validate_id(image_id)
            if image_id not in entries:
                raise KeyError("Image not found")
            entries[image_id].update(name=label, preset=preset)
            self._save(entries)
            return dict(entries[image_id])

    def delete(self, image_id: str) -> dict:
        with self._lock:
            entries = self._read()
            image_id = _validate_id(image_id)
            if image_id not in entries:
                raise KeyError("Image not found")
            entry = entries.pop(image_id)
            moved = []
            try:
                # Keep recoverable originals until the index commit succeeds.
                for thumbnail in (False, True):
                    path = self._asset_path(image_id, thumbnail)
                    if path.exists():
                        temporary = path.with_suffix(f".delete-{uuid.uuid4().hex}.tmp")
                        os.replace(path, temporary)
                        moved.append((path, temporary))
                self._save(entries)
            except Exception:
                for path, temporary in reversed(moved):
                    os.replace(temporary, path)
                raise
            for _, temporary in moved:
                temporary.unlink(missing_ok=True)
            return dict(entry)

    def path(self, image_id: str, thumbnail: bool = False) -> Path:
        with self._lock:
            self.get(image_id)
            path = self._asset_path(image_id, thumbnail)
            if not path.is_file():
                raise KeyError("Image file not found")
            return path

    def frame(self, image_id: str, width: int, height: int) -> bytes:
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ValueError("Frame width and height must be positive integers")
        if width * height > MAX_FRAME_PIXELS:
            raise ValueError("Frame exceeds the maximum supported dimensions")
        with self._lock:
            # Retain the lock until pixels are read, so deletion cannot race a load.
            with Image.open(self.path(image_id)) as source:
                with ImageOps.contain(source, (width, height), Image.Resampling.LANCZOS) as scaled:
                    with Image.new("RGBA", (width, height), (0, 0, 0, 255)) as canvas:
                        canvas.alpha_composite(scaled.convert("RGBA"), ((width - scaled.width) // 2, (height - scaled.height) // 2))
                        result = canvas.tobytes()
            if len(result) != width * height * 4:
                raise OSError("Prepared image has an unexpected byte size")
            return result
