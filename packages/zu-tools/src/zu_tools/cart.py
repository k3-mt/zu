"""cart — deterministic product → cart: recognise, click, verify it took (#122).

The step BEFORE :class:`~zu_core.ports.CheckoutProceeder` (#117): add the current
product to the cart. Left to the model it is feedback-blind — a required option
leaves the click a silent no-op and the drive loops. :class:`WholeWordCartAdder`
makes it deterministic and, crucially, VERIFIES the click took via a genuine
before/after delta, so a silent no-op returns False (the host satisfies the option
and retries) rather than a false success. Short of the commit boundary — the host's
vault/approval still owns pay.
"""

from __future__ import annotations

from zu_core.ports import CartAddition, ConnectedSurface, SurfaceAction
from zu_core.surface import SurfaceView

from ._commerce import add_to_cart_handle, cart_count, has_advance_control, in_cart


def _took(before: SurfaceView, after: SurfaceView) -> bool:
    """A genuine before/after DELTA that add-to-cart took — never a PERSISTENT
    signal (a header 'View basket' link present in BOTH does not count):
      * a NEW 'added to cart' / mini-cart / cart-summary signal appeared, or
      * a NEW advance (checkout / view-cart) control appeared, or
      * the cart count went up."""
    if in_cart(after) and not in_cart(before):
        return True
    if has_advance_control(after) and not has_advance_control(before):
        return True
    return cart_count(after) > cart_count(before)


class WholeWordCartAdder:
    """The reference :class:`~zu_core.ports.CartAdder`."""

    __zu_interface__ = 1  # the cart_adders interface major this targets
    name = "whole_word_cart_adder"

    def inspect(self, view: SurfaceView) -> CartAddition:
        return CartAddition(added=in_cart(view), handle=add_to_cart_handle(view))

    async def add(self, surface: ConnectedSurface) -> bool:
        view = await surface.perceive()
        handle = add_to_cart_handle(view)
        if handle is None:
            return False  # no LIVE add-to-cart control (absent, or a required option unmet)
        after = await surface.act(SurfaceAction(handle=handle, kind="click"))
        return _took(view, after)
