"""Pattern → rail helpers — success/failure criteria as declarable Invariants.

A pattern's predicted "done" state and its known failure modes are expressed as
``zu_core.invariants.Invariant``s, which compile (via ``compile_spec``) to
Monitors the loop's ZU-RAIL-5 checkpoint runs. This is FULL reuse of the §1
machinery — no new monitor type. A breach yields ``MonitorVerdict(VIOLATION)`` →
the existing escalation path, which is the ZU-RAIL-9 guarantee: a recognized
pattern's prediction is VERIFIED, never trusted as ground truth.

The Monitor names are namespaced ``pattern.<archetype>.<criterion>`` so audits
read cleanly.
"""

from __future__ import annotations

from collections.abc import Sequence

from zu_core import events as ev
from zu_core.invariants import Invariant, InvariantKind, Predicate, PredicateKind


def _name(archetype: str, criterion: str) -> str:
    return f"pattern.{archetype}.{criterion}"


def surface_shows(
    archetype: str,
    criterion: str,
    *,
    label: str | None = None,
    labels: Sequence[str] | None = None,
    handle: str | None = None,
    state: str | None = None,
    states: Sequence[str] | None = None,
    recognized_archetype: str | None = None,
    event_type: str = ev.SURFACE_CAPTURED,
    negate: bool = False,
    liveness: bool = False,
    deadline: str | None = None,
) -> Invariant:
    """An Invariant over a surface event — the seam a Pattern's success/failure
    criterion compiles to (ZU-RAIL-9).

    One of ``label`` / ``labels`` / ``handle`` / ``state`` / ``recognized_archetype``
    is the token(s) SURFACE_CONTAINS folds the event log for. ``labels`` is an
    ANY-OF set (#46): the rail is satisfied if ANY of the equivalent success/failure
    markers appears — so a casing/synonym variant of the expected token no longer
    defeats the verify layer. Label matching is normalized + word-boundary-aware
    (#57). ``state``/``states`` + ``handle`` asserts "handle H reached that state
    (or ANY of these states)" — the content-free "a control became selected" success
    criterion (#39); ``states`` is the any-of set (selected/checked/pressed/…).
    ``negate=True`` asserts ABSENCE (the natural shape for "the banner is gone").

    Two semantics, chosen by ``liveness``:

    * ``liveness=True`` — a SUCCESS / POSTCONDITION criterion: an
      ``EVENTUALLY``-by-deadline property. The predicted post-state is, by
      definition, ABSENT until the interaction completes, so it must NOT be a
      violation that early/pre-interaction surfaces lack it. The Monitor stays
      inert until the post-state appears (then satisfied forever) OR the
      ``deadline`` event arrives without it (then, and only then, VIOLATION).
      ``deadline`` is the deadline event TYPE; ``None`` ⇒ any terminal event
      (``TASK_TERMINAL``/``TASK_COMPLETED``) marking the interaction/run complete.
      For a non-negated success token we also require the token to ACTUALLY appear
      (``require_present``) so "no surface ever showed it" correctly violates at
      the deadline rather than passing vacuously.

    * ``liveness=False`` (default) — a SAFETY criterion: ``THROUGHOUT``. The
      correct shape for a FAILURE CONTEXT is ``negate=True`` ("throughout: NOT
      contains(error-context)") so the Monitor fires the instant the failure
      context appears, and the pre-interaction state (where the context is absent)
      satisfies it. Do NOT model a failure as a positive must-contain-THROUGHOUT —
      that wrongly fires on every normal surface lacking the token.
    """
    params: dict = {"event_type": event_type, "negate": negate}
    if recognized_archetype is not None:
        params["archetype"] = recognized_archetype
    elif state is not None or states is not None:
        # A per-control state rail (#39): "handle H reached ANY of these states".
        # The handle scopes the check to the acted control, not any control. An
        # any-of set (``states``) covers the equivalent selection markers
        # (selected/checked/pressed/aria-selected) so a picker that flips ``checked``
        # rather than ``selected`` still satisfies "became selected".
        if states is not None:
            params["states"] = list(states)
        if state is not None:
            params["state"] = state
        if handle is not None:
            params["handle"] = handle
    elif handle is not None:
        params["handle"] = handle
    elif labels is not None:
        # An any-of set of equivalent success/failure markers (#46).
        params["labels"] = list(labels)
    elif label is not None:
        params["label"] = label
    if liveness:
        # A non-negated liveness token must genuinely appear by the deadline;
        # a negated one (a state that must become ABSENT) needs no evidence floor.
        if not negate:
            params["require_present"] = True
        return Invariant(
            name=_name(archetype, criterion),
            kind=InvariantKind.EVENTUALLY,
            predicate=Predicate(kind=PredicateKind.SURFACE_CONTAINS, params=params),
            applies_to=deadline,
        )
    return Invariant(
        name=_name(archetype, criterion),
        kind=InvariantKind.THROUGHOUT,
        predicate=Predicate(kind=PredicateKind.SURFACE_CONTAINS, params=params),
    )
