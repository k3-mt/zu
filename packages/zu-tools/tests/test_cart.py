"""#122 — WholeWordCartAdder: product → cart, recognise → click → VERIFY it took.

``inspect`` is pure over a SurfaceView; ``add`` is driven over a scripted
ConnectedSurface. The load-bearing test is the VERIFY: a genuine before/after delta
counts, a persistent header 'View basket' does not, and a silent no-op returns False.
"""

from __future__ import annotations

from zu_core.ports import CartAdder, SurfaceAction
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_tools.cart import WholeWordCartAdder


def aff(handle: str, role: str, label: str, *, states: tuple[str, ...] = ()) -> SurfaceAffordance:
    return SurfaceAffordance(handle=handle, role=role, label=label, states=states)


def view(*affs: SurfaceAffordance, context: tuple[str, ...] = ()) -> SurfaceView:
    return SurfaceView(affordances=tuple(affs), context=context)


class ScriptedSurface:
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


_PRODUCT = view(aff("a1", "button", "Add to cart"), aff("a2", "link", "Home"))
_DRAWER = view(aff("b1", "button", "Checkout"), aff("b2", "link", "View cart"),
               context=("Leather collar added to your cart", "Subtotal £24"))


# --- inspect() --------------------------------------------------------------

def test_adder_conforms_to_protocol() -> None:
    assert isinstance(WholeWordCartAdder(), CartAdder)


def test_inspect_finds_a_live_add_to_cart_control() -> None:
    state = WholeWordCartAdder().inspect(_PRODUCT)
    assert state.handle == "a1"
    assert state.added is False


def test_inspect_ignores_a_disabled_add_to_cart_control() -> None:
    v = view(aff("a1", "button", "Add to cart", states=("disabled",)))
    assert WholeWordCartAdder().inspect(v).handle is None  # required option unmet


def test_inspect_never_returns_a_committing_control() -> None:
    v = view(aff("a1", "button", "Buy now"), aff("a2", "button", "Place order"))
    assert WholeWordCartAdder().inspect(v).handle is None


# --- add() ------------------------------------------------------------------

async def test_add_clicks_and_verifies_a_took_via_new_signal() -> None:
    surface = ScriptedSurface(_PRODUCT, transitions={("a1", "click"): _DRAWER})
    assert await WholeWordCartAdder().add(surface) is True
    assert surface.acted == [("a1", "click")]


async def test_add_silent_no_op_returns_false() -> None:
    # Click accepted but the surface does not change (a required option is unmet and
    # the button no-ops) — NOT a success, so the host can satisfy the option + retry.
    surface = ScriptedSurface(_PRODUCT)  # no transition -> unchanged
    assert await WholeWordCartAdder().add(surface) is False
    assert surface.acted == [("a1", "click")]


async def test_add_persistent_view_basket_header_is_not_a_took_signal() -> None:
    # A header 'View basket' link is present BEFORE and AFTER — no delta, so the
    # verify must not read it as success.
    before = view(aff("a1", "button", "Add to cart"), aff("a2", "link", "View basket"))
    after = view(aff("a1", "button", "Add to cart"), aff("a2", "link", "View basket"))
    surface = ScriptedSurface(before, transitions={("a1", "click"): after})
    assert await WholeWordCartAdder().add(surface) is False


async def test_add_verifies_a_took_via_cart_count_increase() -> None:
    before = view(aff("a1", "button", "Add to cart"), aff("a2", "link", "Cart (0)"))
    after = view(aff("a1", "button", "Add to cart"), aff("a2", "link", "Cart (1)"))
    surface = ScriptedSurface(before, transitions={("a1", "click"): after})
    assert await WholeWordCartAdder().add(surface) is True


async def test_add_returns_false_and_never_clicks_when_no_add_to_cart() -> None:
    surface = ScriptedSurface(view(aff("a1", "button", "Place order")))
    assert await WholeWordCartAdder().add(surface) is False
    assert surface.acted == []  # a committing control is never clicked
