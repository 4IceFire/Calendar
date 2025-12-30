"""App package.

Import app modules for side-effect registration with `package.core`.
"""

# Import apps so they register themselves via `register_app()`
from package.apps import calendar  # noqa: F401

__all__ = ["calendar"]
