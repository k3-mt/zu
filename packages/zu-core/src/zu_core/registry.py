"""The one registry the loop reads.

Plugins enter it three ways, in increasing distance from the core:
  1. installed packages, via entry points  (built-ins and user pip packages)
  2. in-process, via a decorator            (quick local work, no packaging)
  3. by reference in config                 (build step 8 — import path)
All three resolve into the same registry.

``REGISTRY`` is the process-wide default registry. The decorators
(``@zu.tool`` etc.), ``zu plugins``, and ``run_task`` (when no registry is
passed) all operate on this one instance, so a decorator-registered plugin is
visible to the loop and the CLI without any extra wiring. Pass an explicit
``Registry`` to isolate (the tests do this); otherwise the default is shared.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any, NamedTuple

log = logging.getLogger("zu.registry")

GROUPS = {
    "providers": "zu.providers",
    "tools": "zu.tools",
    "detectors": "zu.detectors",
    "validators": "zu.validators",
    "backends": "zu.backends",
    "sinks": "zu.sinks",
}


class LoadFailure(NamedTuple):
    """A plugin whose entry point failed to import/load during discovery."""

    kind: str
    name: str
    error: Exception


class Registry:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {k: {} for k in GROUPS}
        self.failures: list[LoadFailure] = []

    def discover(self) -> list[LoadFailure]:
        """Load every pip-installed plugin declared via entry points.

        Discovery is resilient: a single broken plugin — a third-party package
        whose entry point raises on import — must not take down discovery of
        everything else (the same principle the event bus applies to a crashing
        subscriber). Each failure is isolated, recorded on ``self.failures``,
        and returned, so a caller can surface it instead of crashing.
        """
        self.failures = []
        for kind, group in GROUPS.items():
            for ep in entry_points(group=group):
                try:
                    obj = ep.load()
                except Exception as exc:  # noqa: BLE001 - isolate any broken plugin
                    self.failures.append(LoadFailure(kind=kind, name=ep.name, error=exc))
                    continue
                self.register(kind, ep.name, obj)
        return self.failures

    def register(self, kind: str, name: str, obj: Any) -> None:
        if kind not in self._items:
            raise KeyError(f"unknown plugin kind: {kind!r}")
        existing = self._items[kind].get(name)
        if existing is not None and existing is not obj:
            # A name collision means one plugin is shadowing another (e.g. a
            # typosquat on a built-in like 'http_fetch'). Last-write-wins is
            # preserved for back-compat, but the collision must not be silent —
            # surface it so a caller can see what overrode what.
            log.warning(
                "plugin name collision on %s:%s — %r is overriding %r",
                kind, name, obj, existing,
            )
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
