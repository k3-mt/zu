"""#121 — funnel_phase: content-free purchase-funnel phase from surface shape."""

from __future__ import annotations

from zu_core.ports import FunnelPhase, FunnelPhaseClassifier
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_tools.funnel import WebFunnelPhaseClassifier, funnel_phase


def aff(role: str, label: str, *, autocomplete: str | None = None) -> SurfaceAffordance:
    return SurfaceAffordance(handle="h", role=role, label=label, autocomplete=autocomplete)


def view(*affs: SurfaceAffordance, url: str = "", context: tuple[str, ...] = ()) -> SurfaceView:
    return SurfaceView(url=url, affordances=tuple(affs), context=context)


def test_classifier_conforms_and_matches_the_function() -> None:
    assert isinstance(WebFunnelPhaseClassifier(), FunnelPhaseClassifier)
    v = view(aff("button", "Add to cart"))
    assert WebFunnelPhaseClassifier().classify(v) == funnel_phase(v)


def test_browsing_is_a_listing_with_no_commerce_control() -> None:
    v = view(aff("link", "Dog collars"), aff("link", "Cat toys"), context=("Shop all",))
    assert funnel_phase(v) == FunnelPhase.ENTRY


def test_on_product_when_an_add_to_cart_control_is_present() -> None:
    v = view(aff("button", "Add to basket"), aff("link", "Home"))
    assert funnel_phase(v) == FunnelPhase.SELECTING


def test_in_cart_on_an_added_signal() -> None:
    v = view(aff("link", "View cart"), context=("Leather collar added to your cart", "Subtotal £24"))
    assert funnel_phase(v) == FunnelPhase.ASSEMBLING


def test_at_checkout_on_a_place_order_control() -> None:
    v = view(aff("button", "Place order"), context=("Order summary",))
    assert funnel_phase(v) == FunnelPhase.AT_CHECKOUT


def test_at_checkout_on_a_checkout_url() -> None:
    v = view(aff("button", "Continue"), url="https://shop.test/checkouts/abc")
    assert funnel_phase(v) == FunnelPhase.AT_CHECKOUT


def test_at_payment_on_a_card_field() -> None:
    v = view(aff("textbox", "Card number", autocomplete="cc-number"), aff("button", "Pay"))
    assert funnel_phase(v) == FunnelPhase.AT_COMMIT


def test_deepest_phase_wins_over_shallower_signals() -> None:
    # A payment page also carries a cart 'subtotal' and could show an add-to-cart —
    # the deepest recognised phase must win.
    v = view(
        aff("textbox", "CVV", autocomplete="cc-csc"),
        aff("button", "Add to cart"),
        context=("Subtotal £24", "Order summary"),
    )
    assert funnel_phase(v) == FunnelPhase.AT_COMMIT


def test_unknown_on_an_empty_surface() -> None:
    assert funnel_phase(SurfaceView()) == FunnelPhase.UNKNOWN


def test_phase_is_content_free_ignoring_product_prose() -> None:
    # The same structural shape classifies the same regardless of product wording.
    a = view(aff("button", "Add to cart"), context=("Artisan Leather Dog Collar — hand-stitched",))
    b = view(aff("button", "Add to cart"), context=("Bulk Nylon Cat Harness — 12 pack",))
    assert funnel_phase(a) == funnel_phase(b) == FunnelPhase.SELECTING
