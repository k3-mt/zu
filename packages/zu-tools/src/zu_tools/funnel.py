"""funnel — classify where a page sits in the purchase funnel (#121).

The connected-surface family ships the funnel TRANSITIONS (consent, variant-select,
add-to-cart, checkout-advance); this is the content-free STATE they move between —
so a host gets a readable phase timeline (drift shows up as a phase REGRESSION) and
a precise progress/regress signal, instead of hand-rolling its own. Derived purely
from the SHAPE the surface already carries — an add-to-cart control, a cart /
'added' signal, a checkout url / place-order control, a card field — never product
prose. The :class:`~zu_core.ports.FunnelPhase` enum is the shared vocabulary.
"""

from __future__ import annotations

from zu_core.ports import FunnelPhase
from zu_core.surface import SurfaceView

from ._commerce import add_to_cart_handle, at_checkout, has_card_field, in_cart


def funnel_phase(view: SurfaceView) -> FunnelPhase:
    """Classify a SHOPPING surface onto the universal funnel rungs — DEEPEST phase wins. A checkout
    page also shows a cart 'subtotal', and a product page that just added shows a drawer, so a
    shallower signal must not shadow a deeper one: test commit → checkout → cart → product → entry in
    that order. Structural and content-free. (The shopping signals map onto the shared rungs: card
    fields → AT_COMMIT, a cart → ASSEMBLING, an add-to-cart → SELECTING — so a booking classifier
    returning the same rungs is directly comparable.)"""
    if has_card_field(view):
        return FunnelPhase.AT_COMMIT
    if at_checkout(view):
        return FunnelPhase.AT_CHECKOUT
    if in_cart(view):
        return FunnelPhase.ASSEMBLING
    if add_to_cart_handle(view) is not None:
        return FunnelPhase.SELECTING
    if view.affordances or view.context:
        return FunnelPhase.ENTRY
    return FunnelPhase.UNKNOWN


class WebFunnelPhaseClassifier:
    """The reference :class:`~zu_core.ports.FunnelPhaseClassifier` for the web."""

    __zu_interface__ = 1  # the funnel_phase_classifiers interface major this targets
    name = "web_funnel_phase_classifier"

    def classify(self, view: SurfaceView) -> FunnelPhase:
        return funnel_phase(view)
