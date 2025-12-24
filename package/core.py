from typing import Callable, Dict, Optional


class AppBase:
    """Minimal app interface for registration and lifecycle management."""

    def start(self, blocking: bool = True) -> None:
        raise NotImplementedError()

    def stop(self) -> None:
        raise NotImplementedError()

    def status(self) -> Dict:
        return {}


_registry: Dict[str, Callable[[], AppBase]] = {}


def register_app(name: str, factory: Callable[[], AppBase]) -> None:
    _registry[name] = factory


def list_apps() -> Dict[str, Callable[[], AppBase]]:
    return dict(_registry)


def get_app(name: str) -> Optional[AppBase]:
    f = _registry.get(name)
    if f:
        return f()
    return None
