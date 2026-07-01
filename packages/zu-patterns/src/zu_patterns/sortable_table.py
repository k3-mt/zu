"""sortable_table — a table/grid with column-header buttons carrying sort states.

Script: click a header. Sorting is a view change ⇒ REVERSIBLE. Success: the sort
state toggles.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceView

from . import _match as m
from .confidence import GOOD, LOW
from .rail import surface_shows

_SORT_STATES = ("sort-asc", "sort-desc", "ascending", "descending", "sortable")


class SortableTable:
    name = "sortable_table"
    archetype = "sortable_table"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        has_table = bool(m.of_role(surface, "table", "grid"))
        # A sortable header is a columnheader/button affordance with a sort state.
        header = next(
            (
                a
                for a in surface.affordances
                if a.role.lower() in {"columnheader", "button"} and m.has_state(a, *_SORT_STATES)
            ),
            None,
        )
        if header is None:
            return None
        confidence = GOOD if has_table else LOW
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=(header.handle,),
            script=(
                PatternStep(
                    op="click", role=header.role, label_hint=m.norm(header.label), note="sort"
                ),
            ),
            detail="sortable table",
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        handle = result.matched_handles[0] if result.matched_handles else None
        # Done = the same header is EVENTUALLY present again (the table re-rendered)
        # — a minimal liveness-by-deadline check; a richer "state toggled" check is
        # a later predicate. Asserted as presence of the header by the deadline.
        return [surface_shows(self.archetype, "resorted", handle=handle, liveness=True)]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = an error banner appears (the re-sort errored). Safety
        # shape: THROUGHOUT NOT contains(error) — fires the instant it lands.
        return [surface_shows(self.archetype, "sort_error", label="error", negate=True)]
