"""selection — deterministically satisfy product-variant selects (#95, #110).

A huge fraction of shops gate add-to-basket behind a product option (a
``<select>`` for colour / size / fitting whose default is an unselected
placeholder). Until every such option is set, 'Add to basket' is disabled and the
click is a silent no-op — the single biggest hidden cause of 'couldn't add to
basket'. #39 shipped the ``VariantPicker`` PATTERN + a 'control is now selected'
invariant; :class:`FirstOptionSelectionSatisfier` is the deterministic ACTION
that satisfies the options, as a :class:`~zu_core.ports.SelectionSatisfier`.

It drives the :class:`~zu_core.ports.ConnectedSurface`: for each single-choice
``<select>`` it issues a ``select`` act, whose browser-side mechanic sets the
FIRST VALID option (placeholder/disabled skipped) — but ONLY if the control is
still unset, so it never overrides a choice that already took. It then re-reads
the surface and reports each control whose value actually CHANGED. This keeps the
"is it unset?" decision where it is truthful (the DOM: a placeholder option has
``value === ""``) and keeps this side content-free — it chooses nothing by prose;
it reports the observable structural change.

Scope note (#110): it does NOT gate on the HTML ``required`` attribute. Real
variant selects (e.g. WooCommerce ``attribute_pa_*``) are gated by the shop's JS,
not ``required``, so requiring the attribute set zero on real products. Every
single-choice ``<select>`` is a candidate; over-inclusion is safe because the
browser-side picker only sets a control the DOM reports as unset, and only actual
changes are reported (a sort/already-chosen dropdown is a no-op, not a clobber).
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
    """A single-choice ``<select>`` worth attempting to satisfy: a combobox that
    is not disabled. We deliberately do NOT gate on the HTML ``required`` state —
    real variant selects are gated by the shop's JS, not the attribute (#110).
    Over-inclusion is safe: the browser-side picker only SETS a control the DOM
    reports as unset (``value === ""``), and only actual value changes are
    reported — so a sort/already-chosen dropdown is a no-op, never a clobber. The
    'is it unset?' judgement stays in the DOM, never guessed from the AX value
    (a placeholder's AX value is its prose, e.g. 'Choose an option', not '')."""
    return a.role in _SINGLE_CHOICE_ROLES and "disabled" not in a.states


def _candidates(view: SurfaceView) -> list[SurfaceAffordance]:
    return [a for a in view.affordances if _is_candidate(a)]


def _took(before: SurfaceAffordance, after: SurfaceAffordance) -> bool:
    """The select actually landed: the control now carries a non-empty value that
    differs from before (a placeholder → a real option)."""
    return bool(after.value) and after.value != before.value


class FirstOptionSelectionSatisfier:
    """The reference :class:`~zu_core.ports.SelectionSatisfier`."""

    __zu_interface__ = 1  # the selection_satisfiers interface major this targets
    name = "first_option_selection_satisfier"

    async def satisfy_required(self, surface: ConnectedSurface) -> list[RequiredSelection]:
        # Address the candidate selects by POSITION, not (role, label): on a real
        # product the variant selects routinely share one placeholder label
        # ('Choose an option'), so an identity match would keep re-finding the
        # first one. Acting re-prices/re-renders and renumbers handles, so we
        # re-read the current surface each pass and act on the i-th candidate by
        # its CURRENT handle. Bounded by the initial candidate count.
        view = await surface.perceive()
        count = len(_candidates(view))
        results: list[RequiredSelection] = []
        for i in range(count):
            candidates = _candidates(view)
            if i >= len(candidates):
                break  # the set shrank (a select became disabled/removed) — stop
            before = candidates[i]
            view = await surface.act(SurfaceAction(handle=before.handle, kind="select", text=None))
            after = _candidates(view)
            chosen = after[i] if i < len(after) else None
            if chosen is not None and _took(before, chosen):
                results.append(
                    RequiredSelection(handle=chosen.handle, chosen_label=chosen.value or "")
                )
        return results
