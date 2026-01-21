"""VideoHub integration package.

Contains:
- `client.py`: compatibility wrapper for `videohub.py`
- `app.py`: AppBase-compatible backend for routing presets
- `storage.py`: JSON persistence for presets
"""

# Import for side-effect registration with package.core
from package.apps.videohub import app  # noqa: F401

__all__ = ["client", "app", "storage", "models"]
