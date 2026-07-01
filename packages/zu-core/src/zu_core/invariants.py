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

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
    # Spend velocity over a sliding window (§8): sums the ``amount`` on
    # harness.capability.used events whose timestamp falls within the last
    # ``window_s`` and HOLDS iff that sum stays at or below ``limit``. This is the
    # velocity/anomaly rail a consumer declares in agent.yaml as DATA (ZU-RAIL-6);
    # it compiles to a Monitor exactly like BUDGET_CAP and joins the existing
    # VIOLATION→TERMINAL escalation path. Distinct from a Grant's cumulative_limit
    # (a per-grant hard stop at the broker via incr_if_below): this is a
    # cross-use, time-windowed anomaly rail over the whole log.
    SPEND_VELOCITY = "spend_velocity"


class Predicate(BaseModel):
    """A frozen typed predicate — the v1 vocabulary. ``params`` carries the
    predicate-specific data the consumer declares:

      * budget_cap:       {"metric": "tool_calls"|"events", "limit": int}
      * domain_allowlist: {"event_type": str, "field": <dotted path into payload>,
                           "allow": list[str], "wildcard": bool (optional, default
                           False — when True, ``field`` holds a URL/host and ``allow``
                           is matched as wildcard host patterns via zu_core.hosts, the
                           SAME match the pre-exec navigation gate uses so they can't
                           drift)}
      * required_field:   {"event_type": str, "field": str}
      * surface_contains: {"event_type": str, one of
                           "handle"|"label"|"state"|"archetype" (a SINGLE literal)
                           OR its plural any-of list
                           "handles"|"labels"|"states"|"archetypes" (satisfied if
                           ANY token matches — #46). Label/archetype matching is
                           normalized + word-boundary-aware (#57). A "state" key
                           plus a single "handle" asserts "handle H reached that
                           state" (e.g. selected — #39).
                           "negate": bool (optional, default False),
                           "require_present": bool (optional, default False — when
                           True, absence-of-evidence is unsatisfied, not vacuously
                           true; the liveness/EVENTUALLY reading)}
      * spend_velocity:   {"window_s": int, "limit": float} — summed spend on
                           ``harness.capability.used`` within the last window_s must
                           stay ≤ limit (the §8 velocity/anomaly rail).
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
    """True iff every matching event's field value is in the allowlist.

    Two modes, selected by ``wildcard``:
      * ``wildcard`` absent/False (the original) — exact set membership; the field
        value must equal one of ``allow`` literally.
      * ``wildcard: True`` — the value is treated as a URL (or a bare host) and its
        HOST is matched against ``allow`` as wildcard host patterns via the shared
        ``zu_core.hosts`` helper (``*.example.com``). This is the mode the
        declarative ``allowed_domains`` block feeds, so the pre-execution gate and
        this audit invariant use the SAME match and cannot drift.
    """
    event_type = str(params.get("event_type", ""))
    field = str(params.get("field", ""))
    wildcard = bool(params.get("wildcard", False))
    if wildcard:
        from .hosts import host_matches_any, normalize_host

        allow_patterns = list(params.get("allow", []))
        for e in events:
            if getattr(e, "type", None) != event_type:
                continue
            value = _dotted(_payload(e), field)
            if value is _MISSING or not isinstance(value, str):
                continue  # nothing to check on this event
            host = _host_of(value)
            if not host:
                continue  # no host to judge (e.g. a non-URL value)
            if not host_matches_any(normalize_host(host), allow_patterns):
                return False
        return True
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


def _host_of(value: str) -> str:
    """The host of a URL string, or the value itself if it is already a bare host.
    Defensive — returns ``""`` when neither shape yields a host."""
    from urllib.parse import urlsplit

    try:
        host = urlsplit(value).hostname
    except ValueError:
        host = None
    if host:
        return host
    # already a bare host (no scheme) — accept it as-is if it has no path/space
    v = value.strip()
    if v and "/" not in v and " " not in v:
        return v
    return ""


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


# The keys SURFACE_CONTAINS can fold, in the priority order it selects them. A
# ``state`` key (#39) lets a rail express "a control reached STATE selected" — the
# content-free "a control became selected" success criterion — folding an
# affordance's ``states`` carried on the surface event, not page text. ``state``
# is checked BEFORE ``handle`` because a state rail also carries a ``handle`` (to
# scope the state check to the acted control), and that handle must NOT be mistaken
# for a "handle appeared" check.
_SURFACE_KEYS = ("state", "handle", "label", "archetype")

# The alphanumeric "words" of a normalized label — the unit a short/symbol token
# matches by whole-word equality. Replicated (not imported) here to keep zu-core
# SDK-free and free of any zu-patterns dependency; the SAME logic lives in
# zu_patterns._match.token_matches (issue #57), so the rail and the pattern layer
# agree on word-boundary matching without a cross-package import.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _norm(s: str) -> str:
    """Lowercase, collapse whitespace — the canonical label form (mirrors
    ``zu_patterns._match.norm``)."""
    return " ".join(s.lower().split())


def _token_matches(text_norm: str, token: str) -> bool:
    """True iff ``token`` matches the already-normalized ``text_norm`` on WORD
    BOUNDARIES (issue #57/#46). A multi-word token matches a consecutive word-run
    ("order confirmed" ⊂ "Order confirmed!"); a pure-symbol token matches when it
    stands alone with non-word neighbours; a plain word matches only as a WHOLE word
    ("confirmation" does NOT match "Reconfirmation", "error" does NOT match a stray
    substring). This is the rail's matcher: a decoy label that merely CONTAINS the
    token as a substring never falsely satisfies the verify layer, while real
    casing/punctuation/synonym variants do. Word-boundary-aligned with
    ``zu_patterns._match.token_matches`` (which keeps raw substring for the longer
    button-label vocabulary used in affordance selection — a deliberately looser
    reading appropriate there; the rail is the strict, decoy-proof one)."""
    tok = _norm(token)
    if not tok:
        return False
    if not any(ch.isalnum() for ch in tok):
        # A pure symbol ("×", ">", "<"): present with non-word neighbours.
        return bool(re.search(r"(?<!\w)" + re.escape(tok) + r"(?!\w)", text_norm))
    # A word or multi-word phrase: match on word boundaries so it is a whole word /
    # a consecutive word-run, never glued inside a longer word.
    return bool(re.search(r"\b" + re.escape(tok) + r"\b", text_norm))


def _surface_tokens(payload: dict, key: str) -> set[str]:
    """The set of string tokens a surface/recognition event exposes under one of
    ``handle`` | ``label`` | ``state`` | ``archetype`` — the minimal vocabulary
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
    elif key == "state":
        # The union of every affordance's ``states`` on this surface — the raw
        # material for a "a control reached state X" (e.g. selected) rail (#39).
        affs = payload.get("affordances")
        if isinstance(affs, (list, tuple)):
            for a in affs:
                if isinstance(a, dict):
                    st = a.get("states")
                    if isinstance(st, (list, tuple)):
                        out.update(str(x) for x in st)
    elif key == "archetype":
        v = payload.get("archetype")
        if isinstance(v, str):
            out.add(v)
    return out


def _handle_in_state(payload: dict, handle: str, state: str) -> bool:
    """True iff the affordance ``handle`` on this surface event carries ``state``
    (case/space-normalized). The precise form of the #39 "handle H became STATE
    selected" success criterion — a per-handle state check, not a surface-wide
    "some control is selected"."""
    affs = payload.get("affordances")
    if not isinstance(affs, (list, tuple)):
        return False
    want = _norm(state)
    for a in affs:
        if isinstance(a, dict) and str(a.get("handle", "")) == handle:
            st = a.get("states")
            if isinstance(st, (list, tuple)) and any(_norm(str(x)) == want for x in st):
                return True
    return False


def _expected_tokens(params: dict, key: str) -> list[str]:
    """The any-of expected token list a SURFACE_CONTAINS rail folds for (#46).

    Accepts either a single ``params[key]`` literal OR a ``params[key + 's']``
    list (``labels``/``handles``/``states``/``archetypes``) — so a rail satisfied
    by ANY of a set of equivalent success/failure markers is expressible while the
    single-token form still works."""
    plural = key + "s"
    out: list[str] = []
    raw_plural = params.get(plural)
    if isinstance(raw_plural, (list, tuple)):
        out.extend(str(x) for x in raw_plural)
    if key in params:
        out.append(str(params[key]))
    return out


def _eval_surface_contains(events: Sequence[Any], params: dict) -> bool:
    """True iff the expected post-state appeared on some matching surface event.

    Folds events of ``event_type`` and checks whether ANY expected token (one of
    ``handle`` | ``label`` | ``state`` | ``archetype``, singular OR an any-of list
    via the plural key — #46) is present on ANY of them. Label/archetype matching
    is NORMALIZED and word-boundary-aware (#57): "Order confirmed"/"order
    confirmed!" satisfy the token "order confirmed", while a decoy that merely
    contains a short token as a substring does not. A ``state`` key + a single
    ``handle`` expresses "handle H reached state selected" (#39).

    With ``negate=True`` the property is INVERSION: it holds iff NO expected token
    is present on any matching event (e.g. "the cookie banner's accept button is
    gone"). The predicate HOLDS (returns True) when no matching event has yet
    appeared — a postcondition is only meaningful once its anchor has fired (the
    Invariant's ``applies_to``/POST gating decides when to check), so
    absence-of-evidence is not a violation here."""
    event_type = str(params.get("event_type", ""))
    negate = bool(params.get("negate", False))
    # ``require_present``: when set, absence-of-evidence is NOT vacuously true — the
    # token must actually have appeared. EVENTUALLY/liveness success criteria set
    # this so "no surface ever showed the success state" is unsatisfied (and so
    # VIOLATES at the deadline), rather than passing vacuously.
    require_present = bool(params.get("require_present", False))
    key = next(
        (k for k in _SURFACE_KEYS if k in params or (k + "s") in params),
        None,
    )
    if key is None:
        return True  # nothing to check (a malformed spec is inert, never a false VIOLATION)
    expected = _expected_tokens(params, key)
    if not expected:
        return True  # malformed (key named but no value) — inert, never a false VIOLATION
    matching = [e for e in events if getattr(e, "type", None) == event_type]
    if not matching:
        # No evidence yet. For a negated (absence) property this holds; for a
        # plain postcondition it is vacuously true UNLESS the caller demands the
        # token actually appear (require_present), the liveness reading.
        return True if negate else not require_present
    present = any(_event_has_token(_payload(e), key, expected, params) for e in matching)
    return (not present) if negate else present


def _event_has_token(payload: dict, key: str, expected: list[str], params: dict) -> bool:
    """True iff this surface event exposes ANY of the ``expected`` tokens under
    ``key`` — the any-of, normalized, word-boundary match (#46/#57/#39)."""
    if key == "state":
        # "handle H reached state selected": if a specific handle is named, check
        # THAT handle's states; otherwise any control on the surface in the state.
        handle = params.get("handle")
        if isinstance(handle, str):
            return any(_handle_in_state(payload, handle, st) for st in expected)
        toks = {_norm(t) for t in _surface_tokens(payload, "state")}
        return any(_norm(st) in toks for st in expected)
    if key in ("handle", "archetype"):
        # Opaque identifiers — exact (normalized) set membership, no substring.
        toks = {_norm(t) for t in _surface_tokens(payload, key)}
        return any(_norm(t) in toks for t in expected)
    # label — normalized, word-boundary-aware substring/whole-word matching (#57),
    # so real English variants ("Order confirmed", "Payment received") satisfy.
    labels = _surface_tokens(payload, key)
    for lbl in labels:
        lbl_norm = _norm(lbl)
        if any(_token_matches(lbl_norm, t) for t in expected):
            return True
    return False


def _eval_spend_velocity(events: Sequence[Any], params: dict) -> bool:
    """True iff summed spend on ``harness.capability.used`` events within the last
    ``window_s`` seconds stays at or below ``limit`` (§8 velocity rail).

    Sums ``payload["outcome"]["captured"]`` (the instrument's captured amount) —
    falling back to ``payload["args"]["amount"]`` / ``payload["amount"]`` if a
    consumer's instrument reports the amount elsewhere. The window is anchored at
    the latest event's timestamp (``now`` when the log has none), so the property
    is a pure fold over the timestamps already on the chain — no wall clock, so it
    is deterministic on replay."""
    window_s = float(params.get("window_s", 0))
    limit = float(params.get("limit", 0))
    used = [e for e in events if getattr(e, "type", None) == ev.CAPABILITY_USED]
    if not used:
        return True
    # Anchor the window at the most recent event timestamp (deterministic on replay
    # — never the wall clock), falling back to now only for a tsless synthetic log.
    stamps = [getattr(e, "ts", None) for e in events]
    real = [t for t in stamps if isinstance(t, datetime)]
    anchor = max(real) if real else datetime.now(UTC)
    total = 0.0
    for e in used:
        ts = getattr(e, "ts", None)
        if isinstance(ts, datetime) and (anchor - ts).total_seconds() > window_s:
            continue  # outside the window
        p = _payload(e)
        raw_outcome = p.get("outcome")
        outcome: dict = raw_outcome if isinstance(raw_outcome, dict) else {}
        amt = outcome.get("captured")
        if amt is None:
            raw_args = p.get("args")
            args: dict = raw_args if isinstance(raw_args, dict) else {}
            amt = args.get("amount", p.get("amount", 0))
        try:
            total += float(amt)
        except (TypeError, ValueError):
            continue
    return total <= limit


_PREDICATE_EVALUATORS: dict[PredicateKind, Callable[[Sequence[Any], dict], bool]] = {
    PredicateKind.BUDGET_CAP: _eval_budget_cap,
    PredicateKind.DOMAIN_ALLOWLIST: _eval_domain_allowlist,
    PredicateKind.REQUIRED_FIELD: _eval_required_field,
    PredicateKind.SURFACE_CONTAINS: _eval_surface_contains,
    PredicateKind.SPEND_VELOCITY: _eval_spend_velocity,
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
