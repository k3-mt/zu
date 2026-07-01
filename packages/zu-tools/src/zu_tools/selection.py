"""selection — deterministically satisfy product-variant pickers (#95, #110, #120).

A huge fraction of shops gate add-to-basket behind a product option — a
``<select>`` (colour / size / fitting) OR a custom swatch / radio group — whose
default is unselected. Until every such option is set, 'Add to basket' is disabled
and the click is a silent no-op — the single biggest hidden cause of 'couldn't add
to basket'. #39 shipped the ``VariantPicker`` PATTERN + a 'control became selected'
invariant; :class:`FirstOptionSelectionSatisfier` is the deterministic ACTION that
satisfies the options, as a :class:`~zu_core.ports.SelectionSatisfier`.

It drives the :class:`~zu_core.ports.ConnectedSurface` and handles BOTH shapes,
content-free throughout (role + selected-state + the structural GROUP id, never the
option labels):

  * Native ``<select>`` (role ``combobox``): a ``select`` act whose browser-side
    mechanic sets the FIRST VALID option — but only if the control is still unset
    (a placeholder has ``value === ""``), so it never overrides a real choice. We
    report each control whose value actually CHANGED.
  * Custom swatch / radio GROUPS (#120): the flat surface cannot say which swatches
    are colour vs size, so the reducer stamps each option with its enclosing group
    container's id (``SurfaceAffordance.group``). For each group with NO selected
    option we ``click`` its first enabled option and confirm it reached a SELECTED
    state (the #39 invariant); a silent no-op (click accepted, nothing selected) is
    a liveness violation — it is simply NOT reported, so the host still sees the
    group as unsatisfied rather than a false success.

Scope note (#110): it does NOT gate on the HTML ``required`` attribute — real
variant controls are gated by the shop's JS, not the attribute. Over-inclusion is
safe: a select is only set when the DOM reports it unset, a group only when NONE of
its options is selected, and only actual changes are reported.
"""

from __future__ import annotations

from zu_core.ports import ConnectedSurface, RequiredSelection, SurfaceAction
from zu_core.surface import SurfaceAffordance, SurfaceView

# A native single-choice <select> reduces to AX role 'combobox' — the shape whose
# browser-side option picker (which reads `element.options`) we drive with `select`.
_SINGLE_CHOICE_ROLES: frozenset[str] = frozenset({"combobox"})
# Roles of a custom swatch / radio-group OPTION (a subset of zu_patterns'
# SELECTABLE_ROLES; a drift test keeps them aligned). Anything already carrying a
# selected-style state also counts (a styled <div role=button> swatch, #120).
_SELECTABLE_ROLES: frozenset[str] = frozenset({"radio", "option", "tab", "swatch", "menuitemradio"})
# The state tokens that read as 'this option is the chosen one' — must match
# zu_patterns' SELECTED_STATES so the #39 'became selected' invariant agrees.
_SELECTED_STATES: frozenset[str] = frozenset({"selected", "checked", "pressed", "active", "aria-selected"})

_GroupKey = str | tuple[str, ...]


# --- native <select> path ---------------------------------------------------

def _is_select_candidate(a: SurfaceAffordance) -> bool:
    """A single-choice ``<select>`` worth attempting: a combobox that is not
    disabled. Not gated on the HTML ``required`` state (#110); the browser-side
    picker only sets a control the DOM reports as unset, and only actual value
    changes are reported."""
    return a.role in _SINGLE_CHOICE_ROLES and "disabled" not in a.states


def _select_candidates(view: SurfaceView) -> list[SurfaceAffordance]:
    return [a for a in view.affordances if _is_select_candidate(a)]


def _took(before: SurfaceAffordance, after: SurfaceAffordance) -> bool:
    """The select landed: a non-empty value that differs from before."""
    return bool(after.value) and after.value != before.value


# --- swatch / radio-group path ----------------------------------------------

def _norm_states(a: SurfaceAffordance) -> set[str]:
    # states may arrive as 'selected' or 'selected:true' — key off the token.
    return {s.split(":", 1)[0] for s in a.states}


def _is_selected(a: SurfaceAffordance) -> bool:
    return bool(_norm_states(a) & _SELECTED_STATES)


def _is_selectable(a: SurfaceAffordance) -> bool:
    return a.role.lower() in _SELECTABLE_ROLES or _is_selected(a)


def _groups(view: SurfaceView) -> list[list[SurfaceAffordance]]:
    """Partition the selectable options into single-choice GROUPS. Options sharing
    a structural ``group`` id are one group (the framework primitive, #120); an
    option with no group id falls back to a maximal contiguous run of the same
    role — the best a flat surface allows when the tree exposed no group container."""
    opts = [a for a in view.affordances if _is_selectable(a)]
    by_id: dict[str, list[SurfaceAffordance]] = {}
    ungrouped: list[SurfaceAffordance] = []
    for a in opts:
        if a.group:
            by_id.setdefault(a.group, []).append(a)
        else:
            ungrouped.append(a)
    groups: list[list[SurfaceAffordance]] = list(by_id.values())
    run: list[SurfaceAffordance] = []
    for a in ungrouped:
        if run and a.role == run[-1].role:
            run.append(a)
        else:
            if run:
                groups.append(run)
            run = [a]
    if run:
        groups.append(run)
    return groups


def _group_key(group: list[SurfaceAffordance]) -> _GroupKey:
    """A key that re-identifies a group across a re-render (handles renumber): its
    structural group id if it has one, else the tuple of its option labels."""
    return group[0].group or ("labels", *(a.label for a in group))


def _find_group(view: SurfaceView, key: _GroupKey) -> list[SurfaceAffordance] | None:
    for g in _groups(view):
        if _group_key(g) == key:
            return g
    if isinstance(key, tuple):  # label key: fall back to any run sharing a label
        want = set(key[1:])
        for g in _groups(view):
            if {a.label for a in g} & want:
                return g
    return None


class FirstOptionSelectionSatisfier:
    """The reference :class:`~zu_core.ports.SelectionSatisfier`."""

    __zu_interface__ = 1  # the selection_satisfiers interface major this targets
    name = "first_option_selection_satisfier"

    async def satisfy_required(self, surface: ConnectedSurface) -> list[RequiredSelection]:
        results = await self._satisfy_selects(surface)
        results.extend(await self._satisfy_groups(surface))
        return results

    async def _satisfy_selects(self, surface: ConnectedSurface) -> list[RequiredSelection]:
        # Address candidate <select>s by POSITION, not (role, label): on a real
        # product they routinely share one placeholder label ('Choose an option'),
        # and acting renumbers handles — so re-read each pass and act on the i-th by
        # its CURRENT handle. Bounded by the initial candidate count.
        view = await surface.perceive()
        count = len(_select_candidates(view))
        results: list[RequiredSelection] = []
        for i in range(count):
            candidates = _select_candidates(view)
            if i >= len(candidates):
                break
            before = candidates[i]
            view = await surface.act(SurfaceAction(handle=before.handle, kind="select", text=None))
            after = _select_candidates(view)
            chosen = after[i] if i < len(after) else None
            if chosen is not None and _took(before, chosen):
                results.append(
                    RequiredSelection(handle=chosen.handle, chosen_label=chosen.value or "")
                )
        return results

    async def _satisfy_groups(self, surface: ConnectedSurface) -> list[RequiredSelection]:
        view = await surface.perceive()
        # Snapshot the groups that still need a selection, by a stable key (handles
        # renumber after each click, group ids do not).
        pending = [_group_key(g) for g in _groups(view) if not any(_is_selected(a) for a in g)]
        results: list[RequiredSelection] = []
        for key in pending:
            view = await surface.perceive()
            group = _find_group(view, key)
            if group is None or any(_is_selected(a) for a in group):
                continue  # gone, or already satisfied by a shared re-render
            option = next((a for a in group if "disabled" not in a.states), None)
            if option is None:
                continue  # every option disabled (e.g. out of stock)
            view = await surface.act(SurfaceAction(handle=option.handle, kind="click"))
            after = _find_group(view, key)
            chosen = next((a for a in after if _is_selected(a)), None) if after else None
            if chosen is not None:
                results.append(RequiredSelection(handle=chosen.handle, chosen_label=chosen.label))
            # else: silent no-op — the click was accepted but nothing became selected
            # (a #39 liveness violation). Not reported, so the host still sees the
            # group as unsatisfied rather than a false success.
        return results
