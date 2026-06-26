"""newsletter_signup — a lone email box next to a subscribe/join button.

Fires on a single email textbox (with <=2 fillable fields, no shipping slots and
no OTP field — so it never poaches contact_form's territory) accompanied by a
subscribe/join/sign-up button OR a subscribe/newsletter context. The submit step
is REVERSIBLE-leaning: subscribing is a low-stakes, typically-undoable opt-in
(an unsubscribe link follows), so the prior pulls the classifier off the
default-committing floor rather than declaring a hard boundary.

Script (a PROPOSAL, never auto-run): fill email, then submit.
Success: a confirmation/opt-in surface. Failure: a validation error.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceAffordance, SurfaceView

from . import _match as m
from .rail import surface_shows
from .reversibility import ActionPrior, Commitment

_FILLABLE_ROLES = ("textbox", "searchbox", "combobox")
_SUBSCRIBE_CONTEXT = ("newsletter", "subscribe", "sign up for", "get updates", "stay updated")


def _is_password(aff: SurfaceAffordance) -> bool:
    return m.has_state(aff, "password") or m.label_has(aff, m.PASSWORD_TOKENS)


class NewsletterSignup:
    name = "newsletter_signup"
    archetype = "newsletter_signup"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        fillable = [a for a in m.of_role(surface, *_FILLABLE_ROLES) if not _is_password(a)]
        # A lone-email box: a small region (<=2 fillable fields) with one email box.
        if not fillable or len(fillable) > 2:
            return None
        email = m.first(surface, roles=_FILLABLE_ROLES, tokens=("email", "e-mail"))
        if email is None:
            return None
        # Shipping/OTP vocabulary ⇒ this is a contact_form, not a signup — defer.
        if any(m.label_has(a, m.SHIPPING_TOKENS) for a in fillable):
            return None
        if any(m.label_has(a, m.OTP_TOKENS) or m.has_state(a, "otp") for a in fillable):
            return None
        button = m.first(surface, roles=("button",), tokens=m.SUBSCRIBE_TOKENS)
        ctx = m.context_has(surface, _SUBSCRIBE_CONTEXT)
        # A worded subscribe/join button is the strong tell; subscribe CONTEXT
        # alone (no worded button) is a weaker, still-actionable signal.
        if button is not None:
            confidence = 0.85
        elif ctx:
            confidence = 0.65
        else:
            return None
        script: list[PatternStep] = [
            PatternStep(op="fill", role=email.role, label_hint=m.norm(email.label), note="email")
        ]
        handles = [email.handle]
        if button is not None:
            script.append(
                PatternStep(op="submit", role="button", label_hint=m.norm(button.label), note="subscribe")
            )
            handles.append(button.handle)
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=tuple(handles),
            script=tuple(script),
            detail="newsletter signup",
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Done = a post-submit confirmation/opt-in surface EVENTUALLY appears.
        return [surface_shows(self.archetype, "subscribed", label="subscribed", liveness=True)]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = a validation error appears. Safety shape:
        # THROUGHOUT NOT contains(error) — fires the instant the error lands.
        return [surface_shows(self.archetype, "signup_error", label="error", negate=True)]

    # The reversibility prior this pattern CONTRIBUTES: its subscribe step is
    # REVERSIBLE-leaning (a low-stakes opt-in, typically undoable). The weight
    # overcomes the generic ``submit`` op-signal so ``classify_action`` lands on
    # REVERSIBLE for this step — without a hardcoded core constant.
    @staticmethod
    def submit_prior() -> ActionPrior:
        def _is_subscribe(facts: dict) -> bool:
            op = str(facts.get("op", "")).lower()
            note = str(facts.get("note", "")).lower()
            return op in {"submit", "subscribe"} or "subscribe" in note

        return ActionPrior(
            name="newsletter_signup.subscribe",
            matcher=_is_subscribe,
            commitment=Commitment.REVERSIBLE,
            weight=2.0,
        )
