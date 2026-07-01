"""#117 — WholeWordCheckoutProceeder: advance add-to-cart → checkout, never commit.

``inspect()`` is pure over a SurfaceView; ``proceed()`` orchestrates perceive/act,
tested over a scripted in-memory ConnectedSurface. The load-bearing test is that
``proceed`` NEVER clicks a committing (place-order/pay) control — proven directly
and single-sourced against zu_patterns' commit vocabulary by a drift guard.
"""

from __future__ import annotations

from zu_core.ports import CheckoutProceeder, SurfaceAction
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_tools._commerce import is_commit as _is_commit
from zu_tools.checkout import WholeWordCheckoutProceeder


def aff(handle: str, role: str, label: str) -> SurfaceAffordance:
    return SurfaceAffordance(handle=handle, role=role, label=label)


def view(*affs: SurfaceAffordance, url: str = "", context: tuple[str, ...] = ()) -> SurfaceView:
    return SurfaceView(url=url, affordances=tuple(affs), context=context)


class ScriptedSurface:
    """In-memory ConnectedSurface: act() swaps to the view registered for
    (handle, kind), else stays put."""

    def __init__(self, initial: SurfaceView,
                 transitions: dict[tuple[str, str], SurfaceView] | None = None) -> None:
        self._view = initial
        self._transitions = transitions or {}
        self.acted: list[tuple[str, str]] = []

    async def perceive(self) -> SurfaceView:
        return self._view

    async def act(self, action: SurfaceAction) -> SurfaceView:
        self.acted.append((action.handle, action.kind))
        self._view = self._transitions.get((action.handle, action.kind), self._view)
        return self._view


_DRAWER = view(
    aff("a1", "button", "Checkout"),
    aff("a2", "link", "View cart"),
    aff("a3", "link", "Continue shopping"),
    context=("Leather Dog Collar added to your cart", "Subtotal £24.00"),
)
_CHECKOUT_PAGE = view(
    aff("b1", "textbox", "Email"),
    aff("b2", "button", "Place order"),
    url="https://shop.test/checkouts/abc123",
    context=("Shipping address", "Order summary"),
)


def test_proceeder_conforms_to_protocol() -> None:
    assert isinstance(WholeWordCheckoutProceeder(), CheckoutProceeder)


# --- inspect() --------------------------------------------------------------

def test_inspect_reads_the_post_add_drawer() -> None:
    state = WholeWordCheckoutProceeder().inspect(_DRAWER)
    assert state.in_cart is True
    assert state.at_checkout is False
    assert state.proceed_handle == "a1"  # the drawer 'Checkout', not 'View cart'


def test_inspect_at_checkout_offers_no_advance_handle() -> None:
    state = WholeWordCheckoutProceeder().inspect(_CHECKOUT_PAGE)
    assert state.at_checkout is True
    assert state.proceed_handle is None  # the only step left is committing — host owns it


def test_inspect_prefers_view_cart_when_no_direct_checkout() -> None:
    v = view(aff("a1", "link", "View cart"), aff("a2", "button", "Add to cart"),
             context=("Subtotal £10",))
    assert WholeWordCheckoutProceeder().inspect(v).proceed_handle == "a1"


def test_inspect_never_selects_a_committing_control() -> None:
    # 'Buy now' / 'Checkout & Pay' are committing — even the latter matching
    # 'checkout' must be excluded; here no safe advance exists.
    v = view(aff("a1", "button", "Buy now"), aff("a2", "button", "Checkout & Pay"),
             context=("Subtotal £10",))
    assert WholeWordCheckoutProceeder().inspect(v).proceed_handle is None


def test_inspect_advance_matches_whole_words_only() -> None:
    v = view(aff("a1", "link", "Checkoutopia"), aff("a2", "button", "Add to cart"))
    assert WholeWordCheckoutProceeder().inspect(v).proceed_handle is None  # not 'checkout'


# --- proceed() --------------------------------------------------------------

async def test_proceed_clicks_drawer_checkout_and_reaches_checkout_page() -> None:
    surface = ScriptedSurface(_DRAWER, transitions={("a1", "click"): _CHECKOUT_PAGE})
    assert await WholeWordCheckoutProceeder().proceed(surface) is True
    assert surface.acted == [("a1", "click")]


async def test_proceed_view_cart_advances_one_step_to_a_cart_page() -> None:
    cart_page = view(aff("c1", "button", "Checkout"), aff("c2", "button", "Update cart"),
                     url="https://shop.test/cart", context=("Order summary",))
    surface = ScriptedSurface(
        view(aff("a1", "link", "View cart"), context=("Item added to your bag",)),
        transitions={("a1", "click"): cart_page},
    )
    # Moved toward checkout (surface advanced, a further 'Checkout' step now present).
    assert await WholeWordCheckoutProceeder().proceed(surface) is True
    assert surface.acted == [("a1", "click")]


async def test_proceed_stops_at_checkout_and_never_clicks_place_order() -> None:
    surface = ScriptedSurface(_CHECKOUT_PAGE)
    assert await WholeWordCheckoutProceeder().proceed(surface) is False
    assert surface.acted == []  # the committing 'Place order' was never touched


async def test_proceed_refuses_a_page_whose_only_control_commits() -> None:
    only_commit = view(aff("a1", "button", "Place order"), context=("Order summary",))
    surface = ScriptedSurface(only_commit)
    assert await WholeWordCheckoutProceeder().proceed(surface) is False
    assert surface.acted == []


async def test_proceed_returns_false_when_no_advance_control() -> None:
    surface = ScriptedSurface(view(aff("a1", "link", "Continue shopping")))
    assert await WholeWordCheckoutProceeder().proceed(surface) is False
    assert surface.acted == []


# --- the commit boundary, single-sourced ------------------------------------

def test_commit_guard_covers_zu_patterns_commit_vocabulary() -> None:
    # The safety guarantee is single-sourced: whatever cart_checkout classifies as
    # the committing place-order/pay step, this proceeder must also refuse to click.
    from zu_patterns._match import PLACE_ORDER_TOKENS

    for token in PLACE_ORDER_TOKENS:
        assert _is_commit(token), f"commit guard missed {token!r} from zu_patterns"


def test_commit_guard_flags_common_commit_labels() -> None:
    for label in ("Place order", "Pay now", "Buy now", "Complete order", "Checkout & Pay"):
        assert _is_commit(label)
    for label in ("Checkout", "View cart", "Continue shopping"):
        assert not _is_commit(label)
