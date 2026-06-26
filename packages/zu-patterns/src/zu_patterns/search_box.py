"""search_box — a lone search/textbox (label/placeholder 'search') + optional submit.

Script: fill query, submit. Submitting a search is a GET ⇒ reversible-leaning.
Success: a results-list/listbox surface appears. Failure: no surface change.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceView

from . import _match as m
from .rail import surface_shows


class SearchBox:
    name = "search_box"
    archetype = "search_box"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        box = m.first(surface, roles=("searchbox",)) or m.first(
            surface, roles=("textbox", "combobox"), tokens=m.SEARCH_TOKENS
        )
        if box is None:
            return None
        # A dedicated searchbox role is a strong signal; a textbox merely labelled
        # 'search' is weaker. Don't fire if it looks like a login (a password
        # field present) — that is login_form's territory.
        if any(m.has_state(a, "password") for a in m.of_role(surface, "textbox")):
            return None
        submit = m.first(surface, roles=("button",), tokens=m.SEARCH_TOKENS + ("go",))
        confidence = 0.85 if box.role.lower() == "searchbox" else 0.62
        script = [
            PatternStep(op="fill", role=box.role, label_hint=m.norm(box.label), note="query")
        ]
        if submit is not None:
            script.append(
                PatternStep(
                    op="submit", role="button", label_hint=m.norm(submit.label), note="search"
                )
            )
        handles = tuple(h for h in (box.handle, submit.handle if submit else None) if h)
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=handles,
            script=tuple(script),
            detail="search box",
            # Declared outcome: a results/listing surface — on-path for a "find X" goal (#69).
            outcome=m.SEARCH_TOKENS + ("results", "listing"),
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Done = a results list/listbox surface EVENTUALLY appears (by the deadline).
        return [surface_shows(self.archetype, "results_shown", label="results", liveness=True)]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = an explicit "no results" empty-state appears. Safety
        # shape: THROUGHOUT NOT contains(no results) — fires the instant it shows.
        return [surface_shows(self.archetype, "no_results", label="no results", negate=True)]
