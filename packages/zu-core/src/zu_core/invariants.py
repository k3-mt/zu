"""Declarable invariants over the event log, compiled to Monitors (§1.7 spec layer).

An agent.yaml can carry invariants as DATA — a budget cap, a domain allowlist, a
required-field check — and this module compiles each into a :class:`Monitor`
(``zu_core.ports.Monitor``) whose violation is detected over the log by the loop's
monitor checkpoint (ZU-RAIL-6). The limits/allowlists are the CONSUMER's data, never
a magic constant baked into Zu.

Pure data + pure evaluators: pydantic + stdlib only, no model, no I/O. The
``Predicate`` is a tagged union keyed by ``kind``; adding an LTL predicate later is
one new enum value + one evaluator entry, with ``compile_invariant``/``compile_spec``
unchanged — and an LTL→Monitor compiler emits objects satisfying the SAME Monitor
shape, so the registry/loop wiring is untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from . import events as ev
from .ports import MonitorState, MonitorVerdict, RunContext


class InvariantKind(str, Enum):
    PRE = "precondition"  # must hold at the step BEFORE applies_to takes effect
    POST = "postcondition"  # must hold after applies_to appears
    THROUGHOUT = "throughout"  # must hold at every step


class PredicateKind(str, Enum):
    BUDGET_CAP = "budget_cap"
    DOMAIN_ALLOWLIST = "domain_allowlist"
    REQUIRED_FIELD = "required_field"


class Predicate(BaseModel):
    """A frozen typed predicate — the v1 vocabulary. ``params`` carries the
    predicate-specific data the consumer declares:

      * budget_cap:       {"metric": "tool_calls"|"events", "limit": int}
      * domain_allowlist: {"event_type": str, "field": <dotted path into payload>,
                           "allow": list[str]}
      * required_field:   {"event_type": str, "field": str}
    """

    model_config = {"frozen": True}

    kind: PredicateKind
    params: dict = Field(default_factory=dict)


class Invariant(BaseModel):
    """A declarable invariant: a named predicate, anchored by ``kind`` and
    optionally a triggering ``applies_to`` event/tool name."""

    name: str
    kind: InvariantKind
    predicate: Predicate
    # Which event the pre/post is anchored on (e.g. a tool name); ``None`` ⇒ every
    # step (the natural reading for THROUGHOUT).
    applies_to: str | None = None


# --- pure predicate evaluators over an event Sequence ---------------------
#
# Each returns ``True`` when the predicate HOLDS over ``events``, ``False`` when it
# is broken. No model, no I/O — a fold over the typed event stream.


def _payload(e: Any) -> dict:
    p = getattr(e, "payload", None)
    return p if isinstance(p, dict) else {}


def _dotted(d: dict, path: str) -> Any:
    """Read a dotted path into a nested dict; ``_MISSING`` if any segment absent."""
    cur: Any = d
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return _MISSING
        cur = cur[seg]
    return cur


_MISSING = object()


def _eval_budget_cap(events: Sequence[Any], params: dict) -> bool:
    """True iff the counted metric stays at or below the declared limit."""
    metric = str(params.get("metric", "tool_calls"))
    limit = int(params.get("limit", 0))
    if metric == "events":
        count = len(events)
    else:  # tool_calls (the default)
        count = sum(1 for e in events if getattr(e, "type", None) == ev.TOOL_INVOKED)
    return count <= limit


def _eval_domain_allowlist(events: Sequence[Any], params: dict) -> bool:
    """True iff every matching event's field value is in the allowlist."""
    event_type = str(params.get("event_type", ""))
    field = str(params.get("field", ""))
    allow = set(params.get("allow", []))
    for e in events:
        if getattr(e, "type", None) != event_type:
            continue
        value = _dotted(_payload(e), field)
        if value is _MISSING:
            continue  # nothing to check on this event
        if value not in allow:
            return False
    return True


def _eval_required_field(events: Sequence[Any], params: dict) -> bool:
    """True iff every matching event carries the required field (non-missing)."""
    event_type = str(params.get("event_type", ""))
    field = str(params.get("field", ""))
    for e in events:
        if getattr(e, "type", None) != event_type:
            continue
        if _dotted(_payload(e), field) is _MISSING:
            return False
    return True


_PREDICATE_EVALUATORS: dict[PredicateKind, Callable[[Sequence[Any], dict], bool]] = {
    PredicateKind.BUDGET_CAP: _eval_budget_cap,
    PredicateKind.DOMAIN_ALLOWLIST: _eval_domain_allowlist,
    PredicateKind.REQUIRED_FIELD: _eval_required_field,
}


def predicate_holds(predicate: Predicate, events: Sequence[Any]) -> bool:
    """Evaluate ``predicate`` over ``events`` (pure). True ⇒ holds."""
    return _PREDICATE_EVALUATORS[predicate.kind](events, predicate.params)


# --- the bridge: invariant -> Monitor (the #2 -> #1 seam) -----------------


@dataclass(frozen=True)
class _CompiledInvariant:
    """A concrete Monitor (satisfies ``zu_core.ports.Monitor`` structurally) whose
    ``evaluate`` folds ``ctx.events`` through the invariant's predicate and reports
    a VIOLATION when the property is broken, else ``None`` (inert)."""

    name: str
    invariant: Invariant

    def _anchor_seen(self, events: Sequence[Any]) -> bool:
        anchor = self.invariant.applies_to
        if anchor is None:
            return True
        for e in events:
            if getattr(e, "type", None) == ev.TOOL_INVOKED and _payload(e).get("tool") == anchor:
                return True
        return False

    def evaluate(self, ctx: RunContext) -> MonitorVerdict | None:
        events = ctx.events
        inv = self.invariant
        # POST: only check once the anchoring event has appeared. THROUGHOUT/PRE
        # check on every fold (PRE anchors a future effect, so a broken predicate
        # is reportable as soon as it is broken). A missing anchor ⇒ nothing yet.
        if inv.kind == InvariantKind.POST and not self._anchor_seen(events):
            return None
        if predicate_holds(inv.predicate, events):
            return None
        return MonitorVerdict(
            monitor=self.name,
            state=MonitorState.VIOLATION,
            detail=f"invariant {inv.name!r} ({inv.kind.value}) broken: "
            f"{inv.predicate.kind.value}",
            step=len(events) - 1 if events else None,
        )


def compile_invariant(inv: Invariant) -> _CompiledInvariant:
    """Compile a declared :class:`Invariant` into a Monitor (ZU-RAIL-6).

    The returned object satisfies the ``Monitor`` structural Protocol (it carries a
    ``name`` and an ``evaluate(ctx)``), so it registers under the ``monitors`` kind
    and runs in the loop's monitor checkpoint with zero further wiring.
    """
    return _CompiledInvariant(name=inv.name, invariant=inv)


def compile_spec(invs: list[Invariant]) -> list[_CompiledInvariant]:
    """Compile a list of invariants into a list of Monitors."""
    return [compile_invariant(i) for i in invs]
