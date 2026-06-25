"""The reversible-vs-committing action classifier — principled & generic.

It marks the boundary the guided search must not cross during LIVE exploration
(and the rail's commit boundary): a REVERSIBLE action is read-only/idempotent
(safe to explore), a COMMITTING action is side-effecting/irreversible (the
boundary). The discipline is **default-to-committing** — the safe rail behaviour
— whenever the signals are inconclusive. There is NO site-specific magic keyword
blocklist; the signals are principled and generic:

1. EXPLICIT rail annotation (``ctx.annotations["consequence"]``, ZU-RAIL-4 — a
   content-free consequence class) — authoritative when present.
2. HTTP method / idempotency when OBSERVABLE — RFC 7231 safe/idempotent
   semantics (GET/HEAD/OPTIONS ⇒ reversible; POST/PUT/PATCH/DELETE ⇒ committing),
   not site words.
3. Affordance SEMANTICS from role/op — generic interaction verbs (``fill``,
   ``read``, ``open`` ⇒ reversible-leaning; ``submit``, ``confirm``, ``pay``,
   ``delete`` ⇒ committing-leaning), not site words.
4. A small EXTENSIBLE prior set a pattern/plugin contributes (additive,
   community-extensible, never hardcoded into core).
5. DEFAULT: uncertain ⇒ COMMITTING.

Pure, deterministic, hand-testable. It never decides the task action; it only
classifies an action's consequence class for the planner and the rail.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum


class Commitment(str, Enum):
    REVERSIBLE = "reversible"  # read-only / idempotent: safe to explore live
    COMMITTING = "committing"  # side-effecting / irreversible: the boundary


@dataclass(frozen=True)
class Signal:
    """An extensible piece of evidence toward a verdict. ``weight`` is positive
    toward COMMITTING, negative toward REVERSIBLE."""

    name: str
    weight: float


@dataclass(frozen=True)
class ActionPrior:
    """A community-extensible prior a pattern/plugin contributes: when ``matcher``
    holds for an action's signals, contribute ``commitment`` with ``weight``. The
    classifier sums priors; this is how a checkout pattern declares its submit
    step COMMITTING without a hardcoded core constant."""

    name: str
    matcher: Callable[[dict], bool]
    commitment: Commitment
    weight: float = 1.0

    def signal(self, facts: dict) -> Signal | None:
        if not self.matcher(facts):
            return None
        w = self.weight if self.commitment is Commitment.COMMITTING else -self.weight
        return Signal(name=self.name, weight=w)


# RFC 7231 §4.2: safe methods never have observable side effects.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# Methods that, by HTTP semantics, may create/modify/remove server state.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Generic interaction verbs (NOT site words): what the OP semantically does.
_REVERSIBLE_OPS = frozenset({"fill", "read", "open", "reduce", "select", "expand", "focus"})
_COMMITTING_OPS = frozenset(
    {"submit", "confirm", "purchase", "pay", "checkout", "delete", "place_order"}
)
# Roles whose interaction is, by accessibility semantics, read-only/navigational.
_REVERSIBLE_ROLES = frozenset(
    {"textbox", "searchbox", "combobox", "checkbox", "radio", "switch", "tab", "option", "link"}
)


def _op_signal(op: str | None) -> Signal | None:
    if not op:
        return None
    o = op.strip().lower()
    if o in _COMMITTING_OPS:
        return Signal(name=f"op:{o}", weight=1.0)
    if o in _REVERSIBLE_OPS:
        return Signal(name=f"op:{o}", weight=-0.5)
    return None


def _role_signal(role: str | None) -> Signal | None:
    if not role:
        return None
    r = role.strip().lower()
    if r in _REVERSIBLE_ROLES:
        return Signal(name=f"role:{r}", weight=-0.5)
    # A plain ``button`` is ambiguous (it might submit a form) — no signal, so it
    # falls to the default-committing floor unless another signal resolves it.
    return None


def _http_signal(http_method: str | None, idempotent: bool | None) -> Signal | None:
    if http_method:
        m = http_method.strip().upper()
        if m in _SAFE_METHODS:
            return Signal(name=f"http:{m}", weight=-1.0)
        if m in _WRITE_METHODS:
            # An explicitly idempotent write (PUT/DELETE with an idempotency key)
            # is still a state change — committing — but a caller may down-weight.
            return Signal(name=f"http:{m}", weight=1.0)
    if idempotent is True:
        return Signal(name="idempotent", weight=-0.5)
    if idempotent is False:
        return Signal(name="non_idempotent", weight=1.0)
    return None


# A safe-default empty prior set; a consumer passes its own (or a pattern's).
DEFAULT_PRIORS: tuple[ActionPrior, ...] = ()


def classify_action(
    *,
    http_method: str | None = None,
    role: str | None = None,
    op: str | None = None,
    idempotent: bool | None = None,
    annotations: dict | None = None,
    priors: Sequence[ActionPrior] = DEFAULT_PRIORS,
) -> Commitment:
    """Classify one action as REVERSIBLE or COMMITTING (pure, deterministic).

    Priority: an explicit rail ``annotations["consequence"]`` is authoritative
    when present (ZU-RAIL-4); otherwise sum the HTTP/op/role/prior signals. A net
    negative sum is REVERSIBLE; zero or positive — including the no-signal case —
    is COMMITTING (default-to-safe). The signed sum, not any single keyword,
    decides, so the result is robust to a single weak hint.
    """
    # 1) authoritative rail annotation: a content-free consequence class.
    if annotations:
        consequence = annotations.get("consequence")
        if isinstance(consequence, str):
            c = consequence.strip().lower()
            if c in {"read", "readonly", "read_only", "reversible", "none", "safe"}:
                return Commitment.REVERSIBLE
            if c in {"write", "commit", "committing", "irreversible", "payment", "purchase"}:
                return Commitment.COMMITTING

    signals: list[Signal] = []
    facts = {"http_method": http_method, "role": role, "op": op, "idempotent": idempotent}
    for maybe in (
        _http_signal(http_method, idempotent),
        _op_signal(op),
        _role_signal(role),
    ):
        if maybe is not None:
            signals.append(maybe)
    # 4) extensible priors (a pattern's contributed evidence).
    for prior in priors:
        s = prior.signal(facts)
        if s is not None:
            signals.append(s)

    score = sum(s.weight for s in signals)
    # 5) DEFAULT-TO-COMMITTING: only a net-reversible balance of evidence flips it.
    return Commitment.REVERSIBLE if score < 0 else Commitment.COMMITTING


@dataclass(frozen=True)
class ClassifiedAction:
    """A concrete action with its consequence class — the value the planner reads
    to know which FSM edges are safe to cross live."""

    label: str
    commitment: Commitment
    signals: tuple[Signal, ...] = field(default_factory=tuple)
