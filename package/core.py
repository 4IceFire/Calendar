from typing import Callable, Dict, Optional
import threading


class AppBase:
    """Minimal app interface for registration and lifecycle management."""

    def start(self, blocking: bool = True) -> None:
        raise NotImplementedError()

    def stop(self) -> None:
        raise NotImplementedError()

    def status(self) -> Dict:
        return {}


_registry: Dict[str, Callable[[], AppBase]] = {}
# singleton instances cache (name -> AppBase)
_instances: Dict[str, AppBase] = {}
# lock to protect instance creation
_instances_lock = threading.Lock()


def register_app(name: str, factory: Callable[[], AppBase]) -> None:
    _registry[name] = factory


def list_apps() -> Dict[str, Callable[[], AppBase]]:
    return dict(_registry)


def get_app(name: str) -> Optional[AppBase]:
    """Return a singleton instance for the named app.

    This ensures only one instance of a given app is created and used
    throughout the process. The factory registered with `register_app`
    is used on first access to construct the instance.
    """
    # fast-path: existing instance
    inst = _instances.get(name)
    if inst is not None:
        return inst

    f = _registry.get(name)
    if f is None:
        return None

    with _instances_lock:
        # double-check after acquiring lock
        inst = _instances.get(name)
        if inst is not None:
            return inst
        try:
            inst = f()
        except Exception:
            return None
        _instances[name] = inst
        return inst
