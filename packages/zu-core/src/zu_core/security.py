"""The thin security contract the core knows about.

The core stays capability-free, but it owns one small security primitive: a
plugin (a tool, a guard) raises :class:`SecurityBlock` when it *contains* an
action — an SSRF/egress refusal, a denied capability — and the interpreter loop
turns that into a ``harness.defense.blocked`` event on the log. That makes a
contained adversarial attempt **visible by construction**: it is on the
append-only record, never a silent return. A concrete guard (e.g. zu-tools'
SSRF check) subclasses this; the loop only depends on the base shape.
"""

from __future__ import annotations

import os
from typing import Any

from .ports import declared_envelope

# Env var the sandboxed launcher sets *inside* the container, marking "this run is
# executing within the Zu sandbox — the container (default-DROP network + egress
# proxy + dropped caps) is the boundary, so tools may run." Absent on a bare host.
SANDBOX_ENV = "ZU_SANDBOXED"


class ContainmentRequired(RuntimeError):
    """Raised before a run starts when ``containment='required'`` but the process
    is not inside the Zu sandbox and a tool with off-box reach would otherwise run
    unguarded. Fail-closed: the runtime refuses rather than pretend to contain
    code it cannot contain in-process. ``tools`` names the offenders."""

    def __init__(self, tools: list[str]) -> None:
        self.tools = tools
        super().__init__(
            "containment='required' but this run is not inside the Zu sandbox "
            f"(${SANDBOX_ENV} unset), so these tools with off-box reach would run "
            f"unguarded: {', '.join(tools)}. Run via the sandboxed launcher "
            "(the whole agent in a container behind the egress proxy), or set "
            "containment='audit' to accept in-process execution."
        )


def _needs_containment(tool: Any) -> bool:
    """True if a tool has off-box reach the in-process runtime cannot bound: it
    declares any egress or capability, or it is tier >= 2 (a heavier capability
    unlocked by image, e.g. a real browser on a hostile page). Pure-CPU tools
    (empty envelope, tier 1) are host-safe and never need the sandbox."""
    env = declared_envelope(tool)
    if env["capabilities"] or env["egress"]:
        return True
    return int(getattr(tool, "tier", 1)) >= 2


def enforce_containment(policy: str, tools: dict[str, Any]) -> None:
    """The fail-closed floor. With ``policy='required'`` and the run NOT inside the
    sandbox, refuse if any active tool needs containment. Inside the sandbox
    (``ZU_SANDBOXED`` set) the container is the boundary, so this is a no-op; under
    ``policy='audit'`` (the default) it is also a no-op — declarations are merely
    logged. This is the difference between 'declared' and 'enforced'."""
    if policy != "required" or os.environ.get(SANDBOX_ENV):
        return
    offenders = sorted(name for name, tool in tools.items() if _needs_containment(tool))
    if offenders:
        raise ContainmentRequired(offenders)


class SecurityBlock(Exception):
    """A guard refused an action. ``kind`` categorises it (e.g. ``"ssrf"``),
    ``target`` is what was refused (a host/path), when known. Raising it (rather
    than returning a bare error) is what gets the block recorded as a defense."""

    kind = "security_block"

    def __init__(self, message: str, *, kind: str | None = None, target: str | None = None) -> None:
        super().__init__(message)
        if kind is not None:
            self.kind = kind
        self.target = target
