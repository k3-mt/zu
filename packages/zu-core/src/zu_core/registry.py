"""The one registry the loop reads.

Plugins enter it three ways, in increasing distance from the core:
  1. installed packages, via entry points  (built-ins and user pip packages)
  2. in-process, via a decorator            (quick local work, no packaging)
  3. by reference in config                 (build step 8 — import path)
All three resolve into the same registry.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

GROUPS = {
    "providers": "zu.providers",
    "tools": "zu.tools",
    "detectors": "zu.detectors",
    "validators": "zu.validators",
    "backends": "zu.backends",
    "sinks": "zu.sinks",
}


class Registry:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {k: {} for k in GROUPS}

    def discover(self) -> None:
        """Load every pip-installed plugin declared via entry points."""
        for kind, group in GROUPS.items():
            for ep in entry_points(group=group):
                self._items[kind][ep.name] = ep.load()

    def register(self, kind: str, name: str, obj: Any) -> None:
        if kind not in self._items:
            raise KeyError(f"unknown plugin kind: {kind!r}")
        self._items[kind][name] = obj

    def get(self, kind: str, name: str) -> Any:
        return self._items[kind][name]

    def names(self, kind: str) -> list[str]:
        return sorted(self._items[kind])


REGISTRY = Registry()


def _deco(kind: str):
    """In-process registration: @zu.tool / @zu.detector / ..."""

    def wrap(obj: Any) -> Any:
        REGISTRY.register(kind, getattr(obj, "name", obj.__name__), obj)
        return obj

    return wrap


tool = _deco("tools")
detector = _deco("detectors")
validator = _deco("validators")
provider = _deco("providers")
backend = _deco("backends")
sink = _deco("sinks")
