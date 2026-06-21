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
import threading
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any, NamedTuple

from .ports import INTERFACE_ATTR, INTERFACE_VERSION

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
    """A plugin that failed to enter the registry during discovery — either its
    entry point raised on import, or it was built against an incompatible
    interface major version (see ``IncompatibleInterfaceError``)."""

    kind: str
    name: str
    error: Exception


class IncompatibleInterfaceError(RuntimeError):
    """A plugin built against an interface major version this runtime does not
    provide for its port. Raised by ``register`` (decorator / config doors) and
    isolated-and-recorded by ``discover`` (the entry-point door)."""


def _declared_major(kind: str, obj: Any) -> int:
    """The interface major a plugin targets: its ``__zu_interface__`` attribute,
    or 1 (the original contract) when absent. Raises if it is present but not a
    usable integer — a malformed declaration is a refusal, not a silent pass."""
    raw = getattr(obj, INTERFACE_ATTR, 1)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise IncompatibleInterfaceError(
            f"plugin {getattr(obj, 'name', obj)!r} declares a non-integer "
            f"{INTERFACE_ATTR}={raw!r}; it must be an interface major version (int)."
        ) from None


def check_interface(kind: str, obj: Any) -> int:
    """Verify ``obj`` targets the runtime's interface major for ``kind`` and
    return that major. Raises ``IncompatibleInterfaceError`` on a mismatch with a
    message that names both versions and what to do."""
    host = INTERFACE_VERSION.get(kind)
    if host is None:
        raise KeyError(f"unknown plugin kind: {kind!r}")
    major = _declared_major(kind, obj)
    if major != host:
        raise IncompatibleInterfaceError(
            f"{kind[:-1]} {getattr(obj, 'name', obj)!r} targets Zu {kind} interface "
            f"v{major}, but this runtime provides v{host}. Refusing to load it — "
            f"install a build of it for interface v{host} (or upgrade/downgrade Zu)."
        )
    return major


class Registry:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {k: {} for k in GROUPS}
        self.failures: list[LoadFailure] = []
        # Guards mutation/iteration of the shared maps. ``REGISTRY`` is a
        # process-wide singleton that decorators, entry-point discovery, and
        # config assembly can all write — a plain ``threading.Lock`` keeps
        # register's check-then-set and ``names``' iteration consistent.
        self._lock = threading.Lock()

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
                    # The version gate runs here too: a plugin built against an
                    # incompatible interface major is isolated and recorded, the
                    # same as one that fails to import — discovery of everything
                    # else is unaffected.
                    self.register(kind, ep.name, obj)
                except Exception as exc:  # noqa: BLE001 - isolate any broken/incompatible plugin
                    self.failures.append(LoadFailure(kind=kind, name=ep.name, error=exc))
                    continue
        return self.failures

    def register(self, kind: str, name: str, obj: Any, *, replace: bool = False) -> None:
        if kind not in self._items:
            raise KeyError(f"unknown plugin kind: {kind!r}")
        # The interface-version gate (MLR §6): refuse a plugin built against an
        # incompatible major for this port, with a clear error, before it can
        # enter the registry and fail in confusing ways at call time.
        check_interface(kind, obj)
        with self._lock:
            existing = self._items[kind].get(name)
            if existing is not None and existing is not obj and not replace:
                # A name collision means one plugin is shadowing another (e.g. a
                # typosquat on a built-in like 'http_fetch'). Last-write-wins is
                # preserved for back-compat, but the collision must not be silent —
                # surface it so a caller can see what overrode what. ``replace=True``
                # is the opposite case — a deliberate swap (e.g. the offline harness
                # rebinding a tool to a fixture-backed double) — so it stays quiet.
                log.warning(
                    "plugin name collision on %s:%s — %r is overriding %r",
                    kind, name, obj, existing,
                )
            self._items[kind][name] = obj

    def get(self, kind: str, name: str) -> Any:
        return self._items[kind][name]

    def names(self, kind: str) -> list[str]:
        with self._lock:
            return sorted(self._items[kind])


REGISTRY = Registry()


def _deco(kind: str) -> Callable[[Any], Any]:
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
