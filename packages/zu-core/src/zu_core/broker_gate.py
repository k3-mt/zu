"""BrokerGate — route a HIGH-CONSEQUENCE capability use to the EXISTING human pause (§8).

The wiring reuses the shipped machinery and reinvents nothing. A broker tool call
(``use``) is gated by an ``InvocationGate`` whose ``check`` maps the broker's
high-consequence predicate — a use whose amount exceeds the grant's
``scope.requires_human_over``, or a new/off-allowlist payee — to the existing
verdict vocabulary: ``Verdict(severity=ESCALATE, kind="human")``. The loop already
turns a gate ESCALATE with ``kind=="human"`` into ``_GateEscalation`` →
``_pause_for_human``, which emits ``harness.approval.requested`` with the LITERAL
harness-held invocation args as ground truth (ZU-CD-1), binds the resolution by
approval_id + idempotency_key (ZU-CD-2), and on resume claims the key in the
ExecutionLedger BEFORE re-executing (consume-once, ZU-CD-6). So a large spend / new
payee pauses BEFORE the instrument operation, a human approves, then the broker use
runs exactly once — zero new pause code.

The high-consequence predicate is computed HARNESS-SIDE from the Grant and the
LITERAL call args the gate sees — NEVER from policy self-report. A compromised
policy that lies about the amount cannot game it: the gate reads the amount from
the same args the broker will execute, and the threshold from the harness-held
Grant. ``fail_closed_on_crash`` is set so a crashed gate guarding money fails
CLOSED regardless of the broker tool's self-declared tier (loop ZU-CORE-2).
"""

from __future__ import annotations

from typing import Any

from .ports import CapScope, Grant, RunContext, Severity, ToolCall, Verdict


class BrokerGate:
    """A thin ``InvocationGate`` (``zu_core.ports.InvocationGate``) over a credential
    broker tool. It fires ONLY for ``tool_name`` and only on a high-consequence use;
    otherwise it is inert (returns ``None`` — allow, the default).

    ``grants`` is a harness-held lookup of ``capability_id -> Grant`` (the broker's
    grant table, or a copy of it) so the threshold + allowlist come from the
    authority object, never from the policy. ``known_payees`` is an optional set of
    previously-seen recipients; a payee NOT in it is treated as high-consequence (a
    new recipient → human), matching the §8 "a new payee is the same high-consequence
    trip" rule."""

    name = "broker_gate"
    # Guard money: a crashed gate must fail CLOSED even if the broker tool was
    # authored tier-1 / no-capabilities (loop reads this in _gate_checkpoint).
    fail_closed_on_crash = True

    def __init__(
        self,
        grants: dict[str, Grant],
        *,
        tool_name: str = "broker_use",
        known_payees: set[str] | None = None,
    ) -> None:
        self._grants = grants
        self._tool_name = tool_name
        self._known_payees = known_payees

    def check(self, call: ToolCall, ctx: RunContext) -> Verdict | None:
        if call.name != self._tool_name:
            return None  # not the broker tool — inert
        args: dict[str, Any] = call.args or {}
        # The use request is carried in the call args (capability_id + operation +
        # args). Read the amount/payee from the LITERAL call the broker will execute.
        cap_id = args.get("capability_id")
        raw_inner = args.get("args")
        inner: dict[str, Any] = raw_inner if isinstance(raw_inner, dict) else args
        grant = self._grants.get(cap_id) if isinstance(cap_id, str) else None
        scope: CapScope | None = grant.scope if grant is not None else None
        amount = _as_float(inner.get("amount"))
        payee = inner.get("payee")
        # High-consequence #1: amount over the grant's requires_human_over threshold.
        if scope is not None and scope.requires_human_over is not None and amount is not None:
            if amount > scope.requires_human_over:
                return Verdict(
                    severity=Severity.ESCALATE,
                    detector=self.name,
                    kind="human",
                    detail=f"high-consequence spend {amount} > "
                    f"{scope.requires_human_over}; human approval required",
                )
        # High-consequence #2: a NEW payee/recipient (not previously seen). The
        # allowlist is harness-held; a payee outside the known set routes to human.
        if self._known_payees is not None and isinstance(payee, str) and payee not in self._known_payees:
            return Verdict(
                severity=Severity.ESCALATE,
                detector=self.name,
                kind="human",
                detail=f"new payee {payee!r}; human approval required",
            )
        return None  # in-limit, known payee — allow (the broker still enforces scope)


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = ["BrokerGate"]
