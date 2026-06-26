"""The capability surface, made explicit and machine-checkable (issue #30).

Zu advertises plugin *kinds* via ``INTERFACE_VERSION`` and also ships powerful
*library* packages (``zu-shadow``, ``zu-patterns``, ``zu-providers``) that are not
plugin-discoverable at all. From the outside, "what does Zu provide here" was
tribal knowledge — a downstream integrator reimplemented shadow/recognizer/
providers locally because they were undiscoverable, missing the redaction,
promotion-gate and content-free-invariant safety those Zu packages already carry.

This module answers, at runtime, *what is actually installed in THIS environment*:

  * ``provenance()`` — the running ``zu-core`` version + the interface major it
    provides for every plugin kind (a deployed system is self-describing).
  * ``capabilities()`` — every ``INTERFACE_VERSION`` kind mapped to its
    implementing package + canonical symbol + interface major + an installed flag.
  * ``library_surface()`` — the headline import-only packages (no entry points):
    what each is for and the symbols to import, with an installed flag.

Pure stdlib + the existing ``importlib.metadata`` the registry already uses; no
new dependency, no import of the optional packages themselves.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, entry_points
from importlib.metadata import version as _dist_version
from importlib.util import find_spec
from typing import NamedTuple

from .ports import INTERFACE_VERSION

__zu_spec__ = "§6"  # the plugin interface contract / capability surface


def _core_version() -> str:
    """The installed ``zu-core`` version, or a clear sentinel if run from a
    source tree with no installed metadata."""
    try:
        return _dist_version("zu-core")
    except PackageNotFoundError:  # pragma: no cover - only when run uninstalled
        return "0+unknown"


#: The installed ``zu-core`` version — ``zu_core.__version__``. A process can
#: report its own Zu binding instead of a consumer guessing from a git pin.
__version__ = _core_version()


def provenance() -> dict[str, object]:
    """What this process is running: ``{version, interface_majors}``.

    ``interface_majors`` is the per-kind interface major this runtime provides
    (the contract a plugin must match to load). Self-describing by design — a
    live system can report its Zu binding without a git pin lookup."""
    return {"version": __version__, "interface_majors": dict(INTERFACE_VERSION)}


class Capability(NamedTuple):
    """One plugin kind, reconciled against what is actually installed here."""

    kind: str
    interface_major: int
    group: str
    #: (entry-point name, ``module:symbol``, distribution) for each implementation
    #: discoverable in this environment.
    implementations: tuple[tuple[str, str, str], ...]
    installed: bool  # is at least one implementation discoverable here?


def capabilities() -> list[Capability]:
    """Every ``INTERFACE_VERSION`` kind → its entry-point group, interface major,
    the implementations discoverable in THIS environment (package + symbol), and
    whether the kind is installed at all. The manifest the install actually keeps,
    not just the one the core promises."""
    from .registry import GROUPS  # local: registry imports ports; avoid a cycle

    out: list[Capability] = []
    for kind, major in INTERFACE_VERSION.items():
        group = GROUPS.get(kind, f"zu.{kind}")
        impls = sorted(
            (ep.name, ep.value, (ep.dist.name if ep.dist else "?"))
            for ep in entry_points(group=group)
        )
        out.append(Capability(kind, major, group, tuple(impls), bool(impls)))
    return out


class LibrarySurface(NamedTuple):
    """A headline *library* package — imported directly, not discovered as a
    plugin — surfaced so it stops being invisible."""

    dist: str
    top_module: str
    purpose: str
    imports: tuple[str, ...]  # the main symbols to import
    installed: bool  # importable in this environment?


# The import-only packages a downstream is most likely to reimplement by hand if
# it can't find them (issue #30, Part B). Each: what it is for + the symbols to
# import. ``installed`` is computed against the live environment.
_LIBRARY: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "zu-shadow",
        "zu_shadow",
        "record a task once → synthesize a resilient path → run it live and "
        "generalise; with capture-time redaction (§9) and a promotion gate.",
        (
            "zu_shadow.Recorder",
            "zu_shadow.Synthesizer",
            "zu_shadow.live_executor.run_live",
            "zu_shadow.verify_and_gate",
        ),
    ),
    (
        "zu-patterns",
        "zu_patterns",
        "recognize surface archetypes (§5), cross-run site memory as an FSM, and "
        "a live model-predictive-control loop — all first-class, tested primitives.",
        (
            "zu_patterns.recognize",
            "zu_patterns.fsm_from_events",
            "zu_patterns.mpc_run",
        ),
    ),
    (
        "zu-providers",
        "zu_providers",
        "model providers, including any OpenAI-compatible endpoint (OpenRouter, "
        "Together, vLLM, …) — no need to hand-roll an HTTP client.",
        (
            "zu_providers.openai_compatible:OpenAICompatibleProvider",
            "zu_providers.anthropic:AnthropicProvider",
            "zu_providers.scripted:ScriptedProvider",
        ),
    ),
    (
        "zu-checks",
        "zu_checks",
        "detectors (bot-walls, CAPTCHAs, human gates, action-surface blindness) "
        "and validators (schema, grounding) over the surface.",
        ("zu_checks.detectors", "zu_checks.validators"),
    ),
)


def library_surface() -> list[LibrarySurface]:
    """The headline import-only packages, each with its purpose, the symbols to
    import, and whether it is installed (importable) in this environment."""
    return [
        LibrarySurface(dist, mod, purpose, imports, find_spec(mod) is not None)
        for dist, mod, purpose, imports in _LIBRARY
    ]
