"""#95 — FirstOptionSelectionSatisfier: satisfy required, unset variant selects.

The browser-side option picker (only-set-if-unset, first valid option) is JS that
cannot run offline, so the fake ConnectedSurface models exactly that DOM contract:
a ``select`` act sets a control only while it is unset. The tests then verify the
satisfier's orchestration — it targets required single-choice controls, sets each
unset one, reports what changed, and leaves the rest alone — plus direct unit
tests of the content-free candidate/took predicates.
"""

from __future__ import annotations

from typing import Any

from zu_core.ports import SelectionSatisfier, SurfaceAction
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_tools.selection import FirstOptionSelectionSatisfier, _is_candidate, _took


class VariantSurface:
    """In-memory ConnectedSurface over native <select>s. A ``select`` act sets a
    control's value to its first option ONLY while it is unset — mirroring
    ``_SELECT_FN``'s DOM check (a placeholder has ``value === ''``)."""

    def __init__(self, controls: list[dict[str, Any]]) -> None:
        self._controls = controls
        self.selected: list[str] = []  # handles that received a select act

    async def perceive(self) -> SurfaceView:
        affs = tuple(
            SurfaceAffordance(
                handle=c["handle"], role=c["role"], label=c["label"], value=c["value"],
                states=tuple(c.get("states", ())),
            )
            for c in self._controls
        )
        return SurfaceView(affordances=affs)

    async def act(self, action: SurfaceAction) -> SurfaceView:
        if action.kind == "select":
            self.selected.append(action.handle)
            for c in self._controls:
                if c["handle"] == action.handle and c.get("unset"):
                    c["value"] = c["first"]
                    c["unset"] = False
        return await self.perceive()


def _required(handle: str, label: str, *, unset: bool, value: str, first: str,
              disabled: bool = False) -> dict[str, Any]:
    states = ["required"] + (["disabled"] if disabled else [])
    return {"handle": handle, "role": "combobox", "label": label, "value": value,
            "states": states, "unset": unset, "first": first}


def test_satisfier_conforms_to_protocol() -> None:
    assert isinstance(FirstOptionSelectionSatisfier(), SelectionSatisfier)


async def test_sets_every_unset_required_select_and_reports_them() -> None:
    surface = VariantSurface([
        _required("a1", "Colour", unset=True, value="Choose an option", first="Red"),
        _required("a2", "Size", unset=True, value="Choose an option", first="S"),
        # Not required -> not a candidate, never touched.
        {"handle": "a3", "role": "combobox", "label": "Gift wrap", "value": "No",
         "states": (), "unset": True, "first": "Yes"},
        # Already set -> acted (idempotent) but reports no change.
        _required("a4", "Style", unset=False, value="Modern", first="Classic"),
        {"handle": "a5", "role": "button", "label": "Add to basket", "value": None,
         "states": (), "unset": False, "first": ""},
    ])

    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)

    chosen = {r.handle: r.chosen_label for r in results}
    assert chosen == {"a1": "Red", "a2": "S"}      # only the unset required selects
    assert "a3" not in surface.selected            # non-required never acted
    assert "a4" in surface.selected                # required-but-set acted, reported nothing


async def test_disabled_required_select_is_not_a_candidate() -> None:
    surface = VariantSurface([
        _required("a1", "Colour", unset=True, value="Choose", first="Red", disabled=True),
    ])
    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)
    assert results == []
    assert surface.selected == []  # disabled control is skipped entirely


async def test_no_required_selects_returns_empty() -> None:
    surface = VariantSurface([
        {"handle": "a1", "role": "button", "label": "Buy", "value": None,
         "states": (), "unset": False, "first": ""},
    ])
    assert await FirstOptionSelectionSatisfier().satisfy_required(surface) == []


# --- the content-free predicates, directly ----------------------------------

def _aff(role: str, *, states: tuple[str, ...] = (), value: str | None = None) -> SurfaceAffordance:
    return SurfaceAffordance(handle="h", role=role, label="Colour", value=value, states=states)


def test_is_candidate_requires_native_select_role_and_required_and_enabled() -> None:
    assert _is_candidate(_aff("combobox", states=("required",)))
    assert not _is_candidate(_aff("combobox"))                       # not required
    assert not _is_candidate(_aff("textbox", states=("required",)))  # wrong role
    assert not _is_candidate(_aff("listbox", states=("required",)))  # ARIA listbox: follow-up
    assert not _is_candidate(_aff("combobox", states=("required", "disabled")))


def test_took_is_a_nonempty_changed_value() -> None:
    before = _aff("combobox", value="Choose an option")
    assert _took(before, _aff("combobox", value="Red"))          # placeholder -> option
    assert not _took(before, _aff("combobox", value="Choose an option"))  # unchanged
    assert not _took(before, _aff("combobox", value=""))         # empty is not a real choice
