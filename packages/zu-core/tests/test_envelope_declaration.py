"""ZU-CORE-5 — a tool must DECLARE its capability envelope (#48).

``declared_envelope`` reads a missing ``capabilities``/``egress`` as least
privilege (empty), so an undeclared off-box tool reads as host-safe and slips
past ``enforce_containment(policy='required', ...)`` to run in-process UNGUARDED
(fail-OPEN). The fix is a registration-time gate that fails loud in strict mode
and warns otherwise — the defensive reader stays defensive. These proofs are
$0: two fakes, no model, no network.
"""

from __future__ import annotations

import logging

import pytest

from zu_core.registry import MissingEnvelopeError, Registry
from zu_core.security import (
    SANDBOX_ENV,
    ContainmentRequired,
    _needs_containment,
    enforce_containment,
)


class _Reaching:
    """A genuinely off-box tool that DELIBERATELY omits its envelope — the #48
    shape: no ``capabilities``, no ``egress``."""

    name = "reach"
    tier = 1

    async def __call__(self, *args: object, **kwargs: object) -> str:
        return "reached"


class _PureCPU:
    """The host-safe sibling: identical, but with an EXPLICIT empty envelope —
    a deliberate least-privilege declaration (frozenset present ⇒ not missing)."""

    name = "pure"
    tier = 1
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    async def __call__(self, *args: object, **kwargs: object) -> str:
        return "pure"


def test_strict_mode_rejects_undeclared_envelope_and_admits_explicit_empty() -> None:
    """Strict registration refuses the undeclared tool (naming both missing
    fields) and admits the one that declares an explicit empty envelope."""
    reg = Registry(strict_envelope=True)

    with pytest.raises(MissingEnvelopeError) as excinfo:
        reg.register("tools", "reach", _Reaching)
    assert excinfo.value.tool == "reach"
    assert set(excinfo.value.missing) == {"capabilities", "egress"}

    # An explicit frozenset() IS a declaration — it enters cleanly.
    reg.register("tools", "pure", _PureCPU)
    assert reg.get("tools", "pure") is _PureCPU


def test_fail_open_is_closed_only_once_the_envelope_is_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the fail-OPEN: undeclared, the off-box tool reads as host-safe and
    ``containment='required'`` does NOT flag it on a bare host. Declared with a
    real capability, the same tool DOES make containment refuse."""
    monkeypatch.delenv(SANDBOX_ENV, raising=False)  # ensure a bare host

    bare = _Reaching()
    assert _needs_containment(bare) is False
    # The fail-OPEN: undeclared off-box reach runs unguarded — no refusal.
    enforce_containment("required", {"reach": bare})

    class _Declared(_Reaching):
        capabilities: frozenset[str] = frozenset({"net"})
        egress: frozenset[str] = frozenset()

    declared = _Declared()
    assert _needs_containment(declared) is True
    with pytest.raises(ContainmentRequired):
        enforce_containment("required", {"reach": declared})


def test_default_registry_warns_and_stays_backward_compatible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default (strict off) keeps back-compat: the undeclared tool registers,
    but emits a warning naming the tool and the missing field(s)."""
    reg = Registry()
    with caplog.at_level(logging.WARNING):
        reg.register("tools", "reach", _Reaching)

    assert reg.get("tools", "reach") is _Reaching
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "reach" in msg
    assert "capabilities" in msg
    assert "egress" in msg
