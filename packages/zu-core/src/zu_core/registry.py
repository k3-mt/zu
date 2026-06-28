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

# The entry-point group for each built-in kind. ``GROUPS`` is kept as the
# module-level back-compat export (``from zu_core.registry import GROUPS``); it
# is the *seed* for a Registry's live kind map, not the source of truth — a
# consumer adds kinds at runtime via ``Registry.register_kind`` (ZU-EXT-1)
# without editing this dict.
GROUPS = {
    "providers": "zu.providers",
    "tools": "zu.tools",
    "detectors": "zu.detectors",
    "validators": "zu.validators",
    "backends": "zu.backends",
    "sinks": "zu.sinks",
    "policies": "zu.policies",
    "triggers": "zu.triggers",
    # Discovery/trust kinds — the ModelProvider siblings for "find the site" (#81)
    # and "who is safe to transact with" (#84). Ports in zu_core.ports; reference
    # impls are plugins, discovered like any other kind.
    "retrieval_providers": "zu.retrieval_providers",
    "reputation_providers": "zu.reputation_providers",
    # Security-conformance kinds (ports in zu_core.ports; impls are plugins):
    "gates": "zu.gates",
    "channels": "zu.channels",
    "workload_identity": "zu.workload_identity",
    "egress_enforcement": "zu.egress_enforcement",
    "replay_arbiters": "zu.replay_arbiters",
    "monitors": "zu.monitors",
    # The pattern port (§5): a recognizer over the Action Surface + the rail
    # invariants it emits. Read-only plugins, discovered like any other kind.
    "patterns": "zu.patterns",
    # The credential broker (§8): the scoped/revocable/audited capability to USE
    # an instrument without the policy ever holding the secret. The broker holds
    # the secret harness-side; an Instrument adapter (a real issuer/vault) plugs in
    # behind it. Discovered like any other kind.
    "credential_brokers": "zu.credential_brokers",
}

# The reserved entry-point group a package uses to declare a brand-new kind
# (ZU-EXT-1): each entry loads to a ``KindSpec`` (or any object with
# ``name``/``group``/optional ``interface_major``). ``discover`` loads these
# first, so a pip-installed package can add a port type with zero core edits.
KINDS_GROUP = "zu.kinds"


class KindSpec(NamedTuple):
    """A registrable plugin kind: its name, its entry-point group, and the
    interface major version the runtime provides for it."""

    name: str
    group: str
    interface_major: int = 1


# The built-in kinds, the seed every Registry starts from. The interface major
# comes from ``INTERFACE_VERSION`` (defaulting to 1) so the two stay in lockstep.
_BUILTIN_KINDS: dict[str, KindSpec] = {
    name: KindSpec(name, group, INTERFACE_VERSION.get(name, 1))
    for name, group in GROUPS.items()
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
    def __init__(self, kinds: dict[str, KindSpec] | None = None) -> None:
        # The live kind map (ZU-EXT-1). Seeded from the built-ins; a consumer
        # adds to it with ``register_kind`` without editing the core. ``GROUPS``
        # / ``INTERFACE_VERSION`` are only the defaults that seed this.
        self._kinds: dict[str, KindSpec] = dict(kinds or _BUILTIN_KINDS)
        self._items: dict[str, dict[str, Any]] = {k: {} for k in self._kinds}
        self.failures: list[LoadFailure] = []
        # Guards mutation/iteration of the shared maps. ``REGISTRY`` is a
        # process-wide singleton that decorators, entry-point discovery, and
        # config assembly can all write — a plain ``threading.Lock`` keeps
        # register's check-then-set and ``names``' iteration consistent.
        self._lock = threading.Lock()

    def register_kind(self, name: str, group: str, *, interface_major: int = 1) -> None:
        """Register a NEW port kind at runtime (ZU-EXT-1) — e.g. a consumer's
        ``CredentialBroker`` — so its implementations are discoverable and
        loadable through the one registry the loop reads, WITHOUT editing the
        core. Idempotent for an identical (name, group); a conflicting group for
        an existing name is a refusal."""
        with self._lock:
            existing = self._kinds.get(name)
            if existing is not None and existing.group != group:
                raise ValueError(
                    f"kind {name!r} already registered to group {existing.group!r}; "
                    f"refusing to rebind it to {group!r}"
                )
            self._kinds.setdefault(name, KindSpec(name, group, interface_major))
            self._items.setdefault(name, {})

    def kinds(self) -> list[str]:
        """Every registered kind — built-in plus consumer-registered."""
        with self._lock:
            return sorted(self._kinds)

    def _check_interface(self, kind: str, obj: Any) -> int:
        """Instance-aware interface gate: verifies ``obj`` targets the major this
        registry provides for ``kind`` (which may be a consumer-registered kind
        the module-level ``INTERFACE_VERSION`` knows nothing about)."""
        spec = self._kinds.get(kind)
        if spec is None:
            raise KeyError(f"unknown plugin kind: {kind!r}")
        major = _declared_major(kind, obj)
        if major != spec.interface_major:
            raise IncompatibleInterfaceError(
                f"{kind} plugin {getattr(obj, 'name', obj)!r} targets interface "
                f"v{major}, but this runtime provides v{spec.interface_major}. "
                f"Refusing to load it — install a build for interface "
                f"v{spec.interface_major} (or upgrade/downgrade the provider)."
            )
        return major

    def discover(self) -> list[LoadFailure]:
        """Load every pip-installed plugin declared via entry points.

        New *kinds* are loaded first (the ``zu.kinds`` group, ZU-EXT-1) so a
        package can add a port type and its implementations in one install; then
        every plugin for every known kind.

        Discovery is resilient: a single broken plugin — a third-party package
        whose entry point raises on import — must not take down discovery of
        everything else (the same principle the event bus applies to a crashing
        subscriber). Each failure is isolated, recorded on ``self.failures``,
        and returned, so a caller can surface it instead of crashing.
        """
        self.failures = []
        # 1) consumer-declared new kinds, before we enumerate their groups.
        for ep in entry_points(group=KINDS_GROUP):
            try:
                spec = ep.load()
                self.register_kind(
                    spec.name, spec.group, interface_major=getattr(spec, "interface_major", 1)
                )
            except Exception as exc:  # noqa: BLE001 - isolate any broken kind declaration
                self.failures.append(LoadFailure(kind="kinds", name=ep.name, error=exc))
                continue
        # 2) plugins for every known kind (snapshot the items so a registered
        #    kind added above is included).
        for kind, spec in list(self._kinds.items()):
            for ep in entry_points(group=spec.group):
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

    def register(self, kind: str, name: str, obj: Any) -> None:
        if kind not in self._items:
            raise KeyError(f"unknown plugin kind: {kind!r}")
        # The interface-version gate (MLR §6): refuse a plugin built against an
        # incompatible major for this port, with a clear error, before it can
        # enter the registry and fail in confusing ways at call time.
        self._check_interface(kind, obj)
        with self._lock:
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
        with self._lock:
            return sorted(self._items[kind])


REGISTRY = Registry()


def kind_decorator(kind: str) -> Callable[[Any], Any]:
    """In-process registration for any kind: @zu.tool / @zu.detector / … and,
    for a consumer-registered kind, ``broker = kind_decorator("credential_brokers")``.
    The named decorators below are one-liners over this single code path."""

    def wrap(obj: Any) -> Any:
        REGISTRY.register(kind, getattr(obj, "name", obj.__name__), obj)
        return obj

    return wrap


# Back-compat alias for the original private name.
_deco = kind_decorator

tool = kind_decorator("tools")
detector = kind_decorator("detectors")
validator = kind_decorator("validators")
provider = kind_decorator("providers")
backend = kind_decorator("backends")
sink = kind_decorator("sinks")
policy = kind_decorator("policies")
trigger = kind_decorator("triggers")
gate = kind_decorator("gates")
retrieval_provider = kind_decorator("retrieval_providers")
reputation_provider = kind_decorator("reputation_providers")
channel = kind_decorator("channels")
arbiter = kind_decorator("replay_arbiters")
monitor = kind_decorator("monitors")
pattern = kind_decorator("patterns")
credential_broker = kind_decorator("credential_brokers")
