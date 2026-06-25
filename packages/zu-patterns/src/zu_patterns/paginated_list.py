"""paginated_list — a list/listbox plus a next/prev/page-N link cluster.

Script: click next. Navigation ⇒ REVERSIBLE. Success: the list refreshes / the
page context advances.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceView

from . import _match as m
from .rail import surface_shows


class PaginatedList:
    name = "paginated_list"
    archetype = "paginated_list"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        has_list = bool(m.of_role(surface, "list", "listbox", "table", "grid"))
        # A 'next' affordance is a link or a button labelled next/more.
        nxt = m.first(surface, roles=("link", "button"), tokens=m.NEXT_TOKENS)
        prev = m.first(surface, roles=("link", "button"), tokens=m.PREV_TOKENS)
        if nxt is None and prev is None:
            return None
        # Pagination is strongest with a list AND a next control.
        confidence = 0.8 if (has_list and nxt is not None) else 0.6
        target = nxt or prev
        assert target is not None
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=(target.handle,),
            script=(
                PatternStep(
                    op="click", role=target.role, label_hint=m.norm(target.label), note="paginate"
                ),
            ),
            detail="paginated list",
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Done = a fresh list surface EVENTUALLY appears (a results/list affordance
        # present by the deadline). Liveness, not THROUGHOUT.
        return [surface_shows(self.archetype, "page_advanced", label="results", liveness=True)]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = an error/empty-state banner appears (navigation broke).
        # Safety shape: THROUGHOUT NOT contains(error) — fires when it lands.
        return [surface_shows(self.archetype, "page_error", label="error", negate=True)]
