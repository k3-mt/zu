"""#125 — ChooseOne: pick ONE from a group of equivalent options (+ content-free hint).

The load-bearing evidence that ONE primitive generalises ACROSS verticals: the same
``choose_one`` call satisfies a shopping variant group (no hint), picks the earliest
appointment slot (positional hint), picks a named service from a list (token hint), and
selects a native <select> option — all content-free, each verified it TOOK. ``inspect``
is pure over a SurfaceView; ``apply`` is driven over a scripted ConnectedSurface.
"""

from __future__ import annotations

from zu_core.ports import InteractionPrimitive, SurfaceAction
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_tools.choose import ChooseOne, resolve


def aff(handle: str, role: str, label: str, *, states: tuple[str, ...] = (),
        group: str | None = None, value: str | None = None) -> SurfaceAffordance:
    return SurfaceAffordance(handle=handle, role=role, label=label, states=states,
                             group=group, value=value)


def view(*affs: SurfaceAffordance, title: str = "", url: str = "") -> SurfaceView:
    return SurfaceView(affordances=tuple(affs), title=title, url=url)


def chosen(v: SurfaceView, hint: str) -> str:
    r = resolve(v, hint)
    assert r is not None
    return r.handle


class ScriptedSurface:
    """perceive/act over a canned view + transitions keyed on (handle, kind) — the
    shipped connected-surface test convention (mirrors test_cart.ScriptedSurface)."""

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


# --- fixtures across verticals ---------------------------------------------- #

# Shopping: a colour swatch group (two grouped options), none selected yet.
_VARIANTS = view(
    aff("c1", "radio", "Black", group="colour"),
    aff("c2", "radio", "Tan", group="colour"),
    aff("atb", "button", "Add to basket"),
)
_VARIANTS_CHOSEN = view(
    aff("c1", "radio", "Black", group="colour", states=("checked",)),
    aff("c2", "radio", "Tan", group="colour"),
    aff("atb", "button", "Add to basket"),
)

# Booking: a time-slot grid of buttons (a real grid — two or more, one sold out).
_TIMES = view(
    aff("t1", "button", "9:00"),
    aff("t2", "button", "9:30"),
    aff("t3", "button", "10:00", states=("disabled",)),
    title="Pick a time", url="clinic/times",
)
_DETAILS = view(aff("nm", "textbox", "Full name"), aff("cb", "button", "Confirm booking"),
                title="Your details", url="clinic/details")

# Booking discovery: a list of services (list items) — the prefix the audit stalled on.
_SERVICES = view(
    aff("s1", "listitem", "Haircut — 30 min"),
    aff("s2", "listitem", "Beard trim — 15 min"),
    aff("s3", "listitem", "Colour & cut — 60 min"),
    title="Services", url="salon/services",
)
_SERVICE_PAGE = view(aff("bk", "button", "Book"), url="salon/haircut")


# --- protocol + group recognition ------------------------------------------- #

def test_choose_one_conforms_to_the_primitive_protocol() -> None:
    c = ChooseOne()
    assert isinstance(c, InteractionPrimitive)
    assert c.kind == "choose_one"


def test_resolve_positional_earliest_picks_the_first_enabled_option() -> None:
    assert chosen(_TIMES, "earliest") == "t1"
    assert chosen(_TIMES, "first") == "t1"
    # 'last' skips over nothing here; the sold-out 10:00 is not enabled, so last enabled is 9:30
    assert chosen(_TIMES, "last") == "t2"


def test_resolve_token_matches_a_service_by_name() -> None:
    assert chosen(_SERVICES, "haircut") == "s1"
    assert chosen(_SERVICES, "beard") == "s2"


def test_resolve_token_respects_word_boundaries_on_times() -> None:
    v = view(aff("x", "button", "19:30"), aff("y", "button", "9:30"))
    assert chosen(v, "9:30") == "y"      # not glued inside 19:30


def test_resolve_returns_none_when_nothing_matches() -> None:
    assert resolve(_SERVICES, "massage") is None
    assert resolve(view(aff("a", "button", "Home")), "earliest") is None  # no group


# --- no hint: the shopping variant path (delegates to the satisfier) --------- #

def test_inspect_no_hint_is_applicable_on_an_unsatisfied_variant_group() -> None:
    assert ChooseOne().inspect(_VARIANTS).applicable is True


def test_inspect_no_hint_is_inert_once_the_group_is_satisfied() -> None:
    assert ChooseOne().inspect(_VARIANTS_CHOSEN).applicable is False


async def test_apply_no_hint_satisfies_the_required_variant_group() -> None:
    surface = ScriptedSurface(_VARIANTS, transitions={("c1", "click"): _VARIANTS_CHOSEN})
    out = await ChooseOne().apply(surface)
    assert out.progress == "advance" and "c1" in out.handles


# --- a hint: the booking + discovery paths, one primitive -------------------- #

async def test_apply_earliest_picks_the_first_slot_and_verifies_advance() -> None:
    # A slot click navigates to the next step (details) — the fingerprint changes, so the
    # pick is verified as an advance even though no 'selected' state toggles.
    surface = ScriptedSurface(_TIMES, transitions={("t1", "click"): _DETAILS})
    out = await ChooseOne().apply(surface, hint="earliest")
    assert surface.acted == [("t1", "click")]
    assert out.progress == "advance" and out.handles == ("t1",)


async def test_apply_token_picks_the_named_service() -> None:
    surface = ScriptedSurface(_SERVICES, transitions={("s1", "click"): _SERVICE_PAGE})
    out = await ChooseOne().apply(surface, hint="haircut")
    assert surface.acted == [("s1", "click")]
    assert out.progress == "advance"


async def test_apply_verifies_a_selected_state_without_navigation() -> None:
    # A swatch that toggles its own 'checked' state in place (no navigation) still verifies.
    surface = ScriptedSurface(_VARIANTS, transitions={("c1", "click"): _VARIANTS_CHOSEN})
    out = await ChooseOne().apply(surface, hint="black")
    assert out.progress == "advance" and out.handles == ("c1",)


async def test_apply_reports_no_op_on_a_silent_click() -> None:
    # Click accepted but nothing changed (no selection, no navigation) — a silent no-op.
    surface = ScriptedSurface(_TIMES)  # no transition -> unchanged
    out = await ChooseOne().apply(surface, hint="earliest")
    assert out.progress == "no_op"


# --- inert on the wrong page (no false choosing) ----------------------------- #

def test_inspect_hint_is_inert_when_no_group_is_present() -> None:
    shop = view(aff("p", "text", "£9.00"), aff("atb", "button", "Add to cart"))
    assert ChooseOne().inspect(shop, hint="earliest").applicable is False


def test_a_lone_slot_shaped_control_is_not_a_group() -> None:
    lone = view(aff("t1", "button", "9:00"), aff("home", "link", "Home"))
    assert resolve(lone, "earliest") is None      # one option is not a choice


def test_choose_one_never_selects_a_committing_control() -> None:
    # A run that includes a place-order button: choose_one must SKIP it — the commit boundary is
    # the host's approval line, never a choosable option (doctrine 3).
    v = view(aff("t1", "button", "9:00"), aff("po", "button", "Place order"),
             aff("t2", "button", "9:30"))
    assert chosen(v, "earliest") == "t1"
    assert chosen(v, "last") == "t2"              # last ENABLED non-commit option, never 'po'
    assert resolve(v, "place order") is None      # a hint can't reach the commit control either


# --- native <select> (combobox) --------------------------------------------- #

async def test_apply_hint_selects_a_native_select_option() -> None:
    combo = view(aff("sz", "combobox", "Size", value=""))
    chosen = view(aff("sz", "combobox", "Size", value="Large"))
    surface = ScriptedSurface(combo, transitions={("sz", "select"): chosen})
    out = await ChooseOne().apply(surface, hint="Large")
    assert surface.acted == [("sz", "select")]
    assert out.progress == "advance"


def test_inspect_no_hint_is_applicable_on_an_unset_select() -> None:
    combo = view(aff("sz", "combobox", "Size", value=""))
    assert ChooseOne().inspect(combo).applicable is True


# --- #127: repeated-card lists + disambiguation by enclosing label ----------

def _card(handle: str, label: str, enclosing: str) -> SurfaceAffordance:
    return SurfaceAffordance(handle=handle, role="button", label=label, enclosing_label=enclosing)


def test_disambiguates_identical_select_buttons_by_card_heading() -> None:
    # A service list whose buttons are ALL 'Select' — selectable only by each card's
    # enclosing heading (#127). The treatwell shape.
    v = view(
        _card("a1", "Select", "Cut & Finish"),
        _card("a2", "Select", "Full Head Colour"),
        _card("a3", "Select", "Blow Dry"),
    )
    assert chosen(v, "Colour") == "a2"     # matched via the enclosing card heading
    assert chosen(v, "Blow Dry") == "a3"


def test_does_not_fold_enclosing_label_when_names_are_distinctive() -> None:
    # No name collision → the enclosing label is NOT folded, so a normal distinctive
    # name is never broadened (a hint matching only the shared card container fails).
    v = view(
        _card("a1", "Haircut", "Salon A"),
        _card("a2", "Colour", "Salon A"),
    )
    assert resolve(v, "Salon") is None
    assert chosen(v, "Haircut") == "a1"


def test_empty_named_option_is_addressable_by_enclosing_label() -> None:
    v = view(_card("a1", "", "Monday 6 Jul"), _card("a2", "", "Tuesday 7 Jul"))
    assert chosen(v, "Tuesday") == "a2"


def test_positional_hint_picks_from_a_repeated_card_list() -> None:
    v = view(_card("a1", "Select", "Cut"), _card("a2", "Select", "Colour"), _card("a3", "Select", "Wax"))
    assert chosen(v, "first") == "a1"
    assert chosen(v, "last") == "a3"


async def test_apply_chooses_the_named_card_and_verifies_advance() -> None:
    before = view(_card("a1", "Select", "Cut & Finish"), _card("a2", "Select", "Full Colour"))
    after = view(SurfaceAffordance(handle="b1", role="heading", label="Choose a time"))
    surface = ScriptedSurface(before, transitions={("a2", "click"): after})

    outcome = await ChooseOne().apply(surface, hint="Colour")

    assert outcome.progress == "advance"
    assert surface.acted == [("a2", "click")]  # picked the 'Full Colour' card, not the first


async def test_apply_hinted_card_that_selects_a_committing_control_is_refused() -> None:
    # A run that includes a committing control must never be chosen by choose_one.
    v = view(
        SurfaceAffordance(handle="a1", role="button", label="Pay now", enclosing_label="Total"),
        SurfaceAffordance(handle="a2", role="button", label="Pay now", enclosing_label="Deposit"),
    )
    assert resolve(v, "Deposit") is None  # committing controls are not candidates
