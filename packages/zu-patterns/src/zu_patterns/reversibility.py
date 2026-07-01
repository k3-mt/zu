"""The reversible-vs-committing action classifier — principled & generic.

It marks the boundary the guided search must not cross during LIVE exploration
(and the rail's commit boundary): a REVERSIBLE action is read-only/idempotent
(safe to explore), a COMMITTING action is side-effecting/irreversible (the
boundary). The discipline is **default-to-committing** — the safe rail behaviour
— whenever the signals are inconclusive. There is NO site-specific magic keyword
blocklist — in particular NO commerce-verb list (``pay``/``checkout``/
``place_order`` are SITE VOCABULARY, not structure, and live in the checkout
pattern's contributed prior, not here). The signals are principled and generic:

1. EXPLICIT rail annotation (``ctx.annotations["consequence"]``, ZU-RAIL-4 — a
   content-free consequence class) — authoritative when present.
2. HTTP method / idempotency when OBSERVABLE — RFC 7231 safe/idempotent
   semantics (GET/HEAD/OPTIONS ⇒ reversible; POST/PUT/PATCH/DELETE ⇒ committing),
   not site words.
3. STRUCTURE — a control that DECLARES a side effect: ``submits`` (the
   locale-independent ``button[type=submit]`` / form-submit structural signal
   threaded from the harness, ``SurfaceAffordance.submits``) is the PRIMARY
   irreversibility tell. This is a form's own SHAPE, not an English word in its
   label, so a non-English ``Bezahlen`` submit button still reads as committing.
4. Affordance SEMANTICS from role/op — a SMALL, generic set of INTERACTION verbs
   as a weak SECONDARY hint (``fill``/``read``/``open`` ⇒ reversible-leaning;
   ``submit``/``confirm``/``delete`` ⇒ committing-leaning). These are generic
   interaction primitives, NOT a commerce blocklist — a checkout's ``pay`` step is
   declared by the cart pattern's prior (5), never by a keyword here.
5. A small EXTENSIBLE prior set a pattern/plugin contributes (additive,
   community-extensible, never hardcoded into core) — this is where site-specific
   commit boundaries (a checkout's place-order step) are declared.
6. DEFAULT: uncertain ⇒ COMMITTING.

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

# Generic INTERACTION verbs (NOT site words): what the OP semantically does. This
# is the primitive interaction alphabet — ``fill``/``select``/``open`` are
# read-only-leaning; ``submit``/``confirm``/``delete`` are the generic
# side-effecting primitives (submit a form, confirm a prompt, remove an item).
# DELIBERATELY there is NO commerce vocabulary here (``pay``/``checkout``/
# ``purchase``/``place_order``): those are SITE words, not interaction primitives —
# a checkout's commit boundary is declared by the cart pattern's contributed prior
# (``CartCheckout.commit_prior``), never by a keyword in this core classifier (#65).
_REVERSIBLE_OPS = frozenset({"fill", "read", "open", "reduce", "select", "expand", "focus"})
_COMMITTING_OPS = frozenset({"submit", "confirm", "delete"})
# Roles whose interaction is, by accessibility semantics, read-only/navigational —
# UNLESS the control also declares a side effect (``submits``): a ``link``/``tab``
# is reversible for PLAIN navigation, but a link/tab that submits a form (a logout
# or delete link rendered as an <a>/tab) is a committing navigation and must NOT be
# assumed reversible (#65 F18). The commit is decided STRUCTURALLY (``submits``),
# never by matching "logout"/"delete" words.
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


def _submits_signal(submits: bool | None) -> Signal | None:
    """The PRIMARY structural irreversibility signal (#65 F16): a control that
    STRUCTURALLY declares a side effect — a ``button[type=submit]`` / form-submit
    (``SurfaceAffordance.submits``) — is committing by SHAPE, not by any English
    word in its label. Weighted to dominate a single reversible role/op hint (a
    submit control rendered as a link is still committing), so it is the tell the
    verb blocklist used to (wrongly) stand in for."""
    if submits:
        return Signal(name="submits", weight=1.0)
    return None


def _role_signal(role: str | None, submits: bool | None = None) -> Signal | None:
    if not role:
        return None
    r = role.strip().lower()
    if r in _REVERSIBLE_ROLES:
        # F18: a link/tab is reversible for PLAIN navigation only. When it carries a
        # commit/side-effect signal (``submits`` — a logout/delete link rendered as
        # an <a>/tab that submits) it is a committing navigation: withhold the
        # reversible role signal so the structural ``submits`` signal governs and the
        # action does not read as safe. Decided structurally, never by word.
        if submits and r in {"link", "tab"}:
            return None
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
    submits: bool | None = None,
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
    facts = {
        "http_method": http_method,
        "role": role,
        "op": op,
        "idempotent": idempotent,
        "submits": submits,
    }
    for maybe in (
        _http_signal(http_method, idempotent),
        _submits_signal(submits),
        _op_signal(op),
        _role_signal(role, submits),
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
