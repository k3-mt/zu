"""#125 — the interaction-primitive family + the composition runtime.

Each primitive (dismiss / advance / commit_stop / search) self-locates content-free,
applies over a scripted ConnectedSurface, and verifies a success invariant; the runtime
dispatches {kind, hint} over them and reports the applicable SELF-GATING plans. The
cross-vertical evidence is in ``free``: a shopping product surfaces choose_one + advance;
a booking slot grid surfaces NOTHING free (a slot needs a hint — no false auto-clicking);
a payment surface surfaces commit_stop FIRST (the boundary guard).
"""

from __future__ import annotations

from zu_core.ports import (
    InteractionPrimitive,
    PrimitiveRuntime,
    SurfaceAction,
)
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_tools.primitives import (
    AdvancePrimitive,
    CommitStopPrimitive,
    DismissPrimitive,
    SearchPrimitive,
    StandardPrimitiveRuntime,
)


def aff(handle: str, role: str, label: str, *, states: tuple[str, ...] = (),
        group: str | None = None, autocomplete: str | None = None) -> SurfaceAffordance:
    return SurfaceAffordance(handle=handle, role=role, label=label, states=states,
                             group=group, autocomplete=autocomplete)


def view(*affs: SurfaceAffordance, context: tuple[str, ...] = (), url: str = "") -> SurfaceView:
    return SurfaceView(affordances=tuple(affs), context=context, url=url)


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


# --- fixtures ---------------------------------------------------------------- #

_BANNER = view(aff("ok", "button", "Accept all cookies"), aff("no", "button", "Decline"),
               aff("p", "link", "Product"))
_CLEARED = view(aff("p", "link", "Product"))

_PRODUCT = view(aff("atb", "button", "Add to basket"), aff("home", "link", "Home"))
_DRAWER = view(aff("co", "button", "Checkout"), context=("added to cart", "Subtotal £24"))
_CHECKOUT = view(aff("po", "button", "Place order"), url="shop/checkout")

_PAYMENT = view(aff("cn", "textbox", "Card number", autocomplete="cc-number"),
                aff("pay", "button", "Pay now"))

_SEARCH = view(aff("q", "searchbox", "Search"), aff("btn", "button", "Search"))
_SEARCH_NO_BTN = view(aff("q", "searchbox", "Search"))
_RESULTS = view(aff("r1", "link", "Result one"), aff("r2", "link", "Result two"), url="q=x")
_LOGIN = view(aff("u", "textbox", "Email"),
              aff("pw", "textbox", "Password", states=("password",)),
              aff("s", "button", "Search"))

# Booking slot grid (a real grid — needs a hint; nothing fires free on it).
_TIMES = view(aff("t1", "button", "9:00"), aff("t2", "button", "9:30"), url="clinic/times")
_DETAILS = view(aff("nm", "textbox", "Full name"), url="clinic/details")

# Shopping product with an unmet variant group AND an add-to-cart.
_VARIANT_PRODUCT = view(
    aff("c1", "radio", "Black", group="colour"), aff("c2", "radio", "Tan", group="colour"),
    aff("atb", "button", "Add to basket"),
)


# --- dismiss ----------------------------------------------------------------- #

def test_dismiss_conforms_and_inspects() -> None:
    d = DismissPrimitive()
    assert isinstance(d, InteractionPrimitive) and d.kind == "dismiss"
    assert d.inspect(_BANNER).applicable is True
    assert d.inspect(_PRODUCT).applicable is False


async def test_dismiss_clears_a_banner() -> None:
    surface = ScriptedSurface(_BANNER, transitions={("ok", "click"): _CLEARED})
    out = await DismissPrimitive().apply(surface)
    assert out.progress == "advance" and surface.acted == [("ok", "click")]


# --- advance (add-to-cart + proceed-to-checkout, unified) -------------------- #

def test_advance_inspects_add_to_cart_on_a_product() -> None:
    plan = AdvancePrimitive().inspect(_PRODUCT)
    assert plan.applicable and plan.handles == ("atb",)


def test_advance_inspects_proceed_in_the_cart() -> None:
    plan = AdvancePrimitive().inspect(_DRAWER)
    assert plan.applicable and plan.handles == ("co",)


def test_advance_is_inert_on_a_booking_slot_grid() -> None:
    assert AdvancePrimitive().inspect(_TIMES).applicable is False


async def test_advance_adds_to_cart_then_proceeds() -> None:
    surface = ScriptedSurface(_PRODUCT, transitions={("atb", "click"): _DRAWER})
    assert (await AdvancePrimitive().apply(surface)).progress == "advance"
    surface2 = ScriptedSurface(_DRAWER, transitions={("co", "click"): _CHECKOUT})
    assert (await AdvancePrimitive().apply(surface2)).progress == "advance"


def test_advance_never_targets_a_committing_control() -> None:
    # A cart page whose only control is 'Place order' — advance must NOT offer it.
    assert AdvancePrimitive().inspect(_CHECKOUT).applicable is False


# --- commit_stop ------------------------------------------------------------- #

def test_commit_stop_recognises_the_payment_boundary() -> None:
    plan = CommitStopPrimitive().inspect(_PAYMENT)
    assert plan.applicable and "pay" in plan.handles
    assert CommitStopPrimitive().inspect(_PRODUCT).applicable is False


async def test_commit_stop_stops_and_never_acts() -> None:
    surface = ScriptedSurface(_PAYMENT)
    out = await CommitStopPrimitive().apply(surface)
    assert out.progress == "commit_stop"
    assert surface.acted == []   # the boundary is never crossed


# --- search ------------------------------------------------------------------ #

def test_search_recognises_a_box_and_is_inert_on_login() -> None:
    assert SearchPrimitive().inspect(_SEARCH).applicable is True
    assert SearchPrimitive().inspect(_LOGIN).applicable is False   # a password -> login


async def test_search_types_and_clicks_submit() -> None:
    surface = ScriptedSurface(_SEARCH, transitions={("btn", "click"): _RESULTS})
    out = await SearchPrimitive().apply(surface, hint="dog collar")
    assert out.progress == "advance"
    assert surface.acted == [("q", "type"), ("btn", "click")]


async def test_search_submits_with_enter_when_no_button() -> None:
    surface = ScriptedSurface(_SEARCH_NO_BTN, transitions={("q", "submit"): _RESULTS})
    out = await SearchPrimitive().apply(surface, hint="collar")
    assert out.progress == "advance"
    assert surface.acted == [("q", "type"), ("q", "submit")]


async def test_search_with_no_query_is_a_no_op() -> None:
    surface = ScriptedSurface(_SEARCH)
    assert (await SearchPrimitive().apply(surface)).progress == "no_op"


# --- the composition runtime ------------------------------------------------- #

def test_runtime_conforms_to_the_protocol() -> None:
    assert isinstance(StandardPrimitiveRuntime(), PrimitiveRuntime)


def test_runtime_get_resolves_kinds() -> None:
    rt = StandardPrimitiveRuntime()
    p = rt.get("choose_one")
    assert p is not None and p.kind == "choose_one"
    assert rt.get("nope") is None


def test_free_surfaces_shopping_mechanics_in_priority_order() -> None:
    plans = StandardPrimitiveRuntime().free(_VARIANT_PRODUCT)
    kinds = [p.kind for p in plans]
    assert kinds == ["choose_one", "advance"]   # satisfy the variant, then advance


def test_free_is_empty_on_a_booking_slot_grid() -> None:
    # A slot grid needs a HINT (host/model directed) — nothing auto-fires, so the drive
    # never blindly clicks a time. This is the inert-on-wrong-page guard for booking.
    assert StandardPrimitiveRuntime().free(_TIMES) == ()


def test_free_guards_the_commit_boundary_first() -> None:
    plans = StandardPrimitiveRuntime().free(_PAYMENT)
    assert plans and plans[0].kind == "commit_stop"


async def test_step_runs_a_hinted_choose_one_on_a_slot_grid() -> None:
    surface = ScriptedSurface(_TIMES, transitions={("t1", "click"): _DETAILS})
    out = await StandardPrimitiveRuntime().step(surface, "choose_one", hint="earliest")
    assert out.progress == "advance" and out.handles == ("t1",)


async def test_step_on_an_unknown_kind_is_a_no_op() -> None:
    out = await StandardPrimitiveRuntime().step(ScriptedSurface(_TIMES), "teleport")
    assert out.progress == "no_op"
