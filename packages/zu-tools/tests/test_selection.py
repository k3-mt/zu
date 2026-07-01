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
from zu_tools.selection import FirstOptionSelectionSatisfier, _is_candidate, _took


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


def test_is_candidate_is_any_enabled_native_select_not_gated_on_required() -> None:
    assert _is_candidate(_aff("combobox"))                     # #110: no `required` needed
    assert _is_candidate(_aff("combobox", states=("required",)))
    assert not _is_candidate(_aff("textbox"))                  # wrong role
    assert not _is_candidate(_aff("listbox"))                  # ARIA listbox: follow-up
    assert not _is_candidate(_aff("combobox", states=("disabled",)))


def test_took_is_a_nonempty_changed_value() -> None:
    before = _aff("combobox", value="Choose an option")
    assert _took(before, _aff("combobox", value="Red"))          # placeholder -> option
    assert not _took(before, _aff("combobox", value="Choose an option"))  # unchanged
    assert not _took(before, _aff("combobox", value=""))         # empty is not a real choice
