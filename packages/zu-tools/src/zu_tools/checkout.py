"""checkout — deterministically advance add-to-cart → checkout page (#117).

The connected-surface family clears consent (#94) and satisfies variant selects
(#95/#110/#120) — the PRE-add funnel steps. The step RIGHT AFTER add-to-cart was
still left to the model, and it stalls there: the item is added, a mini-cart drawer
pops, and its 'Checkout' control is never clicked. :class:`WholeWordCheckoutProceeder`
is the deterministic action, the third sibling of
:class:`~zu_core.ports.ConsentResolver` / :class:`~zu_core.ports.SelectionSatisfier`.

  * ``inspect()`` reads a :class:`~zu_core.ports.CheckoutState` off the surface.
  * ``proceed()`` clicks the post-add drawer 'Checkout' (or 'View cart' → then, on a
    second call, its 'Checkout'), chosen by WHOLE-WORD accessible name, and reports
    whether it moved toward checkout.

The commit boundary is absolute and structural: the control is chosen from the
ADVANCE vocabulary with any committing control (place-order / pay) EXCLUDED (all in
:mod:`._commerce`), so ``proceed`` can never cross the place-order/pay step — the
host's approval/vault owns that boundary. Content-free; a bounded funnel step.
"""

from __future__ import annotations

from zu_core.ports import CheckoutState, ConnectedSurface, SurfaceAction
from zu_core.surface import SurfaceView

from ._commerce import advance_handle, at_checkout, in_cart


class WholeWordCheckoutProceeder:
    """The reference :class:`~zu_core.ports.CheckoutProceeder`."""

    __zu_interface__ = 1  # the checkout_proceeders interface major this targets
    name = "whole_word_checkout_proceeder"

    def inspect(self, view: SurfaceView) -> CheckoutState:
        reached = at_checkout(view)
        # No advance handle once we are at checkout: the only step left is the
        # committing one, which the host owns.
        proceed_handle = None if reached else advance_handle(view)
        return CheckoutState(
            in_cart=in_cart(view), at_checkout=reached, proceed_handle=proceed_handle
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
