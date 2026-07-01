"""cookie_banner — a consent/cookie banner with an accept/reject button cluster.

Recognized when the surface's context mentions cookies/consent (or an
alert/region carries it) and the affordances are dominated by accept/agree/reject
buttons with no other task affordances. Dismissing is idempotent ⇒ REVERSIBLE.
Success: the accept button is GONE from the next surface (a negated
SURFACE_CONTAINS). Failure: the banner persists.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceView

from . import _match as m
from .rail import surface_shows

_CONSENT_CONTEXT = ("cookie", "consent", "gdpr", "privacy", "tracking")
# Consent-wall failure vocabulary — an any-of set (#46), so a real block surface
# fires the failure rail, not only the single literal "error".
_ERROR_TOKENS = ("error", "blocked", "please accept", "try again")


class CookieBanner:
    name = "cookie_banner"
    archetype = "cookie_banner"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        accept = m.first(surface, roles=("button",), tokens=m.ACCEPT_TOKENS)
        if accept is None:
            return None
        buttons = m.of_role(surface, "button")
        consent_ctx = m.context_has(surface, _CONSENT_CONTEXT)
        consent_label = any(m.label_has(b, _CONSENT_CONTEXT) for b in buttons)
        if not (consent_ctx or consent_label):
            return None
        # Confidence: a small banner (few affordances) dominated by accept/reject
        # is a strong match; a page with many other affordances is weaker.
        non_consent = [
            a
            for a in surface.affordances
            if a.handle != accept.handle
            and not m.label_has(a, m.ACCEPT_TOKENS + m.REJECT_TOKENS + m.CLOSE_TOKENS)
        ]
        confidence = 0.9 if len(non_consent) <= 1 else 0.65
        if consent_ctx and consent_label:
            confidence = min(1.0, confidence + 0.05)
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=(accept.handle,),
            script=(
                PatternStep(
                    op="click", role="button", label_hint=m.norm(accept.label), note="accept"
                ),
            ),
            detail="consent banner",
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        handle = result.matched_handles[0] if result.matched_handles else None
        # Done = the accept button is EVENTUALLY gone (a liveness-of-ABSENCE check:
        # the banner is present pre-dismiss, so it must not fire until the deadline;
        # if the button never goes away by the deadline, that liveness VIOLATES —
        # which IS the "banner persists" failure, captured without a redundant
        # positive must-contain-THROUGHOUT invariant).
        return [surface_shows(self.archetype, "dismissed", handle=handle, negate=True, liveness=True)]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = a consent-wall error appears. Safety shape: THROUGHOUT
        # NOT contains(error) — fires the instant it lands. (The "banner persists"
        # mode is the success liveness deadline-violation, not duplicated here.)
        return [
            surface_shows(self.archetype, "consent_error", labels=_ERROR_TOKENS, negate=True)
        ]
