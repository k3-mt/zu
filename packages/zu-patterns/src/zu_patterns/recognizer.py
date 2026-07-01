"""The recognizer pass — a pure, $0 classification over a core ``SurfaceView``.

This is the move-ordering prior of the §5 stack: given the affordances at one
step, ask every registered pattern whether it recognizes the situation, sort the
hits by confidence, and surface the best (above a threshold) plus the full
candidate list for audit/move-ordering. It is deterministic — no model, no I/O —
so a low-confidence step yields NO hint and the policy + safe search take over.

The recognizer NEVER chooses the task action: it ENUMERATES/CLASSIFIES (archetype
+ confidence + a PROPOSED script); disposing/deciding stays with the policy and
the guided search. ``record_recognition`` builds the ``data.pattern.recognized``
payload a harness shim can put on the log — "what the agent perceived/inferred",
never an instruction it obeyed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from zu_core import events as ev
from zu_core.ports import Pattern, RecognitionResult
from zu_core.surface import SurfaceView

from .confidence import MIN_CONFIDENCE


@dataclass(frozen=True)
class Recognition:
    """The result of the recognizer pass.

    ``result`` is the best hit at or above ``min_confidence`` (``None`` ⇒
    low-confidence fall-through: NO hint). ``candidates`` is every pattern that
    fired, confidence-sorted, for audit and move-ordering in the planner.
    """

    result: RecognitionResult | None
    candidates: tuple[RecognitionResult, ...]


def recognize(
    surface: SurfaceView,
    patterns: Sequence[Pattern],
    *,
    min_confidence: float = MIN_CONFIDENCE,
) -> Recognition:
    """Run every pattern's ``recognize`` over ``surface`` and pick the best hit.

    Pure and deterministic: the patterns are structural matchers, the sort is a
    stable confidence sort, and the threshold gate (``min_confidence``) decides
    confident-path vs fall-through. A blind surface is recognizable too (a
    pattern may still match its visible affordances), but the blind signal rides
    on the surface for the policy to weigh.
    """
    hits: list[RecognitionResult] = []
    for p in patterns:
        r = p.recognize(surface)
        if r is not None:
            hits.append(r)
    hits.sort(key=lambda r: r.confidence, reverse=True)
    best = hits[0] if hits and hits[0].confidence >= min_confidence else None
    return Recognition(result=best, candidates=tuple(hits))


def record_recognition(result: RecognitionResult, *, blind: bool = False) -> dict:
    """The ``data.pattern.recognized`` payload for a confident recognition.

    A harness shim emits ``zu_core.events.PATTERN_RECOGNIZED`` with this payload,
    parented to the turn — the auditable record of what the agent inferred. A
    low-confidence (``None``) recognition emits NOTHING: no hint masquerading as
    ground truth (the rail is what verifies a prior; see ZU-RAIL-9)."""
    return {
        "archetype": result.archetype,
        "confidence": result.confidence,
        "matched_handles": list(result.matched_handles),
        "blind": blind,
    }


# The event type the harness shim stamps for ``record_recognition`` — re-exported
# so a consumer does not have to reach into zu_core.events directly.
PATTERN_RECOGNIZED = ev.PATTERN_RECOGNIZED
