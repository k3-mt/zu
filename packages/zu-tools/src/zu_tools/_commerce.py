"""_commerce — the shared, content-free vocabulary + structural signals the web
purchase-funnel resolvers speak (checkout #117, funnel phase #121, cart #122).

One home for the small commerce control vocabulary (add-to-cart / checkout /
place-order) and the structural signals derived from a ``SurfaceView`` — so the
resolvers agree on what a 'commit' control is, what a 'took' signal is, etc., and
none of it drifts between them. Content-free throughout: control wording + the
locale-independent structural signals a ``SurfaceAffordance`` already carries
(``autocomplete``/``states``/``role``), never product prose.
"""

from __future__ import annotations

import re

from zu_core.surface import SurfaceAffordance, SurfaceView

from ._wholeword import contains_any, matches_whole_word

# Roles a commerce control actually takes — a button or link, never a textbox.
CLICKABLE_ROLES: frozenset[str] = frozenset({"button", "link", "menuitem"})

# ADD-TO-CART — the product→cart control, matched WHOLE-WORD when we mean to click
# it, or as a substring when we mean to EXCLUDE it from another selection.
ADD_TO_CART_MARKERS: tuple[str, ...] = (
    "add to cart", "add to bag", "add to basket", "add to trolley", "add to order",
)
# CHECKOUT / VIEW-CART — the advance controls (whole-word click targets).
CHECKOUT_PHRASES: tuple[str, ...] = (
    "checkout", "check out", "proceed to checkout", "go to checkout",
    "continue to checkout", "proceed to check out", "secure checkout",
)
VIEW_CART_PHRASES: tuple[str, ...] = (
    "view cart", "view basket", "view bag", "go to cart", "go to basket",
    "view my cart", "view my bag", "see cart", "open cart", "review order",
)
# COMMIT — the place-order/pay boundary. NEVER clicked; matched as a broad
# SUBSTRING because over-exclusion is the safe direction. Covers zu_patterns'
# PLACE_ORDER_TOKENS (a drift test enforces that single-sourced guarantee).
COMMIT_MARKERS: tuple[str, ...] = (
    "place order", "place your order", "buy now", "pay", "purchase",
    "complete purchase", "complete order", "confirm order", "confirm and pay",
    "order and pay", "submit order", "checkout & pay", "checkout and pay",
)
# The FINAL-submit subset — a narrow signal that we are AT the checkout page (these
# do not appear on a cart/drawer, unlike express-pay 'pay'/'buy now'/wallet buttons).
PLACE_ORDER_MARKERS: tuple[str, ...] = (
    "place order", "place your order", "complete order", "complete purchase",
    "confirm order", "submit order", "order and pay", "pay now", "complete checkout",
)
# 'Item added' / cart-summary signals — a real cart state. Deliberately NOT bare
# 'cart'/'basket' (a nav link is everywhere); a subtotal/'added to …' means a cart.
IN_CART_SIGNALS: tuple[str, ...] = (
    "added to cart", "added to bag", "added to basket", "added to your", "just added",
    "in your cart", "in your bag", "in your basket", "cart subtotal", "order summary",
    "subtotal",
)
# Cart-count vocabulary — a control labelled 'Cart (2)' / 'Basket 2 items' carries a
# structural count whose INCREASE is a 'took' signal (a before/after delta, #122).
_CART_LABEL_WORDS: tuple[str, ...] = ("cart", "basket", "bag")
# The autocomplete prefix of a card field (cc-number/cc-csc/cc-exp/…) — the
# structural, locale-independent tell that we are AT the payment/commit boundary.
CARD_AUTOCOMPLETE_PREFIX = "cc-"


def is_commit(label: str) -> bool:
    """A committing control (place-order / pay / buy-now / …) we must NEVER click."""
    return contains_any(label, COMMIT_MARKERS)


def is_place_order(label: str) -> bool:
    """A FINAL place-order/submit control — the 'we are at checkout' tell."""
    return contains_any(label, PLACE_ORDER_MARKERS)


def _clickable(view: SurfaceView) -> list[SurfaceAffordance]:
    return [a for a in view.affordances if a.role in CLICKABLE_ROLES]


def texts(view: SurfaceView) -> list[str]:
    return [c.lower() for c in view.context] + [a.label.lower() for a in view.affordances]


def in_cart(view: SurfaceView) -> bool:
    return any(any(sig in text for sig in IN_CART_SIGNALS) for text in texts(view))


def has_card_field(view: SurfaceView) -> bool:
    """A card field is present — the payment/commit boundary. Structural
    (``autocomplete=cc-*``), not the word 'pay', so it holds on a non-English page."""
    return any(
        (a.autocomplete or "").lower().startswith(CARD_AUTOCOMPLETE_PREFIX)
        for a in view.affordances
    )


def at_checkout(view: SurfaceView) -> bool:
    """At the checkout/shipping page: the url says so, or a FINAL place-order
    control is present (express-pay buttons on a cart page are deliberately not)."""
    if "checkout" in view.url.lower():
        return True
    return any(is_place_order(a.label) for a in _clickable(view))


def add_to_cart_handle(view: SurfaceView) -> str | None:
    """The LIVE (non-disabled) add-to-cart control by whole-word name, never a
    committing control. ``None`` when absent or disabled (a required option unmet)."""
    for a in _clickable(view):
        if "disabled" in a.states or is_commit(a.label):
            continue
        if matches_whole_word(a.label, ADD_TO_CART_MARKERS):
            return a.handle
    return None


def advance_handle(view: SurfaceView) -> str | None:
    """The handle that advances one step toward checkout: a whole-word 'Checkout'
    (preferred), else 'View cart'. Never a committing control, never add-to-cart."""
    for phrases in (CHECKOUT_PHRASES, VIEW_CART_PHRASES):
        for a in _clickable(view):
            if is_commit(a.label) or contains_any(a.label, ADD_TO_CART_MARKERS):
                continue
            if matches_whole_word(a.label, phrases):
                return a.handle
    return None


def has_advance_control(view: SurfaceView) -> bool:
    return advance_handle(view) is not None


def cart_count(view: SurfaceView) -> int:
    """The largest count shown on a cart/basket-labelled control (``Cart (2)`` → 2),
    or 0 — a structural number whose INCREASE across an action is a 'took' delta."""
    best = 0
    for a in _clickable(view):
        low = a.label.lower()
        if any(w in low for w in _CART_LABEL_WORDS):
            for n in re.findall(r"\d+", low):
                best = max(best, int(n))
    return best
