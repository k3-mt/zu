"""#95/#110 — FirstOptionSelectionSatisfier: satisfy unset variant selects.

The browser-side option picker (only-set-if-unset, first valid option) is JS that
cannot run offline, so the fake ConnectedSurface models exactly that DOM contract:
a ``select`` act sets a control only while it is unset. The tests verify the
satisfier's orchestration — it targets single-choice selects regardless of the
HTML ``required`` flag (#110), sets each unset one, reports what changed, and is
robust to real-page hazards: many selects sharing one placeholder label, and
handle renumbering between acts.
"""

from __future__ import annotations

from typing import Any

from zu_core.ports import SelectionSatisfier, SurfaceAction
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_tools.selection import FirstOptionSelectionSatisfier, _is_select_candidate, _took


class VariantSurface:
    """In-memory ConnectedSurface over native <select>s. A ``select`` act sets a
    control's value to its first option ONLY while it is unset — mirroring
    ``_SELECT_FN``'s DOM check (a placeholder has ``value === ''``). Handles are
    stable (the control's own key)."""

    def __init__(self, controls: list[dict[str, Any]]) -> None:
        self._controls = controls
        self.selected: list[str] = []  # handles that received a select act

    async def perceive(self) -> SurfaceView:
        return SurfaceView(affordances=tuple(self._affordance(c) for c in self._controls))

    @staticmethod
    def _affordance(c: dict[str, Any]) -> SurfaceAffordance:
        return SurfaceAffordance(
            handle=c["handle"], role=c["role"], label=c["label"], value=c["value"],
            states=tuple(c.get("states", ())),
        )

    async def act(self, action: SurfaceAction) -> SurfaceView:
        if action.kind == "select":
            self.selected.append(action.handle)
            for c in self._controls:
                if c["handle"] == action.handle and c.get("unset"):
                    c["value"] = c["first"]
                    c["unset"] = False
        return await self.perceive()


class RenumberingVariantSurface(VariantSurface):
    """Like VariantSurface, but the FIRST successful select enables an add-to-basket
    button that is prepended to the surface — so every subsequent perceive
    RENUMBERS the selects' handles. A satisfier that snapshots handles from the
    first perceive would then act on the wrong control; one that re-reads the
    current handle each pass survives."""

    def __init__(self, controls: list[dict[str, Any]]) -> None:
        super().__init__(controls)
        self._button = False

    async def perceive(self) -> SurfaceView:
        affs = [VariantSurface._affordance(c) for c in self._controls]
        if self._button:
            affs.insert(0, SurfaceAffordance(handle="_", role="button", label="Add to basket"))
        # Reassign handles a1..aN in current document order — the renumber.
        renumbered = tuple(a.model_copy(update={"handle": f"a{i + 1}"}) for i, a in enumerate(affs))
        return SurfaceView(affordances=renumbered)

    async def act(self, action: SurfaceAction) -> SurfaceView:
        # Resolve the CURRENT handle to a control via the latest perceive, then set.
        view = await self.perceive()
        target = next((a for a in view.affordances if a.handle == action.handle), None)
        if action.kind == "select" and target is not None:
            for c in self._controls:
                if c["label"] == target.label and c["value"] == target.value and c.get("unset"):
                    c["value"] = c["first"]
                    c["unset"] = False
                    self._button = True
                    break
        return await self.perceive()


def sel(handle: str, label: str = "Choose an option", *, first: str,
        unset: bool = True, value: str | None = None, disabled: bool = False) -> dict[str, Any]:
    return {"handle": handle, "role": "combobox", "label": label,
            "value": value if value is not None else ("" if unset else first),
            "states": (["disabled"] if disabled else []), "unset": unset, "first": first}


def test_satisfier_conforms_to_protocol() -> None:
    assert isinstance(FirstOptionSelectionSatisfier(), SelectionSatisfier)


async def test_sets_unset_selects_regardless_of_required_flag() -> None:
    # #110: none of these carry the HTML `required` state, yet all unset ones must
    # be satisfied. Already-set and disabled selects and non-selects are left alone.
    surface = VariantSurface([
        sel("a1", "Colour", first="Red"),
        sel("a2", "Size", first="Small"),
        sel("a3", "Style", unset=False, value="Modern", first="Classic"),   # already chosen
        sel("a4", "Locked", first="X", disabled=True),                      # disabled
        {"handle": "a5", "role": "button", "label": "Add to basket", "value": None},
    ])

    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)

    assert {r.handle: r.chosen_label for r in results} == {"a1": "Red", "a2": "Small"}
    assert "a3" in surface.selected and "a4" not in surface.selected  # set acted (no-op), disabled skipped


async def test_sets_four_variant_selects_that_share_one_placeholder_label() -> None:
    # The real WooCommerce shape: four required variant selects, every one showing
    # the same 'Choose an option' placeholder. Addressing by (role,label) would keep
    # re-finding the first; the positional pass must set all four.
    surface = VariantSurface([
        sel("a1", first="Black"),
        sel("a2", first="Brass"),
        sel("a3", first="13mm"),
        sel("a4", first="25cm"),
    ])

    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)

    assert [r.chosen_label for r in results] == ["Black", "Brass", "13mm", "25cm"]
    assert surface.selected == ["a1", "a2", "a3", "a4"]


async def test_survives_handle_renumbering_between_acts() -> None:
    surface = RenumberingVariantSurface([
        sel("c1", first="Black"),
        sel("c2", first="Brass"),
    ])
    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)
    assert sorted(r.chosen_label for r in results) == ["Black", "Brass"]


async def test_no_selects_returns_empty() -> None:
    surface = VariantSurface([{"handle": "a1", "role": "button", "label": "Buy", "value": None}])
    assert await FirstOptionSelectionSatisfier().satisfy_required(surface) == []


# --- the content-free predicates, directly ----------------------------------

def _aff(role: str, *, states: tuple[str, ...] = (), value: str | None = None) -> SurfaceAffordance:
    return SurfaceAffordance(handle="h", role=role, label="Colour", value=value, states=states)


def test_is_select_candidate_is_any_enabled_native_select_not_gated_on_required() -> None:
    assert _is_select_candidate(_aff("combobox"))                     # #110: no `required` needed
    assert _is_select_candidate(_aff("combobox", states=("required",)))
    assert not _is_select_candidate(_aff("textbox"))                  # wrong role
    assert not _is_select_candidate(_aff("listbox"))                  # ARIA listbox: follow-up
    assert not _is_select_candidate(_aff("combobox", states=("disabled",)))


def test_took_is_a_nonempty_changed_value() -> None:
    before = _aff("combobox", value="Choose an option")
    assert _took(before, _aff("combobox", value="Red"))          # placeholder -> option
    assert not _took(before, _aff("combobox", value="Choose an option"))  # unchanged
    assert not _took(before, _aff("combobox", value=""))         # empty is not a real choice


# --- #120: custom swatch / radio-group pickers ------------------------------

class SwatchSurface:
    """In-memory ConnectedSurface over swatch/radio groups. A ``click`` selects the
    option and deselects its GROUP siblings (single-choice). Options are dicts:
    {handle, role, label, group, selected?, disabled?}."""

    def __init__(self, options: list[dict[str, Any]], *, renumber: bool = False) -> None:
        self._options = options
        self._renumber = renumber
        self.clicks: list[str] = []
        self._extra = False  # a control that appears after the first click (forces renumber)

    async def perceive(self) -> SurfaceView:
        affs = [
            SurfaceAffordance(
                handle=o["handle"], role=o["role"], label=o["label"], group=o.get("group"),
                states=tuple((["selected"] if o.get("selected") else [])
                             + (["disabled"] if o.get("disabled") else [])),
            )
            for o in self._options
        ]
        if self._renumber and self._extra:
            affs.insert(0, SurfaceAffordance(handle="_", role="button", label="Add to basket"))
            affs = [a.model_copy(update={"handle": f"h{i + 1}"}) for i, a in enumerate(affs)]
        return SurfaceView(affordances=tuple(affs))

    async def act(self, action: SurfaceAction) -> SurfaceView:
        if action.kind == "click":
            self.clicks.append(action.handle)
            view = await self.perceive()
            target = next((a for a in view.affordances if a.handle == action.handle), None)
            if target is not None and "disabled" not in target.states:
                for o in self._options:
                    if o.get("group") == target.group:
                        o["selected"] = o["label"] == target.label
                self._extra = True
        return await self.perceive()


class NoOpSwatchSurface(SwatchSurface):
    """A broken swatch: the click is accepted but nothing becomes selected — the
    silent no-op #39 must catch (and #120 must NOT report as success)."""

    async def act(self, action: SurfaceAction) -> SurfaceView:
        self.clicks.append(action.handle)
        return await self.perceive()


def swatch(handle: str, label: str, group: str, *, selected: bool = False,
           disabled: bool = False, role: str = "radio") -> dict[str, Any]:
    return {"handle": handle, "role": role, "label": label, "group": group,
            "selected": selected, "disabled": disabled}


async def test_sets_two_swatch_groups_separated_only_by_group_id() -> None:
    # colour and size are BOTH role=radio and contiguous — a flat list can't tell
    # them apart. The group id does; both groups must be satisfied.
    surface = SwatchSurface([
        swatch("a1", "Red", "g:colour"), swatch("a2", "Green", "g:colour"),
        swatch("a3", "Small", "g:size"), swatch("a4", "Large", "g:size"),
    ])
    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)
    assert [r.chosen_label for r in results] == ["Red", "Small"]  # first enabled of each group


async def test_skips_a_group_that_already_has_a_selection() -> None:
    surface = SwatchSurface([
        swatch("a1", "Red", "g:colour", selected=True), swatch("a2", "Green", "g:colour"),
        swatch("a3", "Small", "g:size"), swatch("a4", "Large", "g:size"),
    ])
    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)
    assert [r.chosen_label for r in results] == ["Small"]   # colour already chosen
    assert "a2" not in surface.clicks                        # colour group untouched


async def test_picks_first_enabled_option_skipping_disabled() -> None:
    surface = SwatchSurface([
        swatch("a1", "Red", "g:colour", disabled=True),
        swatch("a2", "Green", "g:colour"),
    ])
    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)
    assert [r.chosen_label for r in results] == ["Green"]
    assert "a1" not in surface.clicks


async def test_silent_no_op_swatch_is_not_reported_as_success() -> None:
    surface = NoOpSwatchSurface([swatch("a1", "Red", "g:colour"), swatch("a2", "Green", "g:colour")])
    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)
    assert results == []            # click accepted, nothing selected -> not a success
    assert surface.clicks == ["a1"]  # it did try once


async def test_group_selection_survives_handle_renumbering() -> None:
    surface = SwatchSurface(
        [swatch("a1", "Red", "g:colour"), swatch("a2", "Small", "g:size")],
        renumber=True,
    )
    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)
    assert sorted(r.chosen_label for r in results) == ["Red", "Small"]


async def test_native_selects_and_swatch_groups_both_satisfied_in_one_pass() -> None:
    # A product with a native <select> AND a swatch group — one call sets both.
    class Combined(SwatchSurface):
        def __init__(self) -> None:
            super().__init__([swatch("a2", "Red", "g:colour"), swatch("a3", "Blue", "g:colour")])
            self._sel_unset = True

        async def perceive(self) -> SurfaceView:
            base = await super().perceive()
            select = SurfaceAffordance(
                handle="a1", role="combobox", label="Size",
                value="Choose an option" if self._sel_unset else "Medium",
            )
            return SurfaceView(affordances=(select, *base.affordances))

        async def act(self, action: SurfaceAction) -> SurfaceView:
            if action.kind == "select" and self._sel_unset:
                self._sel_unset = False
                return await self.perceive()
            return await super().act(action)

    results = await FirstOptionSelectionSatisfier().satisfy_required(Combined())
    labels = {r.chosen_label for r in results}
    assert "Medium" in labels and "Red" in labels  # select + swatch group both set


# --- grouping + vocabulary, directly ----------------------------------------

def test_groups_partitions_by_group_id_then_contiguous_run() -> None:
    from zu_tools.selection import _groups

    v = SurfaceView(affordances=(
        SurfaceAffordance(handle="a1", role="radio", label="Red", group="g:colour"),
        SurfaceAffordance(handle="a2", role="radio", label="Blue", group="g:colour"),
        SurfaceAffordance(handle="a3", role="option", label="S"),   # ungrouped run
        SurfaceAffordance(handle="a4", role="option", label="M"),
    ))
    groups = _groups(v)
    assert {tuple(a.label for a in g) for g in groups} == {("Red", "Blue"), ("S", "M")}


def test_selection_vocab_is_single_sourced_with_zu_patterns() -> None:
    from zu_patterns._match import SELECTABLE_ROLES, SELECTED_STATES
    from zu_tools.selection import _SELECTABLE_ROLES, _SELECTED_STATES

    assert _SELECTED_STATES == set(SELECTED_STATES)         # the 'selected' vocabulary agrees
    assert _SELECTABLE_ROLES <= set(SELECTABLE_ROLES)       # no roles zu_patterns doesn't know
