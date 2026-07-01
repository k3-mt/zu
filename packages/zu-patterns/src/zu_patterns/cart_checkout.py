"""cart_checkout — the canonical IRREVERSIBLE-BOUNDARY pattern.

Recognizes a button cluster {add to cart, checkout, place order, pay} alongside
line-item context. The proposed script STOPS BEFORE the committing step: it adds
to cart / proceeds to checkout, but the place-order/pay step is classified
COMMITTING — the live-search and rail commit boundary. This is the discipline
made into a pattern: search may explore up to the boundary, never auto-cross it.
Success: an order-confirmation surface. The place-order step never auto-executes.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceView

from . import _match as m
from .rail import surface_shows
from .reversibility import ActionPrior, Commitment

_LINE_ITEM_CONTEXT = ("cart", "basket", "bag", "subtotal", "order summary", "line item", "qty")
_CONFIRM_CONTEXT = (
    "order confirmed",
    "thank you for your order",
    "order number",
    "confirmation",
    "payment received",
    "payment successful",
    "order placed",
)
# Payment/checkout FAILURE vocabulary — an any-of set of real decline/error
# surfaces (#46), so "Card declined"/"Payment failed" fire the failure rail, not
# only the single English literal "error".
_ERROR_TOKENS = (
    "error",
    "declined",
    "payment failed",
    "card declined",
    "could not process",
    "try again",
)


class CartCheckout:
    name = "cart_checkout"
    archetype = "cart_checkout"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        add = m.first(surface, roles=("button",), tokens=m.CART_TOKENS)
        checkout = m.first(surface, roles=("button", "link"), tokens=m.CHECKOUT_TOKENS)
        place = m.first(surface, roles=("button",), tokens=m.PLACE_ORDER_TOKENS)
        if add is None and checkout is None and place is None:
            return None
        line_ctx = m.context_has(surface, _LINE_ITEM_CONTEXT)
        # Confidence: a cart/checkout button WITH line-item context is strong.
        present = [x for x in (add, checkout, place) if x is not None]
        confidence = 0.85 if (line_ctx and present) else 0.6
        # The proposed script advances toward — but STOPS BEFORE — the committing
        # place-order/pay step. We propose the safe step (add / go to checkout) and
        # mark the committing step with an ``expect`` (a boundary marker), never a
        # ``submit`` the search would auto-cross.
        script: list[PatternStep] = []
        handles: list[str] = []
        safe = add or checkout
        if safe is not None:
            script.append(
                PatternStep(op="click", role=safe.role, label_hint=m.norm(safe.label), note="proceed")
            )
            handles.append(safe.handle)
        if place is not None:
            # The boundary: marked, not proposed for execution.
            script.append(
                PatternStep(
                    op="expect",
                    role="button",
                    label_hint=m.norm(place.label),
                    note="COMMIT BOUNDARY: place-order/pay is committing — do not auto-cross",
                )
            )
            handles.append(place.handle)
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=tuple(handles),
            script=tuple(script),
            detail="cart/checkout (irreversible boundary)",
            # Declared outcome: a basket/cart → order/checkout surface (#69).
            outcome=m.CART_TOKENS + m.CHECKOUT_TOKENS + m.PLACE_ORDER_TOKENS,
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Done = an order-confirmation surface EVENTUALLY appears (by the deadline).
        # A committed-but-never-confirmed run violates this liveness at the deadline.
        # ANY of the confirmation-vocabulary variants satisfies it (#46), so a real
        # "Payment received"/"Order #123" success is recognized, not only the single
        # literal "order confirmed".
        return [
            surface_shows(
                self.archetype, "order_confirmed", labels=_CONFIRM_CONTEXT, liveness=True
            )
        ]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = a payment/checkout error appears. Safety shape:
        # THROUGHOUT NOT contains(<any error variant>) — fires the instant a decline
        # or failure surface lands (#46), not only on the literal "error".
        return [
            surface_shows(self.archetype, "checkout_error", labels=_ERROR_TOKENS, negate=True)
        ]

    # The reversibility prior this pattern CONTRIBUTES: its place-order/pay step is
    # COMMITTING. A planner/classifier passes this into ``classify_action`` so the
    # boundary is declared by the pattern, not hardcoded into the core classifier.
    @staticmethod
    def commit_prior() -> ActionPrior:
        def _is_place_order(facts: dict) -> bool:
            note = str(facts.get("note", "")).lower()
            op = str(facts.get("op", "")).lower()
            label = str(facts.get("label_hint", "")).lower()
            return (
                op in {"place_order", "pay", "purchase", "checkout"}
                or "commit boundary" in note
                or any(tok in label for tok in m.PLACE_ORDER_TOKENS)
            )

        return ActionPrior(
            name="cart_checkout.place_order",
            matcher=_is_place_order,
            commitment=Commitment.COMMITTING,
            weight=2.0,
        )
