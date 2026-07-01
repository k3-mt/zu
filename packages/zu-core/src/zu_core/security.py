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
#
# TRUST ASSUMPTION (F81 — READ THIS). ``ZU_SANDBOXED`` is a plain environment
# variable with NO cryptographic attestation: any process that can set its own
# environment can set ``ZU_SANDBOXED=1`` and thereby claim to be contained, so the
# ``containment='required'`` floor ULTIMATELY RESTS ON A FORGEABLE SIGNAL. This is
# a deliberate, documented residual: real, unforgeable containment attestation
# (a TPM/TEE quote, a signed launcher token verified against a harness-held key, a
# kernel-attested namespace) belongs to the sandbox BACKEND (ZU-NET-5 carries the
# measurement seam) and cannot be manufactured honestly inside zu-core, which
# imports nothing but pydantic. What zu-core CAN do — and now does — is:
#   1. CORROBORATE the env signal with the OTHER harness-controlled signals the
#      REAL launcher always sets alongside it (the egress-proxy env + the internal
#      network name — see zu_cli.sandbox). A bare forged ``ZU_SANDBOXED=1`` with no
#      proxy configured does not look like a launcher-established boundary; the
#      mismatch is ``corroborated=False`` in ``containment_basis``.
#   2. RECORD the basis on the audit log (``harness.containment.attested``) so a
#      forged env is at least DETECTABLE/recorded after the fact — a run claiming
#      containment on an uncorroborated signal is a reviewable anomaly, not a
#      silent bypass. Corroboration RAISES the forgery bar (an attacker must now
#      also fake the proxy wiring) but does NOT close it; only backend attestation
#      does. The floor stays fail-CLOSED regardless — this only hardens the
#      contained→"trusted" direction.
SANDBOX_ENV = "ZU_SANDBOXED"

# The other environment signals the REAL sandboxed launcher (zu_cli.sandbox) sets
# ALONGSIDE ``ZU_SANDBOXED`` when it establishes the boundary: the egress proxy the
# container must route through, and the internal default-DROP network name. Their
# presence corroborates that a launcher — not a bare ``export ZU_SANDBOXED=1`` —
# established the environment. Read defensively; a launcher change adds a signal
# here (each is necessary-not-sufficient — corroboration, never proof).
_CONTAINMENT_COORROBORATING_ENV = ("HTTPS_PROXY", "ZU_SANDBOX_NETWORK")


def containment_basis(policy: str) -> dict[str, Any]:
    """The auditable BASIS for a run's containment judgement (F81).

    Returns ``{"policy", "sandboxed", "corroborated", "signals"}``: whether the
    (forgeable) ``ZU_SANDBOXED`` signal is set, whether the harness-controlled
    structural signals the real launcher sets alongside it AGREE (so a bare forged
    env is distinguishable from a launcher-established boundary), and the per-signal
    breakdown. Pure (reads the environment only); the loop emits it as
    ``harness.containment.attested`` at run start so the basis is on the log and a
    forged env is at least recorded/detectable. This does NOT make the signal
    unforgeable — see the SANDBOX_ENV trust-assumption note — it makes the basis
    transparent and the anomaly auditable."""
    sandboxed = bool(os.environ.get(SANDBOX_ENV))
    signals = {name: bool(os.environ.get(name)) for name in _CONTAINMENT_COORROBORATING_ENV}
    # Corroborated only when the sandbox signal is set AND every structural signal
    # the real launcher establishes is present — a bare ``ZU_SANDBOXED=1`` with no
    # proxy wiring is uncorroborated (a forged-signal candidate).
    corroborated = sandboxed and all(signals.values())
    return {
        "policy": policy,
        "sandboxed": sandboxed,
        "corroborated": corroborated,
        "signals": signals,
    }


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
