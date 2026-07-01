"""checkout — deterministically advance add-to-cart → checkout page (#117).

The connected-surface family clears consent (#94) and satisfies variant selects
(#95/#110) — the PRE-add funnel steps. The step RIGHT AFTER add-to-cart was still
left to the model, and it stalls there: the item is added, a mini-cart drawer
pops, and its 'Checkout' control is never clicked (a real Shopify drive looped
until it escalated). :class:`WholeWordCheckoutProceeder` is the deterministic
action that ties the recognised cart/checkout cluster to a step — the third
sibling of :class:`~zu_core.ports.ConsentResolver` /
:class:`~zu_core.ports.SelectionSatisfier`.

  * ``inspect()`` reads a :class:`~zu_core.ports.CheckoutState` off the surface —
    is the item in the cart, are we already at the checkout page, and which
    control advances one step toward it.
  * ``proceed()`` clicks that control (the drawer/mini-cart 'Checkout', or a
    'View cart' → then, on a second call, its 'Checkout'), chosen by WHOLE-WORD
    accessible name, and reports whether it moved toward checkout.

The commit boundary is absolute and structural, NOT a property of the fuzzy
``at_checkout`` signal: the control we click is chosen from the ADVANCE vocabulary
with any committing control (place-order / pay / buy-now / complete-order)
EXCLUDED — so ``proceed`` can never cross the place-order/pay step regardless of
what page we think we are on. That step is the host's approval/vault boundary.
Content-free; a bounded funnel step, not goal orchestration.
"""

from __future__ import annotations

from zu_core.ports import CheckoutState, ConnectedSurface, SurfaceAction
from zu_core.surface import SurfaceView

from ._wholeword import contains_any, matches_whole_word

# ADVANCE — controls we WILL click, matched on WHOLE WORDS (a click target must be
# precise). 'View cart' is the two-step path (view cart → then its checkout).
_CHECKOUT_PHRASES: tuple[str, ...] = (
    "checkout", "check out", "proceed to checkout", "go to checkout",
    "continue to checkout", "proceed to check out", "secure checkout",
)
_VIEW_CART_PHRASES: tuple[str, ...] = (
    "view cart", "view basket", "view bag", "go to cart", "go to basket",
    "view my cart", "view my bag", "see cart", "open cart", "review order",
)

# COMMIT — the place-order/pay boundary. NEVER clicked; matched as a broad
# SUBSTRING because over-exclusion is the safe direction (skip a control, never
# click a committing one). Must cover zu_patterns' PLACE_ORDER_TOKENS — a drift
# test enforces that single-sourced guarantee.
_COMMIT_MARKERS: tuple[str, ...] = (
    "place order", "place your order", "buy now", "pay", "purchase",
    "complete purchase", "complete order", "confirm order", "confirm and pay",
    "order and pay", "submit order", "checkout & pay", "checkout and pay",
)
# The FINAL-submit subset — a narrow signal that we are AT the checkout page (these
# do not appear on a cart/drawer, unlike express-pay 'pay'/'buy now'/wallet buttons).
_PLACE_ORDER_MARKERS: tuple[str, ...] = (
    "place order", "place your order", "complete order", "complete purchase",
    "confirm order", "submit order", "order and pay", "pay now", "complete checkout",
)
# ADD-TO-CART — excluded from advance (clicking it again is not "proceeding").
_ADD_TO_CART_MARKERS: tuple[str, ...] = (
    "add to cart", "add to bag", "add to basket", "add to trolley",
)
# 'Item added' / cart-summary signals — evidence add-to-cart took. Deliberately
# NOT bare 'cart'/'basket' (a nav link is everywhere); a subtotal/order-summary or
# an 'added to …' message means a real cart state.
_IN_CART_SIGNALS: tuple[str, ...] = (
    "added to cart", "added to bag", "added to basket", "added to your", "just added",
    "in your cart", "in your bag", "in your basket", "cart subtotal", "order summary",
    "subtotal",
)
_CLICKABLE_ROLES: frozenset[str] = frozenset({"button", "link", "menuitem"})


def _is_commit(label: str) -> bool:
    """A committing control (place-order / pay / buy-now / …) we must NEVER click."""
    return contains_any(label, _COMMIT_MARKERS)


def _texts(view: SurfaceView) -> list[str]:
    return [c.lower() for c in view.context] + [a.label.lower() for a in view.affordances]


def _in_cart(view: SurfaceView) -> bool:
    return any(any(sig in text for sig in _IN_CART_SIGNALS) for text in _texts(view))


def _at_checkout(view: SurfaceView) -> bool:
    """At the checkout/shipping page: the url says so, or a FINAL place-order
    control is present. (Express-pay buttons on a cart page are deliberately NOT a
    signal here — they would falsely stop us short of the real Checkout.)"""
    if "checkout" in view.url.lower():
        return True
    return any(
        contains_any(a.label, _PLACE_ORDER_MARKERS)
        for a in view.affordances
        if a.role in _CLICKABLE_ROLES
    )


def _advance_handle(view: SurfaceView) -> str | None:
    """The handle that advances one step toward checkout: a whole-word 'Checkout'
    (preferred), else a 'View cart'. Never a committing control, never add-to-cart."""
    for phrases in (_CHECKOUT_PHRASES, _VIEW_CART_PHRASES):
        for a in view.affordances:
            if a.role not in _CLICKABLE_ROLES:
                continue
            if _is_commit(a.label) or contains_any(a.label, _ADD_TO_CART_MARKERS):
                continue
            if matches_whole_word(a.label, phrases):
                return a.handle
    return None


class WholeWordCheckoutProceeder:
    """The reference :class:`~zu_core.ports.CheckoutProceeder`."""

    __zu_interface__ = 1  # the checkout_proceeders interface major this targets
    name = "whole_word_checkout_proceeder"

    def inspect(self, view: SurfaceView) -> CheckoutState:
        at_checkout = _at_checkout(view)
        # No advance handle once we are at checkout: the only step left is the
        # committing one, which the host owns.
        proceed_handle = None if at_checkout else _advance_handle(view)
        return CheckoutState(
            in_cart=_in_cart(view), at_checkout=at_checkout, proceed_handle=proceed_handle
        )

    async def proceed(self, surface: ConnectedSurface) -> bool:
        view = await surface.perceive()
        state = self.inspect(view)
        if state.proceed_handle is None:
            return False  # nothing safe to advance (at checkout, or no advance control)
        after = await surface.act(SurfaceAction(handle=state.proceed_handle, kind="click"))
        after_state = self.inspect(after)
        # Moved toward checkout: we are now AT the checkout page, or the surface
        # advanced and still offers a further safe step (drawer → cart → checkout).
        return after_state.at_checkout or (
            after.fingerprint() != view.fingerprint() and after_state.proceed_handle is not None
        )
