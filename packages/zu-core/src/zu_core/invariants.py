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

# The default deadline for an EVENTUALLY invariant: any terminal event marks the
# interaction/run complete, so a success postcondition that never held by then is
# a real VIOLATION. Kept generic (no pattern-specific knowledge) and additive.
_DEADLINE_TYPES: frozenset[str] = frozenset({ev.TASK_TERMINAL, ev.TASK_COMPLETED})


class InvariantKind(str, Enum):
    PRE = "precondition"  # must hold at the step BEFORE applies_to takes effect
    POST = "postcondition"  # must hold after applies_to appears
    THROUGHOUT = "throughout"  # must hold at every step
    # A liveness-by-deadline property (an LTL "eventually F p, bounded by a
    # deadline"): the predicate need NOT hold on early/in-progress steps — it must
    # have held AT LEAST ONCE by the time the deadline event appears. The Monitor
    # is inert (OK) until the deadline, and only VIOLATES at the deadline if the
    # predicate never held. This is the correct shape for a pattern's SUCCESS
    # criterion (a postcondition that is, by definition, absent until the
    # interaction completes) — distinct from THROUGHOUT ("must hold at every
    # step"), which would wrongly fire on the pre-interaction surface.
    EVENTUALLY = "eventually"


class PredicateKind(str, Enum):
    BUDGET_CAP = "budget_cap"
    DOMAIN_ALLOWLIST = "domain_allowlist"
    REQUIRED_FIELD = "required_field"
    # Did an expected post-state appear on a surface/recognition event? The
    # predicate a Pattern's success/failure criterion compiles to (§5 / ZU-RAIL-9):
    # it folds data.surface.captured / data.pattern.recognized events and checks
    # whether the expected handle/label/archetype is present (or, negated, absent).
    SURFACE_CONTAINS = "surface_contains"


class Predicate(BaseModel):
    """A frozen typed predicate — the v1 vocabulary. ``params`` carries the
    predicate-specific data the consumer declares:

      * budget_cap:       {"metric": "tool_calls"|"events", "limit": int}
      * domain_allowlist: {"event_type": str, "field": <dotted path into payload>,
                           "allow": list[str]}
      * required_field:   {"event_type": str, "field": str}
      * surface_contains: {"event_type": str, one of "handle"|"label"|"archetype",
                           "negate": bool (optional, default False),
                           "require_present": bool (optional, default False — when
                           True, absence-of-evidence is unsatisfied, not vacuously
                           true; the liveness/EVENTUALLY reading)}
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
    # Which event the pre/post/eventually is anchored on; ``None`` ⇒ every step
    # (the natural reading for THROUGHOUT). For POST it is a tool name (the anchor
    # whose appearance starts checking). For EVENTUALLY it is the DEADLINE event
    # TYPE by which the predicate must have held at least once; ``None`` ⇒ the
    # default deadline (any terminal event — ``TASK_TERMINAL``/``TASK_COMPLETED``
    # — marking the interaction/run complete).
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


def _surface_tokens(payload: dict, key: str) -> set[str]:
    """The set of string tokens a surface/recognition event exposes under one of
    ``handle`` | ``label`` | ``archetype`` — the minimal vocabulary
    SURFACE_CONTAINS matches against. Reads defensively: a surface event carries
    ``handles`` (and may carry ``labels``/``affordances`` dicts); a recognition
    event carries ``matched_handles`` and ``archetype``. Anything unrecognised
    yields the empty set (nothing matched)."""
    out: set[str] = set()
    if key == "handle":
        for src in ("handles", "matched_handles"):
            v = payload.get(src)
            if isinstance(v, (list, tuple)):
                out.update(str(x) for x in v)
    elif key == "label":
        v = payload.get("labels")
        if isinstance(v, (list, tuple)):
            out.update(str(x) for x in v)
        affs = payload.get("affordances")
        if isinstance(affs, (list, tuple)):
            out.update(str(a["label"]) for a in affs if isinstance(a, dict) and "label" in a)
    elif key == "archetype":
        v = payload.get("archetype")
        if isinstance(v, str):
            out.add(v)
    return out


def _eval_surface_contains(events: Sequence[Any], params: dict) -> bool:
    """True iff the expected post-state appeared on some matching surface event.

    Folds events of ``event_type`` and checks whether the expected token (one of
    ``handle`` | ``label`` | ``archetype``) is present on ANY of them. With
    ``negate=True`` the property is INVERSION: it holds iff the token is ABSENT
    from every matching event (e.g. "the cookie banner's accept button is gone").
    The predicate HOLDS (returns True) when no matching event has yet appeared —
    a postcondition is only meaningful once its anchor has fired (the Invariant's
    ``applies_to``/POST gating decides when to check), so absence-of-evidence is
    not a violation here."""
    event_type = str(params.get("event_type", ""))
    negate = bool(params.get("negate", False))
    # ``require_present``: when set, absence-of-evidence is NOT vacuously true — the
    # token must actually have appeared. EVENTUALLY/liveness success criteria set
    # this so "no surface ever showed the success state" is unsatisfied (and so
    # VIOLATES at the deadline), rather than passing vacuously.
    require_present = bool(params.get("require_present", False))
    key = next((k for k in ("handle", "label", "archetype") if k in params), None)
    if key is None:
        return True  # nothing to check (a malformed spec is inert, never a false VIOLATION)
    expected = str(params[key])
    matching = [e for e in events if getattr(e, "type", None) == event_type]
    if not matching:
        # No evidence yet. For a negated (absence) property this holds; for a
        # plain postcondition it is vacuously true UNLESS the caller demands the
        # token actually appear (require_present), the liveness reading.
        return True if negate else not require_present
    present = any(expected in _surface_tokens(_payload(e), key) for e in matching)
    return (not present) if negate else present


_PREDICATE_EVALUATORS: dict[PredicateKind, Callable[[Sequence[Any], dict], bool]] = {
    PredicateKind.BUDGET_CAP: _eval_budget_cap,
    PredicateKind.DOMAIN_ALLOWLIST: _eval_domain_allowlist,
    PredicateKind.REQUIRED_FIELD: _eval_required_field,
    PredicateKind.SURFACE_CONTAINS: _eval_surface_contains,
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

    def _deadline_seen(self, events: Sequence[Any]) -> bool:
        """For EVENTUALLY: has the deadline that bounds the liveness arrived?

        ``applies_to`` names the deadline event TYPE; ``None`` ⇒ any terminal event
        (the interaction/run is complete). Before the deadline the property is not
        yet falsifiable — the predicate may still come true — so the Monitor stays
        inert."""
        deadline = self.invariant.applies_to
        if deadline is None:
            return any(getattr(e, "type", None) in _DEADLINE_TYPES for e in events)
        return any(getattr(e, "type", None) == deadline for e in events)

    def evaluate(self, ctx: RunContext) -> MonitorVerdict | None:
        events = ctx.events
        inv = self.invariant
        # POST: only check once the anchoring event has appeared. THROUGHOUT/PRE
        # check on every fold (PRE anchors a future effect, so a broken predicate
        # is reportable as soon as it is broken). A missing anchor ⇒ nothing yet.
        if inv.kind == InvariantKind.POST and not self._anchor_seen(events):
            return None
        # EVENTUALLY (liveness-by-deadline): satisfied the instant the predicate
        # has held at least once; otherwise inert until the deadline event arrives,
        # and only THEN — if the predicate still never held — a VIOLATION. This is
        # what makes a SUCCESS criterion correct: pre-interaction surfaces lacking
        # the success state do NOT fire; only a never-arrived success state by the
        # deadline does.
        if inv.kind == InvariantKind.EVENTUALLY:
            if predicate_holds(inv.predicate, events):
                return None
            if not self._deadline_seen(events):
                return None
            return MonitorVerdict(
                monitor=self.name,
                state=MonitorState.VIOLATION,
                detail=f"invariant {inv.name!r} (eventually) never satisfied by deadline: "
                f"{inv.predicate.kind.value}",
                step=len(events) - 1 if events else None,
            )
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
