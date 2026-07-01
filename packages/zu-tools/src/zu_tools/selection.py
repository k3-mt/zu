"""selection — deterministically satisfy required product-variant selects (#95).

A huge fraction of shops gate add-to-basket behind a REQUIRED product option (a
``<select>`` for colour / size / fitting whose default is an unselected
placeholder). Until every required option is set, 'Add to basket' is disabled and
the click is a silent no-op — the single biggest hidden cause of 'couldn't add to
basket'. #39 shipped the ``VariantPicker`` PATTERN + a 'control is now selected'
invariant; :class:`FirstOptionSelectionSatisfier` is the deterministic ACTION
that satisfies the options, as a :class:`~zu_core.ports.SelectionSatisfier`.

It drives the :class:`~zu_core.ports.ConnectedSurface`: for each REQUIRED
single-choice control it issues a ``select`` act, whose browser-side mechanic
sets the FIRST VALID option (placeholder/disabled skipped) — but ONLY if the
control is still unset, so it never overrides a choice that already took. It then
re-reads the surface and reports each control whose value actually CHANGED. This
keeps the "is it unset?" decision where it is truthful (the DOM: a placeholder
option has ``value === ""``) and keeps this side content-free — it chooses nothing
by prose; it reports the observable structural change.
"""

from __future__ import annotations

from zu_core.ports import ConnectedSurface, RequiredSelection, SurfaceAction
from zu_core.surface import SurfaceAffordance, SurfaceView

# A native single-choice <select> reduces to AX role 'combobox' — the shape this
# satisfier's browser-side option picker (which reads `element.options`) handles.
# Structural, never prose. ARIA listboxes and custom div/radio swatches (no
# `.options`) are the natural follow-up — see #95 — so they are deliberately out.
_SINGLE_CHOICE_ROLES: frozenset[str] = frozenset({"combobox"})


def _is_candidate(a: SurfaceAffordance) -> bool:
    """A required, enabled single-choice control. We do NOT guess 'unset' from the
    accessible value here (a placeholder's AX value is its prose, e.g. 'Choose an
    option', not ''): the ``select`` act only sets a control the DOM reports as
    unset, and we report only the ones whose value then changed — so this
    predicate can be lenient and stay content-free."""
    return (
        a.role in _SINGLE_CHOICE_ROLES
        and "required" in a.states
        and "disabled" not in a.states
    )


def _find(view: SurfaceView, role: str, label: str) -> SurfaceAffordance | None:
    """Re-find a control by (role, label) identity — handles renumber after each
    act re-renders, so a stale handle must not be trusted across acts."""
    for a in view.affordances:
        if a.role == role and a.label == label:
            return a
    return None


def _took(before: SurfaceAffordance, after: SurfaceAffordance) -> bool:
    """The select actually landed: the control now carries a non-empty value that
    differs from before (a placeholder → a real option)."""
    return bool(after.value) and after.value != before.value


class FirstOptionSelectionSatisfier:
    """The reference :class:`~zu_core.ports.SelectionSatisfier`."""

    __zu_interface__ = 1  # the selection_satisfiers interface major this targets
    name = "first_option_selection_satisfier"

    async def satisfy_required(self, surface: ConnectedSurface) -> list[RequiredSelection]:
        view = await surface.perceive()
        # Snapshot candidates by identity (role, label); re-find each before acting
        # since a prior act may have re-priced/re-rendered and renumbered handles.
        targets = [(a.role, a.label) for a in view.affordances if _is_candidate(a)]
        results: list[RequiredSelection] = []
        for role, label in targets:
            before = _find(view, role, label)
            if before is None:
                continue
            view = await surface.act(SurfaceAction(handle=before.handle, kind="select", text=None))
            after = _find(view, role, label)
            if after is not None and _took(before, after):
                results.append(
                    RequiredSelection(handle=after.handle, chosen_label=after.value or "")
                )
        return results
